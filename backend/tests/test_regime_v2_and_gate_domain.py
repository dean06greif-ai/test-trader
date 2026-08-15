"""Tests Fix 0.7a + 0.7b: Gate-Domain (krypto-only) und Regime v2.

0.7a: shadow_predict/shadow_report werten nur noch Krypto (Trainings-Domain).
0.7b: Vol-Label per Symbol-Perzentil, Trend-Hysterese, breakout nur mit
Bestätigung (sonst drift), Mehr-Horizont-Trend (1d/3d).
"""
import math
import sys

sys.path.insert(0, "/app/backend")

from services import ai_market_observer as obs  # noqa: E402
from services.ml_gate import MLGate, DEFAULT_SETTINGS  # noqa: E402


def _candles(n, step=0.0, base=100.0, noise=0.0, vol_spike_last=None):
    out = []
    price = base
    for i in range(n):
        price = price + step + (noise * math.sin(i * 1.7))
        vol = 10.0
        if vol_spike_last and i >= n - vol_spike_last:
            vol = 40.0
        out.append({"open": price, "high": price + 0.05, "low": price - 0.05,
                    "close": price, "volume": vol})
    return out


# ---------- 0.7b: classify_regime_v2 (rein) ----------

def test_v2_vol_percentile_bases():
    assert obs.classify_regime_v2(0.0, 0.02, 50, vol_rank=95.0) == "range_volatil"
    assert obs.classify_regime_v2(0.0, 0.02, 50, vol_rank=10.0) == "range_ruhig"
    assert obs.classify_regime_v2(0.0, 0.02, 50, vol_rank=55.0) == "range_normal"


def test_v2_fallback_fixed_thresholds_without_rank():
    # ohne Historie: identische Vol-Basis wie v1
    assert obs.classify_regime_v2(0.0, 0.5, 50).endswith("volatil")
    assert obs.classify_regime_v2(0.0, 0.01, 50).endswith("ruhig")


def test_v2_trend_hysteresis():
    # Eintritt wie v1 ab 0.08
    assert obs.classify_regime_v2(0.09, 0.1, 50).startswith("trend_up")
    # 0.06 ohne Vorzustand: kein Trend
    assert obs.classify_regime_v2(0.06, 0.1, 50).startswith("range")
    # 0.06 mit bestehendem trend_up: bleibt im Trend (Hysterese)
    assert obs.classify_regime_v2(0.06, 0.1, 50, prev_regime="trend_up_normal").startswith("trend_up")
    # unter 0.05: Trend wird verlassen
    assert obs.classify_regime_v2(0.04, 0.1, 50, prev_regime="trend_up_normal").startswith("range")
    # dito short
    assert obs.classify_regime_v2(-0.06, 0.1, 50, prev_regime="trend_down_ruhig").startswith("trend_down")


def test_v2_breakout_needs_confirmation():
    # Rand oben, aber kaum Bewegung + kein Volumen -> drift (B2-Fix)
    assert obs.classify_regime_v2(0.0, 0.1, 95, change_60m_pct=0.02,
                                  volume_ratio=1.0).startswith("drift_")
    # Rand oben + starker 60m-Move in Randrichtung -> breakout
    assert obs.classify_regime_v2(0.0, 0.1, 95, change_60m_pct=1.0,
                                  volume_ratio=1.0).startswith("breakout_")
    # Rand oben + Volumen-Spike -> breakout
    assert obs.classify_regime_v2(0.0, 0.1, 95, change_60m_pct=0.06,
                                  volume_ratio=2.0).startswith("breakout_")
    # Rand oben, aber Bewegung NACH UNTEN (falsche Richtung) -> drift
    assert obs.classify_regime_v2(0.0, 0.1, 95, change_60m_pct=-1.0,
                                  volume_ratio=2.0).startswith("drift_")
    # Rand unten + Move nach unten -> breakout
    assert obs.classify_regime_v2(0.0, 0.1, 5, change_60m_pct=-1.0).startswith("breakout_")


def test_v1_unchanged_for_legacy():
    assert obs.classify_regime(0.5, 0.5, 50).startswith("trend_up_volatil")
    assert obs.classify_regime(0.0, 0.1, 95).startswith("breakout_")


# ---------- 0.7b: compute_features ----------

def test_compute_features_marks_regime_v2():
    f = obs.compute_features(_candles(140, step=0.2))
    assert f is not None
    assert f["regime_v"] == 2
    # 140 Kerzen: zu wenig Historie fuer Perzentil -> Fallback
    assert f["vol_basis"] == "fixed_fallback"
    assert "trend_1d_pct" not in f


def test_compute_features_percentile_and_daily_trend():
    # 48h ruhige Historie, letzte Stunde deutlich bewegter -> Vol-Rank hoch
    candles = _candles(2900, step=0.0, noise=0.01)
    candles += _candles(80, step=0.0, base=candles[-1]["close"], noise=0.6)
    f = obs.compute_features(candles)
    assert f["vol_basis"] == "percentile"
    assert f["vol_rank"] >= obs.VOL_RANK_HIGH
    assert f["regime"].endswith("volatil")
    assert "trend_1d_pct" in f and "daily_bias" in f


def test_compute_features_daily_bias_up():
    f = obs.compute_features(_candles(2000, step=0.05))
    assert f["trend_1d_pct"] > 0
    assert f["daily_bias"] == "up"


def test_compute_features_prev_regime_hysteresis():
    candles = _candles(300, step=0.0, noise=0.05)
    f_no_prev = obs.compute_features(candles)
    f_prev = obs.compute_features(candles, prev_regime="trend_up_normal")
    assert f_no_prev is not None and f_prev is not None
    # gleicher Input, prev darf das Label nur Richtung Trend-Beibehalt ändern
    if f_prev["regime"].startswith("trend_up"):
        assert f_no_prev["trend_pct"] > obs.TREND_EXIT_PCT


def test_snapshot_to_text_with_and_without_daily():
    old = {"symbol": "BTCUSDT", "features": {"regime": "range_normal", "rsi": 50,
           "trend_pct": 0.0, "volatility_pct": 0.1, "atr_pct": 0.1,
           "volume_ratio": 1.0, "range_pos": 50.0}}
    assert "24h" not in obs.snapshot_to_text(old)
    new = dict(old)
    new["features"] = {**old["features"], "trend_1d_pct": 2.5,
                       "trend_3d_pct": -1.0, "daily_bias": "up"}
    txt = obs.snapshot_to_text(new)
    assert "24h +2.50%" in txt and "3d -1.00%" in txt and "Tages-Bias: up" in txt


# ---------- 0.7a: Gate-Domain krypto-only ----------

def test_shadow_predict_skips_out_of_domain():
    gate = MLGate()
    gate.settings = dict(DEFAULT_SETTINGS)
    gate._booster = object()  # Modell "vorhanden"
    gate.predict_row = lambda row: {"p_win": 0.61, "raw": 0.61}
    dec = {"symbol": "OIL", "confidence": 70, "action": "LONG"}
    assert gate.shadow_predict(dec) is None  # Out-of-Domain -> kein Log
    dec_btc = {"symbol": "BTCUSDT", "confidence": 70, "action": "LONG",
               "entry_snapshot": {"features": {}}}
    res = gate.shadow_predict(dec_btc)
    assert res is not None and res["p_win"] == 0.61
