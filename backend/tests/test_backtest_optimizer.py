"""
Backend tests for Backtester + Optimizer + new strategies + strategy-configs.
Covers the review_request checklist end-to-end.
"""
import os
import time
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://daytrader-ml.preview.emergentagent.com").rstrip("/")

NEW_STRATEGIES = {"macd_rsi_momentum", "bollinger_squeeze", "vwap_reversion", "stoch_reversal"}


# ---------- Fixtures ----------
@pytest.fixture(scope="session")
def api_client():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="session")
def admin_token(api_client):
    r = api_client.post(f"{BASE_URL}/api/auth/login",
                        json={"username": os.environ.get("ADMIN_USER", "Admin"), "password": os.environ.get("ADMIN_PASSWORD", "admin")})
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    data = r.json()
    assert "token" in data and isinstance(data["token"], str) and len(data["token"]) > 20
    return data["token"]


@pytest.fixture(scope="session")
def admin_client(admin_token):
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json",
                      "Authorization": f"Bearer {admin_token}"})
    return s


def _wait_for_job(client, url, timeout=180, poll=3):
    start = time.time()
    last = None
    while time.time() - start < timeout:
        r = client.get(url)
        assert r.status_code == 200, f"status endpoint {r.status_code} {r.text}"
        last = r.json()
        st = last.get("status")
        if st in ("done", "success", "completed"):
            return last
        if st in ("error", "failed"):
            raise AssertionError(f"job failed: {last}")
        time.sleep(poll)
    raise AssertionError(f"job timeout after {timeout}s, last={last}")


# ---------- Strategien / Auth ----------
class TestStrategies:
    def test_list_contains_9_including_new(self, api_client):
        r = api_client.get(f"{BASE_URL}/api/strategies")
        assert r.status_code == 200
        data = r.json()
        strategies = data.get("strategies", [])
        ids = {s["id"] for s in strategies}
        # Mindestens die 9 Kern-Strategien; Custom-Strategien kommen dazu
        assert len(strategies) >= 9, f"expected >=9, got {len(strategies)}: {ids}"
        for sid in NEW_STRATEGIES:
            assert sid in ids, f"missing new strategy {sid}"
        # verify params metadata is present for the new ones
        by_id = {s["id"]: s for s in strategies}
        for sid in NEW_STRATEGIES:
            s = by_id[sid]
            assert "params" in s, f"{sid} missing params metadata"
            assert isinstance(s["params"], (dict, list)), f"{sid} params wrong type"
            # non-empty parameter metadata
            assert len(s["params"]) > 0, f"{sid} params empty"


class TestAuth:
    def test_login_success(self, api_client):
        r = api_client.post(f"{BASE_URL}/api/auth/login",
                            json={"username": os.environ.get("ADMIN_USER", "Admin"), "password": os.environ.get("ADMIN_PASSWORD", "admin")})
        assert r.status_code == 200
        d = r.json()
        assert d.get("user") in ("Admin", None) or d.get("user")
        assert isinstance(d.get("token"), str)

    def test_login_wrong_pw(self, api_client):
        r = api_client.post(f"{BASE_URL}/api/auth/login",
                            json={"username": os.environ.get("ADMIN_USER", "Admin"), "password": "wrong"})
        assert r.status_code == 401


# ---------- Backtester ----------
class TestBacktester:
    """Backtest with per-strategy config incl. timeframes + CSV export."""
    job_id = None

    def test_run_with_per_strategy_configs(self, admin_client):
        body = {
            "strategy_ids": ["rsi_only", "macd_rsi_momentum"],
            "symbols": ["BTCUSDT"],
            "days": 2,
            "strategy_configs": {
                "rsi_only": {"timeframe": "5m", "tp1_crv": 1.5,
                             "params": {"rsi_long_threshold": 35}},
                "macd_rsi_momentum": {"timeframe": "15m"},
            },
        }
        r = admin_client.post(f"{BASE_URL}/api/backtest/run", json=body)
        # If a previous run is in progress (409), just retry after short wait
        if r.status_code == 409:
            time.sleep(5)
            r = admin_client.post(f"{BASE_URL}/api/backtest/run", json=body)
        assert r.status_code == 200, f"run failed: {r.status_code} {r.text}"
        job = r.json()
        assert job["status"] == "started"
        assert "job_id" in job
        TestBacktester.job_id = job["job_id"]

    def test_status_finishes_and_has_per_strategy_timeframes(self, admin_client):
        assert TestBacktester.job_id, "job_id from previous test missing"
        job = _wait_for_job(admin_client,
                            f"{BASE_URL}/api/backtest/status/{TestBacktester.job_id}",
                            timeout=240, poll=4)
        result = job.get("result") or {}
        per_raw = result.get("per_strategy") or []
        # per_strategy is a sorted list of dicts
        per = {p.get("strategy_id"): p for p in per_raw} if isinstance(per_raw, list) else per_raw
        assert "rsi_only" in per and "macd_rsi_momentum" in per, f"missing per_strategy entries: {list(per.keys())}"
        assert per["rsi_only"].get("timeframe") == "5m", f"rsi_only tf: {per['rsi_only'].get('timeframe')}"
        assert per["macd_rsi_momentum"].get("timeframe") == "15m", f"macd tf: {per['macd_rsi_momentum'].get('timeframe')}"
        # aggregated map
        stf = result.get("strategy_timeframes") or {}
        assert stf.get("rsi_only") == "5m"
        assert stf.get("macd_rsi_momentum") == "15m"

    def test_export_trades_csv(self, admin_client):
        assert TestBacktester.job_id
        r = admin_client.get(f"{BASE_URL}/api/backtest/export/{TestBacktester.job_id}?kind=trades")
        assert r.status_code == 200, f"csv trades: {r.status_code} {r.text[:200]}"
        text = r.text
        # header
        first_line = text.splitlines()[0]
        for col in ("strategy_id", "symbol", "timeframe", "side", "opened",
                    "entry", "exit", "sl_initial", "tp1", "result", "pnl",
                    "rsi_entry", "entry_candle_open"):
            assert col in first_line, f"missing csv header col: {col}"
        # data rows may be 0 or more depending on signals; verify csv parses
        import csv, io
        rows = list(csv.DictReader(io.StringIO(text)))
        # allow zero-trade case but header must be present
        assert isinstance(rows, list)

    def test_export_candles_csv(self, admin_client):
        assert TestBacktester.job_id
        r = admin_client.get(f"{BASE_URL}/api/backtest/export/{TestBacktester.job_id}?kind=candles")
        assert r.status_code == 200, f"csv candles: {r.status_code} {r.text[:200]}"
        text = r.text
        first_line = text.splitlines()[0]
        for col in ("symbol", "timeframe", "timestamp", "open", "high",
                    "low", "close", "volume"):
            assert col in first_line, f"missing candles col: {col}"
        # should contain BTCUSDT rows
        assert "BTCUSDT" in text

    def test_strategy_configs_get_and_set(self, api_client, admin_client):
        # POST requires admin
        payload = {"configs": {"rsi_only": {"timeframe": "5m",
                                            "params": {"rsi_long_threshold": 32}}}}
        r_unauth = api_client.post(f"{BASE_URL}/api/backtest/strategy-configs",
                                   json=payload)
        assert r_unauth.status_code in (401, 403), f"unauth POST should fail: {r_unauth.status_code}"
        r = admin_client.post(f"{BASE_URL}/api/backtest/strategy-configs", json=payload)
        assert r.status_code == 200, f"admin POST failed: {r.status_code} {r.text}"
        # GET
        r2 = api_client.get(f"{BASE_URL}/api/backtest/strategy-configs")
        assert r2.status_code == 200
        got = r2.json().get("configs", {})
        assert got.get("rsi_only", {}).get("timeframe") == "5m"


# ---------- Optimizer ----------
class TestOptimizer:
    params_job_id = None
    discovery_job_id = None
    discovery_definition = None

    def _wait_for_no_running(self, admin_client):
        # small sleep to let previous jobs be marked done
        time.sleep(2)

    def test_run_params_mode(self, admin_client):
        self._wait_for_no_running(admin_client)
        body = {"mode": "params", "strategy_id": "rsi_only",
                "symbols": ["BTCUSDT"], "days": 2, "timeframe": "5m",
                "iterations": 6, "min_trades": 3}
        r = admin_client.post(f"{BASE_URL}/api/optimizer/run", json=body)
        if r.status_code == 409:
            time.sleep(8)
            r = admin_client.post(f"{BASE_URL}/api/optimizer/run", json=body)
        assert r.status_code == 200, f"optimizer run: {r.status_code} {r.text}"
        d = r.json()
        assert d["status"] == "started" and "job_id" in d
        TestOptimizer.params_job_id = d["job_id"]

    def test_params_status_and_result(self, admin_client):
        assert TestOptimizer.params_job_id
        job = _wait_for_job(admin_client,
                            f"{BASE_URL}/api/optimizer/status/{TestOptimizer.params_job_id}",
                            timeout=300, poll=5)
        result = job.get("result") or {}
        assert "baseline" in result, f"missing baseline: keys={list(result.keys())}"
        best = result.get("best")
        assert best, f"missing best: {result}"
        # Best may contain params/trade_params/metrics
        assert "metrics" in best, f"best missing metrics: {best}"
        # top list should exist
        assert "top" in result and isinstance(result["top"], list)

    def test_apply_params_writes_settings(self, admin_client):
        # Read best params
        r = admin_client.get(f"{BASE_URL}/api/optimizer/status/{TestOptimizer.params_job_id}")
        assert r.status_code == 200
        best = (r.json().get("result") or {}).get("best") or {}
        params = best.get("params") or {"rsi_long_threshold": 33}
        body = {"type": "params", "strategy_id": "rsi_only", "params": params}
        r2 = admin_client.post(f"{BASE_URL}/api/optimizer/apply", json=body)
        assert r2.status_code == 200, f"apply params: {r2.status_code} {r2.text}"
        # Verify persisted (settings at root, no "settings" wrapper)
        r3 = admin_client.get(f"{BASE_URL}/api/settings")
        assert r3.status_code == 200
        settings_doc = r3.json()
        sp = settings_doc.get("strategy_params") or settings_doc.get("settings", {}).get("strategy_params", {})
        assert "rsi_only" in sp, f"strategy_params keys: {list(sp.keys())}"
        for k, v in params.items():
            assert sp["rsi_only"].get(k) == v, f"param {k} not persisted"

    def test_run_discovery_mode(self, admin_client):
        self._wait_for_no_running(admin_client)
        body = {"mode": "discovery", "symbols": ["BTCUSDT"], "days": 2,
                "timeframe": "15m", "max_rules": 2, "min_trades": 3,
                "indicators": ["rsi", "ema_slow", "macd_hist"]}
        r = admin_client.post(f"{BASE_URL}/api/optimizer/run", json=body)
        if r.status_code == 409:
            time.sleep(8)
            r = admin_client.post(f"{BASE_URL}/api/optimizer/run", json=body)
        assert r.status_code == 200, f"discovery run: {r.status_code} {r.text}"
        TestOptimizer.discovery_job_id = r.json()["job_id"]

    def test_discovery_result_has_definition(self, admin_client):
        job = _wait_for_job(admin_client,
                            f"{BASE_URL}/api/optimizer/status/{TestOptimizer.discovery_job_id}",
                            timeout=300, poll=5)
        result = job.get("result") or {}
        definition = result.get("definition")
        assert definition, f"missing definition: keys={list(result.keys())}"
        assert "long_rules" in definition or "short_rules" in definition
        # step / rule labels
        assert "steps" in result and isinstance(result["steps"], list)
        # rules field with labels can be checked loosely
        TestOptimizer.discovery_definition = definition

    def test_apply_strategy_creates_custom(self, admin_client, api_client):
        definition = TestOptimizer.discovery_definition
        if not definition:
            pytest.skip("no discovery definition")
        body = {"type": "strategy", "definition": definition,
                "timeframe": "15m", "name": "TEST_OPT_STRATEGY"}
        r = admin_client.post(f"{BASE_URL}/api/optimizer/apply", json=body)
        assert r.status_code == 200, f"apply strategy: {r.status_code} {r.text}"
        new_id = r.json().get("id")
        assert new_id and new_id.startswith("custom_")
        # Verify visible in list
        r2 = api_client.get(f"{BASE_URL}/api/strategies")
        assert r2.status_code == 200
        ids = {s["id"] for s in r2.json().get("strategies", [])}
        assert new_id in ids
        # Cleanup
        r3 = admin_client.delete(f"{BASE_URL}/api/strategies/{new_id}")
        assert r3.status_code in (200, 204)
        r4 = api_client.get(f"{BASE_URL}/api/strategies")
        ids2 = {s["id"] for s in r4.json().get("strategies", [])}
        assert new_id not in ids2, "custom strategy not deleted"


# ---------- Settings: strategy_timeframes ----------
class TestSettingsTimeframes:
    def test_set_and_reset_strategy_timeframes(self, admin_client, api_client):
        # SET
        r = admin_client.post(f"{BASE_URL}/api/settings",
                              json={"strategy_timeframes": {"rsi_only": "5m"}})
        assert r.status_code == 200, f"post settings: {r.status_code} {r.text}"
        # GET verify (settings at root)
        r2 = api_client.get(f"{BASE_URL}/api/settings")
        assert r2.status_code == 200
        doc = r2.json()
        stf = doc.get("strategy_timeframes") or doc.get("settings", {}).get("strategy_timeframes", {})
        assert stf.get("rsi_only") == "5m", f"stf: {stf}"
        # RESET
        r3 = admin_client.post(f"{BASE_URL}/api/settings",
                               json={"strategy_timeframes": {}})
        assert r3.status_code == 200
        r4 = api_client.get(f"{BASE_URL}/api/settings")
        doc2 = r4.json()
        stf2 = doc2.get("strategy_timeframes") or doc2.get("settings", {}).get("strategy_timeframes", {})
        assert stf2 == {} or (isinstance(stf2, dict) and stf2.get("rsi_only") is None), f"reset failed: {stf2}"
