import json

from lending_risk_mcp.sources.cfpb import BASE as CFPB_BASE
from lending_risk_mcp.sources.linear import GRAPHQL

from .conftest import wire_happy_path


async def test_brief_all_sources_ok(copilot_factory, mock_api):
    wire_happy_path(mock_api)
    out = await copilot_factory("analyst").lender_risk_brief("Wells Fargo")
    assert out["status"] == "ok", out["coverage"]
    assert set(out["coverage"]) == {
        "lending_outcomes",
        "complaint_trend",
        "filing_risk_factors",
        "macro_context",
    }
    assert out["sections"]["lending_outcomes"]["data"]["denial_rate"] == 0.25


async def test_brief_degrades_when_cfpb_down(copilot_factory, mock_api):
    wire_happy_path(mock_api)
    mock_api.get(CFPB_BASE).respond(503)
    out = await copilot_factory("analyst").lender_risk_brief("Wells Fargo")
    assert out["status"] == "degraded"
    assert out["coverage"]["complaint_trend"] == "unavailable"
    assert out["coverage"]["lending_outcomes"] == "ok"
    assert out["coverage"]["filing_risk_factors"] == "ok"


async def test_brief_respects_role(copilot_factory, mock_api):
    wire_happy_path(mock_api)
    out = await copilot_factory("viewer").lender_risk_brief("Wells Fargo")
    assert out["coverage"]["filing_risk_factors"] == "not_permitted"
    assert out["status"] == "ok"  # everything the viewer *can* see worked
    assert not any("Archives" in str(c.request.url) for c in mock_api.calls)


async def test_tool_denied_and_audited(copilot_factory, tmp_path):
    out = await copilot_factory("viewer").create_review_ticket("Wells Fargo", "t", "s")
    assert out["status"] == "denied"
    entry = json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[-1])
    assert entry["outcome"] == "denied" and entry["tool"] == "create_review_ticket"


async def test_narratives_follow_role(copilot_factory, mock_api):
    from .conftest import cfpb_search_body

    mock_api.get(CFPB_BASE).respond(json=cfpb_search_body())
    analyst = await copilot_factory("analyst").search_complaints("Wells Fargo", query="escrow")
    reviewer = await copilot_factory("reviewer").search_complaints("Wells Fargo", query="escrow")
    assert analyst["data"]["complaints"][0]["narrative"].startswith("[redacted")
    assert "escrow" in reviewer["data"]["complaints"][0]["narrative"]


async def test_unknown_private_lender_has_no_cik(copilot_factory, mock_api):
    wire_happy_path(mock_api)
    out = await copilot_factory("analyst").filing_risk_factors("Some Tiny Credit Union")
    assert out["status"] == "unavailable"


async def test_ticket_body_carries_evidence(copilot_factory, mock_api):
    route = mock_api.post(GRAPHQL).respond(
        json={
            "data": {
                "issueCreate": {
                    "success": True,
                    "issue": {"id": "1", "identifier": "RISK-1", "url": "u", "title": "t"},
                }
            }
        }
    )
    out = await copilot_factory("reviewer").create_review_ticket(
        "Wells Fargo",
        "Mortgage complaint spike",
        "Complaints up 2x vs trailing avg.",
        evidence=["Sep 2026: 200 complaints vs 100 avg"],
    )
    assert out["status"] == "ok"
    sent = json.loads(route.calls[0].request.content)["variables"]["input"]
    assert sent["title"].startswith("[Risk review] Wells Fargo")
    assert "Sep 2026: 200 complaints" in sent["description"]


def test_month_windows_are_complete_months(copilot_factory):
    windows = copilot_factory().month_windows(3)
    assert [w[0].isoformat() for w in windows] == ["2026-07-01", "2026-08-01", "2026-09-01"]
    assert windows[-1][1].isoformat() == "2026-09-30"
