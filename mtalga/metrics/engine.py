"""Rebuild every derived table from the games table. Runs nightly, from scratch.

Order matters:
  season aggregates -> league OPR normalizers -> OPR -> regression ->
  expected wins -> SOS -> career -> H2H -> seed history -> record book ->
  adjustments (always last, always logged).
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict

from .. import db as dbm
from ..config import Config
from . import formulas as f


def rebuild(conn: sqlite3.Connection, cfg: Config) -> None:
    dbm.reset_derived(conn)
    seasons = [r["year"] for r in conn.execute("SELECT year FROM seasons ORDER BY year")]

    per_season: dict[int, list[dict]] = {}
    for year in seasons:
        per_season[year] = _season_stats(conn, year)

    _apply_opr(per_season)
    _apply_expected_wins(per_season, cfg)
    _apply_sos(conn, per_season)
    _write_season_stats(conn, per_season)
    _career(conn)
    _h2h(conn)
    _seed_history(conn, per_season)
    _records(conn)
    _adjustments(conn, cfg)
    conn.commit()


# ---------------------------------------------------------------- season level

def _season_stats(conn: sqlite3.Connection, year: int) -> list[dict]:
    rows = conn.execute(
        """SELECT ts.id AS team_season_id, ts.owner_slug, g.game_type,
                  g.pts_for, g.pts_against, g.period, g.complete
           FROM games g JOIN team_seasons ts ON ts.id = g.team_season_id
           WHERE g.year = ? AND g.complete = 1""",
        (year,),
    ).fetchall()

    by_team: dict[int, dict] = {}
    for r in rows:
        s = by_team.setdefault(r["team_season_id"], {
            "team_season_id": r["team_season_id"], "year": year,
            "owner_slug": r["owner_slug"],
            "games": 0, "w": 0, "l": 0, "t": 0,
            "rs_games": 0, "rs_w": 0, "rs_l": 0, "rs_t": 0,
            "pf": 0.0, "pa": 0.0, "high_game": None, "low_game": None,
            "playoff_app": 0, "playoff_w": 0, "playoff_l": 0,
            "final_app": 0, "champion": 0, "bye": 0,
            "_playoff_periods": [],
        })
        pf, pa = r["pts_for"], r["pts_against"]
        wlt = "w" if pf > pa else ("l" if pf < pa else "t")
        if r["game_type"] in ("R", "P", "F"):  # sheet convention: consolation excluded
            s["games"] += 1
            s[wlt] += 1
            s["pf"] += pf
            s["pa"] += pa
            s["high_game"] = pf if s["high_game"] is None else max(s["high_game"], pf)
            s["low_game"] = pf if s["low_game"] is None else min(s["low_game"], pf)
        if r["game_type"] == "R":
            s["rs_games"] += 1
            s["rs_" + wlt] += 1
        if r["game_type"] in ("P", "F"):
            s["playoff_app"] = 1
            s["playoff_w" if wlt == "w" else "playoff_l"] += 1 if wlt in ("w", "l") else 0
            s["_playoff_periods"].append((r["period"], wlt))

    # champion: winner of the main-bracket playoff game in the final playoff period
    final_period = conn.execute(
        "SELECT MAX(period) AS p FROM games WHERE year=? AND game_type='P' AND complete=1",
        (year,),
    ).fetchone()["p"]
    season_over = _season_complete(conn, year)
    if final_period is not None and season_over:
        finalists = conn.execute(
            """SELECT team_season_id, pts_for, pts_against FROM games
               WHERE year=? AND period=? AND game_type='P' AND bracket IS NULL""",
            (year, final_period),
        ).fetchall()
        if len(finalists) == 2:
            for fr in finalists:
                s = by_team.get(fr["team_season_id"])
                if s:
                    s["final_app"] = 1
                    if fr["pts_for"] > fr["pts_against"]:
                        s["champion"] = 1

    out = []
    for s in by_team.values():
        s["win_pct"] = f.win_pct(s["w"], s["l"])
        s["rs_win_pct"] = f.win_pct(s["rs_w"], s["rs_l"])
        s["ppg"] = s["pf"] / s["games"] if s["games"] else 0.0
        s["papg"] = s["pa"] / s["games"] if s["games"] else 0.0
        s["margin"] = s["pf"] - s["pa"]
        s["margin_pg"] = s["margin"] / s["games"] if s["games"] else 0.0
        s.pop("_playoff_periods")
        out.append(s)

    # finish: final standings rank by RS wins then PF (playoff results refine
    # top spots; good enough until Fantrax final standings are stored)
    out.sort(key=lambda s: (-s["champion"], -s["final_app"], -s["rs_w"], -s["pf"]))
    for i, s in enumerate(out, 1):
        s["finish"] = i

    # moves + hitting/pitching splits
    for s in out:
        s["moves"] = conn.execute(
            "SELECT COUNT(*) AS n FROM transactions WHERE team_season_id=?",
            (s["team_season_id"],),
        ).fetchone()["n"] or None
        split = conn.execute(
            """SELECT SUM(pitching_pts) AS p, SUM(hitting_pts) AS h
               FROM period_splits WHERE team_season_id=?""",
            (s["team_season_id"],),
        ).fetchone()
        s["pitching_pf"], s["hitting_pf"] = split["p"], split["h"]
    return out


def _season_complete(conn: sqlite3.Connection, year: int) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM games WHERE year=? AND complete=0", (year,)
    ).fetchone()
    latest = conn.execute("SELECT MAX(year) AS y FROM seasons").fetchone()["y"]
    return year < latest or row["n"] == 0


def _apply_opr(per_season: dict[int, list[dict]]) -> None:
    for year, stats in per_season.items():
        for s in stats:
            if s["games"] and s["high_game"] is not None:
                s["raw_opr"] = f.raw_opr(s["ppg"], s["high_game"], s["low_game"], s["rs_win_pct"])
            else:
                s["raw_opr"] = None
        raws = [s["raw_opr"] for s in stats if s["raw_opr"] is not None]
        avg = sum(raws) / len(raws) if raws else None
        for s in stats:
            s["opr"] = f.normalized_opr(s["raw_opr"], avg) if s["raw_opr"] is not None and avg else None


def _apply_expected_wins(per_season: dict[int, list[dict]], cfg: Config) -> None:
    history = [
        (s["opr"], s["rs_w"])
        for year, stats in per_season.items() if year < cfg.current_season
        for s in stats if s["opr"] is not None and s["rs_games"]
    ]
    for year, stats in per_season.items():
        for s in stats:
            if s["opr"] is not None and len(history) >= 2:
                s["expected_wins"] = f.expected_wins(s["opr"], history)
                s["luck"] = s["rs_w"] - s["expected_wins"]
            else:
                s["expected_wins"] = s["luck"] = None


def _apply_sos(conn: sqlite3.Connection, per_season: dict[int, list[dict]]) -> None:
    for year, stats in per_season.items():
        lookup = {s["team_season_id"]: s for s in stats}
        for s in stats:
            opps = conn.execute(
                """SELECT opp_season_id FROM games
                   WHERE year=? AND team_season_id=? AND game_type='R' AND complete=1""",
                (year, s["team_season_id"]),
            ).fetchall()
            opp_stats = [lookup.get(o["opp_season_id"]) for o in opps]
            opp_stats = [o for o in opp_stats if o]
            if opp_stats:
                w = sum(o["rs_w"] for o in opp_stats)
                l = sum(o["rs_l"] for o in opp_stats)
                s["sos_win_pct"] = f.win_pct(w, l)
                oprs = [o["opr"] for o in opp_stats if o["opr"] is not None]
                s["sos_opr"] = sum(oprs) / len(oprs) if oprs else None
            else:
                s["sos_win_pct"] = s["sos_opr"] = None


def _write_season_stats(conn: sqlite3.Connection, per_season: dict[int, list[dict]]) -> None:
    cols = ["team_season_id", "year", "owner_slug", "games", "w", "l", "t",
            "rs_games", "rs_w", "rs_l", "rs_t", "win_pct", "rs_win_pct", "finish",
            "pf", "pa", "ppg", "papg", "high_game", "low_game",
            "pitching_pf", "hitting_pf", "moves", "margin", "margin_pg",
            "sos_win_pct", "sos_opr", "raw_opr", "opr", "expected_wins", "luck",
            "playoff_app", "bye", "playoff_w", "playoff_l", "final_app", "champion"]
    for stats in per_season.values():
        for s in stats:
            conn.execute(
                f"INSERT INTO season_stats ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                [s.get(c) for c in cols],
            )


# ---------------------------------------------------------------- career level

def _career(conn: sqlite3.Connection) -> None:
    owners = [r["slug"] for r in conn.execute("SELECT slug FROM owners")]
    # league-average raw OPR per season, for career OPR normalization
    year_avgs = {
        r["year"]: r["avg_raw"]
        for r in conn.execute(
            "SELECT year, AVG(raw_opr) AS avg_raw FROM season_stats WHERE raw_opr IS NOT NULL GROUP BY year"
        )
    }
    for slug in owners:
        seasons = conn.execute(
            "SELECT * FROM season_stats WHERE owner_slug=? ORDER BY year", (slug,)
        ).fetchall()
        if not seasons:
            continue
        n = len(seasons)
        games = sum(s["games"] for s in seasons)
        w = sum(s["w"] for s in seasons)
        l = sum(s["l"] for s in seasons)
        t = sum(s["t"] for s in seasons)
        rs_w = sum(s["rs_w"] for s in seasons)
        rs_l = sum(s["rs_l"] for s in seasons)
        pf = sum(s["pf"] for s in seasons)
        pa = sum(s["pa"] for s in seasons)
        ppg = pf / games if games else 0.0
        best_game = max((s["high_game"] for s in seasons if s["high_game"] is not None), default=None)
        worst_game = min((s["low_game"] for s in seasons if s["low_game"] is not None), default=None)
        wp = f.win_pct(w, l)
        rs_wp = f.win_pct(rs_w, rs_l)

        career_raw = career_norm = None
        if best_game is not None:
            career_raw = f.raw_opr(ppg, best_game, worst_game, rs_wp)
            avgs = [year_avgs[s["year"]] for s in seasons if s["year"] in year_avgs]
            if avgs:
                career_norm = career_raw / (sum(avgs) / len(avgs))

        playoff_apps = sum(s["playoff_app"] for s in seasons)
        titles = sum(s["champion"] for s in seasons)
        conn.execute(
            """INSERT INTO career_stats
               (owner_slug, seasons, games, w, l, t, win_pct, rs_win_pct, pf, pa, ppg,
                best_game, worst_game, best_season_pf, worst_season_pf, moves,
                winning_seasons, losing_seasons, avg_finish, best_finish, worst_finish,
                career_raw_opr, career_opr, playoff_apps, byes, playoff_w, playoff_l,
                final_apps, titles, title_odds)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (slug, n, games, w, l, t, wp, rs_wp, pf, pa, ppg,
             best_game, worst_game,
             max(s["pf"] for s in seasons), min(s["pf"] for s in seasons),
             sum(s["moves"] or 0 for s in seasons) or None,
             sum(1 for s in seasons if s["w"] > s["l"]),
             sum(1 for s in seasons if s["l"] > s["w"]),
             sum(s["finish"] for s in seasons) / n,
             min(s["finish"] for s in seasons), max(s["finish"] for s in seasons),
             career_raw, career_norm,
             playoff_apps, sum(s["bye"] or 0 for s in seasons),
             sum(s["playoff_w"] for s in seasons), sum(s["playoff_l"] for s in seasons),
             sum(s["final_app"] for s in seasons), titles,
             f.title_odds(playoff_apps, n, wp)),
        )


# ---------------------------------------------------------------- H2H & records

def _h2h(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """SELECT a.owner_slug AS oa, b.owner_slug AS ob, g.game_type,
                  g.pts_for, g.pts_against
           FROM games g
           JOIN team_seasons a ON a.id = g.team_season_id
           JOIN team_seasons b ON b.id = g.opp_season_id
           WHERE g.complete = 1 AND a.owner_slug IS NOT NULL AND b.owner_slug IS NOT NULL"""
    ).fetchall()
    acc: dict[tuple, dict] = defaultdict(lambda: {"games": 0, "w": 0, "l": 0, "t": 0, "pf": 0.0, "pa": 0.0})
    for r in rows:
        for gt in (r["game_type"], "ALL"):
            k = (r["oa"], r["ob"], gt)
            a = acc[k]
            a["games"] += 1
            a["pf"] += r["pts_for"]
            a["pa"] += r["pts_against"]
            key = "w" if r["pts_for"] > r["pts_against"] else ("l" if r["pts_for"] < r["pts_against"] else "t")
            a[key] += 1
    for (oa, ob, gt), a in acc.items():
        conn.execute(
            "INSERT INTO h2h (owner_a, owner_b, game_type, games, w, l, t, pf, pa) VALUES (?,?,?,?,?,?,?,?,?)",
            (oa, ob, gt, a["games"], a["w"], a["l"], a["t"], a["pf"], a["pa"]),
        )


def _seed_history(conn: sqlite3.Connection, per_season) -> None:
    # Requires final standings/seeds per season; populated once playoff seeds
    # are confirmed (open question #5). Placeholder keeps the table present.
    pass


def _records(conn: sqlite3.Connection) -> None:
    specs = [
        ("Most points, game", "game",
         "SELECT ts.owner_slug o, g.pts_for v, g.year y FROM games g JOIN team_seasons ts ON ts.id=g.team_season_id WHERE g.complete=1 AND g.game_type IN ('R','P','F') ORDER BY g.pts_for DESC LIMIT 1"),
        ("Fewest points, game", "game",
         "SELECT ts.owner_slug o, g.pts_for v, g.year y FROM games g JOIN team_seasons ts ON ts.id=g.team_season_id WHERE g.complete=1 AND g.game_type IN ('R','P','F') ORDER BY g.pts_for ASC LIMIT 1"),
        ("Biggest blowout", "game",
         "SELECT ts.owner_slug o, (g.pts_for-g.pts_against) v, g.year y FROM games g JOIN team_seasons ts ON ts.id=g.team_season_id WHERE g.complete=1 ORDER BY v DESC LIMIT 1"),
        ("Most points, season", "season",
         "SELECT owner_slug o, pf v, year y FROM season_stats ORDER BY pf DESC LIMIT 1"),
        ("Best season OPR", "season",
         "SELECT owner_slug o, opr v, year y FROM season_stats WHERE opr IS NOT NULL ORDER BY opr DESC LIMIT 1"),
        ("Best win pct, season", "season",
         "SELECT owner_slug o, win_pct v, year y FROM season_stats WHERE games >= 6 ORDER BY win_pct DESC LIMIT 1"),
    ]
    for category, scope, sql in specs:
        row = conn.execute(sql).fetchone()
        if row and row["v"] is not None:
            conn.execute(
                """INSERT OR REPLACE INTO records_book (category, scope, value, display, owner_slug, year, detail)
                   VALUES (?,?,?,?,?,?,?)""",
                (category, scope, row["v"], f"{row['v']:g}", row["o"], row["y"], None),
            )


# ---------------------------------------------------------------- adjustments

def _adjustments(conn: sqlite3.Connection, cfg: Config) -> None:
    for adj in cfg.adjustments:
        table, key, column = adj["table"], adj["key"], adj["column"]
        if table == "season_stats":
            where, params = "owner_slug=? AND year=?", (key["owner"], key["year"])
        elif table == "career_stats":
            where, params = "owner_slug=?", (key["owner"],)
        else:
            print(f"[adjustments] unsupported table {table!r}, skipping")
            continue
        if "set" in adj:
            conn.execute(f"UPDATE {table} SET {column}=? WHERE {where}", (adj["set"], *params))
        elif "delta" in adj:
            conn.execute(f"UPDATE {table} SET {column}={column}+? WHERE {where}", (adj["delta"], *params))
        print(f"[adjustments] {table} {key} {column}: {adj.get('set', adj.get('delta'))} — {adj['reason']}")
