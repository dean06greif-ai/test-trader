"""Backend regression for rly-3.1 review: 1Y history endpoint + regressions for 7d/30d."""
import os
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL").rstrip("/")


@pytest.fixture(scope="module")
def s():
    session = requests.Session()
    session.headers.update({"Content-Type": "application/json"})
    return session


class TestKlinesHistory:
    def test_days_365_daily(self, s):
        r = s.get(f"{BASE_URL}/api/klines/BTCUSDT/history?days=365", timeout=45)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d.get("timeframe") == "1d", f"expected 1d, got {d.get('timeframe')}"
        candles = d.get("candles", [])
        n = len(candles)
        assert 340 <= n <= 380, f"expected ~365 candles, got {n}"
        # plausible closes
        closes = [float(c["close"]) if isinstance(c, dict) else float(c[4]) for c in candles[-5:]]
        for c in closes:
            assert 1000 < c < 500000, f"implausible BTC close: {c}"
        # candles must be time-ordered and roughly one per day
        _tk = "timestamp" if (isinstance(candles[0], dict) and "timestamp" in candles[0]) else "time"
        times = [c[_tk] if isinstance(c, dict) else c[0] for c in candles]
        assert times == sorted(times)
        # normalize to seconds
        def _sec(t):
            t = int(t)
            return t // 1000 if t > 10_000_000_000 else t
        diffs = [_sec(times[i + 1]) - _sec(times[i]) for i in range(len(times) - 1)]
        # median diff should be ~86400 (1 day)
        med = sorted(diffs)[len(diffs) // 2]
        assert 80000 <= med <= 100000, f"median diff not ~1d: {med}"

    def test_days_7_default_15m(self, s):
        r = s.get(f"{BASE_URL}/api/klines/BTCUSDT/history?days=7", timeout=30)
        assert r.status_code == 200
        d = r.json()
        assert d.get("timeframe") == "15m"
        n = len(d.get("candles", []))
        assert 600 <= n <= 720, n

    def test_days_30_default_1h(self, s):
        r = s.get(f"{BASE_URL}/api/klines/BTCUSDT/history?days=30", timeout=30)
        assert r.status_code == 200
        d = r.json()
        assert d.get("timeframe") == "1h"
        n = len(d.get("candles", []))
        assert 650 <= n <= 760, n

    def test_explicit_timeframe_param(self, s):
        r = s.get(f"{BASE_URL}/api/klines/BTCUSDT/history?days=7&timeframe=30m", timeout=30)
        assert r.status_code == 200
        d = r.json()
        assert d.get("timeframe") == "30m"
        n = len(d.get("candles", []))
        assert 300 <= n <= 400, n
