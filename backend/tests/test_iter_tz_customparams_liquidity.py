"""Regressionstests dieser Iteration (rein, ohne Netz/DB):

  1. core.timeutil          – deutsche Zeit (Europe/Berlin) inkl. naive Altdaten
  2. strategies.custom_params – Alias-Normalisierung, Suchraum, apply_params
  3. CustomStrategy         – neue Indikatoren + Parameter wirken wirklich
  4. services.liquidity_levels – Liquidity Levels & Heatmap (X-Ray-Äquivalent)
"""
import math
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import timeutil  # noqa: E402
from services import liquidity_levels as ll  # noqa: E402
from strategies import custom_params as cp  # noqa: E402
from strategies.custom_strategy import CustomStrategy, INDICATORS, OPERATORS  # noqa: E402


# --------------------------------------------------------------------- timeutil
class TestTimeUtil:
    def test_naive_iso_is_utc(self):
        dt = timeutil.parse_iso("2026-01-15T12:00:00")
        assert dt.tzinfo is not None
        assert dt.utcoffset().total_seconds() == 0

    def test_winter_time_is_utc_plus_1(self):
        assert timeutil.fmt_berlin("2026-01-15T12:00:00+00:00") == "15.01.2026 13:00"

    def test_summer_time_is_utc_plus_2(self):
        assert timeutil.fmt_berlin("2026-07-15T12:00:00+00:00") == "15.07.2026 14:00"

    def test_berlin_date_rolls_over_before_utc_midnight(self):
        # 22:30 UTC im Juli = 00:30 deutscher Zeit des Folgetages
        assert timeutil.berlin_date("2026-07-15T22:30:00+00:00") == "2026-07-16"

    def test_berlin_minutes(self):
        assert timeutil.berlin_minutes("2026-07-15T12:00:00+00:00") == 14 * 60

    def test_now_iso_has_offset(self):
        assert timeutil.parse_iso(timeutil.now_iso()).tzinfo is not None
        assert timeutil.now_berlin().tzinfo is not None
        assert isinstance(timeutil.now_utc(), datetime)
        assert timeutil.now_utc().tzinfo == timezone.utc

    def test_fallback_for_garbage(self):
        assert timeutil.fmt_berlin("keine-zeit", fallback="-") == "-"
        assert timeutil.to_berlin(None) is None


# ------------------------------------------------------------------ custom_params
def _ai_definition():
    return {
        "id": "custom_ai_test", "name": "KI Kandidat", "timeframe": "5m",
        "indicators": {"rsi_period": 14, "ema_fast_period": 9, "ema_slow_period": 50},
        "long_rules": [
            {"indicator": "rsi", "op": "lt", "value": 35},
            {"indicator": "close", "op": "above", "value": "ema9"},
            {"indicator": "adx14", "op": ">", "value": 20},
        ],
        "short_rules": [{"indicator": "rsi", "op": "greater_than", "value": 65}],
    }


class TestNormalizeDefinition:
    def test_aliases_resolved(self):
        d, problems = cp.normalize_definition(_ai_definition(), INDICATORS, OPERATORS)
        assert problems == []
        assert d["long_rules"][0]["op"] == "<"
        assert d["long_rules"][1]["indicator"] == "price"
        assert d["long_rules"][1]["value"] == "ema_fast"
        assert d["long_rules"][2]["indicator"] == "adx"
        assert d["short_rules"][0]["op"] == ">"

    def test_unknown_indicator_is_reported_not_silent(self):
        bad = {"id": "x", "long_rules": [{"indicator": "supertrend", "op": ">", "value": 1}]}
        _, problems = cp.normalize_definition(bad, INDICATORS, OPERATORS)
        assert problems and "supertrend" in problems[0]

    def test_original_definition_untouched(self):
        src = _ai_definition()
        cp.normalize_definition(src, INDICATORS, OPERATORS)
        assert src["long_rules"][0]["op"] == "lt"


class TestParamSpace:
    def test_periods_and_thresholds_present(self):
        s = CustomStrategy(_ai_definition())
        keys = set(s.DEFAULT_PARAMS)
        assert {"rsi_period", "ema_fast_period", "adx_period"} <= keys
        assert {"long1_value", "long3_value", "short1_value"} <= keys

    def test_meta_is_optimizer_compatible(self):
        from services.optimizer import strategy_param_space
        s = CustomStrategy(_ai_definition())
        space = strategy_param_space(s)
        assert space, "Optimizer-Suchraum darf für KI-Strategien nicht leer sein"
        for key, values in space.items():
            assert len(values) >= 2, f"{key} hat keinen echten Suchraum"

    def test_indicator_only_rules_have_no_threshold_param(self):
        s = CustomStrategy(_ai_definition())
        assert "long2_value" not in s.DEFAULT_PARAMS  # value ist ein Indikator

    def test_max_params_capped(self):
        rules = [{"indicator": "rsi", "op": "<", "value": 30 + i} for i in range(20)]
        s = CustomStrategy({"id": "x", "long_rules": rules})
        assert len(s.DEFAULT_PARAMS) <= cp.MAX_PARAMS

    def test_apply_params_changes_rules_and_periods(self):
        s = CustomStrategy(_ai_definition())
        eff = s.effective_definition({"rsi_period": 7, "long1_value": 25})
        assert eff["indicators"]["rsi_period"] == 7
        assert eff["long_rules"][0]["value"] == 25
        # Original bleibt unverändert (Rückwärtskompatibilität)
        assert s.definition["long_rules"][0]["value"] == 35

    def test_apply_params_without_params_returns_same(self):
        s = CustomStrategy(_ai_definition())
        assert s.effective_definition({}) is s.definition


# ------------------------------------------------------- CustomStrategy Auswertung
def _synthetic_candles(n=300, start=100.0, step=0.35):
    out, price = [], start
    for i in range(n):
        price += step if (i // 20) % 2 == 0 else -step
        out.append({"timestamp": 1700000000000 + i * 60000,
                    "open": price - 0.1, "high": price + 0.5,
                    "low": price - 0.5, "close": price, "volume": 100 + i % 30})
    return out


class TestCustomStrategyIndicators:
    def test_adx_rules_are_evaluated(self):
        s = CustomStrategy({"id": "x", "name": "adx",
                            "indicators": {"adx_period": 14},
                            "long_rules": [{"indicator": "adx", "op": ">", "value": 1}]})
        res = s.analyze(_synthetic_candles(), "BTCUSDT", {})
        assert res is not None
        assert res["rules"][0]["long"] is True, "ADX muss auswertbar sein (vorher immer False)"

    def test_keltner_and_donchian_available(self):
        s = CustomStrategy({"id": "x", "name": "kc",
                            "long_rules": [{"indicator": "price", "op": ">", "value": "keltner_lower"},
                                           {"indicator": "price", "op": "<", "value": "donchian_high"}]})
        res = s.analyze(_synthetic_candles(), "BTCUSDT", {})
        assert res is not None
        assert all(r["long"] in (True, False) for r in res["rules"])

    def test_threshold_param_changes_signal(self):
        s = CustomStrategy({"id": "x", "name": "rsi",
                            "indicators": {"rsi_period": 14},
                            "long_rules": [{"indicator": "rsi", "op": "<", "value": 1}]})
        candles = _synthetic_candles()
        assert s.analyze(candles, "BTCUSDT", {})["rules"][0]["long"] is False
        assert s.analyze(candles, "BTCUSDT", {"long1_value": 99})["rules"][0]["long"] is True

    def test_fast_path_and_reference_path_agree(self):
        from services import fast_sim
        definition = {"id": "x", "name": "cmp", "indicators": {"rsi_period": 14, "adx_period": 14},
                      "long_rules": [{"indicator": "adx", "op": ">", "value": 5},
                                     {"indicator": "rsi", "op": "<", "value": 60}]}
        s = CustomStrategy(definition)
        candles = _synthetic_candles()
        params = {"long2_value": 45}
        fs = fast_sim.FastSeries(candles)
        provider = fast_sim.provider_for(
            s, fs, {"strategy_params": {"x": params}}, "BTCUSDT")
        fast_signal = provider(len(candles) - 1)
        ref = s.analyze(candles, "BTCUSDT", params)
        ref_long = bool(ref.get("signal_type") == "LONG")
        assert bool(fast_signal and fast_signal.get("type") == "LONG") == ref_long


# --------------------------------------------------------------- liquidity levels
class TestLiquidityLevels:
    def test_swing_pivots_found(self):
        candles = _synthetic_candles(120)
        pivots = ll.swing_pivots(candles)
        assert any(p["type"] == "swing_high" for p in pivots)
        assert any(p["type"] == "swing_low" for p in pivots)

    def test_equal_levels_cluster(self):
        pivots = [{"index": i, "price": 100.0, "type": "swing_high"} for i in range(3)]
        eq = ll.equal_levels(pivots)
        assert eq and eq[0]["type"] == "eqh" and eq[0]["touches"] == 3

    def test_volume_profile_value_area(self):
        prof = ll.volume_profile(_synthetic_candles(200))
        assert prof["val"] <= prof["poc"] <= prof["vah"]

    def test_levels_scored_and_sorted(self):
        out = ll.liquidity_levels(_synthetic_candles(200))
        assert out["levels"]
        strengths = [x["strength"] for x in out["levels"]]
        assert strengths == sorted(strengths, reverse=True)
        for lvl in out["levels"]:
            assert 0 <= lvl["strength"] <= 100
            assert lvl["side"] in ("above", "below")

    def test_untested_pivot_detection(self):
        candles = _synthetic_candles(60)
        pivots = ll.swing_pivots(candles)
        untested = ll.untested_pivots(candles, pivots)
        for p in untested:
            later = candles[p["index"] + 1:]
            if p["type"] == "swing_high":
                assert all(c["high"] < p["price"] for c in later)

    def test_heatmap_bins_normalised(self):
        clusters = {"below_price": [{"price": 90.0, "est_leverage": "50x", "strength": "high"}],
                    "above_price": [{"price": 130.0, "est_leverage": "50x", "strength": "high"}]}
        hm = ll.heatmap(_synthetic_candles(200), clusters, bins=20)
        assert len(hm["bins"]) == 20
        assert math.isclose(max(b["heat"] for b in hm["bins"]), 1.0, rel_tol=1e-6)
        assert any("liq" in " ".join(b["tags"]) for b in hm["bins"])

    def test_empty_candles_safe(self):
        assert ll.liquidity_levels([])["levels"] == []
        assert ll.heatmap([], {})["bins"] == []


# ----------------------------------------------------------------- clear scopes
class TestClearScopes:
    def test_strategy_scope_filter(self):
        from routers.analytics import _clear_scope_filter, CLEAR_SCOPES
        assert "strategy" in CLEAR_SCOPES
        assert _clear_scope_filter("strategy", None, "sc") == {"strategy_id": "sc"}
        assert _clear_scope_filter("coin_strategy", "BTCUSDT", "sc") == {
            "symbol": "BTCUSDT", "strategy_id": "sc"}
        assert _clear_scope_filter("all") == {}

    def test_strategy_scope_requires_strategy_id(self):
        from fastapi import HTTPException
        from routers.analytics import _validate_clear
        with pytest.raises(HTTPException):
            _validate_clear("all", "strategy", None, None)
        _validate_clear("all", "strategy", None, "sc")  # darf nicht werfen
