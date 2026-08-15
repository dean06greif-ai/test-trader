"""Tests: Multi-Timeframe pro Regel (Timeframe-Override für Custom-/KI-Strategien).

Deckt ab:
  - Timeframe-Helper (normalize/valid/options, 1d bis 1 Tag)
  - Aggregation mit base_ms (drop_partial auch aus Nicht-1m-Quellen)
  - FastSeries.htf(): Mapping auf letzte GESCHLOSSENE HTF-Kerze (kein Lookahead)
  - _rule_cond mit "timeframe"-Override + Rückwärtskompatibilität ohne Override
  - Parität Live-Pfad (CustomStrategy.analyze) vs. Fast-Path (build_signal_provider)
  - normalize_definition / apply_params / rule_timeframe_space / get_params
  - Optimizer build_candidates mit TF-Varianten
  - Scanner buffer_limit mit Regel-TF-Overrides
"""
import numpy as np
import pytest

from services import fast_sim
from services.timeframes import (RULE_TIMEFRAMES, TIMEFRAMES, aggregate_candles,
                                 normalize_rule_tf, rule_tf_options, tf_minutes,
                                 valid_rule_tf)
from strategies import custom_params
from strategies.custom_strategy import INDICATORS, OPERATORS, CustomStrategy


def mk_candles(n, start=0, step_ms=60000, base=100.0, wave=True):
    out = []
    for i in range(n):
        px = base + (np.sin(i / 7.0) * 2.0 if wave else i * 0.1)
        out.append({"timestamp": start + i * step_ms, "open": px,
                    "high": px + 0.3, "low": px - 0.3, "close": px + 0.1,
                    "volume": 10.0 + (i % 5)})
    return out


# ---------------------------------------------------------------- Helper ---
class TestTimeframeHelpers:
    def test_1d_supported(self):
        assert TIMEFRAMES["1d"] == 1440
        assert "1d" in RULE_TIMEFRAMES
        assert RULE_TIMEFRAMES[-1] == "1d"

    def test_normalize(self):
        assert normalize_rule_tf("15M") == "15m"
        assert normalize_rule_tf("24h") == "1d"
        assert normalize_rule_tf("1d") == "1d"
        assert normalize_rule_tf("60m") == "1h"
        assert normalize_rule_tf("xx") is None
        assert normalize_rule_tf(None) is None
        # 2m/6h sind Strategie-TFs, aber keine Regel-TF-Stufen
        assert normalize_rule_tf("2m") is None

    def test_valid_rule_tf(self):
        assert valid_rule_tf("15m", "5m")     # Vielfaches, höher
        assert valid_rule_tf("1h", "1m")
        assert valid_rule_tf("5m", "5m")      # gleich = gültig (wird zu "kein Override")
        assert not valid_rule_tf("5m", "15m")  # niedriger als Basis
        assert not valid_rule_tf("30m", "4h")
        assert not valid_rule_tf("xx", "1m")

    def test_options_range(self):
        opts = rule_tf_options("1m", "1m", "4h")
        assert opts == ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h"]
        opts5 = rule_tf_options("5m", "1m", "4h")
        assert "3m" not in opts5 and "5m" in opts5 and "4h" in opts5
        assert rule_tf_options("1m", "1m", "1d")[-1] == "1d"


class TestAggregationBaseMs:
    def test_drop_partial_from_5m_source(self):
        # 6 komplette 5m-Kerzen (0..30min) -> zwei komplette 15m-Buckets
        c5 = mk_candles(6, step_ms=300000)
        agg = aggregate_candles(c5, "15m", drop_partial=True, base_ms=300000)
        assert len(agg) == 2
        # 7 Kerzen -> dritter Bucket unvollständig -> weiterhin 2
        c5 = mk_candles(7, step_ms=300000)
        agg = aggregate_candles(c5, "15m", drop_partial=True, base_ms=300000)
        assert len(agg) == 2

    def test_default_1m_unchanged(self):
        c = mk_candles(10)
        assert len(aggregate_candles(c, "5m", drop_partial=True)) == 2
        assert aggregate_candles(c, "1m") is c


# ---------------------------------------------------------------- Mapping ---
class TestHtfMapping:
    def test_map_last_closed_bucket(self):
        fs = fast_sim.FastSeries(mk_candles(32))
        sub, idx = fs.htf("5m")
        assert fs.base_tf_ms() == 60000
        # Kerzen 0-3: noch keine 5m-Kerze geschlossen
        assert idx[0] == -1 and idx[3] == -1
        # Kerze 4 schließt um Minute 5 -> Bucket 0 geschlossen
        assert idx[4] == 0 and idx[8] == 0
        assert idx[9] == 1
        # sub enthält nur geschlossene Buckets: 32 Kerzen -> 6 volle 5m-Buckets
        assert sub.n == 6

    def test_no_lookahead(self):
        candles = mk_candles(40, wave=False)  # streng steigend
        fs = fast_sim.FastSeries(candles)
        sub, idx = fs.htf("5m")
        for i in range(fs.n):
            if idx[i] < 0:
                continue
            # Schlusszeit der gemappten HTF-Kerze <= Schlusszeit der Basis-Kerze
            assert int(sub.ts[idx[i]]) + 300000 <= int(fs.ts[i]) + 60000


# --------------------------------------------------------------- RuleCond ---
class TestRuleCondOverride:
    def test_price_rule_on_5m(self):
        candles = mk_candles(30, wave=False)
        fs = fast_sim.FastSeries(candles)
        rule = {"indicator": "price", "op": ">", "value": 0, "timeframe": "5m"}
        cond = fast_sim._rule_cond(rule, fs, {})
        # vor der ersten geschlossenen 5m-Kerze: False, danach True
        assert not cond[:4].any()
        assert cond[4:].all()

    def test_same_tf_equals_base_path(self):
        candles = mk_candles(50)
        fs = fast_sim.FastSeries(candles)
        base = fast_sim._rule_cond({"indicator": "price", "op": ">", "value": 100}, fs, {})
        same = fast_sim._rule_cond({"indicator": "price", "op": ">", "value": 100,
                                    "timeframe": "1m"}, fs, {})
        assert np.array_equal(base, same)

    def test_invalid_tf_falls_back(self):
        # Basis 5m, Override 3m (niedriger) -> defensiver Fallback auf Basis-TF
        candles = mk_candles(30, step_ms=300000)
        fs = fast_sim.FastSeries(candles)
        base = fast_sim._rule_cond({"indicator": "price", "op": ">", "value": 0}, fs, {})
        low = fast_sim._rule_cond({"indicator": "price", "op": ">", "value": 0,
                                   "timeframe": "3m"}, fs, {})
        assert np.array_equal(base, low)

    def test_htf_matches_manual_aggregation(self):
        candles = mk_candles(120)
        fs = fast_sim.FastSeries(candles)
        rule = {"indicator": "price", "op": ">", "value": 100.0, "timeframe": "15m"}
        cond = fast_sim._rule_cond(rule, fs, {})
        agg = aggregate_candles(candles, "15m", drop_partial=True)
        expected = np.zeros(len(candles), dtype=bool)
        for i, c in enumerate(candles):
            closed = [b for b in agg if b["timestamp"] + 900000 <= c["timestamp"] + 60000]
            if closed:
                expected[i] = closed[-1]["close"] > 100.0
        assert np.array_equal(cond, expected)


# ----------------------------------------------------------------- Parität ---
class TestLiveFastParity:
    def _definition(self):
        return {"id": "mtf_test", "name": "MTF Test", "timeframe": "1m",
                "indicators": {"rsi_period": 14, "ema_slow_period": 20},
                "long_rules": [
                    {"indicator": "price", "op": ">", "value": 0},
                    {"indicator": "rsi", "op": "<", "value": 100, "timeframe": "15m"},
                ],
                "short_rules": [{"indicator": "rsi", "op": ">", "value": 999}]}

    def test_analyze_matches_provider(self):
        candles = mk_candles(800)
        strat = CustomStrategy(self._definition())
        assert strat.rule_problems == []
        fs = fast_sim.FastSeries(candles)
        provider = fast_sim.build_signal_provider(strat.effective_definition({}), fs)
        i = len(candles) - 1
        sig = provider(i)
        res = strat.analyze(candles, "BTCUSDT", {})
        assert res is not None
        assert (sig["type"] if sig else None) == res.get("signal_type") == "LONG"
        # Regel-Label enthält den TF-Hinweis
        labels = [r["label"] for r in res["rules"]]
        assert any("@15m" in (lb or "") for lb in labels)

    def test_no_override_backwards_compatible(self):
        d = self._definition()
        for r in d["long_rules"]:
            r.pop("timeframe", None)
        candles = mk_candles(400)
        strat = CustomStrategy(d)
        res = strat.analyze(candles, "BTCUSDT", {})
        assert res is not None and res.get("signal_type") == "LONG"


# ------------------------------------------------------------ Normalisierung ---
class TestNormalizeDefinition:
    def test_invalid_tf_rejected(self):
        d = {"timeframe": "1m", "long_rules": [
            {"indicator": "rsi", "op": "<", "value": 30, "timeframe": "7m"}]}
        _, problems = custom_params.normalize_definition(d, INDICATORS, OPERATORS)
        assert any("Timeframe" in p for p in problems)

    def test_lower_than_base_rejected(self):
        d = {"timeframe": "15m", "long_rules": [
            {"indicator": "rsi", "op": "<", "value": 30, "timeframe": "5m"}]}
        _, problems = custom_params.normalize_definition(d, INDICATORS, OPERATORS)
        assert any("Vielfaches" in p or "≥" in p for p in problems)

    def test_equal_base_removed_and_alias_normalized(self):
        d = {"timeframe": "5m", "long_rules": [
            {"indicator": "rsi", "op": "<", "value": 30, "timeframe": "5m"},
            {"indicator": "price", "op": ">", "value": 1, "timeframe": "24h"}]}
        norm, problems = custom_params.normalize_definition(d, INDICATORS, OPERATORS)
        assert problems == []
        assert "timeframe" not in norm["long_rules"][0]
        assert norm["long_rules"][1]["timeframe"] == "1d"

    def test_definition_without_tf_unchanged(self):
        d = {"timeframe": "1m", "long_rules": [{"indicator": "rsi", "op": "<", "value": 30}]}
        norm, problems = custom_params.normalize_definition(d, INDICATORS, OPERATORS)
        assert problems == []
        assert "timeframe" not in norm["long_rules"][0]


# ---------------------------------------------------------------- Optimizer ---
class TestOptimizerIntegration:
    def test_apply_params_sets_and_clears_tf(self):
        d = {"timeframe": "1m", "long_rules": [{"indicator": "rsi", "op": "<", "value": 30}],
             "short_rules": [{"indicator": "rsi", "op": ">", "value": 70,
                              "timeframe": "15m"}]}
        out = custom_params.apply_params(d, {"long1_tf": "1h", "short1_tf": "1m"})
        assert out["long_rules"][0]["timeframe"] == "1h"
        assert "timeframe" not in out["short_rules"][0]  # Basis-TF -> Override entfernt
        # ungültiger TF wird ignoriert
        out2 = custom_params.apply_params(d, {"long1_tf": "7m"})
        assert "timeframe" not in out2["long_rules"][0]

    def test_rule_timeframe_space(self):
        d = {"timeframe": "5m",
             "long_rules": [{"indicator": "rsi", "op": "<", "value": 30}],
             "short_rules": [{"indicator": "rsi", "op": ">", "value": 70}]}
        space = custom_params.rule_timeframe_space(d, ["15m", "1h", "3m"])
        assert set(space) == {"long1_tf", "short1_tf"}
        # Basis-TF immer erste Option; 3m (< Basis) gefiltert
        assert space["long1_tf"] == ["5m", "15m", "1h"]
        assert custom_params.rule_timeframe_space(d, []) == {}

    def test_get_params_passes_tf_keys(self):
        strat = CustomStrategy({"id": "x", "timeframe": "1m",
                                "long_rules": [{"indicator": "rsi", "op": "<", "value": 30}]})
        settings = {"strategy_params": {"x": {"long1_tf": "15m", "rsi_period": 10}}}
        p = strat.get_params(settings)
        assert p["long1_tf"] == "15m"
        eff = strat.effective_definition(p)
        assert eff["long_rules"][0]["timeframe"] == "15m"

    def test_build_candidates_tf_variants(self):
        from services import optimizer
        base = optimizer.build_candidates(["rsi"])
        with_tf = optimizer.build_candidates(["rsi"], ["15m", "1h"])
        assert len(with_tf) == len(base) * 3
        variant = with_tf[len(base)]
        assert variant["label"].endswith("@15m")
        assert variant["long"]["timeframe"] == "15m"
        assert variant["short"]["timeframe"] == "15m"
        # ohne tf_options: identisch wie bisher
        assert optimizer.build_candidates(["rsi"], None) == base

    def test_rule_text_shows_tf(self):
        assert custom_params.rule_text(
            {"indicator": "rsi", "op": "<", "value": 30, "timeframe": "1h"}) == "rsi < 30 @1h"
        assert custom_params.rule_text(
            {"indicator": "rsi", "op": "<", "value": 30}) == "rsi < 30"


# ------------------------------------------------------------------ Scanner ---
class TestScannerBuffer:
    def test_buffer_limit_with_rule_tf(self, monkeypatch):
        from services import strategy_scanner as sc_mod
        sc = sc_mod.StrategyScanner()

        class Dummy:
            STRATEGY_TIMEFRAME = "1m"
            definition = {"timeframe": "1m", "long_rules": [
                {"indicator": "rsi", "op": "<", "value": 30, "timeframe": "1d"}],
                "short_rules": []}

        monkeypatch.setattr(sc_mod.registry, "get", lambda sid: Dummy())
        sc.settings["enabled_strategies"] = ["dummy"]
        # 60 Kerzen * 1440 Min = 86400 -> Deckel 43200 (30 Tage)
        assert sc.buffer_limit() == 43200

    def test_buffer_limit_unchanged_without_tf(self, monkeypatch):
        from services import strategy_scanner as sc_mod
        sc = sc_mod.StrategyScanner()

        class Dummy:
            STRATEGY_TIMEFRAME = "1m"
            definition = {"timeframe": "1m",
                          "long_rules": [{"indicator": "rsi", "op": "<", "value": 30}],
                          "short_rules": []}

        monkeypatch.setattr(sc_mod.registry, "get", lambda sid: Dummy())
        sc.settings["enabled_strategies"] = ["dummy"]
        assert sc.buffer_limit() == 220  # exakt altes Verhalten


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
