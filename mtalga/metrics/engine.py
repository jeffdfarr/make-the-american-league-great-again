"""Rebuild every derived table from the games table. Runs nightly, from scratch.

League conventions (reverse-engineered from the sheet and verified against it):
  * Season points (PF/PA/PPG/high/low) are REGULAR SEASON ONLY — these feed OPR.
  * W/L records count regular season + the championship bracket + the
    3rd-place game. Consolation games (named side brackets, and main-bracket
    games among non-qualifiers) count nothing.
  * The championship bracket is derived by walking back from the final:
    a postseason game is in the bracket iff its winner advances toward the
    final. Byes fall out of first appearance round.
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
    _records(conn)
    _adjustments(conn, cfg)
    conn.commit()


# ------------------------------------------------------------- bracket walker

def _classify_postseason(conn: sqlite3.Connection, year: int) -> dict:
    """Classify each postseason matchup_uid.

    Returns {matchup_uid: 'TREE' | 'THIRD' | 'CONS'} plus metadata under the
    special keys '_final' (matchup_uid), '_byes' (set of team_season_ids).
    """
    rows = conn.execute(
        """SELECT matchup_uid, period, bracket, game_type, team_season_id,
                  opp_season_id, pts_for, pts_against
           FROM games WHERE year=? AND game_type != 'R' AND complete=1
             AND pts_for >= pts_against""",  # winner's row = one row per matchup
        (year,),
    ).fetchall()
    out: dict = {"_final": None, "_byes": set()}
    if not rows:
        return out

    # Named 3rd-place brackets count; other named brackets are consolation.
    main, named_third = [], []
    for r in rows:
        if r["bracket"] is None:
            main.append(r)
        elif any(k in r["bracket"].lower() for k in ("3rd", "third")):
            named_third.append(r)
        else:
            out[r["matchup_uid"]] = "CONS"
    for r in named_third:
        out[r["matchup_uid"]] = "THIRD"
    if not main:
        return out

    if any(r["pts_for"] == r["pts_against"] for r in main):
        # A tied postseason game breaks winner-walking; punt to CONS for safety.
        for r in main:
            out.setdefault(r["matchup_uid"], "CONS")
        return out

    periods = sorted({r["period"] for r in main})
    fp = periods[-1]
    fp_games = [r for r in main if r["period"] == fp]
    prior_losers = {r["opp_season_id"] for r in main if r["period"] < fp}

    finals = [g for g in fp_games
              if g["team_season_id"] not in prior_losers and g["opp_season_id"] not in prior_losers]
    if len(finals) != 1:
        # Ambiguous structure — count everything in main as playoffs (old behavior)
        for r in main:
            out.setdefault(r["matchup_uid"], "TREE")
        return out
    final = finals[0]
    out["_final"] = final["matchup_uid"]

    tree = {final["matchup_uid"]}
    survivors = {final["team_season_id"], final["opp_season_id"]}
    for p in reversed(periods[:-1]):
        games_p = [r for r in main if r["period"] == p]
        tree_p = [r for r in games_p if r["team_season_id"] in survivors]
        tree |= {r["matchup_uid"] for r in tree_p}
        appeared = {t for r in games_p for t in (r["team_season_id"], r["opp_season_id"])}
        survivors = ({t for r in tree_p for t in (r["team_season_id"], r["opp_season_id"])}
                     | {t for t in survivors if t not in appeared})

    # 3rd-place game hiding in the main bracket: final-period game whose
    # participants both already lost a bracket game.
    tree_losers = {r["opp_season_id"] for r in main
                   if r["matchup_uid"] in tree and r["period"] < fp}
    for g in fp_games:
        if g["matchup_uid"] in tree:
            continue
        if g["team_season_id"] in tree_losers and g["opp_season_id"] in tree_losers:
            out[g["matchup_uid"]] = "THIRD"

    for r in main:
        if r["matchup_uid"] in tree:
            out[r["matchup_uid"]] = "TREE"
        out.setdefault(r["matchup_uid"], "CONS")

    # byes: bracket teams whose first bracket appearance is after round 1
    first_seen: dict[int, int] = {}
    for r in sorted(main, key=lambda r: r["period"]):
        if r["matchup_uid"] in tree:
            for t in (r["team_season_id"], r["opp_season_id"]):
                first_seen.setdefault(t, r["period"])
    if first_seen:
        r1 = min(first_seen.values())
        out["_byes"] = {t for t, p in first_seen.items() if p > r1}
    return out


# ---------------------------------------------------------------- season level

def _season_stats(conn: sqlite3.Connection, year: int) -> list[dict]:
    post = _classify_postseason(conn, year)
    season_over = _season_complete(conn, year)

    rows = conn.execute(
        """SELECT ts.id AS team_season_id, ts.owner_slug, g.game_type, g.matchup_uid,
                  g.pts_for, g.pts_against, g.period, g.period_days
           FROM games g JOIN team_seasons ts ON ts.id = g.team_season_id
           WHERE g.year = ? AND g.complete = 1""",
        (year,),
    ).fetchall()

    def blank(tsid, slug):
        return {
            "team_season_id": tsid, "year": year, "owner_slug": slug,
            "games": 0, "w": 0, "l": 0, "t": 0,
            "rs_games": 0, "rs_w": 0, "rs_l": 0, "rs_t": 0,
            "pf": 0.0, "pa": 0.0, "high_game": None, "low_game": None,
            "strd_high": None,
            "playoff_app": 0, "playoff_w": 0, "playoff_l": 0,
            "final_app": 0, "champion": 0, "bye": 0, "third_w": 0,
        }

    by_team: dict[int, dict] = {}
    for r in rows:
        s = by_team.setdefault(r["team_season_id"], blank(r["team_season_id"], r["owner_slug"]))
        pf, pa = r["pts_for"], r["pts_against"]
        wlt = "w" if pf > pa else ("l" if pf < pa else "t")

        if r["game_type"] == "R":
            # Regular season: counts for record AND for points/high/low (sheet rule)
            s["rs_games"] += 1
            s["rs_" + wlt] += 1
            s["games"] += 1
            s[wlt] += 1
            s["pf"] += pf
            s["pa"] += pa
            s["high_game"] = pf if s["high_game"] is None else max(s["high_game"], pf)
            s["low_game"] = pf if s["low_game"] is None else min(s["low_game"], pf)
            # "Strd High": best score among STANDARD-LENGTH weeks only — the
            # stretched periods (opening week, All-Star break) are excluded
            # (commissioner's rule; feeds the alternate OPR).
            if r["period_days"] is not None and r["period_days"] <= 8:
                s["strd_high"] = pf if s["strd_high"] is None else max(s["strd_high"], pf)
        else:
            cls = post.get(r["matchup_uid"], "CONS")
            if cls in ("TREE", "THIRD"):
                # counted postseason: record only, never points (sheet rule)
                s["games"] += 1
                s[wlt] += 1
                if cls == "TREE":
                    s["playoff_app"] = 1
                    s["playoff_w" if wlt == "w" else "playoff_l"] += 1 if wlt in ("w", "l") else 0
                    if r["matchup_uid"] == post.get("_final") and season_over:
                        s["final_app"] = 1
                        if wlt == "w":
                            s["champion"] = 1
                elif cls == "THIRD" and wlt == "w":
                    s["third_w"] = 1

    for tsid in post.get("_byes", set()):
        if tsid in by_team:
            by_team[tsid]["bye"] = 1

    out = []
    for s in by_team.values():
        if s["strd_high"] is None:      # no period-length data (or no standard weeks)
            s["strd_high"] = s["high_game"]
        s["win_pct"] = f.win_pct(s["w"], s["l"])
        s["rs_win_pct"] = f.win_pct(s["rs_w"], s["rs_l"])
        s["ppg"] = s["pf"] / s["rs_games"] if s["rs_games"] else 0.0
        s["papg"] = s["pa"] / s["rs_games"] if s["rs_games"] else 0.0
        s["margin"] = s["pf"] - s["pa"]
        s["margin_pg"] = s["margin"] / s["rs_games"] if s["rs_games"] else 0.0
        out.append(s)

    # finish: champion, runner-up, 3rd-place winner, then bracket teams,
    # then the field — ties broken by regular-season record, then points.
    def finish_key(s):
        return (
            -s["champion"], -s["final_app"], -s["third_w"], -s["playoff_app"],
            -s["rs_w"], -s["pf"],
        )
    out.sort(key=finish_key)
    for i, s in enumerate(out, 1):
        s["finish"] = i
        s.pop("third_w")

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
            if s["rs_games"] and s["high_game"] is not None:
                s["raw_opr"] = f.raw_opr(s["ppg"], s["high_game"], s["low_game"], s["rs_win_pct"])
                s["alt_raw_opr"] = f.raw_opr(s["ppg"], s["strd_high"], s["low_game"], s["rs_win_pct"])
            else:
                s["raw_opr"] = s["alt_raw_opr"] = None
        raws = [s["raw_opr"] for s in stats if s["raw_opr"] is not None]
        avg = sum(raws) / len(raws) if raws else None
        alts = [s["alt_raw_opr"] for s in stats if s["alt_raw_opr"] is not None]
        alt_avg = sum(alts) / len(alts) if alts else None
        for s in stats:
            s["opr"] = f.normalized_opr(s["raw_opr"], avg) if s["raw_opr"] is not None and avg else None
            s["alt_opr"] = (f.normalized_opr(s["alt_raw_opr"], alt_avg)
                            if s["alt_raw_opr"] is not None and alt_avg else None)


def _apply_expected_wins(per_season: dict[int, list[dict]], cfg: Config) -> None:
    # Projections regress over completed FULL seasons only — 2020's short
    # season is flagged exclude_from_projections in seasons.yaml
    # (commissioner's rule, matching the sheet's hidden Projection tab).
    excluded = {y for y, sc in cfg.seasons.items() if sc.exclude_from_projections}
    history = [
        (s["opr"], s["rs_w"])
        for year, stats in per_season.items()
        if year < cfg.current_season and year not in excluded
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
            "sos_win_pct", "sos_opr", "raw_opr", "opr",
            "strd_high", "alt_raw_opr", "alt_opr", "expected_wins", "luck",
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
        rs_games = sum(s["rs_games"] for s in seasons)
        ppg = pf / rs_games if rs_games else 0.0     # points are RS-only, so PPG is per RS game
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


def _records(conn: sqlite3.Connection) -> None:
    specs = [
        ("Most points, game", "game",
         "SELECT ts.owner_slug o, g.pts_for v, g.year y FROM games g JOIN team_seasons ts ON ts.id=g.team_season_id WHERE g.complete=1 AND g.game_type='R' ORDER BY g.pts_for DESC LIMIT 1"),
        ("Fewest points, game", "game",
         "SELECT ts.owner_slug o, g.pts_for v, g.year y FROM games g JOIN team_seasons ts ON ts.id=g.team_season_id WHERE g.complete=1 AND g.game_type='R' ORDER BY g.pts_for ASC LIMIT 1"),
        ("Biggest blowout", "game",
         "SELECT ts.owner_slug o, (g.pts_for-g.pts_against) v, g.year y FROM games g JOIN team_seasons ts ON ts.id=g.team_season_id WHERE g.complete=1 AND g.game_type='R' ORDER BY v DESC LIMIT 1"),
        ("Most points, season", "season",
         "SELECT owner_slug o, pf v, year y FROM season_stats ORDER BY pf DESC LIMIT 1"),
        ("Best season OPR", "season",
         "SELECT owner_slug o, opr v, year y FROM season_stats WHERE opr IS NOT NULL AND rs_games >= 15 ORDER BY opr DESC LIMIT 1"),
        ("Best win pct, season", "season",
         "SELECT owner_slug o, win_pct v, year y FROM season_stats WHERE rs_games >= 15 ORDER BY win_pct DESC LIMIT 1"),
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
