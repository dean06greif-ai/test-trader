"""
Read-only tests for the new /api/strategies endpoint (Multi-Strategy system).

Mutating tests for `active_strategy` live in test_settings_persistence.py so
that pytest-xdist loadscope keeps all settings-mutating tests on one worker
(they share the global scanner singleton).
"""
import os
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


class TestStrategiesEndpoint:
    """Read-only checks for GET /api/strategies."""

    def test_strategies_200_and_shape(self):
        r = requests.get(f"{BASE_URL}/api/strategies", timeout=TIMEOUT)
        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
        data = r.json()
        assert isinstance(data, dict)
        assert "strategies" in data
        assert "active" in data
        assert isinstance(data["strategies"], list)
        assert isinstance(data["active"], str)

    def test_strategies_list_contains_scalping_and_rsi_only(self):
        data = requests.get(f"{BASE_URL}/api/strategies", timeout=TIMEOUT).json()
        ids = [s["id"] for s in data["strategies"]]
        assert "scalping_4_rules" in ids, f"Missing scalping_4_rules in {ids}"
        assert "rsi_only" in ids, f"Missing rsi_only in {ids}"

    def test_strategies_each_has_required_metadata(self):
        data = requests.get(f"{BASE_URL}/api/strategies", timeout=TIMEOUT).json()
        for strat in data["strategies"]:
            for field in ["id", "name", "description", "timeframe"]:
                assert field in strat, f"Strategy missing '{field}': {strat}"
            assert isinstance(strat["id"], str) and strat["id"]
            assert isinstance(strat["name"], str) and strat["name"]
            assert isinstance(strat["description"], str)
            assert isinstance(strat["timeframe"], str)

    def test_strategies_active_is_one_of_the_listed(self):
        data = requests.get(f"{BASE_URL}/api/strategies", timeout=TIMEOUT).json()
        ids = [s["id"] for s in data["strategies"]]
        assert data["active"] in ids, (
            f"active '{data['active']}' not in strategy ids {ids}"
        )

    def test_get_settings_exposes_active_strategy(self):
        r = requests.get(f"{BASE_URL}/api/settings", timeout=TIMEOUT)
        assert r.status_code == 200
        data = r.json()
        assert "active_strategy" in data, f"active_strategy missing in settings: {data}"
        assert isinstance(data["active_strategy"], str)
