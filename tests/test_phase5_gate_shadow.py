"""Phase 5 – Gate v1 Shadow-Modus: Feature-Bau, Purged Walk-Forward + Embargo,
Training/Kalibrierung, Shadow-Prediction (blockt nie) und kontrafaktischer Report."""
import math
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, "/app/backend")

from services.ml_gate import (  # noqa: E402
    GATE_FEATURES, MLGate, encode_regime, evaluate_shadow, gate_feature_row,
    purged_walk_forward, row_from_decision, row_from_ghost, row_from_signal,
    train_sync, _apply_calib,
)


def _dt(days_ago: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days_ago)


FEATS = {"price": 100.0, "rsi": 60.0, "trend_pct": 0.1, "atr_pct": 0.2,
         "volatility_pct": 0.1, "volume_ratio": 1.2, "range_pos": 70.0,
         "change_60m_pct": 0.5, "regime": "trend_up_normal"}


def test_encode_regime():
    assert encode_regime("trend_up_volatil") == (1.0, 2.0, 0.0)
    assert encode_regime("trend_down_ruhig") == (-1.0, 0.0, 0.0)
    assert encode_regime("breakout_normal") == (0.0, 1.0, 1.0)
    assert encode_regime(None) == (0.0, 1.0, 0.0)


def test_gate_feature_row_complete_and_ordered():
    row = gate_feature_row("LONG", 72, 0.6, 0.9, _dt(1), FEATS, "decision")
    assert set(row.keys()) == set(GATE_FEATURES)
    assert row["side_long"] == 1.0 and row["src_decision"] == 1.0
    assert row["has_market_state"] == 1.0
    assert row["crv"] == 1.5 and row["regime_trend"] == 1.0
    row2 = gate_feature_row("short", 0, 0.5, 1.0, _dt(1), None, "ghost")
    assert row2["side_long"] == 0.0 and row2["has_market_state"] == 0.0
    assert row2["src_ghost"] == 1.0 and row2["rsi"] == 50.0


def test_row_builders_labels_and_weights():
    dec = {"action": "LONG", "confidence": 70, "sl_pct": 0.6, "tp1_pct": 0.9,
           "ts": _dt(2).isoformat(), "outcome": "win", "outcome_source": "trade_pnl",
           "entry_market_snapshot": {"features": FEATS}}
    row, y, w, _ = row_from_decision(dec)
    assert y == 1 and w == 1.0 and row["has_market_state"] == 1.0
    dec["outcome_source"] = "tp1_touch"
    dec["data_collection"] = True
    _, _, w2, _ = row_from_decision(dec)
    assert abs(w2 - 0.8 * 0.85) < 1e-9
    assert row_from_decision({"action": "LONG", "ts": _dt(1).isoformat()}) is None

    sig = {"type": "short", "timestamp": _dt(3).isoformat(), "result": "loss",
           "result_source": "trade_pnl", "entry_price": 100, "stop_loss": 101,
           "take_profit_1": 98, "rsi": 40}
    row, y, w, _ = row_from_signal(sig)
    assert y == 0 and w == 0.7 and row["side_long"] == 0.0
    assert abs(row["sl_pct"] - 1.0) < 1e-9 and abs(row["tp1_pct"] - 2.0) < 1e-9
    sig["result_ambiguous"] = True
    assert row_from_signal(sig) is None

    g = {"side": "long", "opened_at": _dt(4).isoformat(), "result": "win",
         "entry": 50, "sl": 49.5, "tp": 51}
    row, y, w, _ = row_from_ghost(g)
    assert y == 1 and w == 0.5 and row["src_ghost"] == 1.0


def test_purged_walk_forward_no_lookahead_and_embargo():
    ts = [_dt(200 - i) for i in range(200)]  # aufsteigend, 1 Tag Abstand
    splits = purged_walk_forward(ts, n_folds=5, embargo_hours=24, min_train=20, min_test=5)
    assert splits, "es muss Splits geben"
    for train_idx, test_idx in splits:
        test_start = ts[min(test_idx)]
        for i in train_idx:
            # strikt vor Test-Start MINUS Embargo (kein Look-Ahead, kein Leakage)
            assert ts[i] < test_start - timedelta(hours=24)
        assert not set(train_idx) & set(test_idx)
    # zu wenig Daten -> keine Splits
    assert purged_walk_forward(ts[:10], min_train=20, min_test=5) == []


def _synthetic_dataset(n=400):
    """Lernbares Muster: LONG bei RSI<50 gewinnt, sonst verliert (mit Rauschen)."""
    import random
    random.seed(7)
    rows, y, w, tss = [], [], [], []
    for i in range(n):
        rsi = random.uniform(20, 80)
        side = "LONG" if i % 2 == 0 else "SHORT"
        feats = dict(FEATS, rsi=rsi)
        win = (rsi < 50) if side == "LONG" else (rsi > 50)
        if random.random() < 0.15:
            win = not win
        rows.append(gate_feature_row(side, 65, 0.6, 0.9, _dt(n - i), feats, "decision"))
        y.append(1 if win else 0)
        w.append(1.0)
        tss.append(_dt((n - i) / 4))
    return rows, y, w, tss


def test_train_sync_beats_baseline_and_calibrates():
    rows, y, w, tss = _synthetic_dataset()
    res = train_sync(rows, y, w, tss)
    m = res["metrics"]
    assert m["folds_used"] >= 3 and m["oos_samples"] > 100
    assert m["oos_brier_raw"] < m["baseline_brier"], "Muster muss gelernt werden"
    assert m["beats_baseline"] is True
    assert res["calibration"] is not None and "coef" in res["calibration"]
    assert m.get("calibration_bins"), "Kalibrierungs-Bins müssen existieren"
    assert res["booster_b64"] and res["importances"][0]["gain"] >= 0
    # Kalibrierung ist eine gültige Wahrscheinlichkeit
    p = _apply_calib(0.7, res["calibration"])
    assert 0.0 < p < 1.0


def test_shadow_predict_with_trained_model():
    rows, y, w, tss = _synthetic_dataset()
    res = train_sync(rows, y, w, tss)
    gate = MLGate()
    gate._restore({"version": 1, "booster_b64": res["booster_b64"],
                   "calibration": res["calibration"], "metrics": res["metrics"]})
    dec_good = {"action": "LONG", "confidence": 65, "sl_pct": 0.6, "tp1_pct": 0.9,
                "ts": _dt(0).isoformat(),
                "entry_market_snapshot": {"features": dict(FEATS, rsi=30)}}
    dec_bad = dict(dec_good, entry_market_snapshot={"features": dict(FEATS, rsi=75)})
    good = gate.shadow_predict(dec_good)
    bad = gate.shadow_predict(dec_bad)
    assert good and bad and 0 <= good["p_win"] <= 1
    assert good["p_win"] > bad["p_win"], "RSI-Muster muss sich in p_win zeigen"
    assert good["model_version"] == 1 and "would_block" in good
    # Ohne Modell / bei Fehlern: None, nie Exception (blockt nie)
    empty = MLGate()
    assert empty.shadow_predict(dec_good) is None
    assert empty.shadow_predict({"kaputt": True}) is None


def test_evaluate_shadow_criteria():
    # 100 Wins mit hohem p, 100 Losses mit niedrigem p -> Gate wäre perfekt
    items = ([{"p_win": 0.7, "label": 1, "r": 1.5} for _ in range(100)]
             + [{"p_win": 0.2, "label": 0, "r": -1.0} for _ in range(100)])
    rep = evaluate_shadow(items, 0.45)
    assert rep["evaluated"] == 200
    assert rep["pct_losers_blocked"] == 100.0 and rep["pct_winners_blocked"] == 0.0
    assert rep["criteria"]["min_samples_150"] is True
    assert rep["criteria"]["losers_blocked_ge_35"] is True
    assert rep["criteria"]["winners_blocked_le_15"] is True
    assert rep["criteria"]["uplift_ge_20"] is True
    assert rep["criteria"]["brier_beats_baseline"] is True
    assert rep["avg_r_passed"] == 1.5
    # Leere Menge crasht nicht
    rep0 = evaluate_shadow([], 0.45)
    assert rep0["evaluated"] == 0 and rep0["criteria"]["min_samples_150"] is False
