"""E2E API-Tests für Iteration 4 (Preview): Supervisor-Settings/History/Rollback,
Quick-Prompts (GET/POST inkl. Auth-Schutz) und apply-assist.
"""
import os
import time
import pytest
import requests

BASE = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
ADMIN_USER = os.environ.get("ADMIN_USER", "Admin")


def _read_admin_password():
    try:
        import re as _re
        from pathlib import Path as _Path
        _txt = _Path("/app/memory/test_credentials.md").read_text(encoding="utf-8")
        _m = _re.search(r"Passwort\s*`([^`]+)`", _txt)
        return _m.group(1) if _m else None
    except OSError:
        return None

ADMIN_PW = os.environ.get("ADMIN_PASSWORD") or _read_admin_password() or "Dean06Greif!/Admin"


@pytest.fixture(scope="module")
def token():
    r = requests.post(f"{BASE}/api/auth/login",
                      json={"username": ADMIN_USER, "password": ADMIN_PW}, timeout=15)
    assert r.status_code == 200, r.text
    return r.json().get("token") or r.json().get("access_token")


@pytest.fixture(scope="module")
def auth(token):
    return {"Authorization": f"Bearer {token}"}


# ---------- Supervisor Settings ----------
class TestSupervisorSettings:
    def test_get_supervisor_returns_settings(self, auth):
        r = requests.get(f"{BASE}/api/ai/supervisor", headers=auth, timeout=15)
        assert r.status_code == 200
        data = r.json()
        assert "settings" in data
        s = data["settings"]
        for k in ("auto_enabled", "interval_hours", "auto_switch"):
            assert k in s

    def test_settings_sanitize_low_and_high(self, auth):
        # low clamps to 6
        r = requests.post(f"{BASE}/api/ai/supervisor/settings",
                          headers=auth, json={"interval_hours": 1}, timeout=15)
        assert r.status_code == 200, r.text
        s = r.json().get("settings", r.json())
        assert s["interval_hours"] == 6
        # high clamps to 168
        r = requests.post(f"{BASE}/api/ai/supervisor/settings",
                          headers=auth, json={"interval_hours": 999}, timeout=15)
        assert r.status_code == 200
        s = r.json().get("settings", r.json())
        assert s["interval_hours"] == 168

    def test_settings_persist_across_get(self, auth):
        r = requests.post(f"{BASE}/api/ai/supervisor/settings",
                          headers=auth,
                          json={"auto_enabled": True, "interval_hours": 48,
                                "auto_switch": True}, timeout=15)
        assert r.status_code == 200
        r2 = requests.get(f"{BASE}/api/ai/supervisor", headers=auth, timeout=15)
        s = r2.json()["settings"]
        assert s["auto_enabled"] is True
        assert s["interval_hours"] == 48
        assert s["auto_switch"] is True

    def test_reset_to_defaults(self, auth):
        r = requests.post(f"{BASE}/api/ai/supervisor/settings",
                          headers=auth,
                          json={"auto_enabled": False, "auto_switch": False,
                                "interval_hours": 24}, timeout=15)
        assert r.status_code == 200
        s = r.json().get("settings", r.json())
        assert s == {"auto_enabled": False,
                     "interval_hours": 24, "auto_switch": False}


# ---------- Supervisor History ----------
class TestSupervisorHistory:
    def test_history_endpoint(self, auth):
        r = requests.get(f"{BASE}/api/ai/supervisor/history?limit=10",
                         headers=auth, timeout=15)
        assert r.status_code == 200
        payload = r.json()
        rows = payload.get("reports", payload) if isinstance(payload, dict) else payload
        assert isinstance(rows, list)
        if rows:
            for k in ("ts", "trigger", "model", "roles"):
                assert k in rows[0]
            # neueste zuerst
            ts_list = [row["ts"] for row in rows]
            assert ts_list == sorted(ts_list, reverse=True)


# ---------- Rollback ----------
class TestRollback:
    def test_rollback_without_active_switch_returns_error(self, auth):
        r = requests.post(f"{BASE}/api/ai/supervisor/rollback",
                          headers=auth, json={}, timeout=15)
        # 400 wenn keine Umschaltung aktiv
        assert r.status_code in (400, 404), r.text
        body = r.text.lower()
        assert "keine automatische umschaltung" in body or "umschalt" in body


# ---------- Quick-Prompts ----------
class TestQuickPrompts:
    def test_get_quick_prompts_public(self):
        r = requests.get(f"{BASE}/api/ai/quick-prompts", timeout=15)
        assert r.status_code == 200
        data = r.json()
        assert "prompts" in data and isinstance(data["prompts"], list)
        assert "customized" in data

    def test_post_requires_admin(self):
        r = requests.post(f"{BASE}/api/ai/quick-prompts",
                          json={"prompts": ["x"]}, timeout=15)
        assert r.status_code in (401, 403)

    def test_post_trims_and_saves(self, auth):
        payload = {"prompts": ["   Test A  ", "", "Test B", "  ", "Test C"]}
        r = requests.post(f"{BASE}/api/ai/quick-prompts",
                          headers=auth, json=payload, timeout=15)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["prompts"] == ["Test A", "Test B", "Test C"]
        # persistiert
        g = requests.get(f"{BASE}/api/ai/quick-prompts", timeout=15).json()
        assert g["prompts"] == ["Test A", "Test B", "Test C"]
        assert g["customized"] is True

    def test_post_limits_to_30(self, auth):
        payload = {"prompts": [f"P{i}" for i in range(40)]}
        r = requests.post(f"{BASE}/api/ai/quick-prompts",
                          headers=auth, json=payload, timeout=15)
        assert r.status_code == 200
        assert len(r.json()["prompts"]) == 30

    def test_restore_defaults(self, auth):
        defaults = [
            "Wie ist deine aktuelle Performance?",
            "Was hast du zuletzt gelernt?",
            "Sei heute defensiv",
            "Begründe deine letzte Entscheidung",
        ]
        r = requests.post(f"{BASE}/api/ai/quick-prompts",
                          headers=auth, json={"prompts": defaults}, timeout=15)
        assert r.status_code == 200
        assert r.json()["prompts"] == defaults


# ---------- apply-assist ----------
class TestApplyAssist:
    def test_apply_without_prior_assist_returns_400(self, auth):
        # neuen Kandidaten anlegen (ohne last_assist)
        r = requests.post(f"{BASE}/api/ai/strategies",
                          headers=auth,
                          json={"name": "TEST_iter4_apply",
                                "thesis": "Test",
                                "rules_text": "wenn RSI<30 kaufen",
                                "symbols": ["BTCUSDT"]},
                          timeout=20)
        assert r.status_code in (200, 201), r.text
        cand = r.json()
        cid = cand.get("id") or cand.get("candidate", {}).get("id")
        assert cid
        try:
            r2 = requests.post(f"{BASE}/api/ai/strategies/{cid}/apply-assist",
                               headers=auth, json={}, timeout=15)
            assert r2.status_code == 400
        finally:
            # cleanup - reject candidate
            requests.post(f"{BASE}/api/ai/strategies/{cid}/decide",
                          headers=auth, json={"action": "reject"}, timeout=15)
