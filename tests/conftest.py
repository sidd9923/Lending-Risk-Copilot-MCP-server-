from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from lending_risk_mcp import resilience
from lending_risk_mcp.access import AuditLog, Policy, Principal
from lending_risk_mcp.entities import LenderRegistry
from lending_risk_mcp.service import Copilot
from lending_risk_mcp.sources import (
    CFPBComplaints,
    EdgarFilings,
    FredMacro,
    HmdaSource,
    LinearTickets,
)
from lending_risk_mcp.sources.cfpb import BASE as CFPB_BASE

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).resolve().parent / "fixtures"

WF_LEI = "KB1H1DSPRFMYMCUFXT09"


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    """Retries are tested for behavior, not for wall-clock time."""
    monkeypatch.setattr(resilience, "_backoff", lambda *a, **k: 0.0)


@pytest.fixture
def policy() -> Policy:
    return Policy.load(ROOT / "config" / "policy.yaml")


@pytest.fixture
def registry() -> LenderRegistry:
    return LenderRegistry.load(ROOT / "data" / "lender_registry.yaml")


@pytest.fixture
def mock_api():
    with respx.mock(assert_all_called=False) as router:
        yield router


def make_copilot(
    policy, registry, role="analyst", *, linear_key="lin_test", tmp_path=None, db_path=None
) -> Copilot:
    client = httpx.AsyncClient()
    kw = {"client": client, "retries": 1, "cache_ttl_s": 900}
    return Copilot(
        principal=Principal("test-user", role),
        policy=policy,
        registry=registry,
        hmda=HmdaSource(db_path=db_path, **kw),
        cfpb=CFPBComplaints(**kw),
        edgar=EdgarFilings(user_agent="Test Runner test@example.com", **kw),
        fred=FredMacro(api_key="fred_test", **kw),
        linear=LinearTickets(api_key=linear_key, team_id="team_1" if linear_key else None, **kw),
        audit=AuditLog(tmp_path / "audit.jsonl" if tmp_path else None),
        today=lambda: date(2026, 10, 6),
    )


@pytest.fixture
def copilot_factory(policy, registry, tmp_path):
    def _make(role="analyst", **kw):
        return make_copilot(policy, registry, role, tmp_path=tmp_path, **kw)

    return _make


# ---------------------------------------------------------------- canned upstream responses


def hmda_filers_body():
    return {
        "institutions": [
            {"lei": WF_LEI, "name": "Wells Fargo Bank, National Association", "period": 2023},
            {"lei": "549300EXAMPLEROCKET01", "name": "Rocket Mortgage, LLC", "period": 2023},
        ]
    }


def hmda_agg_body():
    return {
        "aggregations": [
            {"actions_taken": "1", "count": 700, "sum": 210_000_000.0},
            {"actions_taken": "2", "count": 50, "sum": 15_000_000.0},
            {"actions_taken": "3", "count": 250, "sum": 60_000_000.0},
            {"actions_taken": "4", "count": 90, "sum": 25_000_000.0},
        ]
    }


def cfpb_count_body(n: int, relation: str = "eq"):
    return {"hits": {"total": {"value": n, "relation": relation}, "hits": []}}


def cfpb_search_body():
    return {
        "hits": {
            "total": {"value": 2, "relation": "eq"},
            "hits": [
                {
                    "_source": {
                        "complaint_id": "1",
                        "date_received": "2026-09-02",
                        "product": "Mortgage",
                        "issue": "Trouble during payment process",
                        "state": "CA",
                        "company_response": "Closed with explanation",
                        "timely": "Yes",
                        "complaint_what_happened": "My escrow payment was applied twice...",
                    }
                },
                {
                    "_source": {
                        "complaint_id": "2",
                        "date_received": "2026-09-01",
                        "product": "Mortgage",
                        "issue": "Applying for a mortgage",
                        "state": "TX",
                        "company_response": "In progress",
                        "timely": "Yes",
                        "complaint_what_happened": "",
                    }
                },
            ],
        }
    }


def edgar_submissions_body():
    return {
        "name": "WELLS FARGO & COMPANY/MN",
        "filings": {
            "recent": {
                "form": ["8-K", "10-K", "10-Q"],
                "accessionNumber": [
                    "0000072971-26-000010",
                    "0000072971-26-000005",
                    "0000072971-25-000099",
                ],
                "filingDate": ["2026-04-10", "2026-02-20", "2025-11-01"],
                "primaryDocument": ["wfc-8k.htm", "wfc-20251231.htm", "wfc-10q.htm"],
            }
        },
    }


def edgar_10k_html():
    filler = "Lorem ipsum risk text. " * 60
    return f"""<html><body>
    <p>Table of Contents</p><p>Item 1A. Risk Factors ..... 12</p><p>Item 1B. Unresolved ..... 30</p>
    <h2>Item 1A. Risk Factors</h2>
    <p>{filler}</p>
    <p>Our mortgage servicing operations are subject to extensive regulatory scrutiny, and
    consumer complaints could lead to enforcement actions. {filler}</p>
    <h2>Item 1B. Unresolved Staff Comments</h2><p>None.</p>
    </body></html>"""


def fred_body():
    return {
        "observations": [
            {"date": "2026-09-18", "value": "6.21"},
            {"date": "2026-09-25", "value": "."},
            {"date": "2026-10-02", "value": "6.12"},
        ]
    }


def wire_happy_path(router: respx.MockRouter):
    from lending_risk_mcp.sources.edgar import SUBMISSIONS
    from lending_risk_mcp.sources.fred import OBS
    from lending_risk_mcp.sources.hmda import API, FILERS

    router.get(FILERS.format(year=2023)).respond(json=hmda_filers_body())
    router.get(API).respond(json=hmda_agg_body())
    router.get(CFPB_BASE).respond(json=cfpb_count_body(100))
    router.get(SUBMISSIONS.format(cik=72971)).respond(json=edgar_submissions_body())
    router.get(url__regex=r"https://www\.sec\.gov/Archives/.*wfc-20251231\.htm").respond(
        text=edgar_10k_html()
    )
    router.get(OBS).respond(json=fred_body())
