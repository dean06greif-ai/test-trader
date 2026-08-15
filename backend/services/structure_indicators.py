"""Struktur-/Liquiditäts-/Event-Indikatoren (Etappe 5, Indikator-Paket).

Alle Berechnungen sind STRENG KAUSAL (nur Vergangenheit je Bar):
- Markt-Struktur (HH/HL vs. LH/LL) aus BESTÄTIGTEN Swing-Pivots
  (ein Pivot gilt erst ab dem Bar, an dem seine `wing` rechten Nachbarn
  vorliegen – kein Lookahead).
- Break of Structure (BOS): Close kreuzt das letzte bestätigte Swing-Hoch/-Tief.
- Support/Resistance: Abstand zum nächsten bestätigten Swing-Level unter/über
  dem Kurs.
- Equal Highs/Lows ("institutionelle Liquidität"): mehrere Pivots auf fast
  gleichem Preis = ruhende Stop-Liquidität; Abstand dorthin.
- Liquidity Sweep (Liquidity Grab): Docht reißt das letzte Hoch/Tief,
  Schlusskurs kehrt zurück.
- Trendkanal: lineare Regression über ein Fenster, Position im ±2σ-Kanal
  (0-100) und Kanal-Steigung in % – O(n) über Cumsums.
- Range-Position: Lage in der Donchian-Spanne (0-100, Range-Trading).
- FOMC-Kalender 2024-2026 (statisch, Fed-Terminplan): Meeting-Tage und
  Tage bis zur nächsten Zins-Entscheidung.

Genutzt vom Fast-Path (services/fast_sim.py) für Backtest UND Live
(eine Auswertungs-Logik, garantierte Parität).
"""
from typing import Dict, List, Tuple

import numpy as np

# ---------------------------------------------------------------------------
# FOMC-Meetings 2024-2026 (offizieller Fed-Kalender; jeweils Tag 1 + Tag 2,
# die Zins-Entscheidung fällt am 2. Tag).
# ---------------------------------------------------------------------------
FOMC_MEETINGS: List[Tuple[str, str]] = [
    ("2024-01-30", "2024-01-31"), ("2024-03-19", "2024-03-20"),
    ("2024-04-30", "2024-05-01"), ("2024-06-11", "2024-06-12"),
    ("2024-07-30", "2024-07-31"), ("2024-09-17", "2024-09-18"),
    ("2024-11-06", "2024-11-07"), ("2024-12-17", "2024-12-18"),
    ("2025-01-28", "2025-01-29"), ("2025-03-18", "2025-03-19"),
    ("2025-05-06", "2025-05-07"), ("2025-06-17", "2025-06-18"),
    ("2025-07-29", "2025-07-30"), ("2025-09-16", "2025-09-17"),
    ("2025-10-28", "2025-10-29"), ("2025-12-09", "2025-12-10"),
    ("2026-01-27", "2026-01-28"), ("2026-03-17", "2026-03-18"),
    ("2026-04-28", "2026-04-29"), ("2026-06-16", "2026-06-17"),
    ("2026-07-28", "2026-07-29"), ("2026-09-15", "2026-09-16"),
    ("2026-10-27", "2026-10-28"), ("2026-12-08", "2026-12-09"),
]

_MEETING_DAYS = np.array(sorted({d for pair in FOMC_MEETINGS for d in pair}),
                         dtype="datetime64[D]")
_DECISION_DAYS = np.array(sorted(d2 for _, d2 in FOMC_MEETINGS),
                          dtype="datetime64[D]")


def fomc_features(ts_ms: np.ndarray) -> Dict[str, np.ndarray]:
    """fomc_today (1 an Meeting-Tagen, UTC) und days_to_fomc (Kalendertage bis
    zur nächsten Zins-Entscheidung; 0 am Entscheidungstag, 99 außerhalb des
    bekannten Kalenders)."""
    days = np.asarray(ts_ms, dtype="int64") // 86_400_000
    dates = days.astype("datetime64[D]")
    today = np.isin(dates, _MEETING_DAYS).astype(float)
    idx = np.searchsorted(_DECISION_DAYS, dates, side="left")
    dtf = np.full(len(dates), 99.0)
    m = idx < len(_DECISION_DAYS)
    dtf[m] = (_DECISION_DAYS[idx[m]] - dates[m]).astype(float)
    dtf = np.clip(dtf, 0.0, 99.0)
    return {"fomc_today": today, "days_to_fomc": dtf}


# ---------------------------------------------------------------------------
# Swing-Pivots (bestätigt, kausal)
# ---------------------------------------------------------------------------
def confirmed_pivots(high: np.ndarray, low: np.ndarray, wing: int
                     ) -> List[Tuple[int, int, str, float]]:
    """[(pivot_i, confirm_i, 'high'|'low', preis)] – Pivot an Position j ist
    Extremum gegenüber `wing` Nachbarn links UND rechts, gilt aber erst ab
    confirm_i = j + wing (kausal)."""
    n = len(high)
    wing = max(int(wing), 1)
    if n < 2 * wing + 1:
        return []
    is_ph = np.ones(n, dtype=bool)
    is_pl = np.ones(n, dtype=bool)
    for k in range(1, wing + 1):
        is_ph[k:] &= high[k:] > high[:-k]
        is_ph[:-k] &= high[:-k] >= high[k:]
        is_pl[k:] &= low[k:] < low[:-k]
        is_pl[:-k] &= low[:-k] <= low[k:]
    is_ph[:wing] = is_ph[n - wing:] = False
    is_pl[:wing] = is_pl[n - wing:] = False
    out = []
    for j in np.flatnonzero(is_ph | is_pl):
        if is_ph[j]:
            out.append((int(j), int(j + wing), "high", float(high[j])))
        if is_pl[j]:
            out.append((int(j), int(j + wing), "low", float(low[j])))
    out.sort(key=lambda p: (p[1], p[0]))
    return out


def structure_features(high: np.ndarray, low: np.ndarray, close: np.ndarray,
                       wing: int, bos_window: int) -> Dict[str, np.ndarray]:
    """market_structure (+1 HH/HL, -1 LH/LL, 0 gemischt/unbekannt) sowie
    bos_up/bos_dn (1 innerhalb `bos_window` Bars nach einem Struktur-Bruch)."""
    n = len(close)
    ms = np.zeros(n)
    last_ph = np.full(n, np.nan)
    last_pl = np.full(n, np.nan)
    pivots = confirmed_pivots(high, low, wing)
    ph_vals: List[float] = []
    pl_vals: List[float] = []
    cur_ms, cur_ph, cur_pl = 0.0, np.nan, np.nan
    k = 0
    for i in range(n):
        while k < len(pivots) and pivots[k][1] <= i:
            _, _, typ, price = pivots[k]
            if typ == "high":
                ph_vals.append(price)
                cur_ph = price
            else:
                pl_vals.append(price)
                cur_pl = price
            if len(ph_vals) >= 2 and len(pl_vals) >= 2:
                hh = ph_vals[-1] > ph_vals[-2]
                hl = pl_vals[-1] > pl_vals[-2]
                lh = ph_vals[-1] < ph_vals[-2]
                ll = pl_vals[-1] < pl_vals[-2]
                cur_ms = 1.0 if (hh and hl) else (-1.0 if (lh and ll) else 0.0)
            k += 1
        ms[i] = cur_ms
        last_ph[i] = cur_ph
        last_pl[i] = cur_pl
    # BOS: Close kreuzt das letzte bestätigte Swing-Hoch/-Tief
    up_evt = np.zeros(n, dtype=bool)
    dn_evt = np.zeros(n, dtype=bool)
    with np.errstate(invalid="ignore"):
        above = close > last_ph
        below = close < last_pl
    up_evt[1:] = above[1:] & ~above[:-1]
    dn_evt[1:] = below[1:] & ~below[:-1]
    w = max(int(bos_window), 1)
    bos_up = _sticky(up_evt, w)
    bos_dn = _sticky(dn_evt, w)
    return {"market_structure": ms, "bos_up": bos_up, "bos_dn": bos_dn}


def _sticky(evt: np.ndarray, window: int) -> np.ndarray:
    """1.0 für `window` Bars ab jedem Event (inklusive Event-Bar)."""
    n = len(evt)
    out = np.zeros(n)
    idx = np.flatnonzero(evt)
    for j in idx:
        out[j:min(j + window, n)] = 1.0
    return out


# ---------------------------------------------------------------------------
# Support/Resistance + Equal Highs/Lows (ruhende Liquidität)
# ---------------------------------------------------------------------------
def sr_features(high: np.ndarray, low: np.ndarray, close: np.ndarray,
                wing: int, max_levels: int = 20,
                eq_tol_pct: float = 0.15) -> Dict[str, np.ndarray]:
    """dist_support_pct / dist_resistance_pct: % Abstand des Close zum
    nächsten bestätigten Swing-Tief darunter / Swing-Hoch darüber.
    eq_low_dist_pct / eq_high_dist_pct: % Abstand zum nächsten Equal-Low/-High
    (>= 2 Pivots innerhalb eq_tol_pct – ruhende Stop-Liquidität).
    NaN solange kein Level existiert."""
    n = len(close)
    dist_sup = np.full(n, np.nan)
    dist_res = np.full(n, np.nan)
    eq_hi_d = np.full(n, np.nan)
    eq_lo_d = np.full(n, np.nan)
    pivots = confirmed_pivots(high, low, wing)
    highs: List[float] = []
    lows: List[float] = []
    eq_his: List[float] = []
    eq_los: List[float] = []

    def _clusters(vals: List[float]) -> List[float]:
        if len(vals) < 2:
            return []
        s = sorted(vals)
        out, grp = [], [s[0]]
        for v in s[1:]:
            if (v - grp[-1]) / max(grp[-1], 1e-12) * 100.0 <= eq_tol_pct:
                grp.append(v)
            else:
                if len(grp) >= 2:
                    out.append(max(grp))
                grp = [v]
        if len(grp) >= 2:
            out.append(max(grp))
        return out

    k = 0
    hi_arr = lo_arr = None
    for i in range(n):
        changed = False
        while k < len(pivots) and pivots[k][1] <= i:
            _, _, typ, price = pivots[k]
            (highs if typ == "high" else lows).append(price)
            changed = True
            k += 1
        if changed:
            highs = highs[-max_levels:]
            lows = lows[-max_levels:]
            hi_arr = np.sort(np.array(highs)) if highs else None
            lo_arr = np.sort(np.array(lows)) if lows else None
            eq_his = _clusters(highs)
            eq_los = _clusters(lows)
        c = close[i]
        if lo_arr is not None:
            # Support = nächstes Level (Hoch ODER Tief) unter dem Kurs
            below = []
            j = np.searchsorted(lo_arr, c) - 1
            if j >= 0:
                below.append(lo_arr[j])
            if hi_arr is not None:
                j = np.searchsorted(hi_arr, c) - 1
                if j >= 0:
                    below.append(hi_arr[j])
            if below:
                dist_sup[i] = (c - max(below)) / max(c, 1e-12) * 100.0
        if hi_arr is not None:
            above = []
            j = np.searchsorted(hi_arr, c)
            if j < len(hi_arr):
                above.append(hi_arr[j])
            if lo_arr is not None:
                j = np.searchsorted(lo_arr, c)
                if j < len(lo_arr):
                    above.append(lo_arr[j])
            if above:
                dist_res[i] = (min(above) - c) / max(c, 1e-12) * 100.0
        if eq_his:
            eq_hi_d[i] = min(abs(v - c) for v in eq_his) / max(c, 1e-12) * 100.0
        if eq_los:
            eq_lo_d[i] = min(abs(v - c) for v in eq_los) / max(c, 1e-12) * 100.0
    return {"dist_support_pct": dist_sup, "dist_resistance_pct": dist_res,
            "eq_high_dist_pct": eq_hi_d, "eq_low_dist_pct": eq_lo_d}


# ---------------------------------------------------------------------------
# Liquidity Sweep (Liquidity Grab)
# ---------------------------------------------------------------------------
def sweep_features(high: np.ndarray, low: np.ndarray, close: np.ndarray,
                   lookback: int, window: int) -> Dict[str, np.ndarray]:
    """liq_sweep_low: Docht reißt das Tief der letzten `lookback` Kerzen,
    Close schließt wieder DARÜBER (bullischer Grab). liq_sweep_high spiegelbildlich
    (bärisch). Wert 1 für `window` Bars nach dem Event."""
    n = len(close)
    if n < lookback + 2:
        z = np.zeros(n)
        return {"liq_sweep_low": z, "liq_sweep_high": z.copy()}
    import pandas as pd
    prior_lo = pd.Series(low).shift(1).rolling(lookback, min_periods=lookback).min().to_numpy()
    prior_hi = pd.Series(high).shift(1).rolling(lookback, min_periods=lookback).max().to_numpy()
    with np.errstate(invalid="ignore"):
        lo_evt = (low < prior_lo) & (close > prior_lo)
        hi_evt = (high > prior_hi) & (close < prior_hi)
    lo_evt = np.nan_to_num(lo_evt).astype(bool)
    hi_evt = np.nan_to_num(hi_evt).astype(bool)
    w = max(int(window), 1)
    return {"liq_sweep_low": _sticky(lo_evt, w),
            "liq_sweep_high": _sticky(hi_evt, w)}


# ---------------------------------------------------------------------------
# Trendkanal (lineare Regression, O(n) über Cumsums)
# ---------------------------------------------------------------------------
def channel_features(close: np.ndarray, period: int) -> Dict[str, np.ndarray]:
    """channel_pos: Lage des Close im ±2σ-Regressions-Kanal (0=Unterkante,
    50=Mittellinie, 100=Oberkante; kann über-/unterschießen).
    channel_slope_pct: Kanal-Steigung über das Fenster in % vom Preis."""
    n = len(close)
    w = max(int(period), 5)
    pos = np.full(n, np.nan)
    slope_pct = np.full(n, np.nan)
    if n < w:
        return {"channel_pos": pos, "channel_slope_pct": slope_pct}
    y = np.asarray(close, dtype=float)
    cy = np.concatenate([[0.0], np.cumsum(y)])
    cy2 = np.concatenate([[0.0], np.cumsum(y * y)])
    j = np.arange(n, dtype=float)
    cjy = np.concatenate([[0.0], np.cumsum(j * y)])
    i = np.arange(w - 1, n)
    s = i - w + 1                     # Fensterstart
    Sy = cy[i + 1] - cy[s]
    Syy = cy2[i + 1] - cy2[s]
    Sjy = cjy[i + 1] - cjy[s]
    Sxy = Sjy - s * Sy                # x relativ zum Fensterstart (0..w-1)
    Sx = w * (w - 1) / 2.0
    Sxx = (w - 1) * w * (2 * w - 1) / 6.0
    denom = w * Sxx - Sx * Sx
    b = (w * Sxy - Sx * Sy) / denom   # Steigung je Bar
    a = (Sy - b * Sx) / w             # Achsenabschnitt
    yhat_end = a + b * (w - 1)        # Kanal-Mitte am aktuellen Bar
    # Residuen-Std: Σ(y-ŷ)² = Syy - 2aSy - 2bSxy + a²w + 2abSx + b²Sxx
    sse = Syy - 2 * a * Sy - 2 * b * Sxy + a * a * w + 2 * a * b * Sx + b * b * Sxx
    sigma = np.sqrt(np.maximum(sse, 0.0) / w)
    width = np.where(sigma > 1e-12, 4.0 * sigma, np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        pos[i] = (y[i] - (yhat_end - 2.0 * sigma)) / width * 100.0
        slope_pct[i] = b * w / np.maximum(y[i], 1e-12) * 100.0
    return {"channel_pos": pos, "channel_slope_pct": slope_pct}


def range_pos(high: np.ndarray, low: np.ndarray, close: np.ndarray,
              period: int) -> np.ndarray:
    """Lage des Close in der Donchian-Spanne der letzten `period` Kerzen
    (0 = am Tief, 100 = am Hoch) – Range-Trading-Basis."""
    import pandas as pd
    p = max(int(period), 2)
    hi = pd.Series(high).rolling(p, min_periods=p).max().to_numpy()
    lo = pd.Series(low).rolling(p, min_periods=p).min().to_numpy()
    rng = hi - lo
    with np.errstate(invalid="ignore", divide="ignore"):
        out = np.where(rng > 0, (close - lo) / np.where(rng > 0, rng, 1) * 100.0, 50.0)
    out[np.isnan(hi)] = np.nan
    return out
