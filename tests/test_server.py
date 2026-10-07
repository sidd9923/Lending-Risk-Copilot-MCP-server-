import json

from lending_risk_mcp.server import build_server

from .conftest import wire_happy_path


async def _tool_names(copilot):
    return {t.name for t in await build_server(copilot).list_tools()}


async def test_viewer_cannot_see_write_tools(copilot_factory):
    names = await _tool_names(copilot_factory("viewer"))
    assert "create_review_ticket" not in names
    assert "filing_risk_factors" not in names
    assert "lender_risk_brief" in names


async def test_reviewer_sees_ticket_tool(copilot_factory):
    assert "create_review_ticket" in await _tool_names(copilot_factory("reviewer"))


async def test_ticket_tool_annotated_as_write(copilot_factory):
    tools = await build_server(copilot_factory("reviewer")).list_tools()
    ticket = next(t for t in tools if t.name == "create_review_ticket")
    assert ticket.annotations.read_only_hint is False


async def test_call_through_mcp(copilot_factory, mock_api):
    wire_happy_path(mock_api)
    server = build_server(copilot_factory("analyst"))
    result = await server.call_tool("hmda_summary", {"lender": "Wells Fargo", "year": 2023})
    payload = json.loads(result.content[0].text)
    assert payload["status"] == "ok" and payload["data"]["lender"] == "Wells Fargo"
