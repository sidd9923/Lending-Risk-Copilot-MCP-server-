"""SEC EDGAR filings.

Friction worth knowing about:
  * SEC fair-access policy: max 10 requests/sec and a descriptive User-Agent
    with contact info, or you get 403'd. We cap at 8/s and refuse to run
    without SEC_USER_AGENT set.
  * Big banks often put Risk Factors in an exhibit (the annual report, EX-13)
    rather than the 10-K primary document. When we can't find Item 1A we say
    so and hand back the filing index URL instead of pretending.

Docs: https://www.sec.gov/search-filings/edgar-application-programming-interfaces
"""

from __future__ import annotations

import html
import re

from ..errors import NotConfigured, SourceResult, Status
from .base import BaseSource

TICKERS = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
INDEX = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/"

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_ITEM_1A = re.compile(r"item\s*1a\.?\s*[\-–—:]?\s*risk\s+factors", re.IGNORECASE)
_ITEM_1B = re.compile(r"item\s*1b\.?|item\s*2\.?\s*[\-–—:]?\s*properties", re.IGNORECASE)


class EdgarFilings(BaseSource):
    name = "sec_edgar"
    rate_per_s = 8.0

    def __init__(self, *args, user_agent: str | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user_agent = user_agent

    def configured(self) -> bool:
        return bool(self.user_agent)

    def _headers(self) -> dict:
        if not self.user_agent:
            raise NotConfigured(self.name, "set SEC_USER_AGENT='Your Name you@example.com'")
        return {"User-Agent": self.user_agent, "Accept-Encoding": "gzip, deflate"}

    async def search_cik(self, name: str) -> list[dict]:
        fetched = await self._get_json(TICKERS, headers=self._headers())
        needle = name.lower()
        out = []
        for row in (fetched.data or {}).values():
            title = str(row.get("title", ""))
            if needle in title.lower():
                out.append(
                    {"cik": int(row["cik_str"]), "ticker": row.get("ticker"), "title": title}
                )
        return out[:10]

    async def latest_filing(self, cik: int, form: str = "10-K") -> dict | None:
        fetched = await self._get_json(SUBMISSIONS.format(cik=cik), headers=self._headers())
        recent = (fetched.data or {}).get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        for i, f in enumerate(forms):
            if f == form:
                acc = recent["accessionNumber"][i]
                acc_nodash = acc.replace("-", "")
                doc = recent["primaryDocument"][i]
                return {
                    "company": (fetched.data or {}).get("name"),
                    "cik": cik,
                    "form": form,
                    "accession_number": acc,
                    "filing_date": recent["filingDate"][i],
                    "document_url": ARCHIVE.format(cik=cik, acc=acc_nodash, doc=doc),
                    "index_url": INDEX.format(cik=cik, acc=acc_nodash),
                }
        return None

    async def risk_factors(
        self, cik: int, keywords: list[str] | None = None, form: str = "10-K", max_chars: int = 6000
    ) -> SourceResult:
        filing = await self.latest_filing(cik, form)
        if not filing:
            return SourceResult.unavailable(self.name, f"no {form} found for CIK {cik}")

        fetched = await self._get_text(filing["document_url"], headers=self._headers())
        text = _to_text(fetched.data or "")
        section = _extract_risk_factors(text)
        warnings = [fetched.note] if fetched.stale and fetched.note else []

        if section is None:
            warnings.append(
                "Item 1A not found in the primary document; it's probably in an exhibit "
                "(often EX-13). Check index_url."
            )
            return SourceResult(
                source=self.name,
                status=Status.DEGRADED,
                data={"filing": filing, "excerpts": []},
                warnings=warnings,
            )

        excerpts = _keyword_excerpts(section, keywords) if keywords else [section[:max_chars]]
        if keywords and not excerpts:
            warnings.append(f"none of {keywords} appear in Risk Factors")
        return SourceResult(
            source=self.name,
            status=Status.STALE if fetched.stale else Status.OK,
            data={"filing": filing, "section_chars": len(section), "excerpts": excerpts},
            warnings=warnings,
        )


def _to_text(raw_html: str) -> str:
    raw_html = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw_html)
    return _WS.sub(" ", html.unescape(_TAG.sub(" ", raw_html))).strip()


def _extract_risk_factors(text: str) -> str | None:
    # The table of contents mentions "Item 1A Risk Factors" too, so take the
    # match that's followed by the longest section.
    best = None
    for m in _ITEM_1A.finditer(text):
        end = _ITEM_1B.search(text, m.end())
        chunk = text[m.end() : end.start() if end else m.end() + 200_000]
        if best is None or len(chunk) > len(best):
            best = chunk
    return best.strip() if best and len(best) > 500 else None


def _keyword_excerpts(
    section: str, keywords: list[str], window: int = 400, cap: int = 8
) -> list[str]:
    out = []
    lowered = section.lower()
    for kw in keywords:
        for m in re.finditer(re.escape(kw.lower()), lowered):
            start, end = max(0, m.start() - window), min(len(section), m.end() + window)
            out.append(f"[{kw}] ...{section[start:end]}...")
            if len(out) >= cap:
                return out
    return out
