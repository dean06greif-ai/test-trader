"""Kombi-Detektor (Detektor "kombi", Etappe 1).

Kombiniert die glatte EMA-Steigungs-Erkennung mit den reaktiven
Umkehrpunkten – als EIGENSTÄNDIGE, parallele Option (die Detektoren
"reactive" und "ema" bleiben unverändert):

1. BASIS = EMA-Steigung: Regime folgt der Steigung EINER EMA
   (Default 14 Tage), gemessen über ein Steigungs-Fenster (Default 5 Tage),
   normiert auf die Tagesvolatilität. Trend ab kombi_thr (Default 0.18 x Vola),
   zurück zu Seitwärts erst unter der Hälfte (Hysterese).
   LIVE = streng kausal (nur Vergangenheit) · FINAL = zentrierte, lag-freie
   Steigung (Zukunftssicht ist in der Final-Sicht erlaubt).
2. UMKEHRPUNKTE NUR ALS BESCHLEUNIGER: ein bestätigter Hoch-/Tiefpunkt
   (ATR-ZigZag wie beim reaktiven Detektor) darf einen Wechsel, den die
   EMA-Sicht bereits anzeigt, SOFORT schalten (überspringt die
   Stabilitäts-Wartezeit) bzw. eine Trend-Dominanz-Überbrückung sofort
   beenden. Er erzeugt NIE eigenständig ein Regime, das die EMA-Steigung
   nicht sieht.
3. TREND-DOMINANZ: Seitwärts-Einschübe bis kombi_dominance_days (Default
   3 Tage) INNERHALB eines Trends werden dem Trend zugeschlagen –
   live kausal überbrückt (Seitwärts wird erst nach Ablauf angezeigt),
   final rückwirkend absorbiert (nur zwischen GLEICHGERICHTETEN Trends).

Ziel: mittlere Phasendauer im 5-15-Tage-Band, keine Mini-Seitwärtsphasen
mitten in einem übergeordneten Trend, kein Lookahead in der Live-Sicht.
"""
import logging
from typing import Dict, Tuple

import numpy as np

from services import regime_features as rf
from services.regime_reactive import _absorb_short, _ffill

logger = logging.getLogger(__name__)


def _hyst_labels(sn: np.ndarray, thr: float, lo: float) -> np.ndarray:
    """Hysterese-Zustandsautomat: Trend ab |Steigung| >= thr, zurück zu
    Seitwärts erst unter lo (= thr/2). 0 ab · 1 seitwärts · 2 auf."""
    n = len(sn)
    lab = np.ones(n, dtype=np.int8)
    cur = 1
    for i in range(n):
        s = sn[i]
        if cur == 2:
            cur = 0 if s <= -thr else (1 if s < lo else 2)
        elif cur == 0:
            cur = 2 if s >= thr else (1 if s > -lo else 0)
        else:
            cur = 2 if s >= thr else (0 if s <= -thr else 1)
        lab[i] = cur
    return lab


def _pivot_scan(high: np.ndarray, low: np.ndarray, close: np.ndarray,
                thr_piv: np.ndarray, persist: int) -> Tuple:
    """Kausaler ATR-ZigZag (wie Detektor 'reactive', ohne Volumen-Boost):
    liefert bestätigte Umkehrpunkte + Beschleunigungs-Signale.
    accel[i] = +1 (Tief bestätigt -> Aufwärtsphase beginnt) bzw. -1
    (Hoch bestätigt -> Abwärtsphase beginnt) am Bestätigungs-Bar."""
    n = len(close)
    accel = np.zeros(n, dtype=np.int8)
    retrace = np.zeros(n)
    rev_count = np.zeros(n, dtype=np.int64)
    since_ext = np.zeros(n, dtype=np.int64)
    piv_dir = np.zeros(n, dtype=np.int8)
    pivots = []
    if not n:
        return pivots, accel, retrace, rev_count, since_ext, piv_dir
    d = 0
    ext_hi, ext_hi_i = float(high[0]), 0
    ext_lo, ext_lo_i = float(low[0]), 0
    cnt_up = cnt_dn = 0
    for i in range(n):
        if high[i] >= ext_hi:
            ext_hi, ext_hi_i = float(high[i]), i
        if low[i] <= ext_lo:
            ext_lo, ext_lo_i = float(low[i]), i
        th = thr_piv[i]
        drop = (ext_hi - close[i]) / max(ext_hi, 1e-12) * 100.0
        rise = (close[i] - ext_lo) / max(ext_lo, 1e-12) * 100.0
        if d >= 0:
            cnt_dn = cnt_dn + 1 if drop >= th else 0
        if d <= 0:
            cnt_up = cnt_up + 1 if rise >= th else 0
        if d >= 0 and cnt_dn >= persist:
            pivots.append({"type": "high", "i": int(ext_hi_i),
                           "price": float(ext_hi), "confirmed_i": i})
            accel[i] = -1
            d = -1
            j0 = ext_hi_i
            k = int(np.argmin(low[j0:i + 1]))
            ext_lo, ext_lo_i = float(low[j0 + k]), j0 + k
            ext_hi, ext_hi_i = float(high[i]), i
            cnt_dn = cnt_up = 0
        elif d <= 0 and cnt_up >= persist:
            pivots.append({"type": "low", "i": int(ext_lo_i),
                           "price": float(ext_lo), "confirmed_i": i})
            accel[i] = 1
            d = 1
            j0 = ext_lo_i
            k = int(np.argmax(high[j0:i + 1]))
            ext_hi, ext_hi_i = float(high[j0 + k]), j0 + k
            ext_lo, ext_lo_i = float(low[i]), i
            cnt_up = cnt_dn = 0
        piv_dir[i] = d
        if d > 0:
            since_ext[i] = i - ext_hi_i
            retrace[i] = max(drop, 0.0)
            rev_count[i] = cnt_dn
        elif d < 0:
            since_ext[i] = i - ext_lo_i
            retrace[i] = max(rise, 0.0)
            rev_count[i] = cnt_up
        else:
            since_ext[i] = i
    return pivots, accel, retrace, rev_count, since_ext, piv_dir


def _dominance_merge(lab: np.ndarray, max_bars: int) -> np.ndarray:
    """Final-Sicht der Trend-Dominanz: Seitwärts-Läufe bis max_bars zwischen
    zwei GLEICHGERICHTETEN Trend-Läufen werden dem Trend zugeschlagen
    (Mini-Seitwärtsphase mitten im übergeordneten Trend)."""
    out = lab.copy()
    for _ in range(8):
        runs, s = [], 0
        for i in range(1, len(out) + 1):
            if i == len(out) or out[i] != out[s]:
                runs.append((s, i, int(out[s])))
                s = i
        changed = False
        for k in range(1, len(runs) - 1):
            a, b_run, c = runs[k - 1][2], runs[k], runs[k + 1][2]
            s0, s1, blab = b_run
            if blab == 1 and a == c and a in (0, 2) and (s1 - s0) <= max_bars:
                out[s0:s1] = a
                changed = True
        if not changed:
            break
    return out


def detect_kombi(f: Dict, cfg: Dict) -> Dict:
    close = np.asarray(f["close"], dtype=float)
    high = np.asarray(f["high"], dtype=float)
    low = np.asarray(f["low"], dtype=float)
    n = len(close)
    bpd = max(float(cfg.get("bars_per_day") or 24.0), 1e-9)
    dvol = np.nan_to_num(np.asarray(f["daily_vol_pct"], dtype=float), nan=2.0)
    dvol = np.where(dvol <= 0, 2.0, dvol)

    # --- 1. Basis: EMA-Steigung (kausal + zentriert) ---
    days = float(cfg.get("kombi_ema_days") or 14.0)
    span = max(int(round(days * bpd)), 2)
    ema = rf.ema(close, span)
    thr = max(float(cfg.get("kombi_thr") or 0.18), 0.01)
    lo = thr * 0.5
    k = max(int(round(float(cfg.get("kombi_slope_days") or 5.0) * bpd)), 1)

    def _slope_n(shift_back: int, shift_fwd: int) -> np.ndarray:
        w = shift_back + shift_fwd
        sn = np.zeros(n)
        if n > w > 0:
            seg = (ema[w:] / np.maximum(ema[:-w], 1e-12) - 1.0) * 100.0 / (w / bpd)
            sn[shift_back:n - shift_fwd] = seg
            sn[n - shift_fwd:] = sn[n - shift_fwd - 1] if shift_fwd else 0.0
        return sn / dvol

    sn_live = _slope_n(k, 0)                          # streng kausal
    h = max(int(round(k / 2)), 1)
    sn_final = _slope_n(h, h) if n > 2 * h else sn_live.copy()
    if n > 2 * h:                                     # Rand: kausal auffüllen
        sn_final[n - h:] = sn_live[n - h:]

    raw_live = _hyst_labels(sn_live, thr, lo)

    # --- 2. Umkehrpunkte (ATR-ZigZag) – nur Beschleuniger ---
    atr = np.asarray(f["atr_pct"], dtype=float)
    finite = np.isfinite(atr)
    warm_atr = int(np.argmax(finite)) if finite.any() else n
    mult = float(cfg.get("rev_atr_mult") or 3.0)
    lo_pct = float(cfg.get("rev_min_pct") or 0.0) or 0.05
    hi_pct = float(cfg.get("rev_max_pct") or 30.0)
    thr_piv = np.clip(mult * _ffill(atr, warm_atr), lo_pct, hi_pct)
    persist = min(max(int(cfg.get("persist_candles") or 3), 1), 20)
    use_piv = bool(cfg.get("kombi_pivot_accel", True))
    pivots, accel, retrace, rev_count, since_ext, piv_dir = _pivot_scan(
        high, low, close, thr_piv, persist)
    if not use_piv:
        accel = np.zeros(n, dtype=np.int8)

    # --- 3. Kausale Live-Sicht: Stabilitäts-Filter + Trend-Dominanz +
    #        Pivot-Beschleunigung ---
    stab = max(int(round(float(cfg.get("kombi_persist_days") or 1.0) * bpd)), 1)
    dom = max(int(round(float(cfg.get("kombi_dominance_days") or 3.0) * bpd)), 1)
    live3 = np.ones(n, dtype=np.int8)
    cur = int(raw_live[0]) if n else 1
    pend, cnt, side_run = cur, 0, 0
    for i in range(n):
        r = int(raw_live[i])
        a = int(accel[i])
        if a != 0:
            tgt = 2 if a > 0 else 0
            if r == tgt and cur != tgt:
                # EMA sieht den neuen Trend bereits -> Pivot überspringt die
                # Stabilitäts-Wartezeit (reine Beschleunigung).
                cur, pend, cnt, side_run = tgt, tgt, 0, 0
                live3[i] = cur
                continue
            if r == 1 and cur in (0, 2) and tgt != cur:
                # Gegen-Pivot während einer Trend-Dominanz-Überbrückung:
                # Überbrückung sofort beenden (Trend -> Seitwärts früher).
                cur, pend, cnt, side_run = 1, 1, 0, 0
                live3[i] = cur
                continue
        if r == cur:
            pend, cnt, side_run = cur, 0, 0
        elif r == 1 and cur in (0, 2):
            # Trend-Dominanz: Seitwärts erst nach dom Kerzen anzeigen –
            # kehrt der Trend vorher zurück, war es nur eine Pause im Trend.
            side_run += 1
            pend, cnt = cur, 0
            if side_run >= dom:
                cur, side_run = 1, 0
        else:
            side_run = 0
            if r == pend:
                cnt += 1
                if cnt >= stab:
                    cur, cnt = r, 0
            else:
                pend, cnt = r, 1
        live3[i] = cur

    # --- 4. Final-Sicht: zentrierte Labels + Trend-Dominanz + Mini-Phasen ---
    final3 = _dominance_merge(_hyst_labels(sn_final, thr, lo), dom)
    mp_days = float(cfg.get("min_phase_days") or 0.0)
    if mp_days <= 0:
        # auto: ~0.7% des Zeitraums (720d -> ~5d) – Ziel 5-15 Tage je Phase
        mp_days = min(max(n / bpd * 0.007, 1.0), 7.0)
    mp_bars = max(int(round(mp_days * bpd)), 2)
    final3 = _absorb_short(final3, mp_bars)

    # --- 5. Output (gleiche Struktur wie die anderen Detektoren) ---
    warm = min(max(span, k), n)
    live_dir = piv_dir.copy()                         # Pivot-Richtung (Frühwarnung)
    trendiness = np.clip(np.abs(sn_live) / (2.0 * thr), 0.0, 1.0)
    conf = np.clip(np.abs(sn_live) / thr, 0.15, 1.0)
    side_m = live3 == 1
    conf[side_m] = np.clip(1.0 - np.abs(sn_live[side_m]) / thr, 0.15, 1.0)
    probs = np.zeros((n, 3))
    z = np.clip(sn_live / (2.0 * thr), -1.4, 1.4)
    probs[:, 2] = np.clip(0.34 + 0.33 * z, 0.02, 0.96)
    probs[:, 0] = np.clip(0.34 - 0.33 * z, 0.02, 0.96)
    probs[:, 1] = np.clip(1.0 - probs[:, 0] - probs[:, 2], 0.02, 0.96)
    probs /= probs.sum(axis=1, keepdims=True)

    ema_crosses = []
    if f.get("ema_fast") is not None:
        ef = np.asarray(f["ema_fast"], dtype=float)
        em2 = np.asarray(f["ema_mid"], dtype=float)
        es2 = np.asarray(f["ema_slow"], dtype=float)
        for a, b, pair in ((ef, em2, "fast_mid"), (em2, es2, "mid_slow")):
            sgn = np.sign(a - b)
            flips = np.where((sgn[1:] != sgn[:-1]) & (sgn[1:] != 0)
                             & (sgn[:-1] != 0))[0] + 1
            for i in flips:
                ema_crosses.append({"i": int(i), "pair": pair,
                                    "dir": int(sgn[i])})
        ema_crosses.sort(key=lambda x: x["i"])

    return {"live_dir": live_dir, "live3": live3, "final3": final3,
            "trendiness": trendiness, "probs": probs, "conf": conf,
            "thr": thr_piv, "need": thr_piv.copy(), "retrace": retrace,
            "since_ext": since_ext, "leg_prog": trendiness,
            "rev_count": rev_count, "pivots": pivots,
            "ema_dir": (live3.astype(np.int8) - 1), "ema_crosses": ema_crosses,
            "warm": int(warm), "persist": persist,
            "min_phase_days": round(mp_days, 2)}
