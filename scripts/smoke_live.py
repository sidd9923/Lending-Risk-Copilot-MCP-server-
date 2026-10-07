"""Hit every real upstream once and print what came back. Not part of CI.

The unit tests mock HTTP, which proves the logic but not that the APIs still
look the way the adapters expect. This is the "does it actually work today"
check. Run it after setting up .env:

  set -a; source .env; set +a
  python scripts/smoke_live.py "Wells Fargo"
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from lending_risk_mcp.config import Settings
from lending_risk_mcp.service import Copilot


async def main(lender: str) -> None:
    settings = Settings.from_env()
    cp = Copilot.from_settings(settings)
    print(f"role={settings.role}\n")
    print(json.dumps(await cp.source_health(), indent=2))
    print("\n--- resolve")
    print(json.dumps(await cp.resolve_lender(lender), indent=2))
    print("\n--- brief")
    brief = await cp.lender_risk_brief(lender)
    print(json.dumps(brief["coverage"], indent=2))
    for name, section in brief["sections"].items():
        for w in section.get("warnings", []):
            print(f"  [{name}] {w}")
    await cp.aclose()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "Wells Fargo"))
