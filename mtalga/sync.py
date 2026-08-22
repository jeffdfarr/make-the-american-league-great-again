"""Pull one Fantrax season into the database. Idempotent — safe to rerun.

Used by both the nightly job (current season) and the backfill (all seasons).
"""

from __future__ import annotations

import datetime as dt
import sqlite3

from . import db as dbm
from .config import Config, SeasonCfg
from .fantrax_client import open_league


def log(conn: sqlite3.Connection, year: int | None, step: str, ok: bool, message: str = "") -> None:
    conn.execute(
        "INSERT INTO sync_log (ran_at, year, step, ok, message) VALUES (?,?,?,?,?)",
        (dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), year, step, int(ok), message),
    )
    print(f"[{'ok' if ok else 'FAIL'}] {year} {step} {message}")


def classify_game_type(playoffs: bool, bracket: str | None) -> str:
    """R = regular season; P = playoff (main bracket); C = consolation.

    The champion is derived later (winner of the main-bracket game in the
    final playoff period), so no 'F' tagging happens here.
    """
    if not playoffs:
        return "R"
    if bracket is None:
        return "P"
    name = bracket.lower()
    if any(word in name for word in ("consolation", "toilet", "sacko", "loser", "3rd", "third", "5th", "7th")):
        return "C"
    if any(word in name for word in ("championship", "final")):
        return "P"
    return "C"  # unknown side brackets are consolation until told otherwise


def sync_season(conn: sqlite3.Connection, cfg: Config, season: SeasonCfg, session=None) -> None:
    year = season.year
    lg = open_league(season.league_id, session=session)

    # --- season row + verification that the ID resolves to the expected year
    api_year = str(getattr(lg, "year", "") or "")
    api_name = str(getattr(lg, "name", "") or "")
    ok = str(year) in api_year if api_year else True
    log(conn, year, "league.verify", ok,
        f"id={season.league_id} api_name={api_name!r} api_year={api_year!r}"
        + ("" if ok else "  <-- YEAR MISMATCH, check seasons.yaml"))
    conn.execute(
        """INSERT INTO seasons (year, fantrax_league_id, league_name, reg_season_games, playoff_teams, byes, notes)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(year) DO UPDATE SET
             fantrax_league_id=excluded.fantrax_league_id,
             league_name=excluded.league_name,
             reg_season_games=excluded.reg_season_games,
             playoff_teams=excluded.playoff_teams,
             byes=excluded.byes,
             notes=excluded.notes""",
        (year, season.league_id, api_name, season.reg_season_games,
         season.playoff_teams, season.byes, season.notes),
    )

    # --- teams
    mapping = cfg.team_map.get(year, {})
    ts_ids: dict[str, int] = {}
    for team in lg.teams:
        owner = mapping.get(team.id)
        conn.execute(
            """INSERT INTO team_seasons (year, owner_slug, fantrax_team_id, team_name, team_short, logo_url)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(year, fantrax_team_id) DO UPDATE SET
                 owner_slug=excluded.owner_slug, team_name=excluded.team_name,
                 team_short=excluded.team_short, logo_url=excluded.logo_url""",
            (year, owner, team.id, team.name, team.short, getattr(team, "logo", None)),
        )
        row = conn.execute(
            "SELECT id FROM team_seasons WHERE year=? AND fantrax_team_id=?",
            (year, team.id),
        ).fetchone()
        ts_ids[team.id] = row["id"]
    unmapped = [t.id for t in lg.teams if t.id not in mapping]
    log(conn, year, "teams", not unmapped,
        f"{len(ts_ids)} teams" + (f"; UNMAPPED (fill owners.yaml team_map): {unmapped}" if unmapped else ""))

    # --- games from scoring period results (regular season + playoffs + brackets)
    results = lg.scoring_period_results(season=True, playoffs=True)
    n_games = 0
    for _, spr in sorted(results.items()):
        if getattr(spr, "future", False):
            continue
        period = spr.period.number
        buckets: list[tuple[str | None, list]] = [(None, spr.matchups)]
        for bracket_name, matchups in getattr(spr, "other_brackets", {}).items():
            buckets.append((bracket_name, matchups))
        for bracket, matchups in buckets:
            gtype = classify_game_type(spr.playoffs, bracket)
            for m in matchups:
                away, home = m.away, m.home
                # Placeholder rows ("TBD" strings before a bracket is set)
                if not hasattr(away, "id") or not hasattr(home, "id"):
                    continue
                if m.away_score == 0 and m.home_score == 0 and not spr.complete:
                    continue
                uid = f"{year}:{period}:" + ":".join(sorted([away.id, home.id]))
                for me, opp, pf, pa, is_home in (
                    (away, home, m.away_score, m.home_score, 0),
                    (home, away, m.home_score, m.away_score, 1),
                ):
                    conn.execute(
                        """INSERT INTO games (year, period, period_name, bracket, game_type, matchup_uid,
                                              team_season_id, opp_season_id, pts_for, pts_against, is_home, complete)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(matchup_uid, team_season_id) DO UPDATE SET
                             pts_for=excluded.pts_for, pts_against=excluded.pts_against,
                             game_type=excluded.game_type, bracket=excluded.bracket,
                             period_name=excluded.period_name, complete=excluded.complete""",
                        (year, period, spr.name, bracket, gtype, uid,
                         ts_ids[me.id], ts_ids[opp.id], pf, pa, is_home, int(spr.complete)),
                    )
                n_games += 1
    log(conn, year, "games", True, f"{n_games} team-games upserted")

    # --- transactions (moves)
    try:
        txs = lg.transactions(count=1000)
        n = 0
        for tx in txs:
            for p in tx.players:
                conn.execute(
                    """INSERT OR REPLACE INTO transactions
                       (fantrax_tx_id, year, team_season_id, tx_date, kind, player)
                       VALUES (?,?,?,?,?,?)""",
                    (tx.id, year, ts_ids.get(tx.team.id),
                     tx.date.isoformat() if tx.date else None,
                     getattr(p, "type", None) or getattr(p, "claim_type", None), str(getattr(p, "name", p))),
                )
                n += 1
        log(conn, year, "transactions", True, f"{n} rows")
    except Exception as exc:  # transactions need login; degrade gracefully
        log(conn, year, "transactions", True, f"WARN skipped (moves not counted yet): {exc}")

    conn.commit()


def discover(season: SeasonCfg, session=None) -> None:
    """Print a season's teams so owners.yaml team_map can be filled in."""
    lg = open_league(season.league_id, session=session)
    print(f"\n{season.year}  {getattr(lg, 'name', '?')}  (api year: {getattr(lg, 'year', '?')})")
    print(f"  league_id: {season.league_id}")
    for team in lg.teams:
        print(f"    {team.id}: \"{team.name}\" ({team.short})")
    print("\nPaste into config/owners.yaml under team_map:")
    print(f"  {season.year}:")
    for team in lg.teams:
        print(f"    {team.id}: <owner_slug>   # {team.name}")


def seed_owners(conn: sqlite3.Connection, cfg: Config) -> None:
    for o in cfg.owners:
        conn.execute(
            """INSERT INTO owners (slug, name, joined, left_year) VALUES (?,?,?,?)
               ON CONFLICT(slug) DO UPDATE SET name=excluded.name,
                 joined=excluded.joined, left_year=excluded.left_year""",
            (o["slug"], o["name"], o.get("joined"), o.get("left")),
        )
    conn.commit()
