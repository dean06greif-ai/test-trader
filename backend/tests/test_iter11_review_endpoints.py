"""Regressionstests für Review-Iteration 11 - konkrete API-Punkte aus dem Review:

- GET /api/localworker/package/manifest -> complete=true, required_version=1.9.0
- GET /api/localworker/package -> ZIP mit worker.py, requirements.txt, README.md,
  start_worker.bat und services/deep_explore.py
- POST /api/optimizer/run mode=explore Kurz-Lauf mit Zeitlimit 1 min
- POST /api/optimizer/explore/stop/{id} sanfter Stop
- GET /api/optimizer/explore/best öffentlich
- Ungültiger Mode wird mit 400 abgelehnt und enthält 'explore'
- Discovery-Regression (mode=discovery)
- Backtest-Regression POST /api/backtest/run rsi_only
"""
import io
import os
import time
import zipfile

import requests

BASE = "http://localhost:8001"
ADMIN_USER = os.environ.get("ADMIN_USER", "Admin")
ADMIN_PASS = os.environ.get("ADMIN_PASSWORD", "admin")


def _login():
    r = requests.post(f"{BASE}/api/auth/login",
                      json={"username": ADMIN_USER, "password": ADMIN_PASS},
                      timeout=10)
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _wait_for_optimizer(job_id, timeout=180):
    for _ in range(timeout):
        j = requests.get(f"{BASE}/api/optimizer/status/{job_id}", timeout=10).json()
        if j.get("status") != "running":
            return j
        time.sleep(2)
    return j


def _wait_no_running_optimizer(hdr, timeout=180):
    """Warten bis kein Optimizer-Lauf mehr aktiv ist (kein Kill von fremden Jobs)."""
    for _ in range(timeout):
        r = requests.get(f"{BASE}/api/optimizer/active", timeout=10).json()
        if not r.get("active"):
            return True
        time.sleep(2)
    return False


# ============= Worker-Paket =============
class TestWorkerPackage:
    def test_manifest_complete_and_version(self):
        r = requests.get(f"{BASE}/api/localworker/package/manifest", timeout=10)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["complete"] is True
        assert d["missing"] == []
        assert d["required_version"] == "1.9.0"
        for f in ("worker.py", "requirements.txt", "README.md"):
            assert f in d["worker_files"]

    def test_zip_contains_all_required(self):
        r = requests.get(f"{BASE}/api/localworker/package", timeout=60)
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/zip"
        z = zipfile.ZipFile(io.BytesIO(r.content))
        names = set(z.namelist())
        for name in ("worker.py", "requirements.txt", "README.md",
                     "start_worker.bat", "services/deep_explore.py"):
            assert name in names, f"{name} fehlt im ZIP"


# ============= Optimizer explore =============
class TestExploreEndpoints:
    def test_invalid_mode_returns_400_with_explore(self):
        hdr = _login()
        r = requests.post(f"{BASE}/api/optimizer/run", headers=hdr,
                          json={"mode": "bogus", "symbols": ["BTCUSDT"]},
                          timeout=10)
        assert r.status_code == 400
        assert "explore" in r.json()["detail"]

    def test_explore_best_endpoint_public(self):
        """GET /api/optimizer/explore/best ist öffentlich und liefert items[]."""
        r = requests.get(f"{BASE}/api/optimizer/explore/best", timeout=10)
        assert r.status_code == 200
        d = r.json()
        assert "items" in d
        assert isinstance(d["items"], list)

    def test_explore_stop_requires_admin(self):
        r = requests.post(f"{BASE}/api/optimizer/explore/stop/does-not-matter",
                          timeout=10)
        assert r.status_code == 401

    def test_explore_run_short_completes(self):
        hdr = _login()
        _wait_no_running_optimizer(hdr)
        body = {"mode": "explore", "symbols": ["BTCUSDT"], "days": 2,
                "timeframe": "5m", "iterations": 5, "min_trades": 3,
                "max_rules": 3,
                "indicators": ["rsi", "ema_fast", "ema_slow", "macd_hist",
                               "price_change_pct", "range_pos"],
                "explore": {"target_champions": 1, "max_minutes": 1}}
        r = requests.post(f"{BASE}/api/optimizer/run", headers=hdr, json=body,
                          timeout=15)
        if r.status_code == 409:
            # anderer Lauf blockt -> ok
            return
        assert r.status_code == 200, r.text
        job_id = r.json()["job_id"]
        st = _wait_for_optimizer(job_id, timeout=180)
        assert st.get("status") == "done", st
        rep = (st.get("result") or {}).get("explore_report")
        assert rep, "explore_report fehlt"
        assert rep.get("tested", 0) > 0
        assert rep.get("stop_reason")
        assert "top5" in (st.get("result") or {})

    def test_explore_soft_stop_keeps_best(self):
        hdr = _login()
        _wait_no_running_optimizer(hdr)
        body = {"mode": "explore", "symbols": ["BTCUSDT"], "days": 2,
                "timeframe": "5m", "iterations": 5, "min_trades": 3,
                "max_rules": 3,
                "indicators": ["rsi", "ema_fast", "ema_slow", "macd_hist",
                               "price_change_pct", "range_pos", "atr_pct"],
                "explore": {"target_champions": 10, "max_minutes": 0}}
        r = requests.post(f"{BASE}/api/optimizer/run", headers=hdr, json=body,
                          timeout=15)
        if r.status_code == 409:
            return
        assert r.status_code == 200
        job_id = r.json()["job_id"]
        time.sleep(10)
        s = requests.post(f"{BASE}/api/optimizer/explore/stop/{job_id}",
                          headers=hdr, timeout=10)
        assert s.status_code in (200, 409)
        st = _wait_for_optimizer(job_id, timeout=120)
        assert st.get("status") == "done", st
        rep = (st.get("result") or {}).get("explore_report")
        assert rep is not None
        # nur wenn tatsächlich vom Nutzer gestoppt:
        if s.status_code == 200:
            assert rep.get("stop_reason") == "stopped_by_user", rep


# ============= Discovery Regression =============
class TestDiscoveryRegression:
    def test_discovery_still_runs(self):
        hdr = _login()
        _wait_no_running_optimizer(hdr)
        body = {"mode": "discovery", "symbols": ["BTCUSDT"], "days": 2,
                "timeframe": "15m", "iterations": 3, "min_trades": 3,
                "max_rules": 3,
                "indicators": ["rsi", "ema_slow", "macd_hist"]}
        r = requests.post(f"{BASE}/api/optimizer/run", headers=hdr, json=body,
                          timeout=15)
        if r.status_code == 409:
            return
        assert r.status_code == 200, r.text
        job_id = r.json()["job_id"]
        st = _wait_for_optimizer(job_id, timeout=180)
        assert st.get("status") == "done", st
        res = st.get("result") or {}
        assert res.get("definition"), "definition muss im Discovery-Ergebnis vorhanden sein"


# ============= Backtest Regression =============
class TestBacktestRegression:
    def test_backtest_rsi_only_completes(self):
        hdr = _login()
        body = {"strategy_ids": ["rsi_only"], "symbols": ["BTCUSDT"], "days": 1}
        r = requests.post(f"{BASE}/api/backtest/run", headers=hdr, json=body,
                          timeout=15)
        if r.status_code == 409:
            return
        assert r.status_code == 200, r.text
        job_id = r.json()["job_id"]
        for _ in range(120):
            s = requests.get(f"{BASE}/api/backtest/status/{job_id}",
                             timeout=10).json()
            if s.get("status") != "running":
                break
            time.sleep(2)
        assert s.get("status") == "done", s
