"""Prototyp: Regime = Steigung einer EMA (Nutzer-Idee: 'EMA-9-Steigung als
Regime'). Kausal, mit Hysterese + Mindest-Phasendauer. Vergleich gegen den
reaktiven Detektor auf 720d/1h und 2000d/1d."""
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


def ema_regime(close, dvol, bpd, days=9.0, thr=0.15, hys=0.5,
               min_phase_days=3.0):
    span = max(int(round(days * bpd)), 2)
    e = rf.ema(close, span)
    k = max(int(round(span * 0.25)), 1)
    sl = np.zeros(len(e))
    sl[k:] = (e[k:] / np.maximum(e[:-k], 1e-12) - 1.0) * 100.0 / (k / bpd)
    sn = sl / np.maximum(dvol, 1e-9)
    lab = np.ones(len(e), dtype=np.int8)
    cur = 1
    lo = thr * hys
    for i in range(len(e)):
        s = sn[i]
        if cur == 1:
            if s >= thr:
                cur = 2
            elif s <= -thr:
                cur = 0
        elif cur == 2:
            if s <= -thr:
                cur = 0
            elif s < lo:
                cur = 1
        else:
            if s >= thr:
                cur = 2
            elif s > -lo:
                cur = 1
        lab[i] = cur
    live = lab.copy()
    mp = max(int(round(min_phase_days * bpd)), 2)
    fin = rx._absorb_short(lab.copy(), mp)
    return live, fin


def nseg(a):
    return int(np.sum(a[1:] != a[:-1]) + 1)


def report(name, candles, tf, days, thr, mp):
    cfg = eng.resolve_config({"regime_mode": 3}, tf, len(candles))
    bpd = cfg["bars_per_day"]
    f = eng.compute_matrix(candles, cfg)
    close = np.asarray(f["close"])
    dvol = np.nan_to_num(np.asarray(f["daily_vol_pct"]), nan=2.0)
    live, fin = ema_regime(close, dvol, bpd, days=days, thr=thr,
                           min_phase_days=mp)
    warm = max(int(round(days * bpd * 0.25)), 1)
    span_d = len(candles) / bpd
    agree = float(np.mean(live[warm:] == fin[warm:])) * 100
    shares = {k: round(float(np.mean(fin[warm:] == v)), 2)
              for k, v in (("dn", 0), ("side", 1), ("up", 2))}
    # Widersprüche der FINAL-Segmente (Netto-Richtung vs Label)
    bad = tot = 0
    s = warm
    for i in range(warm + 1, len(fin) + 1):
        if i == len(fin) or fin[i] != fin[s]:
            lab = int(fin[s])
            if lab in (0, 2) and i - s > 4:
                net = close[i - 1] / close[s] - 1
                tot += 1
                if (lab == 2 and net < -0.005) or (lab == 0 and net > 0.005):
                    bad += 1
            s = i
    print(f"{name} EMA{days:g} thr={thr} mp={mp}d: final_segs={nseg(fin[warm:])} "
          f"(Ø {span_d / max(nseg(fin[warm:]), 1):.1f}d) live_segs={nseg(live[warm:])} "
          f"live=final {agree:.0f}% · shares {shares} · contra {bad}/{tot}")
    return fin, warm, cfg, close


h720 = pickle.load(open(os.path.join(os.path.dirname(__file__),
                                     "_testbed_candles.pkl"), "rb"))
h2000 = pickle.load(open(os.path.join(os.path.dirname(__file__),
                                      "_testbed_2000d.pkl"), "rb"))
for days in (5.0, 9.0, 14.0):
    for thr in (0.12, 0.18, 0.25):
        fin, warm, cfg, close = report("BTC720", h720["BTCUSDT"], "1h",
                                       days, thr, 3.0)
print("---")
# Wie wird Aug-Nov 2024 (BTC-Rally 55k->90k+) gelabelt? (erste ~90 Tage)
fin, warm, cfg, close = report("BTC720", h720["BTCUSDT"], "1h", 9.0, 0.18, 3.0)
bpd = cfg["bars_per_day"]
a, b = warm, int(90 * bpd)
print("Erste ~90 Tage Anteile:", {k: round(float(np.mean(fin[a:b] == v)), 2)
                                  for k, v in (("dn", 0), ("side", 1), ("up", 2))})
print("---")
for days in (9.0, 14.0):
    report("ETH720", h720["ETHUSDT"], "1h", days, 0.18, 3.0)
    report("BTC2000", h2000["BTCUSDT"], "24h", days, 0.18, 5.0)
# Referenz: reaktiver Detektor
for sym in ("BTCUSDT",):
    candles = h720[sym]
    cfg = eng.resolve_config({"regime_mode": 3}, "1h", len(candles))
    f = eng.compute_matrix(candles, cfg)
    det = rx.detect(f, cfg)
    warm = det["warm"]
    print(f"REF reaktiv {sym}: final_segs={nseg(det['final3'][warm:])} "
          f"live_segs={nseg(det['live3'][warm:])}")
