"""FRED (St. Louis Fed) macro series. Needs a free API key.

Useful series for this use case:
  MORTGAGE30US  30-year fixed mortgage rate (weekly)
  DRSFRMACBS    delinquency rate, single-family residential mortgages (quarterly)
  RRVRUSQ156N   rental vacancy rate (quarterly)

Docs: https://fred.stlouisfed.org/docs/api/fred/
"""

from __future__ import annotations

from ..errors import NotConfigured, SourceResult, Status
from .base import BaseSource

OBS = "https://api.stlouisfed.org/fred/series/observations"

KNOWN_SERIES = {
    "MORTGAGE30US": "30-Year Fixed Rate Mortgage Average (%)",
    "DRSFRMACBS": "Delinquency Rate on Single-Family Residential Mortgages (%)",
    "RRVRUSQ156N": "Rental Vacancy Rate (%)",
}


class FredMacro(BaseSource):
    name = "fred"
    rate_per_s = 2.0

    def __init__(self, *args, api_key: str | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.api_key = api_key

    def configured(self) -> bool:
        return bool(self.api_key)

    async def series(self, series_id: str, start: str | None = None) -> SourceResult:
        if not self.api_key:
            raise NotConfigured(self.name, "set FRED_API_KEY (free at fred.stlouisfed.org)")
        params = {"series_id": series_id, "api_key": self.api_key, "file_type": "json"}
        if start:
            params["observation_start"] = start
        # Cache key deliberately excludes the API key so it never lands in logs.
        fetched = await self._get_json(OBS, params=params, cache_key=f"fred:{series_id}:{start}")
        obs = [
            {"date": o["date"], "value": float(o["value"])}
            for o in (fetched.data or {}).get("observations", [])
            if o.get("value") not in (None, ".")  # FRED uses "." for missing
        ]
        warnings = [fetched.note] if fetched.stale and fetched.note else []
        return SourceResult(
            source=self.name,
            status=Status.STALE if fetched.stale else Status.OK,
            data={
                "series_id": series_id,
                "title": KNOWN_SERIES.get(series_id, series_id),
                "observations": obs[-60:],
                "latest": obs[-1] if obs else None,
            },
            warnings=warnings,
        )
