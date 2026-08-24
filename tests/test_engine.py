"""Integration tests: synthetic mini-leagues through the full engine.

Conventions under test (verified against the League History sheet):
  * points/high/low are regular-season only
  * W/L counts RS + championship bracket + 3rd-place game
  * consolation games (named brackets, or main-bracket games among
    non-qualifiers) count nothing
  * byes, finals, and champions fall out of the bracket walk
"""

import math

import pytest

from mtalga import db as dbm
from mtalga.config import Config, SeasonCfg
from mtalga.metrics.engine import rebuild


@pytest.fixture
def conn(tmp_path):
    return dbm.connect(tmp_path / "test.db")


def make_cfg(year, slugs):
    return Config(
        seasons={year: SeasonCfg(year=year, league_id="x")},
        current_season=year + 1,
        owners=[{"slug": s, "name": s.title()} for s in slugs],
        team_map={},
        adjustments=[],
    )


def seed(conn, year, slugs):
    for s in slugs:
        conn.execute("INSERT INTO owners (slug, name) VALUES (?,?)", (s, s.title()))
    conn.execute("INSERT INTO seasons (year, fantrax_league_id) VALUES (?, 'x')", (year,))
    ids = {}
    for s in slugs:
        cur = conn.execute(
            "INSERT INTO team_seasons (year, owner_slug, fantrax_team_id, team_name) VALUES (?,?,?,?)",
            (year, s, f"ft_{s}", s.upper()),
        )
        ids[s] = cur.lastrowid

    def game(period, t1, p1, t2, p2, gtype, bracket=None):
        uid = f"{year}:{period}:" + ":".join(sorted([f"ft_{t1}", f"ft_{t2}"]))
        for me, opp, pf, pa, home in ((t1, t2, p1, p2, 0), (t2, t1, p2, p1, 1)):
            conn.execute(
                """INSERT INTO games (year, period, period_name, bracket, game_type, matchup_uid,
                                      team_season_id, opp_season_id, pts_for, pts_against, is_home, complete)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,1)""",
                (year, period, f"Period {period}", bracket, gtype, uid, ids[me], ids[opp], pf, pa, home),
            )
    return ids, game


def test_simple_bracket(conn):
    slugs = ["a", "b", "c", "d"]
    ids, game = seed(conn, 2025, slugs)
    # regular season
    game(1, "a", 120, "b", 100, "R"); game(1, "c", 90, "d", 95, "R")
    game(2, "a", 130, "c", 110, "R"); game(2, "b", 105, "d", 115, "R")
    game(3, "a", 140, "d", 100, "R"); game(3, "b", 125, "c", 96, "R")
    # playoffs: semis + final; consolation in a named bracket
    game(4, "a", 150, "d", 120, "P"); game(4, "b", 110, "c", 108, "P")
    game(5, "a", 160, "b", 130, "P")
    game(5, "c", 100, "d", 90, "C", bracket="Consolation")
    conn.commit()
    rebuild(conn, make_cfg(2025, slugs))

    ss = {r["owner_slug"]: dict(r) for r in conn.execute("SELECT * FROM season_stats")}

    a = ss["a"]
    assert (a["w"], a["l"]) == (5, 0)
    assert a["champion"] == 1 and a["final_app"] == 1
    assert a["pf"] == 120 + 130 + 140          # RS only — playoff points excluded
    assert a["high_game"] == 140 and a["low_game"] == 120
    assert a["ppg"] == a["pf"] / 3
    assert a["finish"] == 1

    c = ss["c"]
    assert (c["w"], c["l"]) == (0, 4)          # RS 0-3 + semi loss; consolation not counted
    assert c["pf"] == 90 + 110 + 96            # RS only

    d = ss["d"]
    assert d["playoff_app"] == 1 and d["champion"] == 0

    oprs = [s["opr"] for s in ss.values()]
    assert math.isclose(sum(oprs) / len(oprs), 1.0, abs_tol=0.02)

    cs = {r["owner_slug"]: dict(r) for r in conn.execute("SELECT * FROM career_stats")}
    assert cs["a"]["titles"] == 1

    rec = conn.execute("SELECT * FROM records_book WHERE category='Highest single-week score'").fetchone()
    assert rec["owner_slug"] == "a" and rec["value"] == 140  # RS games only


def test_mtalga_2021_shape(conn):
    """Byes, consolation-in-main-bracket, and a named 3rd-place game."""
    slugs = list("abcdefgh")
    ids, game = seed(conn, 2021, slugs)
    # one RS period (records: a best ... h worst)
    game(1, "a", 200, "h", 100, "R"); game(1, "b", 190, "g", 110, "R")
    game(1, "c", 180, "f", 120, "R"); game(1, "e", 170, "d", 130, "R")
    # p2: quarterfinals (c>d, e>f) + a consolation game hiding in the MAIN bracket (g>h)
    game(2, "c", 150, "d", 140, "P")
    game(2, "e", 155, "f", 135, "P")
    game(2, "g", 160, "h", 120, "P")           # main bracket, but winners go nowhere
    # p3: semis — a and b enter on byes
    game(3, "a", 170, "c", 150, "P")
    game(3, "b", 165, "e", 145, "P")
    # p4: final + named 3rd-place game
    game(4, "a", 180, "b", 170, "P")
    game(4, "c", 150, "e", 140, "C", bracket="3rd Place")
    conn.commit()
    rebuild(conn, make_cfg(2021, slugs))

    ss = {r["owner_slug"]: dict(r) for r in conn.execute("SELECT * FROM season_stats")}

    # champion and byes
    assert ss["a"]["champion"] == 1 and ss["a"]["bye"] == 1
    assert ss["b"]["final_app"] == 1 and ss["b"]["bye"] == 1 and ss["b"]["champion"] == 0

    # g's main-bracket win is consolation: not counted anywhere
    assert (ss["g"]["w"], ss["g"]["l"]) == (0, 1)          # RS loss only; p2 win excluded
    assert ss["g"]["playoff_app"] == 0
    assert (ss["h"]["w"], ss["h"]["l"]) == (0, 1)          # RS loss only; p2 loss excluded

    # c: RS win + QF win + semi loss + 3rd-place win => 3-1, playoff record 1-1
    assert (ss["c"]["w"], ss["c"]["l"]) == (3, 1)
    assert (ss["c"]["playoff_w"], ss["c"]["playoff_l"]) == (1, 1)
    assert ss["c"]["pf"] == 180                             # RS points only

    # finish order: a, b, then 3rd-place winner c, then e
    assert ss["a"]["finish"] == 1 and ss["b"]["finish"] == 2
    assert ss["c"]["finish"] == 3 and ss["e"]["finish"] == 4

    cs = {r["owner_slug"]: dict(r) for r in conn.execute("SELECT * FROM career_stats")}
    assert cs["a"]["byes"] == 1 and cs["a"]["titles"] == 1
    assert cs["g"]["playoff_apps"] == 0


def test_adjustments_applied_last(conn):
    slugs = ["a", "b", "c", "d"]
    ids, game = seed(conn, 2025, slugs)
    game(1, "a", 120, "b", 100, "R"); game(1, "c", 90, "d", 95, "R")
    conn.commit()
    cfg = make_cfg(2025, slugs)
    cfg.adjustments = [{
        "table": "season_stats", "key": {"owner": "a", "year": 2025},
        "column": "pf", "delta": -7.333, "reason": "test correction",
    }]
    rebuild(conn, cfg)
    a = conn.execute("SELECT pf FROM season_stats WHERE owner_slug='a'").fetchone()
    assert math.isclose(a["pf"], 120 - 7.333, abs_tol=1e-9)


def test_rebuild_is_idempotent(conn):
    slugs = ["a", "b", "c", "d"]
    ids, game = seed(conn, 2025, slugs)
    game(1, "a", 120, "b", 100, "R"); game(1, "c", 90, "d", 95, "R")
    game(2, "a", 150, "c", 120, "P")
    conn.commit()
    cfg = make_cfg(2025, slugs)
    rebuild(conn, cfg)
    first = [dict(r) for r in conn.execute("SELECT * FROM season_stats ORDER BY owner_slug")]
    rebuild(conn, cfg)
    second = [dict(r) for r in conn.execute("SELECT * FROM season_stats ORDER BY owner_slug")]
    assert first == second
