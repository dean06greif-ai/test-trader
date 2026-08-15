"""Iteration 22 - verify RAM/ML-Lab projection fix and Trade-Manager guards via public API."""
import os
import time
import uuid
from datetime import datetime, timezone

import pytest
import requests
from pymongo import MongoClient

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://auto-retrain-hub.preview.emergentagent.com").rstrip("/")
ADMIN_USER = "Admin"
ADMIN_PW = "Dean06Greif!/Admin"

MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "test_database")


@pytest.fixture(scope="module")
def token():
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={"username": ADMIN_USER, "password": ADMIN_PW}, timeout=15)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    tok = r.json().get("token")
    assert tok
    return tok


@pytest.fixture(scope="module")
def auth_headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


@pytest.fixture(scope="module")
def mongo_coll():
    cli = MongoClient(MONGO_URL)
    coll = cli[DB_NAME]["auto_trades"]
    yield coll
    # Cleanup TEST_ prefixed trades
    coll.delete_many({"id": {"$regex": "^TEST_"}})
    cli.close()


# ---------------- Basic health/auth ----------------
def test_health_alive():
    r = requests.get(f"{BASE_URL}/api/health", timeout=15)
    assert r.status_code == 200
    j = r.json()
    assert j.get("status") == "alive", f"unexpected: {j}"


def test_admin_login_returns_token(token):
    assert isinstance(token, str) and len(token) > 10


# ---------------- ML Lab endpoints (projection change) ----------------
def test_ml_lab_dataset_endpoint(auth_headers):
    r = requests.get(f"{BASE_URL}/api/ai/ml/dataset", headers=auth_headers, timeout=60)
    assert r.status_code == 200, f"dataset failed: {r.status_code} {r.text[:400]}"
    j = r.json()
    meta = j.get("meta") or j.get("dataset") or {}
    assert isinstance(meta, dict) and meta, f"empty meta/dataset: {j}"
    assert "samples" in meta or "n" in meta or "wins" in meta or "losses" in meta, f"unexpected meta: {meta}"


def test_ml_lab_status_endpoint(auth_headers):
    r = requests.get(f"{BASE_URL}/api/ai/ml/status", headers=auth_headers, timeout=30)
    assert r.status_code == 200, f"status failed: {r.status_code} {r.text[:400]}"


# ---------------- ML Gate v1 ----------------
def test_ml_gate_status():
    r = requests.get(f"{BASE_URL}/api/ml/gate/status", timeout=30)
    assert r.status_code == 200
    j = r.json()
    assert j.get("mode") == "shadow", f"mode: {j.get('mode')}"
    assert j.get("model_loaded") is True, f"model_loaded: {j.get('model_loaded')} full={j}"


def test_ml_gate_train(auth_headers):
    r = requests.post(f"{BASE_URL}/api/ml/gate/train", headers=auth_headers,
                      json={}, timeout=180)
    assert r.status_code == 200, f"train failed: {r.status_code} {r.text[:400]}"
    j = r.json()
    # Expect version + metrics
    assert ("version" in j) or ("metrics" in j) or ("status" in j), f"unexpected train resp: {j}"


# ---------------- Trade Manager Guards ----------------
def _make_dc_trade(mongo_coll, dc=True, symbol="TESTUSDT", entry=100.0, sl=99.0):
    tid = f"TEST_{uuid.uuid4().hex[:12]}"
    doc = {
        "id": tid,
        "symbol": symbol,
        "side": "LONG",
        "mode": "paper",
        "status": "open",
        "entry": entry,
        "sl": sl,
        "initial_sl": sl,
        "tp1": entry + 2.0,
        "tp_final": entry + 5.0,
        "qty": 1.0,
        "qty_remaining": 1.0,
        "leverage": 10,
        "strategy_id": "ai_trader",
        "ai_actions": 0,
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "data_collection": dc,
    }
    mongo_coll.insert_one(doc)
    return tid


def test_guard1_ki_blocked_on_data_collection_trade(mongo_coll, auth_headers):
    tid = _make_dc_trade(mongo_coll, dc=True)
    try:
        r = requests.post(f"{BASE_URL}/api/ai/trade/action", headers=auth_headers,
                          json={"trade_id": tid, "action": "adjust_sl", "value": 99.5, "source": "ki"},
                          timeout=30)
        assert r.status_code == 200, f"{r.status_code} {r.text}"
        j = r.json()
        assert j.get("status") == "blocked", f"expected blocked, got: {j}"
        assert "datensammel" in (j.get("detail") or "").lower(), f"detail: {j.get('detail')}"
    finally:
        mongo_coll.delete_one({"id": tid})


def test_guard1b_manual_allowed_on_data_collection_trade(mongo_coll, auth_headers):
    tid = _make_dc_trade(mongo_coll, dc=True)
    try:
        r = requests.post(f"{BASE_URL}/api/autotrade/trade/{tid}/action",
                          headers=auth_headers,
                          json={"action": "adjust_sl", "value": 99.5},
                          timeout=30)
        assert r.status_code == 200, f"{r.status_code} {r.text}"
        j = r.json()
        status = j.get("status")
        # Manual path must not be blocked by data_collection guard
        assert status != "blocked" or "datensammel" not in (j.get("detail") or "").lower(), (
            f"manual should be allowed on DC trade: {j}")
    finally:
        mongo_coll.delete_one({"id": tid})


def test_guard2_sl_ratchet_blocked_too_close(mongo_coll, auth_headers):
    # Non-DC trade on fictitious symbol (mark falls back to entry=100)
    # initial_sl distance = 1.0 -> min_gap = 0.3
    tid = _make_dc_trade(mongo_coll, dc=False, symbol="TESTUSDT",
                         entry=100.0, sl=99.0)
    try:
        # value 99.95 -> gap 0.05 < 0.3 -> blocked
        r = requests.post(f"{BASE_URL}/api/ai/trade/action", headers=auth_headers,
                          json={"trade_id": tid, "action": "adjust_sl",
                                "value": 99.95, "source": "ki"}, timeout=30)
        assert r.status_code == 200, f"{r.status_code} {r.text}"
        j = r.json()
        assert j.get("status") == "blocked", f"expected blocked, got: {j}"
        assert "mindestabstand" in (j.get("detail") or "").lower(), f"detail: {j.get('detail')}"
    finally:
        mongo_coll.delete_one({"id": tid})


def test_guard2_sl_ratchet_allowed_with_gap(mongo_coll, auth_headers):
    # Non-DC trade; new SL=99.5 (between old 99 and mark 100) reduces risk and gap 0.5 >= 0.3
    tid = _make_dc_trade(mongo_coll, dc=False, symbol="TESTUSDT",
                         entry=100.0, sl=99.0)
    try:
        r = requests.post(f"{BASE_URL}/api/ai/trade/action", headers=auth_headers,
                          json={"trade_id": tid, "action": "adjust_sl",
                                "value": 99.5, "source": "ki"}, timeout=30)
        assert r.status_code == 200, f"{r.status_code} {r.text}"
        j = r.json()
        # Allowed statuses: 'ok', 'success', 'adjusted'
        assert j.get("status") in ("ok", "success", "adjusted", "updated"), f"expected ok, got: {j}"
    finally:
        mongo_coll.delete_one({"id": tid})
