"""Regime-Erkennung (Marktphasen) für dynamische Strategien.

Grundsätze (siehe Anforderungen):
- Rein statistisch: K-Means-Clustering über Trend / Volatilität / Effizienz / Volumen.
- KEIN Lookahead: alle Features je Kerze nutzen ausschließlich zurückliegende Daten.
- Anzahl der Regime wird automatisch bestimmt (Silhouette-Score), mit einstellbarer
  Obergrenze. Zu kleine Regime (< min_share) werden automatisch zusammengelegt.
- Online-Klassifikation liefert Regime + Vertrauenswert; Umschalten nur bei
  ausreichender Sicherheit und Mindesthaltedauer (kein ständiges Hin- und Herspringen).
"""
import logging
import math
from typing import Dict, List, Optional, Tuple

import numpy as np

from services import regime_engine as v2
from services.timeframes import TIMEFRAMES

logger = logging.getLogger(__name__)

# Standard-Engine: "v2" = deterministische Regime-Engine (services.regime_engine),
# "kmeans" = ursprüngliches Clustering (bleibt für alte Modelle/Vergleiche nutzbar).
DEFAULT_ENGINE = "v2"

FEATURE_NAMES = ["trend_pct", "vol_pct", "efficiency", "rel_volume"]
DEFAULT_LOOKBACK_DAYS = 3.0
DEFAULT_MAX_REGIMES = 5
DEFAULT_MIN_SHARE_PCT = 5.0
DEFAULT_CONF_MIN = 0.70
DEFAULT_MIN_HOLD_DAYS = 2.0


def bars_per_day(timeframe: str) -> float:
    return 1440.0 / max(TIMEFRAMES.get(timeframe, 1), 1)


# ---------------- Features (nur rückblickend -> kein Lookahead) ----------------
def compute_features(candles, lookback_bars: int) -> np.ndarray:
    """Feature-Matrix (n, 4) je Kerze; erste `lookback_bars` Zeilen sind NaN.
    trend_pct:  %-Veränderung über das Fenster (Richtung + Stärke)
    vol_pct:    Streuung der Kerzen-Renditen im Fenster (Volatilität, %)
    efficiency: |Netto-Bewegung| / Summe |Einzelbewegungen| (Trend vs. Seitwärts, 0..1)
    rel_volume: Volumen im Fenster relativ zum 5x längeren Referenzfenster
    Vollständig vektorisiert – bei Millionen Kerzen sonst minutenlang."""
    from services.candles import CandleArray
    n = len(candles)
    w = max(int(lookback_bars), 5)
    if isinstance(candles, CandleArray):
        close, vol = candles.cl, candles.vol
    else:
        close = np.array([float(c["close"]) for c in candles])
        vol = np.array([float(c.get("volume") or 0.0) for c in candles])
    feats = np.full((n, 4), np.nan)
    if n <= w:
        return feats
    ret1 = np.zeros(n)
    ret1[1:] = np.abs(np.diff(close))
    log_ret = np.zeros(n)
    log_ret[1:] = np.diff(np.log(np.maximum(close, 1e-12))) * 100.0
    csum_abs = np.concatenate([[0.0], np.cumsum(ret1)])
    csum_vol = np.concatenate([[0.0], np.cumsum(vol)])
    csum_lr = np.concatenate([[0.0], np.cumsum(log_ret)])
    csum_lr2 = np.concatenate([[0.0], np.cumsum(log_ret ** 2)])
    ref_w = min(w * 5, n - 1)

    idx = np.arange(w, n)
    base = close[idx - w]
    with np.errstate(invalid="ignore", divide="ignore"):
        feats[w:, 0] = np.where(base > 0, (close[idx] - base) / np.where(base > 0, base, 1) * 100.0, 0.0)
        m = (csum_lr[idx + 1] - csum_lr[idx + 1 - w]) / w
        var = (csum_lr2[idx + 1] - csum_lr2[idx + 1 - w]) / w - m * m
        feats[w:, 1] = np.sqrt(np.maximum(var, 0.0)) * math.sqrt(w)
        path = csum_abs[idx + 1] - csum_abs[idx + 1 - w]
        feats[w:, 2] = np.where(path > 0, np.abs(close[idx] - base) / np.where(path > 0, path, 1), 0.0)
        j = np.maximum(idx - ref_w, 0)
        ref_vol = (csum_vol[idx + 1] - csum_vol[j]) / np.maximum(idx + 1 - j, 1)
        win_vol = (csum_vol[idx + 1] - csum_vol[idx + 1 - w]) / w
        feats[w:, 3] = np.where(ref_vol > 0, win_vol / np.where(ref_vol > 0, ref_vol, 1), 1.0)
    return feats


# ---------------- K-Means + automatische Regime-Anzahl ----------------
def _kmeans(X: np.ndarray, k: int, seed: int = 42, iters: int = 60
            ) -> Tuple[np.ndarray, np.ndarray]:
    rng = np.random.RandomState(seed)
    # k-means++ Initialisierung
    centroids = [X[rng.randint(len(X))]]
    for _ in range(k - 1):
        d2 = np.min([np.sum((X - c) ** 2, axis=1) for c in centroids], axis=0)
        probs = d2 / max(d2.sum(), 1e-12)
        centroids.append(X[rng.choice(len(X), p=probs)])
    C = np.array(centroids)
    labels = np.zeros(len(X), dtype=int)
    for _ in range(iters):
        d = np.linalg.norm(X[:, None, :] - C[None, :, :], axis=2)
        new_labels = d.argmin(axis=1)
        if np.array_equal(new_labels, labels) and _ > 0:
            break
        labels = new_labels
        for j in range(k):
            pts = X[labels == j]
            if len(pts):
                C[j] = pts.mean(axis=0)
    return C, labels


def _silhouette(X: np.ndarray, labels: np.ndarray, C: np.ndarray) -> float:
    """Vereinfachter Silhouette-Score über Centroid-Distanzen (schnell, stabil)."""
    d = np.linalg.norm(X[:, None, :] - C[None, :, :], axis=2)
    a = d[np.arange(len(X)), labels]
    d_masked = d.copy()
    d_masked[np.arange(len(X)), labels] = np.inf
    b = d_masked.min(axis=1)
    s = (b - a) / np.maximum(np.maximum(a, b), 1e-12)
    return float(np.mean(s))


def _label_de(f_mean, vol_z: float) -> str:
    """Menschlich lesbares Regime-Label aus den ROHEN Feature-Mittelwerten.

    Vorher wurde der z-normierte Centroid benutzt – das ist relativ zum
    Datensatz: In einem Zeitraum, der überwiegend steigt, wurde ein (immer noch
    steigendes) Cluster fälschlich als "Seitwärtsmarkt" beschriftet. Jetzt
    entscheidet das skalenfreie Verhältnis |Trend| / Volatilität zusammen mit
    der Effizienz – Seitwärts ist damit wirklich seitwärts."""
    trend, vola, eff, _vol = [float(x) for x in f_mean]
    strength = abs(trend) / max(vola, 1e-9)
    if strength >= 1.0:
        t = "Aufwärtstrend" if trend > 0 else "Abwärtstrend"
    elif strength >= 0.5 and eff >= 0.08:
        t = "Leicht aufwärts" if trend > 0 else "Leicht abwärts"
    else:
        t = "Seitwärtsmarkt"
    v = "hohe Volatilität" if vol_z > 0.5 else ("niedrige Volatilität" if vol_z < -0.5
                                                else "mittlere Volatilität")
    return f"{t} · {v}"


def is_v2(model: Dict) -> bool:
    return bool(model) and str(model.get("engine") or "").lower() == v2.ENGINE


def relabel_regimes(model: Dict) -> bool:
    """Gespeicherte Modelle auf die aktuelle Label-Logik heben (Migration ohne
    Neu-Clustern). Nutzt die ROHEN Feature-Mittelwerte je Regime; liefert True,
    wenn sich mindestens ein Label geändert hat."""
    changed = False
    if is_v2(model):
        mode = v2.norm_mode((model.get("config") or {}).get(
            "regime_mode", model.get("regime_mode", 9)))
        for r in model.get("regimes") or []:
            new_label = v2.regime_label(int(r.get("id")), mode)
            if new_label != r.get("label"):
                r["label"] = new_label
                changed = True
            if not r.get("nnfx"):
                r["nnfx"] = v2.nnfx_regime(int(r.get("id")), mode)
                r["nnfx_label"] = v2.NNFX_LABELS[r["nnfx"]]
                changed = True
        return changed
    mean = model.get("norm_mean") or [0.0] * 4
    std = model.get("norm_std") or [1.0] * 4
    for r in model.get("regimes") or []:
        f = r.get("features") or {}
        fm = [float(f.get("trend_pct") or 0.0), float(f.get("vol_pct") or 0.0),
              float(f.get("efficiency") or 0.0), float(f.get("rel_volume") or 1.0)]
        vol_z = (fm[1] - float(mean[1])) / max(float(std[1]), 1e-9)
        new_label = _label_de(fm, vol_z)
        if new_label != r.get("label"):
            r["label"] = new_label
            changed = True
        if "stats" not in r:
            lb = float(model.get("lookback_days") or 1.0)
            r["stats"] = {"trend_pct": round(fm[0], 3),
                          "trend_pct_per_day": round(fm[0] / max(lb, 1e-9), 3),
                          "vol_pct": round(fm[1], 3),
                          "efficiency": round(fm[2], 3),
                          "trend_strength": round(abs(fm[0]) / max(fm[1], 1e-9), 2)}
            changed = True
    return changed


def detect_regimes(histories: Dict[str, List[Dict]], timeframe: str,
                   max_regimes: int = DEFAULT_MAX_REGIMES,
                   lookback_days: float = DEFAULT_LOOKBACK_DAYS,
                   min_share_pct: float = DEFAULT_MIN_SHARE_PCT,
                   engine: str = None, engine_config: Dict = None) -> Optional[Dict]:
    """Regime-Modell trainieren.

    engine="v2" (Standard): feste 9er-Taxonomie (Trend x Volatilität) aus
    Multi-Timeframe-Regression + ADX + Volatilitäts-z-Wert – siehe
    services.regime_engine. engine="kmeans": ursprüngliches Clustering (unten).
    """
    if (engine or DEFAULT_ENGINE).lower() in ("v2", "engine_v2", "deterministic"):
        return v2.build_model(histories, timeframe, engine_config)
    return _detect_regimes_kmeans(histories, timeframe, max_regimes,
                                  lookback_days, min_share_pct)


def _detect_regimes_kmeans(histories: Dict[str, List[Dict]], timeframe: str,
                           max_regimes: int = DEFAULT_MAX_REGIMES,
                           lookback_days: float = DEFAULT_LOOKBACK_DAYS,
                           min_share_pct: float = DEFAULT_MIN_SHARE_PCT) -> Optional[Dict]:
    """Regime-Modell auf historischen Daten trainieren.
    Anzahl der Regime automatisch (bester Silhouette-Score, 2..max_regimes);
    Regime mit zu wenig Daten werden in den nächstgelegenen Cluster gemergt.
    Rückgabe: {"centroids", "norm_mean", "norm_std", "regimes":[{id,label,share_pct,...}]}"""
    lookback_bars = max(int(lookback_days * bars_per_day(timeframe)), 8)
    rows = []
    for candles in histories.values():
        f = compute_features(candles, lookback_bars)
        rows.append(f[~np.isnan(f).any(axis=1)])
    if not rows:
        return None
    X_raw = np.concatenate(rows)
    if len(X_raw) < 50:
        return None
    mean = X_raw.mean(axis=0)
    std = np.maximum(X_raw.std(axis=0), 1e-9)
    X = (X_raw - mean) / std
    # Sampling für Geschwindigkeit (deterministisch)
    if len(X) > 4000:
        idx = np.linspace(0, len(X) - 1, 4000).astype(int)
        Xs = X[idx]
    else:
        Xs = X
    max_k = int(min(max(max_regimes, 2), 10))
    best = None
    for k in range(2, max_k + 1):
        C, labels = _kmeans(Xs, k)
        score = _silhouette(Xs, labels, C)
        if best is None or score > best[0] + 1e-6:
            best = (score, k, C)
    score, k, C = best
    # Volle Zuordnung + zu kleine Regime zusammenlegen
    d = np.linalg.norm(X[:, None, :] - C[None, :, :], axis=2)
    labels = d.argmin(axis=1)
    min_n = max(int(len(X) * min_share_pct / 100.0), 20)
    keep = [j for j in range(len(C)) if int((labels == j).sum()) >= min_n]
    if len(keep) < 2:
        keep = list(np.argsort([-(labels == j).sum() for j in range(len(C))])[:2])
    C = C[keep]
    d = np.linalg.norm(X[:, None, :] - C[None, :, :], axis=2)
    labels = d.argmin(axis=1)
    regimes = []
    for j in range(len(C)):
        pts_raw = X_raw[labels == j]
        share = len(pts_raw) / len(X) * 100.0
        f_mean = pts_raw.mean(axis=0) if len(pts_raw) else mean
        strength = abs(float(f_mean[0])) / max(float(f_mean[1]), 1e-9)
        regimes.append({
            "id": j, "label": _label_de(f_mean, float(C[j][1])),
            "share_pct": round(share, 1),
            "features": {FEATURE_NAMES[fi]: round(float(f_mean[fi]), 4)
                         for fi in range(4)},
            "stats": {"trend_pct": round(float(f_mean[0]), 3),
                      "trend_pct_per_day": round(float(f_mean[0]) / max(lookback_days, 1e-9), 3),
                      "vol_pct": round(float(f_mean[1]), 3),
                      "efficiency": round(float(f_mean[2]), 3),
                      "trend_strength": round(strength, 2)},
        })
    return {"centroids": [[round(float(v), 6) for v in c] for c in C],
            "norm_mean": [round(float(v), 6) for v in mean],
            "norm_std": [round(float(v), 6) for v in std],
            "timeframe": timeframe, "lookback_days": lookback_days,
            "lookback_bars": lookback_bars, "silhouette": round(score, 3),
            "k_tested_max": max_k, "n_samples": int(len(X)),
            "regimes": regimes}


# ---------------- Online-Klassifikation (mit Vertrauenswert & Hysterese) ----------------
def classify_point(model: Dict, feat_row) -> Tuple[Optional[int], float, List[float]]:
    """(regime_id, confidence 0..1, Ähnlichkeiten je Regime). confidence basiert
    auf dem Abstand zum besten vs. zweitbesten Centroid."""
    if feat_row is None or np.isnan(feat_row).any():
        return None, 0.0, []
    x = (np.array(feat_row) - np.array(model["norm_mean"])) / np.array(model["norm_std"])
    C = np.array(model["centroids"])
    d = np.linalg.norm(C - x, axis=1)
    sims = 1.0 / np.maximum(d, 1e-9)
    sims = sims / sims.sum()
    order = np.argsort(d)
    best, second = order[0], (order[1] if len(order) > 1 else order[0])
    conf = float(d[second] / max(d[best] + d[second], 1e-12))
    return int(best), round(conf, 4), [round(float(s), 4) for s in sims]


def classify_matrix(model: Dict, feats: np.ndarray):
    """Vektorisierte Klassifikation aller Feature-Zeilen auf einmal.
    -> (rid array int, conf array float, valid mask)"""
    n = feats.shape[0]
    valid = ~np.isnan(feats).any(axis=1)
    rid = np.full(n, -1, dtype=int)
    conf = np.zeros(n)
    if not valid.any():
        return rid, conf, valid
    mean = np.array(model["norm_mean"])
    std = np.array(model["norm_std"])
    C = np.array(model["centroids"])
    X = (feats[valid] - mean) / std
    d = np.linalg.norm(X[:, None, :] - C[None, :, :], axis=2)
    order = np.argsort(d, axis=1)
    best = order[:, 0]
    second = order[:, 1] if C.shape[0] > 1 else order[:, 0]
    rows = np.arange(X.shape[0])
    db, ds = d[rows, best], d[rows, second]
    rid[valid] = best
    conf[valid] = ds / np.maximum(db + ds, 1e-12)
    return rid, conf, valid


def classify_series(model: Dict, candles, timeframe: str,
                    conf_min: float = DEFAULT_CONF_MIN,
                    min_hold_days: float = DEFAULT_MIN_HOLD_DAYS) -> List[Optional[int]]:
    """Aktives Regime je Kerze – nur mit Informationen bis zur jeweiligen Kerze.
    Umschalten nur wenn: neues Regime mit Sicherheit >= conf_min erkannt UND das
    aktuelle Regime mindestens min_hold_days aktiv war (Anti-Flattern)."""
    if is_v2(model):
        return v2.classify_series(model, candles, conf_min, min_hold_days)
    n = len(candles)
    feats = compute_features(candles, int(model["lookback_bars"]))
    rid_arr, conf_arr, valid = classify_matrix(model, feats)
    min_hold_bars = max(int(min_hold_days * bars_per_day(timeframe)), 1)
    out: List[Optional[int]] = [None] * n
    active, active_since = None, 0
    for i in range(n):
        if not valid[i]:
            out[i] = active
            continue
        rid = int(rid_arr[i])
        if active is None:
            active, active_since = rid, i
        elif rid != active and conf_arr[i] >= conf_min and (i - active_since) >= min_hold_bars:
            active, active_since = rid, i
        out[i] = active
    return out


def segments_from_labels(labels: List[Optional[int]]) -> List[Tuple[int, int, int]]:
    """Zusammenhängende (start_idx, end_idx_exklusiv, regime_id)-Abschnitte."""
    segs = []
    start, cur = None, None
    for i, r in enumerate(labels):
        if r is None:
            continue
        if cur is None:
            start, cur = i, r
        elif r != cur:
            segs.append((start, i, cur))
            start, cur = i, r
    if cur is not None:
        segs.append((start, len(labels), cur))
    return segs


def current_regime(model: Dict, candles: List[Dict], timeframe: str,
                   conf_min: float = DEFAULT_CONF_MIN,
                   min_hold_days: float = DEFAULT_MIN_HOLD_DAYS) -> Dict:
    """Aktuelles Regime inkl. Sicherheit, Ähnlichkeiten und letztem Wechsel –
    für die Live-Anzeige ('Aktuelles Regime: X · Sicherheit: 84%')."""
    if is_v2(model):
        return v2.current_regime(model, candles, conf_min, min_hold_days)
    labels = classify_series(model, candles, timeframe, conf_min, min_hold_days)
    feats = compute_features(candles, int(model["lookback_bars"]))
    last_valid = next((i for i in range(len(candles) - 1, -1, -1)
                       if labels[i] is not None), None)
    if last_valid is None:
        return {"regime": None, "confidence": 0.0, "similarities": [],
                "last_switch": None, "reason": "Zu wenig Daten für die Klassifikation"}
    active = labels[last_valid]
    _rid, conf, sims = classify_point(model, feats[last_valid])
    switch_i = last_valid
    while switch_i > 0 and labels[switch_i - 1] == active:
        switch_i -= 1
    reg = next((r for r in model["regimes"] if r["id"] == active), None)
    return {"regime": active,
            "label": (reg or {}).get("label"),
            "confidence": round(conf * 100, 1),
            "similarities": [{"regime": r["id"], "label": r["label"],
                              "similarity_pct": round(sims[r["id"]] * 100, 1) if r["id"] < len(sims) else None}
                             for r in model["regimes"]],
            "last_switch": candles[switch_i]["timestamp"] if switch_i < len(candles) else None,
            "active_since_bars": last_valid - switch_i}
