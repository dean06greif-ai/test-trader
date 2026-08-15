"""
Iteration 3 backend tests:
 - Auth login
 - /api/system/ram
 - /api/system/cache/clear
 - /api/backtest/run (fast-path & legacy)  + status polling
 - /api/backtest/reset, /api/optimizer/reset
 - /api/backtest/apply -> /api/autotrade/strategy/{sid}/coin/{sym}
 - strategy_configs per-strategy (timeframe override)
"""
import os
import time
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://daytrader-ml.preview.emergentagent.com").rstrip("/")
TIMEOUT = 30


# ---------- Fixtures ----------
@pytest.fixture(scope="session")
def api():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="session")
def admin_token(api):
    r = api.post(f"{BASE_URL}/api/auth/login",
                 json={"username": os.environ.get("ADMIN_USER", "Admin"), "password": os.environ.get("ADMIN_PASSWORD", "admin")}, timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    tok = r.json().get("token")
    assert tok
    return tok


@pytest.fixture(scope="session")
def admin(api, admin_token):
    api.headers.update({"Authorization": f"Bearer {admin_token}"})
    return api


# ---------- Auth ----------
class TestAuth:
    def test_login_success(self, api):
        r = api.post(f"{BASE_URL}/api/auth/login",
                     json={"username": os.environ.get("ADMIN_USER", "Admin"), "password": os.environ.get("ADMIN_PASSWORD", "admin")}, timeout=TIMEOUT)
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data.get("token"), str) and len(data["token"]) > 20
        assert data.get("user") == "Admin"

    def test_login_wrong_password(self, api):
        r = api.post(f"{BASE_URL}/api/auth/login",
                     json={"username": os.environ.get("ADMIN_USER", "Admin"), "password": "nope"}, timeout=TIMEOUT)
        assert r.status_code == 401


# ---------- system/ram + cache/clear ----------
class TestSystemRam:
    def test_ram_shape(self, api):
        r = api.get(f"{BASE_URL}/api/system/ram", timeout=TIMEOUT)
        assert r.status_code == 200
        d = r.json()
        for k in ("process_rss_mb", "candle_cache", "backtest_exports", "breakdown_hint"):
            assert k in d, f"missing {k}: {d.keys()}"
        cc = d["candle_cache"]
        for k in ("total_candles", "estimated_mb", "symbols", "max_candles"):
            assert k in cc
        assert isinstance(cc["total_candles"], int)

    def test_cache_clear_requires_admin(self, api):
        # Sanity: without token -> 401
        r = requests.post(f"{BASE_URL}/api/system/cache/clear", timeout=TIMEOUT)
        assert r.status_code in (401, 403)

    def test_cache_clear_with_admin(self, admin):
        r = admin.post(f"{BASE_URL}/api/system/cache/clear", timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d.get("status") == "cleared"
        assert "candles_freed" in d
        # follow-up: candle_cache.total_candles should be 0 now
        r2 = admin.get(f"{BASE_URL}/api/system/ram", timeout=TIMEOUT)
        assert r2.status_code == 200
        assert r2.json()["candle_cache"]["total_candles"] == 0


# ---------- reset endpoints ----------
class TestResetEndpoints:
    def test_backtest_reset(self, admin):
        r = admin.post(f"{BASE_URL}/api/backtest/reset", timeout=TIMEOUT)
        assert r.status_code == 200
        d = r.json()
        assert d.get("status") == "reset"

    def test_optimizer_reset(self, admin):
        r = admin.post(f"{BASE_URL}/api/optimizer/reset", timeout=TIMEOUT)
        assert r.status_code == 200
        d = r.json()
        assert d.get("status") == "reset"


# ---------- backtest run + status ----------
def _poll_job(admin, job_id, max_seconds=120):
    end = time.time() + max_seconds
    last = None
    while time.time() < end:
        r = admin.get(f"{BASE_URL}/api/backtest/status/{job_id}", timeout=TIMEOUT)
        if r.status_code == 200:
            last = r.json()
            st = last.get("status")
            if st in ("done", "error", "cancelled"):
                return last
        time.sleep(2)
    return last


class TestBacktestRun:
    def test_backtest_fast_path_liquidations(self, admin):
        body = {
            "strategy_ids": ["rsi_only"],
            "symbols": ["BTCUSDT"],
            "days": 2,
            "leverage": 100,
            "use_fast_path": True,
        }
        r = admin.post(f"{BASE_URL}/api/backtest/run", json=body, timeout=TIMEOUT)
        assert r.status_code in (200, 409), r.text
        if r.status_code == 409:
            # reset and retry
            admin.post(f"{BASE_URL}/api/backtest/reset", timeout=TIMEOUT)
            r = admin.post(f"{BASE_URL}/api/backtest/run", json=body, timeout=TIMEOUT)
            assert r.status_code == 200, r.text
        job_id = r.json()["job_id"]
        job = _poll_job(admin, job_id, max_seconds=180)
        assert job is not None and job.get("status") == "done", f"job did not finish: {job}"
        result = job.get("result") or {}
        per_strat = result.get("per_strategy") or []
        assert per_strat, f"per_strategy missing: {result}"
        row = per_strat[0]
        assert "liquidations" in row, f"liquidations field missing: {row.keys()}"
        assert isinstance(row["liquidations"], (int, float))
        assert row["liquidations"] >= 0

    def test_backtest_legacy_path(self, admin):
        # ensure no other job running
        admin.post(f"{BASE_URL}/api/backtest/reset", timeout=TIMEOUT)
        body = {
            "strategy_ids": ["rsi_only"],
            "symbols": ["BTCUSDT"],
            "days": 1,
            "use_fast_path": False,
        }
        r = admin.post(f"{BASE_URL}/api/backtest/run", json=body, timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        job_id = r.json()["job_id"]
        job = _poll_job(admin, job_id, max_seconds=240)
        assert job is not None and job.get("status") == "done", f"legacy job did not finish: {job}"

    def test_backtest_strategy_configs_timeframe(self, admin):
        admin.post(f"{BASE_URL}/api/backtest/reset", timeout=TIMEOUT)
        body = {
            "strategy_ids": ["bollinger_reversion"],
            "symbols": ["BTCUSDT"],
            "days": 2,
            "strategy_configs": {
                "bollinger_reversion": {"timeframe": "15m", "tp_mode": "structure"},
            },
        }
        r = admin.post(f"{BASE_URL}/api/backtest/run", json=body, timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        job_id = r.json()["job_id"]
        job = _poll_job(admin, job_id, max_seconds=180)
        assert job is not None and job.get("status") == "done", f"cfg job did not finish: {job}"
        result = job.get("result") or {}
        per_pair = result.get("per_pair") or []
        assert per_pair, f"per_pair missing: {result}"
        tfs = [p.get("timeframe") for p in per_pair]
        assert "15m" in tfs, f"expected timeframe 15m in per_pair: {tfs}"


# ---------- backtest/apply -> autotrade ----------
class TestBacktestApply:
    def test_apply_paper_and_verify(self, admin):
        body = {
            "strategy_id": "rsi_only",
            "symbols": ["BTCUSDT"],
            "mode": "paper",
            "config": {
                "leverage": 5,
                "tp_mode": "fixed_pct",
                "tp1_percent": 0.4,
                "tp_full_percent": 0.9,
            },
        }
        r = admin.post(f"{BASE_URL}/api/backtest/apply", json=body, timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d.get("status") == "success"
        assert d.get("mode") == "paper"
        assert "BTCUSDT" in (d.get("symbols") or [])

        # verify persisted config
        r2 = admin.get(
            f"{BASE_URL}/api/autotrade/strategy/rsi_only/coin/BTCUSDT", timeout=TIMEOUT)
        assert r2.status_code == 200
        cfg = r2.json().get("config") or {}
        assert cfg.get("mode") == "paper"
        assert cfg.get("enabled") is True
        assert cfg.get("leverage") == 5
        assert cfg.get("tp_mode") == "fixed_pct"
        assert float(cfg.get("tp1_percent")) == 0.4
        assert float(cfg.get("tp_full_percent")) == 0.9
