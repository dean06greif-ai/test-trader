"""
Iter9 E2E: Multi-core / Cloud regression via public API.
Real local worker must be running in the pod for LOCAL scenarios.
"""
import os, time, json, requests, pytest

BASE = os.environ.get("REACT_APP_BACKEND_URL", "http://localhost:8001").rstrip("/")
API = f"{BASE}/api"


@pytest.fixture(scope="module")
def admin_token():
    r = requests.post(f"{API}/auth/login", json={"username": os.environ.get("ADMIN_USER", "Admin"), "password": os.environ.get("ADMIN_PASSWORD", "admin")})
    assert r.status_code == 200, r.text
    return r.json()["token"]


@pytest.fixture(scope="module")
def h(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


def _list_strategy_ids():
    d = requests.get(f"{API}/strategies").json()
    if isinstance(d, dict) and "strategies" in d:
        d = d["strategies"]
    return [s["id"] for s in d]


def _wait_backtest(job_id, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = requests.get(f"{API}/backtest/status/{job_id}")
        if r.status_code == 200:
            d = r.json()
            if d.get("status") in ("done", "error", "cancelled"):
                return d
        time.sleep(1)
    return {"status": "timeout"}


def _wait_optimizer(job_id, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = requests.get(f"{API}/optimizer/status/{job_id}")
        if r.status_code == 200:
            d = r.json()
            if d.get("status") in ("done", "error", "cancelled"):
                return d
        time.sleep(1)
    return {"status": "timeout"}


def _wait_no_active(kind, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = requests.get(f"{API}/{kind}/active")
        if r.status_code == 200 and not r.json().get("active"):
            return True
        time.sleep(1)
    return False


# ---------- REGRESSION Cloud Backtest ----------
def test_cloud_backtest_regression(h):
    assert _wait_no_active("backtest")
    _stids = _list_strategy_ids()
    ids = _stids[:2]
    assert len(ids) == 2
    payload = {
        "strategy_ids": ids,
        "symbols": ["BTCUSDT"],
        "days": 2,
        # no execution -> cloud
    }
    r = requests.post(f"{API}/backtest/run", headers=h, json=payload)
    assert r.status_code == 200, r.text
    job_id = r.json().get("job_id") or r.json().get("id")
    res = _wait_backtest(job_id, timeout=180)
    assert res["status"] == "done", res
    per = res["result"]["per_strategy"]
    assert isinstance(per, list) and len(per) >= 1
    # equity
    eq = requests.get(f"{API}/backtest/equity/{job_id}")
    assert eq.status_code == 200
    ej = eq.json()
    assert "points" in ej or "series" in ej or "equity" in ej
    # export CSV
    ex = requests.get(f"{API}/backtest/export/{job_id}", params={"kind": "trades"})
    assert ex.status_code == 200
    assert "text/csv" in ex.headers.get("content-type", "") or ex.text.startswith(("id,", "trade", "strategy", "coin", "ts"))


# ---------- REGRESSION Cloud Optimizer params ----------
def test_cloud_optimizer_params_regression(h):
    assert _wait_no_active("optimizer")
    payload = {
        "strategy_id": "rsi_only",
        "symbols": ["BTCUSDT"],
        "days": 1,
        "iterations": 6,
        "mode": "params",
    }
    r = requests.post(f"{API}/optimizer/run", headers=h, json=payload)
    assert r.status_code == 200, r.text
    job_id = r.json().get("job_id") or r.json().get("id")
    res = _wait_optimizer(job_id, timeout=180)
    assert res["status"] == "done", res
    result = res["result"]
    assert "best" in result and "top" in result
    baseline = result.get("baseline") or {}
    metrics = baseline.get("metrics") or {}
    assert metrics.get("trades", 0) > 0, baseline


# ---------- REGRESSION Cloud Optimizer discovery ----------
def test_cloud_optimizer_discovery_regression(h):
    assert _wait_no_active("optimizer")
    payload = {
        "mode": "discovery",
        "indicators": ["rsi", "ema"],
        "max_rules": 2,
        "symbols": ["BTCUSDT"],
        "days": 1,
    }
    r = requests.post(f"{API}/optimizer/run", headers=h, json=payload)
    assert r.status_code == 200, r.text
    job_id = r.json().get("job_id") or r.json().get("id")
    res = _wait_optimizer(job_id, timeout=240)
    assert res["status"] == "done", res
    result = res["result"]
    assert "rules" in result
    assert "steps" in result


# ---------- Worker status ----------
def test_worker_status():
    r = requests.get(f"{API}/localworker/status")
    assert r.status_code == 200
    d = r.json()
    workers = d.get("workers", [])
    assert len(workers) >= 1
    w = workers[0]
    # sim_workers kommt vom Worker als int oder numerischer String ("0" = alle Kerne)
    sw = w.get("sim_workers")
    assert sw is not None and int(sw) >= 0, w
    # Version dynamisch gegen das ausgelieferte Paket pruefen (kein Hardcoding)
    import re
    pkg_ver = re.search(r'VERSION = "([\d.]+)"',
                        open("/app/local_worker/worker.py").read()).group(1)
    assert w.get("version") == pkg_ver, w


# ---------- LOCAL Multi-Core Backtest ----------
def test_local_multicore_backtest(h):
    assert _wait_no_active("backtest")
    _stids = _list_strategy_ids()
    ids = _stids[:3]
    payload = {
        "strategy_ids": ids,
        "symbols": ["BTCUSDT", "ETHUSDT"],
        "date_from": "2026-07-20",
        "date_to": "2026-07-22",
        "execution": "local",
    }
    r = requests.post(f"{API}/backtest/run", headers=h, json=payload)
    assert r.status_code == 200, r.text
    job_id = r.json().get("job_id") or r.json().get("id")
    res = _wait_backtest(job_id, timeout=240)
    assert res["status"] == "done", res
    per = res["result"]["per_strategy"]
    assert isinstance(per, list) and len(per) >= 1

    # Check worker log for pool line
    try:
        with open("/root/worker.log", "r") as f:
            log = f.read()
        assert "parallel_sim: Pool mit" in log, "pool line missing in worker.log"
    except FileNotFoundError:
        pytest.skip("worker.log not found in this env")


# ---------- LOCAL Determinism (cpu_cores=1 vs 0) ----------
def _run_local_bt(h, strategy_ids):
    payload = {
        "strategy_ids": strategy_ids,
        "symbols": ["BTCUSDT", "ETHUSDT"],
        "date_from": "2026-07-20",
        "date_to": "2026-07-22",
        "execution": "local",
    }
    r = requests.post(f"{API}/backtest/run", headers=h, json=payload)
    assert r.status_code == 200, r.text
    return _wait_backtest(r.json().get("job_id") or r.json().get("id"), timeout=240)


def _set_cores(h, n):
    # POST /api/localworker/settings
    r = requests.post(f"{API}/localworker/settings", headers=h, json={"cpu_cores": n})
    assert r.status_code == 200, r.text
    time.sleep(6)


def _extract_metrics(res):
    per = res["result"]["per_strategy"]
    out = {}
    for row in per:
        sid = row.get("strategy_id") or row.get("id") or row.get("strategy")
        m = row.get("metrics") or row
        out[sid] = (m.get("trades"), m.get("pnl"), m.get("win_rate"))
    return out


def test_local_determinism_cores_1_vs_0(h):
    assert _wait_no_active("backtest")
    _stids = _list_strategy_ids()
    ids = _stids[:2]

    try:
        _set_cores(h, 1)
        assert _wait_no_active("backtest")
        r1 = _run_local_bt(h, ids)
        assert r1["status"] == "done", r1

        _set_cores(h, 0)
        assert _wait_no_active("backtest")
        r2 = _run_local_bt(h, ids)
        assert r2["status"] == "done", r2

        m1 = _extract_metrics(r1)
        m2 = _extract_metrics(r2)
        assert m1 == m2, f"determinism mismatch:\n{m1}\nvs\n{m2}"
    finally:
        # reset
        try:
            _set_cores(h, 0)
        except Exception:
            pass


# ---------- LOCAL Multi-Core Optimizer ----------
def test_local_multicore_optimizer(h):
    assert _wait_no_active("optimizer")
    payload = {
        "strategy_id": "rsi_only",
        "symbols": ["BTCUSDT", "ETHUSDT"],
        "days": 7,
        "iterations": 20,
        "mode": "params",
        "execution": "local",
    }
    r = requests.post(f"{API}/optimizer/run", headers=h, json=payload)
    assert r.status_code == 200, r.text
    job_id = r.json().get("job_id") or r.json().get("id")
    res = _wait_optimizer(job_id, timeout=300)
    assert res["status"] == "done", res
    assert "best" in res["result"] and "top" in res["result"]


# ---------- Cancel Multi-Core ----------
def test_local_optimizer_cancel_then_new_job(h):
    assert _wait_no_active("optimizer")
    payload = {
        "mode": "discovery",
        "indicators": ["rsi", "ema", "macd", "bollinger"],
        "max_rules": 3,
        "symbols": ["BTCUSDT", "ETHUSDT"],
        "days": 90,
        "timeframe": "1m",
        "execution": "local",
    }
    r = requests.post(f"{API}/optimizer/run", headers=h, json=payload)
    assert r.status_code == 200, r.text
    job_id = r.json().get("job_id") or r.json().get("id")
    time.sleep(3)
    c = requests.post(f"{API}/optimizer/cancel/{job_id}", headers=h)
    assert c.status_code in (200, 202), c.text
    # wait for cancelled
    deadline = time.time() + 45
    final = None
    while time.time() < deadline:
        s = requests.get(f"{API}/optimizer/status/{job_id}").json()
        if s.get("status") in ("cancelled", "done", "error"):
            final = s
            break
        time.sleep(1)
    assert final and final["status"] == "cancelled", final

    # Worker should still accept new job
    assert _wait_no_active("backtest")
    _stids = _list_strategy_ids()
    ids = [_stids[0]]
    payload2 = {
        "strategy_ids": ids,
        "symbols": ["BTCUSDT"],
        "date_from": "2026-07-21",
        "date_to": "2026-07-22",
        "execution": "local",
    }
    r2 = requests.post(f"{API}/backtest/run", headers=h, json=payload2)
    assert r2.status_code == 200, r2.text
    res2 = _wait_backtest(r2.json().get("job_id") or r2.json().get("id"), timeout=120)
    assert res2["status"] == "done", res2
