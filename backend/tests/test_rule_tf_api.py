"""API-Level Integration-Tests fuer Multi-Timeframe pro Regel Feature.

Deckt ab:
  - Admin-Login /api/auth/login
  - GET /api/strategies/builder-options -> rule_timeframes Liste
  - POST /api/strategies/custom (mit/ohne Overrides, Validierung)
  - POST /api/backtest/run + Status Polling (MTF und Regression)
  - POST /api/optimizer/run + Status Polling (mit/ohne rule_timeframes)
"""
import os
import time
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL").rstrip("/")
ADMIN_USER = "Admin"
ADMIN_PASS = "Dean06Greif!/Admin"
MTF_STRAT_ID = "custom_ba25570d"


@pytest.fixture(scope="module")
def api():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"username": ADMIN_USER, "password": ADMIN_PASS}, timeout=15)
    assert r.status_code == 200, f"Login failed: {r.status_code} {r.text}"
    tok = r.json().get("token")
    assert tok
    s.headers.update({"Authorization": f"Bearer {tok}"})
    return s


@pytest.fixture(scope="module")
def created_ids():
    ids = []
    yield ids
    # cleanup nur eigens angelegte Test-Strategien
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"username": ADMIN_USER, "password": ADMIN_PASS}, timeout=15)
    if r.status_code == 200:
        s.headers.update({"Authorization": f"Bearer {r.json()['token']}",
                          "Content-Type": "application/json"})
        for sid in ids:
            try:
                s.delete(f"{BASE_URL}/api/strategies/custom/{sid}", timeout=10)
            except Exception:
                pass


def _poll(api, kind, job_id, timeout=180):
    """Poll /api/{kind}/status/{job_id} bis done oder timeout."""
    start = time.time()
    last = None
    while time.time() - start < timeout:
        r = api.get(f"{BASE_URL}/api/{kind}/status/{job_id}", timeout=15)
        if r.status_code == 200:
            last = r.json()
            st = (last.get("status") or last.get("state") or "").lower()
            if st in ("done", "finished", "completed", "success", "error", "failed"):
                return last
        time.sleep(2)
    return last or {}


class TestAuth:
    def test_login_ok(self, api):
        # api-Fixture bereits authentifiziert; teste zusaetzlich falschen Pass
        r = requests.post(f"{BASE_URL}/api/auth/login",
                          json={"username": ADMIN_USER, "password": "WRONG"}, timeout=15)
        assert r.status_code in (401, 403)


class TestBuilderOptions:
    def test_rule_timeframes_field(self, api):
        r = api.get(f"{BASE_URL}/api/strategies/builder-options", timeout=15)
        assert r.status_code == 200, r.text
        data = r.json()
        assert "rule_timeframes" in data, f"missing key, got {list(data)[:20]}"
        expected = ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "8h", "12h", "1d"]
        assert data["rule_timeframes"] == expected


def _def_with_overrides(base_tf="1m", long_tfs=("1h", "5m", None), name="TEST_MTF_API"):
    long_rules = []
    for i, tf in enumerate(long_tfs):
        r = {"indicator": "rsi", "op": "<", "value": 50 + i}
        if tf:
            r["timeframe"] = tf
        long_rules.append(r)
    return {
        "name": name,
        "timeframe": base_tf,
        "indicators": {"rsi_period": 14},
        "long_rules": long_rules,
        "short_rules": [{"indicator": "rsi", "op": ">", "value": 70}],
    }


class TestCustomStrategyCRUD:
    def test_create_with_tf_overrides(self, api, created_ids):
        payload = _def_with_overrides()
        r = api.post(f"{BASE_URL}/api/strategies/custom", json=payload, timeout=20)
        assert r.status_code in (200, 201), f"{r.status_code} {r.text}"
        data = r.json()
        # id kann unter verschiedenen Keys liegen
        sid = data.get("id") or data.get("strategy_id") or (data.get("strategy") or {}).get("id")
        assert sid, f"no id in response: {data}"
        created_ids.append(sid)
        # Verify via list GET (kein GET /custom/{id} Endpoint)
        rg = api.get(f"{BASE_URL}/api/strategies", timeout=15)
        assert rg.status_code == 200, rg.text
        arr = rg.json() if isinstance(rg.json(), list) else rg.json().get("strategies", [])
        found = next((s for s in arr if s.get("id") == sid), None)
        assert found, f"strategy {sid} not in list"
        definition = found.get("definition") or {}
        tfs = [r.get("timeframe") for r in definition["long_rules"]]
        assert tfs[0] == "1h"
        assert tfs[1] == "5m"
        # dritter ohne Override sollte kein "timeframe" haben
        assert not definition["long_rules"][2].get("timeframe")

    def test_reject_lower_than_base(self, api):
        payload = _def_with_overrides(base_tf="15m", long_tfs=("5m",),
                                      name="TEST_MTF_BAD_LOW")
        r = api.post(f"{BASE_URL}/api/strategies/custom", json=payload, timeout=15)
        assert r.status_code == 422, f"expected 422, got {r.status_code}: {r.text}"

    def test_reject_invalid_tf(self, api):
        payload = _def_with_overrides(base_tf="1m", long_tfs=("7m",),
                                      name="TEST_MTF_BAD_TF")
        r = api.post(f"{BASE_URL}/api/strategies/custom", json=payload, timeout=15)
        assert r.status_code == 422, f"expected 422, got {r.status_code}: {r.text}"


class TestBacktest:
    def test_mtf_strategy_backtest(self, api):
        body = {
            "strategy_ids": [MTF_STRAT_ID],
            "symbols": ["BTCUSDT"],
            "days": 3,
            "execution": "cloud",
        }
        r = api.post(f"{BASE_URL}/api/backtest/run", json=body, timeout=30)
        assert r.status_code == 200, r.text
        j = r.json()
        job_id = j.get("job_id") or j.get("id")
        assert job_id, j
        result = _poll(api, "backtest", job_id, timeout=240)
        st = (result.get("status") or result.get("state") or "").lower()
        assert st in ("done", "finished", "completed", "success"), \
            f"backtest not done: {result}"
        # trades key
        res = result.get("result") or result
        assert "per_pair" in res or "results" in res or "trades" in res, \
            f"unexpected result keys: {list(res)[:20]}"

    def test_regression_scalping(self, api):
        body = {"strategy_ids": ["scalping_4_rules"], "symbols": ["BTCUSDT"],
                "days": 2, "execution": "cloud"}
        r = api.post(f"{BASE_URL}/api/backtest/run", json=body, timeout=30)
        assert r.status_code == 200, r.text
        job_id = r.json().get("job_id") or r.json().get("id")
        result = _poll(api, "backtest", job_id, timeout=240)
        st = (result.get("status") or result.get("state") or "").lower()
        assert st in ("done", "finished", "completed", "success"), \
            f"regression backtest failed: {result}"


class TestOptimizer:
    def test_with_rule_timeframes(self, api):
        body = {
            "mode": "params",
            "strategy_id": MTF_STRAT_ID,
            "symbols": ["BTCUSDT"],
            "days": 3,
            "timeframe": "1m",
            "iterations": 40,
            "min_trades": 1,
            "rule_timeframes": {"enabled": True, "min": "1m", "max": "4h"},
            "optimize": {"tpsl": False},
        }
        r = api.post(f"{BASE_URL}/api/optimizer/run", json=body, timeout=30)
        assert r.status_code == 200, r.text
        job_id = r.json().get("job_id") or r.json().get("id")
        assert job_id
        result = _poll(api, "optimizer", job_id, timeout=300)
        st = (result.get("status") or result.get("state") or "").lower()
        assert st in ("done", "finished", "completed", "success"), \
            f"optimizer not done: {result}"
        res = result.get("result") or result
        best = res.get("best") or {}
        params = best.get("params") or {}
        tf_keys = [k for k in params if k.endswith("_tf")]
        assert tf_keys, f"expected *_tf keys in best.params, got {list(params)}"

    def test_without_rule_timeframes_backcompat(self, api):
        body = {
            "mode": "params",
            "strategy_id": MTF_STRAT_ID,
            "symbols": ["BTCUSDT"],
            "days": 3,
            "timeframe": "1m",
            "iterations": 6,
            "min_trades": 1,
            "optimize": {"tpsl": False},
        }
        r = api.post(f"{BASE_URL}/api/optimizer/run", json=body, timeout=30)
        assert r.status_code == 200, r.text
        job_id = r.json().get("job_id") or r.json().get("id")
        result = _poll(api, "optimizer", job_id, timeout=300)
        st = (result.get("status") or result.get("state") or "").lower()
        assert st in ("done", "finished", "completed", "success"), \
            f"optimizer not done: {result}"
        res = result.get("result") or result
        best = res.get("best") or {}
        params = best.get("params") or {}
        tf_keys = [k for k in params if k.endswith("_tf")]
        assert tf_keys == [], f"unexpected *_tf keys: {tf_keys}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
