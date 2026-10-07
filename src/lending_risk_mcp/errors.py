"""Error types and the result envelope every tool returns.

The envelope is the contract with the model: Claude always gets `status` and
`warnings`, so it can say "complaint data is stale" instead of hallucinating
around a gap.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class SourceError(Exception):
    """Base class for anything that goes wrong talking to an upstream source."""

    def __init__(self, source: str, message: str):
        super().__init__(f"[{source}] {message}")
        self.source = source
        self.message = message


class SourceUnavailable(SourceError):
    """Network error, 5xx, or circuit breaker open."""


class RateLimited(SourceError):
    def __init__(self, source: str, retry_after_s: float | None = None):
        super().__init__(source, f"rate limited (retry after {retry_after_s}s)")
        self.retry_after_s = retry_after_s


class AuthError(SourceError):
    """Missing or rejected credentials. Never retried."""


class NotConfigured(SourceError):
    """The source needs config (API key, team id) that isn't set."""


class AccessDenied(Exception):
    def __init__(self, user: str, role: str, permission: str):
        super().__init__(f"user '{user}' (role '{role}') lacks permission '{permission}'")
        self.permission = permission


class Status(StrEnum):
    OK = "ok"
    STALE = "stale"  # served from cache because the live call failed
    DEGRADED = "degraded"  # partial answer, some sub-sources missing
    UNAVAILABLE = "unavailable"


class SourceResult(BaseModel):
    source: str
    status: Status
    data: Any = None
    warnings: list[str] = Field(default_factory=list)
    fetched_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

    @classmethod
    def ok(cls, source: str, data: Any, warnings: list[str] | None = None) -> SourceResult:
        return cls(source=source, status=Status.OK, data=data, warnings=warnings or [])

    @classmethod
    def unavailable(cls, source: str, reason: str) -> SourceResult:
        return cls(source=source, status=Status.UNAVAILABLE, data=None, warnings=[reason])
