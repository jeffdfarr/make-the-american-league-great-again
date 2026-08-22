"""Command-line entry points.

    python -m mtalga.cli discover --year 2026     # list a season's teams (fill owners.yaml)
    python -m mtalga.cli sync                     # nightly: current season + metrics + export
    python -m mtalga.cli sync --year 2024         # one specific season
    python -m mtalga.cli backfill                 # all seasons, oldest first
    python -m mtalga.cli metrics                  # recompute derived tables only
    python -m mtalga.cli export                   # write site JSON only
"""

from __future__ import annotations

import argparse
import sys

from . import config as cfgm
from . import db as dbm
from .export_json import export_all
from .metrics.engine import rebuild
from .sync import discover, seed_owners, sync_season


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mtalga")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_disc = sub.add_parser("discover", help="print a season's Fantrax teams")
    p_disc.add_argument("--year", type=int, required=True)

    p_sync = sub.add_parser("sync", help="sync a season (default: current), then metrics + export")
    p_sync.add_argument("--year", type=int)
    p_sync.add_argument("--no-metrics", action="store_true")

    sub.add_parser("backfill", help="sync every configured season, oldest first")
    sub.add_parser("metrics", help="rebuild derived tables only")
    sub.add_parser("export", help="write site JSON from derived tables")

    args = ap.parse_args(argv)
    cfg = cfgm.load()

    if args.cmd == "discover":
        discover(cfg.seasons[args.year])
        return 0

    conn = dbm.connect()
    seed_owners(conn, cfg)

    if args.cmd == "sync":
        year = args.year or cfg.current_season
        sync_season(conn, cfg, cfg.seasons[year])
        if not args.no_metrics:
            rebuild(conn, cfg)
            export_all(conn)
    elif args.cmd == "backfill":
        from .fantrax_client import build_session
        session = build_session()
        for year in sorted(cfg.seasons):
            sync_season(conn, cfg, cfg.seasons[year], session=session)
        rebuild(conn, cfg)
        export_all(conn)
    elif args.cmd == "metrics":
        rebuild(conn, cfg)
    elif args.cmd == "export":
        export_all(conn)

    # any FAIL rows this run?
    fails = conn.execute(
        "SELECT COUNT(*) AS n FROM sync_log WHERE ok=0 AND ran_at >= datetime('now','-10 minutes')"
    ).fetchone()["n"]
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
