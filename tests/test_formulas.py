"""Formula tests pinned to values verified against the League History sheet.

If these break, the code no longer matches the league's actual math.
"""

import math

import pytest

from mtalga.metrics import formulas as f


class TestRawOPR:
    def test_jfarr_2021_exact(self):
        # Sheet JFarr!D6 = 392.0604636 (2021 season)
        got = f.raw_opr(ppg=345.7878636, high_game=531.333, low_game=264.333,
                        rs_win_pct=0.6363636364)
        assert math.isclose(got, 392.060463616, rel_tol=0, abs_tol=1e-8)

    def test_alt_variant_jfarr_2021(self):
        # Sheet JFarr!E6 = 365.4598636 uses "Strd High" 398.33 instead of 531.333
        got = f.raw_opr(ppg=345.7878636, high_game=398.33, low_game=264.333,
                        rs_win_pct=0.6363636364)
        assert math.isclose(got, 365.459863616, rel_tol=0, abs_tol=1e-8)

    def test_weights_sum_to_one(self):
        # 6/10 + 2/10 + 2/10 — a uniform league should get raw_opr driven by ppg
        got = f.raw_opr(ppg=100, high_game=100, low_game=100, rs_win_pct=0.5)
        assert got == (600 + 400 + 200) / 10


class TestNormalizedOPR:
    def test_jfarr_2021_exact(self):
        # Sheet Data!B180 (2021 league avg raw) = 353.8329173; JFarr!B6 = 1.108038412
        got = f.normalized_opr(392.0604636, 353.8329173)
        assert math.isclose(got, 1.108038412, rel_tol=0, abs_tol=1e-8)

    def test_zero_average_raises(self):
        with pytest.raises(ValueError):
            f.normalized_opr(100.0, 0.0)


class TestExpectedWins:
    def test_matches_sheet_forecast_semantics(self):
        # FORECAST is plain least-squares. Perfectly linear data: wins = 10*opr
        history = [(0.9, 9.0), (1.0, 10.0), (1.1, 11.0)]
        assert math.isclose(f.expected_wins(1.05, history), 10.5, abs_tol=1e-12)

    def test_regression_on_scattered_data(self):
        history = [(0.8, 6), (0.9, 9), (1.0, 10), (1.1, 11), (1.2, 14)]
        # slope = Sxy/Sxx = 1.8/0.1 = 18 ; intercept = 10 - 18*1.0 = -8
        assert math.isclose(f.expected_wins(1.0, history), 10.0, abs_tol=1e-9)
        assert math.isclose(f.expected_wins(1.1, history), 11.8, abs_tol=1e-9)

    def test_needs_two_points(self):
        with pytest.raises(ValueError):
            f.expected_wins(1.0, [(1.0, 10)])


class TestTitleOdds:
    def test_tfarr_exact(self):
        # Sheet Overall!N5 = 0.2263470795 for TFarr: 6 playoff apps, 7 seasons, .62937 win%
        got = f.title_odds(playoff_apps=6, seasons=7, win_pct=0.6293706294)
        assert math.isclose(got, 0.2263470795, rel_tol=0, abs_tol=1e-9)

    def test_no_seasons(self):
        assert f.title_odds(0, 0, 0.5) == 0.0


class TestWinPct:
    def test_ties_excluded_from_denominator(self):
        # Sheet col K convention: =H/SUM(H:I) — ties don't count
        assert f.win_pct(10, 5, t=3) == 10 / 15

    def test_no_games(self):
        assert f.win_pct(0, 0) == 0.0
