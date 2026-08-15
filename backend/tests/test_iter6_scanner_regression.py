"""Iteration 6: Regression tests for scanner freshness refactor (scheduler.py).
Read-only endpoints only – no strategy/trade/config mutations, no AI, no telegram."""
import os
import time
import requests

BASE = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")


def _get(path, **kwargs):
    """Retry 3x on Cloudflare 502/503/504 (preview cold-start)."""
    last = None
    for _ in range(3):
        r = requests.get(f"{BASE}{path}", timeout=90, **kwargs)
        if r.status_code not in (502, 503, 504):
            return r
        last = r
        time.sleep(5)
    return last


class TestScannerRegression:
    def test_signals_endpoint_ok(self):
        r = _get("/api/signals")
        assert r.status_code == 200, r.text[:400]
        data = r.json()
        assert isinstance(data, (list, dict))

    def test_session_status_active(self):
        r = _get("/api/session/status")
        assert r.status_code == 200, r.text[:400]
        data = r.json()
        active_val = data.get("is_active", data.get("active", data.get("session_active")))
        assert active_val is True, f"Session not active: {data}"

    def test_autotrade_trades_ok(self):
        r = _get("/api/autotrade/trades")
        assert r.status_code == 200, r.text[:400]
        data = r.json()
        assert isinstance(data, (list, dict))

    def test_autotrade_config_ok(self):
        r = _get("/api/autotrade/config")
        assert r.status_code == 200, r.text[:400]
        data = r.json()
        assert isinstance(data, dict)

    def test_klines_endpoint_recent(self):
        """Neueste 1m-Kerze BTCUSDT: forming timestamp < 10 min alt (freshness)."""
        r = _get("/api/klines/BTCUSDT", params={"limit": 5})
        assert r.status_code == 200, r.text[:400]
        data = r.json()
        candles = data if isinstance(data, list) else data.get("candles", data.get("data", []))
        assert candles and len(candles) >= 2, f"No candles returned: {data}"
        last_ts = candles[-1]["timestamp"] if isinstance(candles[-1], dict) else candles[-1][0]
        now_ms = int(time.time() * 1000)
        age_min = (now_ms - int(last_ts)) / 60000
        assert age_min < 10, f"BTCUSDT letzte Kerze ist {age_min:.1f} min alt"
