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
    _career(conn, cfg)
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

def _career(conn: sqlite3.Connection, cfg: Config) -> None:
    owners = [r["slug"] for r in conn.execute("SELECT slug FROM owners")]
    year_avgs = {
        r["year"]: r["avg_raw"]
        for r in conn.execute(
            "SELECT year, AVG(raw_opr) AS avg_raw FROM season_stats WHERE raw_opr IS NOT NULL GROUP BY year"
        )
    }
    # Sheet convention: every career OPR is normalized by the same number —
    # the flat mean of the yearly league-average raw OPRs (all seasons).
    league_norm = sum(year_avgs.values()) / len(year_avgs) if year_avgs else None
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

        # Career OPR — the sheet's exact formula (verified to 4 decimals
        # against TFarr/JFarr/Tessman tabs): the season blend applied to
        # SEASON PPGs — average, best, and worst season PPG (2020's short
        # season excluded from these), with career win% (all seasons) —
        # normalized by the flat mean of every year's league-average raw OPR.
        career_raw = career_norm = None
        excluded = {y for y, sc in cfg.seasons.items() if sc.exclude_from_projections}
        ppgs = [se["ppg"] for se in seasons if se["year"] not in excluded and se["rs_games"]]
        if not ppgs:  # owner played only excluded seasons (e.g. 2020-only)
            ppgs = [se["ppg"] for se in seasons if se["rs_games"]]
        if ppgs:
            career_raw = (6 * (sum(ppgs) / len(ppgs)) + 2 * (max(ppgs) + min(ppgs)) + 400 * wp) / 10
            if league_norm:
                career_norm = career_raw / league_norm

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
        ("Highest single-week score", "scoring", "{:g} pts", None,
         "SELECT ts.owner_slug o, g.pts_for v, g.year y FROM games g JOIN team_seasons ts ON ts.id=g.team_season_id WHERE g.complete=1 AND g.game_type='R' ORDER BY g.pts_for DESC LIMIT 1"),
        ("Lowest single-week score", "scoring", "{:g} pts", None,
         "SELECT ts.owner_slug o, g.pts_for v, g.year y FROM games g JOIN team_seasons ts ON ts.id=g.team_season_id WHERE g.complete=1 AND g.game_type='R' ORDER BY g.pts_for ASC LIMIT 1"),
        ("Biggest blowout", "scoring", "+{:g} pts", None,
         "SELECT ts.owner_slug o, (g.pts_for-g.pts_against) v, g.year y FROM games g JOIN team_seasons ts ON ts.id=g.team_season_id WHERE g.complete=1 AND g.game_type='R' ORDER BY v DESC LIMIT 1"),
        ("Most points in a season", "scoring", "{:,.0f} pts", None,
         "SELECT owner_slug o, pf v, year y FROM season_stats ORDER BY pf DESC LIMIT 1"),
        ("Best average finish", "franchise", "{:.2f}", "all-time",
         "SELECT owner_slug o, avg_finish v, NULL y FROM career_stats WHERE seasons >= 2 ORDER BY avg_finish ASC LIMIT 1"),
        ("Worst average finish", "franchise", "{:.2f}", "all-time",
         "SELECT owner_slug o, avg_finish v, NULL y FROM career_stats WHERE seasons >= 2 ORDER BY avg_finish DESC LIMIT 1"),
    ]
    # OPR records — minimum-side and count records use completed seasons only,
    # so a half-played year can't sneak in; maxima break only when exceeded.
    comp = [r["year"] for r in conn.execute("SELECT DISTINCT year FROM season_stats WHERE champion=1")]
    comp_in = ",".join(str(y) for y in comp) or "0"
    specs += [
        ("Best career OPR", "opr", "{:.3f}", "all-time",
         "SELECT owner_slug o, career_opr v, NULL y FROM career_stats WHERE career_opr IS NOT NULL ORDER BY career_opr DESC LIMIT 1"),
        ("Worst career OPR", "opr", "{:.3f}", "all-time",
         "SELECT owner_slug o, career_opr v, NULL y FROM career_stats WHERE career_opr IS NOT NULL ORDER BY career_opr ASC LIMIT 1"),
        ("Best single-season OPR", "opr", "{:.3f}", None,
         "SELECT owner_slug o, opr v, year y FROM season_stats WHERE opr IS NOT NULL AND rs_games >= 15 ORDER BY opr DESC LIMIT 1"),
        ("Worst single-season OPR", "opr", "{:.3f}", None,
         f"SELECT owner_slug o, opr v, year y FROM season_stats WHERE opr IS NOT NULL AND year IN ({comp_in}) ORDER BY opr ASC LIMIT 1"),
        ("Lowest OPR to make the playoffs", "opr", "{:.3f}", None,
         f"SELECT owner_slug o, opr v, year y FROM season_stats WHERE opr IS NOT NULL AND playoff_app=1 AND year IN ({comp_in}) ORDER BY opr ASC LIMIT 1"),
        ("Highest OPR to miss the playoffs", "opr", "{:.3f}", None,
         f"SELECT owner_slug o, opr v, year y FROM season_stats WHERE opr IS NOT NULL AND playoff_app=0 AND year IN ({comp_in}) ORDER BY opr DESC LIMIT 1"),
    ]
    for category, scope, disp, when, sql in specs:
        row = conn.execute(sql).fetchone()
        if row and row["v"] is not None:
            conn.execute(
                """INSERT OR REPLACE INTO records_book (category, scope, value, display, owner_slug, year, detail)
                   VALUES (?,?,?,?,?,?,?)""",
                (category, scope, row["v"], disp.format(row["v"]), row["o"], row["y"], when),
            )
    row = conn.execute(
        f"SELECT owner_slug o, COUNT(*) v FROM season_stats WHERE opr > 1.0 AND year IN ({comp_in}) "
        "AND owner_slug IS NOT NULL GROUP BY owner_slug ORDER BY v DESC LIMIT 1").fetchone()
    if row and row["v"]:
        conn.execute(
            """INSERT OR REPLACE INTO records_book (category, scope, value, display, owner_slug, year, detail)
               VALUES (?,?,?,?,?,?,?)""",
            ("Most 1.000+ OPR seasons", "opr", row["v"], f"{row['v']:g} seasons", row["o"], None, "all-time"),
        )
    _wl_records(conn)
    _category_records(conn)


CATEGORY_RECORDS = [
    ("Most runs scored", "hitting", "H", "R"),
    ("Most singles", "hitting", "H", "1B"),
    ("Most doubles", "hitting", "H", "2B"),
    ("Most triples", "hitting", "H", "3B"),
    ("Most home runs", "hitting", "H", "HR"),
    ("Most RBI", "hitting", "H", "RBI"),
    ("Most walks drawn", "hitting", "H", "BB"),
    ("Most strikeouts (batters)", "hitting", "H", "SO"),
    ("Most stolen bases", "hitting", "H", "SB"),
    ("Most wins", "pitching", "P", "W"),
    ("Most losses", "pitching", "P", "L"),
    ("Most quality starts", "pitching", "P", "QS"),
    ("Most strikeouts (pitchers)", "pitching", "P", "K"),
    ("Most saves", "pitching", "P", "SV"),
    ("Most blown saves", "pitching", "P", "BS"),
]


def _category_records(conn: sqlite3.Connection) -> None:
    """Raw counting-stat records: all-time franchise totals AND best single season."""
    for category, scope, side, stat in CATEGORY_RECORDS:
        career = conn.execute(
            """SELECT ts.owner_slug o, SUM(cs.value) v
               FROM category_stats cs
               JOIN team_seasons ts ON ts.id = cs.team_season_id
               WHERE cs.side=? AND cs.stat=? AND ts.owner_slug IS NOT NULL
               GROUP BY ts.owner_slug ORDER BY v DESC LIMIT 1""",
            (side, stat),
        ).fetchone()
        if career and career["v"]:
            conn.execute(
                """INSERT OR REPLACE INTO records_book (category, scope, value, display, owner_slug, year, detail)
                   VALUES (?,?,?,?,?,?,?)""",
                (category, scope, career["v"], f"{career['v']:,.0f}", career["o"], None, "all-time"),
            )
        season = conn.execute(
            """SELECT ts.owner_slug o, SUM(cs.value) v, cs.year y
               FROM category_stats cs
               JOIN team_seasons ts ON ts.id = cs.team_season_id
               WHERE cs.side=? AND cs.stat=? AND ts.owner_slug IS NOT NULL
               GROUP BY ts.owner_slug, cs.year ORDER BY v DESC LIMIT 1""",
            (side, stat),
        ).fetchone()
        if season and season["v"]:
            conn.execute(
                """INSERT OR REPLACE INTO records_book (category, scope, value, display, owner_slug, year, detail)
                   VALUES (?,?,?,?,?,?,?)""",
                (category + ", season", scope + "-season", season["v"], f"{season['v']:,.0f}",
                 season["o"], season["y"], None),
            )


def _wl_records(conn: sqlite3.Connection) -> None:
    """Win/loss record book. Counted games = regular season + championship
    bracket + 3rd-place game (consolation never counts — league convention).
    Season-count records (winning seasons, improvement, fewest losses, ...)
    look only at COMPLETED seasons; single-season maxima may include the
    current season once it actually exceeds the old record."""
    name_of = {r["slug"]: r["name"] for r in conn.execute("SELECT slug, name FROM owners")}
    completed = sorted(r["year"] for r in conn.execute(
        "SELECT DISTINCT year FROM season_stats WHERE champion=1"))
    comp_set = set(completed)
    out: list[tuple] = []  # (category, scope, value, display, owner, year, detail)

    def span(y0, y1):
        return str(y0) if y0 == y1 else f"{y0}–{y1}"

    # ---------------- season_stats / career_stats one-liners
    def pick(category, scope, sql, disp="{:g}", when=None, params=()):
        row = conn.execute(sql, params).fetchone()
        if row and row["v"] is not None:
            y = row["y"] if "y" in row.keys() else None
            txt = disp(row["v"]) if callable(disp) else disp.format(row["v"])
            out.append((category, scope, row["v"], txt, row["o"], y, when))

    pct = lambda v: (f"{v:.3f}".lstrip("0") or "0")  # .842 style
    comp_in = ",".join(str(y) for y in completed) or "0"
    pick("Most wins in a season (regular season)", "wins",
         "SELECT owner_slug o, rs_w v, year y FROM season_stats ORDER BY rs_w DESC LIMIT 1", "{:g} wins")
    pick("Most wins in a season (with playoffs)", "wins",
         "SELECT owner_slug o, w v, year y FROM season_stats ORDER BY w DESC LIMIT 1", "{:g} wins")
    pick("Most career wins", "wins",
         "SELECT owner_slug o, w v FROM career_stats ORDER BY w DESC LIMIT 1", "{:g} wins", when="all-time")
    pick("Best season win pct (regular season)", "wins",
         "SELECT owner_slug o, rs_win_pct v, year y FROM season_stats WHERE rs_games >= 15 ORDER BY rs_win_pct DESC LIMIT 1", pct)
    pick("Best season win pct (with playoffs)", "wins",
         "SELECT owner_slug o, win_pct v, year y FROM season_stats WHERE rs_games >= 15 ORDER BY win_pct DESC LIMIT 1", pct)
    pick("Best career win pct", "wins",
         "SELECT owner_slug o, win_pct v FROM career_stats ORDER BY win_pct DESC LIMIT 1", pct, when="all-time")
    pick("Most winning seasons", "wins",
         "SELECT owner_slug o, winning_seasons v FROM career_stats ORDER BY winning_seasons DESC LIMIT 1", "{:g} seasons", when="all-time")
    pick("Most losing seasons", "losses",
         "SELECT owner_slug o, losing_seasons v FROM career_stats ORDER BY losing_seasons DESC LIMIT 1", "{:g} seasons", when="all-time")
    pick("Most career losses", "losses",
         "SELECT owner_slug o, l v FROM career_stats ORDER BY l DESC LIMIT 1", "{:g} losses", when="all-time")
    pick("Most losses in a season", "losses",
         f"SELECT owner_slug o, l v, year y FROM season_stats WHERE year IN ({comp_in}) ORDER BY l DESC LIMIT 1", "{:g} losses")
    pick("Fewest losses in a full season", "losses",
         f"SELECT owner_slug o, l v, year y FROM season_stats WHERE year IN ({comp_in}) ORDER BY l ASC LIMIT 1", "{:g} losses")

    # ---------------- season sequences (completed seasons, consecutive years)
    seas = conn.execute(
        "SELECT owner_slug, year, w, l, rs_w, rs_l, rs_win_pct FROM season_stats "
        "WHERE owner_slug IS NOT NULL ORDER BY owner_slug, year").fetchall()
    by_owner: dict[str, list] = defaultdict(list)
    for r in seas:
        by_owner[r["owner_slug"]].append(r)

    seq_best: dict[str, tuple | None] = {"win": None, "lose": None, "15": None}
    imp_best = reg_best = None  # (pct_diff, slug, year, display)
    for slug, rows in by_owner.items():
        crows = [r for r in rows if r["year"] in comp_set]
        runs = {"win": (0, None), "lose": (0, None), "15": (0, None)}  # len, start
        prev_year = None
        for r in crows:
            fresh = prev_year is None or r["year"] != prev_year + 1
            flags = {"win": r["w"] > r["l"], "lose": r["l"] > r["w"], "15": r["rs_w"] >= 15}
            for k, on in flags.items():
                ln, y0 = runs[k]
                if on:
                    runs[k] = (1, r["year"]) if (fresh or ln == 0) else (ln + 1, y0)
                    ln, y0 = runs[k]
                    if seq_best[k] is None or ln > seq_best[k][0]:
                        seq_best[k] = (ln, slug, y0, r["year"])
                else:
                    runs[k] = (0, None)
            prev_year = r["year"]
        for prev, cur in zip(crows, crows[1:]):
            if cur["year"] != prev["year"] + 1:
                continue
            diff = (cur["rs_win_pct"] or 0) - (prev["rs_win_pct"] or 0)
            disp = f"{prev['rs_w']}–{prev['rs_l']} → {cur['rs_w']}–{cur['rs_l']}"
            if imp_best is None or diff > imp_best[0]:
                imp_best = (diff, slug, cur["year"], disp)
            if reg_best is None or diff < reg_best[0]:
                reg_best = (diff, slug, cur["year"], disp)

    for k, cat, scope in (("win", "Most consecutive winning seasons", "wins"),
                          ("lose", "Most consecutive losing seasons", "losses"),
                          ("15", "Most consecutive 15-win regular seasons", "wins")):
        b = seq_best[k]
        if b and b[0] >= 2:
            out.append((cat, scope, b[0], f"{b[0]} seasons", b[1], b[3], span(b[2], b[3])))
    n15 = conn.execute(
        f"SELECT owner_slug o, COUNT(*) v FROM season_stats WHERE rs_w >= 15 AND year IN ({comp_in}) "
        "GROUP BY owner_slug ORDER BY v DESC LIMIT 1").fetchone()
    if n15 and n15["v"]:
        out.append(("Most 15-win regular seasons", "wins", n15["v"], f"{n15['v']:g} seasons", n15["o"], None, "all-time"))
    if imp_best:
        out.append(("Biggest one-year improvement", "wins", round(imp_best[0], 3), imp_best[3], imp_best[1], imp_best[2], None))
    if reg_best:
        out.append(("Biggest one-year collapse", "losses", round(reg_best[0], 3), reg_best[3], reg_best[1], reg_best[2], None))

    # ---------------- game-by-game timeline records
    years = [r["year"] for r in conn.execute("SELECT year FROM seasons ORDER BY year")]
    timeline: dict[str, list] = defaultdict(list)  # owner -> (year, period, res, margin, opp)
    for year in years:
        post = _classify_postseason(conn, year)
        rows = conn.execute(
            """SELECT ts.owner_slug slug, opp.owner_slug opp, g.game_type, g.matchup_uid,
                      g.period, g.pts_for pf, g.pts_against pa
               FROM games g
               JOIN team_seasons ts ON ts.id = g.team_season_id
               JOIN team_seasons opp ON opp.id = g.opp_season_id
               WHERE g.year=? AND g.complete=1 AND ts.owner_slug IS NOT NULL
               ORDER BY g.period""",
            (year,),
        ).fetchall()
        for r in rows:
            if r["game_type"] != "R" and post.get(r["matchup_uid"]) not in ("TREE", "THIRD"):
                continue
            res = "W" if r["pf"] > r["pa"] else ("L" if r["pf"] < r["pa"] else "T")
            timeline[r["slug"]].append((year, r["period"], res, r["pf"] - r["pa"], r["opp"]))

    best: dict[tuple, tuple | None] = {}
    def upd(key, ln, slug, y0, y1, extra=None):
        cur = best.get(key)
        if cur is None or ln > cur[0]:
            best[key] = (ln, slug, y0, y1, extra)

    fast_w, fast_l, onept = {}, {}, {}
    begin_l: dict[tuple, int] = {}  # (owner, year) -> season-opening losing streak
    for slug, games in timeline.items():
        kind, ln, y0 = None, 0, None
        s_kind, s_len = None, 0
        b_kind, b_len, b_open = None, 0, False
        prev_year = None
        wins = losses = played = pts1 = 0
        pair: dict[str, int] = defaultdict(int)
        pair_start: dict[str, int] = {}
        for (year, _p, res, margin, opp) in games:
            if res == kind:
                ln += 1
            else:
                kind, ln, y0 = res, 1, year
            if kind in "WL":
                upd(("overall", kind), ln, slug, y0, year)
            if year != prev_year:
                s_kind, s_len = None, 0
                b_kind, b_len, b_open = None, 0, True
                prev_year = year
            if res == s_kind:
                s_len += 1
            else:
                s_kind, s_len = res, 1
            if s_kind in "WL":
                upd(("season", s_kind), s_len, slug, year, year)
            if b_open:
                if b_kind is None:
                    b_kind = res
                if res == b_kind and res in "WL":
                    b_len += 1
                    upd(("begin", b_kind), b_len, slug, year, year)
                    if b_kind == "L":
                        begin_l[(slug, year)] = b_len
                else:
                    b_open = False
            played += 1
            if res == "W":
                wins += 1
                if wins == 100 and slug not in fast_w:
                    fast_w[slug] = (played, year)
                if 0 < margin <= 1:
                    pts1 += 1
                if opp:
                    if pair[opp] == 0:
                        pair_start[opp] = year
                    pair[opp] += 1
                    upd(("pairW",), pair[opp], slug, pair_start[opp], year, opp)
            else:
                if opp:
                    pair[opp] = 0
                if res == "L":
                    losses += 1
                    if losses == 100 and slug not in fast_l:
                        fast_l[slug] = (played, year)
        onept[slug] = pts1

    STREAKS = [
        (("overall", "W"), "Longest win streak", "wins", "{} games"),
        (("season", "W"), "Longest win streak in one season", "wins", "{} games"),
        (("begin", "W"), "Best start to a season", "wins", "{}–0 start"),
        (("overall", "L"), "Longest losing streak", "losses", "{} games"),
        (("season", "L"), "Longest losing streak in one season", "losses", "{} games"),
        (("begin", "L"), "Worst start to a season", "losses", "0–{} start"),
    ]
    for key, cat, scope, disp in STREAKS:
        b = best.get(key)
        if b:
            out.append((cat, scope, b[0], disp.format(b[0]), b[1], b[3], span(b[2], b[3])))
    b = best.get(("pairW",))
    if b:
        out.append(("Most consecutive wins over one opponent", "wins", b[0], f"{b[0]} straight",
                    b[1], b[3], f"vs {name_of.get(b[4], b[4])} · {span(b[2], b[3])}"))
    if fast_w:
        slug, (g, y) = min(fast_w.items(), key=lambda kv: kv[1][0])
        out.append(("Fastest to 100 wins", "wins", g, f"{g} games", slug, y, None))
    if fast_l:
        slug, (g, y) = min(fast_l.items(), key=lambda kv: kv[1][0])
        out.append(("Fastest to 100 losses", "losses", g, f"{g} games", slug, y, None))
    if onept:
        slug, n = max(onept.items(), key=lambda kv: kv[1])
        if n:
            out.append(("Most wins by 1 point or less", "wins", n, f"{n:g} wins", slug, None, "all-time"))

    # ---------------- championship records
    finals = conn.execute(
        "SELECT owner_slug o, year y, champion c FROM season_stats "
        "WHERE final_app=1 AND owner_slug IS NOT NULL ORDER BY owner_slug, year").fetchall()
    if finals:
        apps: dict[str, list] = defaultdict(list)
        for r in finals:
            apps[r["o"]].append((r["y"], r["c"]))
        slug, v = max(apps.items(), key=lambda kv: len(kv[1]))
        out.append(("Most championship game appearances", "titles", len(v), f"{len(v)} finals", slug, None, "all-time"))
        losses_by = {s: sum(1 for _, c in v if not c) for s, v in apps.items()}
        slug, n = max(losses_by.items(), key=lambda kv: kv[1])
        if n:
            out.append(("Most championship game losses", "titles", n, f"{n} losses", slug, None, "all-time"))
        wins_by = {s: sum(1 for _, c in v if c) for s, v in apps.items()}
        slug, n = max(wins_by.items(), key=lambda kv: kv[1])
        if n:
            out.append(("Most championships", "titles", n, f"{n} titles", slug, None, "all-time"))

        def consec(want_champ):
            bb = None
            for s, v in apps.items():
                years = [y for y, c in v if (c or not want_champ)]
                run, y0, prev = 0, None, None
                for y in years:
                    if prev is not None and y == prev + 1:
                        run += 1
                    else:
                        run, y0 = 1, y
                    prev = y
                    if bb is None or run > bb[0]:
                        bb = (run, s, y0, y)
            return bb

        for want, cat in ((False, "Most consecutive finals appearances"),
                          (True, "Most consecutive championships")):
            bb = consec(want)
            if bb and bb[0] >= 2:
                out.append((cat, "titles", bb[0], f"{bb[0]} straight", bb[1], bb[3], span(bb[2], bb[3])))
        fin_set = {(r["o"], r["y"]) for r in finals}
        cands = [(ln, s, y) for (s, y), ln in begin_l.items() if (s, y) in fin_set]
        if cands:
            ln, s, y = max(cands)
            out.append(("Worst season start by a finalist", "titles", ln,
                        f"0–{ln} start", s, y, None))

    for row in out:
        conn.execute(
            """INSERT OR REPLACE INTO records_book (category, scope, value, display, owner_slug, year, detail)
               VALUES (?,?,?,?,?,?,?)""",
            row,
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
