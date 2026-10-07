# Access control

## Model

`config/policy.yaml` defines permissions and roles. Roles can inherit:

```
viewer  ->  analyst  ->  reviewer
admin: "*"
```

## Enforcement points

1. **Tool registration** (`server.build_server`): a tool is only added to the MCP server if the caller's role has its permission. The model never sees tools it can't use, so it can't plan around them or try them.
2. **Call time** (`service.Copilot._run`): every tool calls `policy.require(...)` before doing anything. If the two ever drift (a policy hot-reload, a bug in registration), the call is still denied.
3. **Field level** (`sources/cfpb.py`): complaint narratives are included only with `complaints:narratives`.
4. **Composite tools** (`lender_risk_brief`): sections the role can't read are reported as `not_permitted` and their sources are never called. There's a test asserting no EDGAR request goes out for a viewer.

## Identity

| Transport | Where identity comes from | Notes |
|---|---|---|
| stdio (today) | `LRC_USER` / `LRC_ROLE` env in the client's launch config | One process per user. Whoever can edit the config picks the role, so this is a local/dev trust model. |
| streamable HTTP (next) | OAuth bearer token → IdP group claims → role | The MCP SDK supports a `token_verifier`; map groups like `risk-reviewers` → `reviewer`. |

## Audit

`logs/audit.jsonl`, one line per tool call:

```json
{"ts": "2026-10-06T15:58:17Z", "user": "sid", "role": "reviewer", "tool": "create_review_ticket",
 "args_sha256": "2aa54f71e34b0d59", "outcome": "degraded", "ms": 341.3}
```

Arguments are hashed (first 16 hex chars of SHA-256), so you can correlate repeated queries without the log storing what was searched. Outcomes: `ok | stale | degraded | unavailable | denied | not_configured`.
