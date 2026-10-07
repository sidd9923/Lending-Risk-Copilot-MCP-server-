import httpx
import pytest

from lending_risk_mcp.errors import AuthError, RateLimited, SourceUnavailable
from lending_risk_mcp.resilience import CircuitBreaker, TokenBucket, TTLCache, call_with_retry


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _responses(*codes, headers=None):
    it = iter(codes)

    async def fn():
        return httpx.Response(
            next(it), headers=headers or {}, request=httpx.Request("GET", "http://x")
        )

    return fn


async def _nosleep(_):
    return None


async def test_retries_then_succeeds():
    resp = await call_with_retry("s", _responses(503, 429, 200), retries=3, sleep=_nosleep)
    assert resp.status_code == 200


async def test_rate_limited_after_retries():
    with pytest.raises(RateLimited) as exc:
        await call_with_retry(
            "s", _responses(429, 429, headers={"retry-after": "2"}), retries=1, sleep=_nosleep
        )
    assert exc.value.retry_after_s == 2.0


async def test_auth_errors_not_retried():
    calls = 0

    async def fn():
        nonlocal calls
        calls += 1
        return httpx.Response(401, request=httpx.Request("GET", "http://x"))

    with pytest.raises(AuthError):
        await call_with_retry("s", fn, retries=5, sleep=_nosleep)
    assert calls == 1


async def test_network_error_becomes_unavailable():
    async def fn():
        raise httpx.ConnectError("boom")

    with pytest.raises(SourceUnavailable):
        await call_with_retry("s", fn, retries=1, sleep=_nosleep)


async def test_retry_after_is_honored():
    slept = []

    async def sleep(s):
        slept.append(s)

    await call_with_retry("s", _responses(429, 200, headers={"retry-after": "3"}), sleep=sleep)
    assert slept == [3.0]


def test_breaker_opens_and_half_opens():
    clock = FakeClock()
    b = CircuitBreaker(failure_threshold=2, cooldown_s=10, clock=clock)
    b.record_failure()
    assert b.state == "closed"
    b.record_failure()
    assert b.state == "open" and not b.allow()
    clock.t = 11
    assert b.state == "half_open" and b.allow()
    b.record_failure()  # probe failed -> straight back to open
    assert b.state == "open"
    clock.t = 22
    b.record_success()
    assert b.state == "closed"


def test_cache_ttl_and_stale():
    clock = FakeClock()
    c = TTLCache(ttl_s=5, clock=clock)
    c.set("k", 1)
    assert c.get("k") == 1
    clock.t = 6
    assert c.get("k") is None
    assert c.get_stale("k") == 1


async def test_token_bucket_limits_burst():
    clock = FakeClock()
    bucket = TokenBucket(rate=1, capacity=2, clock=clock)
    await bucket.acquire()
    await bucket.acquire()
    assert bucket._tokens < 1  # burst used up; a third call would have to wait
