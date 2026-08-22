"""Authenticated access to Fantrax via the unofficial fantraxapi library.

Private leagues require a logged-in session cookie, produced by
login_helper.py and supplied either as the FANTRAX_COOKIES env var
(GitHub secret) or as data/fantrax_cookies.json (local).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from fantraxapi import League
from requests import Session

COOKIE_FILE = Path(__file__).resolve().parent.parent / "data" / "fantrax_cookies.json"


def _patch_matchup_parsing() -> None:
    """Make fantraxapi's Matchup tolerant of malformed rows.

    Unknown teams become plain strings and unparseable scores become 0 —
    both of which the sync skips safely.
    """
    from decimal import Decimal, InvalidOperation

    import fantraxapi.objs.scoring_period as sp
    from fantraxapi.exceptions import NotTeamInLeague

    def safe_init(self, scoring_period, matchup_key, data):
        sp.FantraxBaseObject.__init__(self, scoring_period.league, data)
        self.scoring_period = scoring_period
        self.matchup_key = matchup_key

        def cell(i):
            try:
                c = self._data[i]
                return c if isinstance(c, dict) else {"content": str(c)}
            except (IndexError, KeyError, TypeError):
                return {}

        def team_at(i):
            c = cell(i)
            tid = c.get("teamId")
            if tid:
                try:
                    return self.league.team(tid)
                except NotTeamInLeague:
                    pass
            return str(c.get("content", ""))

        def score_at(i):
            raw = str(cell(i).get("content", "0")).replace(",", "").strip()
            if raw in ("", "-", "–", "—", "TBD", "N/A"):
                return Decimal(0)
            try:
                return Decimal(raw)
            except InvalidOperation:
                return Decimal(0)

        self.away = team_at(0)
        self._away_score = score_at(1)
        self.home = team_at(2)
        self._home_score = score_at(3)

    sp.Matchup.__init__ = safe_init


_patch_matchup_parsing()


def _patch_scoring_period_results() -> None:
    """Fix fantraxapi's scoring_period_results for mid-season leagues.

    Upstream bug: with no extra bracket tabs (playoffs not started), the API
    helper returns a single dict instead of a list, and `responses[1:]`
    raises KeyError. Same logic, plus normalization and guards.
    """
    import re as _re

    import fantraxapi.api as api
    from fantraxapi.objs.league import League as _League
    from fantraxapi.objs.scoring_period import ScoringPeriodResult

    def fixed(self, season: bool = True, playoffs: bool = True):
        periods = {}
        response = api.get_standings(self, view="SCHEDULE")

        if season:
            for scoring_period_data in response.get("tableList", []):
                sp_ = ScoringPeriodResult(self, scoring_period_data)
                periods[sp_.period.number] = sp_

        if playoffs:
            tabs = [t["id"] for t in response.get("displayedLists", {}).get("tabs", [])
                    if str(t.get("id", "")).startswith(".")]
            try:
                playoff_responses = api.get_standings(self, views=["PLAYOFFS"] + tabs)
            except Exception:
                return periods  # no playoff view at all yet
            if not isinstance(playoff_responses, list):
                playoff_responses = [playoff_responses]

            def period_num(caption):
                m = _re.search(r"(\d+)$", str(caption))
                return int(m.group()) if m else None

            other_data = {}
            for bracket_response in playoff_responses[1:]:
                other_id = bracket_response.get("displayedSelections", {}).get("view")
                name = next((t["name"] for t in bracket_response.get("displayedLists", {}).get("tabs", [])
                             if t.get("id") == other_id), None)
                for obj in bracket_response.get("tableList", []):
                    n = period_num(obj.get("caption"))
                    if obj.get("caption") == "Standings" or n is None:
                        continue
                    other_data.setdefault(n, []).append((name, obj))

            for obj in reversed(playoff_responses[0].get("tableList", [])):
                n = period_num(obj.get("caption"))
                if obj.get("caption") == "Standings" or n is None:
                    continue
                try:
                    sp_ = ScoringPeriodResult(self, obj, other_data=other_data.get(n))
                except Exception:
                    continue  # unscheduled/TBD playoff period — nothing to store yet
                periods[sp_.period.number] = sp_

        return periods

    _League.scoring_period_results = fixed


_patch_scoring_period_results()

# Be a polite guest: one low-volume sync a day, spaced requests.
REQUEST_SPACING_SECONDS = 1.0


class AuthError(RuntimeError):
    pass


def _parse_cookies(data) -> dict[str, str]:
    """Accept {name: value} JSON or a Cookie-Editor style list export."""
    if isinstance(data, dict):
        return {str(k): str(v) for k, v in data.items()}
    if isinstance(data, list):
        out = {}
        for c in data:
            if isinstance(c, dict) and "name" in c and "value" in c:
                out[str(c["name"])] = str(c["value"])
        if out:
            return out
    raise AuthError("Unrecognized cookie file format.")


def build_session() -> Session:
    raw = os.environ.get("FANTRAX_COOKIES")
    if not raw and COOKIE_FILE.exists():
        raw = COOKIE_FILE.read_text()
    if not raw:
        raise AuthError(
            "No Fantrax cookies found. Run `python -m mtalga.login_helper` locally, "
            "or set the FANTRAX_COOKIES env var / GitHub secret."
        )
    cookies = _parse_cookies(json.loads(raw))
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
