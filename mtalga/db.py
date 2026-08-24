"""SQLite schema and helpers. The database is the single source of truth.

Source tables are written only by the sync; derived tables are dropped and
rebuilt from scratch by the metrics engine on every run.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "mtalga.db"

SCHEMA = """
-- ---------- source of truth (written by sync) ----------
CREATE TABLE IF NOT EXISTS owners (
    slug        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    joined      INTEGER,
    left_year   INTEGER
);

CREATE TABLE IF NOT EXISTS seasons (
    year              INTEGER PRIMARY KEY,
    fantrax_league_id TEXT NOT NULL UNIQUE,
    league_name       TEXT,                 -- as reported by the API (verification)
    reg_season_games  INTEGER,
    playoff_teams     INTEGER,
    byes              INTEGER,
    notes             TEXT
);

CREATE TABLE IF NOT EXISTS team_seasons (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    year            INTEGER NOT NULL REFERENCES seasons(year),
    owner_slug      TEXT REFERENCES owners(slug),   -- NULL until owner_map filled
    fantrax_team_id TEXT NOT NULL,
    team_name       TEXT,
    team_short      TEXT,
    logo_url        TEXT,
    conference      TEXT,
    UNIQUE (year, fantrax_team_id)
);

-- One row per team-game (two rows per matchup) keeps win/loss queries simple;
-- matchup_uid ties the pair together and makes upserts idempotent.
CREATE TABLE IF NOT EXISTS games (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    year            INTEGER NOT NULL REFERENCES seasons(year),
    period          INTEGER NOT NULL,
    period_name     TEXT,                 -- raw Fantrax caption, for auditing
    period_days     INTEGER,              -- length of the scoring period in days
    bracket         TEXT,                 -- NULL = main bracket; else Fantrax bracket name
    game_type       TEXT NOT NULL,        -- 'R' regular, 'P' playoff, 'C' consolation, 'F' final
    matchup_uid     TEXT NOT NULL,        -- year:period:sortedTeamIds
    team_season_id  INTEGER NOT NULL REFERENCES team_seasons(id),
    opp_season_id   INTEGER NOT NULL REFERENCES team_seasons(id),
    pts_for         REAL NOT NULL,
    pts_against     REAL NOT NULL,
    is_home         INTEGER NOT NULL DEFAULT 0,
    complete        INTEGER NOT NULL DEFAULT 1,
    UNIQUE (matchup_uid, team_season_id)
);

CREATE TABLE IF NOT EXISTS transactions (
    fantrax_tx_id   TEXT NOT NULL,
    year            INTEGER NOT NULL REFERENCES seasons(year),
    team_season_id  INTEGER REFERENCES team_seasons(id),
    tx_date         TEXT,
    kind            TEXT,                 -- CLAIM / DROP / TRADE / ...
    player          TEXT,
    PRIMARY KEY (fantrax_tx_id, player)
);

-- Per-team season category totals (SEASON_STATS standings view):
-- side 'H' hitting / 'P' pitching, stat 'HR','SB','QS','SV',...
CREATE TABLE IF NOT EXISTS category_stats (
    year            INTEGER NOT NULL REFERENCES seasons(year),
    team_season_id  INTEGER NOT NULL REFERENCES team_seasons(id),
    side            TEXT NOT NULL,
    stat            TEXT NOT NULL,
    value           REAL,
    PRIMARY KEY (year, team_season_id, side, stat)
);

-- Daily points credited to each lineup slot (from per-day roster fetches);
-- week = the weekly scoring period the day belongs to. Regular season only.
CREATE TABLE IF NOT EXISTS position_points (
    year            INTEGER NOT NULL REFERENCES seasons(year),
    week            INTEGER NOT NULL,
    daily           INTEGER NOT NULL,
    team_season_id  INTEGER NOT NULL REFERENCES team_seasons(id),
    slot            TEXT NOT NULL,      -- 'SP','RP','P','C','1B',...
    pts             REAL NOT NULL,
    PRIMARY KEY (year, daily, team_season_id, slot)
);

-- Weekly hitting/pitching split (filled by the roster fetch; nullable early on)
CREATE TABLE IF NOT EXISTS period_splits (
    year            INTEGER NOT NULL,
    period          INTEGER NOT NULL,
    team_season_id  INTEGER NOT NULL REFERENCES team_seasons(id),
    pitching_pts    REAL,
    hitting_pts     REAL,
    PRIMARY KEY (year, period, team_season_id)
);

-- ---------- derived (rebuilt nightly by metrics engine) ----------
CREATE TABLE IF NOT EXISTS season_stats (
    team_season_id  INTEGER PRIMARY KEY REFERENCES team_seasons(id),
    year            INTEGER NOT NULL,
    owner_slug      TEXT,
    games           INTEGER, w INTEGER, l INTEGER, t INTEGER,
    rs_games        INTEGER, rs_w INTEGER, rs_l INTEGER, rs_t INTEGER,
    win_pct         REAL, rs_win_pct REAL,
    finish          INTEGER,
    pf REAL, pa REAL, ppg REAL, papg REAL,
    high_game REAL, low_game REAL,
    pitching_pf REAL, hitting_pf REAL,
    moves           INTEGER,
    margin REAL, margin_pg REAL,
    sos_win_pct REAL, sos_opr REAL,
    raw_opr REAL, opr REAL,
    strd_high REAL, alt_raw_opr REAL, alt_opr REAL,
    expected_wins REAL, luck REAL,
    playoff_app INTEGER, bye INTEGER,
    playoff_w INTEGER, playoff_l INTEGER,
    final_app INTEGER, champion INTEGER
);

CREATE TABLE IF NOT EXISTS career_stats (
    owner_slug      TEXT PRIMARY KEY,
    seasons INTEGER, games INTEGER, w INTEGER, l INTEGER, t INTEGER,
    win_pct REAL, rs_win_pct REAL,
    pf REAL, pa REAL, ppg REAL,
    best_game REAL, worst_game REAL,
    best_season_pf REAL, worst_season_pf REAL,
    moves INTEGER,
    winning_seasons INTEGER, losing_seasons INTEGER,
    avg_finish REAL, best_finish INTEGER, worst_finish INTEGER,
    career_raw_opr REAL, career_opr REAL,
    playoff_apps INTEGER, byes INTEGER,
    playoff_w INTEGER, playoff_l INTEGER,
    final_apps INTEGER, titles INTEGER,
    title_odds REAL
);

CREATE TABLE IF NOT EXISTS h2h (
    owner_a TEXT NOT NULL,
    owner_b TEXT NOT NULL,
    game_type TEXT NOT NULL,              -- 'R','P','C','F' or 'ALL'
    games INTEGER, w INTEGER, l INTEGER, t INTEGER,
    pf REAL, pa REAL,
    PRIMARY KEY (owner_a, owner_b, game_type)
);

CREATE TABLE IF NOT EXISTS seed_history (
    seed INTEGER PRIMARY KEY,
    appearances INTEGER,
    final_apps INTEGER,
    titles INTEGER
);

CREATE TABLE IF NOT EXISTS records_book (
    category TEXT NOT NULL,
    scope    TEXT NOT NULL,               -- 'game' | 'season' | 'career'
    value    REAL,
    display  TEXT,
    owner_slug TEXT,
    year     INTEGER,
    detail   TEXT,
    PRIMARY KEY (category, scope)
);

CREATE TABLE IF NOT EXISTS sync_log (
    ran_at TEXT NOT NULL,
    year INTEGER,
    step TEXT,
    ok INTEGER,
    message TEXT
);
"""

DERIVED_TABLES = [
    "season_stats", "career_stats", "h2h", "seed_history", "records_book",
]


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    p = Path(path) if path else DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive migrations for source tables that predate a schema change."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(games)")}
    if "period_days" not in cols:
        conn.execute("ALTER TABLE games ADD COLUMN period_days INTEGER")


def reset_derived(conn: sqlite3.Connection) -> None:
    """Derived tables are always rebuilt from scratch — cheap at this scale.

    DROP + recreate so schema additions to derived tables apply without
    hand-run migrations.
    """
    for table in DERIVED_TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.executescript(SCHEMA)
