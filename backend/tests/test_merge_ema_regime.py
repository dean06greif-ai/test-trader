"""Merge-Verification Tests (Iteration 3): Dean's base + Anton's EMA regime detector.

Coverage:
- /api/health and /api/auth/login (JWT)
- /api/regime-lab/engine/defaults exposes EMA detector config (detector option,
  ema_regime_days, ema_regime_thr, ema_regime_smooth_days, ema_regime_persist_days)
- POST /api/regime-lab/analyze with detector='ema' runs end-to-end (final centered
  slope + live causal slope) without server errors
- Regression: detector='reactive' still works (produces pivots)
- Smoke tests on Dean's routers (autotrade, strategies, ai_lab, ai chat, dynamic)
  respond without 500 (401/403/400/graceful 500-with-mock-hint acceptable per brief).
"""
import os
import time

import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL") or "http://localhost:8001"
BASE_URL = BASE_URL.rstrip("/")
API = f"{BASE_URL}/api"

ADMIN_USER = "Admin"
ADMIN_PASS = os.environ.get("ADMIN_PASSWORD", "admin")


# ------------------------------ Fixtures --------------------------------------
@pytest.fixture(scope="module")
def api_client():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="module")
def admin_token(api_client):
    r = api_client.post(f"{API}/auth/login",
                        json={"username": ADMIN_USER, "password": ADMIN_PASS},
                        timeout=15)
    assert r.status_code == 200, f"Login failed: {r.status_code} {r.text[:200]}"
    tok = r.json().get("token") or r.json().get("access_token")
    assert tok and isinstance(tok, str) and len(tok) > 10
    return tok


@pytest.fixture(scope="module")
def auth_client(api_client, admin_token):
    api_client.headers.update({"Authorization": f"Bearer {admin_token}"})
    return api_client


# ------------------------------ Helpers ---------------------------------------
def _wait_for_slot(client, max_sec=180):
    """Wait until no regime-lab job is running."""
    t0 = time.time()
    while time.time() - t0 < max_sec:
        r = client.get(f"{API}/regime-lab/active", timeout=10)
        if r.status_code == 200 and not r.json().get("active"):
            return True
        time.sleep(3)
    return False


def _poll_job(client, job_id, max_sec=300):
    t0 = time.time()
    last_phase = ""
    while time.time() - t0 < max_sec:
        r = client.get(f"{API}/regime-lab/status/{job_id}", timeout=15)
        if r.status_code != 200:
            time.sleep(2)
            continue
        st = r.json()
        status = st.get("status")
        phase = st.get("phase", "")
        if phase != last_phase:
            print(f"  [{int(time.time()-t0)}s] {status} {st.get('progress',0)}% {phase}")
            last_phase = phase
        if status == "done":
            return st.get("result", {}).get("analysis_id")
        if status in ("error", "cancelled"):
            pytest.fail(f"Job {job_id} ended with {status}: {st.get('error') or ''}")
        time.sleep(3)
    pytest.fail(f"Job {job_id} did not finish within {max_sec}s")


# ------------------------------ Health & Auth ---------------------------------
class TestHealthAuth:
    def test_health(self, api_client):
        r = api_client.get(f"{API}/health", timeout=10)
        assert r.status_code == 200
        assert r.json().get("status") == "alive"

    def test_login_success(self, api_client):
        r = api_client.post(f"{API}/auth/login",
                            json={"username": ADMIN_USER, "password": ADMIN_PASS},
                            timeout=15)
        assert r.status_code == 200
        data = r.json()
        assert "token" in data
        assert isinstance(data["token"], str) and len(data["token"]) > 10

    def test_login_wrong_password(self, api_client):
        r = api_client.post(f"{API}/auth/login",
                            json={"username": ADMIN_USER, "password": "wrong"},
                            timeout=10)
        assert r.status_code == 401

    def test_verify_token(self, api_client, admin_token):
        r = api_client.get(f"{API}/auth/verify",
                           headers={"Authorization": f"Bearer {admin_token}"},
                           timeout=10)
        assert r.status_code == 200
        assert r.json().get("valid") is True


# ------------------------------ Regime-Engine Defaults (Anton merge) ----------
class TestEngineDefaults:
    def test_engine_defaults_exposes_ema(self, api_client):
        r = api_client.get(f"{API}/regime-lab/engine/defaults", timeout=15)
        assert r.status_code == 200
        data = r.json()
        cfg = data.get("config") or {}
        meta = data.get("meta") or []

        # detector default is still reactive (Anton didn't change default)
        assert cfg.get("detector") == "reactive"

        # EMA regime config keys must exist with correct defaults
        assert cfg.get("ema_regime_days") == 14 or cfg.get("ema_regime_days") == 14.0
        assert cfg.get("ema_regime_thr") == 0.18
        assert cfg.get("ema_regime_smooth_days") == 1.0
        assert cfg.get("ema_regime_persist_days") == 1.0

        # Meta keys must exist so UI can render them
        meta_keys = {m.get("key") for m in meta}
        for k in ("detector", "ema_regime_days", "ema_regime_thr",
                  "ema_regime_smooth_days", "ema_regime_persist_days"):
            assert k in meta_keys, f"missing meta key: {k}"

        # detector help text must mention 'ema'
        det_meta = next(m for m in meta if m.get("key") == "detector")
        assert "ema" in (det_meta.get("help", "") or "").lower()


# ------------------------------ EMA detector end-to-end -----------------------
class TestEMARegimeAnalyze:
    def test_analyze_with_ema_detector(self, auth_client):
        assert _wait_for_slot(auth_client), "another job still running"
        body = {
            "symbols": ["BTCUSDT"],
            "timeframe": "1h",
            "days": 90,
            "scope": "combined",
            "engine": "v2",
            "regime_mode": 3,
            "train_pct": 75,
            "engine_config": {"detector": "ema", "ema_regime_days": 9},
        }
        r = auth_client.post(f"{API}/regime-lab/analyze", json=body, timeout=15)
        assert r.status_code == 200, f"start failed: {r.status_code} {r.text[:300]}"
        job_id = r.json()["job_id"]
        aid = _poll_job(auth_client, job_id, max_sec=300)
        assert aid

        r = auth_client.get(f"{API}/regime-lab/{aid}", timeout=20)
        assert r.status_code == 200
        analysis = r.json().get("analysis") or {}
        combined = analysis.get("combined") or {}
        model = combined.get("model") or {}
        mcfg = model.get("config") or {}
        assert mcfg.get("detector") == "ema"
        assert mcfg.get("ema_regime_days") == 9

        per_sym = combined.get("per_symbol") or {}
        btc = per_sym.get("BTCUSDT") or {}
        # FINAL segments (centered slope)
        segs = btc.get("segments") or []
        assert segs, "no final segments produced by EMA detector"
        # LIVE segments (causal slope)
        live_segs = btc.get("live_segments") or []
        assert live_segs, "no live segments produced by EMA detector"
        # live_agreement present (proves centered vs causal comparison ran)
        la = btc.get("live_agreement") or {}
        assert "direction_pct" in la
        # Corrections: EMA detector has NO pivots (per Anton's design)
        corrections = btc.get("corrections") or {}
        assert corrections.get("pivots") == 0, f"EMA detector must have 0 pivots, got {corrections.get('pivots')}"

    def test_reactive_detector_regression(self, auth_client):
        assert _wait_for_slot(auth_client), "another job still running"
        body = {
            "symbols": ["BTCUSDT"],
            "timeframe": "1h",
            "days": 90,
            "scope": "combined",
            "engine": "v2",
            "regime_mode": 3,
            "train_pct": 75,
            # no engine_config -> uses default detector = reactive
        }
        r = auth_client.post(f"{API}/regime-lab/analyze", json=body, timeout=15)
        assert r.status_code == 200, f"start failed: {r.status_code} {r.text[:300]}"
        job_id = r.json()["job_id"]
        aid = _poll_job(auth_client, job_id, max_sec=300)
        assert aid

        r = auth_client.get(f"{API}/regime-lab/{aid}", timeout=20)
        assert r.status_code == 200
        analysis = r.json()["analysis"]
        combined = analysis.get("combined") or {}
        mcfg = (combined.get("model") or {}).get("config") or {}
        assert mcfg.get("detector") == "reactive"
        btc = (combined.get("per_symbol") or {}).get("BTCUSDT") or {}
        corrections = btc.get("corrections") or {}
        # reactive detector must produce pivots (Umkehrpunkte)
        assert corrections.get("pivots", 0) > 0, "reactive detector must produce pivots"


# ------------------------------ Dean's routers smoke --------------------------
DEAN_SMOKE_ENDPOINTS = [
    "/api/coins",
    "/api/signals",
    "/api/settings",
    "/api/session/status",
    "/api/strategies",
    "/api/strategies/builder-options",
    "/api/autotrade/config",
    "/api/autotrade/trades",
    "/api/autotrade/capital",
    "/api/autotrade/sync-status",
    "/api/ai/lab/status",
    "/api/ai/trade/status",
    "/api/ai/closed_loop/status",
    "/api/ai/ml/status",
    "/api/ai/observer/status",
    "/api/ai/memory/stats",
    "/api/regime-lab/list",
    "/api/regime-lab/engine/defaults",
]


@pytest.mark.parametrize("path", DEAN_SMOKE_ENDPOINTS)
def test_dean_smoke_no_500(auth_client, path):
    r = auth_client.get(f"{BASE_URL}{path}", timeout=20)
    # allow 200 or graceful client codes; only 5xx counts as regression
    assert r.status_code < 500, (
        f"{path} returned {r.status_code}: {r.text[:200]}"
    )
