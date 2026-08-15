"""E2E tests for iteration 3: KI-Trader improvements.
Tests the new endpoints:
  * GET /api/ai/proposals/actionable (empty in auto, list in suggest)
  * GET /api/ai/supervisor / POST /api/ai/supervisor/review
  * max_lessons persistence up to 100
"""
import os
import time
import requests
import pytest

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://daytrader-ml.preview.emergentagent.com").rstrip("/")
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

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD") or _read_admin_password() or "Dean06Greif!/Admin"


@pytest.fixture(scope="module")
def token():
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={"username": ADMIN_USER, "password": ADMIN_PASSWORD}, timeout=30)
    assert r.status_code == 200, r.text
    return r.json().get("token") or r.json().get("access_token")


@pytest.fixture(scope="module")
def auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def original_config(auth):
    r = requests.get(f"{BASE_URL}/api/ai/status", headers=auth, timeout=30)
    assert r.status_code == 200
    cfg = r.json().get("config", {})
    yield cfg
    # restore
    restore = {k: cfg.get(k) for k in ("autonomy", "max_lessons") if cfg.get(k) is not None}
    if restore:
        requests.post(f"{BASE_URL}/api/ai/config", headers=auth, json=restore, timeout=30)


def _set_autonomy(auth, mode):
    r = requests.post(f"{BASE_URL}/api/ai/config", headers=auth, json={"autonomy": mode}, timeout=30)
    assert r.status_code == 200, r.text
    return r.json()


def test_actionable_proposals_empty_in_auto(auth, original_config):
    _set_autonomy(auth, "auto")
    r = requests.get(f"{BASE_URL}/api/ai/proposals/actionable", headers=auth, timeout=30)
    assert r.status_code == 200, r.text
    data = r.json()
    proposals = data.get("proposals", data if isinstance(data, list) else [])
    assert proposals == [], f"expected empty in auto, got {proposals}"


def test_actionable_proposals_shape_in_suggest(auth, original_config):
    _set_autonomy(auth, "suggest")
    r = requests.get(f"{BASE_URL}/api/ai/proposals/actionable", headers=auth, timeout=30)
    assert r.status_code == 200, r.text
    data = r.json()
    proposals = data.get("proposals", data if isinstance(data, list) else [])
    assert isinstance(proposals, list)


def test_max_lessons_100(auth, original_config):
    r = requests.post(f"{BASE_URL}/api/ai/config", headers=auth,
                      json={"max_lessons": 100}, timeout=30)
    assert r.status_code == 200, r.text
    r2 = requests.get(f"{BASE_URL}/api/ai/status", headers=auth, timeout=30)
    cfg = r2.json().get("config", {})
    assert cfg.get("max_lessons") == 100


def test_max_lessons_clamped(auth, original_config):
    r = requests.post(f"{BASE_URL}/api/ai/config", headers=auth,
                      json={"max_lessons": 500}, timeout=30)
    assert r.status_code == 200
    cfg = requests.get(f"{BASE_URL}/api/ai/status", headers=auth, timeout=30).json().get("config", {})
    assert cfg.get("max_lessons") == 100


def test_supervisor_get():
    r = requests.get(f"{BASE_URL}/api/ai/supervisor", timeout=30)
    assert r.status_code == 200, r.text
    data = r.json()
    # can be {report: None|{...}, status: 'idle'|'running'|'done'}
    assert isinstance(data, dict)


def test_supervisor_start(auth):
    r = requests.post(f"{BASE_URL}/api/ai/supervisor/review", headers=auth, timeout=30)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data.get("status") in ("started", "running", "done"), data
