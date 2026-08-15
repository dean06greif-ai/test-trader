"""Iteration 13: Bugfix Worker-Version-Gate, OOM-Guard, DynamicResult, Discovery-Indicators
+ Features Rule-Variants (unit), Dynamic-Verwaltung, Learning, Auto-Watcher (_due unit)."""
import os
import sys
import time
import pytest
import requests
import subprocess
import json

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL').rstrip('/')
sys.path.insert(0, '/app/backend')


@pytest.fixture(scope="session")
def token():
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={"username": os.environ.get("ADMIN_USER", "Admin"), "password": os.environ.get("ADMIN_PASSWORD", "admin")}, timeout=10)
    assert r.status_code == 200, r.text
    return r.json()["token"]


@pytest.fixture(scope="session")
def auth(token):
    return {"Authorization": f"Bearer {token}"}


def _mongo(cmd):
    r = subprocess.run(["mongosh", "test_database", "--quiet", "--eval", cmd],
                       capture_output=True, text=True, timeout=15)
    return r.stdout.strip()


# ---------- BUGFIX 1: Worker Version Gate ----------
class TestWorkerGate:
    def test_local_dynamic_gate_present(self, auth):
        # Reset first to avoid 409 (already running)
        requests.post(f"{BASE_URL}/api/optimizer/reset", headers=auth, timeout=10)
        # Existierende Custom-Strategie dynamisch auflösen (kein Hardcoding)
        sid = None
        try:
            d = requests.get(f"{BASE_URL}/api/strategies", timeout=15).json()
            strats = d.get("strategies", d) if isinstance(d, dict) else d
            sid = next((s["id"] for s in strats
                        if str(s.get("id", "")).startswith("custom_")), None)
        except Exception:
            pass
        if not sid:
            pytest.skip("Keine Custom-Strategie vorhanden")
        body = {"mode": "dynamic", "execution": "local",
                "strategy_id": sid, "symbols": ["BTCUSDT"],
                "days": 3, "timeframe": "5m"}
        r = requests.post(f"{BASE_URL}/api/optimizer/run", json=body, headers=auth, timeout=10)
        # Expected: 503 (no worker), 409 (outdated), or 200 (worker>=1.3.0 online).
        assert r.status_code in (200, 409, 503), r.text
        if r.status_code == 200:
            # Cancel it, don't leave a job running
            jid = r.json().get("job_id")
            if jid:
                requests.post(f"{BASE_URL}/api/optimizer/cancel/{jid}", headers=auth, timeout=10)
            requests.post(f"{BASE_URL}/api/optimizer/reset", headers=auth, timeout=10)
        elif r.status_code == 503:
            assert "Worker" in r.text or "worker" in r.text
        elif r.status_code == 409:
            assert "veraltet" in r.text or "Dynamik" in r.text

    def test_worker_supports_dynamic_unit(self):
        from services import local_exec
        local_exec.WORKERS.clear()
        # Version 1.2.0 -> False
        local_exec.WORKERS["w1"] = {"version": "1.2.0", "last_seen": time.time()}
        assert local_exec.worker_supports_dynamic() is False
        # Version 1.3.0 -> True
        local_exec.WORKERS["w1"] = {"version": "1.3.0", "last_seen": time.time()}
        assert local_exec.worker_supports_dynamic() is True
        # Stale worker -> False
        local_exec.WORKERS["w1"] = {"version": "1.3.0", "last_seen": 0}
        assert local_exec.worker_supports_dynamic() is False
        local_exec.WORKERS.clear()

    def test_worker_py_version(self):
        import re
        content = open("/app/local_worker/worker.py").read()
        m = re.search(r'^VERSION = "([\d.]+)"', content, re.M)
        assert m, "VERSION fehlt in worker.py"
        # Dynamik-Gate braucht Worker >= 1.3.0
        assert tuple(int(x) for x in m.group(1).split(".")) >= (1, 3, 0)


# ---------- BUGFIX 2: OOM Guard on /api/optimizer/equity ----------
class TestOOMGuard:
    def _insert_fake(self, doc_id, result):
        js = f"db.optimizer_runs.insertOne({{id:'{doc_id}',params:{{}},created_at:'2026-01-01',result:{json.dumps(result)}}})"
        _mongo(js)

    def _delete_fake(self, doc_id):
        _mongo(f"db.optimizer_runs.deleteOne({{id:'{doc_id}'}})")

    def test_equity_big_run_400(self):
        result = {"mode": "discovery", "days": 2000,
                  "symbols": ["BTCUSDT", "ETHUSDT"], "timeframe": "1m",
                  "definition": {"name": "x", "indicators": {},
                                 "long_rules": [], "short_rules": []}}
        self._insert_fake("fake_big", result)
        try:
            r = requests.get(f"{BASE_URL}/api/optimizer/equity/fake_big?scope=optimized",
                             timeout=15)
            assert r.status_code == 400, f"expected 400 RAM guard, got {r.status_code}: {r.text[:200]}"
            detail = (r.json().get("detail") or "").lower()
            assert "ram" in detail or "zu viele" in detail
        finally:
            self._delete_fake("fake_big")

    def test_equity_dynamic_no_trades_400(self):
        result = {"mode": "dynamic", "days": 14, "symbols": ["BTCUSDT"],
                  "timeframe": "5m"}
        self._insert_fake("fake_dyn", result)
        try:
            r = requests.get(f"{BASE_URL}/api/optimizer/equity/fake_dyn?scope=optimized",
                             timeout=15)
            assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text[:200]}"
            detail = (r.json().get("detail") or "").lower()
            assert "dynamisch" in detail or "worker" in detail
        finally:
            self._delete_fake("fake_dyn")


# ---------- Auto-Watcher _due unit ----------
class TestDueUnit:
    def test_due_disabled(self):
        from services import dynamic_live
        assert dynamic_live._due({"settings": {"auto_check_enabled": False}}) is False

    def test_due_never_checked(self):
        from services import dynamic_live
        assert dynamic_live._due({"settings": {"auto_check_enabled": True,
                                                "check_interval_minutes": 60},
                                   "last_state": {}}) is True

    def test_due_recent(self):
        from services import dynamic_live
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        doc = {"settings": {"auto_check_enabled": True, "check_interval_minutes": 60},
               "last_state": {"checked_at": now}}
        assert dynamic_live._due(doc) is False

    def test_due_stale(self):
        from services import dynamic_live
        old = "2020-01-01T00:00:00+00:00"
        doc = {"settings": {"auto_check_enabled": True, "check_interval_minutes": 60},
               "last_state": {"checked_at": old}}
        assert dynamic_live._due(doc) is True


# ---------- Learning summary ----------
class TestLearning:
    def test_summary_public(self):
        r = requests.get(f"{BASE_URL}/api/learning/summary", timeout=10)
        assert r.status_code == 200
        d = r.json()
        assert "entries" in d and "regimes" in d
        assert isinstance(d["regimes"], list)


# ---------- Dynamic-Verwaltung: settings/refresh/log/apply on existing AutoTest Dyn ----------
class TestDynamicManagement:
    DID = None

    @pytest.fixture(scope="class")
    def existing_dyn(self):
        r = requests.get(f"{BASE_URL}/api/dynamic/list", timeout=10)
        assert r.status_code == 200
        strats = r.json().get("strategies") or []
        auto = [s for s in strats if s.get("name") == "AutoTest Dyn"]
        if not auto:
            pytest.skip("AutoTest Dyn nicht vorhanden")
        return auto[0]

    def test_settings_update(self, auth, existing_dyn):
        did = existing_dyn["id"]
        body = {"auto_check_enabled": True, "auto_apply_enabled": False,
                "check_interval_minutes": 60, "check_days": 10}
        r = requests.post(f"{BASE_URL}/api/dynamic/{did}/settings",
                          json=body, headers=auth, timeout=10)
        assert r.status_code == 200, r.text
        s = r.json()["settings"]
        assert s["auto_check_enabled"] is True
        assert s["check_interval_minutes"] == 60
        assert s["check_days"] == 10

    def test_log_endpoint(self, existing_dyn):
        did = existing_dyn["id"]
        r = requests.get(f"{BASE_URL}/api/dynamic/{did}/log", timeout=10)
        assert r.status_code == 200
        log = r.json().get("log") or []
        # AutoTest Dyn has 1 log entry per seed
        if log:
            e = log[0]
            for k in ("from_label", "to_label", "confidence", "reason"):
                assert k in e, f"missing {k} in log entry"


# ---------- Regression: reset + list endpoints ----------
class TestRegression:
    def test_reset_available(self, auth):
        r = requests.post(f"{BASE_URL}/api/optimizer/reset", headers=auth, timeout=10)
        assert r.status_code == 200

    def test_dynamic_list(self):
        r = requests.get(f"{BASE_URL}/api/dynamic/list", timeout=10)
        assert r.status_code == 200
        assert "strategies" in r.json()
