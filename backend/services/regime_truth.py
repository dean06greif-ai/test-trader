"""Wissenschaftliche Referenz-Regime ("Ground Truth") + Kalibrierung.

Zwei unabhängige Referenzquellen (beide dürfen Zukunft sehen -> NIE live):
1. "centered": zentrierte OLS-Regression. Für jede Kerze wird die
   t-Statistik der log-Preis-Steigung über ein symmetrisches Fenster
   berechnet (halbes Fenster Vergangenheit, halbes Zukunft). Das entspricht
   dem, was das Auge sieht: steigend / fallend / seitwärts. Zusätzlich ein
   kurzes Fenster, damit Trend-ANFÄNGE scharf erkannt werden.
2. "hmm": 3-Zustands-Gauss-HMM (Markov-Switching, Hamilton 1989) auf
   geglätteten Renditen + Volatilität. Lernt die Regime selbst per
   Baum-Welch/Viterbi - keine festen Schwellwerte (nur numpy, kein Extra-Paket).
3. "vote": beide Quellen; bei Uneinigkeit entscheidet die zentrierte Sicht.

Kalibrierung: die Schlüsselparameter der Live-Engine werden per
Koordinaten-Suche so gewählt, dass die (streng rückblickende!) Live-Erkennung
der Referenz maximal entspricht: balancierte Richtungs-Trefferquote minus
Strafen für zu viele/zu wenige Wechsel, Erkennungs-Verzögerung und verpasste
Phasen. Damit sind die Einstellungen messbar an den Zeitraum angepasst statt
willkürlich gewählt.
"""
import logging
import math
from typing import Callable, Dict, List, Optional

import numpy as np

from services import regime_engine as eng

logger = logging.getLogger(__name__)


# ------------------------------------------------------------ Roll-Helfer
def _roll_sums(y: np.ndarray, W: int):
    """Zentrierte Fenster-Summen (Sy, Sxy_lokal, Syy) über Fensterlänge W."""
    n = len(y)
    idx = np.arange(n, dtype=np.float64)
    c0 = np.concatenate(([0.0], np.cumsum(y)))
    c1 = np.concatenate(([0.0], np.cumsum(y * idx)))
    c2 = np.concatenate(([0.0], np.cumsum(y * y)))
    s = np.arange(0, n - W + 1)
    e = s + W
    Sy = c0[e] - c0[s]
    Sxy = (c1[e] - c1[s]) - s * Sy
    Syy = c2[e] - c2[s]
    return Sy, Sxy, Syy


def _centered_t(y: np.ndarray, W: int):
    """t-Statistik + Steigung der OLS-Regression, zentriert (NaN an den Rändern)."""
    n = len(y)
    W = int(W)
    if W % 2 == 0:
        W += 1
    W = min(max(W, 7), n - 2 if n > 9 else 7)
    if n < W + 2:
        return np.full(n, np.nan), np.full(n, np.nan)
    Sy, Sxy, Syy = _roll_sums(y, W)
    Sx = W * (W - 1) / 2.0
    Sxx = (W - 1) * W * (2 * W - 1) / 6.0
    den = W * Sxx - Sx * Sx
    slope = (W * Sxy - Sx * Sy) / den
    intercept = (Sy - slope * Sx) / W
    sse = np.maximum(Syy - intercept * Sy - slope * Sxy, 0.0)
    se = np.sqrt((sse / max(W - 2, 1)) / (Sxx - Sx * Sx / W) + 1e-18)
    t = slope / np.maximum(se, 1e-12)
    h = W // 2
    tf = np.full(n, np.nan)
    sf = np.full(n, np.nan)
    tf[h:h + len(t)] = t
    sf[h:h + len(slope)] = slope
    return tf, sf


def _centered_mean(a: np.ndarray, W: int) -> np.ndarray:
    n = len(a)
    W = int(W)
    if W % 2 == 0:
        W += 1
    W = min(max(W, 3), n - 2 if n > 5 else 3)
    if n < W + 2:
        return np.full(n, np.nan)
    c = np.concatenate(([0.0], np.cumsum(a)))
    out = np.full(n, np.nan)
    m = (c[W:] - c[:-W]) / W
    out[W // 2:W // 2 + len(m)] = m
    return out


def _segments(ids: np.ndarray):
    segs = []
    start, cur = None, None
    for i, r in enumerate(ids):
        if cur is None:
            start, cur = i, r
        elif r != cur:
            segs.append((start, i, cur))
            start, cur = i, r
    if cur is not None:
        segs.append((start, len(ids), cur))
    return segs


def _merge_short(ids: np.ndarray, min_bars: int) -> np.ndarray:
    """Zu kurze Abschnitte in den Nachbarn mergen (Rauschen entfernen)."""
    ids = ids.copy()
    for _ in range(4):
        segs = [(s, e, r) for (s, e, r) in _segments(ids)]
        changed = False
        for k, (s, e, r) in enumerate(segs):
            if r < 0 or e - s >= min_bars:
                continue
            prev_r = segs[k - 1][2] if k > 0 else -1
            next_r = segs[k + 1][2] if k + 1 < len(segs) else -1
            tgt = prev_r if prev_r >= 0 else next_r
            if next_r >= 0 and prev_r >= 0 and (e - s) > 0:
                # Nachbar mit gleicher Trendrichtung bevorzugen
                tgt = prev_r
            if tgt >= 0 and tgt != r:
                ids[s:e] = tgt
                changed = True
        if not changed:
            break
    return ids


def _fill_nan(a: np.ndarray) -> np.ndarray:
    idx = np.where(np.isfinite(a))[0]
    if len(idx) == 0:
        return a
    return np.interp(np.arange(len(a), dtype=np.float64), idx, a[idx])


def _fill_edges(ids: np.ndarray) -> np.ndarray:
    """Unbekannte Ränder (zentriertes Fenster ragt über die Daten hinaus) mit
    dem nächstgelegenen bekannten Regime auffüllen – nur Kopf/Ende."""
    v = np.where(ids >= 0)[0]
    if len(v):
        ids[:v[0]] = ids[v[0]]
        ids[v[-1] + 1:] = ids[v[-1]]
    return ids


def _majority_smooth(trend: np.ndarray, valid: np.ndarray, Wm: int) -> np.ndarray:
    """Rollende Mehrheits-Glättung der Trend-Achse (entfernt Flip-Flops in
    Trends, ohne echte Wechsel zu verschieben)."""
    n = len(trend)
    Wm = int(Wm) | 1
    if Wm < 3 or n < Wm + 2:
        return trend
    counts = np.zeros((n, 3))
    for cls in range(3):
        a = ((trend == cls) & valid).astype(np.float64)
        c = np.concatenate(([0.0], np.cumsum(a)))
        s = c[Wm:] - c[:-Wm]
        counts[Wm // 2:Wm // 2 + len(s), cls] = s
    tot = counts.sum(1)
    out = trend.copy()
    m = tot > Wm * 0.3
    out[m] = counts[m].argmax(1)
    return out


def _sub_axis(candles, cfg: Dict, mode: int, t_abs: np.ndarray) -> np.ndarray:
    """Zweite Achse der Taxonomie (Vola-Stufe bzw. Trend-Stärke), zentriert."""
    n = len(candles)
    if mode == 3:
        return np.zeros(n, dtype=int)
    if mode == 5:
        strong = np.nan_to_num(t_abs, nan=0.0) >= float(cfg.get("trend_strong_t", 4.5))
        return strong.astype(int)
    close = np.array([float(c["close"]) for c in candles], dtype=np.float64)
    r = np.diff(np.log(np.maximum(close, 1e-12)), prepend=np.log(max(close[0], 1e-12)))
    r[0] = 0.0
    Wv = max(int(cfg.get("vol_window_bars") or 30), 5)
    m1 = _centered_mean(r, Wv)
    m2 = _centered_mean(r * r, Wv)
    v = np.sqrt(np.maximum(m2 - m1 * m1, 0.0))
    fin = v[np.isfinite(v)]
    med = float(np.median(fin)) if len(fin) else 0.0
    mad = float(np.median(np.abs(fin - med))) if len(fin) else 1.0
    z = (v - med) / max(1.4826 * mad, 1e-12)
    sub = np.ones(n, dtype=int)
    sub[np.nan_to_num(z, nan=0.0) < float(cfg.get("vol_low_z", -0.55))] = 0
    sub[np.nan_to_num(z, nan=0.0) > float(cfg.get("vol_high_z", 0.65))] = 2
    return sub


# ------------------------------------------------------------ Quelle 1: zentriert
def centered_labels(candles, cfg: Dict, mode: int = 9) -> List[Optional[int]]:
    """Zentrierte Referenz-Regime: OLS-t-Statistik über ein symmetrisches
    Fenster + kurzes Fenster für scharfe Trend-Anfänge + Mindestlängen-Merge.
    Garantien: steigende Abschnitte haben positive Steigung, fallende negative,
    seitwärts = statistisch nicht signifikanter Drift."""
    mode = eng.norm_mode(mode)
    n = len(candles)
    close = np.array([float(c["close"]) for c in candles], dtype=np.float64)
    logc = np.log(np.maximum(close, 1e-12))
    bpd = float(cfg.get("bars_per_day") or 1.0)
    hz = sorted(cfg.get("horizons_days") or [10.0, 30.0])
    w_days = float(hz[len(hz) // 2])
    W = max(int(round(w_days * bpd)), 15)
    W = min(W, max(n // 3, 15))
    t_long, sl_long = _centered_t(logc, W)
    W2 = max(int(W // 3) | 1, 9)
    t_short, sl_short = _centered_t(logc, W2)

    # Netto-Bewegung des Fensters, skaliert an der Volatilität (z-Wert).
    # Wichtig: die OLS-t-Statistik allein ist bei Preisreihen (Random Walk)
    # nach oben verzerrt (autokorrelierte Residuen) – erst die Kombination
    # "signifikante Steigung UND vol-skalierter Netto-Move" trennt echte
    # Trends von zufälligem Umherwandern.
    r = np.diff(logc, prepend=logc[0])
    r[0] = 0.0
    m2 = _centered_mean(r * r, min(W * 3, max(n - 3, 3)))
    sigma_daily = np.sqrt(np.maximum(_fill_nan(m2), 1e-12) * bpd)

    def _drift_z(win):
        h = win // 2
        net = np.full(n, np.nan)
        net[h:n - h] = logc[2 * h:] - logc[:n - 2 * h]
        return net / np.maximum(sigma_daily * math.sqrt(win / max(bpd, 1e-9)), 1e-9)

    dz_long = _drift_z(W if W % 2 else W + 1)
    dz_short = _drift_z(W2)

    t_thr = float(cfg.get("trend_t", 2.0))
    trend = np.ones(n, dtype=int)
    up = (t_long >= t_thr) & (dz_long >= 0.9) & (sl_long > 0)
    dn = (t_long <= -t_thr) & (dz_long <= -0.9) & (sl_long < 0)
    trend[np.nan_to_num(up, nan=False).astype(bool)] = 2
    trend[np.nan_to_num(dn, nan=False).astype(bool)] = 0
    # Kurzes Fenster: sehr signifikante kurzfristige Bewegungen überstimmen das
    # lange Fenster -> Trend-Anfänge/-Enden werden früher/schärfer gesetzt.
    s_up = (t_short >= t_thr * 1.6) & (dz_short >= 1.8)
    s_dn = (t_short <= -t_thr * 1.6) & (dz_short <= -1.8)
    trend[np.nan_to_num(s_up, nan=False).astype(bool)] = 2
    trend[np.nan_to_num(s_dn, nan=False).astype(bool)] = 0

    invalid = ~np.isfinite(t_long)
    trend = _majority_smooth(trend, ~invalid, max(W // 2, 5))
    t_abs = np.abs(np.nan_to_num(t_long, nan=0.0))
    sub = _sub_axis(candles, cfg, mode, t_abs)
    ids = np.array([eng.regime_id(int(trend[i]), int(sub[i]), mode)
                    for i in range(n)], dtype=int)
    ids[invalid] = -1
    total_days = n / max(bpd, 1e-9)
    min_bars = max(int(max(total_days * 0.008, 1.5) * bpd), 3)
    ids = _merge_short(ids, min_bars)
    return [None if r < 0 else int(r) for r in ids]


# ------------------------------------------------------------ Quelle 2: HMM
def _hmm_states(X: np.ndarray, iters: int = 25, sticky: float = 0.985):
    """3-Zustands-Gauss-HMM (diagonale Kovarianz): Baum-Welch + Viterbi."""
    n = len(X)
    order = np.argsort(X[:, 0])
    terc = np.array_split(order, 3)
    means = np.array([X[t].mean(axis=0) for t in terc])
    var = np.tile(X.var(axis=0) + 1e-6, (3, 1))
    A = np.full((3, 3), (1 - sticky) / 2.0)
    np.fill_diagonal(A, sticky)
    pi = np.full(3, 1.0 / 3.0)

    def emis(means, var):
        ll = -0.5 * (((X[:, None, :] - means[None]) ** 2) / var[None]
                     + np.log(2 * np.pi * var[None])).sum(-1)
        return np.exp(ll - ll.max(axis=1, keepdims=True)) + 1e-300

    for _ in range(iters):
        B = emis(means, var)
        alpha = np.zeros((n, 3))
        c = np.zeros(n)
        alpha[0] = pi * B[0]
        c[0] = alpha[0].sum() + 1e-300
        alpha[0] /= c[0]
        for t in range(1, n):
            alpha[t] = (alpha[t - 1] @ A) * B[t]
            c[t] = alpha[t].sum() + 1e-300
            alpha[t] /= c[t]
        beta = np.ones((n, 3))
        for t in range(n - 2, -1, -1):
            beta[t] = (A @ (B[t + 1] * beta[t + 1])) / c[t + 1]
        gamma = alpha * beta
        gamma /= gamma.sum(1, keepdims=True) + 1e-300
        xi = np.zeros((3, 3))
        for t in range(n - 1):
            x = (alpha[t][:, None] * A) * (B[t + 1] * beta[t + 1])[None]
            xi += x / (x.sum() + 1e-300)
        A = xi / (xi.sum(1, keepdims=True) + 1e-300)
        pi = gamma[0]
        w = gamma.sum(0) + 1e-300
        means = (gamma.T @ X) / w[:, None]
        var = np.maximum((gamma.T @ (X ** 2)) / w[:, None] - means ** 2, 1e-6)
    # Viterbi
    logA = np.log(A + 1e-300)
    B = emis(means, var)
    logB = np.log(B)
    delta = np.log(pi + 1e-300) + logB[0]
    psi = np.zeros((n, 3), dtype=int)
    for t in range(1, n):
        m = delta[:, None] + logA
        psi[t] = m.argmax(0)
        delta = m.max(0) + logB[t]
    states = np.zeros(n, dtype=int)
    states[-1] = int(delta.argmax())
    for t in range(n - 2, -1, -1):
        states[t] = psi[t + 1][states[t + 1]]
    return states, means


def hmm_labels(candles, cfg: Dict, mode: int = 9) -> List[Optional[int]]:
    """Markov-Switching-Referenz: HMM lernt drei Zustände auf geglätteten
    Renditen + Volatilität; Zustände werden nach mittlerer Rendite auf
    fallend/seitwärts/steigend abgebildet."""
    mode = eng.norm_mode(mode)
    n = len(candles)
    if n < 120:
        return [None] * n
    close = np.array([float(c["close"]) for c in candles], dtype=np.float64)
    logc = np.log(np.maximum(close, 1e-12))
    bpd = float(cfg.get("bars_per_day") or 1.0)
    r = np.diff(logc, prepend=logc[0])
    r[0] = 0.0
    total_days = n / max(bpd, 1e-9)
    w1_days = max(total_days * 0.015, 2.0)
    f1 = _centered_mean(r, max(int(w1_days * bpd) | 1, 3))       # geglätteter Drift
    f2 = _centered_mean(np.abs(r), max(int(w1_days * 3 * bpd) | 1, 5))  # Vola
    valid = np.isfinite(f1) & np.isfinite(f2)
    f1 = np.nan_to_num(f1, nan=0.0)
    f2 = np.nan_to_num(f2, nan=float(np.nanmean(f2)) if np.isfinite(f2).any() else 0.0)
    X = np.stack([f1, np.log(f2 + 1e-9)], axis=1)
    X = (X - X.mean(0)) / np.maximum(X.std(0), 1e-9)
    # Für lange Reihen strided trainieren/decodieren (HMM-Loops sind O(n))
    stride = max(n // 15000, 1)
    Xs = X[::stride]
    try:
        states_s, means = _hmm_states(Xs, sticky=0.997)
    except Exception as e:  # noqa: BLE001 – numerische Sonderfälle
        logger.warning(f"hmm failed: {e}")
        return [None] * n
    rank = np.argsort(means[:, 0])  # niedrigste mittlere Rendite -> fallend
    trend_of_state = {int(rank[0]): 0, int(rank[1]): 1, int(rank[2]): 2}
    trend = np.repeat(np.array([trend_of_state[int(s)] for s in states_s]), stride)[:n]
    if len(trend) < n:
        trend = np.concatenate([trend, np.full(n - len(trend), trend[-1])])
    trend = _majority_smooth(trend, valid, max(int(total_days * 0.02 * bpd), 5))
    t_dummy = np.zeros(n)
    sub = _sub_axis(candles, cfg, mode, t_dummy)
    ids = np.array([eng.regime_id(int(trend[i]), int(sub[i]), mode)
                    for i in range(n)], dtype=int)
    ids[~valid] = -1
    min_bars = max(int(max(total_days * 0.008, 1.5) * bpd), 3)
    ids = _merge_short(ids, min_bars)
    ids = _fill_edges(ids)
    return [None if x < 0 else int(x) for x in ids]


def truth_labels(candles, cfg: Dict, mode: int, source: str = "centered") -> List[Optional[int]]:
    source = (source or "centered").lower()
    if source == "hmm":
        return hmm_labels(candles, cfg, mode)
    cen = centered_labels(candles, cfg, mode)
    if source != "vote":
        return cen
    hm = hmm_labels(candles, cfg, mode)
    out = []
    for i in range(len(cen)):
        a, b = cen[i], (hm[i] if i < len(hm) else None)
        if a is None:
            out.append(None)
        elif b is not None and eng.split_id(a, mode)[0] == eng.split_id(b, mode)[0]:
            out.append(a)
        else:
            out.append(a)  # bei Uneinigkeit entscheidet die zentrierte Sicht
    return out


# ------------------------------------------------------------ Metriken
def _trend_arr(labels, mode: int) -> np.ndarray:
    return np.array([-1 if l is None else eng.split_id(int(l), mode)[0]
                     for l in labels], dtype=int)


def _switches(tr: np.ndarray) -> int:
    v = tr[tr >= 0]
    return int(np.sum(v[1:] != v[:-1])) if len(v) > 1 else 0


def agreement(live, truth, mode: int) -> Dict:
    n = min(len(live), len(truth))
    tl = _trend_arr(live[:n], mode)
    tt = _trend_arr(truth[:n], mode)
    m = (tl >= 0) & (tt >= 0)
    if not m.any():
        return {"bars": 0, "exact_pct": 0.0, "direction_pct": 0.0,
                "balanced_direction_pct": 0.0, "switches_live": 0, "switches_truth": 0}
    exact = float(np.mean([live[i] == truth[i] for i in np.where(m)[0]])) * 100
    dir_pct = float(np.mean(tl[m] == tt[m])) * 100
    recalls = []
    for cls in (0, 1, 2):
        mask = m & (tt == cls)
        if mask.any():
            recalls.append(float(np.mean(tl[mask] == cls)))
    bal = float(np.mean(recalls)) * 100 if recalls else 0.0
    return {"bars": int(m.sum()), "exact_pct": round(exact, 1),
            "direction_pct": round(dir_pct, 1),
            "balanced_direction_pct": round(bal, 1),
            "switches_live": _switches(tl), "switches_truth": _switches(tt)}


def detection_lag_days(live, truth, mode: int, bpd: float) -> Dict:
    """Wie schnell erkennt die Live-Erkennung einen Referenz-Trendwechsel?"""
    n = min(len(live), len(truth))
    tl = _trend_arr(live[:n], mode)
    tt = _trend_arr(truth[:n], mode)
    lags, missed, total = [], 0, 0
    for (s, e, r) in _segments(tt):
        if r < 0 or (e - s) < max(int(bpd), 2):
            continue
        total += 1
        hit = np.where(tl[s:e] == r)[0]
        if len(hit):
            lags.append(hit[0] / max(bpd, 1e-9))
        else:
            missed += 1
    return {"segments": total,
            "mean_lag_days": round(float(np.mean(lags)), 2) if lags else None,
            "median_lag_days": round(float(np.median(lags)), 2) if lags else None,
            "missed": missed,
            "missed_pct": round(missed / total * 100, 1) if total else 0.0}


# ------------------------------------------------------------ Kalibrierung
def _uniq_clip(vals, lo, hi):
    out = sorted({round(float(min(max(v, lo), hi)), 4) for v in vals})
    return out


def calibrate(histories: Dict[str, List[Dict]], timeframe: str,
              engine_config: Dict, source: str = "centered",
              stop: Callable = None, progress: Callable = None) -> Optional[Dict]:
    """Koordinaten-Suche über die Schlüsselparameter der Engine v2, bewertet
    gegen die Referenz-Regime. Gibt best_config (vollständig, deterministisch)
    + Vorher/Nachher-Metriken zurück. None = abgebrochen."""
    base_user = dict(engine_config or {})
    n_max = max(len(c) for c in histories.values())
    base = eng.resolve_config(base_user, timeframe, n_max)
    mode = eng.norm_mode(base["regime_mode"])
    bpd = base["bars_per_day"]
    d = float(base["total_days"])

    truths = {}
    for sym, candles in histories.items():
        if stop and stop():
            return None
        if progress:
            progress(5, f"Referenz-Regime ({source}): {sym}")
        cfg_s = eng.resolve_config(base_user, timeframe, len(candles))
        truths[sym] = truth_labels(candles, cfg_s, mode, source)

    feat_cache: Dict = {}

    def features_for(sym: str, smooth_days: float):
        key = (sym, round(float(smooth_days), 4))
        if key not in feat_cache:
            cfg = eng.resolve_config({**base_user, "smooth_days": smooth_days},
                                     timeframe, len(histories[sym]))
            feat_cache[key] = eng.compute_matrix(histories[sym], cfg)
        return feat_cache[key]

    def evaluate(ov: Dict) -> Dict:
        per, scores = {}, []
        for sym, candles in histories.items():
            f = features_for(sym, ov["smooth_days"])
            cfg = eng.resolve_config({**base_user, **ov}, timeframe, len(candles))
            ids, _c = eng.classify_arrays(f, cfg)
            live = [None if x < 0 else int(x) for x in ids]
            m = agreement(live, truths[sym], mode)
            lag = detection_lag_days(live, truths[sym], mode, bpd)
            sw_pen = 14.0 * min(abs(m["switches_live"] - m["switches_truth"])
                                / max(m["switches_truth"], 1), 2.0)
            lag_pen = 10.0 * min((lag["mean_lag_days"] or 0.0)
                                 / max(d * 0.02, 1.0), 1.0)
            miss_pen = 20.0 * (lag["missed_pct"] or 0.0) / 100.0
            score = m["balanced_direction_pct"] - sw_pen - lag_pen - miss_pen
            scores.append(score)
            per[sym] = {**m, **lag, "score": round(score, 2)}
        keys = ("balanced_direction_pct", "direction_pct", "exact_pct")
        summary = {k: round(float(np.mean([p[k] for p in per.values()])), 1) for k in keys}
        summary["switches_live"] = sum(p["switches_live"] for p in per.values())
        summary["switches_truth"] = sum(p["switches_truth"] for p in per.values())
        lag_vals = [p["mean_lag_days"] for p in per.values() if p["mean_lag_days"] is not None]
        summary["mean_lag_days"] = round(float(np.mean(lag_vals)), 2) if lag_vals else None
        summary["missed_pct"] = round(float(np.mean([p["missed_pct"] for p in per.values()])), 1)
        summary["score"] = round(float(np.mean(scores)), 2)
        return {"score": float(np.mean(scores)), "summary": summary, "per_symbol": per}

    grid = {
        "smooth_days": _uniq_clip([base["smooth_days"], d * 0.002, d * 0.0055, d * 0.010], 0.1, 15.0),
        "trend_t": _uniq_clip([base["trend_t"], 1.2, 1.6, 2.0, 2.5, 3.2], 0.5, 10.0),
        "hysteresis": _uniq_clip([base["hysteresis"], 0.15, 0.3, 0.45], 0.0, 0.9),
        "confirm_days": _uniq_clip([base["confirm_days"], d * 0.003, d * 0.008, d * 0.015], 0.25, 20.0),
        "min_hold_days": _uniq_clip([base["min_hold_days"], d * 0.006, d * 0.012,
                                     d * 0.024, d * 0.045], 1.0, 30.0),
        "adx_min": _uniq_clip([base["adx_min"], 10.0, 18.0, 25.0], 0.0, 60.0),
        "confidence_min": _uniq_clip([base["confidence_min"], 0.45, 0.55, 0.65], 0.0, 0.95),
    }
    cur = {k: (base[k] if k != "min_hold_days" else min(base[k], 30.0)) for k in grid}
    total_evals = 2 * sum(len(v) for v in grid.values())
    done_evals = 0

    baseline = evaluate(cur)
    best_score = baseline["score"]
    best_eval = baseline
    for sweep in range(2):
        for pkey, cands in grid.items():
            for v in cands:
                if stop and stop():
                    return None
                done_evals += 1
                if progress:
                    progress(10 + int(done_evals / max(total_evals, 1) * 85),
                             f"Kalibriere {pkey}={v:g} (Durchlauf {sweep + 1}/2)")
                if v == cur[pkey]:
                    continue
                trial = {**cur, pkey: v}
                ev = evaluate(trial)
                if ev["score"] > best_score + 1e-6:
                    best_score = ev["score"]
                    best_eval = ev
                    cur = trial

    best_config = {
        "auto_adapt": False, "adapt_profile": "off", "regime_mode": mode,
        "horizons_days": base["horizons_days"],
        "vol_ref_days": base["vol_ref_days"],
        "vol_window_days": base["vol_window_days"],
        "vol_smooth_days": base["vol_smooth_days"],
        "gate_timeout_days": base["gate_timeout_days"],
        "validate_min_segment_days": base["validate_min_segment_days"],
        **{k: round(float(v), 4) for k, v in cur.items()},
    }
    return {"truth_source": source, "regime_mode": mode, "timeframe": timeframe,
            "total_days": round(d, 1), "symbols": list(histories.keys()),
            "evals": done_evals,
            "baseline": baseline["summary"], "best": best_eval["summary"],
            "per_symbol": best_eval["per_symbol"],
            "best_config": best_config,
            "objective": ("balancierte Richtungs-Trefferquote vs. Referenz "
                          "minus Wechsel-/Verzögerungs-/Verpasst-Strafen")}
