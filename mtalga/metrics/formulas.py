"""The league's calculations, reverse-engineered from the League History sheet
and verified against its actual values to full precision.

Sheet references are noted per function so future archaeologists can check.
"""

from __future__ import annotations


def raw_opr(ppg: float, high_game: float, low_game: float, rs_win_pct: float) -> float:
    """Raw OPR — the league's power rating, per owner-season.

    Sheet: owner tabs col D, e.g. JFarr!D6:
        =((N*6)+((O+Q)*2)+((AH*200)*2))/10
    Verified: JFarr 2021 -> 392.060463616 (exact).

    60% scoring average, 20% ceiling+floor, 20% regular-season win%
    (scaled x200 into point-like units).
    """
    return (6 * ppg + 2 * (high_game + low_game) + 2 * (200 * rs_win_pct)) / 10


def normalized_opr(raw: float, league_avg_raw: float) -> float:
    """OPR = raw OPR / league-average raw OPR for that season.

    Sheet: owner tabs col B (=D/Data!B<yearrow>); normalizers in Data!A179:B185.
    Verified: 392.060464 / 353.832917 = 1.108038412 (exact).
    1.000 is league average; comparable across seasons and season lengths.
    """
    if not league_avg_raw:
        raise ValueError("league average raw OPR is zero/missing")
    return raw / league_avg_raw


def expected_wins(current_opr: float, history: list[tuple[float, float]]) -> float:
    """FORECAST(current_opr, wins, oprs) — simple least-squares regression of
    season wins on season OPR over every completed owner-season in history.

    Sheet: Data!K3 =FORECAST(I3, Projection!$AD:$AD, Projection!$G:$G).
    history: [(opr, wins), ...]
    """
    n = len(history)
    if n < 2:
        raise ValueError("need at least two historical seasons for the regression")
    xs = [h[0] for h in history]
    ys = [h[1] for h in history]
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx == 0:
        return mean_y
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    slope = sxy / sxx
    intercept = mean_y - slope * mean_x
    return intercept + slope * current_opr


def title_odds(playoff_apps: int, seasons: int, win_pct: float) -> float:
    """Career championship odds (the Overall tab's tongue-in-cheek col N):

        =(playoff_apps / seasons) * (2/3) * win_pct^2

    Verified: TFarr (6/7)*(2/3)*0.6293706294^2 = 0.2263470795 (exact).
    """
    if seasons == 0:
        return 0.0
    return (playoff_apps / seasons) * (2 / 3) * (win_pct ** 2)


def win_pct(w: int, l: int, t: int = 0) -> float:
    """Sheet convention: ties excluded from the denominator (col K: =H/SUM(H:I))."""
    games = w + l
    return w / games if games else 0.0
