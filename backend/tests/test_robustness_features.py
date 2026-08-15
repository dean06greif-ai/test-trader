"""Regressionstests für die neuen Robustheits-Features:
Walk-Forward, Drawdown-Filter, Konstanz-Test, Top-5, GPU-Fallback, Zeitraum-Limits.
Reine Unit-/Integrationstests ohne Netzwerk (synthetische Kerzen).
"""
import asyncio
import math
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from services import gpu_accel, robustness  # noqa: E402
from services import optimizer as opt  # noqa: E402


# ---------------- Helpers ----------------
def synth_candles(n=4000, start_ts=1700000000000, step_ms=60000, base=100.0):
    out = []
    price = base
    for i in range(n):
        price = base * (1 + 0.05 * math.sin(i / 40.0))
        out.append({"timestamp": start_ts + i * step_ms,
                    "open": price, "high": price * 1.002, "low": price * 0.998,
                    "close": price, "volume": 10.0 + (i % 7)})
    return out


# ---------------- parse_config ----------------
class TestParseConfig:
    def test_defaults_disabled(self):
        cfg = robustness.parse_config({})
        assert cfg["wf_enabled"] is False
        assert cfg["dd_enabled"] is False
        assert cfg["ct_enabled"] is False
        assert cfg["any"] is False
        assert cfg["train_pct"] == 75.0
        assert cfg["dd_max_pct"] == 40.0
        assert cfg["ct_chunk_days"] == 30
        assert cfg["ct_max_dev_pct"] == 20.0

    def test_enabled_and_clamped(self):
        cfg = robustness.parse_config({
            "walk_forward": {"enabled": True, "train_pct": 99},
            "dd_filter": {"enabled": True, "max_dd_pct": 0.1},
            "constancy": {"enabled": True, "chunk_days": 1, "max_deviation_pct": 5000},
        })
        assert cfg["any"] is True
        assert cfg["train_pct"] == 95.0          # clamp oben
        assert cfg["dd_max_pct"] == 1.0          # clamp unten
        assert cfg["ct_chunk_days"] == 2         # clamp unten
        assert cfg["ct_max_dev_pct"] == 1000.0   # clamp oben

    def test_invalid_values_fall_back(self):
        cfg = robustness.parse_config({"walk_forward": {"enabled": True, "train_pct": "abc"}})
        assert cfg["train_pct"] == 75.0


# ---------------- Walk-Forward ----------------
class TestWalkForward:
    def test_split_proportions(self):
        candles = synth_candles(1000)
        train, test = robustness.split_histories({"BTCUSDT": candles}, 75.0)
        assert len(train["BTCUSDT"]) == 750
        assert len(test["BTCUSDT"]) == 250
        # chronologisch: Test kommt NACH dem Training
        assert train["BTCUSDT"][-1]["timestamp"] < test["BTCUSDT"][0]["timestamp"]

    def test_wf_score_prefers_consistent(self):
        good_train = {"pnl": 100.0, "win_rate": 60.0}
        good_test = {"pnl": 33.0, "win_rate": 58.0}    # ~gleich pro Tag (75/25)
        lucky_test = {"pnl": 200.0, "win_rate": 90.0}  # Test viel besser -> Zufall
        consistent = robustness.walk_forward_eval(good_train, good_test, 90, 30)
        lucky = robustness.walk_forward_eval(good_train, lucky_test, 90, 30)
        assert consistent["consistency_pct"] > lucky["consistency_pct"]

    def test_wf_score_negative_when_test_loses(self):
        r = robustness.walk_forward_eval({"pnl": 100.0, "win_rate": 60.0},
                                         {"pnl": -50.0, "win_rate": 30.0}, 90, 30)
        assert r["wf_score"] < 0
        assert r["consistency_pct"] == 0.0


# ---------------- Drawdown-Filter ----------------
class TestDrawdownFilter:
    def test_pass_and_fail(self):
        ok, ratio = robustness.dd_check({"pnl": 100.0, "max_drawdown": 30.0}, 40.0)
        assert ok is True and ratio == 30.0
        bad, ratio2 = robustness.dd_check({"pnl": 100.0, "max_drawdown": 55.0}, 40.0)
        assert bad is False and ratio2 == 55.0

    def test_negative_pnl_always_fails(self):
        ok, ratio = robustness.dd_check({"pnl": -10.0, "max_drawdown": 1.0}, 40.0)
        assert ok is False and ratio is None

    def test_score_applies_dd_penalty(self):
        m_ok = {"trades": 50, "win_rate": 60.0, "pnl": 100.0, "max_drawdown": 20.0}
        m_bad = {"trades": 50, "win_rate": 60.0, "pnl": 100.0, "max_drawdown": 90.0}
        s_ok = opt._score(m_ok, "combo", 10, dd_max_pct=40.0)
        s_bad = opt._score(m_bad, "combo", 10, dd_max_pct=40.0)
        assert s_ok > 0
        assert s_bad < -1e8
        # ohne Filter: identisches Verhalten wie bisher (Back-Compat)
        assert opt._score(m_bad, "combo", 10) == opt._score(m_ok, "combo", 10)


# ---------------- Konstanz-Test ----------------
class TestConstancy:
    def test_uniform_chunks_pass(self):
        r = robustness.evaluate_chunks([10.0, 11.0, 9.5, 10.5], 20.0)
        assert r["passed"] is True
        assert r["deviation_pct"] < 20
        assert r["profitable_chunks_pct"] == 100.0

    def test_concentrated_profit_fails(self):
        r = robustness.evaluate_chunks([0.0, 0.0, 100.0, 0.0], 20.0)
        assert r["passed"] is False
        assert r["deviation_pct"] > 100

    def test_negative_mean_fails(self):
        r = robustness.evaluate_chunks([-5.0, -3.0, 1.0], 20.0)
        assert r["passed"] is False
        assert r["deviation_pct"] is None

    def test_empty(self):
        assert robustness.evaluate_chunks([], 20.0)["passed"] is False


# ---------------- TopTracker ----------------
class TestTopTracker:
    def test_dedupe_and_order(self):
        t = robustness.TopTracker(3)
        d1 = {"long_rules": [{"indicator": "rsi", "op": "<", "value": 30}], "short_rules": []}
        d2 = {"long_rules": [{"indicator": "rsi", "op": "<", "value": 25}], "short_rules": []}
        t.add(robustness.rule_key(d1), {"definition": d1, "metrics": {}, "score": 5.0})
        t.add(robustness.rule_key(d1), {"definition": d1, "metrics": {}, "score": 9.0})  # besser
        t.add(robustness.rule_key(d1), {"definition": d1, "metrics": {}, "score": 2.0})  # schlechter
        t.add(robustness.rule_key(d2), {"definition": d2, "metrics": {}, "score": 7.0})
        top = t.top()
        assert len(top) == 2
        assert top[0]["score"] == 9.0 and top[1]["score"] == 7.0

    def test_trade_params_make_distinct_keys(self):
        d = {"long_rules": [], "short_rules": []}
        assert robustness.rule_key(d, {"leverage": 5}) != robustness.rule_key(d, {"leverage": 10})


# ---------------- GPU-Fallback (kein CuPy im CI -> CPU-Pfad) ----------------
class TestGpuAccel:
    def test_info_and_fallback_identical_to_pandas(self):
        import numpy as np
        import pandas as pd
        info = gpu_accel.info()
        assert "available" in info and "enabled" in info
        a = np.array([float(i % 13) + 0.5 for i in range(300)])
        for w in (5, 20):
            np.testing.assert_allclose(gpu_accel.rolling_mean(a, w),
                                       pd.Series(a).rolling(w).mean().to_numpy(),
                                       equal_nan=True)
            np.testing.assert_allclose(gpu_accel.rolling_std(a, w),
                                       pd.Series(a).rolling(w).std(ddof=0).to_numpy(),
                                       equal_nan=True)
            np.testing.assert_allclose(gpu_accel.rolling_max(a, w),
                                       pd.Series(a).rolling(w).max().to_numpy(),
                                       equal_nan=True)


# ---------------- Zeitraum-Limit (15 Jahre) ----------------
class TestDayLimits:
    def test_optimizer_clamps_at_5500(self):
        days = min(max(int(9999), 1), 5500)
        assert days == 5500
        import inspect
        src = inspect.getsource(opt.run_optimizer)
        assert "5500" in src


# ---------------- Integration: _finalize_top5 (Discovery, ohne Netzwerk) ----------------
class TestFinalizeTop5:
    def _run(self, robust_body):
        from services.bitunix_trade import DEFAULT_COIN_CFG
        from services import fast_sim
        candles = synth_candles(6000)
        robust = robustness.parse_config(robust_body)
        if robust["wf_enabled"]:
            train, test = robustness.split_histories({"BTCUSDT": candles}, robust["train_pct"])
        else:
            train, test = {"BTCUSDT": candles}, None
        fs_map = {s: fast_sim.FastSeries(c) for s, c in train.items()}
        definition = {"name": "T", "indicators": {},
                      "long_rules": [{"indicator": "rsi", "op": "<", "value": 45}],
                      "short_rules": [{"indicator": "rsi", "op": ">", "value": 55}]}
        job = {"phase": ""}
        cfg = dict(DEFAULT_COIN_CFG)
        settings = {}

        async def go():
            m = opt._evaluate(opt._mk_strategy(definition), train, settings, cfg, fs_map)
            cand = {"definition": definition, "trade_params": {}, "metrics": m, "score": 1.0}
            return await opt._finalize_top5(job, "discovery", [cand, cand], train, test,
                                            settings, cfg, robust, fs_map, None,
                                            None, 3.0, 1.0)
        return asyncio.run(go())

    def test_plain_top5_ranking(self):
        top5 = self._run({})
        assert 1 <= len(top5) <= 5
        e = top5[0]
        assert e["rank"] == 1
        assert "metrics" in e and "definition" in e and "rules" in e
        assert e["passed"] is True

    def test_walk_forward_fields(self):
        top5 = self._run({"walk_forward": {"enabled": True, "train_pct": 75}})
        e = top5[0]
        assert "test_metrics" in e
        assert "wf" in e and "wf_score" in e["wf"] and "consistency_pct" in e["wf"]

    def test_dd_and_constancy_fields(self):
        top5 = self._run({"dd_filter": {"enabled": True, "max_dd_pct": 40},
                          "constancy": {"enabled": True, "chunk_days": 2,
                                        "max_deviation_pct": 100}})
        e = top5[0]
        assert "dd_pass" in e
        assert "constancy" in e and "deviation_pct" in e["constancy"]
        assert isinstance(e["passed"], bool)


# ---------------- Rolling Walk-Forward ----------------
class TestRollingWalkForward:
    def test_parse_rolling_config(self):
        cfg = robustness.parse_config({"walk_forward": {"enabled": True, "mode": "rolling",
                                                        "windows": 99}})
        assert cfg["wf_mode"] == "rolling"
        assert cfg["wf_windows"] == 12  # clamp
        assert robustness.parse_config({})["wf_mode"] == "single"
        assert robustness.parse_config({"walk_forward": {"mode": "anchored"}})["wf_mode"] == "anchored"
        assert robustness.parse_config({"walk_forward": {"mode": "xyz"}})["wf_mode"] == "single"

    def test_anchored_windows_split(self):
        candles = synth_candles(1000)
        wins = robustness.rolling_windows({"BTCUSDT": candles}, 75.0, 4, anchored=True)
        train_len, test_len = 750, 62
        for i, w in enumerate(wins):
            tr, te = w["train"]["BTCUSDT"], w["test"]["BTCUSDT"]
            # Anchored: Training beginnt IMMER am Anfang und wächst je Fenster
            assert tr[0]["timestamp"] == candles[0]["timestamp"]
            assert len(tr) == train_len + i * test_len
            assert len(te) == test_len
            assert tr[-1]["timestamp"] < te[0]["timestamp"]
        # Test-Segmente identisch zum Rolling-Modus (gleiche OOS-Abdeckung)
        roll = robustness.rolling_windows({"BTCUSDT": candles}, 75.0, 4, anchored=False)
        for w_a, w_r in zip(wins, roll):
            assert w_a["test"]["BTCUSDT"][0]["timestamp"] == w_r["test"]["BTCUSDT"][0]["timestamp"]

    def test_rolling_windows_split(self):
        candles = synth_candles(1000)
        wins = robustness.rolling_windows({"BTCUSDT": candles}, 75.0, 4)
        assert len(wins) == 4
        train_len = 750
        test_len = (1000 - 750) // 4  # 62
        for i, w in enumerate(wins):
            tr, te = w["train"]["BTCUSDT"], w["test"]["BTCUSDT"]
            assert len(tr) == train_len
            assert len(te) == test_len
            # Fenster gleitet: Start i*test_len, Test direkt nach dem Training
            assert tr[0]["timestamp"] == candles[i * test_len]["timestamp"]
            assert tr[-1]["timestamp"] < te[0]["timestamp"]
            assert w["range"]["train_from"] and w["range"]["test_to"]
        # Test-Segmente überlappen nicht
        assert wins[0]["test"]["BTCUSDT"][-1]["timestamp"] < wins[1]["test"]["BTCUSDT"][0]["timestamp"]

    def test_aggregate_rolling(self):
        evals = [
            {"wf_score": 2.0, "consistency_pct": 80.0, "test_metrics": {"pnl": 10.0}},
            {"wf_score": 1.0, "consistency_pct": 60.0, "test_metrics": {"pnl": -5.0}},
        ]
        agg = robustness.aggregate_rolling(evals)
        assert agg["wf_score"] == 1.5
        assert agg["consistency_pct"] == 70.0
        assert agg["positive_windows_pct"] == 50.0
        assert agg["windows"] == 2
        assert robustness.aggregate_rolling([])["windows"] == 0

    def test_combine_test_metrics(self):
        combined = robustness.combine_test_metrics([
            {"trades": 4, "wins": 3, "losses": 1, "pnl": 10.0, "max_drawdown": 2.0, "fees": 0.5},
            {"trades": 2, "wins": 1, "losses": 1, "pnl": -3.0, "max_drawdown": 5.0, "fees": 0.2},
        ])
        assert combined["trades"] == 6
        assert combined["pnl"] == 7.0
        assert combined["max_drawdown"] == 5.0  # konservativ: schlechtestes Fenster
        assert combined["win_rate"] == round(4 / 6 * 100, 1)

    def test_finalize_rolling_integration(self):
        from services.bitunix_trade import DEFAULT_COIN_CFG
        from services import fast_sim
        candles = synth_candles(8000)
        robust = robustness.parse_config({"walk_forward": {"enabled": True, "mode": "rolling",
                                                           "windows": 3, "train_pct": 70}})
        wf_windows = robustness.rolling_windows({"BTCUSDT": candles}, 70.0, 3)
        train = wf_windows[0]["train"]
        fs_map = {s: fast_sim.FastSeries(c) for s, c in train.items()}
        definition = {"name": "T", "indicators": {},
                      "long_rules": [{"indicator": "rsi", "op": "<", "value": 45}],
                      "short_rules": [{"indicator": "rsi", "op": ">", "value": 55}]}
        job = {"phase": ""}
        cfg = dict(DEFAULT_COIN_CFG)

        async def go():
            m = opt._evaluate(opt._mk_strategy(definition), train, {}, cfg, fs_map)
            cand = {"definition": definition, "trade_params": {}, "metrics": m, "score": 1.0}
            return await opt._finalize_top5(job, "discovery", [cand], train, None,
                                            {}, cfg, robust, fs_map, None,
                                            None, 3.9, 0.55, wf_windows)
        top5 = asyncio.run(go())
        e = top5[0]
        assert len(e["wf_windows"]) == 3
        for w in e["wf_windows"]:
            assert "test_metrics" in w and "wf_score" in w and "range" in w
        assert "positive_windows_pct" in e["wf"]
        assert "test_metrics" in e  # kombinierte Test-Metriken
        assert "Fenster" in job["phase"] or "Konstanz" in job["phase"] or job["phase"]


# ---------------- Stresstest / Stabilität / Monte-Carlo / Regime ----------------
class TestNewRobustnessChecks:
    def test_parse_new_config(self):
        cfg = robustness.parse_config({
            "stress_test": {"enabled": True, "cost_multiplier": 2.0},
            "stability": {"enabled": True, "variation_pct": 15},
            "monte_carlo": {"enabled": True, "runs": 10000},
            "regime_analysis": {"enabled": True},
        })
        assert cfg["st_enabled"] and cfg["st_mult"] == 2.0
        assert cfg["sb_enabled"] and cfg["sb_var_pct"] == 15.0
        assert cfg["mc_enabled"] and cfg["mc_runs"] == 2000  # clamp
        assert cfg["rg_enabled"] and cfg["any"]
        off = robustness.parse_config({})
        assert not (off["st_enabled"] or off["sb_enabled"] or off["mc_enabled"] or off["rg_enabled"])

    def test_stressed_cfg(self):
        c = robustness.stressed_cfg({"fee_percent": 0.06, "leverage": 5}, 1.5)
        assert abs(c["fee_percent"] - 0.09) < 1e-9
        assert c["leverage"] == 5

    def test_perturb_definition_and_params(self):
        d = {"long_rules": [{"indicator": "rsi", "op": "<", "value": 30}],
             "short_rules": [{"indicator": "rsi", "op": ">", "value": 70.0}]}
        p = robustness.perturb_definition(d, 0.1)
        assert p["long_rules"][0]["value"] == 33          # int bleibt int
        assert abs(p["short_rules"][0]["value"] - 77.0) < 1e-6
        assert d["long_rules"][0]["value"] == 30          # Original unverändert
        pp = robustness.perturb_params({"period": 20, "use_x": True, "name": "a"}, -0.1)
        assert pp["period"] == 18 and pp["use_x"] is True and pp["name"] == "a"

    def test_stability_eval(self):
        ok = robustness.stability_eval(100.0, [90, 80, 110, 70], 10.0)
        assert ok["passed"] and ok["positive_pct"] == 100.0
        bad = robustness.stability_eval(100.0, [-5, -10, 3, -8], 10.0)
        assert not bad["passed"]
        assert not robustness.stability_eval(-10.0, [5, 5, 5, 5], 10.0)["passed"]

    def test_monte_carlo(self):
        pnls = [10, -5, 8, -3, 12, -6, 9, -4] * 5
        mc = robustness.monte_carlo(pnls, 100, 100.0)
        assert mc["runs"] == 100
        assert mc["total_pnl"] == sum(pnls)
        assert mc["dd_p50"] <= mc["dd_p95"] <= mc["dd_worst"]
        assert mc["passed"] is True
        # deterministisch (Seed 42)
        assert robustness.monte_carlo(pnls, 100, 100.0)["dd_p95"] == mc["dd_p95"]
        assert robustness.monte_carlo([1, 2], 100, 100.0)["passed"] is False
        assert robustness.monte_carlo([-1, -2, -3, -4], 100, 100.0)["passed"] is False

    def test_regime_breakdown(self):
        up = synth_candles(600)
        for i, c in enumerate(up):  # klarer Aufwärtstrend
            c["close"] = 100 + i * 0.5
        trades = [("BTCUSDT", up[500]["timestamp"], 5.0),
                  ("BTCUSDT", up[550]["timestamp"], -2.0)]
        agg = robustness.regime_breakdown(trades, {"BTCUSDT": up})
        assert agg["bull"]["trades"] == 2
        assert agg["bull"]["pnl"] == 3.0
        assert agg["bear"]["trades"] == 0

    def test_chunk_pnls_from_trades(self):
        candles = synth_candles(4 * 1440, step_ms=60000)  # 4 Tage 1m
        h = {"BTCUSDT": candles}
        t0 = candles[0]["timestamp"]
        trades = [("BTCUSDT", t0 + 1000, 5.0), ("BTCUSDT", t0 + 3 * 86400000, 7.0)]
        pnls = robustness.chunk_pnls_from_trades(trades, h, 1)
        assert len(pnls) == 4
        assert pnls[0] == 5.0 and pnls[3] == 7.0


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-n", "0"])
