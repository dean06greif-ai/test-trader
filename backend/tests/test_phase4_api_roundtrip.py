"""
Phase 4 iteration test: Self-Tuning Guard + Data Collection Config API roundtrip.
Tests admin login, GET /api/ai/status, POST /api/ai/config, unauthorized access,
clamping consistency and regression on existing fields.
"""
import os
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://ml-daytrade.preview.emergentagent.com").rstrip("/")
ADMIN_USER = "Admin"
ADMIN_PASS = "Dean06Greif!/Admin"

NEW_KEYS_DEFAULTS = {
    "tune_conf_min": 55,
    "tune_conf_max": 75,
    "tune_cooldown_max": 45,
    "collection_enabled": True,
    "collection_min_confidence": 60,
    "collection_cooldown_min": 30,
    "collection_max_same_direction": 5,
    "collection_max_per_coin": 2,
}


@pytest.fixture(scope="module")
def admin_token():
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={"username": ADMIN_USER, "password": ADMIN_PASS},
                      timeout=15)
    assert r.status_code == 200, f"Login failed: {r.status_code} {r.text}"
    data = r.json()
    tok = data.get("token") or data.get("access_token")
    assert tok, f"No token in response: {data}"
    return tok


@pytest.fixture(scope="module")
def auth_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


def _fetch_config(headers):
    r = requests.get(f"{BASE_URL}/api/ai/status", headers=headers, timeout=15)
    assert r.status_code == 200, f"ai/status failed: {r.status_code} {r.text}"
    body = r.json()
    # config may be nested
    cfg = body.get("config") or body
    return body, cfg


def test_health():
    r = requests.get(f"{BASE_URL}/api/health", timeout=10)
    assert r.status_code == 200


def test_admin_login(admin_token):
    assert isinstance(admin_token, str) and len(admin_token) > 10


def test_ai_status_has_new_keys(auth_headers):
    body, cfg = _fetch_config(auth_headers)
    print("ai/status keys:", list(cfg.keys())[:40])
    missing = [k for k in NEW_KEYS_DEFAULTS if k not in cfg]
    assert not missing, f"Missing new config keys: {missing}. Got cfg keys={list(cfg.keys())}"
    for k, v in NEW_KEYS_DEFAULTS.items():
        assert cfg[k] == v, f"Default mismatch {k}: got {cfg[k]} expected {v}"


def test_config_requires_admin():
    # no token
    r = requests.post(f"{BASE_URL}/api/ai/config", json={"collection_min_confidence": 65}, timeout=15)
    assert r.status_code in (401, 403), f"Expected 401/403, got {r.status_code}: {r.text}"


def test_update_new_keys_roundtrip(auth_headers):
    payload = {"collection_min_confidence": 65, "tune_conf_max": 80, "collection_enabled": False}
    r = requests.post(f"{BASE_URL}/api/ai/config", json=payload, headers=auth_headers, timeout=15)
    assert r.status_code == 200, f"update failed: {r.status_code} {r.text}"
    _, cfg = _fetch_config(auth_headers)
    assert cfg["collection_min_confidence"] == 65
    assert cfg["tune_conf_max"] == 80
    assert cfg["collection_enabled"] is False

    # reset
    reset = {"collection_min_confidence": 60, "tune_conf_max": 75, "collection_enabled": True}
    r2 = requests.post(f"{BASE_URL}/api/ai/config", json=reset, headers=auth_headers, timeout=15)
    assert r2.status_code == 200
    _, cfg2 = _fetch_config(auth_headers)
    assert cfg2["collection_min_confidence"] == 60
    assert cfg2["tune_conf_max"] == 75
    assert cfg2["collection_enabled"] is True


def test_clamp_tune_conf_min_le_max(auth_headers):
    # set min above max -> should clamp min to current max (75)
    r = requests.post(f"{BASE_URL}/api/ai/config",
                      json={"tune_conf_min": 90},
                      headers=auth_headers, timeout=15)
    assert r.status_code == 200, r.text
    _, cfg = _fetch_config(auth_headers)
    assert cfg["tune_conf_min"] <= cfg["tune_conf_max"], f"clamp failed: min={cfg['tune_conf_min']} max={cfg['tune_conf_max']}"
    # reset
    requests.post(f"{BASE_URL}/api/ai/config", json={"tune_conf_min": 55}, headers=auth_headers, timeout=15)


def test_regression_existing_keys(auth_headers):
    _, cfg_before = _fetch_config(auth_headers)
    orig_min_conf = cfg_before.get("min_confidence")
    orig_cd = cfg_before.get("cooldown_min")
    assert orig_min_conf is not None
    assert orig_cd is not None

    new_min = 70 if orig_min_conf != 70 else 72
    r = requests.post(f"{BASE_URL}/api/ai/config",
                      json={"min_confidence": new_min, "cooldown_min": 15},
                      headers=auth_headers, timeout=15)
    assert r.status_code == 200, r.text
    _, cfg = _fetch_config(auth_headers)
    assert cfg["min_confidence"] == new_min
    assert cfg["cooldown_min"] == 15
    # restore
    requests.post(f"{BASE_URL}/api/ai/config",
                  json={"min_confidence": orig_min_conf, "cooldown_min": orig_cd},
                  headers=auth_headers, timeout=15)
