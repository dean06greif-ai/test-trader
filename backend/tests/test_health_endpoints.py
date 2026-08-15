"""
Backend tests for Crypto Scalping Scanner REST endpoints.
Focus: /api/health minimal keepalive endpoint + related read-only endpoints.
Updated for the new keepalive-only /api/health contract ({"status":"alive"}) and
new session_status schema (custom_sessions list, berlin_time/berlin_date).
"""
import os
import time
import requests

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


# ---------- /api/health minimal keepalive endpoint ----------
class TestHealthEndpoint:
    """/api/health is a minimal keepalive: {"status":"alive"}."""

    def test_health_status_code_200(self):
        r = requests.get(f"{BASE_URL}/api/health", timeout=TIMEOUT)
        assert r.status_code == 200

    def test_health_returns_json_dict(self):
        r = requests.get(f"{BASE_URL}/api/health", timeout=TIMEOUT)
        data = r.json()
        assert isinstance(data, dict)

    def test_health_status_alive(self):
        r = requests.get(f"{BASE_URL}/api/health", timeout=TIMEOUT)
        data = r.json()
        assert data.get("status") == "alive"

    def test_health_is_minimal(self):
        """Contract change: keepalive returns ONLY {status: alive}."""
        r = requests.get(f"{BASE_URL}/api/health", timeout=TIMEOUT)
        data = r.json()
        assert data == {"status": "alive"}

    def test_health_is_lightweight_fast(self):
        start = time.time()
        r = requests.get(f"{BASE_URL}/api/health", timeout=TIMEOUT)
        elapsed = time.time() - start
        assert r.status_code == 200
        assert elapsed < 3.0, f"Health endpoint too slow: {elapsed:.2f}s"


# ---------- Root endpoint ----------
# The K8s ingress routes non-/api/* to frontend, so root is only reachable at localhost:8001.
LOCAL_BACKEND = "http://localhost:8001"


class TestRootEndpoint:
    def test_root_200(self):
        r = requests.get(f"{LOCAL_BACKEND}/", timeout=TIMEOUT)
        assert r.status_code == 200

    def test_root_returns_app_and_status(self):
        r = requests.get(f"{LOCAL_BACKEND}/", timeout=TIMEOUT)
        data = r.json()
        assert data.get("app") == "Crypto Scalping Scanner"
        assert data.get("status") == "running"


# ---------- /api/coins endpoint ----------
class TestCoinsEndpoint:
    def test_coins_200(self):
        r = requests.get(f"{BASE_URL}/api/coins", timeout=TIMEOUT)
        assert r.status_code == 200

    def test_coins_full_universe(self):
        data = requests.get(f"{BASE_URL}/api/coins", timeout=TIMEOUT).json()
        assert "coins" in data
        assert isinstance(data["coins"], list)
        # Krypto + Resources + Indices + Forex
        assert len(data["coins"]) > 10
        assert len(data["crypto"]) == 10

    def test_coins_contain_expected_symbols(self):
        coins = requests.get(f"{BASE_URL}/api/coins", timeout=TIMEOUT).json()["coins"]
        for expected in ["BTCUSDT", "ETHUSDT", "SOLUSDT"]:
            assert expected in coins


# ---------- /api/session/status endpoint ----------
class TestSessionStatusEndpoint:
    def test_session_status_200(self):
        r = requests.get(f"{BASE_URL}/api/session/status", timeout=TIMEOUT)
        assert r.status_code == 200

    def test_session_status_fields(self):
        data = requests.get(f"{BASE_URL}/api/session/status", timeout=TIMEOUT).json()
        assert isinstance(data.get("is_active"), bool)
        assert "current_session" in data
        assert "custom_sessions" in data
        assert isinstance(data["custom_sessions"], list)
        assert "pre_signal_enabled" in data
        # New schema: berlin_time (HH:MM:SS) + berlin_date (YYYY-MM-DD) replaced current_time_utc
        assert "berlin_time" in data
        assert "berlin_date" in data

    def test_session_berlin_time_shape(self):
        # Retry once - other test-worker may be doing supervisor restart in parallel
        for _ in range(2):
            try:
                data = requests.get(f"{BASE_URL}/api/session/status", timeout=TIMEOUT).json()
                break
            except Exception:
                time.sleep(2)
        else:
            data = requests.get(f"{BASE_URL}/api/session/status", timeout=TIMEOUT).json()
        # HH:MM:SS
        assert len(data["berlin_time"].split(":")) == 3
        # YYYY-MM-DD
        assert len(data["berlin_date"].split("-")) == 3
