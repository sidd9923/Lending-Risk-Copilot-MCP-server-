"""Entity resolution: one lender, three different identities.

This is the least glamorous and most important part of the server. If
"Wells Fargo" resolves to the wrong LEI, every number downstream is wrong
and looks perfectly confident. So every resolution carries:
  * how it was resolved (registry pin vs runtime lookup vs fuzzy match)
  * a confidence score
  * what to show a human if it's ambiguous
"""

from __future__ import annotations

import difflib
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from .sources import CFPBComplaints, EdgarFilings, HmdaSource

_SUFFIXES = re.compile(
    r"\b(national association|n\.?a\.?|inc\.?|incorporated|llc|l\.l\.c\.|corp\.?|corporation|"
    r"co\.?|company|holdings?|group|financial|bancorp|/[a-z]{2})\b"
)
_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")


def normalize(name: str) -> str:
    s = name.lower().replace("&", " ")
    s = _SUFFIXES.sub(" ", s)
    s = _NON_ALNUM.sub(" ", s)
    # "Bank & Trust" and "Bank and Trust" should match, and "X & Company" -> "x".
    return " ".join(t for t in s.split() if t != "and")


def similarity(a: str, b: str) -> float:
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    seq = difflib.SequenceMatcher(None, na, nb).ratio()
    ta, tb = set(na.split()), set(nb.split())
    jacc = len(ta & tb) / len(ta | tb)
    return round(max(seq, jacc), 3)


@dataclass
class Identity:
    value: str | int | None
    method: str  # "registry" | "runtime_lookup" | "unresolved"
    confidence: float
    matched_name: str | None = None


@dataclass
class Lender:
    id: str
    display_name: str
    lei: Identity
    cik: Identity
    cfpb_company: Identity
    alternatives: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class LenderRegistry:
    def __init__(self, entries: list[dict]):
        self.entries = entries
        self._runtime_cache: dict[str, Lender] = {}

    @classmethod
    def load(cls, path: Path) -> LenderRegistry:
        with open(path) as f:
            return cls(yaml.safe_load(f).get("lenders", []))

    def match(self, query: str) -> tuple[dict | None, float, list[str]]:
        """Best registry entry for a free-text name, its score, and runner-ups."""
        scored = []
        for e in self.entries:
            candidates = [e["display_name"], *e.get("aliases", [])]
            if e.get("cfpb_company"):
                candidates.append(e["cfpb_company"])
            scored.append((max(similarity(query, c) for c in candidates), e))
        scored.sort(key=lambda x: x[0], reverse=True)
        if not scored or scored[0][0] < 0.6:
            return None, scored[0][0] if scored else 0.0, [e["display_name"] for _, e in scored[:3]]
        best_score, best = scored[0]
        runner_ups = [e["display_name"] for s, e in scored[1:4] if best_score - s < 0.1]
        return best, best_score, runner_ups

    async def resolve(
        self,
        query: str,
        *,
        hmda: HmdaSource | None = None,
        cfpb: CFPBComplaints | None = None,
        edgar: EdgarFilings | None = None,
        hmda_year: int = 2023,
    ) -> Lender:
        key = f"{normalize(query)}:{hmda_year}"
        if key in self._runtime_cache:
            return self._runtime_cache[key]

        entry, score, alts = self.match(query)
        if entry is None:
            lender = Lender(
                id=normalize(query).replace(" ", "_") or "unknown",
                display_name=query,
                lei=Identity(None, "unresolved", 0.0),
                cik=Identity(None, "unresolved", 0.0),
                cfpb_company=Identity(None, "unresolved", 0.0),
                alternatives=alts,
            )
            # Not in the registry: still try runtime lookups on the raw name.
            entry = {"display_name": query, "hmda_name_hint": query}
        else:
            lender = Lender(
                id=entry["id"],
                display_name=entry["display_name"],
                lei=_pinned(entry.get("lei"), score),
                cik=_pinned(entry.get("cik"), score),
                cfpb_company=_pinned(entry.get("cfpb_company"), score),
                alternatives=alts,
            )

        if lender.lei.value is None and hmda is not None:
            lender.lei = await _lookup_lei(hmda, entry.get("hmda_name_hint") or query, hmda_year)
        if lender.cfpb_company.value is None and cfpb is not None:
            lender.cfpb_company = await _lookup_cfpb(cfpb, entry["display_name"])
        if lender.cik.value is None and edgar is not None and edgar.configured():
            lender.cik = await _lookup_cik(edgar, entry["display_name"])

        self._runtime_cache[key] = lender
        return lender


def _pinned(value, score: float) -> Identity:
    if value is None:
        return Identity(None, "unresolved", 0.0)
    return Identity(value, "registry", score)


async def _lookup_lei(hmda: HmdaSource, hint: str, year: int) -> Identity:
    try:
        filers = await hmda.filers(year)
    except Exception as exc:
        return Identity(None, "unresolved", 0.0, matched_name=f"lookup failed: {exc}")
    best = max(filers, key=lambda f: similarity(hint, f.get("name", "")), default=None)
    if not best:
        return Identity(None, "unresolved", 0.0)
    conf = similarity(hint, best.get("name", ""))
    if conf < 0.6:
        return Identity(None, "unresolved", conf, matched_name=best.get("name"))
    return Identity(best["lei"], "runtime_lookup", conf, matched_name=best.get("name"))


async def _lookup_cfpb(cfpb: CFPBComplaints, name: str) -> Identity:
    try:
        suggestions = await cfpb.suggest_company(name)
    except Exception as exc:
        return Identity(None, "unresolved", 0.0, matched_name=f"lookup failed: {exc}")
    best = max(suggestions, key=lambda s: similarity(name, s), default=None)
    if not best:
        return Identity(None, "unresolved", 0.0)
    conf = similarity(name, best)
    method = "runtime_lookup" if conf >= 0.6 else "unresolved"
    return Identity(best if conf >= 0.6 else None, method, conf, matched_name=best)


async def _lookup_cik(edgar: EdgarFilings, name: str) -> Identity:
    try:
        hits = await edgar.search_cik(name)
    except Exception as exc:
        return Identity(None, "unresolved", 0.0, matched_name=f"lookup failed: {exc}")
    if not hits:
        return Identity(None, "unresolved", 0.0)
    best = max(hits, key=lambda h: similarity(name, h["title"]))
    conf = similarity(name, best["title"])
    if conf < 0.6:
        return Identity(None, "unresolved", conf, matched_name=best["title"])
    return Identity(best["cik"], "runtime_lookup", conf, matched_name=best["title"])
