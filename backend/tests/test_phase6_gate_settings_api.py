"""Phase 6 - Gate v1 Auto-Retrain settings API regression tests."""
import os
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    # Fallback to reading frontend/.env
    with open("/app/frontend/.env") as f:
        for line in f:
            if line.startswith("REACT_APP_BACKEND_URL="):
                BASE_URL = line.split("=", 1)[1].strip().rstrip("/")

ADMIN_USER = "Admin"
ADMIN_PASS = "Dean06Greif!/Admin"


@pytest.fixture(scope="module")
def admin_token():
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={"username": ADMIN_USER, "password": ADMIN_PASS}, timeout=10)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    tok = r.json().get("token")
    assert tok
    return tok


@pytest.fixture(scope="module")
def admin_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}", "Content-Type": "application/json"}


def test_health():
    r = requests.get(f"{BASE_URL}/api/health", timeout=10)
    assert r.status_code == 200


def test_ai_status_ok():
    r = requests.get(f"{BASE_URL}/api/ai/status", timeout=10)
    assert r.status_code == 200


def test_gate_status_has_new_settings_and_trigger():
    r = requests.get(f"{BASE_URL}/api/ml/gate/status", timeout=10)
    assert r.status_code == 200
    data = r.json()
    assert "settings" in data
    s = data["settings"]
    # New Phase-6 fields
    assert "auto_retrain" in s
    assert "retrain_hour_berlin" in s
    assert "retrain_min_new" in s
    assert s["retrain_hour_berlin"] == 4 or isinstance(s["retrain_hour_berlin"], int)
    assert "trigger" in data


def test_settings_persist_and_reset(admin_headers):
    # 1) set auto_retrain false + retrain_min_new 100
    r = requests.post(f"{BASE_URL}/api/ml/gate/settings",
                      headers=admin_headers,
                      json={"auto_retrain": False, "retrain_min_new": 100}, timeout=10)
    assert r.status_code == 200, r.text
    # verify via GET status
    r2 = requests.get(f"{BASE_URL}/api/ml/gate/status", timeout=10)
    s = r2.json()["settings"]
    assert s["auto_retrain"] is False
    assert s["retrain_min_new"] == 100
    # reset
    r3 = requests.post(f"{BASE_URL}/api/ml/gate/settings",
                       headers=admin_headers,
                       json={"auto_retrain": True, "retrain_min_new": 50}, timeout=10)
    assert r3.status_code == 200
    s2 = requests.get(f"{BASE_URL}/api/ml/gate/status", timeout=10).json()["settings"]
    assert s2["auto_retrain"] is True
    assert s2["retrain_min_new"] == 50


def test_settings_clamp_min_new(admin_headers):
    # retrain_min_new=5 must be clamped to 10
    r = requests.post(f"{BASE_URL}/api/ml/gate/settings",
                      headers=admin_headers,
                      json={"retrain_min_new": 5}, timeout=10)
    assert r.status_code == 200
    s = requests.get(f"{BASE_URL}/api/ml/gate/status", timeout=10).json()["settings"]
    assert s["retrain_min_new"] == 10, f"expected clamp to 10, got {s['retrain_min_new']}"
    # reset
    requests.post(f"{BASE_URL}/api/ml/gate/settings",
                  headers=admin_headers,
                  json={"retrain_min_new": 50}, timeout=10)


def test_settings_requires_admin():
    r = requests.post(f"{BASE_URL}/api/ml/gate/settings",
                      json={"auto_retrain": False}, timeout=10)
    assert r.status_code in (401, 403)
