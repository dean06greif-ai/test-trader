"""
Iteration 4 – Backend tests for German daytrading site upgrade:
  - Auth
  - Strategy backup export/import (delete + restore 1:1)
  - Backtest run with auto-leverage flags + 1440-day cap
  - Backtest date_from/date_to filtering
  - Backtest strategy_configs.definition override for custom strategies
  - Equity endpoint + equity CSV export
  - Optimizer params with new optimize groups (tpsl, breakeven, auto_leverage, leverage)
  - Optimizer discovery with trade_params
  - Autotrade per-coin auto_leverage persist

External URL from REACT_APP_BACKEND_URL. Admin login = { username:'Admin', password:'admin' }.
"""

import os
import time
import copy
import pytest
import requests
from datetime import datetime, timedelta, timezone

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://daytrader-ml.preview.emergentagent.com").rstrip("/")
API = f"{BASE_URL}/api"

ADMIN_USER = "Admin"
ADMIN_PW = "admin"


@pytest.fixture(scope="session")
def api_client():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="session")
def admin_token(api_client):
    r = api_client.post(f"{API}/auth/login",
                        json={"username": ADMIN_USER, "password": ADMIN_PW}, timeout=15)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    tok = r.json().get("token")
    assert tok
    return tok


@pytest.fixture(scope="session")
def admin_client(api_client, admin_token):
    api_client.headers.update({"Authorization": f"Bearer {admin_token}"})
    return api_client


# ---------- Auth ----------
class TestAuth:
    def test_login_admin(self, api_client):
        r = api_client.post(f"{API}/auth/login",
                            json={"username": ADMIN_USER, "password": ADMIN_PW}, timeout=15)
        assert r.status_code == 200
        d = r.json()
        assert "token" in d and isinstance(d["token"], str) and len(d["token"]) > 20
        assert d.get("user") in (ADMIN_USER, "Admin")


# ---------- Strategy backup export/import ----------
def _find_custom_id(admin_client):
    r = admin_client.get(f"{API}/strategies", timeout=15)
    assert r.status_code == 200
    for s in r.json().get("strategies", []):
        if s.get("is_custom"):
            return s["id"]
    return None


class TestStrategyBackup:
    def test_export_custom_strategy(self, admin_client):
        cid = _find_custom_id(admin_client)
        if not cid:
            pytest.skip("no custom strategy available")
        r = admin_client.get(f"{API}/strategies/{cid}/export", timeout=15)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["type"] == "strategy_backup"
        assert d["strategy_id"] == cid
        assert d.get("is_custom") is True
        assert isinstance(d.get("definition"), dict), "definition dict must be present for custom"
        # containers exist (may be empty)
        assert "strategy_params" in d
        assert "strategy_coin_configs" in d
        assert "backtest_config" in d

    def test_export_import_roundtrip_delete_restore(self, admin_client):
        cid = _find_custom_id(admin_client)
        if not cid:
            pytest.skip("no custom strategy available")

        # ensure a per-coin autotrade override with distinctive leverage exists
        put = admin_client.post(
            f"{API}/autotrade/strategy/{cid}/coin/BTCUSDT",
            json={"mode": "off", "enabled": False, "leverage": 27,
                  "auto_leverage_enabled": True, "auto_lev_mode": "liq_pct",
                  "auto_lev_value": 0.5, "auto_lev_max": 40},
            timeout=15,
        )
        assert put.status_code == 200, put.text

        # export
        ex = admin_client.get(f"{API}/strategies/{cid}/export", timeout=15).json()
        assert ex["strategy_id"] == cid
        assert ex["strategy_coin_configs"].get("BTCUSDT", {}).get("leverage") == 27
        assert ex["strategy_coin_configs"]["BTCUSDT"].get("auto_leverage_enabled") is True

        backup = copy.deepcopy(ex)

        # delete
        d = admin_client.delete(f"{API}/strategies/{cid}", timeout=15)
        assert d.status_code == 200, d.text
        # confirm gone
        after = admin_client.get(f"{API}/strategies", timeout=15).json().get("strategies", [])
        assert cid not in {s["id"] for s in after}

        # import
        imp = admin_client.post(f"{API}/strategies/import", json=backup, timeout=20)
        assert imp.status_code == 200, imp.text
        j = imp.json()
        assert j.get("status") == "success"
        assert j.get("id") == cid

        # verify restored 1:1
        again = admin_client.get(f"{API}/strategies", timeout=15).json().get("strategies", [])
        assert cid in {s["id"] for s in again}, "strategy not restored"

        ex2 = admin_client.get(f"{API}/strategies/{cid}/export", timeout=15).json()
        assert isinstance(ex2.get("definition"), dict)
        # rules identical
        assert ex2["definition"].get("long_rules") == backup["definition"].get("long_rules")
        assert ex2["definition"].get("short_rules") == backup["definition"].get("short_rules")
        # coin config leverage restored
        assert ex2["strategy_coin_configs"].get("BTCUSDT", {}).get("leverage") == 27
        assert ex2["strategy_coin_configs"]["BTCUSDT"].get("auto_leverage_enabled") is True


# ---------- Backtest helpers ----------
def _wait_backtest(admin_client, job_id, timeout=180):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = admin_client.get(f"{API}/backtest/status/{job_id}", timeout=15)
        assert r.status_code == 200, r.text
        j = r.json()
        if j["status"] in ("done", "error", "cancelled"):
            return j
        time.sleep(2)
    pytest.fail(f"backtest {job_id} timed out after {timeout}s")


class TestBacktestAutoLevAndDays:
    def test_backtest_auto_leverage_and_pnl_pct(self, admin_client):
        r = admin_client.post(f"{API}/backtest/run", json={
            "strategy_ids": ["rsi_only"],
            "symbols": ["BTCUSDT"],
            "days": 2,
            "timeframe": "5m",
            "auto_leverage_enabled": True,
            "auto_lev_mode": "liq_pct",
            "auto_lev_value": 0.5,
            "auto_lev_max": 40,
            "use_fast_path": True,
        }, timeout=20)
        assert r.status_code == 200, r.text
        job_id = r.json()["job_id"]
        j = _wait_backtest(admin_client, job_id, timeout=180)
        assert j["status"] == "done", j
        result = j.get("result") or {}
        per_strategy = result.get("per_strategy") or []
        assert per_strategy, "per_strategy missing"
        row = per_strategy[0]
        assert "pnl_pct" in row, f"pnl_pct missing: keys={list(row.keys())}"
        assert "max_drawdown_pct" in row
        # per_pair avg_leverage
        pp = result.get("per_pair") or []
        assert pp, "per_pair missing"
        assert "avg_leverage" in pp[0]

    def test_backtest_days_cap_accepts_1440(self, admin_client):
        # do NOT run 1440 days (too slow); instead confirm API accepts by
        # checking the job params echo, then cancel immediately.
        r = admin_client.post(f"{API}/backtest/run", json={
            "strategy_ids": ["rsi_only"],
            "symbols": ["BTCUSDT"],
            "days": 1440,
            "timeframe": "1h",
            "use_fast_path": True,
        }, timeout=20)
        assert r.status_code == 200, r.text
        job_id = r.json()["job_id"]
        st = admin_client.get(f"{API}/backtest/status/{job_id}", timeout=15).json()
        assert st.get("params", {}).get("days") == 1440
        # cancel to free the slot
        admin_client.post(f"{API}/backtest/cancel/{job_id}", timeout=15)
        # wait short for cancellation before other tests
        for _ in range(10):
            j = admin_client.get(f"{API}/backtest/status/{job_id}", timeout=15).json()
            if j["status"] in ("done", "cancelled", "error"):
                break
            time.sleep(1)
        # force reset if still running
        admin_client.post(f"{API}/backtest/reset", timeout=15)

    def test_backtest_date_range(self, admin_client):
        # ensure no running job
        admin_client.post(f"{API}/backtest/reset", timeout=15)
        df = (datetime.now(timezone.utc) - timedelta(days=10)).replace(microsecond=0).isoformat()
        dt = (datetime.now(timezone.utc) - timedelta(days=5)).replace(microsecond=0).isoformat()
        r = admin_client.post(f"{API}/backtest/run", json={
            "strategy_ids": ["rsi_only"],
            "symbols": ["BTCUSDT"],
            "days": 3,  # will be overridden by date_from
            "timeframe": "1h",
            "date_from": df,
            "date_to": dt,
            "use_fast_path": True,
        }, timeout=20)
        assert r.status_code == 200, r.text
        job_id = r.json()["job_id"]
        j = _wait_backtest(admin_client, job_id, timeout=180)
        assert j["status"] == "done", j
        params = j.get("params", {})
        assert params.get("date_from") == df
        assert params.get("date_to") == dt


class TestBacktestDefinitionOverride:
    def test_backtest_strategy_configs_definition_override(self, admin_client):
        cid = _find_custom_id(admin_client)
        if not cid:
            pytest.skip("no custom strategy available")
        # get existing definition
        ex = admin_client.get(f"{API}/strategies/{cid}/export", timeout=15).json()
        defn = ex.get("definition") or {}
        long_rules = list(defn.get("long_rules") or [])
        short_rules = list(defn.get("short_rules") or [])
        indicators = dict(defn.get("indicators") or {})
        # override: same structure but a slightly modified numeric value (does
        # not need to trigger trades – just proves the API path runs)
        if long_rules and isinstance(long_rules[0], dict) and "value" in long_rules[0]:
            try:
                long_rules[0]["value"] = float(long_rules[0]["value"]) + 1
            except Exception:
                pass

        override_defn = {"long_rules": long_rules, "short_rules": short_rules,
                         "indicators": indicators}

        admin_client.post(f"{API}/backtest/reset", timeout=15)
        r = admin_client.post(f"{API}/backtest/run", json={
            "strategy_ids": [cid],
            "symbols": ["BTCUSDT"],
            "days": 2,
            "timeframe": "5m",
            "use_fast_path": True,
            "strategy_configs": {cid: {"definition": override_defn}},
        }, timeout=20)
        assert r.status_code == 200, r.text
        job_id = r.json()["job_id"]
        j = _wait_backtest(admin_client, job_id, timeout=180)
        assert j["status"] == "done", j.get("error")


class TestEquityEndpoint:
    def test_equity_and_csv(self, admin_client):
        # run a small backtest and use its job_id
        admin_client.post(f"{API}/backtest/reset", timeout=15)
        r = admin_client.post(f"{API}/backtest/run", json={
            "strategy_ids": ["rsi_only"],
            "symbols": ["BTCUSDT"],
            "days": 2,
            "timeframe": "5m",
            "use_fast_path": True,
        }, timeout=20)
        assert r.status_code == 200
        job_id = r.json()["job_id"]
        j = _wait_backtest(admin_client, job_id, timeout=180)
        assert j["status"] == "done"
        # equity endpoint
        eq = admin_client.get(f"{API}/backtest/equity/{job_id}", timeout=15)
        assert eq.status_code == 200, eq.text
        d = eq.json()
        assert d.get("job_id") == job_id
        assert isinstance(d.get("points"), list)
        # if any trades happened, verify shape
        if d["points"]:
            p = d["points"][0]
            for k in ("t", "equity", "drawdown", "side", "symbol", "liquidated"):
                assert k in p, f"equity point missing {k}"
        # CSV export
        csv = admin_client.get(f"{API}/backtest/export/{job_id}?kind=equity", timeout=15)
        assert csv.status_code == 200
        text = csv.text
        head = text.splitlines()[0]
        for col in ("t", "equity", "peak", "drawdown"):
            assert col in head, f"csv header missing {col}: {head}"


# ---------- Optimizer ----------
def _wait_optimizer(admin_client, job_id, timeout=240):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = admin_client.get(f"{API}/optimizer/status/{job_id}", timeout=15).json()
        if r["status"] in ("done", "error", "cancelled"):
            return r
        time.sleep(2)
    pytest.fail(f"optimizer {job_id} timeout")


class TestOptimizer:
    def test_optimizer_params_with_new_groups(self, admin_client):
        admin_client.post(f"{API}/optimizer/reset", timeout=15)
        r = admin_client.post(f"{API}/optimizer/run", json={
            "mode": "params",
            "strategy_id": "rsi_only",
            "symbols": ["BTCUSDT"],
            "days": 2,
            "timeframe": "5m",
            "iterations": 8,
            "min_trades": 1,
            "objective": "pnl",
            "algorithm": "random",
            "optimize": {"tpsl": True, "breakeven": True,
                         "auto_leverage": True, "leverage": True},
            "use_fast_path": True,
        }, timeout=20)
        assert r.status_code == 200, r.text
        job_id = r.json()["job_id"]
        j = _wait_optimizer(admin_client, job_id, timeout=240)
        assert j["status"] == "done", j.get("error")
        result = j.get("result") or {}
        best = (result.get("best") or {})
        tp = best.get("trade_params")
        # trade_params key must always exist on best (may be empty {} when
        # baseline wins). If populated, keys must come from the selected groups.
        assert tp is not None, f"best.trade_params missing (best keys: {list(best.keys())})"
        allowed = {"tp1_crv", "tp_full_crv", "sl_lookback", "tp1_close_percent",
                   "sl_mode", "sl_fixed_percent", "atr_sl_multiplier",
                   "be_mode", "be_trigger_crv", "be_trigger_profit_pct",
                   "leverage", "auto_leverage_enabled",
                   "auto_lev_mode", "auto_lev_value", "auto_lev_max"}
        if tp:
            unexpected = set(tp.keys()) - allowed
            assert not unexpected, f"unexpected trade_params keys: {unexpected}"
        metrics = best.get("metrics") or {}
        assert "pnl_pct" in metrics
        assert "max_drawdown_pct" in metrics

    def test_optimizer_discovery_trade_params(self, admin_client):
        admin_client.post(f"{API}/optimizer/reset", timeout=15)
        r = admin_client.post(f"{API}/optimizer/run", json={
            "mode": "discovery",
            "symbols": ["BTCUSDT"],
            "days": 2,
            "timeframe": "5m",
            "iterations": 6,
            "min_trades": 2,
            "max_rules": 2,
            "indicators": ["rsi", "ema_slow"],
            "objective": "pnl",
            "optimize": {"tpsl": True},
            "use_fast_path": True,
        }, timeout=20)
        assert r.status_code == 200, r.text
        job_id = r.json()["job_id"]
        j = _wait_optimizer(admin_client, job_id, timeout=300)
        assert j["status"] == "done", j.get("error")
        result = j.get("result") or {}
        # discovery result usually has 'trade_params' at top-level or under best
        has_tp = "trade_params" in result or "trade_params" in (result.get("best") or {})
        assert has_tp, f"discovery result missing trade_params: keys={list(result.keys())}"


# ---------- Autotrade auto_leverage ----------
class TestAutotradeAutoLeverage:
    def test_set_and_read_auto_leverage(self, admin_client):
        sid = "rsi_only"
        sym = "BTCUSDT"
        r = admin_client.post(f"{API}/autotrade/strategy/{sid}/coin/{sym}", json={
            "mode": "off", "enabled": False, "leverage": 8,
            "auto_leverage_enabled": True, "auto_lev_mode": "liq_ticks",
            "auto_lev_value": 5, "auto_lev_max": 60,
        }, timeout=15)
        assert r.status_code == 200, r.text
        g = admin_client.get(f"{API}/autotrade/strategy/{sid}/coin/{sym}", timeout=15)
        assert g.status_code == 200
        cfg = g.json()
        # config may be wrapped
        c = cfg.get("config", cfg)
        assert c.get("auto_leverage_enabled") is True
        assert c.get("auto_lev_mode") == "liq_ticks"
        assert c.get("auto_lev_value") == 5
        assert c.get("auto_lev_max") == 60
