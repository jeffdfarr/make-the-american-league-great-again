"""Authenticated access to Fantrax via the unofficial fantraxapi library.

Fantrax has no official API. Private leagues require a logged-in session
cookie. The cookie is produced once by login_helper.py (run locally, you log
in yourself in a real Chrome window) and is then supplied to the sync either:

  * as the env var FANTRAX_COOKIES  (JSON dict, the GitHub Actions secret), or
  * as a JSON file at data/fantrax_cookies.json (local development).

The cookie is equivalent to being logged in — keep it out of the repo.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from fantraxapi import League
from requests import Session

COOKIE_FILE = Path(__file__).resolve().parent.parent / "data" / "fantrax_cookies.json"

# Be a polite guest: one low-volume sync a day, spaced requests.
REQUEST_SPACING_SECONDS = 1.0


class AuthError(RuntimeError):
    pass


def build_session() -> Session:
    raw = os.environ.get("FANTRAX_COOKIES")
    if not raw and COOKIE_FILE.exists():
        raw = COOKIE_FILE.read_text()
    if not raw:
        raise AuthError(
            "No Fantrax cookies found. Run `python -m mtalga.login_helper` locally, "
            "or set the FANTRAX_COOKIES env var / GitHub secret."
        )
    cookies = json.loads(raw)
    session = Session()
    for name, value in cookies.items():
        session.cookies.set(name, value, domain=".fantrax.com")
    session.headers.update({"User-Agent": "mtalga-league-hq/1.0 (nightly stats sync)"})
    return session


class PacedLeague:
    """Thin wrapper around fantraxapi.League that spaces requests out."""

    def __init__(self, league_id: str, session: Session):
        self.league = League(league_id, session=session)

    def _pace(self):
        time.sleep(REQUEST_SPACING_SECONDS)

    def __getattr__(self, item):
        attr = getattr(self.league, item)
        if callable(attr):
            def paced(*args, **kwargs):
                self._pace()
                return attr(*args, **kwargs)
            return paced
        return attr


def open_league(league_id: str, session: Session | None = None) -> PacedLeague:
    return PacedLeague(league_id, session or build_session())
