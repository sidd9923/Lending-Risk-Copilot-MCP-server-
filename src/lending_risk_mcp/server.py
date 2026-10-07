"""MCP adapter: exposes Copilot methods as tools, filtered by role.

The tool docstrings below are what Claude reads to decide which tool to call,
so they're written for the model: what it does, when to use it, what comes
back, and what the caveats are.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from .config import Settings
from .service import DEFAULT_HMDA_YEAR, Copilot

log = logging.getLogger("lending_risk_mcp")

INSTRUCTIONS = """\
Lending Risk Copilot: public mortgage-market data for lender risk review.

Sources: HMDA (loan application outcomes), CFPB consumer complaints, SEC filings,
FRED macro series, and Linear for review tickets.

Rules of thumb:
- Start with lender_risk_brief for an overview; drill down with the specific tools.
- Every result has a `status`. Treat `stale` as usable but say so. Treat
  `degraded`/`unavailable` as missing data: say what's missing, don't guess.
- Lender names are resolved fuzzily. If lender.*.confidence < 0.8, tell the user
  which entity you matched and ask them to confirm before drawing conclusions.
- Only create a review ticket when the user asks for one or confirms.
"""


@dataclass
class ToolSpec:
    name: str
    permission: str
    fn: Callable[..., Any]
    read_only: bool = True


def build_server(copilot: Copilot) -> MCPServer:
    server = MCPServer("lending-risk-copilot", instructions=INSTRUCTIONS, version="0.1.0")

    async def lender_risk_brief(
        lender: str,
        year: int = DEFAULT_HMDA_YEAR,
        months: int = 6,
        filing_keywords: list[str] | None = None,
    ) -> dict:
        """One-shot risk overview for a mortgage lender across every source you can access.

        Runs HMDA outcomes, CFPB mortgage complaint trend, SEC risk-factor excerpts and
        mortgage-rate context in parallel. Each section has its own status; `coverage`
        lists which sections succeeded. Use this first, then drill down.

        Args:
            lender: Lender name as the user said it, e.g. "Wells Fargo" or "Rocket".
            year: HMDA activity year (public data lags ~1 year).
            months: Complete months of complaint history (2-24).
            filing_keywords: Terms to pull from 10-K Risk Factors.
        """
        return await copilot.lender_risk_brief(lender, year, months, filing_keywords)

    async def resolve_lender(name: str) -> dict:
        """Resolve a lender name to its HMDA LEI, CFPB company name and SEC CIK.

        Returns each identifier with how it was resolved and a confidence score.
        Use when the user names a lender ambiguously, or to explain why numbers
        from different sources refer to different legal entities (bank vs holding co).
        """
        return await copilot.resolve_lender(name)

    async def hmda_summary(lender: str, year: int = DEFAULT_HMDA_YEAR) -> dict:
        """Mortgage application outcomes for a lender from HMDA public data.

        Returns application counts and loan volume by outcome (originated, denied,
        withdrawn...) and the denial rate (denied / decisioned applications).
        If the local warehouse is loaded, also returns top states by denials.
        Note: HMDA is reported under the bank subsidiary's LEI.
        """
        return await copilot.hmda_summary(lender, year)

    async def complaint_trend(lender: str, product: str | None = None, months: int = 6) -> dict:
        """Monthly CFPB consumer complaint counts for a lender, with a spike flag.

        `spike.flag` is true when the latest month is >= 1.5x the trailing average.
        product filters by CFPB product, e.g. "Mortgage", "Checking or savings account".
        Counts above 10,000 are lower bounds (flagged per month).
        """
        return await copilot.complaint_trend(lender, product, months)

    async def search_complaints(
        lender: str, query: str | None = None, product: str | None = None, limit: int = 10
    ) -> dict:
        """Most recent CFPB complaints for a lender, optionally full-text searched.

        Returns issue, sub-issue, state, company response and timeliness. Narratives
        are only included for roles with complaints:narratives; otherwise redacted.
        query searches narrative text, e.g. "escrow", "forbearance", "loan modification".
        """
        return await copilot.search_complaints(lender, query, product, limit)

    async def filing_risk_factors(
        lender: str, keywords: list[str] | None = None, form: str = "10-K"
    ) -> dict:
        """Excerpts from the Risk Factors section (Item 1A) of the lender's latest SEC filing.

        Returns filing metadata, links, and keyword-centered excerpts. Private lenders
        have no SEC filings. Large banks sometimes place Risk Factors in an exhibit; in
        that case status is `degraded` and you get the filing index link instead.
        """
        return await copilot.filing_risk_factors(lender, keywords, form)

    async def macro_series(series_id: str = "MORTGAGE30US", start: str | None = None) -> dict:
        """A FRED macro time series for context (last 60 observations + latest).

        Useful: MORTGAGE30US (30y mortgage rate), DRSFRMACBS (mortgage delinquency rate),
        RRVRUSQ156N (rental vacancy). start is YYYY-MM-DD.
        """
        return await copilot.macro_series(series_id, start)

    async def create_review_ticket(
        lender: str,
        title: str,
        summary: str,
        evidence: list[str] | None = None,
        priority: int = 2,
    ) -> dict:
        """Open a risk review ticket in Linear. Only call when the user asks or confirms.

        evidence: short bullet strings citing the figures behind the ticket.
        priority: 0 none, 1 urgent, 2 high, 3 medium, 4 low.
        If Linear is unreachable or not configured, the ticket is returned as a
        draft (created=false) and is NOT retried automatically.
        """
        return await copilot.create_review_ticket(lender, title, summary, evidence, priority)

    async def source_health() -> dict:
        """Which sources are configured and whether any circuit breakers are open."""
        return await copilot.source_health()

    specs = [
        ToolSpec("lender_risk_brief", "lender:resolve", lender_risk_brief),
        ToolSpec("resolve_lender", "lender:resolve", resolve_lender),
        ToolSpec("hmda_summary", "hmda:read", hmda_summary),
        ToolSpec("complaint_trend", "complaints:read", complaint_trend),
        ToolSpec("search_complaints", "complaints:read", search_complaints),
        ToolSpec("filing_risk_factors", "filings:read", filing_risk_factors),
        ToolSpec("macro_series", "macro:read", macro_series),
        ToolSpec("create_review_ticket", "tickets:create", create_review_ticket, read_only=False),
        ToolSpec("source_health", "admin:health", source_health),
    ]

    exposed = []
    for spec in specs:
        if not copilot.can(spec.permission):
            continue  # the model never sees tools this role can't use
        server.add_tool(
            spec.fn,
            name=spec.name,
            annotations=ToolAnnotations(
                read_only_hint=spec.read_only,
                destructive_hint=False,
                idempotent_hint=spec.read_only,
                open_world_hint=True,
            ),
        )
        exposed.append(spec.name)

    log.info("role=%s exposing %d tools: %s", copilot.principal.role, len(exposed), exposed)
    return server


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings.from_env()
    server = build_server(Copilot.from_settings(settings))
    server.run("stdio")


if __name__ == "__main__":
    main()
