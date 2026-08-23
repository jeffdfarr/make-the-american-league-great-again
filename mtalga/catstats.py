"""Per-team hitting/pitching category totals from the SEASON_STATS view.

The Fantrax standings page's "Statistics" tables carry season-to-date raw
counting stats per team (R, HR, SB, QS, SV, ...). We store them long-form in
category_stats keyed (year, team, side, stat) and rebuild all-time records
from the sums.
"""

from __future__ import annotations

import sqlite3

from fantraxapi.api import get_standings

# Header name (before any " -- " description) -> stat key we store.
HIT = {
    "Runs Scored": "R", "Singles": "1B", "Doubles": "2B", "Triples": "3B",
    "Home Runs": "HR", "RBI": "RBI", "Walks": "BB", "Strikeouts": "SO",
    "Stolen Bases": "SB", "Caught Stealing": "CS", "Hit By Pitches": "HBP",
}
PIT = {
    "Innings Pitched": "IP", "Strikeouts Pitched": "K", "Wins": "W",
    "Losses": "L", "Earned Runs Allowed": "ER", "Hits Allowed": "H",
    "Walks Allowed": "BB", "Saves": "SV", "Blown Saves": "BS",
    "Quality Starts": "QS", "Hit Batsmen": "HB",
}


def _find_team_id(obj):
    if isinstance(obj, dict):
        if "teamId" in obj:
            return obj["teamId"]
        for v in obj.values():
            r = _find_team_id(v)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_team_id(v)
            if r:
                return r
    return None


def _num(x):
    s = str(x if x is not None else "").replace(",", "").strip()
    if not s or s == "-":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _content(c):
    return c.get("content") if isinstance(c, dict) else c


def _header_names(t) -> list[str]:
    names = []
    for c in (t.get("header") or {}).get("cells", []):
        raw = (c.get("name") or c.get("shortName") or c.get("content") or "") if isinstance(c, dict) else str(c)
        names.append(raw.split(" -- ")[0].strip())
    return names


def parse_stats_tables(resp: dict) -> list[tuple]:
    """-> [(side, fantrax_team_id, stat_key, value)] from the Statistics tables."""
    out = []
    for t in resp.get("tableList", []):
        name = f"{t.get('caption') or ''} {t.get('subCaption') or ''}".lower()
        if "statistics" not in name:
            continue  # skip the Points-by-category twins and everything else
        side = "H" if "hitting" in name else ("P" if "pitching" in name else None)
        if side is None:
            continue
        stats = HIT if side == "H" else PIT
        names = _header_names(t)
        for row in t.get("rows", []):
            team_id = _find_team_id(row)
            if not team_id:
                continue
            cells = row.get("cells") or []
            for i, col in enumerate(names):
                key = stats.get(col)
                if key is None or i >= len(cells):
                    continue
                v = _num(_content(cells[i]))
                if v is not None:
                    out.append((side, team_id, key, v))
    return out


def sync_category_stats(conn: sqlite3.Connection, year: int, lg, ts_ids: dict[str, int]) -> int:
    resp = get_standings(lg.league, views="SEASON_STATS")
    n = 0
    for side, team_id, stat, value in parse_stats_tables(resp):
        tsid = ts_ids.get(team_id)
        if tsid is None:
            continue
        conn.execute(
            """INSERT INTO category_stats (year, team_season_id, side, stat, value)
               VALUES (?,?,?,?,?)
               ON CONFLICT(year, team_season_id, side, stat) DO UPDATE SET value=excluded.value""",
            (year, tsid, side, stat, value),
        )
        n += 1
    return n


def backfill_catstats(conn: sqlite3.Connection, cfg) -> None:
    """Fetch category totals for every configured season, then rebuild + export."""
    from .export_json import export_all
    from .fantrax_client import build_session, open_league
    from .metrics.engine import rebuild

    session = build_session()
    for year in sorted(cfg.seasons):
        try:
            lg = open_league(cfg.seasons[year].league_id, session)
            ts_ids = {
                r["fantrax_team_id"]: r["id"]
                for r in conn.execute("SELECT id, fantrax_team_id FROM team_seasons WHERE year=?", (year,))
            }
            n = sync_category_stats(conn, year, lg, ts_ids)
            print(f"[catstats] {year}: {n} stat cells")
        except Exception as e:  # keep going; one bad season shouldn't stop the rest
            print(f"[catstats] {year}: ERROR {type(e).__name__}: {e}")
    conn.commit()
    rebuild(conn, cfg)
    export_all(conn)
