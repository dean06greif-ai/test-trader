"""EMA-Regime-Testbett: misst empirisch, ob und wie EMA-Trendlinien
(9/50/200 & Varianten) die kausale LIVE-Erkennung verbessern.

Metriken je Variante (gegen die pivot-korrigierten FINAL-Labels):
- agree: Richtungs-Übereinstimmung Live vs. Final (alle Bars nach Warmup)
- hold:  dito, nur letzte 25% (simulierter Holdout)
- lag:   Ø Tage bis die Live-Sicht die Richtung eines Final-Trendsegments trifft
- miss:  Final-Trendsegmente, die live nie erkannt wurden
- segs:  Anzahl Live-Segmente (Flicker-Maß; Final als Referenz)
- contra: Live-Trend-Segmente, deren Netto-Kursbewegung dagegen lief

Aufruf: /root/.venv/bin/python scripts/ema_testbed.py
"""
import os
import pickle
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

import numpy as np  # noqa: E402

from services import regime_engine as eng  # noqa: E402
from services import regime_features as rf  # noqa: E402
from services import regime_reactive as rx  # noqa: E402

DATA = os.path.join(os.path.dirname(__file__), "_testbed_candles.pkl")


def ema_arrays(close, bpd, days_set, slope_win_days):
    """EMAs + Steigungen (%/Tag) für ein Perioden-Set (Tage)."""
    out = {}
    for name, dys in zip(("f", "m", "s"), days_set):
        span = max(int(round(dys * bpd)), 2)
        e = rf.ema(close, span)
        k = max(int(round((slope_win_days or max(dys * 0.25, 1.0)) * bpd)), 1)
        sl = np.full(len(e), 0.0)
        sl[k:] = (e[k:] / np.maximum(e[:-k], 1e-12) - 1.0) * 100.0 / (k / bpd)
        out[name] = e
        out["sl_" + name] = sl
    return out


def ema_dir(close, E, dvol, eps=0.05, need_fast=False):
    """Kausale EMA-Regime-Richtung je Bar: +1/0/-1.
    up: close>EMA_s UND EMA_m>EMA_s UND EMA_s-Steigung nicht fallend
    (optional zusätzlich EMA_f>EMA_m = volle Stack-Ordnung)."""
    sn = E["sl_s"] / np.maximum(dvol, 1e-9)
    up = (close > E["s"]) & (E["m"] > E["s"]) & (sn >= -eps)
    dn = (close < E["s"]) & (E["m"] < E["s"]) & (sn <= eps)
    if need_fast:
        up &= E["f"] > E["m"]
        dn &= E["f"] < E["m"]
    return np.where(up, 1, np.where(dn, -1, 0)).astype(np.int8)


def apply_variant(live, ed, mode):
    """EMA-Überstimmung auf die Live-Labels anwenden (kausal, bar-weise)."""
    out = live.copy()
    if mode == "side2trend":            # Seitwärts -> EMA-Trend
        m = (live == 1) & (ed != 0)
        out[m] = np.where(ed[m] > 0, 2, 0)
    elif mode == "side2trend+veto":     # zusätzlich: Live-Trend gegen EMA -> Seitwärts
        m = (live == 1) & (ed != 0)
        out[m] = np.where(ed[m] > 0, 2, 0)
        v = ((live == 2) & (ed < 0)) | ((live == 0) & (ed > 0))
        out[v] = 1
    elif mode == "veto":
        v = ((live == 2) & (ed < 0)) | ((live == 0) & (ed > 0))
        out[v] = 1
    return out


def metrics(live, fin, close, warm, bpd):
    n = len(live)
    m = slice(warm, n)
    agree = float(np.mean(live[m] == fin[m])) * 100
    h0 = warm + int((n - warm) * 0.75)
    hold = float(np.mean(live[h0:] == fin[h0:])) * 100
    tm = (fin[m] != 1)
    trend_hit = float(np.mean(live[m][tm] == fin[m][tm])) * 100 if tm.any() else 0

    def nseg(a):
        return int(np.sum(a[1:] != a[:-1]) + 1)

    # Lag & Misses je Final-Trendsegment
    lags, miss = [], 0
    s = warm
    for i in range(warm + 1, n + 1):
        if i == n or fin[i] != fin[s]:
            lab = int(fin[s])
            if lab in (0, 2) and (i - s) >= bpd:  # >= 1 Tag
                hit = np.where(live[s:i] == lab)[0]
                if len(hit):
                    lags.append(hit[0] / bpd)
                else:
                    miss += 1
            s = i
    # Widersprüche: Live-Trend-Segment, Kurs lief netto dagegen
    bad = tot = 0
    s = warm
    for i in range(warm + 1, n + 1):
        if i == n or live[i] != live[s]:
            lab = int(live[s])
            if lab in (0, 2) and i - s > 4:
                net = close[i - 1] / close[s] - 1
                tot += 1
                if (lab == 2 and net < -0.01) or (lab == 0 and net > 0.01):
                    bad += 1
            s = i
    return {"agree": agree, "hold": hold, "hit": trend_hit,
            "lag": float(np.mean(lags)) if lags else None, "miss": miss,
            "segs": nseg(live[m]), "fsegs": nseg(fin[m]),
            "contra": f"{bad}/{tot}"}


def ema_dir_slope(close, E, dvol, thr=0.15, need_fast_sign=True):
    """Steigungs-gegatete EMA-Richtung: nur FRISCHE Trends zählen.
    up: EMA_m>EMA_s, close>EMA_m, EMA_m-Steigung >= thr x Tagesvola
    (optional: EMA_f-Steigung muss dasselbe Vorzeichen haben)."""
    sm = E["sl_m"] / np.maximum(dvol, 1e-9)
    sf = E["sl_f"] / np.maximum(dvol, 1e-9)
    up = (E["m"] > E["s"]) & (close > E["m"]) & (sm >= thr)
    dn = (E["m"] < E["s"]) & (close < E["m"]) & (sm <= -thr)
    if need_fast_sign:
        up &= sf > 0
        dn &= sf < 0
    return np.where(up, 1, np.where(dn, -1, 0)).astype(np.int8)


def main():
    with open(DATA, "rb") as fh:
        hist = pickle.load(fh)
    rows = {}
    for sym, candles in hist.items():
        base_over = {"regime_mode": 3, "min_phase_days": 1.0}
        cfg = eng.resolve_config(base_over, "1h", len(candles))
        bpd = cfg["bars_per_day"]
        f = eng.compute_matrix(candles, cfg)
        close = np.asarray(f["close"])
        dvol = np.nan_to_num(np.asarray(f["daily_vol_pct"]), nan=2.0)
        det0 = rx.detect(f, cfg)
        fin, warm = det0["final3"], det0["warm"]
        rows.setdefault("BASE", []).append(
            (sym, metrics(det0["live3"], fin, close, warm, bpd)))
        # A) Anti-Flicker: live_min_show_bars
        for show in ():
            c2 = dict(cfg)
            c2["live_min_show_bars"] = show
            d2 = rx.detect(f, c2)
            rows.setdefault(f"show={show}", []).append(
                (sym, metrics(d2["live3"], d2["final3"], close, warm, bpd)))
        # B) Steigungs-gegatete EMA-Overrides auf BASE-live
        for ds in ((9, 21, 50), (9, 50, 200), (9, 30, 90)):
            E = ema_arrays(close, bpd, ds, None)
            for thr in (0.1, 0.2, 0.35):
                ed = ema_dir_slope(close, E, dvol, thr=thr)
                lv = apply_variant(det0["live3"], ed, "side2trend")
                rows.setdefault(f"slope {ds} thr={thr} s2t", []).append(
                    (sym, metrics(lv, fin, close, warm, bpd)))
        # C) Verfeinerungen auf BASE-live mit (9,21,50)
        E = ema_arrays(close, bpd, (9, 21, 50), None)
        for thr in (0.15, 0.2):
            ed = ema_dir_slope(close, E, dvol, thr=thr)
            # C1: EMA-Richtung muss k Bars stabil sein
            for k in (6, 12, 24):
                eds = ed.copy()
                run = 0
                for i in range(1, len(ed)):
                    run = run + 1 if ed[i] == ed[i - 1] and ed[i] != 0 else 0
                    if ed[i] != 0 and run < k:
                        eds[i] = 0
                lv = apply_variant(det0["live3"], eds, "side2trend")
                rows.setdefault(f"s2t thr={thr} persist={k}", []).append(
                    (sym, metrics(lv, fin, close, warm, bpd)))
            # C2: zusätzlich Steigungs-Veto (Live-Trend, EMA-Steigung klar dagegen)
            sm = E["sl_m"] / np.maximum(dvol, 1e-9)
            lv = apply_variant(det0["live3"], ed, "side2trend")
            v = ((lv == 2) & (sm < -0.35)) | ((lv == 0) & (sm > 0.35))
            lv2 = lv.copy()
            lv2[v] = 1
            rows.setdefault(f"s2t thr={thr} + slopeveto", []).append(
                (sym, metrics(lv2, fin, close, warm, bpd)))
        # C3: Crossover-getriggertes Früh-Umschalten (f x m Kreuzung,
        # m-Seite von s passend): Overlay-State bis Gegenbeweis
        fm = E["f"] - E["m"]
        sm = E["sl_m"] / np.maximum(dvol, 1e-9)
        for need_ms in (False, True):
            ov = np.zeros(len(fm), dtype=np.int8)
            cur = 0
            for i in range(1, len(fm)):
                if fm[i] > 0 and fm[i - 1] <= 0 and (not need_ms or E["m"][i] > E["s"][i]):
                    cur = 1
                elif fm[i] < 0 and fm[i - 1] >= 0 and (not need_ms or E["m"][i] < E["s"][i]):
                    cur = -1
                if cur == 1 and fm[i] < 0:
                    cur = 0
                elif cur == -1 and fm[i] > 0:
                    cur = 0
                ov[i] = cur
            lv = det0["live3"].copy()
            m1 = (lv == 1) & (ov != 0)
            lv[m1] = np.where(ov[m1] > 0, 2, 0)
            rows.setdefault(f"cross-overlay need_ms={int(need_ms)}", []).append(
                (sym, metrics(lv, fin, close, warm, bpd)))
        # C4: bestes s2t + cross-overlay kombiniert
        ed = ema_dir_slope(close, E, dvol, thr=0.2)
        lv = apply_variant(det0["live3"], ed, "side2trend")
        ov = np.zeros(len(fm), dtype=np.int8)
        cur = 0
        for i in range(1, len(fm)):
            if fm[i] > 0 and fm[i - 1] <= 0 and sm[i] > 0:
                cur = 1
            elif fm[i] < 0 and fm[i - 1] >= 0 and sm[i] < 0:
                cur = -1
            if (cur == 1 and fm[i] < 0) or (cur == -1 and fm[i] > 0):
                cur = 0
            ov[i] = cur
        m1 = (lv == 1) & (ov != 0)
        lv[m1] = np.where(ov[m1] > 0, 2, 0)
        rows.setdefault("s2t thr=0.2 + cross(sm)", []).append(
            (sym, metrics(lv, fin, close, warm, bpd)))
    print(f"{'Variante':44s} {'agree':>6s} {'hold':>6s} {'hit':>6s} "
          f"{'lag':>5s} {'miss':>4s} {'segs':>5s} {'contra':>8s}")
    for key, res in rows.items():
        ag = np.mean([r["agree"] for _, r in res])
        ho = np.mean([r["hold"] for _, r in res])
        hi = np.mean([r["hit"] for _, r in res])
        lg = np.mean([r["lag"] for _, r in res if r["lag"] is not None])
        ms = sum(r["miss"] for _, r in res)
        sg = sum(r["segs"] for _, r in res)
        ct = "+".join(r["contra"] for _, r in res)
        print(f"{key:44s} {ag:6.1f} {ho:6.1f} {hi:6.1f} {lg:5.1f} "
              f"{ms:4d} {sg:5d} {ct:>12s}")
    fs = sum(r['fsegs'] for _, r in rows['BASE'])
    print(f"final_segs gesamt: {fs}")


if __name__ == "__main__":
    main()
