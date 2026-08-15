"""Phase 6 – Auto-Retrain-Logik des Gates (rein testbar via _retrain_due)."""
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, "/app/backend")

from services.ml_gate import MLGate, MIN_SAMPLES  # noqa: E402


def _gate(with_model: bool, samples: int = 900, trained_hours_ago: float = 30):
    g = MLGate()
    if with_model:
        g._booster = object()  # nur Anwesenheit zählt für _retrain_due
        g.model_meta = {
            "version": 1,
            "trained_at": (datetime.now(timezone.utc)
                           - timedelta(hours=trained_hours_ago)).isoformat(),
            "dataset": {"samples": samples},
        }
    return g


def test_first_training_when_enough_data():
    g = _gate(with_model=False)
    assert g._retrain_due(MIN_SAMPLES - 1, now_berlin_hour=12) is None
    assert g._retrain_due(MIN_SAMPLES, now_berlin_hour=12) == "auto (Erst-Training)"


def test_retrain_on_new_samples():
    g = _gate(with_model=True, samples=900)
    assert g._retrain_due(900 + 49, now_berlin_hour=12) is None
    assert g._retrain_due(900 + 50, now_berlin_hour=12) == "auto (neue Ergebnisse)"
    g.settings["retrain_min_new"] = 100
    assert g._retrain_due(900 + 50, now_berlin_hour=12) is None


def test_daily_retrain_only_in_hour_and_after_20h():
    g = _gate(with_model=True, trained_hours_ago=30)
    assert g._retrain_due(900, now_berlin_hour=4) == "auto (täglich)"
    assert g._retrain_due(900, now_berlin_hour=5) is None
    fresh = _gate(with_model=True, trained_hours_ago=2)
    assert fresh._retrain_due(900, now_berlin_hour=4) is None


def test_auto_retrain_off_and_training_lock():
    g = _gate(with_model=True, trained_hours_ago=30)
    g.settings["auto_retrain"] = False
    assert g._retrain_due(2000, now_berlin_hour=4) is None
    g.settings["auto_retrain"] = True
    g.training_now = True
    assert g._retrain_due(2000, now_berlin_hour=4) is None
