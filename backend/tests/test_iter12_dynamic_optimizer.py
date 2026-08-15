"""Iter 12 review: Optimizer bugfixes + transparency + dynamic strategies.

Tests the review request:
- Discovery optimizer run with checks/rank_reason/fail_reasons + stored trades
- Fast equity endpoint (scope=optimized)
- CSV exports (trades + equity)
- History endpoint with checks_passed/enabled
- Dynamic optimizer run + save/list/refresh/apply/delete
- Backtester regression
- Optimizer apply params with timeframe
"""
import io
import csv
import os
import time
import pytest
import requests

def _load_backend_url():
    v = os.environ.get("REACT_APP_BACKEND_URL")
    if not v:
        try:
            for line in open("/app/frontend/.env"):
                if line.startswith("REACT_APP_BACKEND_URL="):
                    v = line.split("=", 1)[1].strip()
                    break
        except Exception:
            pass
    assert v, "REACT_APP_BACKEND_URL not set"
    return v.rstrip("/")

BASE_URL = _load_backend_url()


@pytest.fixture(scope="module")
def admin_token():
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={"username": os.environ.get("ADMIN_USER", "Admin"), "password": os.environ.get("ADMIN_PASSWORD", "admin")}, timeout=10)
    assert r.status_code == 200, r.text
    return r.json()["token"]


@pytest.fixture(scope="module")
def admin_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


def _start_when_free(body, admin_headers, tries=40, wait_s=6):
    """Optimizer-Lauf starten, ohne fremde Jobs zu canceln.

    Zwei Klassen dieser Datei laufen via xdist parallel – ein reset würde den
    Lauf der jeweils anderen Klasse abbrechen. Stattdessen: bei 409 warten,
    bis der Optimizer frei ist, dann erneut starten."""
    last = None
    for _ in range(tries):
        r = requests.post(f"{BASE_URL}/api/optimizer/run", json=body,
                          headers=admin_headers, timeout=30)
        last = r
        if r.status_code != 409:
            return r
        time.sleep(wait_s)
    return last


def _poll(job_id, timeout=240, interval=3):
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        r = requests.get(f"{BASE_URL}/api/optimizer/status/{job_id}", timeout=15)
        assert r.status_code == 200, r.text
        j = r.json()
        last = j
        status = j.get("status")
        if status in ("done", "error", "canceled"):
            return j
        time.sleep(interval)
    raise AssertionError(f"Timeout waiting for job {job_id}. Last: {last}")


# ------------------------------------------------------------------
# 1) Discovery optimizer with transparency (checks/rank_reason/fail_reasons)
# ------------------------------------------------------------------
class TestDiscoveryOptimizer:
    job_id = None
    top0 = None

    def test_admin_login(self, admin_token):
        assert admin_token

    def test_start_discovery(self, admin_headers):
        body = {
            "mode": "discovery",
            "symbols": ["BTCUSDT"],
            "days": 2,
            "timeframe": "5m",
            "iterations": 6,
            "min_trades": 3,
            "max_rules": 2,
            "indicators": ["rsi", "ema_fast"],
            "optimize": {"tpsl": True},
            "walk_forward": {"enabled": True, "train_pct": 70, "mode": "single"},
            "dd_filter": {"enabled": True, "max_dd_pct": 60},
            "constancy": {"enabled": True, "chunk_days": 1, "max_deviation_pct": 100},
        }
        r = _start_when_free(body, admin_headers)
        assert r.status_code == 200, r.text
        TestDiscoveryOptimizer.job_id = r.json()["job_id"]
        assert TestDiscoveryOptimizer.job_id

    def test_poll_done(self):
        assert TestDiscoveryOptimizer.job_id
        job = _poll(TestDiscoveryOptimizer.job_id, timeout=300)
        assert job["status"] == "done", f"job status={job.get('status')} err={job.get('error')}"
        result = job.get("result") or {}
        top5 = result.get("top5") or []
        assert len(top5) >= 1, f"top5 empty: {result}"
        TestDiscoveryOptimizer.top0 = top5[0]
        em = result.get("export_meta") or {}
        assert (em.get("trades") or 0) > 0, f"export_meta.trades expected >0, got {em}"

    def test_top0_checks(self):
        t0 = TestDiscoveryOptimizer.top0
        assert t0 is not None
        checks = t0.get("checks")
        assert isinstance(checks, list) and len(checks) > 0, f"checks missing: keys={list(t0)}"
        first = checks[0]
        for k in ("id", "label", "enabled", "passed"):
            assert k in first, f"check missing key {k}: {first}"

    def test_top0_rank_reason_and_fail(self):
        t0 = TestDiscoveryOptimizer.top0
        assert isinstance(t0.get("rank_reason"), str) and t0["rank_reason"].strip()
        assert isinstance(t0.get("fail_reasons"), list)

    def test_equity_fast_stored(self):
        assert TestDiscoveryOptimizer.job_id
        t0 = time.time()
        r = requests.get(f"{BASE_URL}/api/optimizer/equity/{TestDiscoveryOptimizer.job_id}",
                         params={"scope": "optimized"}, timeout=20)
        dt = time.time() - t0
        assert r.status_code == 200, r.text
        j = r.json()
        assert j.get("source") == "stored", f"expected stored source, got {j.get('source')}"
        assert isinstance(j.get("points"), list) and len(j["points"]) > 0
        assert dt < 8, f"equity endpoint too slow: {dt:.1f}s"

    def test_export_trades_csv(self):
        r = requests.get(f"{BASE_URL}/api/optimizer/export/{TestDiscoveryOptimizer.job_id}",
                         params={"kind": "trades"}, timeout=30)
        assert r.status_code == 200, r.text
        text = r.text
        reader = csv.reader(io.StringIO(text))
        header = next(reader)
        for col in ("strategy_id", "strategy_name", "symbol", "timeframe"):
            assert col in header, f"missing col {col} in {header}"
        rows = list(reader)
        assert len(rows) > 0

    def test_export_equity_csv(self):
        r = requests.get(f"{BASE_URL}/api/optimizer/export/{TestDiscoveryOptimizer.job_id}",
                         params={"kind": "equity"}, timeout=30)
        assert r.status_code == 200, r.text
        reader = csv.reader(io.StringIO(r.text))
        header = next(reader)
        for col in ("t", "equity", "peak", "drawdown"):
            assert col in header, f"missing col {col} in {header}"

    def test_history_checks(self):
        r = requests.get(f"{BASE_URL}/api/optimizer/history", params={"limit": 25}, timeout=15)
        assert r.status_code == 200, r.text
        j = r.json()
        items = j.get("history") if isinstance(j, dict) else j
        assert isinstance(items, list) and items, "history empty"
        # Deterministisch: den Discovery-Lauf DIESER Klasse prüfen (andere
        # parallele Läufe verschmutzen die History).
        target = next((t for t in items
                       if t.get("id") == TestDiscoveryOptimizer.job_id), None)
        if target is None:
            target = next((t for t in items
                           if isinstance(t.get("checks_enabled"), (int, float))), None)
        assert target is not None, f"no history entry with checks: {items[:3]}"
        assert isinstance(target.get("checks_passed"), (int, float)), f"checks_passed missing: {target}"
        assert isinstance(target.get("checks_enabled"), (int, float)), f"checks_enabled missing: {target}"
        assert "fail_reasons" in target


# ------------------------------------------------------------------
# 2) Optimizer apply params with timeframe
# ------------------------------------------------------------------
class TestOptimizerApplyParams:
    def test_apply_params_with_timeframe(self, admin_headers):
        body = {
            "type": "params",
            "strategy_id": "bollinger_reversion",
            "timeframe": "5m",
            "params": {"bb_period": 20, "bb_std": 2.0},
        }
        r = requests.post(f"{BASE_URL}/api/optimizer/apply", json=body,
                          headers=admin_headers, timeout=15)
        assert r.status_code == 200, r.text
        assert r.json().get("success") or r.json().get("status") in ("ok", "success")

    def test_strategy_configs_timeframe(self):
        r = requests.get(f"{BASE_URL}/api/backtest/strategy-configs", timeout=15)
        assert r.status_code == 200, r.text
        j = r.json()
        cfgs = j.get("configs") if isinstance(j, dict) else j
        if isinstance(cfgs, dict):
            entry = cfgs.get("bollinger_reversion")
        else:
            entry = next((c for c in cfgs if c.get("strategy_id") == "bollinger_reversion"), None)
        assert entry is not None, f"bollinger_reversion missing in configs: {list(cfgs) if isinstance(cfgs, dict) else cfgs}"
        tf = entry.get("timeframe") if isinstance(entry, dict) else None
        assert tf == "5m", f"expected timeframe=5m, got {tf} entry={entry}"


# ------------------------------------------------------------------
# 3) Backtester regression
# ------------------------------------------------------------------
class TestBacktesterRegression:
    job_id = None

    def test_start_backtest(self, admin_headers):
        body = {
            "strategy_ids": ["bollinger_reversion"],
            "symbols": ["BTCUSDT"],
            "days": 2,
            "timeframe": "5m",
        }
        r = requests.post(f"{BASE_URL}/api/backtest/run", json=body,
                          headers=admin_headers, timeout=30)
        assert r.status_code == 200, r.text
        TestBacktesterRegression.job_id = r.json().get("job_id")
        assert TestBacktesterRegression.job_id

    def test_poll_backtest(self):
        jid = TestBacktesterRegression.job_id
        t0 = time.time()
        while time.time() - t0 < 240:
            r = requests.get(f"{BASE_URL}/api/backtest/status/{jid}", timeout=15)
            assert r.status_code == 200
            st = r.json().get("status")
            if st in ("done", "error", "canceled"):
                assert st == "done", r.json()
                return
            time.sleep(3)
        pytest.fail("Backtest timeout")

    def test_backtest_equity(self):
        r = requests.get(f"{BASE_URL}/api/backtest/equity/{TestBacktesterRegression.job_id}",
                         timeout=20)
        assert r.status_code == 200, r.text
        j = r.json()
        assert "points" in j or isinstance(j, list)

    def test_backtest_export_trades(self):
        r = requests.get(f"{BASE_URL}/api/backtest/export/{TestBacktesterRegression.job_id}",
                         params={"kind": "trades"}, timeout=20)
        assert r.status_code == 200, r.text
        assert "symbol" in r.text.split("\n")[0].lower()


# ------------------------------------------------------------------
# 4) Dynamic optimizer + save/list/refresh/apply/delete
# ------------------------------------------------------------------
class TestDynamicOptimizer:
    job_id = None
    result = None
    dyn_id = None

    def test_start_dynamic(self, admin_headers):
        body = {
            "mode": "dynamic",
            "strategy_id": "bollinger_reversion",
            "symbols": ["BTCUSDT"],
            "days": 14,
            "timeframe": "5m",
            "iterations": 4,
            "min_trades": 6,
            "optimize": {"tpsl": True},
            "dynamic": {"max_regimes": 4, "lookback_days": 2,
                        "confidence_min": 70, "min_hold_days": 1},
            "walk_forward": {"enabled": True, "train_pct": 75, "mode": "single"},
        }
        r = _start_when_free(body, admin_headers)
        assert r.status_code == 200, r.text
        TestDynamicOptimizer.job_id = r.json()["job_id"]

    def test_poll_dynamic(self):
        job = _poll(TestDynamicOptimizer.job_id, timeout=420, interval=5)
        assert job["status"] == "done", f"dyn job err: {job.get('error')}"
        TestDynamicOptimizer.result = job.get("result") or {}

    def test_dynamic_shape(self):
        res = TestDynamicOptimizer.result or {}
        dyn = res.get("dynamic")
        assert dyn, f"dynamic key missing in result keys={list(res)}"
        model = dyn.get("model") or {}
        regimes_m = model.get("regimes") or []
        assert len(regimes_m) >= 2, f"regimes<2: {regimes_m}"
        for rg in regimes_m:
            assert "label" in rg and "share_pct" in rg

        per_regime = dyn.get("regimes") or []
        assert per_regime, "dynamic.regimes empty"
        for rg in per_regime:
            for k in ("config", "metrics", "baseline_metrics"):
                assert k in rg, f"regime missing {k}: keys={list(rg)}"
            assert "insufficient" in rg

        comp = dyn.get("comparison") or {}
        assert (comp.get("dynamic") or {}).get("test") is not None, f"comparison.dynamic.test missing: {comp}"
        assert (comp.get("static") or {}).get("test") is not None, f"comparison.static.test missing: {comp}"

        verdict = dyn.get("verdict") or {}
        assert isinstance(verdict.get("recommendation"), str)
        assert isinstance(verdict.get("dynamic_better"), bool)

    def test_dynamic_save(self, admin_headers):
        res = TestDynamicOptimizer.result or {}
        dyn = res.get("dynamic") or {}
        payload = {
            "name": "TEST_dyn_bollinger",
            "strategy_id": "bollinger_reversion",
            "symbols": ["BTCUSDT"],
            "timeframe": "5m",
            "model": dyn.get("model"),
            "configs": dyn.get("configs") or dyn.get("regimes"),
            "fallback_config": (dyn.get("static_benchmark") or {}).get("config"),
            "settings": dyn.get("settings"),
            "verdict": dyn.get("verdict"),
        }
        r = requests.post(f"{BASE_URL}/api/dynamic/save", json=payload,
                          headers=admin_headers, timeout=15)
        assert r.status_code == 200, r.text
        TestDynamicOptimizer.dyn_id = r.json().get("id")
        assert TestDynamicOptimizer.dyn_id

    def test_dynamic_list(self):
        r = requests.get(f"{BASE_URL}/api/dynamic/list", timeout=15)
        assert r.status_code == 200, r.text
        j = r.json()
        items = j.get("strategies") or j.get("items") or (j if isinstance(j, list) else [])
        ids = [it.get("id") for it in items]
        assert TestDynamicOptimizer.dyn_id in ids, f"saved id not in list: {ids}"

    def test_dynamic_refresh(self, admin_headers):
        did = TestDynamicOptimizer.dyn_id
        r = requests.post(f"{BASE_URL}/api/dynamic/{did}/refresh",
                          json={"days": 7}, headers=admin_headers, timeout=180)
        assert r.status_code == 200, r.text
        j = r.json()
        ps = j.get("per_symbol") or {}
        btc = ps.get("BTCUSDT")
        assert btc, f"no BTCUSDT in per_symbol: {ps}"
        for k in ("regime", "label", "confidence", "similarities",
                  "active_config", "recent_performance"):
            assert k in btc, f"refresh missing {k}: keys={list(btc)}"

    def test_dynamic_apply(self, admin_headers):
        did = TestDynamicOptimizer.dyn_id
        r = requests.post(f"{BASE_URL}/api/dynamic/{did}/apply",
                          headers=admin_headers, timeout=30)
        assert r.status_code == 200, r.text
        j = r.json()
        assert j.get("status") == "success" or j.get("success") is True, j

    def test_dynamic_delete(self, admin_headers):
        did = TestDynamicOptimizer.dyn_id
        r = requests.delete(f"{BASE_URL}/api/dynamic/{did}",
                            headers=admin_headers, timeout=15)
        assert r.status_code in (200, 204), r.text
