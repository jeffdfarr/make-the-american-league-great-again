"""Probe #4 (final check): sum one week day-by-day and compare to the real score.

Run from the repo folder:  python3 probe_roster4.py
Read-only. If the two numbers match, run:  python3 -m mtalga.cli posstats
"""

from collections import defaultdict

from fantraxapi.api import Method, request

from mtalga import config as cfgm
from mtalga import db as dbm
from mtalga.fantrax_client import build_session, open_league


def main():
    cfg = cfgm.load()
    conn = dbm.connect()
    year = cfg.current_season
    trow = conn.execute(
        "SELECT fantrax_team_id, team_name FROM team_seasons WHERE year=? LIMIT 1", (year,)
    ).fetchone()
    WEEK = 3
    game = conn.execute(
        "SELECT pts_for FROM games g JOIN team_seasons ts ON ts.id=g.team_season_id "
        "WHERE g.year=? AND g.period=? AND ts.fantrax_team_id=?",
        (year, WEEK, trow["fantrax_team_id"]),
    ).fetchone()
    lg = open_league(cfg.seasons[year].league_id, build_session())
    sp = lg.league.scoring_periods[WEEK]
    days = [num for num, d in sorted(lg.league.scoring_dates.items()) if sp.start <= d <= sp.end]
    print(f"{year} · {trow['team_name']} · week {WEEK} ({sp.start} → {sp.end}) · {len(days)} scoring days")
    print(f"actual weekly score from DB: {game['pts_for'] if game else '?'}\n")

    by_slot = defaultdict(float)
    total = 0.0
    for daily in days:
        resp = request(lg.league, [
            Method("getTeamRosterInfo", teamId=trow["fantrax_team_id"], period=daily,
                   view="STATS", timeframeTypeCode="BY_PERIOD"),
        ])
        day_total = 0.0
        for t in resp.get("tables", []):
            hdr = t["header"]["cells"]
            for row in t.get("rows", []):
                if row.get("statusId") != "1":
                    continue
                pos = lg.league.positions.get(row.get("posId"))
                slot = pos.short_name if pos else row.get("posId")
                for h, c in zip(hdr, row.get("cells", [])):
                    if isinstance(h, dict) and h.get("sortKey") == "SCORE" and isinstance(c, dict):
                        try:
                            v = float(str(c.get("content", "")).replace(",", "") or 0)
                        except ValueError:
                            v = 0.0
                        by_slot[slot] += v
                        total += v
                        day_total += v
        print(f"  day {daily}: {day_total:+.2f}")
    print("\nper-slot totals:", {k: round(v, 2) for k, v in sorted(by_slot.items())})
    print(f"\nSUM: {total:.3f}   vs actual {game['pts_for'] if game else '?'}")


if __name__ == "__main__":
    main()
