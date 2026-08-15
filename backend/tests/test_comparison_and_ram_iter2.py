"""Regressionstests: Strategie-Vergleich ohne Watchdog-/Manuell-Trades und
mit funktionierenden mode/days-Filtern (Iteration 2)."""
import os
import uuid

import pytest
import requests
from pymongo import MongoClient

BASE = os.environ.get("REACT_APP_BACKEND_URL", "http://localhost:8001")


@pytest.fixture(scope="module")
def db():
    client = MongoClient(os.environ["MONGO_URL"])
    yield client[os.environ.get("DB_NAME", "crypto_scanner")]
    client.close()


@pytest.fixture()
def manual_trade(db):
    """Geschlossener Watchdog-/Manuell-Trade, der NICHT im Vergleich landen darf."""
    tid = f"TEST-MANUAL-{uuid.uuid4().hex[:8]}"
    doc = {"id": tid, "symbol": "BTCUSDT", "side": "LONG", "mode": "live",
           "status": "closed", "strategy_id": "external",
           "strategy_name": "Manuell (Bitunix)", "manual_trade": True,
           "external_adopted": True, "result": "win", "realized_pnl": 123.45,
           "opened_at": "2099-01-01T00:00:00+00:00",
           "closed_at": "2099-01-01T01:00:00+00:00"}
    db.auto_trades.insert_one(dict(doc))
    yield doc
    db.auto_trades.delete_many({"id": tid})


def test_comparison_excludes_manual_bitunix_trades(manual_trade):
    r = requests.get(f"{BASE}/api/analytics/strategy-comparison", timeout=30)
    assert r.status_code == 200
    data = r.json()
    ids = [row["strategy_id"] for row in data["comparison"]]
    names = [row["strategy_name"] for row in data["comparison"]]
    assert "external" not in ids
    assert "Manuell (Bitunix)" not in names
    assert "Extern (Watchdog)" not in names


def test_comparison_mode_and_days_filters_work():
    for params in ({"mode": "live"}, {"mode": "paper"}, {"days": 7}, {"days": 30}):
        r = requests.get(f"{BASE}/api/analytics/strategy-comparison",
                         params=params, timeout=30)
        assert r.status_code == 200, params
        data = r.json()
        assert "comparison" in data
        if "mode" in params:
            assert data["mode"] == params["mode"]
        if "days" in params:
            assert data["days"] == params["days"]


def test_comparison_has_projected_fields_intact():
    """Die Projektion darf keine für die UI nötigen Felder verlieren."""
    r = requests.get(f"{BASE}/api/analytics/strategy-comparison", timeout=30)
    data = r.json()
    for row in data["comparison"]:
        for field in ("strategy_id", "strategy_name", "trades", "win_rate", "pnl",
                      "profit_factor", "max_drawdown", "avg_duration_min",
                      "paper_trades", "live_trades", "by_symbol", "open_trades"):
            assert field in row, f"{field} fehlt in {row['strategy_id']}"


def test_candle_cache_budget_fits_small_ram(monkeypatch):
    """Server-Default: 500k Kerzen (~24 MB) – passt auf 512-MB-Instanzen;
    per CANDLE_CACHE_MAX_CANDLES weiterhin frei konfigurierbar."""
    import importlib
    from services import candle_cache
    monkeypatch.delenv("CANDLE_CACHE_MAX_CANDLES", raising=False)
    importlib.reload(candle_cache)
    assert candle_cache.MAX_CANDLES_IN_MEMORY == 500000
    monkeypatch.setenv("CANDLE_CACHE_MAX_CANDLES", "8000000")
    importlib.reload(candle_cache)
    assert candle_cache.MAX_CANDLES_IN_MEMORY == 8000000
    monkeypatch.delenv("CANDLE_CACHE_MAX_CANDLES", raising=False)
    importlib.reload(candle_cache)
