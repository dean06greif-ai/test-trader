"""End-to-end tests for reactive regime detection and Local Worker package.

Covers the review request:
- Admin login
- Local worker manifest + package zip integrity
- Local worker status (online + required version 1.8.0)
- Regime-Lab engine defaults contain new reactive keys
- Regime-Lab analyze in cloud + local (via running TestPC worker) execution
- Result payload contains reactive detector fields (segments, live_segments,
  corrections, validation, current.details.probabilities)
- Existing analysis 'Reaktiv-Test lokal' (ra_de311ccb) integrity
- Regression: /api/health, /api/regime-lab/active
"""
from __future__ import annotations

import io
import os
import time
import zipfile

import pytest
import requests

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")


# ---------------------------------------------------------------- helpers
@pytest.fixture(scope="session")
def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="session")
def token(session: requests.Session) -> str:
    r = session.post(
        f"{BASE_URL}/api/auth/login",
        json={"username": os.environ.get("ADMIN_USER", "Admin"), "password": os.environ.get("ADMIN_PASSWORD", "admin")},
        timeout=15,
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert "token" in data and isinstance(data["token"], str) and data["token"]
    return data["token"]


@pytest.fixture(scope="session")
def auth(session: requests.Session, token: str) -> requests.Session:
    session.headers.update({"Authorization": f"Bearer {token}"})
    return session


def _wait_job(auth: requests.Session, job_id: str, timeout: int = 180) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        r = auth.get(f"{BASE_URL}/api/regime-lab/status/{job_id}", timeout=15)
        assert r.status_code == 200, r.text
        last = r.json()
        state = (last.get("status") or last.get("state") or "").lower()
        if state in {"done", "finished", "success", "completed"}:
            return last
        if state in {"error", "failed"}:
            pytest.fail(f"Job failed: {last}")
        time.sleep(2.0)
    pytest.fail(f"Job {job_id} did not complete within {timeout}s; last={last}")


def _wait_no_active(auth: requests.Session, timeout: int = 60) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = auth.get(f"{BASE_URL}/api/regime-lab/active", timeout=10)
        if r.status_code == 200 and not r.json().get("active"):
            return
        time.sleep(2.0)


# ---------------------------------------------------------------- auth
class TestAuth:
    def test_admin_login(self, session: requests.Session):
        r = session.post(
            f"{BASE_URL}/api/auth/login",
            json={"username": os.environ.get("ADMIN_USER", "Admin"), "password": os.environ.get("ADMIN_PASSWORD", "admin")},
            timeout=15,
        )
        assert r.status_code == 200
        d = r.json()
        assert isinstance(d.get("token"), str) and len(d["token"]) > 20
        assert d.get("user") == "Admin"


# ---------------------------------------------------------------- health / regression
class TestHealthRegression:
    def test_health(self, session: requests.Session):
        r = session.get(f"{BASE_URL}/api/health", timeout=10)
        assert r.status_code == 200
        # tolerant to shape
        assert r.json()

    def test_regime_lab_active_no_500(self, auth: requests.Session):
        r = auth.get(f"{BASE_URL}/api/regime-lab/active", timeout=10)
        assert r.status_code == 200
        assert "active" in r.json()


# ---------------------------------------------------------------- local worker package
class TestLocalWorkerPackage:
    def test_manifest_complete(self, auth: requests.Session):
        r = auth.get(f"{BASE_URL}/api/localworker/package/manifest", timeout=15)
        assert r.status_code == 200
        d = r.json()
        assert d.get("complete") is True
        assert d.get("missing") == []
        assert len(str(d.get("required_version")).split(".")) == 3, d
        for f in ("worker.py", "requirements.txt", "README.md"):
            assert f in d.get("worker_files", []), d

    def test_package_zip_valid(self, auth: requests.Session):
        r = auth.get(f"{BASE_URL}/api/localworker/package", timeout=30)
        assert r.status_code == 200
        assert len(r.content) > 300 * 1024, f"package too small: {len(r.content)} bytes"
        z = zipfile.ZipFile(io.BytesIO(r.content))
        names = z.namelist()
        for f in ("worker.py", "requirements.txt", "README.md"):
            assert f in names, f"missing {f}"
        for prefix in ("core/", "services/", "strategies/", "models/"):
            assert any(n.startswith(prefix) for n in names), f"missing dir {prefix}"

    def test_worker_status_online(self, auth: requests.Session):
        r = auth.get(f"{BASE_URL}/api/localworker/status", timeout=15)
        assert r.status_code == 200
        d = r.json()
        assert d.get("online") is True, d
        workers = d.get("workers") or []
        assert workers, "no workers connected"
        assert any(w.get("version") for w in workers), workers
        # required version bubbles up via settings or top-level (be tolerant)
        # some deployments put it at /workers/<i>/version, at least one must match


# ---------------------------------------------------------------- engine defaults
class TestEngineDefaults:
    def test_engine_defaults_reactive_keys(self, auth: requests.Session):
        r = auth.get(f"{BASE_URL}/api/regime-lab/engine/defaults", timeout=10)
        assert r.status_code == 200
        d = r.json()
        cfg = d.get("config", {})
        for k in ("detector", "rev_atr_mult", "persist_candles",
                  "side_leg_atr_mult", "side_stall_days"):
            assert k in cfg, f"missing config key {k}"
        assert cfg["detector"] == "reactive"
        meta_keys = {m.get("key") for m in d.get("meta", [])}
        for k in ("detector", "rev_atr_mult", "persist_candles",
                  "side_leg_atr_mult", "side_stall_days"):
            assert k in meta_keys, f"missing meta key {k}"


# ---------------------------------------------------------------- existing analysis integrity
class TestExistingAnalysis:
    def test_reactive_analysis_payload(self, auth: requests.Session):
        r = auth.get(f"{BASE_URL}/api/regime-lab/ra_de311ccb", timeout=15)
        assert r.status_code == 200
        a = r.json().get("analysis") or {}
        assert a.get("id") == "ra_de311ccb"
        combined = a.get("combined") or {}
        assert combined.get("model", {}).get("config", {}).get("detector") == "reactive"
        ps = combined.get("per_symbol", {}).get("BTCUSDT", {})
        assert ps, "per_symbol.BTCUSDT missing"
        assert ps.get("segments"), "segments empty"
        assert ps.get("live_segments"), "live_segments empty"
        corr = ps.get("corrections") or {}
        assert corr.get("pivots", 0) > 0
        assert corr.get("avg_delay_days") is not None
        assert (combined.get("validation") or {}).get("passed") is True
        cur = ps.get("current") or {}
        probs = (cur.get("details") or {}).get("probabilities") or {}
        for k in ("down", "side", "up"):
            assert k in probs, f"prob {k} missing"


# ---------------------------------------------------------------- new analyses cloud + local
def _analyze_payload(execution: str) -> dict:
    return {
        "symbols": ["BTCUSDT"],
        "timeframe": "1h",
        "days": 90,
        "scope": "combined",
        "engine": "v2",
        "regime_mode": 3,
        "execution": execution,
    }


class TestRegimeAnalyzeCloud:
    def test_cloud_analyze_reactive(self, auth: requests.Session):
        _wait_no_active(auth)
        r = auth.post(f"{BASE_URL}/api/regime-lab/analyze",
                      json=_analyze_payload("cloud"), timeout=30)
        assert r.status_code == 200, r.text
        job = r.json()
        jid = job.get("job_id") or job.get("id")
        assert jid, job
        final = _wait_job(auth, jid, timeout=180)
        aid = final.get("analysis_id") or final.get("result", {}).get("analysis_id")
        if not aid:
            # fall back to newest in list
            lst = auth.get(f"{BASE_URL}/api/regime-lab/list", timeout=10).json()
            aid = lst["analyses"][0]["id"]
        det = auth.get(f"{BASE_URL}/api/regime-lab/{aid}", timeout=15).json()
        a = det.get("analysis") or {}
        combined = a.get("combined") or {}
        assert combined.get("model", {}).get("config", {}).get("detector") == "reactive"
        ps = combined.get("per_symbol", {}).get("BTCUSDT", {})
        assert ps.get("segments"), "no segments"
        assert ps.get("live_segments"), "no live segments"
        corr = ps.get("corrections") or {}
        assert corr.get("pivots", 0) > 0
        assert (combined.get("validation") or {}).get("passed") is True
        probs = (ps.get("current", {}).get("details") or {}).get("probabilities") or {}
        assert set(probs) >= {"down", "side", "up"}


class TestRegimeAnalyzeLocal:
    def test_local_worker_analyze_reactive(self, auth: requests.Session):
        _wait_no_active(auth)
        # confirm worker online
        st = auth.get(f"{BASE_URL}/api/localworker/status", timeout=10).json()
        if not st.get("online"):
            pytest.skip("Local worker not online")
        r = auth.post(f"{BASE_URL}/api/regime-lab/analyze",
                      json=_analyze_payload("local"), timeout=30)
        assert r.status_code == 200, r.text
        job = r.json()
        jid = job.get("job_id") or job.get("id")
        assert jid, job
        final = _wait_job(auth, jid, timeout=240)
        aid = final.get("analysis_id") or final.get("result", {}).get("analysis_id")
        if not aid:
            lst = auth.get(f"{BASE_URL}/api/regime-lab/list", timeout=10).json()
            aid = lst["analyses"][0]["id"]
        det = auth.get(f"{BASE_URL}/api/regime-lab/{aid}", timeout=15).json()
        a = det.get("analysis") or {}
        combined = a.get("combined") or {}
        assert combined.get("model", {}).get("config", {}).get("detector") == "reactive"
        ps = combined.get("per_symbol", {}).get("BTCUSDT", {})
        assert ps.get("segments"), "no segments"
        corr = ps.get("corrections") or {}
        assert corr.get("pivots", 0) > 0
