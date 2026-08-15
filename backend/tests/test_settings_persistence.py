"""
Backend tests for scanner settings persistence (custom_sessions & pre_signal).

Covers:
- GET /api/settings returns custom_sessions + pre_signal_enabled fields
- POST /api/settings persists to MongoDB
- Empty custom_sessions -> 24/7 mode
- Partial updates preserve other fields
- Unknown fields are ignored
- Session-status derivation from custom_sessions
- Settings survive backend restart via supervisorctl

NOTE: All mutating tests live in a single class so pytest-xdist's loadscope
scheduler pins them to one worker. This avoids races against the shared
singleton scanner state on the preview backend.
"""
import os
import time
import subprocess
import requests
import pytest

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    env_path = "/app/frontend/.env"
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                if line.startswith("REACT_APP_BACKEND_URL="):
                    BASE_URL = line.split("=", 1)[1].strip().rstrip("/")
                    break

TIMEOUT = 15

_TOKEN = None


def _auth_headers():
    global _TOKEN
    if _TOKEN is None:
        r = requests.post(f"{BASE_URL}/api/auth/login",
                          json={"username": os.environ.get("ADMIN_USER", "Admin"),
                                "password": os.environ.get("ADMIN_PASSWORD", "admin")},
                          timeout=TIMEOUT)
        assert r.status_code == 200, f"admin login failed: {r.status_code} {r.text}"
        _TOKEN = r.json()["token"]
    return {"Authorization": f"Bearer {_TOKEN}"}


def _get_settings():
    r = requests.get(f"{BASE_URL}/api/settings", timeout=TIMEOUT)
    assert r.status_code == 200, f"GET /api/settings failed: {r.status_code} {r.text}"
    return r.json()


def _post_settings(payload):
    r = requests.post(f"{BASE_URL}/api/settings", json=payload,
                      headers=_auth_headers(), timeout=TIMEOUT)
    assert r.status_code == 200, f"POST /api/settings failed: {r.status_code} {r.text}"
    return r.json()


def _wait_for_backend(retries=40, delay=1.0):
    for _ in range(retries):
        try:
            r = requests.get(f"{BASE_URL}/api/health", timeout=3)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(delay)
    return False


class TestSettingsPersistence:
    """All settings tests kept in a single class so xdist loadscope pins them
    to one worker — otherwise parallel tests race on the shared singleton."""

    @classmethod
    def setup_class(cls):
        # Snapshot original settings; will restore in teardown_class.
        cls._original = _get_settings()

    @classmethod
    def teardown_class(cls):
        try:
            _post_settings(cls._original)
        except Exception:
            pass

    # ---- GET /api/settings ----
    def test_01_get_settings_200_and_shape(self):
        r = requests.get(f"{BASE_URL}/api/settings", timeout=TIMEOUT)
        assert r.status_code == 200
        data = r.json()
        assert "custom_sessions" in data
        assert "pre_signal_enabled" in data
        assert isinstance(data["custom_sessions"], list)
        assert isinstance(data["pre_signal_enabled"], bool)
        assert "_id" not in data, f"_id leak: {data}"

    # ---- POST /api/settings persistence ----
    def test_02_post_updates_custom_sessions_and_get_reflects(self):
        payload = {
            "custom_sessions": [
                {"start": "09:00", "end": "12:00", "name": "London", "enabled": True},
                {"start": "15:30", "end": "18:30", "name": "US", "enabled": True},
            ]
        }
        resp = _post_settings(payload)
        assert resp.get("status") == "success"
        assert resp["settings"]["custom_sessions"] == payload["custom_sessions"]
        assert _get_settings()["custom_sessions"] == payload["custom_sessions"]

    def test_03_post_empty_custom_sessions_marks_247_mode(self):
        _post_settings({"custom_sessions": []})
        assert _get_settings()["custom_sessions"] == []
        r = requests.get(f"{BASE_URL}/api/session/status", timeout=TIMEOUT)
        assert r.status_code == 200
        st = r.json()
        assert st["is_active"] is True, f"Expected 24/7 mode active: {st}"
        assert "24/7" in st.get("current_session", ""), (
            f"current_session should indicate 24/7: {st}"
        )

    def test_04_post_pre_signal_enabled_toggles(self):
        _post_settings({"pre_signal_enabled": False})
        assert _get_settings()["pre_signal_enabled"] is False
        _post_settings({"pre_signal_enabled": True})
        assert _get_settings()["pre_signal_enabled"] is True

    def test_05_partial_update_preserves_custom_sessions(self):
        sessions = [
            {"start": "08:00", "end": "10:00", "name": "Asia", "enabled": True},
        ]
        _post_settings({"custom_sessions": sessions, "pre_signal_enabled": True})
        # Send only pre_signal_enabled=false — custom_sessions must remain
        _post_settings({"pre_signal_enabled": False})
        got = _get_settings()
        assert got["pre_signal_enabled"] is False
        assert got["custom_sessions"] == sessions, (
            f"custom_sessions should be preserved on partial update: {got}"
        )

    def test_06_post_ignores_unknown_field(self):
        _post_settings({"unknown_random_field": "hacker", "pre_signal_enabled": True})
        after = _get_settings()
        assert "unknown_random_field" not in after
        assert "custom_sessions" in after
        assert "pre_signal_enabled" in after

    # ---- /api/session/status derivation ----
    def test_07_session_status_shape(self):
        r = requests.get(f"{BASE_URL}/api/session/status", timeout=TIMEOUT)
        assert r.status_code == 200
        data = r.json()
        # New schema: berlin_time/berlin_date replaced current_time_utc
        for f in ["is_active", "current_session", "custom_sessions",
                  "pre_signal_enabled", "berlin_time", "berlin_date"]:
            assert f in data, f"missing field {f} in {data}"
        assert isinstance(data["is_active"], bool)
        assert isinstance(data["custom_sessions"], list)
        assert isinstance(data["pre_signal_enabled"], bool)

    def test_08_disabled_only_session_yields_247_mode(self):
        _post_settings({
            "custom_sessions": [
                {"start": "00:00", "end": "23:59", "name": "AllDay", "enabled": False}
            ]
        })
        data = requests.get(f"{BASE_URL}/api/session/status", timeout=TIMEOUT).json()
        assert data["is_active"] is True, f"disabled sessions => 24/7: {data}"
        assert "24/7" in data["current_session"]

    def test_09_active_within_full_day_window(self):
        _post_settings({
            "custom_sessions": [
                {"start": "00:00", "end": "23:59", "name": "AlwaysOn", "enabled": True}
            ]
        })
        data = requests.get(f"{BASE_URL}/api/session/status", timeout=TIMEOUT).json()
        assert data["is_active"] is True
        assert data["current_session"] == "AlwaysOn"

    # ---- Multi-Strategy: active_strategy switching ----
    def test_10_get_settings_has_active_strategy_default(self):
        data = _get_settings()
        assert "active_strategy" in data
        # Default must be scalping (user requirement - do not change scalping)
        # Any prior test setup may have changed it, so we just assert it's a valid known strategy.
        strategies = requests.get(
            f"{BASE_URL}/api/strategies", timeout=TIMEOUT
        ).json()["strategies"]
        ids = [s["id"] for s in strategies]
        assert data["active_strategy"] in ids

    def test_11_switch_active_strategy_to_rsi_only(self):
        # New multi-tab schema: active_strategy is snapped to enabled_strategies[0]
        # so rsi_only must also be in enabled_strategies for it to stick.
        _post_settings({"enabled_strategies": ["rsi_only", "scalping_4_rules"],
                        "active_strategy": "rsi_only"})
        got = _get_settings()
        assert got["active_strategy"] == "rsi_only", got
        strategies = requests.get(
            f"{BASE_URL}/api/strategies", timeout=TIMEOUT
        ).json()
        assert strategies["active"] == "rsi_only", strategies

    def test_12_switch_active_strategy_back_to_scalping(self):
        _post_settings({"enabled_strategies": ["scalping_4_rules"],
                        "active_strategy": "scalping_4_rules"})
        got = _get_settings()
        assert got["active_strategy"] == "scalping_4_rules", got
        strategies = requests.get(
            f"{BASE_URL}/api/strategies", timeout=TIMEOUT
        ).json()
        assert strategies["active"] == "scalping_4_rules", strategies

    def test_13_partial_update_preserves_active_strategy(self):
        _post_settings({"enabled_strategies": ["rsi_only", "scalping_4_rules"],
                        "active_strategy": "rsi_only"})
        # unrelated field
        _post_settings({"pre_signal_enabled": True})
        got = _get_settings()
        assert got["active_strategy"] == "rsi_only", (
            f"active_strategy should survive partial update: {got}"
        )

    # ---- Persistence across backend restart ----
    @pytest.mark.skipif(os.environ.get("RUN_RESTART_TESTS") != "1",
                        reason="Backend-Neustart killt parallel laufende Tests "
                               "(xdist) – nur gezielt mit RUN_RESTART_TESTS=1")
    def test_14_settings_survive_backend_restart(self):
        marker_sessions = [
            {"start": "07:15", "end": "09:45", "name": "TEST_Marker_A", "enabled": True},
            {"start": "20:00", "end": "22:30", "name": "TEST_Marker_B", "enabled": False},
        ]
        _post_settings({
            "custom_sessions": marker_sessions,
            "pre_signal_enabled": False,
            "enabled_strategies": ["rsi_only", "scalping_4_rules"],
            "active_strategy": "rsi_only",
        })
        before = _get_settings()
        assert before["custom_sessions"] == marker_sessions
        assert before["pre_signal_enabled"] is False
        assert before["active_strategy"] == "rsi_only"

        result = subprocess.run(
            ["sudo", "supervisorctl", "restart", "backend"],
            capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, f"supervisorctl failed: {result.stderr}"
        assert _wait_for_backend(retries=40, delay=1.0), "Backend did not come back"

        after = _get_settings()
        assert after["custom_sessions"] == marker_sessions, (
            f"custom_sessions lost after restart: {after}"
        )
        assert after["pre_signal_enabled"] is False, (
            f"pre_signal_enabled lost after restart: {after}"
        )
        assert after["active_strategy"] == "rsi_only", (
            f"active_strategy lost after restart: {after}"
        )

        # Confirm /api/strategies also reflects the persisted active strategy
        strategies = requests.get(
            f"{BASE_URL}/api/strategies", timeout=TIMEOUT
        ).json()
        assert strategies["active"] == "rsi_only"

    # ---- Telegram keepalive: POST /api/telegram/test still works ----
    def test_15_telegram_test_endpoint(self):
        r = requests.post(f"{BASE_URL}/api/telegram/test",
                          headers=_auth_headers(), timeout=30)
        # Telegram may or may not be configured in this env - both are valid.
        # Configured => 200 + status=success. Not configured => 400 + detail.
        assert r.status_code in (200, 400), (
            f"Unexpected status {r.status_code}: {r.text}"
        )
        body = r.json()
        if r.status_code == 200:
            assert body.get("status") == "success", body
        else:
            assert "not configured" in body.get("detail", "").lower()
