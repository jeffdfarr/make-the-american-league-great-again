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
    """Parse matchup rows by structure, not fixed positions.

    MTALGA's schedule rows have 8 cells (Away, FPts, Adj, Total, Home, FPts,
    Adj, Total); the library assumes 4. We find the two team cells and take
    each side's last numeric column — the adjusted final total.
    """
    from decimal import Decimal, InvalidOperation

    import fantraxapi.objs.scoring_period as sp
    from fantraxapi.exceptions import NotTeamInLeague

    def safe_init(self, scoring_period, matchup_key, data):
        sp.FantraxBaseObject.__init__(self, scoring_period.league, data)
        self.scoring_period = scoring_period
        self.matchup_key = matchup_key

        raw_cells = self._data if isinstance(self._data, list) else []
        cells = [c if isinstance(c, dict) else {"content": str(c)} for c in raw_cells]

        def to_dec(raw):
            raw = str(raw).replace(",", "").strip()
            if raw in ("", "-", "–", "—", "TBD", "N/A"):
                return None
            try:
                return Decimal(raw)
            except InvalidOperation:
                return None

        def team_obj(i):
            c = cells[i]
            tid = c.get("teamId")
            if tid:
                try:
                    return self.league.team(tid)
                except NotTeamInLeague:
                    pass
            return str(c.get("content", ""))

        def final_score(start, end):
            val = None
            for j in range(start, end):
                d = to_dec(cells[j].get("content", ""))
                if d is not None:
                    val = d
            return val if val is not None else Decimal(0)

        team_idx = [i for i, c in enumerate(cells) if c.get("teamId")]
        if len(team_idx) >= 2:
            a, h = team_idx[0], team_idx[1]
            self.away = team_obj(a)
            self._away_score = final_score(a + 1, h)
            self.home = team_obj(h)
            self._home_score = final_score(h + 1, len(cells))
        else:
            self.away = str(cells[0].get("content", "")) if cells else ""
            self._away_score = Decimal(0)
            self.home = str(cells[2].get("content", "")) if len(cells) > 2 else ""
            self._home_score = Decimal(0)

    sp.Matchup.__init__ = safe_init


_patch_matchup_parsing()


def _patch_scoring_period_results() -> None:
    """Fix fantraxapi's scoring_period_results for mid-season leagues."""
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
                return periods
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
                    continue
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