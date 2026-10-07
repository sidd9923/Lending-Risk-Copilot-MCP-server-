"""Runtime settings, read once from the environment.

Everything secret comes from env vars (or a .env file loaded by your MCP client).
Nothing secret is ever written into tool output or the audit log.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parent.parent


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    # Who is calling. In stdio mode the MCP client launches one server per user,
    # so identity comes from the launch environment. See docs/access-control.md.
    user: str = "local-user"
    role: str = "analyst"

    # Source credentials
    sec_user_agent: str | None = None  # SEC requires "Name email@domain"
    fred_api_key: str | None = None
    linear_api_key: str | None = None
    linear_team_id: str | None = None

    # Local HMDA warehouse (built by scripts/load_hmda.py)
    hmda_db_path: Path = REPO_ROOT / "data" / "hmda.sqlite"

    policy_path: Path = REPO_ROOT / "config" / "policy.yaml"
    registry_path: Path = REPO_ROOT / "data" / "lender_registry.yaml"
    audit_log_path: Path = REPO_ROOT / "logs" / "audit.jsonl"

    http_timeout_s: float = 20.0
    cache_ttl_s: int = 900
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            user=_env("LRC_USER", "local-user"),
            role=_env("LRC_ROLE", "analyst"),
            sec_user_agent=_env("SEC_USER_AGENT"),
            fred_api_key=_env("FRED_API_KEY"),
            linear_api_key=_env("LINEAR_API_KEY"),
            linear_team_id=_env("LINEAR_TEAM_ID"),
            hmda_db_path=Path(_env("HMDA_DB_PATH", str(REPO_ROOT / "data" / "hmda.sqlite"))),
            policy_path=Path(_env("LRC_POLICY_PATH", str(REPO_ROOT / "config" / "policy.yaml"))),
            registry_path=Path(
                _env("LRC_REGISTRY_PATH", str(REPO_ROOT / "data" / "lender_registry.yaml"))
            ),
            audit_log_path=Path(_env("LRC_AUDIT_LOG", str(REPO_ROOT / "logs" / "audit.jsonl"))),
            http_timeout_s=float(_env("LRC_HTTP_TIMEOUT_S", "20")),
            cache_ttl_s=int(_env("LRC_CACHE_TTL_S", "900")),
        )
