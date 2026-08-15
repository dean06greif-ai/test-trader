"""Reaktive Regime-Erkennung über Umkehrpunkte (Engine v2, Detector "reactive").

Prinzip (bewusst simpel, KEINE Prediction – nur reaktive Bestätigung):
1. UMKEHRPUNKTE: ATR-gefilterter ZigZag. In einem Aufwärts-Lauf wird das
   höchste Hoch verfolgt; fällt der Kurs um mehr als die Umkehr-Schwelle
   (rev_atr_mult x ATR%) und bleibt das persist_candles Kerzen so
   (2-3 Kerzen Persistenz), ist der Hochpunkt als Umkehrpunkt BESTÄTIGT:
   Vom Hochpunkt aus beginnt die fallende Phase (rückwirkende Korrektur),
   erkannt wurde sie erst am Bestätigungs-Bar (Live-Label ohne Lookahead).
   Vom Tiefpunkt aus beginnt entsprechend die steigende Phase.
2. SEITWÄRTS: beginnt, wenn der Kurs "nicht mehr wirklich steigt oder fällt":
   wenig Fortschritt seit dem letzten Umkehrpunkt, lange kein neues Extrem
   (Stall) und schwacher ADX -> gradueller Trend-Score statt binärem Switch.
3. OUTPUT: Wahrscheinlichkeiten je Klasse (abwärts/seitwärts/aufwärts),
   Confidence, Live-Labels (nur Vergangenheitswissen) und pivot-korrigierte
   Final-Labels. `report` zeigt jede Selbstkorrektur ("da hab ich es gemerkt").
"""
import logging
from typing import Dict

import numpy as np

from services import regime_features as rf

logger = logging.getLogger(__name__)


def _ffill(x, first: int) -> np.ndarray:
    out = np.asarray(x, dtype=float).copy()
    n = len(out)
    if first >= n:
        return np.full(n, 1.0)
    out[:first] = out[first]
    mask = ~np.isfinite(out)
    if mask.any():
        idx = np.where(~mask, np.arange(n), 0)
        np.maximum.accumulate(idx, out=idx)
        out = out[idx]
    return out


def _leg_label(p0, p1, t1, hist, prev_opp, th_mean, leg_mult) -> int:
    """Label EINES Legs (p0 -> p1) – exakt die Kriterien der Final-Sicht:
    neues Extrem über/unter den letzten 3 Pivots gleicher Art (Pflicht),
    Dow-Struktur ODER klar große Bewegung, Mindest-Amplitude."""
    amp = abs(p1 - p0) / max(abs(p0), 1e-12) * 100.0
    th_leg = leg_mult * th_mean
    margin = max(0.3 * th_mean, 0.15 * amp)
    primary = secondary = None
    if t1 == "high":
        if hist:
            primary = p1 > max(hist[-3:]) * (1 + margin / 100.0)
        if prev_opp is not None:
            secondary = p0 > prev_opp * (1 + margin / 100.0)
    else:
        if hist:
            primary = p1 < min(hist[-3:]) * (1 - margin / 100.0)
        if prev_opp is not None:
            secondary = p0 < prev_opp * (1 - margin / 100.0)
    new_ext = primary if primary is not None else True
    structure = secondary if secondary is not None else True
    is_trend = (new_ext and (structure or amp >= 1.5 * th_leg)
                and amp >= 0.7 * th_leg)
    return (2 if p1 > p0 else 0) if is_trend else 1


def _merged_last(leg_labs, leg_spans, max_legs: int = 80,
                 budget_cap: int = 10 ** 9):
    """Letzte zusammengeführte Phase (Label, Länge in Bars) über die bisher
    ABGESCHLOSSENEN Legs – gleiche Merge-Logik wie die Final-Sicht (benachbarte
    gleiche Legs verschmelzen, Seitwärts-Pausen zwischen gleichgerichteten
    Trends werden dem Trend zugeschlagen)."""
    labs = list(leg_labs[-max_legs:])
    spans = list(leg_spans[-max_legs:])
    if not labs:
        return 1, 0
    for _ in range(6):
        ml, ms = [], []
        for lab, span in zip(labs, spans):
            if ml and ml[-1] == lab:
                ms[-1] = (ms[-1][0], span[1])
            else:
                ml.append(lab)
                ms.append(span)
        labs, spans = ml, ms
        changed = False
        for k in range(1, len(labs) - 1):
            if labs[k] == 1 and labs[k - 1] == labs[k + 1] and labs[k - 1] in (0, 2):
                la = spans[k - 1][1] - spans[k - 1][0]
                lb = spans[k][1] - spans[k][0]
                lc = spans[k + 1][1] - spans[k + 1][0]
                if lb < la + lc:
                    labs[k] = labs[k - 1]
                    changed = True
        if not changed:
            break
    # nach der letzten Absorption noch einmal benachbarte gleiche verschmelzen
    ml, ms = [], []
    for lab, span in zip(labs, spans):
        if ml and ml[-1] == lab:
            ms[-1] = (ms[-1][0], span[1])
        else:
            ml.append(lab)
            ms.append(span)
    last_lab = int(ml[-1])
    last_len = int(ms[-1][1] - ms[-1][0])
    if last_lab != 1 or len(ml) < 2:
        return last_lab, min(last_len, budget_cap)
    # Letzter Run ist Seitwärts: ist er kürzer als die Trendphase davor, ist
    # er (Stand jetzt) eine Pause IM Trend – die Final-Sicht würde ihn
    # absorbieren, sobald der Trend weitergeht. Rest-Budget zurückgeben.
    prev_lab = int(ml[-2])
    prev_len = int(ms[-2][1] - ms[-2][0])
    if prev_lab in (0, 2) and last_len < prev_len:
        return prev_lab, min(prev_len - last_len, budget_cap)
    return last_lab, last_len


def detect(f: Dict, cfg: Dict) -> Dict:
    det = str(cfg.get("detector") or "").lower()
    if det == "ema":
        return _detect_ema(f, cfg)
    if det == "kombi":
        from services.regime_kombi import detect_kombi
        return detect_kombi(f, cfg)
    return _detect_reactive(f, cfg)


def _detect_ema(f: Dict, cfg: Dict) -> Dict:
    """Detektor 'ema' (Nutzer-Idee): Regime = Steigung EINER EMA (z.B. 9
    Tage) relativ zur Tagesvolatilität, mit Hysterese.
    LIVE  = kausale Steigung (Fenster ema_regime_smooth_days, nur
            Vergangenheit) + Stabilitäts-Filter gegen Flackern.
    FINAL = ZENTRIERTE, lag-freie Steigung (darf Lookahead nutzen – das ist
            der Sinn der Final-Sicht) + Mindest-Phasendauer.
    Empirisch geprüft (scripts/ema_detector_proto.py, BTC+ETH 720d/1h und
    2000d/1d): Final-Segmente laufen fast nie netto gegen ihr Label
    (1-4 von ~47 statt 26/49 mit kausalem Final), Trend-Rallys werden klar
    als Trend gelabelt, Live=Final ~79-90%."""
    close = np.asarray(f["close"], dtype=float)
    n = len(close)
    bpd = max(float(cfg.get("bars_per_day") or 24.0), 1e-9)
    dvol = np.nan_to_num(np.asarray(f["daily_vol_pct"], dtype=float), nan=2.0)
    dvol = np.where(dvol <= 0, 2.0, dvol)
    days = float(cfg.get("ema_regime_days") or 9.0)
    span = max(int(round(days * bpd)), 2)
    ema = rf.ema(close, span)
    thr = max(float(cfg.get("ema_regime_thr") or 0.18), 0.01)
    lo = thr * 0.5                                   # Hysterese: erst unter
    k = max(int(round(float(cfg.get("ema_regime_smooth_days") or 1.0) * bpd)), 1)

    def _slope_n(shift_back: int, shift_fwd: int) -> np.ndarray:
        w = shift_back + shift_fwd
        sn = np.zeros(n)
        if n > w > 0:
            seg = (ema[w:] / np.maximum(ema[:-w], 1e-12) - 1.0) * 100.0 / (w / bpd)
            sn[shift_back:n - shift_fwd] = seg
            sn[n - shift_fwd:] = sn[n - shift_fwd - 1] if shift_fwd else 0.0
        return sn / dvol

    sn_live = _slope_n(k, 0)                         # kausal
    h = max(int(round(k / 2)), 1)
    sn_final = _slope_n(h, h) if n > 2 * h else sn_live.copy()
    if n > 2 * h:                                    # Rand: kausal auffüllen
        sn_final[n - h:] = sn_live[n - h:]

    def _labels(sn) -> np.ndarray:
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

    live3 = _labels(sn_live)
    # Kausaler Stabilitäts-Filter: ein Wechsel wird erst übernommen, wenn das
    # neue Label 'stab' Kerzen in Folge anliegt (Anti-Flicker, kostet nur an
    # den Wechseln etwas Lag).
    stab = max(int(round(float(cfg.get("ema_regime_persist_days") or 0.5) * bpd)), 1)
    if stab > 1:
        out = live3.copy()
        cur = int(live3[0])
        pend, cnt = cur, 0
        for i in range(n):
            v = int(live3[i])
            if v == cur:
                pend, cnt = cur, 0
            elif v == pend:
                cnt += 1
                if cnt >= stab:
                    cur, cnt = pend, 0
            else:
                pend, cnt = v, 1
            out[i] = cur
        live3 = out

    mp_days = float(cfg.get("min_phase_days") or 0.0)
    if mp_days <= 0:
        # auto: ~0.7% des Zeitraums (720d -> ~5d) – ruhige, tradebare Phasen
        mp_days = min(max(n / bpd * 0.007, 1.0), 7.0)
    mp_bars = max(int(round(mp_days * bpd)), 2)
    final3 = _absorb_short(_labels(sn_final), mp_bars)

    warm = min(span, n)
    live_dir = (live3.astype(np.int8) - 1)
    trendiness = np.clip(np.abs(sn_live) / (2.0 * thr), 0.0, 1.0)
    conf = np.clip(np.abs(sn_live) / thr, 0.15, 1.0)
    conf[live3 == 1] = np.clip(1.0 - np.abs(sn_live[live3 == 1]) / thr, 0.15, 1.0)
    # Frühwarnung: 'Gegenbewegung' = wie weit ist die Steigung vom Kipppunkt
    # entfernt (voll = Steigung hätte das Gegen-Vorzeichen der Schwelle).
    retrace = np.clip(thr - live_dir * sn_live, 0.0, None)
    retrace[live_dir == 0] = 0.0
    thr_arr = np.full(n, 2.0 * thr)
    zeros = np.zeros(n)
    probs = np.zeros((n, 3))
    z = np.clip(sn_live / (2.0 * thr), -1.4, 1.4)
    probs[:, 2] = np.clip(0.34 + 0.33 * z, 0.02, 0.96)
    probs[:, 0] = np.clip(0.34 - 0.33 * z, 0.02, 0.96)
    probs[:, 1] = np.clip(1.0 - probs[:, 0] - probs[:, 2], 0.02, 0.96)
    probs /= probs.sum(axis=1, keepdims=True)

    # EMA-Kreuzungen (Info fürs Frontend, wie beim reaktiven Detektor)
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
            "thr": thr_arr, "need": np.full(n, float(stab)),
            "retrace": retrace, "since_ext": zeros, "leg_prog": trendiness,
            "rev_count": np.zeros(n, dtype=int), "pivots": [],
            "ema_dir": live_dir.copy(), "ema_crosses": ema_crosses,
            "warm": int(warm), "persist": stab,
            "min_phase_days": round(mp_days, 2)}


def _detect_reactive(f: Dict, cfg: Dict) -> Dict:
    """Kern: Umkehrpunkte + Live-/Final-Labels + Wahrscheinlichkeiten."""
    high = np.asarray(f["high"], dtype=float)
    low = np.asarray(f["low"], dtype=float)
    close = np.asarray(f["close"], dtype=float)
    atr = np.asarray(f["atr_pct"], dtype=float)
    n = len(close)
    adx = (np.asarray(f["adx"], dtype=float) if f.get("adx") is not None
           else np.full(n, np.nan))

    mult = float(cfg.get("rev_atr_mult") or 3.0)
    leg_mult = float(cfg.get("side_leg_atr_mult") or 1.5)
    persist = min(max(int(cfg.get("persist_bars")
                          or cfg.get("persist_candles") or 3), 1), 20)
    bpd = float(cfg.get("bars_per_day") or 1.0)
    stall = max(int(cfg.get("stall_bars")
                    or max(int(round(bpd * 1.5)), persist * 6)), 2)
    lo_pct = float(cfg.get("rev_min_pct") or 0.0) or 0.05
    hi_pct = float(cfg.get("rev_max_pct") or 30.0)

    finite = np.isfinite(atr)
    warm = int(np.argmax(finite)) if finite.any() else n
    thr = np.clip(mult * _ffill(atr, warm), lo_pct, hi_pct)
    adx_n = np.clip((np.nan_to_num(adx, nan=20.0) - 20.0) / 8.0, 0.0, 1.0)

    # --- Volumen-Bestätigung: auffälliges Volumen zählt doppelt beim Bestätigen ---
    vol_hot = None
    if bool(cfg.get("use_volume_confirm", True)) and f.get("volume") is not None:
        vol = np.nan_to_num(np.asarray(f["volume"], dtype=float), nan=0.0)
        if n and float(vol.sum()) > 0:
            w = max(int(round(bpd * 2)), 10)
            cs = np.concatenate([[0.0], np.cumsum(vol)])
            idx = np.arange(n)
            lo_i = np.maximum(idx - w + 1, 0)
            vma = (cs[idx + 1] - cs[lo_i]) / np.maximum(idx - lo_i + 1, 1)
            vol_hot = vol > vma * float(cfg.get("volume_boost") or 1.5)

    # --- Multi-Timeframe-Konsens: grobere Sicht (größere Schwelle) als Richtungs-Anker ---
    htf_dir = np.zeros(n, dtype=np.int8)
    htf_since = np.zeros(n, dtype=np.int64)   # Kerzen ohne neues Extrem (grobe Sicht)
    use_mtf = bool(cfg.get("mtf_confirm", True))
    if use_mtf and n:
        thr2 = np.clip(thr * float(cfg.get("mtf_mult") or 2.5), lo_pct, hi_pct * 2)
        p2 = persist * 2
        d2, ehi, elo, cu, cd = 0, float(high[0]), float(low[0]), 0, 0
        e_i = 0
        for i in range(n):
            if high[i] >= ehi:
                ehi = float(high[i])
                if d2 >= 0:
                    e_i = i
            if low[i] <= elo:
                elo = float(low[i])
                if d2 <= 0:
                    e_i = i
            t2 = thr2[i]
            if d2 >= 0:
                cd = cd + 1 if (ehi - close[i]) / max(ehi, 1e-12) * 100.0 >= t2 else 0
            if d2 <= 0:
                cu = cu + 1 if (close[i] - elo) / max(elo, 1e-12) * 100.0 >= t2 else 0
            if d2 >= 0 and cd >= p2:
                d2, elo, ehi, cu, cd = -1, float(low[i]), float(high[i]), 0, 0
                e_i = i
            elif d2 <= 0 and cu >= p2:
                d2, ehi, elo, cu, cd = 1, float(high[i]), float(low[i]), 0, 0
                e_i = i
            htf_dir[i] = d2
            htf_since[i] = i - e_i

    live_dir = np.zeros(n, dtype=np.int8)     # -1 ab · 0 unbekannt · +1 auf
    leg_prog = np.zeros(n)                    # Fortschritt der Bewegung (%)
    since_ext = np.zeros(n, dtype=np.int64)   # Kerzen ohne neues Extrem
    retrace = np.zeros(n)                     # Gegenbewegung vom Extrem (%)
    rev_count = np.zeros(n, dtype=np.int64)   # laufende Bestätigungs-Kerzen
    since_conf = np.zeros(n, dtype=np.int64)  # Kerzen seit letzter Pivot-Bestätigung
    ground = np.full(n, np.nan)               # neuer Boden: HH/HL bzw. LL/LH
    last_hi_arr = np.full(n, np.nan)          # letzter bestätigter Hoch-Pivot
    last_lo_arr = np.full(n, np.nan)          # letzter bestätigter Tief-Pivot
    hi3_arr = np.full(n, np.nan)              # Maximum der letzten 3 Hoch-Pivots
    lo3_arr = np.full(n, np.nan)              # Minimum der letzten 3 Tief-Pivots
    leg_live = np.ones(n, dtype=np.int8)      # kausales Label des laufenden Legs
    mem_lab_arr = np.ones(n, dtype=np.int8)   # letzte gemergte Phase (kausal)
    mem_len_arr = np.zeros(n, dtype=np.int64)
    leg_start_arr = np.zeros(n, dtype=np.int64)
    pivots = []                               # Pivot gleicher Art (in x Schwellen)
    cumthr = np.concatenate([[0.0], np.cumsum(thr)])

    if n:
        d = 0
        ext_hi, ext_hi_i = float(high[0]), 0
        ext_lo, ext_lo_i = float(low[0]), 0
        pivot_price = float(close[0])
        last_hi_piv = last_lo_piv = None      # letzter Pivot je Art
        prev_hi_piv = prev_lo_piv = None      # vorletzter (für HL/LH-Vergleich)
        hi_hist, lo_hist = [], []             # letzte Pivot-Preise je Art
        last_conf_i = 0
        cnt_up = cnt_dn = 0
        leg_labs, leg_spans = [], []          # abgeschlossene Legs (kausal)
        live_budget = int(cfg.get("live_budget_cap_bars")
                          or max(stall * 2, persist * 6))
        leg_i0 = 0                            # Start-Index des laufenden Legs
        memL, memLen = 1, 0                   # letzte gemergte Phase
        for i in range(n):
            if high[i] >= ext_hi:
                ext_hi, ext_hi_i = float(high[i]), i
            if low[i] <= ext_lo:
                ext_lo, ext_lo_i = float(low[i]), i
            th = thr[i]
            drop = (ext_hi - close[i]) / max(ext_hi, 1e-12) * 100.0
            rise = (close[i] - ext_lo) / max(ext_lo, 1e-12) * 100.0
            if d >= 0:
                cnt_dn = (cnt_dn + (2 if vol_hot is not None and vol_hot[i] else 1)
                          if drop >= th else 0)
            if d <= 0:
                cnt_up = (cnt_up + (2 if vol_hot is not None and vol_hot[i] else 1)
                          if rise >= th else 0)
            if d >= 0 and cnt_dn >= persist:
                pivots.append({"type": "high", "i": int(ext_hi_i),
                               "price": float(ext_hi), "confirmed_i": i})
                # abgeschlossener (Aufwärts-)Leg: Label mit Final-Kriterien
                i1 = int(ext_hi_i)
                tm = (cumthr[i1 + 1] - cumthr[leg_i0]) / max(i1 + 1 - leg_i0, 1)
                leg_labs.append(_leg_label(pivot_price, float(ext_hi), "high",
                                           hi_hist, prev_lo_piv, tm, leg_mult))
                leg_spans.append((leg_i0, i1))
                memL, memLen = _merged_last(leg_labs, leg_spans,
                                            budget_cap=live_budget)
                leg_i0 = i1
                prev_hi_piv, last_hi_piv = last_hi_piv, float(ext_hi)
                hi_hist.append(float(ext_hi))
                d = -1
                pivot_price = ext_hi
                j0 = ext_hi_i
                k = int(np.argmin(low[j0:i + 1]))
                ext_lo, ext_lo_i = float(low[j0 + k]), j0 + k
                ext_hi, ext_hi_i = float(high[i]), i
                cnt_dn = cnt_up = 0
                last_conf_i = i
            elif d <= 0 and cnt_up >= persist:
                pivots.append({"type": "low", "i": int(ext_lo_i),
                               "price": float(ext_lo), "confirmed_i": i})
                # abgeschlossener (Abwärts-)Leg: Label mit Final-Kriterien
                i1 = int(ext_lo_i)
                tm = (cumthr[i1 + 1] - cumthr[leg_i0]) / max(i1 + 1 - leg_i0, 1)
                leg_labs.append(_leg_label(pivot_price, float(ext_lo), "low",
                                           lo_hist, prev_hi_piv, tm, leg_mult))
                leg_spans.append((leg_i0, i1))
                memL, memLen = _merged_last(leg_labs, leg_spans,
                                            budget_cap=live_budget)
                leg_i0 = i1
                prev_lo_piv, last_lo_piv = last_lo_piv, float(ext_lo)
                lo_hist.append(float(ext_lo))
                d = 1
                pivot_price = ext_lo
                j0 = ext_lo_i
                k = int(np.argmax(high[j0:i + 1]))
                ext_hi, ext_hi_i = float(high[j0 + k]), j0 + k
                ext_lo, ext_lo_i = float(low[i]), i
                cnt_up = cnt_dn = 0
                last_conf_i = i
            live_dir[i] = d
            since_conf[i] = i - last_conf_i
            if last_hi_piv is not None:
                last_hi_arr[i] = last_hi_piv
            if last_lo_piv is not None:
                last_lo_arr[i] = last_lo_piv
            if hi_hist:
                hi3_arr[i] = max(hi_hist[-3:])
            if lo_hist:
                lo3_arr[i] = min(lo_hist[-3:])
            if d > 0:
                leg_prog[i] = (ext_hi - pivot_price) / max(pivot_price, 1e-12) * 100.0
                since_ext[i] = i - ext_hi_i
                retrace[i] = max(drop, 0.0)
                rev_count[i] = cnt_dn
                # Dow-Struktur: Higher High UND Higher Low nötig
                g = []
                if last_hi_piv:
                    g.append((ext_hi - last_hi_piv) / last_hi_piv * 100.0)
                if prev_lo_piv:
                    g.append((pivot_price - prev_lo_piv) / prev_lo_piv * 100.0)
                if g:
                    ground[i] = min(g) / max(th, 1e-9)
            elif d < 0:
                leg_prog[i] = (pivot_price - ext_lo) / max(pivot_price, 1e-12) * 100.0
                since_ext[i] = i - ext_lo_i
                retrace[i] = max(rise, 0.0)
                rev_count[i] = cnt_up
                # Lower Low UND Lower High nötig
                g = []
                if last_lo_piv:
                    g.append((last_lo_piv - ext_lo) / last_lo_piv * 100.0)
                if prev_hi_piv:
                    g.append((prev_hi_piv - pivot_price) / prev_hi_piv * 100.0)
                if g:
                    ground[i] = min(g) / max(th, 1e-9)
            else:
                since_ext[i] = i
            # --- Kausales Label des LAUFENDEN Legs (gleiche Kriterien wie die
            # Final-Legs, nur ohne Zukunftswissen): der laufende Leg geht vom
            # letzten bestätigten Umkehrpunkt bis zum aktuellen Extrem. Er ist
            # ein Trend, wenn er neues Territorium gewinnt (über/unter den
            # letzten 3 Pivots gleicher Art), die Dow-Struktur stimmt (bzw.
            # die Bewegung klar groß ist) und der Fortschritt reicht. ---
            leg_thr_n = max(i + 1 - leg_i0, 1)
            th_mean = (cumthr[i + 1] - cumthr[leg_i0]) / leg_thr_n
            th_leg = leg_mult * th_mean
            mem_lab_arr[i] = memL
            mem_len_arr[i] = memLen
            leg_start_arr[i] = leg_i0
            if d != 0:
                amp = leg_prog[i]
                margin = max(0.3 * th_mean, 0.15 * amp)
                if d > 0:
                    primary = (ext_hi > max(hi_hist[-3:]) * (1 + margin / 100.0)
                               if hi_hist else None)
                    secondary = (pivot_price > prev_lo_piv * (1 + margin / 100.0)
                                 if prev_lo_piv else None)
                else:
                    primary = (ext_lo < min(lo_hist[-3:]) * (1 - margin / 100.0)
                               if lo_hist else None)
                    secondary = (pivot_price < prev_hi_piv * (1 - margin / 100.0)
                                 if prev_hi_piv else None)
                new_ext = primary if primary is not None else True
                structure = secondary if secondary is not None else True
                is_trend = (new_ext and (structure or amp >= 1.5 * th_leg)
                            and amp >= 0.7 * th_leg)
                # Früh-Erkennung: neues Territorium (über/unter den letzten
                # 3 Pivots) + intakte Struktur reicht schon ab 45% der
                # Amplituden-Schwelle – der Leg wächst ja noch.
                early = (primary is True and structure
                         and amp >= 0.45 * th_leg)
                leg_live[i] = (2 if d > 0 else 0) if (is_trend or early) else 1

    # --- gradueller Trend-Score: Fortschritt + Struktur (HH/HL bzw. LL/LH)
    #     + Frische + ADX ---
    need = np.maximum(leg_mult * thr, 1e-9)
    prog_n = np.clip(leg_prog / need, 0.0, 1.0)
    has_ground = np.isfinite(ground)
    g = np.nan_to_num(ground, nan=0.0)
    struct_n = np.where(has_ground, np.clip(0.5 + g, 0.0, 1.0), 0.5)
    fresh = np.exp(-since_ext / float(stall))
    trendiness = 0.30 * prog_n + 0.35 * struct_n + 0.20 * fresh + 0.15 * adx_n
    # Hartes Prinzip: ohne neuen Boden (kein HH/HL bzw. LL/LH) ist es kein
    # Trend – der Score wird gedeckelt, egal wie groß die Pendel-Bewegung ist.
    cap = 0.30 + 0.60 * np.clip(g, 0.0, 1.0)
    trendiness = np.where(has_ground, np.minimum(trendiness, cap), trendiness)
    # Multi-Timeframe-Konsens: Zustimmung der groben Sicht stärkt den Score,
    # Widerspruch dämpft ihn.
    if use_mtf:
        agree = (htf_dir == live_dir) & (live_dir != 0)
        oppose = (htf_dir == -live_dir) & (live_dir != 0) & (htf_dir != 0)
        trendiness = np.clip(trendiness + 0.08 * agree - 0.12 * oppose, 0.0, 1.0)

    # --- Live-3-Zustand (0 ab · 1 seitwärts · 2 auf) ---
    # = kausales Label des LAUFENDEN Legs (identische Kriterien wie die
    # Final-Sicht, nur ohne Zukunftswissen) + Pullback-Gedächtnis: ein
    # Seitwärts-Leg direkt nach einem Trend-Leg behält die Trendrichtung,
    # solange die Dow-Struktur nicht bricht (die Final-Sicht verschmilzt
    # genau diese Pullbacks rückwirkend in den Trend).
    live3 = leg_live.copy()
    # Pullback-Gedächtnis: die Final-Sicht schlägt Seitwärts-Legs zwischen
    # gleichgerichteten Trend-Legs dem Trend zu. Kausal: solange die letzte
    # gemergte Phase ein Trend ist, die Dow-Struktur nicht bricht (kein Close
    # unter dem letzten Higher-Low bzw. über dem letzten Lower-High) und der
    # laufende Seitwärts-Leg kürzer ist als diese Phase, gilt weiter der Trend.
    dead = False
    prev_leg0 = -1
    for i in range(n):
        if leg_start_arr[i] != prev_leg0:
            prev_leg0 = int(leg_start_arr[i])
            dead = False
        if leg_live[i] != 1:
            continue
        L = int(mem_lab_arr[i])
        if L == 1 or dead:
            continue
        if (i - leg_start_arr[i]) >= mem_len_arr[i]:
            continue
        # Gegen-Leg mit vollem Trend-Fortschritt: die alte Phase ist nicht
        # mehr haltbar, auch wenn das alte Higher-Low/Lower-High noch steht
        # (sonst überlebt das Gedächtnis ganze Crashs).
        if ((L == 2 and live_dir[i] < 0) or (L == 0 and live_dir[i] > 0)) \
                and leg_prog[i] >= 1.5 * need[i]:
            dead = True
            continue
        m3 = 0.3 * thr[i] / 100.0
        ll, lh = last_lo_arr[i], last_hi_arr[i]
        broken = ((L == 2 and bool(np.isfinite(ll)) and close[i] < ll * (1 - m3))
                  or (L == 0 and bool(np.isfinite(lh)) and close[i] > lh * (1 + m3)))
        if broken:
            dead = True
            continue
        live3[i] = L

    # Kausaler Drift-Detektor: langsame, stetige Trends liefern oft keinen
    # Leg-/Ausbruchs-Beweis (Beschwerde: "zäher Abwärtstrend wird live als
    # Seitwärts erkannt"). Deshalb wird ab dem Beginn einer Live-Seitwärts-
    # Phase mitgemessen, ob die Netto-Bewegung pro Tag klar über dem
    # Seitwärts-Rauschen liegt – dann meldet die Live-Sicht den Trend.
    # Spiegelt die Drift-Reklassifikation der Final-Sicht ohne Zukunftswissen.
    dvol_l = np.nan_to_num(np.asarray(f.get("daily_vol_pct"), dtype=float),
                           nan=2.0)
    side_max_l = float(cfg.get("validate_side_max_pct_per_day") or 0.35)
    vmult_l = float(cfg.get("validate_vol_tol_mult") or 1.5)
    side_s = -1
    drift_dir = 0
    for i in range(n):
        if live3[i] != 1:
            side_s, drift_dir = -1, 0
            continue
        if side_s < 0:
            side_s, drift_dir = i, 0
        days = (i - side_s) / bpd
        if days < 2.0:
            continue
        net = (close[i] / max(close[side_s], 1e-12) - 1.0) * 100.0
        v = float(dvol_l[i]) or 2.0
        per_day = abs(net) / max(days, 0.5)
        tol = max(1.0, vmult_l * v * (days ** 0.5) * 0.75)
        if drift_dir == 0:
            if per_day >= side_max_l and abs(net) >= tol:
                drift_dir = 1 if net > 0 else -1
        else:
            keep_dir = 1 if net > 0 else -1
            if per_day < 0.6 * side_max_l or keep_dir != drift_dir:
                drift_dir = 0
        if drift_dir != 0:
            live3[i] = 2 if drift_dir > 0 else 0

    # --- EMA-Bestätigung (empirisch getestet: scripts/ema_testbed.py) ---
    # Steigungs-geprüfte EMA-Trendlinien überstimmen Live-SEITWÄRTS-Phasen:
    # mittlere EMA auf der richtigen Seite der langsamen (Kreuzungs-Prinzip),
    # Kurs auf der richtigen Seite der mittleren, Steigung der mittleren EMA
    # klar über der Tagesvola UND schnelle EMA in dieselbe Richtung (Frische-
    # Check gegen nachlaufende EMAs am Trend-Ende). Bestehende Trend-Labels
    # werden NIE überstimmt – nur Seitwärts wird zum Trend hochgestuft.
    ema_dir = np.zeros(n, dtype=np.int8)
    if bool(cfg.get("use_ema_confirm", True)) and f.get("ema_mid") is not None:
        em = np.asarray(f["ema_mid"], dtype=float)
        es = np.asarray(f["ema_slow"], dtype=float)
        sm = (np.asarray(f["ema_slope_mid"], dtype=float)
              / np.maximum(dvol_l, 1e-9))
        sf = (np.asarray(f["ema_slope_fast"], dtype=float)
              / np.maximum(dvol_l, 1e-9))
        thr_e = float(cfg.get("ema_slope_thr") or 0.2)
        e_up = (em > es) & (close > em) & (sm >= thr_e) & (sf > 0)
        e_dn = (em < es) & (close < em) & (sm <= -thr_e) & (sf < 0)
        # Neues-Terrain-Gate: die mittlere EMA muss über/unter ihrem eigenen
        # Rolling-Extrem der letzten ~2 Mittel-Perioden liegen. In einer Range
        # pendelt die EMA im selben Band (Signal aus), in echten Trends gewinnt
        # sie laufend neues Terrain (Signal an) – verhindert, dass ruhige
        # Mean-Reversion-Märkte als Trend hochgestuft werden.
        import pandas as pd
        w_g = max(int(cfg.get("ema_mid_bars") or 21) * 2, 4)
        s_em = pd.Series(em)
        g_hi = s_em.shift(1).rolling(w_g, min_periods=2).max().to_numpy()
        g_lo = s_em.shift(1).rolling(w_g, min_periods=2).min().to_numpy()
        e_up &= ~np.isfinite(g_hi) | (em >= g_hi)
        e_dn &= ~np.isfinite(g_lo) | (em <= g_lo)
        ema_dir = np.where(e_up, 1, np.where(e_dn, -1, 0)).astype(np.int8)
        k_e = max(int(cfg.get("ema_persist_bars") or 1), 1)
        if k_e > 1:
            stable = ema_dir.copy()
            run = 0
            for i in range(n):
                run = (run + 1 if i and ema_dir[i] == ema_dir[i - 1]
                       and ema_dir[i] != 0 else 0)
                if ema_dir[i] != 0 and run < k_e:
                    stable[i] = 0
            ema_dir = stable
        m_e = (live3 == 1) & (ema_dir != 0)
        live3[m_e] = np.where(ema_dir[m_e] > 0, 2, 0).astype(live3.dtype)

    # EMA-Kreuzungen (schnell x mittel, mittel x langsam) fürs Frontend /
    # den Report – reine Info, ohne Einfluss auf die Labels.
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

    # Kausaler Mini-Phasen-Filter: ein Label-Wechsel wird erst angezeigt,
    # wenn er live_min_show_bars Kerzen Bestand hat (die Final-Sicht löscht
    # Mini-Phasen rückwirkend – live geht das nur mit kurzem Warten).
    show = cfg.get("live_min_show_bars")
    show = int(show) if show is not None else 0
    if show > 0:
        held = live3.copy()
        curh = int(live3[0])
        pend, cnt = curh, 0
        for i in range(n):
            lab = int(live3[i])
            if lab == curh:
                pend, cnt = curh, 0
            elif lab == pend:
                cnt += 1
                if cnt >= show:
                    curh, cnt = lab, 0
            else:
                pend, cnt = lab, 1
            held[i] = curh
        live3 = held

    # --- Wahrscheinlichkeiten je Klasse (gradueller Output) ---
    p_leg = 0.20 + 0.75 * trendiness
    p_opp = 0.05 + 0.45 * np.clip(retrace / np.maximum(thr, 1e-9), 0.0, 1.0)
    p_side = 0.15 + 0.85 * (1.0 - trendiness)
    pos, neg = live_dir > 0, live_dir < 0
    up = np.where(pos, p_leg, np.where(neg, p_opp, 0.2))
    dn = np.where(neg, p_leg, np.where(pos, p_opp, 0.2))
    sd = np.where(live_dir == 0, 0.6, p_side)
    tot = np.maximum(up + dn + sd, 1e-9)
    probs = np.stack([dn / tot, sd / tot, up / tot], axis=1)
    conf = probs.max(axis=1)

    # --- Final-Labels: pivot-korrigiert (Phase beginnt am Hoch-/Tiefpunkt) ---
    # Ein Leg (Pivot -> Pivot) ist nur dann ein Trend, wenn die Bewegung groß
    # genug ist UND neuen Boden gewinnt (Dow-Struktur: Higher High + Higher Low
    # bzw. Lower Low + Lower High gegenüber den vorherigen Pivots). Pendeln
    # zwischen denselben Marken (Range) wird dadurch Seitwärts – exakt das
    # "Umschlagpunkt"-Prinzip.
    final3 = live3.copy()
    bounds = [(0, float(close[0]) if n else 0.0, None)] + \
             [(p["i"], p["price"], p["type"]) for p in pivots]
    leg_lab, leg_span = [], []
    last_piv = {"high": None, "low": None}
    prev_piv = {"high": None, "low": None}
    hist = {"high": [], "low": []}
    for k in range(len(bounds) - 1):
        i0, p0, _t0 = bounds[k]
        i1, p1, t1 = bounds[k + 1]
        if i1 <= i0:
            continue
        amp = abs(p1 - p0) / max(abs(p0), 1e-12) * 100.0
        th_leg = float(np.mean(need[i0:i1 + 1]))
        # Marge skaliert mit Schwunggröße: ein "neues Extrem" muss im Verhältnis
        # zum Pendel-Ausschlag deutlich sein (Range-Rauschen zählt nicht).
        margin = max(0.3 * float(np.mean(thr[i0:i1 + 1])), 0.15 * amp)
        primary = None    # neues Extrem (HH bzw. LL) – Pflicht
        secondary = None  # Gegenseite (HL bzw. LH) – bei großer Bewegung verzichtbar
        if t1 == "high":   # Aufwärts-Leg: neues Hoch über den letzten Hochs
            if hist["high"]:
                primary = p1 > max(hist["high"][-3:]) * (1 + margin / 100.0)
            if prev_piv["low"] is not None:
                secondary = p0 > prev_piv["low"] * (1 + margin / 100.0)
        elif t1 == "low":  # Abwärts-Leg: neues Tief unter den letzten Tiefs
            if hist["low"]:
                primary = p1 < min(hist["low"][-3:]) * (1 - margin / 100.0)
            if prev_piv["high"] is not None:
                secondary = p0 < prev_piv["high"] * (1 - margin / 100.0)
        if t1:
            prev_piv[t1], last_piv[t1] = last_piv[t1], p1
            hist[t1].append(p1)
        # Trend: neues Extrem ist Pflicht; volle Dow-Struktur ODER klar große
        # Bewegung; Mindest-Amplitude weich (langsame stetige Trends zählen).
        new_ext = primary if primary is not None else True
        structure = secondary if secondary is not None else True
        is_trend = (new_ext and (structure or amp >= 1.5 * th_leg)
                    and amp >= 0.7 * th_leg)
        leg_lab.append((2 if p1 > p0 else 0) if is_trend else 1)
        leg_span.append((i0, i1))
    # Pullbacks glätten (iterativ): benachbarte gleiche Legs zusammenfassen,
    # dann Seitwärts-Legs zwischen zwei gleichgerichteten Trend-Legs dem
    # übergeordneten Trend zuschlagen.
    for _ in range(6):
        merged_lab, merged_span = [], []
        for lab, span in zip(leg_lab, leg_span):
            if merged_lab and merged_lab[-1] == lab:
                merged_span[-1] = (merged_span[-1][0], span[1])
            else:
                merged_lab.append(lab)
                merged_span.append(span)
        leg_lab, leg_span = merged_lab, merged_span
        changed = False
        for k in range(1, len(leg_lab) - 1):
            a, b, c = leg_lab[k - 1], leg_lab[k], leg_lab[k + 1]
            if b == 1 and a == c and a in (0, 2):
                la = leg_span[k - 1][1] - leg_span[k - 1][0]
                lb = leg_span[k][1] - leg_span[k][0]
                lc = leg_span[k + 1][1] - leg_span[k + 1][0]
                if lb < la + lc:
                    leg_lab[k] = a      # Seitwärts-Pause im Trend
                    changed = True
        if not changed:
            break
    for lab, (i0, i1) in zip(leg_lab, leg_span):
        final3[i0:i1] = lab

    # --- Mini-Phasen-Filter: zu kurze Abschnitte dem längeren Nachbarn
    #     zuschlagen (weniger, dafür handelbare Regime) ---
    mp_days = float(cfg.get("min_phase_days") or 0.0)
    if mp_days <= 0:
        mp_days = min(max(n / max(bpd, 1e-9) * 0.004, 0.5), 4.0)  # auto ~0.4% Zeitraum
    mp_bars = max(int(round(mp_days * bpd)), persist * 2)
    final3 = _absorb_short(final3, mp_bars)

    # Drift-Reklassifikation: Seitwärts-Abschnitte mit klarer, stetiger
    # Netto-Richtung sind in Wahrheit LANGSAME Trends (häufige Nutzer-
    # Beobachtung: zäher Abwärtstrend wird als Seitwärts angezeigt).
    # Kriterien bewusst konservativ: deutliche Bewegung pro Tag UND
    # Gesamtbewegung klar über dem Vola-Rauschen des Zeitraums.
    dvol_f = np.nan_to_num(np.asarray(f.get("daily_vol_pct"), dtype=float),
                           nan=2.0)
    side_max = float(cfg.get("validate_side_max_pct_per_day") or 0.35)
    vmult = float(cfg.get("validate_vol_tol_mult") or 1.5)
    s0 = 0
    for i in range(1, n + 1):
        if i < n and final3[i] == final3[s0]:
            continue
        if final3[s0] == 1 and i - s0 >= max(mp_bars, 2):
            days = (i - s0) / bpd
            net = (close[i - 1] / max(close[s0], 1e-12) - 1.0) * 100.0
            v = float(np.mean(dvol_f[s0:i])) or 2.0
            tol = max(1.0, vmult * v * (days ** 0.5) * 0.75)
            if abs(net) / max(days, 0.5) >= side_max and abs(net) >= tol:
                final3[s0:i] = 2 if net > 0 else 0
        s0 = i

    return {"live_dir": live_dir, "live3": live3, "final3": final3,
            "trendiness": trendiness, "probs": probs, "conf": conf,
            "thr": thr, "need": need, "retrace": retrace,
            "since_ext": since_ext, "leg_prog": leg_prog,
            "rev_count": rev_count, "pivots": pivots,
            "ema_dir": ema_dir, "ema_crosses": ema_crosses,
            "warm": min(warm, n), "persist": persist,
            "min_phase_days": round(mp_days, 2)}


def _absorb_short(lab: np.ndarray, min_len: int) -> np.ndarray:
    """Läufe kürzer als min_len bekommen das Label des längeren Nachbarn."""
    out = lab.copy()
    for _ in range(8):
        runs, s = [], 0
        for i in range(1, len(out) + 1):
            if i == len(out) or out[i] != out[s]:
                runs.append((s, i))
                s = i
        changed = False
        for k, (a, b) in enumerate(runs):
            if b - a >= min_len or len(runs) == 1:
                continue
            left = runs[k - 1] if k > 0 else None
            right = runs[k + 1] if k < len(runs) - 1 else None
            ll = (left[1] - left[0]) if left else -1
            lr = (right[1] - right[0]) if right else -1
            out[a:b] = out[left[0]] if ll >= lr else out[right[0]]
            changed = True
        if not changed:
            break
    return out


# ---------------------------------------------------------------- Mapping
def _strength_axis(f: Dict, cfg: Dict, dir3, causal: bool) -> np.ndarray:
    """Stark/Leicht je zusammenhängender Trend-PHASE statt je Kerze:
    Netto-Bewegung pro Tag im Verhältnis zur Tagesvolatilität. Das
    verhindert das Stark/Leicht-Flackern (zerhackte Phasen) und vertauschte
    Labels innerhalb einer Phase.
    causal=True (Live-Sicht): nur Wissen bis zur jeweiligen Kerze – die
    Stärke wird vom Phasenstart bis 'jetzt' gemessen, mit Einrast-Hysterese
    gegen Hin- und Herspringen. Sonst wird das ganze Segment einmal bewertet."""
    d3 = np.asarray(dir3, dtype=int)
    n = len(d3)
    close = np.asarray(f["close"], dtype=float)
    dvol = np.nan_to_num(np.asarray(f.get("daily_vol_pct"), dtype=float), nan=2.0)
    dvol = np.where(dvol <= 0, 2.0, dvol)
    bpd = max(float(cfg.get("bars_per_day") or 24.0), 1e-9)
    enter = float(cfg.get("strong_speed_ratio") or 0.35)
    keep = 0.75 * enter
    sub = np.zeros(n, dtype=int)
    s = 0
    for i in range(1, n + 1):
        if i < n and d3[i] == d3[s]:
            continue
        if d3[s] in (0, 2) and i - s >= 2:
            if causal:
                st = 0
                vsum = 0.0
                for j in range(s, i):
                    vsum += float(dvol[j])
                    v = vsum / (j - s + 1)
                    days = max((j - s + 1) / bpd, 0.5)
                    net = abs(close[j] / max(close[s], 1e-12) - 1.0) * 100.0
                    speed = net / days / max(v, 1e-9)
                    # Signifikanz: Bewegung muss über dem Vola-Rauschen des
                    # bisherigen Segments liegen (sonst adelt Zufallsrauschen
                    # junge Segmente zum "starken" Trend).
                    sig = net >= v * (days ** 0.5)
                    if st == 0 and speed >= enter and sig:
                        st = 1
                    elif st == 1 and (speed < keep or not sig):
                        st = 0
                    sub[j] = st
            else:
                v = float(np.mean(dvol[s:i]))
                days = max((i - s) / bpd, 0.5)
                net = abs(close[i - 1] / max(close[s], 1e-12) - 1.0) * 100.0
                speed = net / days / max(v, 1e-9)
                sig = net >= v * (days ** 0.5)
                sub[s:i] = 1 if (speed >= enter and sig) else 0
        s = i
    return sub


def _sub_axis(det: Dict, f: Dict, cfg: Dict, dir3, mode: int,
              causal: bool = False):
    n = len(dir3)
    if mode == 5:
        return _strength_axis(f, cfg, dir3, causal)
    if mode == 9:
        vz = np.nan_to_num(np.asarray(f.get("vol_z"), dtype=float), nan=0.0)
        sub = np.ones(n, dtype=int)
        sub[vz <= float(cfg.get("vol_low_z", -0.55))] = 0
        sub[vz >= float(cfg.get("vol_high_z", 0.65))] = 2
        return sub
    return np.zeros(n, dtype=int)


def _ids(dir3, sub, mode: int) -> np.ndarray:
    t = np.asarray(dir3, dtype=int)
    s = np.asarray(sub, dtype=int)
    if mode == 3:
        return t.copy()
    if mode == 5:
        return np.where(t == 1, 2, 2 + (t - 1) * (1 + s))
    return t * 3 + s


def classify(f: Dict, cfg: Dict, conf_min: float = None,
             min_hold_bars: int = None):
    """(ids, confidence, detail) – Live-Labels ohne Lookahead.
    min_hold_bars (optional, z.B. Trading-Konfiguration): ein Label-Wechsel
    wird erst akzeptiert, wenn der letzte akzeptierte Wechsel mindestens so
    viele Kerzen zurückliegt (kausale Mindesthaltedauer fürs Umschalten)."""
    from services import regime_engine as eng
    det = detect(f, cfg)
    mode = eng.norm_mode(cfg.get("regime_mode", eng.DEFAULT_REGIME_MODE))
    det["mode"] = mode
    sub = _sub_axis(det, f, cfg, det["live3"], mode, causal=True)
    ids = _ids(det["live3"], sub, mode)
    hold = int(min_hold_bars or 0)
    if hold > 1:
        warm = int(det["warm"])
        cur = None
        last_change = -hold
        for i in range(warm, len(ids)):
            if cur is None:
                cur, last_change = int(ids[i]), i
            elif int(ids[i]) != cur:
                if i - last_change >= hold:
                    cur, last_change = int(ids[i]), i
                else:
                    ids[i] = cur
    ids[:det["warm"]] = -1
    conf = det["conf"].copy()
    conf[:det["warm"]] = 0.0
    return ids, conf, det


def final_ids_from(det: Dict, f: Dict, cfg: Dict) -> np.ndarray:
    sub = _sub_axis(det, f, cfg, det["final3"], det["mode"], causal=False)
    ids = _ids(det["final3"], sub, det["mode"])
    ids[:det["warm"]] = -1
    return ids


def _ts(candles, i):
    try:
        return int(candles[i]["timestamp"])
    except (TypeError, KeyError, IndexError):
        try:
            return int(candles.ts[i])
        except Exception:  # noqa: BLE001
            return None


def report(det: Dict, cfg: Dict, candles) -> Dict:
    """Selbstkorrektur-Protokoll: wann wurde jeder Umkehrpunkt live erkannt."""
    bpd = max(float(cfg.get("bars_per_day") or 1.0), 1e-9)
    delays = [(p["confirmed_i"] - p["i"]) / bpd for p in det["pivots"]]
    items = [{"type": p["type"], "price": p["price"],
              "pivot_ts": _ts(candles, p["i"]),
              "detected_ts": _ts(candles, p["confirmed_i"]),
              "delay_days": round((p["confirmed_i"] - p["i"]) / bpd, 2)}
             for p in det["pivots"][-30:]]
    return {"pivots": len(det["pivots"]),
            "avg_delay_days": round(sum(delays) / len(delays), 2) if delays else None,
            "max_delay_days": round(max(delays), 2) if delays else None,
            "min_phase_days": det.get("min_phase_days"),
            "corrections": items,
            "ema_crosses": [{"ts": _ts(candles, c["i"]), "pair": c["pair"],
                             "dir": c["dir"]}
                            for c in (det.get("ema_crosses") or [])[-120:]],
            "note": "Phasen beginnen am bestätigten Hoch-/Tiefpunkt (rückwirkend "
                    "korrigiert); detected_ts = wann die Umkehr live erkannt wurde."}


def full_payload(f: Dict, cfg: Dict, candles) -> Dict:
    ids_live, _conf, det = classify(f, cfg)
    ids_final = final_ids_from(det, f, cfg)
    return {"live_labels": [None if r < 0 else int(r) for r in ids_live],
            "final_labels": [None if r < 0 else int(r) for r in ids_final],
            "report": report(det, cfg, candles)}


# ---------------------------------------------------------------- Frühwarnung
def early_warning(det: Dict, cfg: Dict, i: int, mode: int) -> Dict:
    """Reaktive Frühwarnung: Abstand zur Umkehr-Schwelle statt Prediction."""
    from services import regime_engine as eng
    n = len(det["live3"])
    if i < det["warm"] or i >= n:
        return {"active": False}
    d = int(det["live_dir"][i])
    th = float(det["thr"][i])
    if d == 0 or not np.isfinite(th) or th <= 0:
        return {"active": False}
    retr = float(det["retrace"][i])
    frac = min(retr / th, 1.5)
    persist = det["persist"]
    cnt = int(det["rev_count"][i])
    tn = float(det["trendiness"][i])
    bpd = max(float(cfg.get("bars_per_day") or 1.0), 1e-9)

    if frac >= 1.0 or cnt > 0:
        next_t = 0 if d > 0 else 2
        prob = min(55.0 + 40.0 * (cnt / max(persist, 1)), 99.0)
        why = (f"Gegenbewegung {retr:.2f}% ≥ Umkehr-Schwelle {th:.2f}% – "
               f"Bestätigung {cnt}/{persist} Kerzen")
    elif int(det["live3"][i]) != 1 and tn < 0.5:
        next_t = 1
        prob = min((0.5 - tn) / 0.5 * 80.0 + 15.0, 95.0)
        why = f"Kaum noch Fortschritt (Trend-Score {tn:.2f}) – Seitwärts wird wahrscheinlicher"
    else:
        next_t = 0 if d > 0 else 2
        prob = max(frac, 0.0) * 45.0
        why = (f"Gegenbewegung {retr:.2f}% von {th:.2f}% Umkehr-Schwelle "
               f"({frac * 100:.0f}%)")

    eta_days = None
    w = max(persist * 3, 5)
    lo = max(i - w + 1, det["warm"])
    seg = det["retrace"][lo:i + 1]
    if len(seg) >= 3:
        slope = (float(seg[-1]) - float(seg[0])) / max(len(seg) - 1, 1) * bpd
        if slope > 1e-6 and retr < th:
            eta_days = round((th - retr) / slope, 1)

    rid_next = eng.regime_id(next_t, 0, mode)
    prob = round(float(min(prob, 99.0)), 1)
    return {"active": bool(prob >= 25.0), "next_regime": int(rid_next),
            "next_label": eng.regime_label(rid_next, mode),
            "next_nnfx": eng.nnfx_regime(rid_next, mode),
            "probability_pct": prob, "eta_days": eta_days,
            "pending": bool(cnt > 0), "pending_bars": cnt,
            "pending_days": round(cnt / bpd, 2),
            "confirm_days": round(persist / bpd, 2),
            "retrace_pct": round(retr, 3), "threshold_pct": round(th, 3),
            "trend_score": round(tn, 3),
            "reason": f"{why} · Wahrscheinlichkeit {prob:.0f}%"}
