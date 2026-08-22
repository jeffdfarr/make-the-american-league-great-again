"""Integration test: a synthetic 4-team mini-league through the full engine."""

import math

import pytest

from mtalga import db as dbm
from mtalga.config import Config, SeasonCfg
from mtalga.metrics.engine import rebuild


@pytest.fixture
def conn(tmp_path):
    return dbm.connect(tmp_path / "test.db")


@pytest.fixture
def cfg():
    return Config(
        seasons={2025: SeasonCfg(year=2025, league_id="x")},
        current_season=2026,
        owners=[{"slug": s, "name": s.title()} for s in ("a", "b", "c", "d")],
        team_map={},
        adjustments=[],
    )


def seed_mini_league(conn):
    for s in ("a", "b", "c", "d"):
        conn.execute("INSERT INTO owners (slug, name) VALUES (?,?)", (s, s.title()))
    conn.execute("INSERT INTO seasons (year, fantrax_league_id) VALUES (2025, 'x')")
    ids = {}
    for s in ("a", "b", "c", "d"):
        cur = conn.execute(
            "INSERT INTO team_seasons (year, owner_slug, fantrax_team_id, team_name) VALUES (2025,?,?,?)",
            (s, f"ft_{s}", s.upper()),
        )
        ids[s] = cur.lastrowid

    def game(period, t1, p1, t2, p2, gtype, bracket=None):
        uid = f"2025:{period}:" + ":".join(sorted([f"ft_{t1}", f"ft_{t2}"]))
        for me, opp, pf, pa, home in ((t1, t2, p1, p2, 0), (t2, t1, p2, p1, 1)):
            conn.execute(
                """INSERT INTO games (year, period, period_name, bracket, game_type, matchup_uid,
                                      team_season_id, opp_season_id, pts_for, pts_against, is_home, complete)
                   VALUES (2025,?,?,?,?,?,?,?,?,?,?,1)""",
                (period, f"Period {period}", bracket, gtype, uid, ids[me], ids[opp], pf, pa, home),
            )

    # regular season: 3 periods, round robin
    game(1, "a", 120, "b", 100, "R"); game(1, "c", 90, "d", 95, "R")
    game(2, "a", 130, "c", 110, "R"); game(2, "b", 105, "d", 115, "R")
    game(3, "a", 140, "d", 100, "R"); game(3, "b", 125, "c", 96, "R")
    # playoffs: semis (P) + final (P) + consolation (C)
    game(4, "a", 150, "d", 120, "P"); game(4, "b", 110, "c", 108, "P")
    game(5, "a", 160, "b", 130, "P")
    game(5, "c", 100, "d", 90, "C", bracket="Consolation")
    conn.commit()
    return ids


def test_full_rebuild(conn, cfg):
    seed_mini_league(conn)
    rebuild(conn, cfg)

    ss = {r["owner_slug"]: dict(r) for r in conn.execute("SELECT * FROM season_stats")}

    # a: 3-0 regular, 2-0 playoffs, champion, 5 counted games (consolation none)
    a = ss["a"]
    assert (a["rs_w"], a["rs_l"]) == (3, 0)
    assert (a["w"], a["l"]) == (5, 0)
    assert a["champion"] == 1 and a["final_app"] == 1 and a["playoff_app"] == 1
    assert a["pf"] == 120 + 130 + 140 + 150 + 160
    assert a["high_game"] == 160 and a["low_game"] == 120
    assert a["finish"] == 1

    # c: consolation game excluded from W/L and points (sheet convention)
    c = ss["c"]
    assert (c["w"], c["l"]) == (0, 4)          # 0-3 RS + semi loss; consolation win not counted
    assert c["pf"] == 90 + 110 + 96 + 108       # no 100 from consolation

    # d: lost the semi, played consolation; not a finalist
    d = ss["d"]
    assert d["playoff_app"] == 1 and d["final_app"] == 0 and d["champion"] == 0

    # OPR: normalized values average ~1.0 across the league
    oprs = [s["opr"] for s in ss.values()]
    assert all(o is not None for o in oprs)
    assert math.isclose(sum(oprs) / len(oprs), 1.0, abs_tol=0.02)
    assert ss["a"]["opr"] == max(oprs)

    # career table exists for all owners who played
    cs = {r["owner_slug"]: dict(r) for r in conn.execute("SELECT * FROM career_stats")}
    assert cs["a"]["titles"] == 1 and cs["a"]["seasons"] == 1
    assert cs["b"]["titles"] == 0

    # H2H is symmetric: a vs b mirror of b vs a
    ab = conn.execute("SELECT * FROM h2h WHERE owner_a='a' AND owner_b='b' AND game_type='ALL'").fetchone()
    ba = conn.execute("SELECT * FROM h2h WHERE owner_a='b' AND owner_b='a' AND game_type='ALL'").fetchone()
    assert ab["w"] == ba["l"] == 2
    assert ab["pf"] == ba["pa"]

    # records book found the top game
    rec = conn.execute("SELECT * FROM records_book WHERE category='Most points, game'").fetchone()
    assert rec["owner_slug"] == "a" and rec["value"] == 160


def test_adjustments_applied_last(conn, cfg):
    seed_mini_league(conn)
    cfg.adjustments = [{
        "table": "season_stats",
        "key": {"owner": "a", "year": 2025},
        "column": "pf",
        "delta": -7.333,
        "reason": "test correction",
    }]
    rebuild(conn, cfg)
    a = conn.execute("SELECT pf FROM season_stats WHERE owner_slug='a'").fetchone()
    assert math.isclose(a["pf"], 700 - 7.333, abs_tol=1e-9)


def test_rebuild_is_idempotent(conn, cfg):
    seed_mini_league(conn)
    rebuild(conn, cfg)
    first = [dict(r) for r in conn.execute("SELECT * FROM season_stats ORDER BY owner_slug")]
    rebuild(conn, cfg)
    second = [dict(r) for r in conn.execute("SELECT * FROM season_stats ORDER BY owner_slug")]
    assert first == second
