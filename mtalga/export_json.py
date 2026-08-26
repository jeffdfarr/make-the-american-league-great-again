"""Dump derived tables to static JSON for the future frontend (site/data/)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent.parent / "site" / "data"

EXPORTS = {
    "standings": """
        SELECT ss.*, ts.team_name, ts.logo_url, o.name AS owner_name,
               COALESCE(lv.live_pf, 0) AS live_pf, COALESCE(lv.live_pa, 0) AS live_pa
        FROM season_stats ss
        JOIN team_seasons ts ON ts.id = ss.team_season_id
        LEFT JOIN owners o ON o.slug = ss.owner_slug
        LEFT JOIN (SELECT team_season_id, SUM(pts_for) AS live_pf, SUM(pts_against) AS live_pa
                   FROM games WHERE complete = 0 AND game_type = 'R'
                   GROUP BY team_season_id) lv ON lv.team_season_id = ss.team_season_id
        ORDER BY ss.year DESC, ss.finish ASC""",
    "franchises": """
        SELECT cs.*, o.name AS owner_name
        FROM career_stats cs JOIN owners o ON o.slug = cs.owner_slug
        ORDER BY cs.career_opr DESC""",
    "h2h": "SELECT * FROM h2h ORDER BY owner_a, owner_b, game_type",
    "records": "SELECT * FROM records_book ORDER BY scope, category",
    "games": """
        SELECT g.year, g.period, g.game_type, g.bracket,
               a.owner_slug AS owner, b.owner_slug AS opponent,
               g.pts_for, g.pts_against
        FROM games g
        JOIN team_seasons a ON a.id = g.team_season_id
        JOIN team_seasons b ON b.id = g.opp_season_id
        WHERE g.complete = 1
        ORDER BY g.year, g.period""",
}


def export_all(conn: sqlite3.Connection, out_dir: Path | None = None) -> None:
    out = out_dir or OUT_DIR
    out.mkdir(parents=True, exist_ok=True)
    for name, sql in EXPORTS.items():
        rows = [dict(r) for r in conn.execute(sql).fetchall()]
        (out / f"{name}.json").write_text(json.dumps(rows, indent=1))
        print(f"[export] {name}.json ({len(rows)} rows)")

    last = conn.execute("SELECT MAX(ran_at) AS t FROM sync_log").fetchone()
    meta = {
        "generated": (last["t"] or "")[:10],
        "seasons": [r["year"] for r in conn.execute("SELECT year FROM seasons ORDER BY year")],
        "games": conn.execute("SELECT COUNT(*)/2 AS n FROM games WHERE complete=1").fetchone()["n"],
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=1))
    print(f"[export] meta.json ({meta['generated']})")
