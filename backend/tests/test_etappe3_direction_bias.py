"""Etappe 3 – Richtungs-Bias in der Regime-Strategie-Suche.

Coverage:
- allowed_sides_for(): auto leitet aus der Regime-Richtung ab (Modus 3/5/9),
  long/short fest, off/None = kein Filter
- simulate_pair() respektiert cfg['allowed_sides']: nur erlaubte Seiten werden
  gehandelt; ohne Filter unverändertes Verhalten (Regression)
"""
import numpy as np
import pytest

from services.backtester import simulate_pair
from services.regime_opt import allowed_sides_for


# ------------------------------ allowed_sides_for ------------------------------
# Modus 3: 0=ab · 1=seitwärts · 2=auf
# Modus 5: 0=stark ab · 1=leicht ab · 2=seitwärts · 3=leicht auf · 4=stark auf
def test_auto_bias_mode3():
    assert allowed_sides_for("auto", 2, 3) == ["LONG"]
    assert allowed_sides_for("auto", 0, 3) == ["SHORT"]
    assert allowed_sides_for("auto", 1, 3) is None


def test_auto_bias_mode5():
    assert allowed_sides_for("auto", 4, 5) == ["LONG"]
    assert allowed_sides_for("auto", 3, 5) == ["LONG"]
    assert allowed_sides_for("auto", 2, 5) is None
    assert allowed_sides_for("auto", 1, 5) == ["SHORT"]
    assert allowed_sides_for("auto", 0, 5) == ["SHORT"]


def test_fixed_and_off_bias():
    assert allowed_sides_for("long", 0, 5) == ["LONG"]
    assert allowed_sides_for("short", 4, 5) == ["SHORT"]
    assert allowed_sides_for("off", 4, 5) is None
    assert allowed_sides_for(None, 4, 5) is None
    assert allowed_sides_for("quatsch", 4, 5) is None


# ------------------------------ simulate_pair ----------------------------------
class AlternatingStrategy:
    """Gibt abwechselnd LONG- und SHORT-Signale (alle 30 Kerzen)."""
    STRATEGY_ID = "test_alternating"
    STRATEGY_NAME = "Test Alternating"

    def __init__(self):
        self.n = 0

    def check_signal(self, window, symbol, settings):
        i = len(window)
        self.n += 1
        if self.n % 30 != 0:
            return None
        side = "LONG" if (self.n // 30) % 2 == 0 else "SHORT"
        entry = float(window[-1]["close"])
        return {"type": side, "entry_price": entry}


def make_candles(n=1200, seed=3):
    rng = np.random.default_rng(seed)
    closes = 100 * np.cumprod(1 + rng.normal(0, 0.004, n))
    out = []
    for i, c in enumerate(closes):
        out.append({"timestamp": 1_700_000_000_000 + i * 900_000,
                    "open": c * 0.999, "high": c * 1.006,
                    "low": c * 0.994, "close": float(c), "volume": 50.0})
    return out


CFG = {"max_capital": 100, "leverage": 5, "fee_percent": 0.06,
       "sl_mode": "percent", "sl_percent": 2.0}


def _sides(res):
    return {t["side"] for t in (res.get("all_trades") or [])}


def test_simulate_pair_no_filter_trades_both_sides():
    candles = make_candles()
    res = simulate_pair(AlternatingStrategy(), candles, "TESTUSDT", {},
                        dict(CFG), collect_trades=True)
    assert res.get("trades", 0) > 2
    assert _sides(res) == {"LONG", "SHORT"}


def test_simulate_pair_long_only():
    candles = make_candles()
    res = simulate_pair(AlternatingStrategy(), candles, "TESTUSDT", {},
                        {**CFG, "allowed_sides": ["LONG"]}, collect_trades=True)
    assert res.get("trades", 0) > 0
    assert _sides(res) == {"LONG"}


def test_simulate_pair_short_only():
    candles = make_candles()
    res = simulate_pair(AlternatingStrategy(), candles, "TESTUSDT", {},
                        {**CFG, "allowed_sides": ["SHORT"]}, collect_trades=True)
    assert res.get("trades", 0) > 0
    assert _sides(res) == {"SHORT"}


def test_simulate_pair_invalid_filter_ignored():
    candles = make_candles()
    res = simulate_pair(AlternatingStrategy(), candles, "TESTUSDT", {},
                        {**CFG, "allowed_sides": ["quatsch"]},
                        collect_trades=True)
    # Ungültige Seiten werden verworfen -> kein Filter (beide Seiten)
    assert _sides(res) == {"LONG", "SHORT"}
