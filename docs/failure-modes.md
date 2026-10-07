# Failure modes

The rule: **one source failing should never sink the answer, and the model should always know what's missing.**

Every source goes through the same pipeline (`sources/base.py`):

```
fresh cache? -> breaker open? -> rate limiter -> HTTP w/ retry -> cache + return
                    |                                   |
                    +-- serve stale or fail fast        +-- on failure: serve stale or raise
```

## Per-source behavior

### HMDA (FFIEC)
| Situation | Behavior | Status |
|---|---|---|
| Lender/year is in the local SQLite warehouse | Answer from local DB, no API call | `ok`, `served_from: local_warehouse` |
| Not local, API healthy | Data Browser aggregation | `ok`, `served_from: ffiec_api` |
| API 5xx/timeout, cached before | Serve cached | `stale` |
| API down, nothing cached | Section fails | `unavailable` |
| LEI can't be resolved | Skip the call (don't query with a guess) | `unavailable` + reason |
| Valid LEI, zero rows | Return empty summary | `ok` + warning "no HMDA records" |

Rate limit: 2 req/s (self-imposed; the data browser is slow).

### CFPB complaints
| Situation | Behavior | Status |
|---|---|---|
| Count > 10,000 | API returns `relation: gte` | `ok` + per-month "lower bound" warning |
| Some months fail mid-trend | Each month is cached individually; stale months are flagged | `stale` |
| Company string unknown | Try `_suggest_company`; if confidence < 0.6, stop | `unavailable` |
| Narrative requested by a non-reviewer | Field replaced with `[redacted: ...]` | `ok` |

### SEC EDGAR
| Situation | Behavior | Status |
|---|---|---|
| `SEC_USER_AGENT` not set | Refuse to call (SEC would 403 us anyway) | `unavailable` + how to fix |
| 429 | Honor `Retry-After`, jittered backoff | `ok` or `stale`/`unavailable` |
| 403 | Treated as auth: not retried, breaker not tripped | `unavailable` |
| Item 1A not in primary doc | Return filing + index URL | `degraded` |
| Item 1A appears in TOC and body | Pick the match followed by the longest section | `ok` |
| No CIK (private lender) | Skip | `unavailable` |

Rate limit: 8 req/s (SEC's ceiling is 10).

### FRED
| Situation | Behavior | Status |
|---|---|---|
| No API key | Not called | `unavailable` |
| `"."` values (missing obs) | Dropped | `ok` |

The cache key excludes the API key so it can't leak into logs.

### Linear (writes)
Writes are different. A timeout on a write might mean it *succeeded*, so:

| Situation | Behavior | Status |
|---|---|---|
| Not configured | Return the full draft | `degraded`, `created: false` |
| Timeout / 5xx | **No retry.** Return the draft, say why | `degraded` |
| 401/403 | Return the draft, credentials message | `degraded` |
| GraphQL `errors` (200 OK) | Surface the error message, return the draft | `degraded` |
| Breaker open | Don't attempt | `degraded` |

The draft is never silently dropped.

## Cross-cutting

- **Circuit breaker:** 3 consecutive failures → open for 30s → half-open (one probe). A failed probe reopens immediately.
- **Auth errors never trip the breaker.** A bad key is a config problem, not an availability problem, and shouldn't make a healthy API look "down".
- **Composite tools** (`lender_risk_brief`) run sections with `asyncio.gather(..., return_exceptions=True)`. Even an unexpected exception in one section becomes that section's `unavailable`, never a crashed tool call.
- **Overall status:** `ok` if every permitted section is ok; `unavailable` if none are ok or stale; otherwise `degraded`.
