"""CFPB Consumer Complaint Database.

Public, no key. Elasticsearch-shaped responses. The annoying parts:
  * `hits.total.value` caps at 10,000 with relation "gte", so big banks'
    counts are lower bounds unless you narrow the date range.
  * Company names are the CFPB's own spelling ("WELLS FARGO & COMPANY"),
    which matches neither HMDA nor EDGAR. See entities.py.
  * Narratives only exist when the consumer opted in, and they're scrubbed
    but still sensitive. We gate them behind a permission.

Docs: https://cfpb.github.io/api/ccdb/
"""

from __future__ import annotations

from datetime import date

from ..errors import SourceResult, Status
from .base import BaseSource

BASE = "https://www.consumerfinance.gov/data-research/consumer-complaints/search/api/v1/"


class CFPBComplaints(BaseSource):
    name = "cfpb_complaints"
    rate_per_s = 4.0

    async def suggest_company(self, text: str) -> list[str]:
        fetched = await self._get_json(BASE + "_suggest_company", params={"text": text})
        data = fetched.data
        return [str(x) for x in data] if isinstance(data, list) else []

    async def count(
        self,
        company: str,
        *,
        product: str | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> tuple[int, bool, bool]:
        """Return (count, is_lower_bound, served_stale)."""
        params = {"company": company, "size": 1, "no_aggs": "true", "format": "json"}
        if product:
            params["product"] = product
        if start:
            params["date_received_min"] = start.isoformat()
        if end:
            params["date_received_max"] = end.isoformat()
        fetched = await self._get_json(BASE, params=params)
        total = (fetched.data or {}).get("hits", {}).get("total", {})
        if isinstance(total, int):  # older API shape
            return total, False, fetched.stale
        return int(total.get("value", 0)), total.get("relation") == "gte", fetched.stale

    async def monthly_trend(
        self, company: str, months: list[tuple[date, date]], product: str | None = None
    ) -> SourceResult:
        rows, warnings, stale = [], [], False
        for start, end in months:
            n, lower_bound, was_stale = await self.count(
                company, product=product, start=start, end=end
            )
            stale |= was_stale
            rows.append(
                {"month": start.strftime("%Y-%m"), "complaints": n, "lower_bound": lower_bound}
            )
            if lower_bound:
                warnings.append(f"{start:%Y-%m}: count capped by API, treat as lower bound")
        if stale:
            warnings.append("some months served from cache because CFPB was unreachable")
        return SourceResult(
            source=self.name,
            status=Status.STALE if stale else Status.OK,
            data={"company": company, "product": product, "series": rows, "spike": _spike(rows)},
            warnings=warnings,
        )

    async def search(
        self,
        company: str,
        *,
        query: str | None = None,
        product: str | None = None,
        limit: int = 10,
        include_narratives: bool = False,
    ) -> SourceResult:
        params = {
            "company": company,
            "size": max(1, min(limit, 50)),
            "sort": "created_date_desc",
            "no_aggs": "true",
            "format": "json",
        }
        if query:
            params["search_term"] = query
            params["field"] = "complaint_what_happened"
            params["has_narrative"] = "true"
        if product:
            params["product"] = product

        fetched = await self._get_json(BASE, params=params)
        hits = (fetched.data or {}).get("hits", {}).get("hits", [])
        complaints = []
        for h in hits:
            src = h.get("_source", {})
            item = {
                "complaint_id": src.get("complaint_id"),
                "date_received": src.get("date_received"),
                "product": src.get("product"),
                "sub_product": src.get("sub_product"),
                "issue": src.get("issue"),
                "sub_issue": src.get("sub_issue"),
                "state": src.get("state"),
                "company_response": src.get("company_response"),
                "timely": src.get("timely"),
            }
            narrative = src.get("complaint_what_happened")
            if include_narratives:
                item["narrative"] = narrative
            else:
                item["narrative"] = (
                    "[redacted: requires complaints:narratives]" if narrative else None
                )
            complaints.append(item)

        warnings = [fetched.note] if fetched.stale and fetched.note else []
        return SourceResult(
            source=self.name,
            status=Status.STALE if fetched.stale else Status.OK,
            data={"company": company, "returned": len(complaints), "complaints": complaints},
            warnings=warnings,
        )


def _spike(rows: list[dict]) -> dict | None:
    """Flag the latest month if it's well above the trailing average. Simple on purpose."""
    if len(rows) < 4:
        return None
    *history, latest = [r["complaints"] for r in rows]
    baseline = sum(history) / len(history)
    if baseline == 0:
        return None
    ratio = latest / baseline
    return {
        "latest": latest,
        "trailing_avg": round(baseline, 1),
        "ratio": round(ratio, 2),
        "flag": ratio >= 1.5,
    }
