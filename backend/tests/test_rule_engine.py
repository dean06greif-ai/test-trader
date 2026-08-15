"""Regressionstests: erweiterte Rule-Engine (dynamische Indikatoren, Zeit-Filter,
Mathe-Ausdrücke, in_range/not_in_range, Auto-Fix-Vorschläge)."""
import numpy as np
import pytest

from services import fast_sim
from strategies import custom_params, rule_engine
from strategies.custom_strategy import CustomStrategy, INDICATORS, OPERATORS


def make_candles(n=400, start=100.0):
    rng = np.random.default_rng(7)
    closes = start + np.cumsum(rng.normal(0, 0.5, n))
    out = []
    t0 = 1700000000000
    for i, c in enumerate(closes):
        out.append({"timestamp": t0 + i * 60000, "open": c - 0.1, "high": c + 0.3,
                    "low": c - 0.3, "close": c, "volume": 1000 + i})
    return out


def test_dynamic_indicator_canonicalization():
    assert custom_params.canonical_indicator("ema_200") == "ema(200)"
    assert custom_params.canonical_indicator("EMA(200)") == "ema(200)"
    assert custom_params.canonical_indicator("low_20") == "recent_low(20)"
    assert custom_params.canonical_indicator("high_20") == "recent_high(20)"
    assert custom_params.canonical_indicator("rsi_7") == "rsi(7)"
    assert custom_params.canonical_indicator("time") == "hour"
    assert custom_params.canonical_indicator("volatility_pct") == "atr_pct"
    # bestehende Aliase unverändert
    assert custom_params.canonical_indicator("close") == "price"
    assert custom_params.canonical_indicator("adx14") == "adx"


def test_normalize_accepts_ai_style_definition():
    d = {"long_rules": [
        {"indicator": "ema_200", "op": "crosses_above", "value": "close"},
        {"indicator": "time", "op": "not_in_range", "value": "22-6"},
        {"indicator": "volatility_pct * price", "op": ">", "value": 10},
        {"indicator": "price", "op": "<", "value": "low_20"},
    ]}
    norm, problems = custom_params.normalize_definition(d, INDICATORS, OPERATORS)
    assert problems == []
    rules = norm["long_rules"]
    assert rules[0]["indicator"] == "ema(200)"
    assert rules[0]["op"] == "cross_above"
    assert rules[0]["value"] == "price"
    assert rules[1]["indicator"] == "hour"
    assert rules[1]["value"] == [22.0, 6.0]
    assert rules[2]["indicator"] == "atr_pct * price"
    assert rules[3]["value"] == "recent_low(20)"


def test_fast_sim_dynamic_series_and_hour():
    fs = fast_sim.FastSeries(make_candles())
    ema200 = fs.get("ema(200)", {})
    assert len(ema200) == fs.n and not np.isnan(ema200[-1])
    hour = fs.get("hour", {})
    assert 0 <= hour[-1] <= 23
    rl = fs.get("recent_low(20)", {})
    assert not np.isnan(rl[-1])


def test_rule_cond_expression_and_range():
    fs = fast_sim.FastSeries(make_candles())
    # Ausdruck: price > sma(20) - 1000  -> immer wahr nach Warmup
    c = fast_sim._rule_cond({"indicator": "price", "op": ">",
                             "value": "sma(20) - 1000"}, fs, {})
    assert c[-1]
    # hour in_range 0-23 -> immer wahr
    c2 = fast_sim._rule_cond({"indicator": "hour", "op": "in_range",
                              "value": [0, 23]}, fs, {})
    assert c2.all()
    c3 = fast_sim._rule_cond({"indicator": "hour", "op": "not_in_range",
                              "value": [0, 23]}, fs, {})
    assert not c3.any()


def test_custom_strategy_analyze_fires_with_dynamic_rules():
    d = {"id": "t1", "name": "T", "long_rules": [
        {"indicator": "price", "op": ">", "value": "ema(30) - 1000"},
        {"indicator": "hour", "op": "in_range", "value": [0, 23]},
    ], "short_rules": []}
    s = CustomStrategy(d)
    assert s.rule_problems == []
    out = s.analyze(make_candles(), "BTCUSDT", {})
    assert out is not None
    assert out["signal_type"] == "LONG"
    assert out["long_count"] == 2


def test_backwards_compat_plain_rules_still_work():
    d = {"id": "t2", "name": "T2",
         "long_rules": [{"indicator": "rsi", "op": "<", "value": 101}],
         "short_rules": [{"indicator": "rsi", "op": ">", "value": 101}]}
    s = CustomStrategy(d)
    out = s.analyze(make_candles(), "BTCUSDT", {})
    assert out is not None and out["signal_type"] == "LONG"
    # Fast-Path liefert dieselbe Entscheidung
    fs = fast_sim.FastSeries(make_candles())
    provider = fast_sim.build_signal_provider(s.definition, fs)
    assert provider(fs.n - 1) is not None


def test_fix_suggestions_for_unknown_tokens():
    d = {"long_rules": [
        {"indicator": "emaa_200", "op": "biggerthan", "value": 5},
        {"indicator": "volatilty_pct * price", "op": ">", "value": 1},
    ]}
    fixes = custom_params.fix_suggestions(d, INDICATORS, OPERATORS)
    by_field = {(f["index"], f["field"]): f["to"] for f in fixes}
    assert by_field.get((0, "indicator")) == "ema(200)"
    assert by_field.get((0, "op")) in (">", ">=")
    assert "atr_pct" in by_field.get((1, "indicator"), "")


def test_expression_parser_edge_cases():
    rpn = rule_engine.parse_expression("bb_middle + 2 * atr")
    fs = fast_sim.FastSeries(make_candles())
    arr = rule_engine.eval_rpn(rpn, lambda n: fs.get(n, {}))
    assert arr is not None and not np.isnan(arr[-1])
    assert rule_engine.parse_expression("((") is None
    assert rule_engine.parse_range("9-17") == (9.0, 17.0)
    assert rule_engine.parse_range({"start": 22, "end": 6}) == (22.0, 6.0)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
