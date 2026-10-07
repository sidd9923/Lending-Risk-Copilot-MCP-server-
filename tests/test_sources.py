import sqlite3
from datetime import date

import httpx
import pytest

from lending_risk_mcp.errors import NotConfigured, Status
from lending_risk_mcp.sources import (
    CFPBComplaints,
    EdgarFilings,
    FredMacro,
    HmdaSource,
    LinearTickets,
)
from lending_risk_mcp.sources.cfpb import BASE as CFPB_BASE
from lending_risk_mcp.sources.cfpb import _spike
from lending_risk_mcp.sources.edgar import SUBMISSIONS, _extract_risk_factors, _to_text
from lending_risk_mcp.sources.fred import OBS
from lending_risk_mcp.sources.hmda import API
from lending_risk_mcp.sources.linear import GRAPHQL

from .conftest import (
    WF_LEI,
    cfpb_count_body,
    cfpb_search_body,
    edgar_10k_html,
    edgar_submissions_body,
    fred_body,
    hmda_agg_body,
)

# ------------------------------------------------------------------ HMDA


async def test_hmda_api_summary(mock_api):
    mock_api.get(API).respond(json=hmda_agg_body())
    res = await HmdaSource(retries=0).lender_summary(WF_LEI, 2023)
    assert res.status == Status.OK
    assert res.data["denial_rate"] == round(250 / 1000, 4)
    assert res.data["served_from"] == "ffiec_api"


async def test_hmda_prefers_local_warehouse(tmp_path, mock_api):
    db = tmp_path / "hmda.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE lar (activity_year INT, lei TEXT, action_taken INT, "
            "loan_amount REAL, state_code TEXT)"
        )
        conn.executemany(
            "INSERT INTO lar VALUES (2023, ?, ?, 300000, ?)",
            [(WF_LEI, 1, "CA")] * 3 + [(WF_LEI, 3, "CA")] + [(WF_LEI, 3, "NY")],
        )
    route = mock_api.get(API).respond(json=hmda_agg_body())
    res = await HmdaSource(db_path=db, retries=0).lender_summary(WF_LEI, 2023)
    assert not route.called
    assert res.data["served_from"] == "local_warehouse"
    assert res.data["denial_rate"] == 0.4
    assert res.data["top_denial_states"][0]["denials"] == 1


async def test_hmda_empty_result_warns(mock_api):
    mock_api.get(API).respond(json={"aggregations": []})
    res = await HmdaSource(retries=0).lender_summary("BADLEI", 2023)
    assert res.data["denial_rate"] is None and "no HMDA records" in res.warnings[0]


# ------------------------------------------------------------------ CFPB


async def test_cfpb_redacts_narratives_by_default(mock_api):
    mock_api.get(CFPB_BASE).respond(json=cfpb_search_body())
    res = await CFPBComplaints(retries=0).search("WELLS FARGO & COMPANY")
    assert res.data["complaints"][0]["narrative"].startswith("[redacted")
    assert res.data["complaints"][1]["narrative"] is None  # nothing to redact


async def test_cfpb_includes_narratives_when_allowed(mock_api):
    mock_api.get(CFPB_BASE).respond(json=cfpb_search_body())
    res = await CFPBComplaints(retries=0).search("WELLS FARGO & COMPANY", include_narratives=True)
    assert "escrow" in res.data["complaints"][0]["narrative"]


async def test_cfpb_lower_bound_flag(mock_api):
    mock_api.get(CFPB_BASE).respond(json=cfpb_count_body(10000, "gte"))
    res = await CFPBComplaints(retries=0).monthly_trend(
        "X", [(date(2026, 8, 1), date(2026, 8, 31)), (date(2026, 9, 1), date(2026, 9, 30))]
    )
    assert all(r["lower_bound"] for r in res.data["series"])
    assert any("lower bound" in w for w in res.warnings)


async def test_cfpb_serves_stale_when_down(mock_api):
    src = CFPBComplaints(retries=0, cache_ttl_s=0)  # everything expires immediately
    route = mock_api.get(CFPB_BASE)
    route.respond(json=cfpb_count_body(42))
    n, _, stale = await src.count("X")
    assert (n, stale) == (42, False)
    route.respond(503)
    n, _, stale = await src.count("X")
    assert (n, stale) == (42, True)


def test_spike_detection():
    rows = [{"complaints": c} for c in (100, 110, 90, 200)]
    assert _spike(rows)["flag"] is True
    rows = [{"complaints": c} for c in (100, 110, 90, 105)]
    assert _spike(rows)["flag"] is False


# ------------------------------------------------------------------ EDGAR


async def test_edgar_requires_user_agent():
    with pytest.raises(NotConfigured):
        await EdgarFilings(user_agent=None).latest_filing(72971)


async def test_edgar_risk_factors(mock_api):
    mock_api.get(SUBMISSIONS.format(cik=72971)).respond(json=edgar_submissions_body())
    mock_api.get(url__regex=r".*wfc-20251231\.htm").respond(text=edgar_10k_html())
    res = await EdgarFilings(user_agent="T t@x.com", retries=0).risk_factors(72971, ["servicing"])
    assert res.status == Status.OK
    assert res.data["filing"]["form"] == "10-K"
    assert "servicing" in res.data["excerpts"][0]
    sent_ua = mock_api.calls[0].request.headers["user-agent"]
    assert sent_ua == "T t@x.com"


def test_edgar_skips_table_of_contents():
    section = _extract_risk_factors(_to_text(edgar_10k_html()))
    assert section and "regulatory scrutiny" in section and "Unresolved" not in section


async def test_edgar_missing_item_1a_is_degraded(mock_api):
    mock_api.get(SUBMISSIONS.format(cik=72971)).respond(json=edgar_submissions_body())
    mock_api.get(url__regex=r".*wfc-20251231\.htm").respond(
        text="<html>See Exhibit 13 for the annual report.</html>"
    )
    res = await EdgarFilings(user_agent="T t@x.com", retries=0).risk_factors(72971)
    assert res.status == Status.DEGRADED and "exhibit" in res.warnings[0].lower()


# ------------------------------------------------------------------ FRED


async def test_fred_skips_missing_values(mock_api):
    mock_api.get(OBS).respond(json=fred_body())
    res = await FredMacro(api_key="k", retries=0).series("MORTGAGE30US")
    assert [o["value"] for o in res.data["observations"]] == [6.21, 6.12]
    assert res.data["latest"]["date"] == "2026-10-02"


async def test_fred_needs_key():
    with pytest.raises(NotConfigured):
        await FredMacro(api_key=None).series("MORTGAGE30US")


# ------------------------------------------------------------------ Linear


async def test_linear_creates_issue(mock_api):
    mock_api.post(GRAPHQL).respond(
        json={
            "data": {
                "issueCreate": {
                    "success": True,
                    "issue": {
                        "id": "abc",
                        "identifier": "RISK-12",
                        "url": "https://linear.app/x/RISK-12",
                        "title": "t",
                    },
                }
            }
        }
    )
    res = await LinearTickets(api_key="k", team_id="t").create_issue("t", "d")
    assert res.status == Status.OK and res.data["issue"]["identifier"] == "RISK-12"


async def test_linear_not_configured_returns_draft():
    res = await LinearTickets().create_issue("title", "body")
    assert res.status == Status.DEGRADED and res.data["created"] is False
    assert res.data["draft"]["title"] == "title"


async def test_linear_timeout_is_not_retried(mock_api):
    route = mock_api.post(GRAPHQL).mock(side_effect=httpx.ReadTimeout("slow"))
    res = await LinearTickets(api_key="k", team_id="t").create_issue("t", "d")
    assert route.call_count == 1
    assert res.data["created"] is False and "NOT retried" in res.warnings[0]


async def test_linear_graphql_error(mock_api):
    mock_api.post(GRAPHQL).respond(json={"errors": [{"message": "Team not found"}]})
    res = await LinearTickets(api_key="k", team_id="bad").create_issue("t", "d")
    assert res.data["created"] is False and "Team not found" in res.warnings[0]
