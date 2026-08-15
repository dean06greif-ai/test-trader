"""Gate-Test: EMA-Signal nur wenn die mittlere EMA neues Terrain gewinnt."""
import os
import pickle
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tests"))
sys.path.insert(0, os.path.dirname(__file__))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from services import regime_engine as eng  # noqa: E402
from services import regime_reactive as rx  # noqa: E402
from ema_testbed import metrics  # noqa: E402


def ema_sig(f, cfg, gate):
    close = np.asarray(f["close"])
    dvol = np.nan_to_num(np.asarray(f["daily_vol_pct"]), nan=2.0)
    em, es = np.asarray(f["ema_mid"]), np.asarray(f["ema_slow"])
    sm = np.asarray(f["ema_slope_mid"]) / np.maximum(dvol, 1e-9)
    sf = np.asarray(f["ema_slope_fast"]) / np.maximum(dvol, 1e-9)
    up = (em > es) & (close > em) & (sm >= 0.2) & (sf > 0)
    dn = (em < es) & (close < em) & (sm <= -0.2) & (sf < 0)
    if gate:
        w = int(cfg["ema_slow_bars"] if gate == "slow" else cfg["ema_mid_bars"] * 2)
        s = pd.Series(em)
        hi = s.shift(1).rolling(w, min_periods=2).max().to_numpy()
        lo = s.shift(1).rolling(w, min_periods=2).min().to_numpy()
        up &= ~np.isfinite(hi) | (em >= hi)
        dn &= ~np.isfinite(lo) | (em <= lo)
    ed = np.where(up, 1, np.where(dn, -1, 0)).astype(np.int8)
    k = max(int(cfg.get("ema_persist_bars") or 1), 1)
    if k > 1:
        run = 0
        st = ed.copy()
        for i in range(len(ed)):
            run = run + 1 if i and ed[i] == ed[i - 1] and ed[i] != 0 else 0
            if ed[i] != 0 and run < k:
                st[i] = 0
        ed = st
    return ed


def run(hist, tf, over):
    rows = {}
    for sym, candles in hist.items():
        cfg = eng.resolve_config({**over, "use_ema_confirm": False},
                                 tf, len(candles))
        bpd = cfg["bars_per_day"]
        f = eng.compute_matrix(candles, cfg)
        close = np.asarray(f["close"])
        det = rx.detect(f, cfg)
        live, fin, warm = det["live3"], det["final3"], det["warm"]
        rows.setdefault("ohne EMA", []).append(
            (sym, metrics(live, fin, close, warm, bpd)))
        for gate in (None, "slow", "mid2"):
            ed = ema_sig(f, cfg, gate)
            lv = live.copy()
            m = (lv == 1) & (ed != 0)
            lv[m] = np.where(ed[m] > 0, 2, 0)
            rows.setdefault(f"gate={gate}", []).append(
                (sym, metrics(lv, fin, close, warm, bpd)))
    for key, res in rows.items():
        ag = np.mean([r["agree"] for _, r in res])
        ho = np.mean([r["hold"] for _, r in res])
        hi = np.mean([r["hit"] for _, r in res])
        lgs = [r["lag"] for _, r in res if r["lag"] is not None]
        lg = np.mean(lgs) if lgs else -1
        ms = sum(r["miss"] for _, r in res)
        sg = sum(r["segs"] for _, r in res)
        print(f"  {key:12s} agree={ag:5.1f} hold={ho:5.1f} hit={hi:5.1f} "
              f"lag={lg:5.1f} miss={ms} segs={sg}")


# 1) Synthetische Range: Anteil Seitwärts nach Override?
from test_regime_engine import make_candles, range_series  # noqa: E402
candles = make_candles(range_series(400, amp_pct=3, period=15, seed=24))
cfg = eng.resolve_config({"regime_mode": 3, "use_ema_confirm": False},
                         "24h", len(candles))
f = eng.compute_matrix(candles, cfg)
det = rx.detect(f, cfg)
for gate in (None, "slow", "mid2"):
    ed = ema_sig(f, cfg, gate)
    lv = det["live3"].copy()
    m = (lv == 1) & (ed != 0)
    lv[m] = np.where(ed[m] > 0, 2, 0)
    print(f"RANGE gate={gate}: Anteil seitwärts (live) =",
          round(float(np.mean(lv[det['warm']:] == 1)), 2))

print("720d/1h:")
with open(os.path.join(os.path.dirname(__file__), "_testbed_candles.pkl"), "rb") as fh:
    run(pickle.load(fh), "1h", {"regime_mode": 3, "min_phase_days": 1.0})
print("2000d/1d:")
with open(os.path.join(os.path.dirname(__file__), "_testbed_2000d.pkl"), "rb") as fh:
    run(pickle.load(fh), "24h", {"regime_mode": 3})
