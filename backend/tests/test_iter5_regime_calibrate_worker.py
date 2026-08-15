"""Iteration 5 tests: Regime-Lab calibrate (centered/hmm) + Local Worker robustness.

Covers:
- Admin auth
- POST /api/regime-lab/calibrate (centered, hmm, invalid truth_source)
- POST /api/regime-lab/analyze (cloud + local worker)
- Worker stays online during large-candle jobs (5m/300d)
- Cancel (<=5s), disconnect detection (<=10s), reconnect
- GET /api/localworker/package/manifest
"""
import os
import time
import subprocess
import requests
import pytest

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "http://localhost:8001").rstrip("/")
API = f"{BASE_URL}/api"

ADMIN_USER = "Admin"
ADMIN_PASS = os.environ.get("ADMIN_PASSWORD", "admin")
WORKER_TOKEN = "4bb3e0448acefa98874d6cdcb6472b104e12e1563d3f3d6b"


# ---------- Fixtures ----------
@pytest.fixture(scope="module")
def token():
    r = requests.post(f"{API}/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PASS}, timeout=15)
    assert r.status_code == 200, r.text
    return r.json()["token"]


@pytest.fixture(scope="module")
def headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ---------- Helpers ----------
def _poll_job(headers, job_id, timeout=240, interval=3):
    """Poll regime-lab job status until done/error/cancelled."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        r = requests.get(f"{API}/regime-lab/status/{job_id}", headers=headers, timeout=15)
        assert r.status_code == 200, r.text
        last = r.json()
        st = last.get("status")
        if st in ("done", "error", "cancelled", "failed"):
            return last
        time.sleep(interval)
    return last


def _worker_status(headers):
    r = requests.get(f"{API}/localworker/status", headers=headers, timeout=15)
    assert r.status_code == 200
    return r.json()


# ---------- Tests ----------
class TestAuth:
    def test_login(self, token):
        assert isinstance(token, str) and len(token) > 20


class TestRegimeCalibrate:
    def test_calibrate_centered(self, headers):
        body = {
            "symbols": ["BTCUSDT"],
            "timeframe": "1h",
            "days": 120,
            "truth_source": "centered",
            "engine_config": {"regime_mode": 3},
        }
        r = requests.post(f"{API}/regime-lab/calibrate", headers=headers, json=body, timeout=30)
        assert r.status_code == 200, r.text
        job_id = r.json().get("job_id") or r.json().get("id")
        assert job_id, r.text
        result = _poll_job(headers, job_id, timeout=240)
        assert result["status"] == "done", f"Job did not finish: {result}"
        report = (result.get("result") or {}).get("report") or {}
        assert "baseline" in report and "best" in report and "best_config" in report, report
        bc = report["best_config"]
        assert "trend_t" in bc
        assert bc.get("min_hold_days", 0) <= 30
        assert bc.get("auto_adapt") is False
        assert report["best"]["balanced_direction_pct"] >= report["baseline"]["balanced_direction_pct"] - 1e-6

    def test_calibrate_hmm(self, headers):
        body = {
            "symbols": ["BTCUSDT"],
            "timeframe": "1h",
            "days": 120,
            "truth_source": "hmm",
            "engine_config": {"regime_mode": 3},
        }
        r = requests.post(f"{API}/regime-lab/calibrate", headers=headers, json=body, timeout=30)
        assert r.status_code == 200, r.text
        job_id = r.json().get("job_id") or r.json().get("id")
        result = _poll_job(headers, job_id, timeout=300)
        assert result["status"] == "done", f"HMM job failed: {result}"

    def test_calibrate_invalid_truth_source(self, headers):
        body = {"symbols": ["BTCUSDT"], "timeframe": "1h", "days": 120, "truth_source": "quatsch"}
        r = requests.post(f"{API}/regime-lab/calibrate", headers=headers, json=body, timeout=30)
        assert r.status_code == 400, r.text


class TestRegimeAnalyzeCloud:
    def test_analyze_cloud_ideal_segments(self, headers):
        body = {
            "symbols": ["BTCUSDT"],
            "timeframe": "1h",
            "days": 120,
            "scope": "combined",
            "engine": "v2",
        }
        r = requests.post(f"{API}/regime-lab/analyze", headers=headers, json=body, timeout=30)
        assert r.status_code == 200, r.text
        job_id = r.json().get("job_id") or r.json().get("id")
        result = _poll_job(headers, job_id, timeout=240)
        assert result["status"] == "done", result
        analysis_id = (result.get("result") or {}).get("analysis_id") or result.get("analysis_id")
        if not analysis_id:
            # try nested
            analysis_id = ((result.get("result") or {}).get("id"))
        assert analysis_id, f"missing analysis_id: {result}"
        r2 = requests.get(f"{API}/regime-lab/{analysis_id}", headers=headers, timeout=30)
        assert r2.status_code == 200, r2.text
        data = r2.json()
        # Response is wrapped under 'analysis'
        data = data.get("analysis", data)
        combined = data.get("combined") or {}
        per = (combined.get("per_symbol") or {}).get("BTCUSDT") or {}
        ideal = per.get("ideal") or {}
        segments = ideal.get("segments") or []
        assert len(segments) > 0, f"ideal.segments empty: {per}"
        assert "validation" in data or "validation" in combined


class TestLocalWorker:
    def test_worker_status_online(self, headers):
        st = _worker_status(headers)
        assert st.get("online") is True, st
        workers = st.get("workers") or []
        assert len(workers) >= 1
        w = workers[0]
        st2 = requests.get(f"{API}/localworker/status", headers=headers, timeout=15).json()
        assert w.get("version") == st2.get("required_version"), w
        assert w.get("outdated") is False

    def test_manifest(self, headers):
        r = requests.get(f"{API}/localworker/package/manifest", headers=headers, timeout=15)
        assert r.status_code == 200, r.text
        m = r.json()
        st = requests.get(f"{API}/localworker/status", timeout=15).json()
        assert m.get("required_version") == st.get("required_version"), m
        assert m.get("complete") is True, m

    def test_local_analyze_5m_300d_worker_stays_online(self, headers):
        """Core bug: worker decoupled during many-candle jobs."""
        body = {
            "symbols": ["BTCUSDT"],
            "timeframe": "5m",
            "days": 300,
            "scope": "combined",
            "execution": "local",
            "engine": "v2",
        }
        r = requests.post(f"{API}/regime-lab/analyze", headers=headers, json=body, timeout=30)
        assert r.status_code == 200, r.text
        job_id = r.json().get("job_id") or r.json().get("id")
        assert job_id
        # Poll job while checking worker stays online every ~5s
        deadline = time.time() + 360
        final = None
        offline_count = 0
        while time.time() < deadline:
            js = requests.get(f"{API}/regime-lab/status/{job_id}", headers=headers, timeout=15).json()
            ws = _worker_status(headers)
            if not ws.get("online"):
                offline_count += 1
            if js.get("status") in ("done", "error", "cancelled", "failed"):
                final = js
                break
            time.sleep(5)
        assert final is not None, "job did not finish in time"
        assert final["status"] == "done", f"local analyze failed: {final}"
        assert offline_count == 0, f"Worker went offline {offline_count} times during job"

    def test_cancel_within_5s(self, headers):
        body = {
            "symbols": ["BTCUSDT"],
            "timeframe": "5m",
            "days": 300,
            "scope": "combined",
            "execution": "local",
            "engine": "v2",
        }
        r = requests.post(f"{API}/regime-lab/analyze", headers=headers, json=body, timeout=30)
        assert r.status_code == 200, r.text
        job_id = r.json().get("job_id") or r.json().get("id")
        time.sleep(2)
        t0 = time.time()
        rc = requests.post(f"{API}/regime-lab/cancel/{job_id}", headers=headers, timeout=15)
        assert rc.status_code in (200, 202), rc.text
        # Wait up to 5s for status to flip to cancelled
        final = None
        while time.time() - t0 < 8:
            js = requests.get(f"{API}/regime-lab/status/{job_id}", headers=headers, timeout=10).json()
            if js.get("status") in ("cancelled", "done", "error", "failed"):
                final = js
                break
            time.sleep(0.5)
        elapsed = time.time() - t0
        assert final is not None, "no final status after cancel"
        assert final["status"] == "cancelled", f"expected cancelled, got {final}"
        assert elapsed <= 6, f"cancel took {elapsed:.1f}s (>5s target)"

    def test_disconnect_detection_and_reconnect(self, headers):
        # Kill worker
        subprocess.run(["pkill", "-f", "worker.py --"], check=False)
        t0 = time.time()
        went_offline = False
        while time.time() - t0 < 12:
            ws = _worker_status(headers)
            if not ws.get("online"):
                went_offline = True
                break
            time.sleep(1)
        offline_after = time.time() - t0
        assert went_offline, f"Worker still online after {offline_after:.1f}s"
        assert offline_after <= 10, f"Detection took {offline_after:.1f}s (>10s)"

        # Restart worker
        wtok = requests.get(f"{API}/localworker/token", headers=headers,
                            timeout=10).json()["token"]
        env = os.environ.copy()
        env["WTOK"] = wtok
        subprocess.Popen(
            ["bash", "-c",
             "cd /root/worker_test && nohup python worker.py --server http://localhost:8001 "
             f"--token {wtok} --name TestPC > out.log 2>&1 &"],
            env=env,
        )
        # Wait for reconnect
        t1 = time.time()
        back_online = False
        while time.time() - t1 < 45:
            ws = _worker_status(headers)
            if ws.get("online"):
                back_online = True
                break
            time.sleep(2)
        assert back_online, "Worker did not come back online after restart"
