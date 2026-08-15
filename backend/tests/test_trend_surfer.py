"""Regressionstests für die neue Preset-Strategie "Trend-Surfer" (Iteration 10).

Design-Herleitung: 9 Backtest-Runden auf echten Binance-Daten (730d, 12 Coins,
Walk-Forward, Fees 0.06%/Seite) – siehe scripts/ultimate_strategy_lab*.py und
/app/test_reports/lab*_*.txt.
"""
import numpy as np

from services.fast_sim import FastSeries
from strategies.registry import registry
from strategies.trend_surfer_strategy import TrendSurferStrategy, _efficiency_ratio


def _params(over=None):
    p = {k: meta["value"] for k, meta in TrendSurferStrategy.DEFAULT_PARAMS.items()}
    if over:
        p.update(over)
    return p


def _mk_candles(closes, spread=0.001, vol=100.0):
    out = []
    for i, c in enumerate(closes):
        out.append({"timestamp": 1700000000000 + i * 7200000,
                    "open": c * (1 - spread / 2), "high": c * (1 + spread),
                    "low": c * (1 - spread), "close": c, "volume": vol})
    return out


def _trending_up(n=320, start=100.0, step=0.004):
    # sauberer Aufwärtstrend mit kleinen Wellen -> hohe Effizienz
    xs = np.arange(n)
    closes = start * (1 + step) ** xs * (1 + 0.0015 * np.sin(xs / 6))
    return _mk_candles(list(closes))


def _choppy(n=320, start=100.0):
    # enge Seitwärts-Säge -> niedrige ER, Regime-Gate muss zu sein
    xs = np.arange(n)
    closes = start * (1 + 0.004 * np.sin(xs * 1.3))
    return _mk_candles(list(closes))


def test_registered_with_2h_timeframe():
    strat = registry.get("trend_surfer")
    assert strat is not None
    assert strat.STRATEGY_TIMEFRAME == "2h"
    meta = strat.get_metadata()
    assert meta["id"] == "trend_surfer" and meta["timeframe"] == "2h"


def test_default_params_match_backtest_validation():
    p = _params()
    assert p["donchian_period"] == 48
    assert p["ema_slow_period"] == 200
    assert p["adx_min"] == 20
    assert p["er_min"] == 0.4
    assert p["gate_er_period"] == 120 and p["gate_er_min"] == 0.15
    assert p["atr_sl_mult"] == 3.0 and p["tp1_rr"] == 1.5 and p["tp_rr"] == 5.0


def test_efficiency_ratio_math():
    straight = np.array([float(i) for i in range(50)])
    er = _efficiency_ratio(straight, 20)
    assert er[-1] > 0.99                      # gerade Linie = maximale Effizienz
    saw = np.array([100.0 + (1 if i % 2 else -1) for i in range(60)])
    er2 = _efficiency_ratio(saw, 20)
    assert er2[-1] < 0.2                      # Säge = ineffizient


def test_long_signal_in_clean_uptrend():
    strat = TrendSurferStrategy()
    candles = _trending_up()
    res = strat.analyze(candles, "TESTUSDT", _params({"adx_min": 15}))
    assert res is not None
    assert res["rules_total"] == 5
    rules = {r["id"]: r for r in res["rules"]}
    assert rules["trend"]["long"] is True
    assert rules["efficiency"]["long"] is True
    # Synthetischer Dauertrend: jede Kerze = neues Hoch -> Ausbruch + Signal
    assert res["signal_type"] == "LONG"
    lv = res["levels"]
    assert lv["stop_loss"] < lv["entry"] < lv["take_profit_1"] < lv["take_profit_full"]
    # TP/SL-Verhältnis = tp_rr (5R)
    risk = lv["entry"] - lv["stop_loss"]
    assert abs((lv["take_profit_full"] - lv["entry"]) / risk - 5.0) < 0.01


def test_no_signal_in_chop_regime_gate():
    strat = TrendSurferStrategy()
    res = strat.analyze(_choppy(), "TESTUSDT", _params({"adx_min": 0}))
    assert res is not None
    assert res["signal_type"] is None
    rules = {r["id"]: r for r in res["rules"]}
    assert rules["efficiency"]["long"] is False or rules["regime"]["long"] is False


def test_allow_short_toggle():
    strat = TrendSurferStrategy()
    closes = list(np.linspace(200, 100, 320) * (1 + 0.001 * np.sin(np.arange(320) / 5)))
    res = strat.analyze(_mk_candles(closes), "TESTUSDT",
                        _params({"adx_min": 10, "allow_short": 0}))
    assert res is not None and res["signal_type"] != "SHORT"


def test_vectorized_matches_analyze_last_bar():
    strat = TrendSurferStrategy()
    candles = _trending_up()
    params = _params({"adx_min": 15})
    fs = FastSeries(candles)
    vec_sig = strat.vectorized_signals(fs, params)
    res = strat.analyze(candles, "TESTUSDT", params)
    assert bool(vec_sig["long"][-1]) == (res["signal_type"] == "LONG")
    assert vec_sig["rules_total"] == 5
    # Warmup: keine Signale vor der Aufwärmphase
    assert not vec_sig["long"][:vec_sig["warmup"]].any()


def test_warmup_fits_live_buffer():
    # Live liefert der Scanner ~220 aggregierte 2h-Kerzen (buffer_limit 220*120,
    # Deckel 30240 -> 252 Kerzen). Der Warmup muss sicher darunter liegen.
    strat = TrendSurferStrategy()
    fs = FastSeries(_trending_up(n=260))
    s = strat._series(fs, _params())
    assert s["warmup"] <= 210
    from services.strategy_scanner import StrategyScanner
    sc = StrategyScanner()
    sc.settings["enabled_strategies"] = ["trend_surfer"]
    assert sc.buffer_limit() >= 210 * 120  # genug 1m-Kerzen für >=210 2h-Bars


def test_recommended_override_seed_in_server():
    import inspect
    import server
    src = inspect.getsource(server)
    assert '"trend_surfer" not in overrides' in src
    assert '"atr_sl_multiplier": 3.0' in src and '"tp_full_crv": 5.0' in src
