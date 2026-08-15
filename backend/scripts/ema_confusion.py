"""Konfusions-Analyse Live vs Final + Range-Veto-Test."""
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
from ema_testbed import ema_arrays, ema_dir_slope, apply_variant, metrics  # noqa: E402

DATA = os.path.join(os.path.dirname(__file__), "_testbed_candles.pkl")

with open(DATA, "rb") as fh:
    hist = pickle.load(fh)
for sym, candles in hist.items():
    cfg = eng.resolve_config({"regime_mode": 3, "min_phase_days": 1.0},
                             "1h", len(candles))
    bpd = cfg["bars_per_day"]
    f = eng.compute_matrix(candles, cfg)
    close = np.asarray(f["close"])
    dvol = np.nan_to_num(np.asarray(f["daily_vol_pct"]), nan=2.0)
    det = rx.detect(f, cfg)
    live, fin, warm = det["live3"], det["final3"], det["warm"]
    n = len(live)
    cm = np.zeros((3, 3), dtype=int)
    for i in range(warm, n):
        cm[int(live[i]), int(fin[i])] += 1
    print(sym, "Zeilen=live(ab,seit,auf) Spalten=final:")
    print(cm, " anteil final seitwärts:", round(float(np.mean(fin[warm:] == 1)), 3))
    # Range-Veto: EMAs verflochten (Abstand m..s klein) + flache Steigung
    E = ema_arrays(close, bpd, (9, 21, 50), None)
    ed = ema_dir_slope(close, E, dvol, thr=0.2)
    lv = apply_variant(live, ed, "side2trend")
    width = np.abs(E["m"] - E["s"]) / np.maximum(close, 1e-9) * 100.0 / np.maximum(dvol, 1e-9)
    sm = np.abs(E["sl_m"]) / np.maximum(dvol, 1e-9)
    for w0 in (0.15, 0.3, 0.5):
        v = (width < w0) & (sm < 0.15) & ((lv == 0) | (lv == 2))
        lv2 = lv.copy()
        lv2[v] = 1
        r = metrics(lv2, fin, close, warm, bpd)
        print(f"  s2t0.2 + rangeveto w<{w0}: agree={r['agree']:.1f} hold={r['hold']:.1f} "
              f"hit={r['hit']:.1f} lag={r['lag']:.1f} segs={r['segs']} contra={r['contra']}")
