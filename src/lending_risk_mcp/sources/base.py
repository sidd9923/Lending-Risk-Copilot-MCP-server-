"""Shared plumbing for every upstream source.

Order of operations for a read:
  1. fresh cache hit?          -> return it
  2. circuit breaker open?     -> serve stale cache if we have it, else fail fast
  3. rate limiter              -> wait for a token
  4. HTTP with retry/backoff   -> success: cache + return
  5. failure                   -> serve stale cache (flagged) if we have it, else raise
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from ..errors import AuthError, SourceError, SourceUnavailable
from ..resilience import CircuitBreaker, TokenBucket, TTLCache, call_with_retry

log = logging.getLogger(__name__)


@dataclass
class Fetched:
    data: Any
    stale: bool = False
    note: str | None = None


class BaseSource:
    name: str = "base"
    rate_per_s: float = 5.0

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        cache_ttl_s: float = 900,
        timeout_s: float = 20.0,
        retries: int = 3,
    ):
        self.client = client or httpx.AsyncClient(timeout=timeout_s, follow_redirects=True)
        self.bucket = TokenBucket(self.rate_per_s)
        self.breaker = CircuitBreaker()
        self.cache = TTLCache(cache_ttl_s)
        self.retries = retries

    def health(self) -> dict:
        return {"source": self.name, "breaker": self.breaker.state, "configured": self.configured()}

    def configured(self) -> bool:
        return True

    async def _get_json(
        self,
        url: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
        cache_key: str | None = None,
    ) -> Fetched:
        key = cache_key or f"GET {url} {json.dumps(params or {}, sort_keys=True)}"
        return await self._fetch(
            key,
            lambda: self.client.get(url, params=params, headers=headers),
            parse=lambda r: r.json(),
        )

    async def _get_text(self, url: str, *, headers: dict | None = None) -> Fetched:
        return await self._fetch(
            f"GET {url}", lambda: self.client.get(url, headers=headers), parse=lambda r: r.text
        )

    async def _fetch(self, key, do_request, parse) -> Fetched:
        cached = self.cache.get(key)
        if cached is not None:
            return Fetched(cached)

        if not self.breaker.allow():
            return self._stale_or_raise(
                key, SourceUnavailable(self.name, "circuit open, failing fast")
            )

        await self.bucket.acquire()
        try:
            resp = await call_with_retry(self.name, do_request, retries=self.retries)
            data = parse(resp)
        except AuthError:
            # Bad credentials aren't an availability problem, so they don't trip the breaker.
            raise
        except SourceError as exc:
            self.breaker.record_failure()
            log.warning("%s request failed: %s", self.name, exc)
            return self._stale_or_raise(key, exc)
        except ValueError as exc:  # JSON decode error: upstream sent garbage (HTML error page etc.)
            self.breaker.record_failure()
            return self._stale_or_raise(
                key, SourceUnavailable(self.name, f"unparseable response: {exc}")
            )

        self.breaker.record_success()
        self.cache.set(key, data)
        return Fetched(data)

    def _stale_or_raise(self, key: str, exc: SourceError) -> Fetched:
        stale = self.cache.get_stale(key)
        if stale is not None:
            return Fetched(stale, stale=True, note=f"served from cache: {exc.message}")
        raise exc

    async def aclose(self) -> None:
        await self.client.aclose()
