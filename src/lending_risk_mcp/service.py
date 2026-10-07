"""The actual tool logic, independent of MCP.

server.py is a thin adapter that exposes these methods as MCP tools. Keeping
the logic here means it's testable without spinning up a protocol session,
and the same service could sit behind a REST API or a batch job.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import date, timedelta
from typing import Any

from .access import AuditLog, Policy, Principal
from .config import Settings
from .entities import Lender, LenderRegistry
from .errors import AccessDenied, NotConfigured, SourceError, SourceResult, Status
from .sources import CFPBComplaints, EdgarFilings, FredMacro, HmdaSource, LinearTickets

DEFAULT_HMDA_YEAR = 2023  # most recent full public snapshot at time of writing; override per call


class Copilot:
    def __init__(
        self,
        *,
        principal: Principal,
        policy: Policy,
        registry: LenderRegistry,
        hmda: HmdaSource,
        cfpb: CFPBComplaints,
        edgar: EdgarFilings,
        fred: FredMacro,
        linear: LinearTickets,
        audit: AuditLog | None = None,
        today: Callable[[], date] = date.today,
    ):
        self.principal = principal
        self.policy = policy
        self.registry = registry
        self.hmda, self.cfpb, self.edgar, self.fred, self.linear = hmda, cfpb, edgar, fred, linear
        self.audit = audit or AuditLog(None)
        self.today = today

    @classmethod
    def from_settings(cls, s: Settings) -> Copilot:
        kw = {"cache_ttl_s": s.cache_ttl_s, "timeout_s": s.http_timeout_s}
        return cls(
            principal=Principal(s.user, s.role),
            policy=Policy.load(s.policy_path),
            registry=LenderRegistry.load(s.registry_path),
            hmda=HmdaSource(db_path=s.hmda_db_path, **kw),
            cfpb=CFPBComplaints(**kw),
            edgar=EdgarFilings(user_agent=s.sec_user_agent, **kw),
            fred=FredMacro(api_key=s.fred_api_key, **kw),
            linear=LinearTickets(api_key=s.linear_api_key, team_id=s.linear_team_id, **kw),
            audit=AuditLog(s.audit_log_path),
        )

    # ------------------------------------------------------------------ plumbing

    def can(self, permission: str) -> bool:
        return self.policy.allows(self.principal, permission)

    async def _run(
        self, tool: str, permission: str, args: dict, work: Callable[[], Awaitable[Any]]
    ) -> dict:
        start = time.perf_counter()
        outcome = "ok"
        try:
            self.policy.require(self.principal, permission)
            result = await work()
            if isinstance(result, SourceResult):
                outcome = result.status.value
                return result.model_dump(mode="json")
            return result
        except AccessDenied as exc:
            outcome = "denied"
            return {"status": "denied", "error": str(exc)}
        except NotConfigured as exc:
            outcome = "not_configured"
            return SourceResult.unavailable(exc.source, exc.message).model_dump(mode="json")
        except SourceError as exc:
            outcome = "unavailable"
            return SourceResult.unavailable(exc.source, exc.message).model_dump(mode="json")
        finally:
            self.audit.record(
                self.principal, tool, args, outcome, (time.perf_counter() - start) * 1000
            )

    async def _lender(self, name: str, year: int = DEFAULT_HMDA_YEAR) -> Lender:
        return await self.registry.resolve(
            name, hmda=self.hmda, cfpb=self.cfpb, edgar=self.edgar, hmda_year=year
        )

    def month_windows(self, months: int) -> list[tuple[date, date]]:
        """Last `months` *complete* calendar months, oldest first."""
        first_of_this_month = self.today().replace(day=1)
        windows = []
        end = first_of_this_month - timedelta(days=1)
        for _ in range(months):
            start = end.replace(day=1)
            windows.append((start, end))
            end = start - timedelta(days=1)
        return list(reversed(windows))

    # ------------------------------------------------------------------ tools

    async def resolve_lender(self, name: str) -> dict:
        async def work():
            lender = await self._lender(name)
            return {"status": "ok", "lender": lender.to_dict()}

        return await self._run("resolve_lender", "lender:resolve", {"name": name}, work)

    async def hmda_summary(self, lender: str, year: int = DEFAULT_HMDA_YEAR) -> dict:
        async def work():
            ent = await self._lender(lender, year)
            if not ent.lei.value:
                return SourceResult.unavailable(
                    "hmda", f"couldn't resolve an HMDA LEI for '{lender}'"
                )
            res = await self.hmda.lender_summary(str(ent.lei.value), year)
            res.data = {
                **(res.data or {}),
                "lender": ent.display_name,
                "lei_confidence": ent.lei.confidence,
            }
            return res

        return await self._run("hmda_summary", "hmda:read", {"lender": lender, "year": year}, work)

    async def complaint_trend(
        self, lender: str, product: str | None = None, months: int = 6
    ) -> dict:
        months = max(2, min(months, 24))

        async def work():
            ent = await self._lender(lender)
            if not ent.cfpb_company.value:
                return SourceResult.unavailable(
                    "cfpb_complaints", f"no CFPB company match for '{lender}'"
                )
            return await self.cfpb.monthly_trend(
                str(ent.cfpb_company.value), self.month_windows(months), product
            )

        return await self._run(
            "complaint_trend",
            "complaints:read",
            {"lender": lender, "product": product, "months": months},
            work,
        )

    async def search_complaints(
        self, lender: str, query: str | None = None, product: str | None = None, limit: int = 10
    ) -> dict:
        async def work():
            ent = await self._lender(lender)
            if not ent.cfpb_company.value:
                return SourceResult.unavailable(
                    "cfpb_complaints", f"no CFPB company match for '{lender}'"
                )
            return await self.cfpb.search(
                str(ent.cfpb_company.value),
                query=query,
                product=product,
                limit=limit,
                include_narratives=self.can("complaints:narratives"),
            )

        return await self._run(
            "search_complaints",
            "complaints:read",
            {"lender": lender, "query": query, "product": product},
            work,
        )

    async def filing_risk_factors(
        self, lender: str, keywords: list[str] | None = None, form: str = "10-K"
    ) -> dict:
        async def work():
            ent = await self._lender(lender)
            if not ent.cik.value:
                return SourceResult.unavailable(
                    "sec_edgar", f"no SEC CIK for '{lender}' (private lender?)"
                )
            return await self.edgar.risk_factors(int(ent.cik.value), keywords=keywords, form=form)

        return await self._run(
            "filing_risk_factors",
            "filings:read",
            {"lender": lender, "keywords": keywords, "form": form},
            work,
        )

    async def macro_series(self, series_id: str = "MORTGAGE30US", start: str | None = None) -> dict:
        return await self._run(
            "macro_series",
            "macro:read",
            {"series_id": series_id},
            lambda: self.fred.series(series_id, start),
        )

    async def lender_risk_brief(
        self,
        lender: str,
        year: int = DEFAULT_HMDA_YEAR,
        months: int = 6,
        filing_keywords: list[str] | None = None,
    ) -> dict:
        """Fan out to every source the caller is allowed to use, in parallel.

        One source failing never sinks the brief: each section reports its own
        status, and `coverage` tells the model exactly what it's missing.
        """
        keywords = filing_keywords or ["mortgage", "servicing", "complaint", "regulatory"]

        async def work():
            ent = await self._lender(lender, year)
            plan: dict[str, tuple[str, Callable[[], Awaitable[dict]]]] = {
                "lending_outcomes": ("hmda:read", lambda: self.hmda_summary(lender, year)),
                "complaint_trend": (
                    "complaints:read",
                    lambda: self.complaint_trend(lender, "Mortgage", months),
                ),
                "filing_risk_factors": (
                    "filings:read",
                    lambda: self.filing_risk_factors(lender, keywords),
                ),
                "macro_context": ("macro:read", lambda: self.macro_series("MORTGAGE30US")),
            }
            allowed = {k: fn for k, (perm, fn) in plan.items() if self.can(perm)}
            results = await asyncio.gather(
                *(fn() for fn in allowed.values()), return_exceptions=True
            )

            sections: dict[str, Any] = {}
            for key, res in zip(allowed, results, strict=True):
                if isinstance(res, BaseException):
                    sections[key] = SourceResult.unavailable(
                        key, f"unexpected error: {res}"
                    ).model_dump(mode="json")
                else:
                    sections[key] = res
            for key in plan.keys() - allowed.keys():
                sections[key] = {
                    "status": "not_permitted",
                    "warnings": [f"role '{self.principal.role}' can't read this"],
                }

            statuses = {k: v.get("status") for k, v in sections.items()}
            ok = [k for k, s in statuses.items() if s == "ok"]
            overall = (
                Status.OK
                if len(ok) == len(allowed)
                else (
                    Status.UNAVAILABLE
                    if not ok and not any(s == "stale" for s in statuses.values())
                    else Status.DEGRADED
                )
            )
            return {
                "status": overall.value,
                "lender": ent.to_dict(),
                "coverage": statuses,
                "sections": sections,
                "note": "Sections are independent. Cite only sections with status ok/stale, "
                "and say which ones are missing.",
            }

        return await self._run(
            "lender_risk_brief",
            "lender:resolve",
            {"lender": lender, "year": year, "months": months},
            work,
        )

    async def create_review_ticket(
        self,
        lender: str,
        title: str,
        summary: str,
        evidence: list[str] | None = None,
        priority: int = 2,
    ) -> dict:
        async def work():
            ent = await self._lender(lender)
            lines = [
                f"**Lender:** {ent.display_name}",
                f"**Opened by:** {self.principal.user} via Lending Risk Copilot",
                "",
                summary.strip(),
            ]
            if evidence:
                lines += ["", "**Evidence**", *[f"- {e}" for e in evidence]]
            lines += ["", "_Figures are from public HMDA/CFPB/SEC data; verify before acting._"]
            return await self.linear.create_issue(
                f"[Risk review] {ent.display_name}: {title}",
                "\n".join(lines),
                priority=max(0, min(priority, 4)),
            )

        return await self._run(
            "create_review_ticket", "tickets:create", {"lender": lender, "title": title}, work
        )

    async def source_health(self) -> dict:
        async def work():
            return {
                "status": "ok",
                "principal": {"user": self.principal.user, "role": self.principal.role},
                "sources": [
                    s.health() for s in (self.hmda, self.cfpb, self.edgar, self.fred, self.linear)
                ],
            }

        return await self._run("source_health", "admin:health", {}, work)

    async def aclose(self) -> None:
        for s in (self.hmda, self.cfpb, self.edgar, self.fred, self.linear):
            await s.aclose()
