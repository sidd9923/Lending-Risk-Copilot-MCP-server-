"""Per-source resilience: rate limiting, retries, circuit breaking, caching.

Each upstream gets its own instance of each of these, so a throttled EDGAR
never slows down CFPB, and a dead Linear never trips anyone else's breaker.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

import httpx

from .errors import AuthError, RateLimited, SourceUnavailable

T = TypeVar("T")


class TokenBucket:
    """Async token bucket. `rate` tokens/sec, bursts up to `capacity`."""

    def __init__(self, rate: float, capacity: int | None = None, clock=time.monotonic):
        self.rate = rate
        self.capacity = capacity or max(1, int(rate))
        self._tokens = float(self.capacity)
        self._clock = clock
        self._last = clock()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = self._clock()
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                await asyncio.sleep((1 - self._tokens) / self.rate)


class CircuitBreaker:
    """closed -> open after N consecutive failures; half-open after cooldown.

    While open we fail fast instead of making the user wait 20s for a timeout
    we already know is coming.
    """

    def __init__(self, failure_threshold: int = 3, cooldown_s: float = 30.0, clock=time.monotonic):
        self.failure_threshold = failure_threshold
        self.cooldown_s = cooldown_s
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        if self._clock() - self._opened_at >= self.cooldown_s:
            return "half_open"
        return "open"

    def allow(self) -> bool:
        return self.state != "open"

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.failure_threshold or self.state == "half_open":
            self._opened_at = self._clock()


@dataclass
class _Entry:
    value: Any
    stored_at: float


@dataclass
class TTLCache:
    """Tiny in-memory cache that can also hand back stale entries on purpose."""

    ttl_s: float
    clock: Callable[[], float] = time.monotonic
    _data: dict[str, _Entry] = field(default_factory=dict)

    def get(self, key: str) -> Any | None:
        entry = self._data.get(key)
        if entry and self.clock() - entry.stored_at < self.ttl_s:
            return entry.value
        return None

    def get_stale(self, key: str) -> Any | None:
        entry = self._data.get(key)
        return entry.value if entry else None

    def set(self, key: str, value: Any) -> None:
        self._data[key] = _Entry(value, self.clock())


async def call_with_retry(
    source: str,
    fn: Callable[[], Awaitable[httpx.Response]],
    *,
    retries: int = 3,
    base_delay_s: float = 0.5,
    max_delay_s: float = 8.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> httpx.Response:
    """Run an HTTP call, retrying 429/5xx/network errors with jittered backoff.

    401/403 are raised immediately as AuthError (retrying a bad key just burns quota).
    Retry-After is honored when the server sends one.
    """
    attempt = 0
    while True:
        try:
            resp = await fn()
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            if attempt >= retries:
                raise SourceUnavailable(source, f"network error: {exc.__class__.__name__}") from exc
            await sleep(_backoff(attempt, base_delay_s, max_delay_s))
            attempt += 1
            continue

        if resp.status_code in (401, 403):
            raise AuthError(source, f"HTTP {resp.status_code}: credentials missing or rejected")

        if resp.status_code == 429 or resp.status_code >= 500:
            retry_after = _parse_retry_after(resp)
            if attempt >= retries:
                if resp.status_code == 429:
                    raise RateLimited(source, retry_after)
                raise SourceUnavailable(source, f"HTTP {resp.status_code} after {retries} retries")
            delay = (
                retry_after
                if retry_after is not None
                else _backoff(attempt, base_delay_s, max_delay_s)
            )
            await sleep(min(delay, max_delay_s))
            attempt += 1
            continue

        if resp.status_code >= 400:
            raise SourceUnavailable(source, f"HTTP {resp.status_code}: {resp.text[:200]}")
        return resp


def _backoff(attempt: int, base: float, cap: float) -> float:
    return min(cap, base * (2**attempt)) * (0.5 + random.random() / 2)


def _parse_retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None
