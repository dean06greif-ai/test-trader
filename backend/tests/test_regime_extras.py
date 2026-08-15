"""Tests für die Erweiterungen: Regime-Farbschema-Daten, Frühwarnung,
Strategie-Parameter je Regime (Suchraum + Settings-Weitergabe)."""
import numpy as np
import pytest

from services import dynamic_strategy as dyn
from services import regime_engine as eng
from services.optimizer import strategy_param_space
from strategies.registry import registry
from tests.test_regime_engine import labels_for, make_candles, series


# ------------------------------------------------- Farbschema (Datenseite)
def test_regimes_carry_trend_and_vol_keys_for_colors():
    """Das Frontend färbt nach trend (rot/gelb/grün); im 9er-Modus zusätzlich
    nach vol (Intensität), im 3er/5er-Modus gibt es keine Vola-Achse."""
    candles = make_candles(series(400, 0.4, 1.2, seed=401))
    model, _l = labels_for(candles)
    mode = eng.norm_mode(model["config"]["regime_mode"])
    for r in model["regimes"]:
        assert r["trend"] in ("down", "side", "up")
        t, v = eng.split_id(r["id"], mode)
        assert ["down", "side", "up"][t] == r["trend"]
        if mode == 9:
            assert r["vol"] in ("low", "mid", "high")
            assert ["low", "mid", "high"][v] == r["vol"]
        else:
            assert "vol" not in r


@pytest.mark.parametrize("mode", [3, 5, 9])
def test_taxonomy_is_consistent_per_mode(mode):
    """Jeder Modus liefert genau `mode` Regime mit eindeutigen IDs 0..mode-1."""
    tax = eng.taxonomy(mode)
    assert len(tax) == mode
    assert [t["id"] for t in tax] == list(range(mode))
    for t in tax:
        assert t["label"]
        assert t["nnfx"] in ("trend", "range", "breakout")


@pytest.mark.parametrize("mode", [3, 5, 9])
def test_model_respects_regime_mode(mode):
    candles = make_candles(series(600, 0.3, 1.1, seed=411))
    model = eng.build_model({"X": candles}, "24h", {"regime_mode": mode})
    assert model is not None
    assert model["regime_mode"] == mode
    assert all(0 <= r["id"] < mode for r in model["regimes"])
    labels = eng.classify_series(model, candles)
    assert all(x is None or 0 <= x < mode for x in labels)


# ------------------------------------------------- Frühwarnung
def test_early_warning_points_to_correct_direction():
    up = series(200, 0.5, 0.8, seed=402)
    down = series(80, -0.8, 0.8, start=float(up[-1]), seed=403)
    candles = make_candles(np.concatenate([up, down]))
    model, _l = labels_for(candles)
    mode = eng.norm_mode(model["config"]["regime_mode"])
    hits = 0
    for i in range(205, 240):
        w = eng.current_regime(model, candles[:i + 1]).get("early_warning") or {}
        if w.get("active") and eng.split_id(w["next_regime"], mode)[0] == 0:
            hits += 1
    assert hits > 0


def test_early_warning_eta_is_positive_or_none():
    candles = make_candles(series(300, -0.4, 1.0, seed=404))
    model, _l = labels_for(candles)
    w = eng.current_regime(model, candles)["early_warning"]
    assert w["eta_days"] is None or w["eta_days"] > 0
    assert 0 <= w["probability_pct"] <= 99


# ------------------------------------------------- Strategie-Parameter je Regime
@pytest.mark.parametrize("sid", ["nnfx_trend", "nnfx_reversion", "nnfx_breakout"])
def test_strategy_param_space_is_searchable(sid):
    space = strategy_param_space(registry.get(sid))
    assert len(space) >= 8
    for k, vals in space.items():
        assert len(vals) >= 1 and len(vals) <= 60
        assert all(isinstance(v, (int, float)) for v in vals)
    # Regime-Suche lässt Ein/Aus-Schalter weg (sonst nur Leerläufe)
    lean = strategy_param_space(registry.get(sid), skip_binary=True)
    assert "allow_long" in space and "allow_long" not in lean
    assert len(lean) < len(space)


def test_with_strategy_params_is_pure_and_nested():
    base = {"strategy_params": {"nnfx_trend": {"adx_min": 20}}, "other": 1}
    out = dyn.with_strategy_params(base, "nnfx_trend", {"adx_min": 30, "macd_fast": 8})
    assert base["strategy_params"]["nnfx_trend"]["adx_min"] == 20  # Original bleibt
    assert out["strategy_params"]["nnfx_trend"] == {"adx_min": 30, "macd_fast": 8}
    assert out["other"] == 1
    assert dyn.with_strategy_params(base, "nnfx_trend", {}) is base


def test_strategy_params_change_signal_count():
    """Übergebene Strategie-Parameter müssen wirklich in der Simulation ankommen."""
    from services.fast_sim import FastSeries
    s = registry.get("nnfx_breakout")
    fs = FastSeries(make_candles(series(400, 0.45, 1.2, seed=405)))
    a = s.vectorized_signals(fs, s.get_params({"strategy_params": {
        "nnfx_breakout": {"donchian_period": 10, "atr_expand": 1.0}}}, "BTCUSDT"))
    b = s.vectorized_signals(fs, s.get_params({"strategy_params": {
        "nnfx_breakout": {"donchian_period": 120, "atr_expand": 2.0}}}, "BTCUSDT"))
    assert int(a["long"].sum()) != int(b["long"].sum())


def test_provider_cache_key_includes_strategy_params():
    """Regression: verschiedene Parameter-Kandidaten dürfen NICHT denselben
    Signal-Provider aus dem Segment-Cache wiederverwenden."""
    s = registry.get("nnfx_trend")
    k0 = dyn._def_key(s, {}, "BTCUSDT")
    k1 = dyn._def_key(s, {"strategy_params": {"nnfx_trend": {"adx_min": 40}}}, "BTCUSDT")
    k2 = dyn._def_key(s, {"strategy_params": {"nnfx_trend": {"adx_min": 10}}}, "BTCUSDT")
    assert k0 != k1 and k1 != k2
    kc = dyn._def_key(s, {"coin_params": {"nnfx_trend": {"BTCUSDT": {"adx_min": 10}}}},
                      "BTCUSDT")
    assert kc != k0
    assert dyn._def_key(s, {"coin_params": {"nnfx_trend": {"ETHUSDT": {"adx_min": 10}}}},
                        "BTCUSDT") == k0


def test_provider_cache_returns_different_signals_per_params():
    candles = make_candles(series(300, 0.45, 1.0, seed=407))
    seg = {"candles": candles}
    p_off = dyn.provider_for_seg(registry.get("nnfx_trend"), seg,
                                 {"strategy_params": {"nnfx_trend": {"allow_long": 0,
                                                                     "allow_short": 0}}},
                                 "BTCUSDT")
    p_on = dyn.provider_for_seg(registry.get("nnfx_trend"), seg, {}, "BTCUSDT")
    n_off = sum(1 for i in range(len(candles)) if p_off(i))
    n_on = sum(1 for i in range(len(candles)) if p_on(i))
    assert n_off == 0 and n_on > 0


def test_rows_for_accepts_callable_settings():
    """_rows_for muss Settings je Segment auflösen (Regime-spezifische Parameter)."""
    import asyncio
    candles = make_candles(series(200, 0.3, 1.0, seed=406))
    seg = {"_key": ("BTCUSDT", "24h", 0), "regime": 3, "start_ts": candles[0]["timestamp"],
           "candles": candles, "bars": len(candles)}
    seen = []

    def settings_for(s):
        seen.append(s["regime"])
        return {"strategy_params": {}}

    async def run():
        return await dyn._rows_for(registry.get("nnfx_trend"), [("BTCUSDT", seg)],
                                   settings_for, lambda _s: {}, lambda: False)

    asyncio.run(run())
    assert seen == [3]
