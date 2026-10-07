import json

import pytest

from lending_risk_mcp.access import AuditLog, Policy, Principal
from lending_risk_mcp.errors import AccessDenied


def test_inheritance(policy):
    reviewer = policy.grants_for("reviewer")
    assert {"hmda:read", "filings:read", "tickets:create", "complaints:narratives"} <= reviewer
    assert "tickets:create" not in policy.grants_for("analyst")
    assert "filings:read" not in policy.grants_for("viewer")


def test_admin_wildcard(policy):
    assert policy.allows(Principal("a", "admin"), "anything:at_all")


def test_require_raises(policy):
    with pytest.raises(AccessDenied):
        policy.require(Principal("v", "viewer"), "tickets:create")


def test_unknown_role(policy):
    with pytest.raises(ValueError, match="unknown role"):
        policy.grants_for("superuser")


def test_inheritance_cycle():
    p = Policy({"a": {"inherits": ["b"]}, "b": {"inherits": ["a"]}}, {})
    with pytest.raises(ValueError, match="cycle"):
        p.grants_for("a")


def test_audit_hashes_args(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl")
    log.record(Principal("u", "analyst"), "search_complaints", {"query": "foreclosure"}, "ok", 12.3)
    entry = json.loads((tmp_path / "a.jsonl").read_text().strip())
    assert entry["tool"] == "search_complaints"
    assert "foreclosure" not in json.dumps(entry)
    assert len(entry["args_sha256"]) == 16
