"""Role-based access control + audit logging.

Kept intentionally small: roles -> permissions, with inheritance, loaded from
YAML. The interesting part isn't the RBAC math, it's *where* it's enforced
(see policy.yaml header) and that every decision is audited.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

from .errors import AccessDenied


@dataclass(frozen=True)
class Principal:
    user: str
    role: str


class Policy:
    def __init__(self, roles: dict, permissions: dict):
        self.roles = roles
        self.permissions = permissions
        self._cache: dict[str, frozenset[str]] = {}

    @classmethod
    def load(cls, path: Path) -> Policy:
        with open(path) as f:
            raw = yaml.safe_load(f)
        return cls(raw.get("roles", {}), raw.get("permissions", {}))

    def grants_for(self, role: str, _seen: frozenset[str] = frozenset()) -> frozenset[str]:
        if role in self._cache:
            return self._cache[role]
        if role not in self.roles:
            raise ValueError(f"unknown role '{role}'. Known: {sorted(self.roles)}")
        if role in _seen:
            raise ValueError(f"role inheritance cycle at '{role}'")
        spec = self.roles[role]
        grants = set(spec.get("grants", []))
        for parent in spec.get("inherits", []):
            grants |= self.grants_for(parent, _seen | {role})
        result = frozenset(grants)
        self._cache[role] = result
        return result

    def allows(self, principal: Principal, permission: str) -> bool:
        grants = self.grants_for(principal.role)
        return "*" in grants or permission in grants

    def require(self, principal: Principal, permission: str) -> None:
        if not self.allows(principal, permission):
            raise AccessDenied(principal.user, principal.role, permission)


class AuditLog:
    """Append-only JSONL. Arguments are hashed, not stored, so the log itself
    doesn't become a second copy of sensitive queries."""

    def __init__(self, path: Path | None):
        self.path = path
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, principal: Principal, tool: str, args: dict, outcome: str, ms: float) -> None:
        if not self.path:
            return
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "user": principal.user,
            "role": principal.role,
            "tool": tool,
            "args_sha256": hashlib.sha256(
                json.dumps(args, sort_keys=True, default=str).encode()
            ).hexdigest()[:16],
            "outcome": outcome,
            "ms": round(ms, 1),
        }
        with open(self.path, "a") as f:
            f.write(json.dumps(entry) + "\n")
