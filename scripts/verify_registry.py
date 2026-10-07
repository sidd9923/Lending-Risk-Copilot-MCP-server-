"""Check every pinned identifier in data/lender_registry.yaml against the live sources.

Registries rot: banks merge, CFPB renames a company string, someone fat-fingers
a CIK. Run this before a demo (or on a schedule) and fix anything it flags.

  SEC_USER_AGENT="Your Name you@example.com" python scripts/verify_registry.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from lending_risk_mcp.entities import LenderRegistry, similarity
from lending_risk_mcp.sources import CFPBComplaints, EdgarFilings, HmdaSource

REGISTRY = Path(__file__).resolve().parent.parent / "data" / "lender_registry.yaml"


async def main(year: int = 2023) -> int:
    reg = LenderRegistry.load(REGISTRY)
    edgar = EdgarFilings(user_agent=os.environ.get("SEC_USER_AGENT"))
    cfpb, hmda = CFPBComplaints(), HmdaSource()
    problems = 0

    tickers = {}
    if edgar.configured():
        from lending_risk_mcp.sources.edgar import TICKERS

        data = (await edgar._get_json(TICKERS, headers=edgar._headers())).data
        tickers = {int(r["cik_str"]): r["title"] for r in data.values()}
    else:
        print("! SEC_USER_AGENT not set; skipping CIK checks")

    filers = await hmda.filers(year)

    for e in reg.entries:
        print(f"\n{e['display_name']}")
        if e.get("cik") and tickers:
            title = tickers.get(int(e["cik"]))
            ok = title is not None and similarity(e["display_name"], title) >= 0.5
            problems += not ok
            print(f"  {'ok ' if ok else 'BAD'} CIK {e['cik']} -> {title}")
        if e.get("cfpb_company"):
            n, _, _ = await cfpb.count(e["cfpb_company"])
            ok = n > 0
            problems += not ok
            print(f"  {'ok ' if ok else 'BAD'} CFPB '{e['cfpb_company']}' -> {n:,} complaints")
        else:
            sugg = await cfpb.suggest_company(e["display_name"])
            print(f"  ..  CFPB unpinned; suggestions: {sugg[:3]}")
        hint = e.get("hmda_name_hint") or e["display_name"]
        best = max(filers, key=lambda f: similarity(hint, f.get("name", "")), default=None)
        if best:
            conf = similarity(hint, best["name"])
            print(
                f"  {'ok ' if conf >= 0.6 else 'LOW'} HMDA {year}: {best['name']} ({best['lei']}) "
                f"conf={conf}"
            )

    print(f"\n{problems} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
