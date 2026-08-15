"""Regressionstests der Regime-Engine v2 (services.regime_engine).

Geprüft wird, was in der Praxis schiefging: falsche Richtung, zu wenige
Regime, spät erkannte Wechsel, Lookahead und unplausible Labels.
"""
import numpy as np
import pytest

from services import regime as rg
from services import regime_engine as eng
from services import regime_features as rf

DAY_MS = 86400000


def make_candles(closes, vol_pct=0.8, seed=1):
    """Tageskerzen aus einer Close-Serie (High/Low aus vol_pct abgeleitet)."""
    rnd = np.random.RandomState(seed)
    out = []
    for i, c in enumerate(closes):
        rng_ = abs(c) * vol_pct / 100.0
        hi = c + abs(rnd.randn()) * rng_
        lo = c - abs(rnd.randn()) * rng_
        out.append({"timestamp": 1600000000000 + i * DAY_MS,
                    "open": float(closes[max(i - 1, 0)]), "high": float(max(hi, c)),
                    "low": float(min(lo, c)), "close": float(c), "volume": 1000.0})
    return out


def series(n, drift_pct_per_day, noise_pct, start=100.0, seed=7):
    rnd = np.random.RandomState(seed)
    r = drift_pct_per_day / 100.0 + rnd.randn(n) * noise_pct / 100.0
    return start * np.exp(np.cumsum(r))


def range_series(n, level=100.0, amp_pct=6.0, period=40, noise_pct=0.8, seed=7):
    """Echter Seitwärtsmarkt: Preis kehrt zum Mittelwert zurück (Range)."""
    rnd = np.random.RandomState(seed)
    x = np.arange(n)
    wave = np.sin(2 * np.pi * x / period) * amp_pct / 100.0
    noise = np.cumsum(rnd.randn(n) * noise_pct / 100.0)
    noise -= np.linspace(0, noise[-1], n)          # Drift entfernen (kein Trend)
    return level * np.exp(wave + noise * 0.5)


def labels_for(candles, config=None):
    model = eng.build_model({"X": candles}, "24h", config)
    assert model, "Modell konnte nicht gebaut werden"
    return model, eng.classify_series(model, candles)


def mode_of(model):
    return eng.norm_mode((model.get("config") or {}).get("regime_mode", 9))


def trend_share(labels, mode=None):
    """Anteil der Bars je Trendrichtung (down/side/up)."""
    m = eng.norm_mode(mode if mode is not None else eng.DEFAULT_REGIME_MODE)
    vals = [l for l in labels if l is not None]
    n = max(len(vals), 1)
    return {k: sum(1 for v in vals if eng.split_id(v, m)[0] == i) / n
            for i, k in enumerate(["down", "side", "up"])}


# ---------------------------------------------------------------- Primitive
def test_ols_stats_recovers_slope_and_tstat():
    y = np.arange(200, dtype=float) * 0.01
    slope, t, r2 = rf.ols_stats(y, 100)
    assert slope[-1] == pytest.approx(0.01, rel=1e-6)
    assert r2[-1] == pytest.approx(1.0, abs=1e-9)
    assert t[-1] > 100  # perfekte Gerade -> extrem signifikant


def test_ols_stats_flat_series_has_small_tstat():
    rnd = np.random.RandomState(3)
    y = np.cumsum(rnd.randn(500)) * 0.0  # exakt flach
    _s, t, _r = rf.ols_stats(y + 5.0, 100)
    assert abs(np.nan_to_num(t[-1])) < 1.0


def test_rolling_zscore_is_backward_only():
    x = np.concatenate([np.ones(200), np.ones(200) * 5.0])
    z = rf.rolling_zscore(x, 100)
    assert np.nan_to_num(z[150]) == pytest.approx(0.0, abs=1e-6)
    assert z[210] > 1.0


def test_adx_rises_in_trend_and_falls_in_chop():
    up = make_candles(series(300, 0.6, 0.5, seed=11), vol_pct=0.4)
    flat = make_candles(series(300, 0.0, 0.5, seed=11), vol_pct=0.4)
    h1, l1, c1, _ = rf.ohlc(up)
    h2, l2, c2, _ = rf.ohlc(flat)
    adx_up = np.nanmean(rf.adx_di(h1, l1, c1, 14)[0][100:])
    adx_flat = np.nanmean(rf.adx_di(h2, l2, c2, 14)[0][100:])
    assert adx_up > adx_flat


# ---------------------------------------------------------------- Richtung
def test_strong_uptrend_is_labelled_up():
    candles = make_candles(series(400, 0.5, 1.0, seed=21))
    _m, labels = labels_for(candles)
    sh = trend_share(labels)
    assert sh["up"] > 0.7, sh
    assert sh["down"] < 0.05, sh


def test_strong_downtrend_is_labelled_down():
    candles = make_candles(series(400, -0.6, 1.0, seed=22))
    _m, labels = labels_for(candles)
    sh = trend_share(labels)
    assert sh["down"] > 0.7, sh
    assert sh["up"] < 0.05, sh


def test_slow_long_decline_is_not_sideways():
    """Der alte Fehler: 500 Tage langsam fallend wurde als Seitwärts erkannt."""
    candles = make_candles(series(600, -0.12, 0.9, seed=23))
    _m, labels = labels_for(candles)
    sh = trend_share(labels)
    assert sh["down"] > 0.55, sh
    assert sh["up"] < 0.05, sh


def test_sideways_is_labelled_sideways():
    """Chop/Range (Rückkehr zum Mittelwert) darf nicht als Trend gelten."""
    candles = make_candles(range_series(400, amp_pct=3, period=15, seed=24))
    _m, labels = labels_for(candles)
    sh = trend_share(labels)
    assert sh["side"] > 0.6, sh


def test_random_walk_has_no_directional_bias():
    """Reiner Random-Walk: kein systematischer Auf-/Abwärts-Bias."""
    candles = make_candles(series(600, 0.0, 1.2, seed=124))
    _m, labels = labels_for(candles)
    sh = trend_share(labels)
    assert abs(sh["up"] - sh["down"]) < 0.45, sh
    assert sh["side"] > 0.2, sh


def test_regime_switch_is_detected_and_not_too_late():
    up = series(250, 0.55, 0.9, seed=31)
    down = series(250, -0.55, 0.9, start=float(up[-1]), seed=32)
    candles = make_candles(np.concatenate([up, down]))
    _m, labels = labels_for(candles)
    after = [l for l in labels[250:] if l is not None]
    first_down = next((i for i, l in enumerate(after)
                       if eng.split_id(l, mode_of(_m))[0] == 0), None)
    assert first_down is not None, "Abwärtsphase nicht erkannt"
    assert first_down < 60, f"Wechsel zu spät erkannt: {first_down} Tage"
    assert trend_share(labels[250:])["down"] > 0.5


def test_more_than_two_regimes_over_mixed_market():
    parts, last = [], 100.0
    for drift, noise, seed in [(0.5, 0.7, 41), (0.0, 0.7, 42), (-0.5, 2.5, 43),
                               (0.0, 3.0, 44), (0.6, 1.6, 45)]:
        s = series(220, drift, noise, start=last, seed=seed)
        parts.append(s)
        last = float(s[-1])
    candles = make_candles(np.concatenate(parts))
    model, labels = labels_for(candles)
    seen = {l for l in labels if l is not None}
    assert len(seen) >= 3, seen
    assert len(model["regimes"]) >= 3


def test_high_volatility_is_recognised():
    calm = series(300, 0.0, 0.6, seed=51)
    wild = series(300, 0.0, 4.0, start=float(calm[-1]), seed=52)
    candles = make_candles(np.concatenate([calm, wild]), vol_pct=1.0)
    _m, labels = labels_for(candles, {"regime_mode": 9})
    # kurz nach dem Vola-Sprung muss "hohe Volatilität" erkannt werden
    window = [eng.split_id(l, 9)[1] for l in labels[310:380] if l is not None]
    assert window and sum(1 for v in window if v == 2) / len(window) > 0.5, window


# ---------------------------------------------------------------- Kein Lookahead
def test_no_lookahead_prefix_stability():
    candles = make_candles(np.concatenate([series(200, 0.4, 1.0, seed=61),
                                           series(200, -0.4, 1.0, start=180.0, seed=62)]))
    model, full = labels_for(candles)
    for cut in (150, 250, 350):
        part = eng.classify_series(model, candles[:cut])
        assert part == full[:cut], f"Labels ändern sich rückwirkend bei cut={cut}"


def test_model_config_is_serialisable_and_stable():
    candles = make_candles(series(300, 0.3, 1.0, seed=63))
    model, _labels = labels_for(candles)
    import json
    json.dumps(model)
    assert model["engine"] == "v2"
    assert all("nnfx" in r for r in model["regimes"])


# ---------------------------------------------------------------- Hysterese
def test_hysteresis_reduces_switching():
    candles = make_candles(series(500, 0.0, 2.0, seed=71))
    _m1, few = labels_for(candles, {"hysteresis": 0.4, "confirm_days": 3,
                                    "min_hold_days": 10})
    _m2, many = labels_for(candles, {"hysteresis": 0.0, "confirm_days": 0,
                                     "min_hold_days": 0, "confidence_min": 0.0})
    assert len(eng.segments_from_labels(few)) <= len(eng.segments_from_labels(many))


def test_min_hold_days_is_respected():
    candles = make_candles(series(600, 0.0, 2.5, seed=72))
    model, labels = labels_for(candles, {"min_hold_days": 20, "confirm_days": 2})
    segs = eng.segments_from_labels(labels)
    inner = segs[1:-1]
    assert all((e - s) >= 20 for (s, e, _r) in inner), \
        [(e - s) for (s, e, _r) in inner]


# ---------------------------------------------------------------- Validierung
@pytest.mark.parametrize("drift,noise,seed", [(0.5, 1.0, 81), (-0.5, 1.0, 82),
                                              (-0.1, 0.8, 84), (0.25, 2.0, 86)])
def test_validation_passes_on_clean_series(drift, noise, seed):
    candles = make_candles(series(500, drift, noise, seed=seed))
    model, labels = labels_for(candles)
    rep = eng.validate_labels(candles, labels, model)
    assert rep["passed"], rep["violations"][:3]


def test_validation_flags_wrong_labels():
    candles = make_candles(series(300, -0.6, 0.8, seed=85))
    model, labels = labels_for(candles)
    wrong = [eng.regime_id(2, 1, mode_of(model)) if l is not None else None
             for l in labels]
    rep = eng.validate_labels(candles, wrong, model)
    assert not rep["passed"]
    assert rep["violation_count"] > 0


def test_ideal_labels_agree_with_live_direction():
    candles = make_candles(np.concatenate([series(250, 0.5, 0.9, seed=91),
                                           series(250, -0.5, 0.9, start=250.0, seed=92)]))
    model, labels = labels_for(candles)
    ideal = eng.ideal_labels(model, candles)
    agr = eng.agreement_with_ideal(labels, ideal, mode_of(model))
    assert agr["direction_pct"] > 60, agr


# ---------------------------------------------------------------- Integration
def test_regime_module_dispatches_to_v2_by_default():
    candles = make_candles(series(300, 0.4, 1.0, seed=101))
    model = rg.detect_regimes({"X": candles}, "24h")
    assert rg.is_v2(model)
    labels = rg.classify_series(model, candles, "24h")
    cur = rg.current_regime(model, candles, "24h")
    assert cur["regime"] is not None
    assert cur["nnfx"] in ("trend", "range", "breakout")
    assert len([l for l in labels if l is not None]) > 100


def test_kmeans_engine_still_available():
    candles = make_candles(series(400, 0.3, 1.2, seed=102))
    model = rg.detect_regimes({"X": candles}, "24h", engine="kmeans")
    assert model and not rg.is_v2(model)
    labels = rg.classify_series(model, candles, "24h")
    assert any(l is not None for l in labels)


def test_early_warning_signals_reversal_before_switch():
    """Frühwarnung muss den Wechsel vor der offiziellen Umschaltung anzeigen."""
    up = series(250, 0.55, 0.9, seed=131)
    down = series(120, -0.7, 0.9, start=float(up[-1]), seed=132)
    candles = make_candles(np.concatenate([up, down]))
    model, labels = labels_for(candles)
    # erster Bar, an dem das Label offiziell auf Abwärts wechselt
    switch = next((i for i in range(250, len(labels))
                   if labels[i] is not None
                   and eng.split_id(labels[i], mode_of(model))[0] == 0), None)
    assert switch is not None, "Wechsel wurde nicht erkannt"
    warned = None
    for i in range(250, switch):
        cur = eng.current_regime(model, candles[:i + 1])
        w = cur.get("early_warning") or {}
        if w.get("active") and eng.split_id(w["next_regime"], mode_of(model))[0] == 0:
            warned = i
            break
    assert warned is not None, "keine Frühwarnung vor dem Wechsel"
    assert warned < switch, (warned, switch)


def test_early_warning_quiet_in_stable_trend():
    """In einem stabilen Trend darf keine hohe Wechsel-Wahrscheinlichkeit gemeldet werden."""
    candles = make_candles(series(400, 0.5, 0.7, seed=133))
    model, _labels = labels_for(candles)
    cur = eng.current_regime(model, candles)
    w = cur.get("early_warning") or {}
    assert w.get("probability_pct", 0) < 70, w


def test_early_warning_fields_are_serialisable():
    import json
    candles = make_candles(series(300, -0.3, 1.2, seed=134))
    model, _l = labels_for(candles)
    cur = eng.current_regime(model, candles)
    json.dumps(cur)
    w = cur["early_warning"]
    for k in ("active", "next_regime", "probability_pct", "pending", "reason"):
        assert k in w


def test_nnfx_mapping_covers_all_regimes():
    ids = [t["id"] for t in eng.taxonomy(9)]
    assert len(ids) == 9
    assert {eng.nnfx_regime(i, 9) for i in ids} == {"trend", "range", "breakout"}


def test_scenario_bank_all_plausible():
    """Alle synthetischen Märkte: erwartete Richtung dominiert und die
    Plausibilitätsprüfung ist bestanden."""
    from tests.regime_scenarios import scenarios
    problems = []
    for name, (candles, expect) in scenarios().items():
        model = eng.build_model({"X": candles}, "24h")
        labels = eng.classify_series(model, candles)
        rep = eng.validate_labels(candles, labels, model)
        sh = trend_share(labels, mode_of(model))
        if not rep["passed"]:
            problems.append(f"{name}: Validierung {rep['violation_bars_pct']}%")
        if expect and sh[expect] < 0.55:
            problems.append(f"{name}: {expect} nur {sh[expect]:.2f}")
    assert not problems, problems


def test_performance_on_intraday_history():
    """Laufzeit-Schutz: 5m-Kerzen über ~2 Jahre müssen zügig klassifiziert werden."""
    import time
    n = 200_000
    closes = series(n, 0.0005, 0.05, seed=201)
    candles = make_candles(closes, vol_pct=0.1)
    t0 = time.time()
    model = eng.build_model({"X": candles}, "5m")
    labels = eng.classify_series(model, candles)
    dt = time.time() - t0
    assert len([x for x in labels if x is not None]) > n * 0.5
    assert dt < 60, f"zu langsam: {dt:.1f}s"
