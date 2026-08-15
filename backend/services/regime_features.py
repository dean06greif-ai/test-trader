"""Mathematische Bausteine der Regime-Erkennung (Engine v2).

Alle Funktionen sind
- vektorisiert (O(n) bzw. O(n log n)) – auch bei Millionen Kerzen schnell,
- rein rückblickend: Wert an Index i nutzt ausschließlich Daten bis i
  (kein Lookahead), die ersten (Fenster-1) Werte sind NaN,
- einzeln testbar (siehe tests/test_regime_features.py).

Zentrale Idee gegenüber der alten Cluster-Erkennung: Trend wird als
*statistische Signifikanz* einer linearen Regression über den Log-Kurs
gemessen (t-Wert der Steigung). Der t-Wert ist skalenfrei und vergleichbar
über Coins/Timeframes hinweg: "langsam aber stetig fallend" liefert genauso
ein klares Signal wie "schnell fallend", weil er Steigung gegen Rauschen
normiert.
"""
import numpy as np

from services.timeframes import TIMEFRAMES

EPS = 1e-12


def bars_per_day(timeframe: str) -> float:
    return 1440.0 / max(TIMEFRAMES.get(timeframe, 1), 1)


def ohlc(candles):
    """(high, low, close, volume) als float64-Arrays – akzeptiert CandleArray
    und List[Dict]."""
    from services.candles import CandleArray
    if isinstance(candles, CandleArray):
        return (np.asarray(candles.hi, dtype=float), np.asarray(candles.lo, dtype=float),
                np.asarray(candles.cl, dtype=float), np.asarray(candles.vol, dtype=float))
    high = np.array([float(c["high"]) for c in candles], dtype=float)
    low = np.array([float(c["low"]) for c in candles], dtype=float)
    close = np.array([float(c["close"]) for c in candles], dtype=float)
    vol = np.array([float(c.get("volume") or 0.0) for c in candles], dtype=float)
    return high, low, close, vol


def _prefix(x: np.ndarray) -> np.ndarray:
    return np.concatenate([[0.0], np.cumsum(x)])


def rolling_sum(x: np.ndarray, w: int) -> np.ndarray:
    """Fenstersumme der letzten w Werte (NaN für die ersten w-1 Positionen)."""
    n = len(x)
    out = np.full(n, np.nan)
    if n < w or w < 1:
        return out
    p = _prefix(x)
    out[w - 1:] = p[w:] - p[:n - w + 1]
    return out


def rolling_mean(x: np.ndarray, w: int) -> np.ndarray:
    return rolling_sum(x, w) / float(w)


def rolling_std(x: np.ndarray, w: int) -> np.ndarray:
    """Stichproben-Standardabweichung im Fenster (rückblickend)."""
    n = len(x)
    out = np.full(n, np.nan)
    if n < w or w < 2:
        return out
    s1 = rolling_sum(x, w)
    s2 = rolling_sum(x * x, w)
    var = (s2 - s1 * s1 / w) / (w - 1)
    out[w - 1:] = np.sqrt(np.maximum(var[w - 1:], 0.0))
    return out


def ols_stats(y: np.ndarray, w: int):
    """Rollierende lineare Regression über die letzten w Werte.

    Rückgabe: (slope_pro_bar, t_wert_der_steigung, r2). Der t-Wert ist
    slope / Standardfehler(slope) – dadurch skalen- und längenunabhängig
    interpretierbar (|t| > ~2 => Trend statistisch belegt).
    """
    n = len(y)
    slope = np.full(n, np.nan)
    tstat = np.full(n, np.nan)
    r2 = np.full(n, np.nan)
    w = int(w)
    if n < w or w < 4:
        return slope, tstat, r2
    yc = np.asarray(y, dtype=float)
    yc = yc - yc.mean()  # Zentrierung: hält die Präfixsummen numerisch stabil
    Y = _prefix(yc)
    YY = _prefix(yc * yc)
    k = np.arange(n, dtype=float)
    KY = _prefix(k * yc)
    i = np.arange(w - 1, n)
    s = i - w + 1
    Sy = Y[i + 1] - Y[s]
    Syy = YY[i + 1] - YY[s]
    Sky = KY[i + 1] - KY[s]
    xbar = (w - 1) / 2.0
    Sxy = Sky - (s + xbar) * Sy               # Sum (x - xbar) * y, x = 0..w-1
    Sxx = w * (w * w - 1) / 12.0              # Sum (x - xbar)^2
    b = Sxy / Sxx
    Syy_c = Syy - Sy * Sy / w
    ss_res = np.maximum(Syy_c - b * Sxy, 0.0)
    se = np.sqrt(ss_res / max(w - 2, 1) / Sxx)
    slope[w - 1:] = b
    tstat[w - 1:] = b / np.maximum(se, EPS)
    r2[w - 1:] = 1.0 - ss_res / np.maximum(Syy_c, EPS)
    return slope, tstat, r2


def wilder(x: np.ndarray, period: int) -> np.ndarray:
    """Wilder-Glättung (EMA mit alpha = 1/period) – C-schnell über pandas."""
    import pandas as pd
    p = max(int(period), 1)
    s = pd.Series(np.nan_to_num(np.asarray(x, dtype=float), nan=0.0))
    return np.array(s.ewm(alpha=1.0 / p, adjust=False).mean().to_numpy(), dtype=float)


def true_range(high, low, close) -> np.ndarray:
    prev = np.empty_like(close)
    prev[0] = close[0]
    prev[1:] = close[:-1]
    return np.maximum(high - low, np.maximum(np.abs(high - prev), np.abs(low - prev)))


def adx_di(high, low, close, period: int):
    """ADX + DI+ / DI- nach Wilder (rückblickend, vektorisiert).
    ADX misst Trendstärke unabhängig von der Richtung, DI die Richtung."""
    n = len(close)
    if n < 3:
        z = np.full(n, np.nan)
        return z, z.copy(), z.copy()
    p = max(int(period), 2)
    up = np.zeros(n)
    dn = np.zeros(n)
    up[1:] = high[1:] - high[:-1]
    dn[1:] = low[:-1] - low[1:]
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    atr = wilder(true_range(high, low, close), p)
    plus_di = 100.0 * wilder(plus_dm, p) / np.maximum(atr, EPS)
    minus_di = 100.0 * wilder(minus_dm, p) / np.maximum(atr, EPS)
    dx = 100.0 * np.abs(plus_di - minus_di) / np.maximum(plus_di + minus_di, EPS)
    adx = wilder(dx, p)
    warm = min(2 * p, n)
    adx[:warm] = np.nan
    return adx, plus_di, minus_di


def atr_pct(high, low, close, period: int) -> np.ndarray:
    """ATR in Prozent des Kurses (Volatilitätsmaß je Kerze)."""
    atr = wilder(true_range(high, low, close), period)
    out = 100.0 * atr / np.maximum(close, EPS)
    out[:min(period, len(out))] = np.nan
    return out


def realized_vol_pct(close: np.ndarray, w: int, bars_day: float) -> np.ndarray:
    """Realisierte Tagesvolatilität in % (Std der Log-Renditen im Fenster)."""
    lr = np.zeros(len(close))
    lr[1:] = np.diff(np.log(np.maximum(close, EPS)))
    sd = rolling_std(lr, int(w))
    return sd * np.sqrt(max(bars_day, 1.0)) * 100.0


def efficiency_ratio(close: np.ndarray, w: int) -> np.ndarray:
    """Kaufman-Effizienz: |Netto-Bewegung| / Weglänge (0 = Chop, 1 = glatter Trend)."""
    n = len(close)
    out = np.full(n, np.nan)
    w = int(w)
    if n <= w:
        return out
    step = np.zeros(n)
    step[1:] = np.abs(np.diff(close))
    path = rolling_sum(step, w)
    i = np.arange(w, n)
    net = np.abs(close[i] - close[i - w])
    out[w:] = np.where(path[i] > EPS, net / np.maximum(path[i], EPS), 0.0)
    return out


def rolling_zscore(x: np.ndarray, w: int) -> np.ndarray:
    """z-Wert gegen das eigene, rückblickende Referenzfenster (kein Lookahead).
    Vor dem Referenzfenster wird gegen die bisher bekannten Werte normiert,
    damit auch am Anfang eine (vorsichtige) Einschätzung möglich ist."""
    n = len(x)
    out = np.full(n, np.nan)
    w = max(int(w), 10)
    xv = np.asarray(x, dtype=float)
    valid = ~np.isnan(xv)
    if valid.sum() < 5:
        return out
    filled = np.where(valid, xv, 0.0)
    cnt = _prefix(valid.astype(float))
    s1 = _prefix(filled)
    s2 = _prefix(filled * filled)
    i = np.arange(n)
    lo = np.maximum(i - w + 1, 0)
    c = cnt[i + 1] - cnt[lo]
    m = np.where(c > 0, (s1[i + 1] - s1[lo]) / np.maximum(c, 1), np.nan)
    var = np.where(c > 1, (s2[i + 1] - s2[lo]) / np.maximum(c, 1) - m * m, np.nan)
    sd = np.sqrt(np.maximum(var, 0.0))
    with np.errstate(invalid="ignore", divide="ignore"):
        z = (xv - m) / np.where(sd > EPS, sd, np.nan)
    z[c < 5] = np.nan
    out = np.where(np.isfinite(z), z, np.nan)
    return out


def rolling_max(x: np.ndarray, w: int) -> np.ndarray:
    import pandas as pd
    return np.array(pd.Series(np.asarray(x, dtype=float)).rolling(
        max(int(w), 1), min_periods=max(int(w) // 2, 2)).max().to_numpy(), dtype=float)


def rolling_min(x: np.ndarray, w: int) -> np.ndarray:
    import pandas as pd
    return np.array(pd.Series(np.asarray(x, dtype=float)).rolling(
        max(int(w), 1), min_periods=max(int(w) // 2, 2)).min().to_numpy(), dtype=float)


def range_position(close: np.ndarray, w: int) -> np.ndarray:
    """Donchian-Position: 0 = am unteren Rand der Spanne, 1 = am oberen Rand.
    Trends laufen an einem Rand, Ranges pendeln in der Mitte."""
    hi = rolling_max(close, w)
    lo = rolling_min(close, w)
    span = hi - lo
    with np.errstate(invalid="ignore", divide="ignore"):
        pos = np.where(span > EPS, (close - lo) / np.where(span > EPS, span, 1.0), 0.5)
    return np.where(np.isfinite(hi) & np.isfinite(lo), pos, np.nan)


def variance_ratio(close: np.ndarray, k: int, w: int) -> np.ndarray:
    """Varianz-Verhältnis (Lo/MacKinlay): Var(k-Bar-Renditen) / (k * Var(1-Bar)).

    < 1  => mean-reverting (Range / Seitwärtsmarkt)
    ~ 1  => Random Walk
    > 1  => trendend (Momentum)
    Rückblickend über ein Fenster von w Bars.
    """
    n = len(close)
    out = np.full(n, np.nan)
    k = max(int(k), 2)
    w = max(int(w), k * 3)
    if n < w + k + 2:
        return out
    logc = np.log(np.maximum(np.asarray(close, dtype=float), EPS))
    r1 = np.zeros(n)
    r1[1:] = np.diff(logc)
    rk = np.full(n, np.nan)
    rk[k:] = logc[k:] - logc[:-k]
    sd1 = rolling_std(r1, w)
    sdk = rolling_std(np.nan_to_num(rk, nan=0.0), w)
    with np.errstate(invalid="ignore", divide="ignore"):
        vr = (sdk * sdk) / np.maximum(k * sd1 * sd1, EPS)
    vr[:w + k] = np.nan
    return vr


def run_length(cond) -> np.ndarray:
    """Länge der aktuellen True-Serie, die bei i endet (0 wenn cond[i] False)."""
    c = np.asarray(cond, dtype=bool).astype(np.int64)
    out = np.zeros(len(c), dtype=np.int64)
    if not len(c):
        return out
    # Standard-Trick: laufende Summe minus Summe beim letzten False
    idx = np.arange(len(c))
    csum = np.cumsum(c)
    reset = np.where(c == 0, csum, 0)
    reset = np.maximum.accumulate(reset)
    out = csum - reset
    out[c == 0] = 0
    return out * (idx >= 0)


def donchian_lagged(close: np.ndarray, w: int, lag: int):
    """Höchst-/Tiefstkurs über ein Fenster, das `lag` Bars VOR der aktuellen
    Kerze endet. Damit lässt sich prüfen, ob der Kurs ein *neues* Extrem macht
    (Trend) oder nur die alte Spanne wieder anläuft (Range)."""
    n = len(close)
    hi = rolling_max(close, w)
    lo = rolling_min(close, w)
    out_hi = np.full(n, np.nan)
    out_lo = np.full(n, np.nan)
    lag = max(int(lag), 1)
    if n > lag:
        out_hi[lag:] = hi[:n - lag]
        out_lo[lag:] = lo[:n - lag]
    return out_hi, out_lo


def ema(x: np.ndarray, span: int) -> np.ndarray:
    import pandas as pd
    return np.array(pd.Series(np.asarray(x, dtype=float)).ewm(
        span=max(int(span), 1), adjust=False).mean().to_numpy(), dtype=float)
