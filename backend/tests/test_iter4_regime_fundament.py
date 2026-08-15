"""Iteration 4 (Regime-Fundament) Verification Tests.

Coverage:
- BUG FIX: /api/localworker/package/manifest + /api/localworker/package (ZIP)
  complete with worker.py, requirements.txt, README.md, and modules core/services/strategies/models
- POST /api/regime-lab/ema-compare (admin) starts job; job polls to 'done' with
  rows containing per-period metrics + best_period
- GET /api/regime-lab/engine/defaults: meta entries have 'group' + 'detectors';
  ema_regime_days -> group EMA-Regime, detectors=['ema']; persist_days accepts up to 30
- POST /api/dynamic/{id}/settings accepts transition_protection_enabled,
  transition_mode ('block_new'|'close_open'), transition_lock_days (clipped 0-30);
  invalid transition_mode values ignored
- services.trade_guard.check_open_allowed blocks new trades when a
  dynamic_transition_locks doc exists for the symbol
- REGRESSION: /api/health, /api/auth/login, cloud regime-lab analyze with
  detector=ema still works
"""
import asyncio
import io
import os
import time
import uuid
import zipfile
from datetime import datetime, timedelta, timezone

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
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json",
                      "Authorization": f"Bearer {admin_token}"})
    return s


# ------------------------------ Helpers ---------------------------------------
def _wait_for_slot(client, max_sec=180):
    t0 = time.time()
    while time.time() - t0 < max_sec:
        r = client.get(f"{API}/regime-lab/active", timeout=10)
        if r.status_code == 200 and not r.json().get("active"):
            return True
        time.sleep(3)
    return False


def _poll_job(client, job_id, max_sec=180):
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
            return st
        if status in ("error", "cancelled"):
            pytest.fail(f"Job {job_id} ended {status}: {st.get('error') or phase}")
        time.sleep(3)
    pytest.fail(f"Job {job_id} timeout after {max_sec}s")


# ==============================================================================
# Regression: health + auth
# ==============================================================================
class TestRegression:
    def test_health(self, api_client):
        r = api_client.get(f"{API}/health", timeout=10)
        assert r.status_code == 200

    def test_login(self, admin_token):
        assert isinstance(admin_token, str) and len(admin_token) > 10


# ==============================================================================
# BUG FIX: local worker package
# ==============================================================================
class TestLocalWorkerPackage:
    def test_manifest_complete(self, api_client):
        r = api_client.get(f"{API}/localworker/package/manifest", timeout=10)
        assert r.status_code == 200
        m = r.json()
        assert m["complete"] is True, f"Manifest incomplete: {m}"
        assert m["missing"] == []
        for f in ("worker.py", "requirements.txt", "README.md"):
            assert f in m["worker_files"], f"missing {f} in worker_files"
        for mod in ("core", "services", "strategies", "models"):
            assert mod in m["modules"], f"missing module {mod}"
            assert m["modules"][mod] > 0, f"module {mod} empty"

    def test_package_zip_download(self, api_client):
        r = api_client.get(f"{API}/localworker/package", timeout=30)
        assert r.status_code == 200, f"got {r.status_code}: {r.text[:200]}"
        assert r.headers.get("content-type", "").startswith("application/zip"), \
            r.headers.get("content-type")
        assert len(r.content) > 20000, f"zip too small: {len(r.content)}"
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        names = zf.namelist()
        # Required worker files at root
        for f in ("worker.py", "requirements.txt", "README.md"):
            assert f in names, f"{f} missing from zip"
        # Module folders
        for mod in ("core", "services", "strategies", "models"):
            hits = [n for n in names if n.startswith(f"{mod}/") and n.endswith(".py")]
            assert hits, f"no python files under {mod}/ in zip"
        # PACKAGE_INFO.json manifest
        assert "PACKAGE_INFO.json" in names


# ==============================================================================
# Engine defaults with new group/detectors metadata + persist_days clip
# ==============================================================================
class TestEngineDefaultsGroups:
    def test_meta_has_group_and_detectors(self, api_client):
        r = api_client.get(f"{API}/regime-lab/engine/defaults", timeout=15)
        assert r.status_code == 200
        data = r.json()
        meta = data.get("meta") or []
        assert meta and isinstance(meta, list)
        # every meta entry must have 'group' and 'detectors' fields
        missing = [m["key"] for m in meta if "group" not in m or "detectors" not in m]
        assert not missing, f"meta entries without group/detectors: {missing}"

    def test_ema_regime_days_group_ema(self, api_client):
        r = api_client.get(f"{API}/regime-lab/engine/defaults", timeout=15)
        assert r.status_code == 200
        meta = r.json().get("meta") or []
        entry = next((m for m in meta if m["key"] == "ema_regime_days"), None)
        assert entry, "ema_regime_days not in meta"
        assert "EMA-Regime" in (entry["group"] or ""), entry
        assert entry["detectors"] == ["ema"], entry

    def test_persist_days_default_and_clip(self, api_client, auth_client):
        # Default value present
        r = api_client.get(f"{API}/regime-lab/engine/defaults", timeout=15)
        cfg = r.json().get("config") or {}
        assert "ema_regime_persist_days" in cfg
        # Trigger analyze with ema_regime_persist_days=10 (previously clipped to 5).
        # If new clip (30) is active, the job should not error immediately.
        assert _wait_for_slot(auth_client), "regime-lab busy"
        body = {"symbols": ["BTCUSDT"], "timeframe": "1h", "days": 60,
                "scope": "combined", "engine": "v2",
                "engine_config": {"detector": "ema",
                                  "ema_regime_days": 9,
                                  "ema_regime_persist_days": 10}}
        r = auth_client.post(f"{API}/regime-lab/analyze", json=body, timeout=20)
        assert r.status_code == 200, r.text[:300]
        job_id = r.json()["job_id"]
        # Cancel quickly to avoid long run (we only want to verify no clip-fail)
        time.sleep(3)
        # If it already errored -> clip/normalize rejected the value
        st = auth_client.get(f"{API}/regime-lab/status/{job_id}", timeout=10).json()
        assert st.get("status") != "error", \
            f"persist_days=10 not accepted: {st.get('error')}"
        auth_client.post(f"{API}/regime-lab/cancel/{job_id}", timeout=10)


# ==============================================================================
# EMA period compare endpoint
# ==============================================================================
class TestEmaCompare:
    def test_ema_compare_endpoint(self, auth_client):
        assert _wait_for_slot(auth_client), "regime-lab busy"
        body = {"symbols": ["BTCUSDT"], "timeframe": "1h", "days": 90,
                "periods": [5, 14], "train_pct": 75}
        r = auth_client.post(f"{API}/regime-lab/ema-compare", json=body, timeout=20)
        assert r.status_code == 200, r.text[:300]
        job_id = r.json()["job_id"]
        st = _poll_job(auth_client, job_id, max_sec=180)
        result = st.get("result") or {}
        assert result.get("kind") == "ema_compare"
        rows = result.get("rows") or []
        assert len(rows) == 2, f"expected 2 rows, got {len(rows)}"
        # per-period metric keys
        for row in rows:
            assert "period" in row
            # error rows are allowed only if data too short; here 90d must succeed
            assert "error" not in row, f"row error: {row}"
            for k in ("direction_pct", "holdout_direction_pct", "trend_hit_pct",
                      "avg_final_segment_days", "avg_live_segment_days",
                      "switches_final", "switches_live",
                      "violation_pct", "passed"):
                assert k in row, f"row missing {k}: {row}"
        # best_period is one of the periods
        assert result.get("best_period") in (5.0, 14.0, 5, 14), \
            f"best_period unexpected: {result.get('best_period')}"


# ==============================================================================
# Cloud regime-lab analyze regression (detector=ema)
# ==============================================================================
class TestRegimeLabRegression:
    def test_cloud_ema_analyze(self, auth_client):
        assert _wait_for_slot(auth_client), "regime-lab busy"
        body = {"symbols": ["BTCUSDT"], "timeframe": "1h", "days": 60,
                "scope": "combined", "engine": "v2",
                "engine_config": {"detector": "ema", "ema_regime_days": 9}}
        r = auth_client.post(f"{API}/regime-lab/analyze", json=body, timeout=20)
        assert r.status_code == 200, r.text[:300]
        job_id = r.json()["job_id"]
        st = _poll_job(auth_client, job_id, max_sec=180)
        aid = (st.get("result") or {}).get("analysis_id")
        assert aid, f"no analysis_id in result: {st}"

    def test_optimize_accept_and_cancel(self, auth_client):
        """Regression: /api/regime-lab/{aid}/optimize still accepts requests."""
        # Need an analysis first – list existing
        assert _wait_for_slot(auth_client), "regime-lab busy"
        r = auth_client.get(f"{API}/regime-lab/list", timeout=15)
        assert r.status_code == 200
        analyses = r.json().get("analyses") or []
        if not analyses:
            pytest.skip("no analysis available")
        aid = analyses[0]["id"]
        # Get the analysis to find a regime id
        r = auth_client.get(f"{API}/regime-lab/{aid}", timeout=15)
        assert r.status_code == 200
        doc = r.json().get("analysis") or {}
        model = ((doc.get("combined") or {}).get("model") or {})
        regs = model.get("regimes") or []
        if not regs:
            pytest.skip("no regimes in analysis")
        rid = regs[0]["id"]
        body = {"scope": "combined", "regime_id": rid, "mode": "combo",
                "timeframe": doc.get("timeframe") or "1h",
                "iterations": 5, "min_trades": 5}
        r = auth_client.post(f"{API}/regime-lab/{aid}/optimize", json=body, timeout=20)
        # 200 (started) is required; 400 with a clear reason also acceptable
        assert r.status_code in (200, 400), r.text[:300]
        if r.status_code == 200:
            job_id = r.json()["job_id"]
            # Cancel immediately to avoid long runtime
            time.sleep(1)
            auth_client.post(f"{API}/regime-lab/cancel/{job_id}", timeout=10)


# ==============================================================================
# Dynamic settings: transition protection
# ==============================================================================
def _mongo_url():
    with open("/app/backend/.env") as f:
        for line in f:
            if line.startswith("MONGO_URL="):
                return line.split("=", 1)[1].strip().strip('"')
    return None


def _db_name():
    with open("/app/backend/.env") as f:
        for line in f:
            if line.startswith("DB_NAME="):
                return line.split("=", 1)[1].strip().strip('"')
    return None


@pytest.fixture(scope="module")
def dyn_id(auth_client):
    """Ensure a dynamic strategy exists; return its id."""
    from pymongo import MongoClient
    mc = MongoClient(_mongo_url())
    db = mc[_db_name()]
    row = db.dynamic_strategies.find_one({}, {"id": 1})
    if row and row.get("id"):
        mc.close()
        return row["id"]
    # Create a minimal doc
    did = f"dyn_test_{uuid.uuid4().hex[:6]}"
    db.dynamic_strategies.insert_one({
        "id": did, "name": "QA-Test",
        "strategy_id": "nnfx_trend",
        "symbols": ["BTCUSDT"], "timeframe": "1h",
        "model": {"regimes": []}, "configs": {},
        "settings": {"auto_check_enabled": False, "auto_apply_enabled": False,
                     "check_interval_minutes": 60, "check_days": 30},
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    mc.close()
    return did


class TestDynamicTransitionSettings:
    def test_persist_transition_fields(self, auth_client, dyn_id):
        body = {"transition_protection_enabled": True,
                "transition_mode": "close_open",
                "transition_lock_days": 5}
        r = auth_client.post(f"{API}/dynamic/{dyn_id}/settings", json=body, timeout=15)
        assert r.status_code == 200, r.text[:300]
        s = r.json().get("settings") or {}
        assert s.get("transition_protection_enabled") is True
        assert s.get("transition_mode") == "close_open"
        assert s.get("transition_lock_days") == 5.0

    def test_lock_days_clip(self, auth_client, dyn_id):
        # 999 must clip to 30, -5 to 0
        r = auth_client.post(f"{API}/dynamic/{dyn_id}/settings",
                             json={"transition_lock_days": 999}, timeout=15)
        assert r.status_code == 200
        assert r.json()["settings"]["transition_lock_days"] == 30.0
        r = auth_client.post(f"{API}/dynamic/{dyn_id}/settings",
                             json={"transition_lock_days": -5}, timeout=15)
        assert r.status_code == 200
        assert r.json()["settings"]["transition_lock_days"] == 0.0

    def test_invalid_mode_ignored(self, auth_client, dyn_id):
        # Set to valid first
        auth_client.post(f"{API}/dynamic/{dyn_id}/settings",
                         json={"transition_mode": "block_new"}, timeout=15)
        r = auth_client.post(f"{API}/dynamic/{dyn_id}/settings",
                             json={"transition_mode": "invalid_mode"}, timeout=15)
        assert r.status_code == 200
        # Should remain 'block_new' (invalid ignored)
        assert r.json()["settings"]["transition_mode"] == "block_new"


# ==============================================================================
# trade_guard.check_open_allowed with transition lock
# ==============================================================================
class TestTradeGuardTransitionLock:
    def test_lock_blocks_new_trades(self):
        from pymongo import MongoClient
        from motor.motor_asyncio import AsyncIOMotorClient
        import sys
        sys.path.insert(0, "/app/backend")
        from services import trade_guard

        mongo_url = _mongo_url()
        dbname = _db_name()
        sync_c = MongoClient(mongo_url)
        sync_db = sync_c[dbname]

        symbol = f"TEST_{uuid.uuid4().hex[:6]}USDT"
        locked_until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        lock_doc = {"dynamic_id": "test", "name": "QA",
                    "symbol": symbol,
                    "locked_until": locked_until,
                    "mode": "block_new"}
        sync_db.dynamic_transition_locks.insert_one(dict(lock_doc))
        try:
            async_c = AsyncIOMotorClient(mongo_url)
            async_db = async_c[dbname]
            # Make sure kill switch is disabled to isolate the transition check
            async def _run():
                # Reset trade_guard cache to reflect config
                trade_guard._cfg_cache = None
                allowed, reason = await trade_guard.check_open_allowed(
                    async_db, {"symbol": symbol, "type": "long"}, "1m")
                return allowed, reason

            allowed, reason = asyncio.get_event_loop().run_until_complete(_run())
            assert allowed is False, f"expected blocked, got allowed=True"
            assert "Übergangsschutz" in reason, f"reason missing 'Übergangsschutz': {reason}"
            async_c.close()
        finally:
            sync_db.dynamic_transition_locks.delete_many({"symbol": symbol})
            sync_c.close()
