"""Multi-Core-Äquivalenz: Der Prozess-Pool (parallel_sim) muss EXAKT dieselben
Ergebnisse liefern wie der sequenzielle Pfad – gleiche simulate_pair-/_evaluate-
Logik, nur in Kind-Prozessen ausgeführt.

Läuft in-process (kein Server nötig): pytest tests/test_multicore.py -n 0
"""
import os
import random

from services import parallel_sim
from services import fast_sim
from services.backtester import simulate_pair
from services.bitunix_trade import DEFAULT_COIN_CFG
from services.optimizer import _evaluate
from strategies.custom_strategy import CustomStrategy
from strategies.registry import registry


def _mk_candles(n=3000, seed=7):
    rng = random.Random(seed)
    out, price, ts = [], 50000.0, 1700000000000
    for i in range(n):
        o = price
        c = o * (1 + rng.uniform(-0.004, 0.004))
        hi = max(o, c) * (1 + rng.uniform(0, 0.002))
        lo = min(o, c) * (1 - rng.uniform(0, 0.002))
        out.append({"timestamp": ts + i * 60000, "open": round(o, 2),
                    "high": round(hi, 2), "low": round(lo, 2),
                    "close": round(c, 2), "volume": 10 + rng.random() * 5})
        price = c
    return out


RSI_DEF = {"id": "mc_test", "name": "MC Test", "timeframe": "1m",
           "indicators": {"rsi_period": 14},
           "long_rules": [{"indicator": "rsi", "op": "<", "value": 35}],
           "short_rules": [{"indicator": "rsi", "op": ">", "value": 65}]}
SETTINGS = {"strategy_params": {}}


class TestWorkersConfigured:
    def test_env_semantics(self):
        old = os.environ.pop("SIM_WORKERS", None)
        try:
            assert parallel_sim.workers_configured() == 1  # nicht gesetzt -> Cloud
            os.environ["SIM_WORKERS"] = "0"
            assert parallel_sim.workers_configured() == (os.cpu_count() or 1)
            os.environ["SIM_WORKERS"] = "2"
            assert parallel_sim.workers_configured() == min(2, os.cpu_count() or 1)
            os.environ["SIM_WORKERS"] = "quatsch"
            assert parallel_sim.workers_configured() == 1
        finally:
            if old is None:
                os.environ.pop("SIM_WORKERS", None)
            else:
                os.environ["SIM_WORKERS"] = old


class TestBacktestEquivalence:
    def test_custom_strategy_pool_vs_sequential(self):
        candles = _mk_candles()
        cfg = dict(DEFAULT_COIN_CFG)
        strat = CustomStrategy(dict(RSI_DEF))
        # Sequenzielle Referenz mit Fast-Path (wie run_backtest)
        provider = fast_sim.build_signal_provider(RSI_DEF, fast_sim.FastSeries(candles))
        seq = simulate_pair(strat, candles, "BTCUSDT", SETTINGS, cfg, None,
                            True, None, provider)
        pool = parallel_sim.make_pool({"BTCUSDT|1m": candles}, 2)
        try:
            par = pool.submit(parallel_sim.sim_pair_task,
                              parallel_sim.strategy_spec(strat), "BTCUSDT|1m",
                              "BTCUSDT", SETTINGS, cfg, True, True).result(timeout=180)
        finally:
            parallel_sim.close_pool(pool)
        assert "_error" not in par, par.get("_error")
        assert par == seq  # inkl. all_trades: exakt identisch
        assert seq["trades"] > 0  # Test wäre sonst wertlos

    def test_legacy_path_pool_vs_sequential(self):
        candles = _mk_candles(2000, seed=11)
        cfg = dict(DEFAULT_COIN_CFG)
        strat = CustomStrategy(dict(RSI_DEF))
        seq = simulate_pair(strat, candles, "BTCUSDT", SETTINGS, cfg, None,
                            True, None, None)
        pool = parallel_sim.make_pool({"BTCUSDT|1m": candles}, 2)
        try:
            par = pool.submit(parallel_sim.sim_pair_task,
                              parallel_sim.strategy_spec(strat), "BTCUSDT|1m",
                              "BTCUSDT", SETTINGS, cfg, True, False).result(timeout=180)
        finally:
            parallel_sim.close_pool(pool)
        assert par == seq

    def test_builtin_strategy_in_child(self):
        candles = _mk_candles(2000, seed=5)
        cfg = dict(DEFAULT_COIN_CFG)
        strat = registry.get("rsi_only")
        assert strat is not None
        settings = {"strategy_params": {"rsi_only": {}}}
        try:
            provider = fast_sim.build_builtin_signal_provider(
                strat, fast_sim.FastSeries(candles), settings, "BTCUSDT")
        except Exception:
            provider = None
        seq = simulate_pair(strat, candles, "BTCUSDT", settings, cfg, None,
                            True, None, provider)
        pool = parallel_sim.make_pool({"BTCUSDT|1m": candles}, 2)
        try:
            par = pool.submit(parallel_sim.sim_pair_task,
                              {"strategy_id": "rsi_only"}, "BTCUSDT|1m",
                              "BTCUSDT", settings, cfg, True, True).result(timeout=180)
        finally:
            parallel_sim.close_pool(pool)
        assert "_error" not in par, par.get("_error")
        assert par == seq


class TestOptimizerEquivalence:
    def test_evaluate_task_pool_vs_sequential(self):
        h1 = _mk_candles(2500, seed=3)
        h2 = _mk_candles(2500, seed=4)
        histories = {"BTCUSDT": h1, "ETHUSDT": h2}
        fs_map = {s: fast_sim.FastSeries(c) for s, c in histories.items()}
        strat = CustomStrategy(dict(RSI_DEF))
        cfg = dict(DEFAULT_COIN_CFG)
        seq = _evaluate(strat, histories, SETTINGS, cfg, fs_map, None)
        pool = parallel_sim.make_pool(histories, 2)
        try:
            futs = [pool.submit(parallel_sim.evaluate_task,
                                parallel_sim.strategy_spec(strat),
                                ["BTCUSDT", "ETHUSDT"], SETTINGS,
                                {**cfg, "leverage": lev})
                    for lev in (cfg.get("leverage", 10), 5, 20)]
            par = futs[0].result(timeout=180)
            # verschiedene Configs im selben Pool funktionieren
            assert all(isinstance(f.result(timeout=180), dict) for f in futs)
        finally:
            parallel_sim.close_pool(pool)
        assert par == seq
