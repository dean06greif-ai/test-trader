"""API-level tests for the Watchdog endpoints + auth + autotrade regression.
Uses the external REACT_APP_BACKEND_URL if given, otherwise localhost.
"""
import os
import requests
import pytest

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "http://localhost:8001").rstrip("/")


@pytest.fixture(scope="module")
def admin_token():
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={"username": os.environ.get("ADMIN_USER", "Admin"), "password": os.environ.get("ADMIN_PASSWORD", "admin")}, timeout=15)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:200]}"
    tok = r.json().get("token") or r.json().get("access_token")
    assert tok, f"no token in response: {r.json()}"
    return tok


@pytest.fixture(scope="module")
def auth_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


# --- Auth ------------------------------------------------------------------
class TestAuth:
    def test_login_success_returns_token(self):
        r = requests.post(f"{BASE_URL}/api/auth/login",
                          json={"username": os.environ.get("ADMIN_USER", "Admin"), "password": os.environ.get("ADMIN_PASSWORD", "admin")}, timeout=15)
        assert r.status_code == 200
        data = r.json()
        assert data.get("token") or data.get("access_token")

    def test_login_wrong_password(self):
        r = requests.post(f"{BASE_URL}/api/auth/login",
                          json={"username": os.environ.get("ADMIN_USER", "Admin"), "password": "wrong"}, timeout=15)
        assert r.status_code in (400, 401, 403)

    def test_watchdog_run_without_token_401(self):
        r = requests.post(f"{BASE_URL}/api/autotrade/watchdog/run", timeout=15)
        assert r.status_code in (401, 403)


# --- Watchdog --------------------------------------------------------------
class TestWatchdog:
    def test_status_returns_configured_false_and_settings(self):
        r = requests.get(f"{BASE_URL}/api/autotrade/watchdog/status", timeout=15)
        assert r.status_code == 200
        data = r.json()
        assert data.get("configured") is False
        s = data.get("settings")
        assert isinstance(s, dict), f"settings missing: {data}"
        for key in ("enabled", "interval_sec", "fallback_sl_percent",
                    "max_sl_retries", "emergency_close", "adopt_unknown"):
            assert key in s, f"missing settings key {key}: {s}"

    def test_run_with_admin_returns_skipped(self, auth_headers):
        r = requests.post(f"{BASE_URL}/api/autotrade/watchdog/run",
                          headers=auth_headers, timeout=20)
        assert r.status_code == 200, r.text[:300]
        data = r.json()
        assert data.get("status") == "skipped"

    def test_config_update_persists_and_clamps(self, auth_headers):
        # valid change
        r = requests.post(f"{BASE_URL}/api/autotrade/watchdog/config",
                          headers=auth_headers,
                          json={"interval_sec": 90, "fallback_sl_percent": 1.5},
                          timeout=15)
        assert r.status_code == 200, r.text[:300]
        # verify via status
        s = requests.get(f"{BASE_URL}/api/autotrade/watchdog/status", timeout=15).json()["settings"]
        assert s["interval_sec"] == 90
        assert abs(float(s["fallback_sl_percent"]) - 1.5) < 1e-6

        # clamp: extreme values
        r2 = requests.post(f"{BASE_URL}/api/autotrade/watchdog/config",
                           headers=auth_headers,
                           json={"interval_sec": 1, "fallback_sl_percent": 999,
                                 "max_sl_retries": -5},
                           timeout=15)
        assert r2.status_code == 200
        s2 = requests.get(f"{BASE_URL}/api/autotrade/watchdog/status", timeout=15).json()["settings"]
        # interval_sec should be clamped up (>=some min), fallback_sl_percent clamped down
        assert s2["interval_sec"] >= 5, s2
        assert 0 < float(s2["fallback_sl_percent"]) <= 50, s2
        assert int(s2["max_sl_retries"]) >= 0, s2


# --- Autotrade regression --------------------------------------------------
class TestAutotradeRegression:
    @pytest.mark.parametrize("path", [
        "/api/autotrade/config",
        "/api/autotrade/trades",
        "/api/autotrade/sync-status",
        "/api/autotrade/balance",
    ])
    def test_endpoint_reachable(self, path, auth_headers):
        # try with auth (some are protected)
        r = requests.get(f"{BASE_URL}{path}", headers=auth_headers, timeout=15)
        assert r.status_code in (200, 401, 403), f"{path} -> {r.status_code} {r.text[:200]}"
        # if 200 must return JSON
        if r.status_code == 200:
            r.json()
