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
        SELECT g.year, g.period, g.game_type, g.bracket, g.matchup_uid,
               a.owner_slug AS owner, b.owner_slug AS opponent,
               g.pts_for, g.pts_against
        FROM games g
        JOIN team_seasons a ON a.id = g.team_season_id
        JOIN team_seasons b ON b.id = g.opp_season_id
        WHERE g.complete = 1
        ORDER BY g.year, g.period""",
}


HIT_LADDER = ["C", "SS", "2B", "3B", "OF", "1B", "UT"]  # scarcest first


def _draft_positions(conn: sqlite3.Connection, out: Path) -> None:
    """Map every drafted player (verbatim sheet spelling) to the position
    Fantrax listed him at in his draft year. Pitchers are just 'P' (no SP/RP
    split — league convention for the draft-tendencies view). Sheet typos are
    resolved by fuzzy match against the eligibility harvest, with manual
    overrides in config/draft_aliases.yaml."""
    import re
    import unicodedata
    from difflib import get_close_matches

    draft_path = out / "draft.json"
    if not draft_path.exists():
        return
    boards = json.loads(draft_path.read_text())

    def norm(s: str) -> str:
        s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
        return re.sub(r"[^a-z]", "", s.lower())

    elig: dict[str, dict[int, set[str]]] = {}
    for r in conn.execute("SELECT year, player, positions FROM player_eligibility"):
        elig.setdefault(norm(r["player"]), {}).setdefault(r["year"], set()).update(
            r["positions"].split(","))
    pool = list(elig)

    aliases = {}
    alias_path = Path(__file__).resolve().parent.parent / "config" / "draft_aliases.yaml"
    if alias_path.exists():
        import yaml
        aliases = {norm(k): norm(v) for k, v in (yaml.safe_load(alias_path.read_text()) or {}).items()}

    def bucket(ps: set[str]) -> str:
        for h in HIT_LADDER:
            if h in ps:
                return h
        return "P" if ps & {"SP", "RP", "P"} else "?"

    result = {}
    for b in boards:
        if not b.get("results"):
            continue
        for rd in b["rounds"]:
            for p in rd["picks"]:
                m = re.match(r"^([^(]+?)\s*\(", p["owner"])
                if not m:
                    continue
                sheet_name = m.group(1).strip()
                key = aliases.get(norm(sheet_name), norm(sheet_name))
                if key not in elig:
                    close = get_close_matches(key, pool, n=1, cutoff=0.82)
                    key = close[0] if close else None
                pos = "?"
                if key and key in elig:
                    yrs = elig[key]
                    yr = b["year"] if b["year"] in yrs else min(
                        (y for y in yrs if y >= b["year"]), default=None)
                    if yr:
                        pos = bucket(yrs[yr])
                result[f"{b['year']}|{sheet_name}"] = pos
    (out / "draft_positions.json").write_text(json.dumps(result, indent=1))
    known = sum(1 for v in result.values() if v != "?")
    print(f"[export] draft_positions.json ({known}/{len(result)} classified)")


def export_all(conn: sqlite3.Connection, out_dir: Path | None = None) -> None:
    out = out_dir or OUT_DIR
    out.mkdir(parents=True, exist_ok=True)
    for name, sql in EXPORTS.items():
        rows = [dict(r) for r in conn.execute(sql).fetchall()]
        if name == "games":
            # counted = league W/L convention: regular season + championship
            # bracket + 3rd-place game (engine's bracket walker decides).
            from mtalga.metrics.engine import _classify_postseason
            cls = {y: _classify_postseason(conn, y)
                   for y in {r["year"] for r in rows}}
            for r in rows:
                r["counted"] = 1 if r["game_type"] == "R" else (
                    1 if cls[r["year"]].get(r["matchup_uid"]) in ("TREE", "THIRD") else 0)
                del r["matchup_uid"]
        (out / f"{name}.json").write_text(json.dumps(rows, indent=1))
        print(f"[export] {name}.json ({len(rows)} rows)")
    _draft_positions(conn, out)

    last = conn.execute("SELECT MAX(ran_at) AS t FROM sync_log").fetchone()
    meta = {
        "generated": (last["t"] or "")[:10],
        "seasons": [r["year"] for r in conn.execute("SELECT year FROM seasons ORDER BY year")],
        "games": conn.execute("SELECT COUNT(*)/2 AS n FROM games WHERE complete=1").fetchone()["n"],
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=1))
    print(f"[export] meta.json ({meta['generated']})")
