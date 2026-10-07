# Architecture notes

## Layers

```
MCP client (Claude Desktop / Claude Code / any MCP host)
   │  stdio, JSON-RPC
server.py      tool registration filtered by role; docstrings written for the model
   │
service.py     Copilot: permission check → work → envelope → audit
   │
entities.py    name → {LEI, CFPB company, CIK} with method + confidence
   │
sources/*      one adapter per upstream; all inherit BaseSource
   │
resilience.py  TokenBucket · call_with_retry · CircuitBreaker · TTLCache
```

## Why the service layer is separate from MCP

- Tests call `Copilot` directly for logic, and go through `build_server(...).call_tool(...)` only to test the MCP-facing behavior (registration, annotations, serialization).
- The same `Copilot` could back a REST endpoint or a nightly batch job that scans every lender and opens tickets.

## Tool design choices

- **One composite tool + focused tools.** `lender_risk_brief` gives the model a cheap first move; the focused tools let it drill down without re-fetching everything. Responses stay small enough to fit comfortably in context.
- **Tool annotations:** reads are `read_only_hint=True, idempotent_hint=True`; `create_review_ticket` is `read_only_hint=False`, so hosts can ask for confirmation.
- **Server instructions** tell the model how to treat `status`, when to confirm entity matches, and to only create tickets on request.
- **Bounded outputs:** complaint search caps at 50, FRED returns the last 60 observations, EDGAR excerpts are capped at 8 × ~800 chars.

## Entity resolution flow

```
query "wells"
  ├─ registry fuzzy match (aliases, display name, CFPB string) → score
  ├─ pinned fields (registry) → method=registry, confidence=score
  └─ unpinned fields:
       LEI  ← HMDA filer list for year, best fuzzy match on hmda_name_hint
       CFPB ← /_suggest_company
       CIK  ← SEC company_tickers.json
     each: confidence < 0.6 → unresolved (never guess)
```

Results are cached per `(normalized name, year)`.

## Concurrency

- `lender_risk_brief` fans out with `asyncio.gather`.
- Each source's `TokenBucket` is shared across concurrent calls, so a fan-out can't burst past EDGAR's limit.
- Complaint trend months are fetched sequentially on purpose: they all hit the same API, the bucket would serialize them anyway, and sequential keeps partial-staleness reporting simple.

## Things deliberately left out

- No vector store / RAG. The sources already have search; adding embeddings would be resume-driven development here.
- No LLM calls inside the server. The server returns grounded data; reasoning happens in the client model. That keeps the server deterministic and testable.
