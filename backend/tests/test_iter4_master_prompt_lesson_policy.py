"""Iter4 - regression tests for the lesson_policy bugfix in POST /api/ai/master-prompt."""
import os
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
ADMIN_USER = os.environ.get("ADMIN_USER", "Admin")
ADMIN_PASS = os.environ.get("ADMIN_PASSWORD", "admin")


@pytest.fixture(scope="module")
def admin_token():
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={"username": ADMIN_USER, "password": ADMIN_PASS}, timeout=15)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    return r.json().get("token")


@pytest.fixture(scope="module")
def defaults():
    r = requests.get(f"{BASE_URL}/api/ai/master-prompt", timeout=15)
    assert r.status_code == 200
    d = r.json()["master_prompt"]["defaults"]
    return d


@pytest.fixture(scope="module", autouse=True)
def cleanup_master_prompt(admin_token, defaults):
    yield
    # restore defaults
    requests.post(f"{BASE_URL}/api/ai/master-prompt",
                  json={"text": defaults["text"],
                        "rules": defaults["rules"],
                        "lesson_policy": defaults["lesson_policy"]},
                  headers={"Authorization": f"Bearer {admin_token}"}, timeout=15)


def test_get_master_prompt_shape():
    r = requests.get(f"{BASE_URL}/api/ai/master-prompt", timeout=15)
    assert r.status_code == 200
    mp = r.json()["master_prompt"]
    assert "text" in mp and "lesson_policy" in mp and "rules" in mp
    for k in ("forbidden_terms", "max_daily_loss_usdt", "max_trades_per_day"):
        assert k in mp["rules"]


def test_post_lesson_policy_only(admin_token):
    r = requests.post(f"{BASE_URL}/api/ai/master-prompt",
                      json={"lesson_policy": "TESTPOLICY-XYZ"},
                      headers={"Authorization": f"Bearer {admin_token}"}, timeout=15)
    assert r.status_code == 200, r.text
    assert r.json()["master_prompt"]["lesson_policy"] == "TESTPOLICY-XYZ"

    # GET persistence
    g = requests.get(f"{BASE_URL}/api/ai/master-prompt", timeout=15)
    assert g.json()["master_prompt"]["lesson_policy"] == "TESTPOLICY-XYZ"


def test_post_all_three(admin_token, defaults):
    payload = {
        "text": "UI-TEXT-42",
        "rules": {**defaults["rules"], "max_trades_per_day": 7},
        "lesson_policy": "COMBO-POLICY-42",
    }
    r = requests.post(f"{BASE_URL}/api/ai/master-prompt", json=payload,
                      headers={"Authorization": f"Bearer {admin_token}"}, timeout=15)
    assert r.status_code == 200, r.text
    mp = r.json()["master_prompt"]
    assert mp["text"] == "UI-TEXT-42"
    assert mp["lesson_policy"] == "COMBO-POLICY-42"
    assert mp["rules"]["max_trades_per_day"] == 7

    g = requests.get(f"{BASE_URL}/api/ai/master-prompt", timeout=15).json()["master_prompt"]
    assert g["text"] == "UI-TEXT-42"
    assert g["lesson_policy"] == "COMBO-POLICY-42"
    assert g["rules"]["max_trades_per_day"] == 7


def test_empty_body_400(admin_token):
    r = requests.post(f"{BASE_URL}/api/ai/master-prompt", json={},
                      headers={"Authorization": f"Bearer {admin_token}"}, timeout=15)
    assert r.status_code == 400


def test_unauthorized_no_admin():
    r = requests.post(f"{BASE_URL}/api/ai/master-prompt",
                      json={"lesson_policy": "X"}, timeout=15)
    assert r.status_code in (401, 403)


@pytest.mark.parametrize("endpoint", [
    "/api/ai/master-prompt", "/api/ai/schedule", "/api/ai/validation",
    "/api/ai/strategies", "/api/ai/notify-guard", "/api/ai/providers/health",
    "/api/ai/status", "/api/ai/insights", "/api/ai/lessons",
    "/api/health", "/api/strategies",
])
def test_regression_get_endpoints(endpoint):
    r = requests.get(f"{BASE_URL}{endpoint}", timeout=20)
    assert r.status_code == 200, f"{endpoint} -> {r.status_code}"
