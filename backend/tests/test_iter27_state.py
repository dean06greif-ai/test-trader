"""Iteration 27: explain.state block for closed + open trades."""
import os
import pytest
import requests
from pymongo import MongoClient

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL",
                          "https://regime-analyzer-4.preview.emergentagent.com").rstrip("/")
MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "test_database")


@pytest.fixture(scope="module")
def db():
    return MongoClient(MONGO_URL)[DB_NAME]


@pytest.fixture(scope="module")
def btc_price():
    r = requests.get(f"{BASE_URL}/api/ai/regime/BTCUSDT", timeout=15)
    assert r.status_code == 200
    p = ((r.json() or {}).get("features") or {}).get("price")
    assert p and p > 100
    return float(p)


@pytest.fixture(scope="module", autouse=True)
def seed_and_cleanup(db, btc_price):
    closed = {
        "id": "t28-c", "strategy_id": "ai_trader", "mode": "paper",
        "status": "closed", "symbol": "XRPUSDT", "side": "SHORT",
        "entry": 2.6, "sl": 2.68, "initial_sl": 2.68, "tp1": 2.5, "tpf": 2.4,
        "qty": 40, "qty_remaining": 0, "risk": 3.2, "max_capital": 100,
        "leverage": 5, "fee_percent": 0.06, "fees_paid": 0.12,
        "realized_pnl": 3.88, "exit_price": 2.5, "result": "win",
        "opened_at": "2026-06-15T06:00:00+00:00",
        "closed_at": "2026-06-15T09:30:00+00:00",
    }
    # Open trade: LONG well below current price, SL far below, TP far above
    entry_open = round(btc_price * 0.70, 2)
    sl_open = round(entry_open * 0.90, 2)
    tp1_open = round(entry_open * 1.20, 2)
    tpf_open = round(entry_open * 1.35, 2)
    open_t = {
        "id": "t28-o", "strategy_id": "ai_trader", "mode": "paper",
        "status": "open", "symbol": "BTCUSDT", "side": "LONG",
        "entry": entry_open, "sl": sl_open, "initial_sl": sl_open,
        "tp1": tp1_open, "tpf": tpf_open,
        "qty": 0.001, "qty_remaining": 0.001, "risk": 5.0,
        "max_capital": 100, "leverage": 5, "fee_percent": 0.06,
        "fees_paid": 0.0, "opened_at": "2026-06-15T06:00:00+00:00",
    }
    db.auto_trades.delete_many({"id": {"$in": ["t28-c", "t28-o"]}})
    db.auto_trades.insert_many([closed, open_t])
    yield
    db.auto_trades.delete_many({"id": {"$in": ["t28-c", "t28-o", "t27-ui"]}})


def test_explain_closed_state():
    r = requests.get(f"{BASE_URL}/api/autotrade/trade/t28-c/explain", timeout=15)
    assert r.status_code == 200, r.text
    st = (r.json() or {}).get("state") or {}
    assert st.get("status") == "closed"
    assert st.get("result") == "win"
    assert abs(float(st.get("gross_pnl")) - 4.0) < 0.02, st
    assert abs(float(st.get("fees_paid")) - 0.12) < 0.001, st
    assert abs(float(st.get("realized_pnl_net")) - 3.88) < 0.01, st
    assert int(st.get("duration_seconds")) == 12600, st
    assert st.get("r_multiple") is not None


def test_explain_open_state():
    r = requests.get(f"{BASE_URL}/api/autotrade/trade/t28-o/explain", timeout=15)
    assert r.status_code == 200, r.text
    js = r.json() or {}
    st = js.get("state") or {}
    # Paper monitor may close it if price hits SL/TP; in that case skip open check
    if st.get("status") != "open":
        pytest.skip(f"Trade auto-closed by paper monitor: {st}")
    assert st.get("current_price") is not None
    assert st.get("unrealized_pnl") is not None
    assert st.get("live_pnl") is not None
    assert st.get("sl_distance_pct") is not None


def test_review_single_only_one_action_slice():
    # Code-level check: services/ai_trade_manager.py review_single uses [:1]
    src = open("/app/backend/services/ai_trade_manager.py").read()
    assert "actions\") or [])[:1]" in src, "review_single must slice actions[:1]"
    assert "HÖCHSTENS EINER Aktion" in src
    assert "max. 2 Sätze" in src
