# MTALGA League HQ

Stats site backend for **Make the American League Great Again** — a Fantrax
dynasty baseball league (est. 2020). Replaces the League History spreadsheet:
a nightly job pulls every matchup from Fantrax, SQLite is the source of truth,
and all metrics (OPR, expected wins, H2H, records) are recomputed from games.

Architecture doc: <https://claude.ai/code/artifact/03ffc6ca-a5cb-45bb-bb96-a425b888dee8>

## Layout

    config/            seasons.yaml (league IDs), owners.yaml (owner registry +
                       team_map), adjustments.yaml (logged manual corrections)
    mtalga/            sync, metrics engine, CLI
    mtalga/metrics/    formulas.py — the league math, verified against the sheet
    tests/             pytest suite incl. sheet-pinned formula values
    data/              mtalga.db (created on first run) + your local cookie file
    site/data/         JSON exports for the frontend (Phase 4)
    .github/workflows/ nightly sync cron

## Setup

    pip install -r requirements.txt
    pytest                                  # 15 tests should pass

**Auth (one-time):** the league is private, so the sync needs a logged-in
Fantrax session cookie.

    pip install selenium webdriver-manager
    python -m mtalga.login_helper           # log in yourself in the Chrome window

That writes `data/fantrax_cookies.json` (gitignored). For GitHub Actions,
paste that file's contents into a repo secret named `FANTRAX_COOKIES`.

## First run

    # 1. See each season's teams and fill config/owners.yaml team_map
    python -m mtalga.cli discover --year 2026

    # 2. Pull all seven seasons, compute everything, export JSON
    python -m mtalga.cli backfill

    # 3. Nightly equivalent (current season only)
    python -m mtalga.cli sync

The sync verifies each league ID against the API's reported season and warns
on mismatch. Unmapped teams are loud, not silent.

## Nightly automation

`.github/workflows/nightly.yml` runs `sync` at 07:00 UTC, commits the updated
DB + JSON, and (optionally) pings a Discord webhook on failure — set the
`DISCORD_WEBHOOK` secret to enable. An expired cookie = rerun `login_helper`,
update the `FANTRAX_COOKIES` secret. Takes two minutes.

## Conventions from the sheet (encoded in the engine)

- **Consolation games** don't count toward W/L, points, or OPR.
- **Ties** are excluded from win % denominators.
- **OPR** = `(6·PPG + 2·(high+low) + 2·(200·RS win%)) / 10`, normalized by the
  season's league-average raw OPR.
- **Expected wins** = least-squares regression of RS wins on OPR over all
  completed owner-seasons (the sheet's `FORECAST`).
- **Champion** = winner of the main-bracket game in the last playoff period.
- Manual corrections live in `config/adjustments.yaml` with a required
  `reason:` — never edit derived numbers directly.

## Still open (see architecture doc §10)

"Strd High" rule for the alt OPR · conference labels · exact "Moves"
definition · playoff formats per year (fills `seed_history`) · hitting/pitching
split backfill (roster fetches) · finish/seeding from official final standings.
