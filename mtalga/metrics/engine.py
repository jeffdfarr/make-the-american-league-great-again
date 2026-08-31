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

# A season's aggregate stats enter the record book once its REGULAR SEASON has
# ended (rs_complete, set by the sync the day after the final RS week) — no
# waiting on the playoffs. Champion years double as a backfill for old DBs.
COMP_YEARS_SQL = """SELECT year FROM seasons WHERE rs_complete=1
                    UNION SELECT DISTINCT year FROM season_stats WHERE champion=1"""


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
        # Excluded short seasons (2020) get no expectation: the regression
        # predicts full-length win totals, which makes a 6-game year look
        # like a -10-win catastrophe for everyone.
        skip = year in excluded
        for s in stats:
            if not skip and s["opr"] is not None and len(history) >= 2:
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
            def _blend(pp: list[float]) -> float:
                return (6 * (sum(pp) / len(pp)) + 2 * (max(pp) + min(pp)) + 400 * wp) / 10

            career_raw = _blend(ppgs)
            # League ruling: an excluded short season (2020) still counts toward a
            # manager's career blend when it HELPS them — nobody is punished for
            # the weird year, but production at modern standards isn't erased.
            all_ppgs = [se["ppg"] for se in seasons if se["rs_games"]]
            if all_ppgs != ppgs:
                career_raw = max(career_raw, _blend(all_ppgs))
            if league_norm:
                career_norm = career_raw / league_norm
        # A one-season career IS that season: use the season OPR directly.
        # The blend above needs best/worst SEASON PPGs, which collapse to the
        # same number for a first-year manager and drop the high/low-game
        # spread the season formula keeps (sheet shows the season OPR here).
        if n == 1 and seasons[0]["opr"] is not None:
            career_raw = seasons[0]["raw_opr"]
            career_norm = seasons[0]["opr"]

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
    comp = [r["year"] for r in conn.execute(COMP_YEARS_SQL)]
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
        # strength of schedule / points against (minimum-side: completed seasons only)
        ("Toughest schedule, season", "sos", "{:.3f}", None,
         "SELECT owner_slug o, sos_opr v, year y FROM season_stats WHERE sos_opr IS NOT NULL ORDER BY sos_opr DESC LIMIT 1"),
        ("Softest schedule, season", "sos", "{:.3f}", None,
         f"SELECT owner_slug o, sos_opr v, year y FROM season_stats WHERE sos_opr IS NOT NULL AND year IN ({comp_in}) ORDER BY sos_opr ASC LIMIT 1"),
        ("Most points against, season", "sos", "{:,.0f} pts", None,
         "SELECT owner_slug o, pa v, year y FROM season_stats ORDER BY pa DESC LIMIT 1"),
        ("Fewest points against, season", "sos", "{:,.0f} pts", None,
         f"SELECT owner_slug o, pa v, year y FROM season_stats WHERE year IN ({comp_in}) ORDER BY pa ASC LIMIT 1"),
        ("Most points against per game, career", "sos", "{:.1f} pts/gm", "all-time",
         "SELECT owner_slug o, SUM(pa)/SUM(rs_games) v, NULL y FROM season_stats WHERE owner_slug IS NOT NULL AND rs_games > 0 GROUP BY owner_slug ORDER BY v DESC LIMIT 1"),
        ("Fewest points against per game, career", "sos", "{:.1f} pts/gm", "all-time",
         "SELECT owner_slug o, SUM(pa)/SUM(rs_games) v, NULL y FROM season_stats WHERE owner_slug IS NOT NULL AND rs_games > 0 GROUP BY owner_slug ORDER BY v ASC LIMIT 1"),
        ("Best point differential, season", "sos", "{:+,.0f} pts", None,
         "SELECT owner_slug o, margin v, year y FROM season_stats WHERE margin IS NOT NULL ORDER BY margin DESC LIMIT 1"),
        ("Worst point differential, season", "sos", "{:+,.0f} pts", None,
         f"SELECT owner_slug o, margin v, year y FROM season_stats WHERE margin IS NOT NULL AND year IN ({comp_in}) ORDER BY margin ASC LIMIT 1"),
        # Luck ledger — luck = actual RS wins minus wins the OPR regression
        # expected. Completed full seasons only (2020 has no luck value).
        ("Luckiest season", "sos", "{:+.1f} wins", None,
         f"SELECT owner_slug o, luck v, year y FROM season_stats WHERE luck IS NOT NULL AND year IN ({comp_in}) ORDER BY luck DESC LIMIT 1"),
        ("Most cursed season", "sos", "{:+.1f} wins", None,
         f"SELECT owner_slug o, luck v, year y FROM season_stats WHERE luck IS NOT NULL AND year IN ({comp_in}) ORDER BY luck ASC LIMIT 1"),
        ("Luckiest career", "sos", "{:+.1f} wins", "all-time",
         f"SELECT owner_slug o, SUM(luck) v, NULL y FROM season_stats WHERE luck IS NOT NULL AND year IN ({comp_in}) GROUP BY owner_slug ORDER BY v DESC LIMIT 1"),
        ("Most cursed career", "sos", "{:+.1f} wins", "all-time",
         f"SELECT owner_slug o, SUM(luck) v, NULL y FROM season_stats WHERE luck IS NOT NULL AND year IN ({comp_in}) GROUP BY owner_slug ORDER BY v ASC LIMIT 1"),
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
    _position_records(conn)
    _roster_records(conn)
    _legend_records(conn)
    _rivalry_records(conn)


def _rivalry_records(conn: sqlite3.Connection) -> None:
    """Head-to-head rivalry records over counted games (regular season +
    championship bracket + 3rd-place game — consolation counts nothing)."""
    names = {r["slug"]: r["name"] for r in conn.execute("SELECT slug, name FROM owners")}
    who = lambda s: names.get(s, s or "?")
    years = [r["year"] for r in conn.execute("SELECT year FROM seasons ORDER BY year")]
    pair: dict[tuple, dict] = {}
    for year in years:
        post = _classify_postseason(conn, year)
        for r in conn.execute(
            """SELECT ts.owner_slug me, opp.owner_slug them, g.game_type, g.matchup_uid,
                      g.period, g.pts_for pf, g.pts_against pa
               FROM games g
               JOIN team_seasons ts ON ts.id = g.team_season_id
               JOIN team_seasons opp ON opp.id = g.opp_season_id
               WHERE g.year=? AND g.complete=1
                 AND ts.owner_slug IS NOT NULL AND opp.owner_slug IS NOT NULL
               ORDER BY g.period""", (year,)):
            if r["game_type"] != "R" and post.get(r["matchup_uid"]) not in ("TREE", "THIRD"):
                continue
            d = pair.setdefault((r["me"], r["them"]),
                                {"w": 0, "l": 0, "t": 0, "pf": 0.0, "pa": 0.0, "seq": []})
            res = "W" if r["pf"] > r["pa"] else ("L" if r["pf"] < r["pa"] else "T")
            d[res.lower()] += 1
            d["pf"] += r["pf"]
            d["pa"] += r["pa"]
            d["seq"].append((year, res))

    def put(category, value, display, owner, detail, note=None, year=None):
        conn.execute(
            """INSERT OR REPLACE INTO records_book (category, scope, value, display, owner_slug, year, detail, note)
               VALUES (?, 'rivalry', ?,?,?,?,?,?)""",
            (category, value, display, owner, year, detail, note))

    # most lopsided rivalry (min 10 meetings, best win pct, then most wins)
    best = None
    for (me, them), d in pair.items():
        g = d["w"] + d["l"]
        if d["w"] + d["l"] + d["t"] < 10 or g == 0:
            continue
        k = (d["w"] / g, d["w"])
        if best is None or k > best[0]:
            best = (k, me, them, d)
    if best:
        _, me, them, d = best
        put("Most lopsided rivalry", d["w"] / (d["w"] + d["l"]), f"{d['w']}–{d['l']}", me,
            f"vs {who(them)}", None)

    # never beaten him (min 6 meetings, zero wins, most losses)
    worst = None
    for (me, them), d in pair.items():
        if d["w"] == 0 and d["l"] >= 6 and (worst is None or d["l"] > worst[2]["l"]):
            worst = (me, them, d)
    if worst:
        me, them, d = worst
        put("Never beaten him", d["l"], f"0–{d['l']}", me, f"vs {who(them)}",
            "Most career meetings without a single win.")

    # the curse: longest ACTIVE run of consecutive losses to one manager
    curse = None
    for (me, them), d in pair.items():
        n = 0
        y0 = None
        for y, res in reversed(d["seq"]):
            if res == "L":
                n += 1
                y0 = y
            else:
                break
        if n >= 3 and (curse is None or n > curse[0]):
            curse = (n, me, them, y0)
    if curse:
        n, me, them, y0 = curse
        put("The Curse", n, f"{n} straight losses", me, f"to {who(them)} · since {y0}",
            "Still active — the streak lives until he finally wins one.")

    # career points scored against / allowed to one opponent
    mx = max(pair.items(), key=lambda kv: kv[1]["pf"], default=None)
    if mx:
        (me, them), d = mx
        put("Most career points vs one opponent", d["pf"], f"{d['pf']:,.0f} pts", me, f"vs {who(them)}")
    mx = max(pair.items(), key=lambda kv: kv[1]["pa"], default=None)
    if mx:
        (me, them), d = mx
        put("Most career points allowed to one opponent", d["pa"], f"{d['pa']:,.0f} pts", me, f"to {who(them)}")


def _roster_records(conn: sqlite3.Connection) -> None:
    """Bench crimes, Sunday comebacks, and roster-churn records — all from
    the per-day roster data. Bench = Reserve slots only (IR players could
    not legally start, so their points aren't 'left on the bench')."""
    owner_of = {r["id"]: r["owner_slug"] for r in conn.execute(
        "SELECT id, owner_slug FROM team_seasons WHERE owner_slug IS NOT NULL")}
    names = {r["slug"]: r["name"] for r in conn.execute("SELECT slug, name FROM owners")}

    def put(category, value, display, owner, year, detail, note=None):
        conn.execute(
            """INSERT OR REPLACE INTO records_book (category, scope, value, display, owner_slug, year, detail, note)
               VALUES (?, 'roster', ?,?,?,?,?,?)""",
            (category, value, display, owner, year, detail, note))

    # ---- bench points, week and season
    wk = conn.execute(
        """SELECT year y, week w, team_season_id ts, SUM(pts) v FROM position_points
           WHERE lower(status)='reserve' GROUP BY year, week, team_season_id
           ORDER BY v DESC LIMIT 1""").fetchone()
    if wk and wk["v"] and wk["ts"] in owner_of:
        lead = conn.execute(
            """SELECT player p, SUM(pts) v FROM position_points
               WHERE lower(status)='reserve' AND year=? AND week=? AND team_season_id=?
               GROUP BY player_id ORDER BY v DESC LIMIT 1""", (wk["y"], wk["w"], wk["ts"])).fetchone()
        put("Most points left on the bench, week", wk["v"], f"{wk['v']:,.1f} pts",
            owner_of[wk["ts"]], wk["y"], f"Wk {wk['w']} · {lead['p']} ({lead['v']:,.0f} of it)" if lead else f"Wk {wk['w']}")
    sn = conn.execute(
        """SELECT year y, team_season_id ts, SUM(pts) v FROM position_points
           WHERE lower(status)='reserve' GROUP BY year, team_season_id
           ORDER BY v DESC LIMIT 1""").fetchone()
    if sn and sn["v"] and sn["ts"] in owner_of:
        lead = conn.execute(
            """SELECT player p, SUM(pts) v FROM position_points
               WHERE lower(status)='reserve' AND year=? AND team_season_id=?
               GROUP BY player_id ORDER BY v DESC LIMIT 1""", (sn["y"], sn["ts"])).fetchone()
        put("Most points left on the bench, season", sn["v"], f"{sn['v']:,.0f} pts",
            owner_of[sn["ts"]], sn["y"], f"{lead['p']} ({lead['v']:,.0f} of it)" if lead else None)

    # ---- biggest final-day comeback
    day: dict[tuple, dict] = defaultdict(dict)
    for r in conn.execute(
        """SELECT year y, week w, team_season_id ts, daily d, SUM(pts) v FROM position_points
           WHERE lower(status)='active' GROUP BY year, week, team_season_id, daily"""):
        day[(r["y"], r["w"], r["ts"])][r["d"]] = r["v"]
    best = None
    for g in conn.execute(
        """SELECT year y, period w, team_season_id ts, opp_season_id opp, pts_for pf, pts_against pa
           FROM games WHERE complete=1 AND game_type='R'"""):
        if g["pf"] <= g["pa"] or g["ts"] not in owner_of:
            continue  # only winners can have come back
        mine, theirs = day.get((g["y"], g["w"], g["ts"]), {}), day.get((g["y"], g["w"], g["opp"]), {})
        days = sorted(set(mine) | set(theirs))
        if len(days) < 2:
            continue
        last = days[-1]
        deficit = sum(v for d, v in theirs.items() if d < last) - sum(v for d, v in mine.items() if d < last)
        if deficit > 0 and (best is None or deficit > best[0]):
            best = (deficit, owner_of[g["ts"]], g["y"], g["w"], owner_of.get(g["opp"]))
    if best:
        put("Biggest final-day comeback", best[0], f"down {best[0]:,.1f} pts", best[1], best[2],
            f"vs {names.get(best[4], best[4] or '?')} · Wk {best[3]}",
            "Trailed by this much entering the last day of the scoring week — and won.")

    # ---- roster churn
    comp = {r["year"] for r in conn.execute(COMP_YEARS_SQL)}
    full = {r["year"] for r in conn.execute("SELECT DISTINCT year FROM season_stats WHERE rs_games >= 15")}
    mostp = conn.execute(
        """SELECT year y, team_season_id ts, COUNT(DISTINCT player_id) v FROM position_points
           WHERE lower(status)='active' GROUP BY year, team_season_id ORDER BY v DESC LIMIT 1""").fetchone()
    if mostp and mostp["ts"] in owner_of:
        put("Most players used in a season", mostp["v"], f"{mostp['v']} players", owner_of[mostp["ts"]], mostp["y"], None)
    for r in conn.execute(
        """SELECT year y, team_season_id ts, COUNT(DISTINCT player_id) v FROM position_points
           WHERE lower(status)='active' GROUP BY year, team_season_id ORDER BY v ASC"""):
        if r["y"] in comp and r["y"] in full and r["ts"] in owner_of:
            put("Fewest players used in a season", r["v"], f"{r['v']} players", owner_of[r["ts"]], r["y"], None)
            break



def _legend_records(conn: sqlite3.Connection) -> None:
    """All-time leading scorer for each franchise (points while rostered,
    minors excluded — same convention as position records)."""
    rows = conn.execute(
        """SELECT ts.owner_slug o, pp.player p, SUM(pp.pts) v
           FROM position_points pp JOIN team_seasons ts ON ts.id = pp.team_season_id
           WHERE ts.owner_slug IS NOT NULL AND lower(pp.status) NOT LIKE 'min%'
           GROUP BY ts.owner_slug, pp.player_id""").fetchall()
    best: dict[str, tuple] = {}
    for r in rows:
        if r["o"] not in best or r["v"] > best[r["o"]][0]:
            best[r["o"]] = (r["v"], r["p"])
    for slug, (v, player) in best.items():
        conn.execute(
            """INSERT OR REPLACE INTO records_book (category, scope, value, display, owner_slug, year, detail, note)
               VALUES (?, 'legend', ?,?,?,?,?,?)""",
            (f"legend:{slug}", v, f"{v:,.0f} pts", slug, None, player, None))


POSITION_SLOTS = [
    ("SP", "SP"), ("RP", "Closer (RP)"),
    ("C", "C"), ("1B", "1B"), ("2B", "2B"), ("3B", "3B"),
    ("SS", "SS"), ("OF", "OF"), ("UT", "UT"),
]
HITTER_LADDER = ["C", "SS", "2B", "3B", "OF", "1B", "UT"]  # scarcest first; UT = DH-only
PITCH_SLOT_KEYS = {"SP", "RP", "P"}
HIT_SLOT_KEYS = {"C", "1B", "2B", "3B", "SS", "OF", "UT"}
TWO_WAY_MIN = 50.0  # active pts on BOTH sides to count as a two-way season


def _position_overrides() -> dict:
    """Optional config/position_overrides.yaml: {"Player Name": "SP", ...} —
    commissioner power over classification when the rules get one wrong."""
    try:
        import yaml
        from pathlib import Path
        p = Path(__file__).resolve().parent.parent.parent / "config" / "position_overrides.yaml"
        if p.exists():
            return {str(k).lower(): str(v).upper() for k, v in (yaml.safe_load(p.read_text()) or {}).items()}
    except Exception as e:
        print(f"[records] position_overrides.yaml skipped: {e}")
    return {}


def _position_records(conn: sqlite3.Connection) -> None:
    """Regular-season position records, league convention (2026-08):
    a player's FULL RS points count — started, benched, or on IR — but
    minor-league slot days don't. Classification: pitchers are SP unless
    their eligibility is relief-only (that's a Closer); hitters count at
    their SCARCEST eligible position (C > SS > 2B > 3B > OF > 1B > UT);
    two-way seasons split into a pitching half and a hitting half by the
    slots actually used. Falls back to actual-slot classification when a
    player's eligibility wasn't harvested."""
    labels = dict(POSITION_SLOTS)
    owner_of = {r["id"]: r["owner_slug"] for r in conn.execute(
        "SELECT id, owner_slug FROM team_seasons WHERE owner_slug IS NOT NULL")}
    elig: dict[tuple, set] = {}
    try:
        for r in conn.execute("SELECT year, player_id, positions FROM player_eligibility"):
            elig[(r["year"], r["player_id"])] = {
                p.strip().upper() for p in (r["positions"] or "").replace("/", ",").split(",") if p.strip()}
    except sqlite3.OperationalError:
        pass
    overrides = _position_overrides()

    P: dict[tuple, dict] = {}  # (y, ts, pid) -> aggregates
    for r in conn.execute(
        """SELECT year y, team_season_id ts, player_id pid, player p, slot, status st, week w, SUM(pts) v
           FROM position_points GROUP BY year, team_season_id, player_id, slot, status, week"""):
        st = (r["st"] or "").lower()
        if st.startswith("min"):
            continue
        if r["ts"] not in owner_of:
            continue
        e = P.setdefault((r["y"], r["ts"], r["pid"]),
                         {"player": r["p"], "act": defaultdict(float),
                          "actw": defaultdict(float), "bench": 0.0, "benchw": defaultdict(float)})
        if r["p"]:
            e["player"] = r["p"]
        if st == "active":
            e["act"][r["slot"]] += r["v"]
            side = "P" if r["slot"] in PITCH_SLOT_KEYS else "H"
            e["actw"][(side, r["w"])] += r["v"]
        else:
            e["bench"] += r["v"]
            e["benchw"][r["w"]] += r["v"]

    best_season: dict[str, tuple] = {}   # pos -> (v, player, owner, year, note)
    best_week: dict[str, tuple] = {}     # pos -> (v, player, owner, year, wk, note)

    def consider(pos, v, player, o, y, note):
        if pos and v and (pos not in best_season or v > best_season[pos][0]):
            best_season[pos] = (v, player, o, y, note)

    def consider_week(pos, v, player, o, y, wk, note):
        if pos and v and (pos not in best_week or v > best_week[pos][0]):
            best_week[pos] = (v, player, o, y, wk, note)

    for (y, ts, pid), e in P.items():
        act_p = sum(v for s, v in e["act"].items() if s in PITCH_SLOT_KEYS)
        act_h = sum(v for s, v in e["act"].items() if s in HIT_SLOT_KEYS)
        if act_p <= 0 and act_h <= 0:
            continue
        o = owner_of[ts]
        eset = elig.get((y, pid), set())
        ov = overrides.get((e["player"] or "").lower())
        two_way = act_p >= TWO_WAY_MIN and act_h >= TWO_WAY_MIN
        act_tot = act_p + act_h

        def top_slot(keys):
            d = {s: v for s, v in e["act"].items() if s in keys and v > 0}
            return max(d.items(), key=lambda kv: kv[1])[0] if d else None

        def pitch_pos():
            if ov in ("SP", "RP"):
                return ov, "Commissioner override."
            note = None
            if eset & {"SP", "RP"}:
                pos = "SP" if "SP" in eset else "RP"
            else:
                pos = "SP" if e["act"].get("SP", 0) > 0 else "RP"
                if not eset:
                    note = None  # slot fallback, nothing to explain
            ts_ = top_slot(PITCH_SLOT_KEYS)
            if ts_ and ts_ != pos and pos == "SP":
                note = f"SP-eligible; mostly filled {ts_} slots that season."
            return pos, note

        def hit_pos():
            if ov in HIT_SLOT_KEYS:
                return ov, "Commissioner override."
            note = None
            pos = None
            hset = {("UT" if p == "DH" else p) for p in eset} & set(HITTER_LADDER)
            if hset:
                for cand in HITTER_LADDER:
                    if cand in hset:
                        pos = cand
                        break
            if pos is None:
                pos = top_slot(HIT_SLOT_KEYS)
            ts_ = top_slot(HIT_SLOT_KEYS)
            if ts_ and pos and ts_ != pos:
                note = f"Counted at {pos} (scarcest eligible position); mostly filled {ts_} slots."
            return pos, note

        if two_way:
            share_p = act_p / act_tot
            tw = "Two-way season — pitching and hitting days counted separately. "
            pos, note = pitch_pos()
            consider(pos, act_p + e["bench"] * share_p, e["player"], o, y, tw + (note or ""))
            hpos, hnote = hit_pos()
            consider(hpos, act_h + e["bench"] * (1 - share_p), e["player"], o, y, tw + (hnote or ""))
            dom = "P" if act_p >= act_h else "H"
            for (side, wk), v in e["actw"].items():
                wv = v + (e["benchw"].get(wk, 0.0) if side == dom else 0.0)
                if side == "P":
                    consider_week(pos, wv, e["player"], o, y, wk, tw.strip())
                else:
                    consider_week(hpos, wv, e["player"], o, y, wk, tw.strip())
        else:
            if act_p >= act_h:
                pos, note = pitch_pos()
            else:
                pos, note = hit_pos()
            total = act_tot + e["bench"]
            consider(pos, total, e["player"], o, y, note)
            wktot: dict[int, float] = defaultdict(float)
            for (_side, wk), v in e["actw"].items():
                wktot[wk] += v
            for wk, v in e["benchw"].items():
                wktot[wk] += v
            for wk, v in wktot.items():
                consider_week(pos, v, e["player"], o, y, wk, note)

    for slot, label in POSITION_SLOTS:
        b = best_season.get(slot)
        if b and b[0]:
            conn.execute(
                """INSERT OR REPLACE INTO records_book (category, scope, value, display, owner_slug, year, detail, note)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (f"Most points from {label}", "position", b[0], f"{b[0]:,.0f} pts",
                 b[2], b[3], b[1], (b[4] or "").strip() or None),
            )
        b = best_week.get(slot)
        if b and b[0]:
            conn.execute(
                """INSERT OR REPLACE INTO records_book (category, scope, value, display, owner_slug, year, detail, note)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (f"Most points from {label}, week", "position-week", b[0], f"{b[0]:,.1f} pts",
                 b[2], b[3], f"{b[1]} · Wk {b[4]}", (b[5] or "").strip() or None),
            )


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
    completed = sorted(r["year"] for r in conn.execute(COMP_YEARS_SQL))
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
