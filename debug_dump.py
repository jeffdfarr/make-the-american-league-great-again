"""Dump what Fantrax returns for the schedule so parsing can be fixed."""
import json

import fantraxapi.api as api

from mtalga.config import load
from mtalga.fantrax_client import build_session, open_league

cfg = load()
session = build_session()

for year in sorted(cfg.seasons):
    lg = open_league(cfg.seasons[year].league_id, session=session)
    resp = api.get_standings(lg.league, view="SCHEDULE")
    tl = resp.get("tableList", [])
    print(f"\n===== {year} ({len(tl)} period tables) =====")
    if not tl:
        continue
    t = tl[0]
    hdr = [c.get("name") if isinstance(c, dict) else str(c) for c in t.get("header", {}).get("cells", [])]
    print("  header:", hdr)
    for obj in tl[:2]:
        print(f"  -- {obj.get('caption')} {obj.get('subCaption','')}")
        for row in obj.get("rows", [])[:2]:
            cells = row.get("cells", [])
            desc = []
            for c in cells:
                if isinstance(c, dict):
                    tid = "T:" if c.get("teamId") else ""
                    desc.append(f"{tid}{str(c.get('content'))[:18]}")
                else:
                    desc.append(str(c)[:18])
            print("     ", desc)
    # also peek at a late/playoff table
    late = tl[-1]
    print(f"  -- LAST TABLE: {late.get('caption')} {late.get('subCaption','')}")
    for row in late.get("rows", [])[:3]:
        cells = row.get("cells", [])
        desc = []
        for c in cells:
            if isinstance(c, dict):
                tid = "T:" if c.get("teamId") else ""
                desc.append(f"{tid}{str(c.get('content'))[:18]}")
            else:
                desc.append(str(c)[:18])
        print("     ", desc)