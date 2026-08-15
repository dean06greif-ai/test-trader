"""
Backend tests for new features:
- Health, Strategies list, Builder-Options (25 indicators, 15 period_fields)
- Auth login
- Strategy Comparison (with filter)
- Backtest run/status/results with fees
- Per-Strategy Per-Coin autotrade config (public GET, admin POST, profit_secure fields)
- Custom Strategy CRUD
"""
import os
import time
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    with open("/app/frontend/.env") as f:
        for ln in f:
            if ln.startswith("REACT_APP_BACKEND_URL="):
                BASE_URL = ln.split("=", 1)[1].strip().rstrip("/")

TIMEOUT = 30


# ---------- fixture-style helper ----------
def _login_admin():
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={"username": os.environ.get("ADMIN_USER", "Admin"),
                            "password": os.environ.get("ADMIN_PASSWORD", "admin")},
                      timeout=TIMEOUT)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    return r.json()["token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


# ---------- Health ----------
class TestHealth:
    def test_health_alive(self):
        r = requests.get(f"{BASE_URL}/api/health", timeout=TIMEOUT)
        assert r.status_code == 200
        assert r.json() == {"status": "alive"}


# ---------- Strategies ----------
class TestStrategies:
    def test_strategies_list_has_5_builtin(self):
        r = requests.get(f"{BASE_URL}/api/strategies", timeout=TIMEOUT)
        assert r.status_code == 200
        data = r.json()
        assert "strategies" in data
        # 5 built-in strategies
        ids = [s["id"] for s in data["strategies"]]
        # count only non-custom
        builtins = [s for s in data["strategies"] if not s.get("definition")]
        assert len(builtins) >= 5, f"Expected >=5 built-in strategies, got {len(builtins)}: {ids}"

    def test_builder_options_25_indicators(self):
        r = requests.get(f"{BASE_URL}/api/strategies/builder-options", timeout=TIMEOUT)
        assert r.status_code == 200
        data = r.json()
        assert "indicators" in data and "operators" in data
        assert "indicator_meta" in data and "period_fields" in data
        assert len(data["indicators"]) >= 25, f"Expected >=25 indicators, got {len(data['indicators'])}"
        assert len(data["period_fields"]) >= 15, f"Expected >=15 period_fields, got {len(data['period_fields'])}"


# ---------- Auth ----------
class TestAuth:
    def test_login_admin_ok(self):
        r = requests.post(f"{BASE_URL}/api/auth/login",
                          json={"username": os.environ.get("ADMIN_USER", "Admin"),
                                "password": os.environ.get("ADMIN_PASSWORD", "admin")},
                          timeout=TIMEOUT)
        assert r.status_code == 200
        assert "token" in r.json()

    def test_login_bad_credentials(self):
        r = requests.post(f"{BASE_URL}/api/auth/login",
                          json={"username": os.environ.get("ADMIN_USER", "Admin"), "password": "wrong"},
                          timeout=TIMEOUT)
        assert r.status_code in (401, 400, 403)


# ---------- Strategy Comparison ----------
class TestStrategyComparison:
    def test_comparison_all_mode(self):
        r = requests.get(f"{BASE_URL}/api/analytics/strategy-comparison?mode=all",
                         timeout=TIMEOUT)
        assert r.status_code == 200
        data = r.json()
        assert "comparison" in data
        assert isinstance(data["comparison"], list)
        # each row (if any) must have trades (per-row key; total_trades ist top-level)
        for row in data["comparison"]:
            assert "trades" in row

    def test_comparison_paper_mode_with_days(self):
        r = requests.get(f"{BASE_URL}/api/analytics/strategy-comparison?mode=paper&days=7",
                         timeout=TIMEOUT)
        assert r.status_code == 200
        assert "comparison" in r.json()


# ---------- Backtest ----------
class TestBacktest:
    def test_backtest_run_without_admin_401(self):
        r = requests.post(f"{BASE_URL}/api/backtest/run",
                          json={"strategy_ids": ["rsi_only"], "symbols": ["BTCUSDT"],
                                "days": 1},
                          timeout=TIMEOUT)
        assert r.status_code in (401, 403), f"Expected 401/403, got {r.status_code}"

    def test_backtest_invalid_ids_400(self):
        token = _login_admin()
        r = requests.post(f"{BASE_URL}/api/backtest/run",
                          json={"strategy_ids": ["totally_bogus_id_xyz"],
                                "symbols": ["BTCUSDT"], "days": 1},
                          headers=_auth(token), timeout=TIMEOUT)
        assert r.status_code == 400, f"Expected 400, got {r.status_code}: {r.text}"

    def test_backtest_full_flow(self):
        token = _login_admin()
        r = requests.post(
            f"{BASE_URL}/api/backtest/run",
            json={"strategy_ids": ["rsi_only", "scalping_4_rules"],
                  "symbols": ["BTCUSDT"], "days": 1},
            headers=_auth(token), timeout=TIMEOUT)
        # 409 if a job is already running from a prior call - retry once after wait
        if r.status_code == 409:
            time.sleep(30)
            r = requests.post(
                f"{BASE_URL}/api/backtest/run",
                json={"strategy_ids": ["rsi_only", "scalping_4_rules"],
                      "symbols": ["BTCUSDT"], "days": 1},
                headers=_auth(token), timeout=TIMEOUT)
        assert r.status_code == 200, f"backtest run failed: {r.status_code} {r.text}"
        job_id = r.json()["job_id"]
        assert isinstance(job_id, str) and len(job_id) > 0

        # poll status
        deadline = time.time() + 120
        status = None
        job = {}
        while time.time() < deadline:
            sr = requests.get(f"{BASE_URL}/api/backtest/status/{job_id}",
                              timeout=TIMEOUT)
            assert sr.status_code == 200
            job = sr.json()
            status = job.get("status")
            if status in ("done", "error"):
                break
            time.sleep(3)
        assert status == "done", f"Backtest not done: status={status}, job={job}"
        result = job.get("result") or {}
        assert "per_strategy" in result
        assert "per_pair" in result
        assert "best_per_symbol" in result
        # fees > 0 if trades > 0 (per_strategy is a sorted list of dicts)
        assert isinstance(result["per_strategy"], list)
        for agg in result["per_strategy"]:
            trades = agg.get("trades", 0)
            fees = agg.get("fees", 0)
            if trades > 0:
                assert fees > 0, f"Strategy {agg.get('strategy_id')} has {trades} trades but fees={fees}"

        # results endpoint
        rr = requests.get(f"{BASE_URL}/api/backtest/results", timeout=TIMEOUT)
        assert rr.status_code == 200
        assert "results" in rr.json()
        assert isinstance(rr.json()["results"], list)


# ---------- Per-Strategy Per-Coin AutoTrade config with profit-secure ----------
class TestStrategyCoinAutoTrade:
    def test_post_and_public_get(self):
        token = _login_admin()
        payload = {"mode": "paper", "enabled": True, "max_capital": 50,
                   "leverage": 10, "profit_secure_enabled": True,
                   "profit_secure_trigger_pct": 30, "profit_lock_pct": 50,
                   "fee_percent": 0.06}
        r = requests.post(
            f"{BASE_URL}/api/autotrade/strategy/rsi_only/coin/BTCUSDT",
            json=payload, headers=_auth(token), timeout=TIMEOUT)
        assert r.status_code == 200, f"POST failed: {r.status_code} {r.text}"
        assert r.json().get("ok") is True

        # public GET (no auth)
        gr = requests.get(
            f"{BASE_URL}/api/autotrade/strategy/rsi_only/coin/BTCUSDT",
            timeout=TIMEOUT)
        assert gr.status_code == 200, f"Public GET failed: {gr.status_code} {gr.text}"
        cfg = gr.json().get("config") or {}
        assert cfg.get("mode") == "paper"
        assert cfg.get("enabled") is True
        assert cfg.get("max_capital") == 50
        assert cfg.get("profit_secure_enabled") is True
        assert cfg.get("profit_secure_trigger_pct") == 30
        assert cfg.get("profit_lock_pct") == 50
        assert cfg.get("fee_percent") == 0.06


# ---------- Custom Strategy CRUD ----------
class TestCustomStrategy:
    def test_create_and_delete_custom_strategy(self):
        token = _login_admin()
        definition = {
            "id": "TEST_custom_new_ind",
            "name": "TEST Custom New Indicators",
            "description": "Test strategy using new indicators",
            "timeframe": "1m",
            "long_rules": [
                {"indicator": "macd", "op": "cross_above", "value": "macd_signal"}
            ],
            "short_rules": [
                {"indicator": "stoch_k", "op": ">", "value": 80}
            ],
            "indicators": {
                "ema_fast_period": 9, "ema_slow_period": 50,
                "rsi_period": 14, "macd_fast": 12, "macd_slow": 26,
                "macd_signal_period": 9,
                "stoch_k_period": 14, "stoch_d_period": 3
            }
        }
        r = requests.post(f"{BASE_URL}/api/strategies/custom",
                          json=definition, headers=_auth(token),
                          timeout=TIMEOUT)
        assert r.status_code == 200, f"Create failed: {r.status_code} {r.text}"
        data = r.json()
        assert data.get("status") == "success"
        sid = data.get("id") or "TEST_custom_new_ind"

        # verify in list
        lr = requests.get(f"{BASE_URL}/api/strategies", timeout=TIMEOUT)
        assert lr.status_code == 200
        ids = [s["id"] for s in lr.json()["strategies"]]
        assert sid in ids, f"Custom strategy {sid} not visible in list: {ids}"

        # delete
        dr = requests.delete(f"{BASE_URL}/api/strategies/custom/{sid}",
                             headers=_auth(token), timeout=TIMEOUT)
        assert dr.status_code == 200, f"Delete failed: {dr.status_code} {dr.text}"

        # verify removed
        lr2 = requests.get(f"{BASE_URL}/api/strategies", timeout=TIMEOUT)
        ids2 = [s["id"] for s in lr2.json()["strategies"]]
        assert sid not in ids2, f"Strategy still present after delete: {ids2}"
