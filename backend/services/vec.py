"""Vektorisierte Indikator-Kerne (numpy).

Die Referenz-Implementierungen in technical_indicators.py laufen mit
Python-Schleifen über jede Kerze. Bei 1,5 Mio. aggregierten Kerzen dauert
allein ein EMA mehrere Sekunden – und der Optimizer berechnet das hunderte Male.
Hier stehen exakt gleichwertige, aber vollständig vektorisierte Varianten.

Alle rekursiven Glättungen (EMA, Wilder) nutzen dieselbe Blockformel:
    y[j] = beta^(j+1) * prev + alpha * beta^j * cumsum(x[t] * beta^-t)
Die Blockgröße wird so gewählt, dass beta^-t nicht überläuft.
"""
import numpy as np


def _recursive_smooth(x: np.ndarray, alpha: float, seed: float) -> np.ndarray:
    """y[j] = alpha*x[j] + (1-alpha)*y[j-1], y[-1] = seed."""
    m = x.shape[0]
    out = np.empty(m, dtype=np.float64)
    if m == 0:
        return out
    beta = 1.0 - alpha
    if beta <= 0:
        out[:] = alpha * x
        return out
    # beta^-t darf nicht überlaufen (float64 max ~1e308) -> Blöcke bis 1e200
    block = int(max(64, min(m, 200.0 / max(-np.log10(beta), 1e-12))))
    prev = float(seed)
    for s in range(0, m, block):
        b = x[s:s + block]
        k = b.shape[0]
        j = np.arange(k, dtype=np.float64)
        pj = beta ** j                       # beta^j
        inv = 1.0 / pj                       # beta^-j
        cs = np.cumsum(b * inv)
        y = pj * (beta * prev + alpha * cs)
        out[s:s + k] = y
        prev = float(y[-1])
    return out


def ema(close: np.ndarray, period: int) -> np.ndarray:
    """Identisch zu TechnicalIndicators.calculate_ema (NaN vor period-1)."""
    n = close.shape[0]
    out = np.full(n, np.nan)
    if n < period or period < 1:
        return out
    out[period - 1] = float(np.mean(close[:period]))
    if n > period:
        out[period:] = _recursive_smooth(close[period:], 2.0 / (period + 1),
                                         out[period - 1])
    return out


def rsi(close: np.ndarray, period: int = 14) -> np.ndarray:
    """Identisch zu TechnicalIndicators.calculate_rsi (Wilder)."""
    n = close.shape[0]
    out = np.full(n, np.nan)
    if n < period + 1 or period < 1:
        return out
    d = np.diff(close)
    gains = np.where(d > 0, d, 0.0)
    losses = np.where(d < 0, -d, 0.0)
    g0 = float(np.mean(gains[:period]))
    l0 = float(np.mean(losses[:period]))
    a = 1.0 / period
    ag = np.empty(n - period)
    al = np.empty(n - period)
    ag[0], al[0] = g0, l0
    if n - period > 1:
        ag[1:] = _recursive_smooth(gains[period:n - 1], a, g0)
        al[1:] = _recursive_smooth(losses[period:n - 1], a, l0)
    with np.errstate(invalid="ignore", divide="ignore"):
        rs = ag / al
        vals = 100.0 - (100.0 / (1.0 + rs))
    vals[al == 0] = 100.0
    out[period:] = vals
    return out


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray,
        period: int = 14) -> np.ndarray:
    """Identisch zu TechnicalIndicators.calculate_atr (Wilder, NaN vor period)."""
    n = close.shape[0]
    out = np.full(n, np.nan)
    if n < period + 1 or period < 1:
        return out
    trs = np.zeros(n)
    pc = close[:-1]
    trs[1:] = np.maximum.reduce([high[1:] - low[1:], np.abs(high[1:] - pc),
                                 np.abs(low[1:] - pc)])
    seed = float(np.mean(trs[1:period + 1]))
    out[period] = seed
    if n > period + 1:
        out[period + 1:] = _recursive_smooth(trs[period + 1:], 1.0 / period, seed)
    return out


def adx_di(high: np.ndarray, low: np.ndarray, close: np.ndarray,
           period: int = 14):
    """ADX + DI+/DI- nach Wilder (NNFX-Standard-Trendfilter).
    Rückgabe: (adx, plus_di, minus_di); NaN während der Aufwärmphase."""
    n = close.shape[0]
    nan = np.full(n, np.nan)
    if n < 2 * period + 2 or period < 2:
        return nan, nan.copy(), nan.copy()
    up = np.zeros(n)
    dn = np.zeros(n)
    up[1:] = high[1:] - high[:-1]
    dn[1:] = low[:-1] - low[1:]
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    trs = np.zeros(n)
    pc = close[:-1]
    trs[1:] = np.maximum.reduce([high[1:] - low[1:], np.abs(high[1:] - pc),
                                 np.abs(low[1:] - pc)])

    def _smooth(x):
        out = np.full(n, np.nan)
        seed = float(np.mean(x[1:period + 1]))
        out[period] = seed
        if n > period + 1:
            out[period + 1:] = _recursive_smooth(x[period + 1:], 1.0 / period, seed)
        return out

    atr_s = _smooth(trs)
    pdm_s = _smooth(plus_dm)
    mdm_s = _smooth(minus_dm)
    with np.errstate(invalid="ignore", divide="ignore"):
        pdi = 100.0 * pdm_s / np.where(atr_s > 0, atr_s, np.nan)
        mdi = 100.0 * mdm_s / np.where(atr_s > 0, atr_s, np.nan)
        dx = 100.0 * np.abs(pdi - mdi) / np.where((pdi + mdi) > 0, pdi + mdi, np.nan)
    adx = np.full(n, np.nan)
    start = 2 * period
    if n > start:
        seed = float(np.nanmean(dx[period:start + 1]))
        adx[start] = seed
        if n > start + 1:
            adx[start + 1:] = _recursive_smooth(np.nan_to_num(dx[start + 1:]),
                                                1.0 / period, seed)
    return adx, pdi, mdi


def cci(high: np.ndarray, low: np.ndarray, close: np.ndarray,
        period: int = 20) -> np.ndarray:
    """Commodity Channel Index (NNFX-Bestätigungsindikator)."""
    import pandas as pd
    n = close.shape[0]
    if n < period or period < 2:
        return np.full(n, np.nan)
    tp = pd.Series((high + low + close) / 3.0)
    ma = tp.rolling(period).mean()
    md = (tp - ma).abs().rolling(period).mean()
    with np.errstate(invalid="ignore", divide="ignore"):
        out = (tp - ma) / (0.015 * md.replace(0, np.nan))
    return np.array(out.to_numpy(), dtype=float)


def heikin_ashi_green(op: np.ndarray, high: np.ndarray, low: np.ndarray,
                      close: np.ndarray) -> np.ndarray:
    """1.0 wenn die Heikin-Ashi-Kerze grün ist, sonst 0.0 (wie TI-Referenz)."""
    n = close.shape[0]
    if n < 2:
        return np.zeros(n)
    ha_close = (op + high + low + close) / 4.0
    # ha_open[i] = (ha_open[i-1] + ha_close[i-1]) / 2   -> Rekursion mit alpha=0.5
    ha_open = np.empty(n)
    ha_open[0] = (op[0] + close[0]) / 2.0
    if n > 1:
        ha_open[1:] = _recursive_smooth(ha_close[:-1], 0.5, ha_open[0])
    return (ha_close > ha_open).astype(np.float64)
