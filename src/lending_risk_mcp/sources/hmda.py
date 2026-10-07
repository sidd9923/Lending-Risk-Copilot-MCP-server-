"""HMDA (Home Mortgage Disclosure Act) loan application data.

Two paths, on purpose:
  * Local warehouse: SQLite built from the public LAR snapshot by
    scripts/load_hmda.py. Fast, no rate limits, and it's "the database we
    control" -- this is how a real client deployment would look.
  * FFIEC Data Browser API: used when a lender/year isn't in the local DB.

action_taken codes: 1 originated, 2 approved not accepted, 3 denied,
4 withdrawn, 5 incomplete, 6 purchased, 7/8 preapproval denied/approved.
Denial rate here = denied / (originated + approved-not-accepted + denied),
which is the usual "decisioned applications" denominator.

Docs: https://ffiec.cfpb.gov/documentation/api/data-browser/
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ..errors import SourceResult, Status
from .base import BaseSource

API = "https://ffiec.cfpb.gov/v2/data-browser-api/view/nationwide/aggregations"
FILERS = "https://ffiec.cfpb.gov/v2/reporting/filers/{year}"

ACTION_LABELS = {
    "1": "originated",
    "2": "approved_not_accepted",
    "3": "denied",
    "4": "withdrawn",
    "5": "incomplete",
    "6": "purchased",
}


class HmdaSource(BaseSource):
    name = "hmda"
    rate_per_s = 2.0  # the data browser is slow; be polite

    def __init__(self, *args, db_path: Path | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.db_path = db_path

    def health(self) -> dict:
        h = super().health()
        h["local_warehouse"] = bool(self.db_path and self.db_path.exists())
        return h

    async def filers(self, year: int) -> list[dict]:
        """All institutions that filed HMDA for a year: [{lei, name, ...}]."""
        fetched = await self._get_json(FILERS.format(year=year))
        return (fetched.data or {}).get("institutions", [])

    async def lender_summary(self, lei: str, year: int) -> SourceResult:
        local = self._from_local(lei, year)
        if local is not None:
            return SourceResult.ok(self.name, {**local, "served_from": "local_warehouse"})

        fetched = await self._get_json(
            API, params={"years": str(year), "leis": lei, "actions_taken": "1,2,3,4,5,6"}
        )
        counts: dict[str, int] = {}
        volume: dict[str, float] = {}
        for row in (fetched.data or {}).get("aggregations", []):
            code = str(row.get("actions_taken", ""))
            label = ACTION_LABELS.get(code)
            if label:
                counts[label] = int(row.get("count", 0))
                volume[label] = float(row.get("sum", 0.0))

        data = {**_summarize(lei, year, counts, volume), "served_from": "ffiec_api"}
        warnings = []
        if not counts:
            warnings.append(f"no HMDA records for LEI {lei} in {year} (wrong LEI, or not a filer)")
        if fetched.stale and fetched.note:
            warnings.append(fetched.note)
        return SourceResult(
            source=self.name,
            status=Status.STALE if fetched.stale else Status.OK,
            data=data,
            warnings=warnings,
        )

    def _from_local(self, lei: str, year: int) -> dict | None:
        if not self.db_path or not self.db_path.exists():
            return None
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                """SELECT action_taken, COUNT(*), COALESCE(SUM(loan_amount), 0)
                   FROM lar WHERE lei = ? AND activity_year = ?
                   GROUP BY action_taken""",
                (lei, year),
            ).fetchall()
            if not rows:
                return None
            counts, volume = {}, {}
            for code, n, amt in rows:
                label = ACTION_LABELS.get(str(code))
                if label:
                    counts[label] = n
                    volume[label] = float(amt)
            top_states = conn.execute(
                """SELECT state_code, COUNT(*) AS n FROM lar
                   WHERE lei = ? AND activity_year = ? AND action_taken = 3
                   GROUP BY state_code ORDER BY n DESC LIMIT 5""",
                (lei, year),
            ).fetchall()
        summary = _summarize(lei, year, counts, volume)
        summary["top_denial_states"] = [{"state": s, "denials": n} for s, n in top_states]
        return summary


def _summarize(lei: str, year: int, counts: dict, volume: dict) -> dict:
    decisioned = sum(counts.get(k, 0) for k in ("originated", "approved_not_accepted", "denied"))
    denial_rate = round(counts.get("denied", 0) / decisioned, 4) if decisioned else None
    return {
        "lei": lei,
        "year": year,
        "applications_by_action": counts,
        "loan_volume_by_action_usd": volume,
        "decisioned_applications": decisioned,
        "denial_rate": denial_rate,
    }
