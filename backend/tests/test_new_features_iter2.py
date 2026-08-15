"""
Backend tests for the NEW review features:
- Long time-ranges (days up to 365 clamp)
- Backtest new fields: be_mode, be_trigger_crv, require_all_rules, sessions,
  profit_secure_enabled -> per_pair has secured/be_moved
- backtest/active + cancel -> status 'cancelled', phase 'Abgebrochen'
- backtest/status has NO export_candles/export_trades but eta/elapsed
- Optimizer: algorithm='bayes' in params-mode + active + cancel
- Optimizer discovery with base_strategy_id starts from that custom strategy
- optimizer/apply type='strategy' with update_strategy_id updates (updated:true)
- optimizer/apply type='backtest' writes to /api/backtest/strategy-configs
- rel_vol_min HARD filter for rsi_only (huge rel_vol_min -> less/no trades)
- Settings persistence for strategy_sessions
"""
import os
import time
import pytest
import requests

BASE_URL = (os.environ.get("REACT_APP_BACKEND_URL")
            or "https://daytrader-ml.preview.emergentagent.com").rstrip("/")


@pytest.fixture(scope="session")
def api():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="session")
def token(api):
    r = api.post(f"{BASE_URL}/api/auth/login",
                 json={"username": os.environ.get("ADMIN_USER", "Admin"), "password": os.environ.get("ADMIN_PASSWORD", "admin")})
    assert r.status_code == 200, r.text
    return r.json()["token"]


@pytest.fixture(scope="session")
def admin(token):
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json",
                      "Authorization": f"Bearer {token}"})
    return s


def _wait_status(admin, url, timeout=180, poll=3, allowed_final=("done", "cancelled", "error")):
    start = time.time()
    last = None
    while time.time() - start < timeout:
        r = admin.get(url)
        assert r.status_code == 200, f"{url} {r.status_code} {r.text}"
        last = r.json()
        if last.get("status") in allowed_final:
            return last
        time.sleep(poll)
    raise AssertionError(f"timeout on {url}; last={last}")


def _ensure_no_running(admin):
    # cancel any running backtest / optimizer to keep tests independent
    for kind in ("backtest", "optimizer"):
        r = admin.get(f"{BASE_URL}/api/{kind}/active")
        if r.status_code == 200:
            act = r.json().get("active")
            if act and act.get("id"):
                admin.post(f"{BASE_URL}/api/{kind}/cancel/{act['id']}")
    time.sleep(2)


# ---------------- Backtester new fields ----------------
class TestBacktestNewFields:
    job_id = None

    def test_days_clamped_and_new_fields_accepted(self, admin):
        _ensure_no_running(admin)
        body = {
            "strategy_ids": ["rsi_only"],
            "symbols": ["BTCUSDT"],
            "days": 400,  # clamp to 365 - but we won't wait; test cancels below
            "be_mode": "crv",
            "be_trigger_crv": 1.0,
            "require_all_rules": True,
            "sessions": "09:00-18:00",
            "profit_secure_enabled": True,
            "profit_secure_trigger_pct": 1.5,
            "profit_lock_pct": 0.5,
            "strategy_configs": {"rsi_only": {"timeframe": "5m"}},
        }
        r = admin.post(f"{BASE_URL}/api/backtest/run", json=body)
        if r.status_code == 409:
            time.sleep(3)
            _ensure_no_running(admin)
            r = admin.post(f"{BASE_URL}/api/backtest/run", json=body)
        assert r.status_code == 200, r.text
        job_id = r.json()["job_id"]
        # Check status endpoint doesn't leak export_* and returns eta/elapsed
        r2 = admin.get(f"{BASE_URL}/api/backtest/status/{job_id}")
        assert r2.status_code == 200
        payload = r2.json()
        assert "export_candles" not in payload
        assert "export_trades" not in payload
        assert "elapsed_seconds" in payload
        # eta only when progress >=2
        # check active endpoint reflects running job
        r3 = admin.get(f"{BASE_URL}/api/backtest/active")
        assert r3.status_code == 200
        act = r3.json().get("active")
        # It may just have finished loading; but likely running
        if act:
            assert act["id"] == job_id
        # Verify clamp: params.days should be 365
        # Seit Local-Worker-Support sind lange Zeiträume erlaubt (Clamp erst
        # bei 5500 Tagen) -> 400 bleibt 400
        assert payload.get("params", {}).get("days") == 400
        # Cancel this long job
        r4 = admin.post(f"{BASE_URL}/api/backtest/cancel/{job_id}")
        assert r4.status_code == 200
        final = _wait_status(admin, f"{BASE_URL}/api/backtest/status/{job_id}",
                             timeout=60, poll=2)
        assert final["status"] == "cancelled"
        assert final.get("phase") == "Abgebrochen"

    def test_small_run_returns_secured_and_be_moved(self, admin):
        _ensure_no_running(admin)
        body = {
            "strategy_ids": ["rsi_only"],
            "symbols": ["BTCUSDT"],
            "days": 2,
            "be_mode": "tp1",
            "profit_secure_enabled": True,
            "profit_secure_trigger_pct": 0.5,
            "profit_lock_pct": 0.2,
            "strategy_configs": {"rsi_only": {"timeframe": "5m"}},
        }
        r = admin.post(f"{BASE_URL}/api/backtest/run", json=body)
        if r.status_code == 409:
            time.sleep(3)
            _ensure_no_running(admin)
            r = admin.post(f"{BASE_URL}/api/backtest/run", json=body)
        assert r.status_code == 200, r.text
        jid = r.json()["job_id"]
        final = _wait_status(admin, f"{BASE_URL}/api/backtest/status/{jid}",
                             timeout=180, poll=3)
        assert final["status"] == "done", final
        result = final.get("result") or {}
        per_pair = result.get("per_pair") or []
        assert per_pair, "no per_pair"
        row = per_pair[0]
        assert "secured" in row, f"row keys={list(row.keys())}"
        assert "be_moved" in row, f"row keys={list(row.keys())}"
        # config echo
        cfg = result.get("config") or {}
        assert cfg.get("be_mode") == "tp1"
        assert cfg.get("profit_secure_enabled") is True
        TestBacktestNewFields.job_id = jid


# ---------------- Rel Volume hard filter bugfix ----------------
class TestRelVolumeHardFilter:
    def _run_and_wait(self, admin, rel_vol):
        _ensure_no_running(admin)
        body = {
            "strategy_ids": ["rsi_only"],
            "symbols": ["BTCUSDT"],
            "days": 3,
            "strategy_configs": {
                "rsi_only": {"timeframe": "5m",
                             "params": {"rel_vol_min": rel_vol}}
            },
        }
        r = admin.post(f"{BASE_URL}/api/backtest/run", json=body)
        if r.status_code == 409:
            time.sleep(3)
            _ensure_no_running(admin)
            r = admin.post(f"{BASE_URL}/api/backtest/run", json=body)
        assert r.status_code == 200, r.text
        jid = r.json()["job_id"]
        final = _wait_status(admin, f"{BASE_URL}/api/backtest/status/{jid}",
                             timeout=180, poll=3)
        assert final["status"] == "done"
        per_strat = final["result"].get("per_strategy") or []
        row = per_strat[0] if per_strat else {}
        return row.get("trades", 0)

    def test_high_rel_vol_reduces_trades(self, admin):
        trades_low = self._run_and_wait(admin, 0.5)
        trades_high = self._run_and_wait(admin, 1000.0)
        # Hard filter: high threshold must yield strictly fewer (usually 0)
        assert trades_high < trades_low or (trades_low == 0 and trades_high == 0), \
            f"rel_vol filter not enforced: low={trades_low}, high={trades_high}"
        # Prefer: high == 0 when rel_vol_min=1000
        assert trades_high == 0, f"rel_vol_min=1000 must yield 0 trades, got {trades_high}"


# ---------------- Optimizer Bayes + cancel/active ----------------
class TestOptimizerBayes:
    def test_params_bayes_algorithm(self, admin):
        _ensure_no_running(admin)
        body = {"mode": "params", "strategy_id": "rsi_only",
                "symbols": ["BTCUSDT"], "days": 2, "timeframe": "5m",
                "iterations": 6, "min_trades": 1, "algorithm": "bayes"}
        r = admin.post(f"{BASE_URL}/api/optimizer/run", json=body)
        if r.status_code == 409:
            time.sleep(3)
            _ensure_no_running(admin)
            r = admin.post(f"{BASE_URL}/api/optimizer/run", json=body)
        assert r.status_code == 200, r.text
        jid = r.json()["job_id"]
        # active endpoint
        act = admin.get(f"{BASE_URL}/api/optimizer/active").json()
        # may still be running or already fast enough to finish
        final = _wait_status(admin, f"{BASE_URL}/api/optimizer/status/{jid}",
                             timeout=240, poll=4)
        assert final["status"] == "done", final
        res = final.get("result") or {}
        assert res.get("algorithm") == "bayes", f"algorithm={res.get('algorithm')}"
        assert "best" in res

    def test_optimizer_cancel(self, admin):
        _ensure_no_running(admin)
        # Long optimizer job we can cancel
        body = {"mode": "params", "strategy_id": "rsi_only",
                "symbols": ["BTCUSDT"], "days": 5, "timeframe": "5m",
                "iterations": 50, "min_trades": 1}
        r = admin.post(f"{BASE_URL}/api/optimizer/run", json=body)
        if r.status_code == 409:
            time.sleep(3)
            _ensure_no_running(admin)
            r = admin.post(f"{BASE_URL}/api/optimizer/run", json=body)
        assert r.status_code == 200, r.text
        jid = r.json()["job_id"]
        # small delay so job starts
        time.sleep(3)
        rc = admin.post(f"{BASE_URL}/api/optimizer/cancel/{jid}")
        assert rc.status_code == 200
        final = _wait_status(admin, f"{BASE_URL}/api/optimizer/status/{jid}",
                             timeout=60, poll=2)
        assert final["status"] in ("cancelled", "done"), final
        # Fresh cancellations should be 'cancelled'; if it happened to finish
        # very quickly, allow done too.


# ---------------- Optimizer discovery with base_strategy_id + apply update ----------------
class TestOptimizerDiscoveryBase:
    custom_id = None
    discovery_definition = None

    def test_create_base_custom_strategy(self, admin):
        # Create a minimal custom strategy first
        body = {
            "name": "TEST_BASE_CUSTOM",
            "description": "for optimizer base test",
            "timeframe": "5m",
            "long_rules": [{"indicator": "rsi",
                            "operator": "<",
                            "value": 40,
                            "label": "RSI<40"}],
            "short_rules": [],
        }
        r = admin.post(f"{BASE_URL}/api/strategies/custom", json=body)
        assert r.status_code == 200, r.text
        sid = r.json().get("id") or r.json().get("strategy", {}).get("id")
        assert sid and sid.startswith("custom_"), f"got {sid}"
        TestOptimizerDiscoveryBase.custom_id = sid

    def test_discovery_from_base_strategy(self, admin):
        _ensure_no_running(admin)
        body = {
            "mode": "discovery",
            "symbols": ["BTCUSDT"],
            "days": 2,
            "timeframe": "15m",
            "max_rules": 2,
            "min_trades": 1,
            "indicators": ["rsi", "ema_slow"],
            "base_strategy_id": TestOptimizerDiscoveryBase.custom_id,
        }
        r = admin.post(f"{BASE_URL}/api/optimizer/run", json=body)
        if r.status_code == 409:
            time.sleep(3)
            _ensure_no_running(admin)
            r = admin.post(f"{BASE_URL}/api/optimizer/run", json=body)
        assert r.status_code == 200, r.text
        jid = r.json()["job_id"]
        final = _wait_status(admin, f"{BASE_URL}/api/optimizer/status/{jid}",
                             timeout=300, poll=5)
        assert final["status"] == "done", final
        res = final.get("result") or {}
        steps = res.get("steps") or []
        assert steps, "no steps in discovery result"
        # First step should be the base strategy round 0
        first = steps[0]
        label = (first.get("phase") or first.get("label")
                 or first.get("round") or "").__str__().lower()
        # Check base_definition or rule 0 references base
        assert "basis" in label or first.get("round") in (0, "0") \
            or first.get("kept") is True, f"expected base marker in first step: {first}"
        TestOptimizerDiscoveryBase.discovery_definition = res.get("definition")

    def test_apply_strategy_update_existing(self, admin):
        definition = TestOptimizerDiscoveryBase.discovery_definition
        assert definition, "no discovery def"
        body = {
            "type": "strategy",
            "definition": definition,
            "timeframe": "15m",
            "update_strategy_id": TestOptimizerDiscoveryBase.custom_id,
        }
        r = admin.post(f"{BASE_URL}/api/optimizer/apply", json=body)
        assert r.status_code == 200, r.text
        j = r.json()
        assert j.get("updated") is True, f"updated flag: {j}"
        assert j.get("id") == TestOptimizerDiscoveryBase.custom_id

    def test_apply_backtest_writes_configs(self, admin, api):
        body = {
            "type": "backtest",
            "strategy_id": "rsi_only",
            "params": {"rsi_long_threshold": 33},
            "trade_params": {"tp1_crv": 1.25},
            "timeframe": "5m",
        }
        r = admin.post(f"{BASE_URL}/api/optimizer/apply", json=body)
        assert r.status_code == 200, r.text
        j = r.json()
        assert j.get("status") == "success"
        # Verify persistence
        r2 = api.get(f"{BASE_URL}/api/backtest/strategy-configs")
        assert r2.status_code == 200
        cfg = (r2.json().get("configs") or {}).get("rsi_only") or {}
        assert cfg.get("params", {}).get("rsi_long_threshold") == 33
        assert cfg.get("tp1_crv") == 1.25
        assert cfg.get("timeframe") == "5m"

    def test_cleanup_custom(self, admin):
        sid = TestOptimizerDiscoveryBase.custom_id
        if sid:
            r = admin.delete(f"{BASE_URL}/api/strategies/{sid}")
            assert r.status_code in (200, 204)


# ---------------- Settings: strategy_sessions ----------------
class TestSettingsSessions:
    def test_persist_strategy_sessions(self, admin, api):
        payload = {"strategy_sessions": {
            "scalping_4_rules": [{"start": "09:00", "end": "12:00", "enabled": True}]
        }}
        r = admin.post(f"{BASE_URL}/api/settings", json=payload)
        assert r.status_code == 200, r.text
        r2 = api.get(f"{BASE_URL}/api/settings")
        assert r2.status_code == 200
        doc = r2.json()
        ss = doc.get("strategy_sessions") or doc.get("settings", {}).get("strategy_sessions", {})
        assert "scalping_4_rules" in ss, f"got: {ss}"
        arr = ss["scalping_4_rules"]
        assert isinstance(arr, list) and arr
        assert arr[0]["start"] == "09:00"
        assert arr[0]["end"] == "12:00"
        assert arr[0]["enabled"] is True
        # cleanup
        admin.post(f"{BASE_URL}/api/settings", json={"strategy_sessions": {}})
