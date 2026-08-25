"""Per-day lineup-slot points from Fantrax roster pages.

For every scoring day we fetch each team's roster with stats scoped to that
day (timeframeTypeCode=BY_PERIOD) and credit the day's fantasy points to the
lineup slot each player actually occupied. Days roll up into weekly scoring
periods; the daily sums are exactly how Fantrax builds matchup scores, so the
totals reconcile with the games table. Regular-season weeks only (league
convention: points are RS-only).

Backfill:  python3 -m mtalga.cli posstats     (resumable; skips stored days)
Nightly:   sync_season() picks up new days for the current season.
"""

from __future__ import annotations

import sqlite3
import time
from collections import defaultdict
from datetime import date

from fantraxapi.api import Method, request

PAUSE = 0.8  # seconds between batched day-requests


def _day_rosters(lg, team_ids: list[str], daily: int) -> list[dict]:
    methods = [
        Method("getTeamRosterInfo", teamId=t, period=daily, view="STATS", timeframeTypeCode="BY_PERIOD")
        for t in team_ids
    ]
    datas = request(lg.league, methods)
    return datas if isinstance(datas, list) else [datas]


def _slot_points(lg, resp: dict) -> list[tuple]:
    """-> [(slot, status_name, player_id, player_name, pts)] for every
    rostered player with nonzero points that day (bench and IR included —
    they count toward player records; minors rows are stored but excluded
    at record time)."""
    out = []
    for t in resp.get("tables", []):
        hdr = (t.get("header") or {}).get("cells", [])
        for row in t.get("rows", []):
            sc = row.get("scorer") or {}
            pid = sc.get("scorerId") or sc.get("id")
            if not pid:
                continue  # empty slot
            pos = lg.league.positions.get(row.get("posId"))
            slot = pos.short_name if pos else str(row.get("posId"))
            st = lg.league.status.get(row.get("statusId"))
            status = st.name if st else str(row.get("statusId"))
            for h, c in zip(hdr, row.get("cells", [])):
                if isinstance(h, dict) and h.get("sortKey") == "SCORE" and isinstance(c, dict):
                    try:
                        pts = float(str(c.get("content", "")).replace(",", "") or 0)
                    except ValueError:
                        continue
                    if pts != 0:
                        out.append((slot, status, str(pid), sc.get("name"), pts))
    return out


def sync_position_points(conn: sqlite3.Connection, year: int, lg, ts_ids: dict[str, int],
                         verbose: bool = False) -> int:
    """Fetch any not-yet-stored completed scoring days for a season. Returns
    the number of new days fetched."""
    rs_weeks = {r["period"] for r in conn.execute(
        "SELECT DISTINCT period FROM games WHERE year=? AND game_type='R'", (year,))}
    if not rs_weeks:
        return 0
    sps = lg.league.scoring_periods

    def week_of(d):
        for num in rs_weeks:
            sp = sps.get(num)
            if sp and sp.start <= d <= sp.end:
                return num
        return None

    done = {r["daily"] for r in conn.execute(
        "SELECT DISTINCT daily FROM position_points WHERE year=?", (year,))}
    today = date.today()
    todo = []
    for daily, d in sorted(lg.league.scoring_dates.items()):
        if daily in done or d >= today:
            continue
        wk = week_of(d)
        if wk is not None:
            todo.append((daily, wk))
    if not todo:
        return 0

    team_ids = list(ts_ids.keys())
    for i, (daily, wk) in enumerate(todo):
        datas = _day_rosters(lg, team_ids, daily)
        for tid, resp in zip(team_ids, datas):
            for slot, status, pid, pname, pts in _slot_points(lg, resp):
                conn.execute(
                    """INSERT INTO position_points (year, week, daily, team_season_id, slot, status, player_id, player, pts)
                       VALUES (?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(year, daily, team_season_id, player_id)
                       DO UPDATE SET pts=excluded.pts, week=excluded.week, player=excluded.player,
                                     slot=excluded.slot, status=excluded.status""",
                    (year, wk, daily, ts_ids[tid], slot, status, pid, pname, pts),
                )
        conn.commit()
        if verbose and (i % 10 == 0 or i == len(todo) - 1):
            print(f"[posstats] {year}: day {i + 1}/{len(todo)} fetched")
        time.sleep(PAUSE)
    return len(todo)


def harvest_eligibility(conn: sqlite3.Connection, cfg) -> None:
    """Sample a handful of roster days per season and record each player's
    Fantrax position eligibility (posShortNames). Eligibility rarely changes
    mid-season, so ~6 spread-out days per year covers nearly everyone; the
    union is kept when samples disagree. Fast (~40 requests total)."""
    from .fantrax_client import build_session, open_league

    session = build_session()
    for year in sorted(cfg.seasons):
        try:
            lg = open_league(cfg.seasons[year].league_id, session)
            ts_ids = {
                r["fantrax_team_id"]: r["id"]
                for r in conn.execute("SELECT id, fantrax_team_id FROM team_seasons WHERE year=?", (year,))
            }
            rs_weeks = {r["period"] for r in conn.execute(
                "SELECT DISTINCT period FROM games WHERE year=? AND game_type='R'", (year,))}
            sps = lg.league.scoring_periods
            days = []
            for daily, d in sorted(lg.league.scoring_dates.items()):
                if d >= __import__("datetime").date.today():
                    continue
                if any(sps.get(n) and sps[n].start <= d <= sps[n].end for n in rs_weeks):
                    days.append(daily)
            if not days:
                print(f"[elig] {year}: no completed RS days")
                continue
            step = max(1, len(days) // 6)
            sample = days[::step][:6]
            team_ids = list(ts_ids.keys())
            n = 0
            for daily in sample:
                for resp in _day_rosters(lg, team_ids, daily):
                    for t in resp.get("tables", []):
                        for row in t.get("rows", []):
                            sc = row.get("scorer") or {}
                            pid = sc.get("scorerId") or sc.get("id")
                            pos = sc.get("posShortNames") or ""
                            if not pid or not pos:
                                continue
                            old = conn.execute(
                                "SELECT positions FROM player_eligibility WHERE year=? AND player_id=?",
                                (year, str(pid))).fetchone()
                            merged = set(p.strip() for p in pos.replace("/", ",").split(",") if p.strip())
                            if old and old["positions"]:
                                merged |= set(old["positions"].split(","))
                            conn.execute(
                                """INSERT INTO player_eligibility (year, player_id, player, positions)
                                   VALUES (?,?,?,?)
                                   ON CONFLICT(year, player_id) DO UPDATE SET positions=excluded.positions, player=excluded.player""",
                                (year, str(pid), sc.get("name"), ",".join(sorted(merged))))
                            n += 1
                time.sleep(0.6)
            conn.commit()
            print(f"[elig] {year}: {len(sample)} days sampled, {n} player rows")
        except Exception as e:
            print(f"[elig] {year}: ERROR {type(e).__name__}: {e}")

    print("\n=== VERIFICATION (paste this back) ===")
    checks = [("Spencer Strider", 2022), ("Mookie Betts", 2023), ("Freddie Freeman", 2023),
              ("Emmanuel Clase", 2024), ("Garrett Crochet", 2025), ("Shohei Ohtani", 2023),
              ("Mason Miller", 2026)]
    for name, yr in checks:
        r = conn.execute(
            "SELECT positions FROM player_eligibility WHERE player LIKE ? AND year=?",
            (f"%{name}%", yr)).fetchone()
        print(f"  {name} ({yr}): {r['positions'] if r else 'NOT FOUND'}")


def backfill_posstats(conn: sqlite3.Connection, cfg) -> None:
    """Fetch every season's daily slot points, then rebuild + export.
    Resumable — rerunning skips days already stored."""
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
            n = sync_position_points(conn, year, lg, ts_ids, verbose=True)
            print(f"[posstats] {year}: {n} day(s) fetched")
        except Exception as e:
            print(f"[posstats] {year}: ERROR {type(e).__name__}: {e}")
    rebuild(conn, cfg)
    export_all(conn)
