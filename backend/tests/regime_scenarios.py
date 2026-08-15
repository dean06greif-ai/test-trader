"""Szenario-Bank für die Regime-Engine: synthetische Märkte mit bekannter
Wahrheit. Wird von den Tests und vom Tuning-Skript genutzt."""
import numpy as np

from tests.test_regime_engine import make_candles, range_series, series


def scenarios():
    up = series(250, 0.55, 0.9, seed=31)
    down = series(250, -0.55, 0.9, start=float(up[-1]), seed=32)
    calm = series(300, 0.0, 0.6, seed=51)
    wild = series(300, 0.0, 4.0, start=float(calm[-1]), seed=52)
    mixed, last = [], 100.0
    for drift, noise, seed in [(0.5, 0.7, 41), (0.0, 0.7, 42), (-0.5, 2.5, 43),
                               (0.0, 3.0, 44), (0.6, 1.6, 45)]:
        s = series(220, drift, noise, start=last, seed=seed)
        mixed.append(s)
        last = float(s[-1])
    return {
        "up_strong": (make_candles(series(400, 0.5, 1.0, seed=21)), "up"),
        "down_strong": (make_candles(series(400, -0.6, 1.0, seed=22)), "down"),
        "down_slow_long": (make_candles(series(600, -0.12, 0.9, seed=23)), "down"),
        "up_slow_long": (make_candles(series(600, 0.10, 0.9, seed=25)), "up"),
        "chop": (make_candles(range_series(400, amp_pct=3, period=15, seed=24)), "side"),
        "chop_wide": (make_candles(range_series(500, amp_pct=4, period=20, seed=26)),
                      "side"),
        "cycle_long": (make_candles(range_series(400, amp_pct=6, period=40, seed=27)), None),
        "reversal": (make_candles(np.concatenate([up, down])), None),
        "vol_jump": (make_candles(np.concatenate([calm, wild]), vol_pct=1.0), None),
        "mixed": (make_candles(np.concatenate(mixed)), None),
        "random_walk": (make_candles(series(600, 0.0, 1.2, seed=124)), None),
    }
