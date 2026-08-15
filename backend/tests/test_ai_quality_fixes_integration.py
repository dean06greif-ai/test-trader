"""
Integration tests for AI Quality Fixes against the live server on :8022.
Covers:
- Default config toggles in GET /api/ai/status
- POST /api/ai/config persists new toggles + clamps smart_skip_move_pct
- Lessons conflict resolution (leverage, break_even) via /api/ai/lessons/conflicts
- Unrelated lessons produce no conflict
- Regression: CRUD, master-prompt, validation, schedule, providers/health
"""
import os
import time
import pytest
import requests

BASE_URL = (os.environ.get("KRYPTO_BASE_URL")
            or os.environ.get("REACT_APP_BACKEND_URL")
            or "http://localhost:8022").rstrip("/")
ADMIN_USER = os.environ.get("ADMIN_USER", "Admin")
ADMIN_PASS = os.environ.get("ADMIN_PASSWORD", "admin")


def _server_reachable() -> bool:
    try:
        return requests.get(f"{BASE_URL}/api/health", timeout=5).status_code == 200
    except requests.RequestException:
        return False


pytestmark = pytest.mark.skipif(
    not _server_reachable(),
    reason=f"Kein laufender Backend-Server unter {BASE_URL} (E2E-Test)")


@pytest.fixture(scope="module")
def token():
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={"username": ADMIN_USER, "password": ADMIN_PASS}, timeout=10)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    tok = r.json().get("token") or r.json().get("access_token")
    assert tok, f"no token in response: {r.json()}"
    return tok


@pytest.fixture(scope="module")
def auth_headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ---------------- Config ----------------

def test_status_contains_new_config_keys():
    r = requests.get(f"{BASE_URL}/api/ai/status", timeout=10)
    assert r.status_code == 200
    cfg = r.json()["config"]
    # new keys exist with correct types
    assert "use_heatmap_data" in cfg and isinstance(cfg["use_heatmap_data"], bool)
    assert "use_liquidation_data" in cfg and isinstance(cfg["use_liquidation_data"], bool)
    assert "lean_prompt" in cfg and isinstance(cfg["lean_prompt"], bool)
    assert "smart_skip" in cfg and isinstance(cfg["smart_skip"], bool)
    assert "smart_skip_move_pct" in cfg


def test_config_update_persists_and_clamps(auth_headers):
    # Read current
    original = requests.get(f"{BASE_URL}/api/ai/status", timeout=10).json()["config"]

    # Update with a clamp test (>2.0 -> 2.0)
    payload = {
        "use_heatmap_data": True,
        "use_liquidation_data": False,
        "lean_prompt": False,
        "smart_skip": False,
        "smart_skip_move_pct": 9.9,
    }
    r = requests.post(f"{BASE_URL}/api/ai/config", headers=auth_headers,
                      json=payload, timeout=10)
    assert r.status_code == 200, r.text

    cfg = requests.get(f"{BASE_URL}/api/ai/status", timeout=10).json()["config"]
    assert cfg["use_heatmap_data"] is True
    assert cfg["use_liquidation_data"] is False
    assert cfg["lean_prompt"] is False
    assert cfg["smart_skip"] is False
    assert cfg["smart_skip_move_pct"] == 2.0, f"expected clamp to 2.0, got {cfg['smart_skip_move_pct']}"

    # Lower-bound clamp (<0.02 -> 0.02)
    r2 = requests.post(f"{BASE_URL}/api/ai/config", headers=auth_headers,
                       json={"smart_skip_move_pct": 0.001}, timeout=10)
    assert r2.status_code == 200
    cfg2 = requests.get(f"{BASE_URL}/api/ai/status", timeout=10).json()["config"]
    assert cfg2["smart_skip_move_pct"] == 0.02

    # Restore defaults
    restore = {
        "use_heatmap_data": original.get("use_heatmap_data", False),
        "use_liquidation_data": original.get("use_liquidation_data", True),
        "lean_prompt": original.get("lean_prompt", True),
        "smart_skip": original.get("smart_skip", True),
        "smart_skip_move_pct": original.get("smart_skip_move_pct", 0.15),
    }
    requests.post(f"{BASE_URL}/api/ai/config", headers=auth_headers, json=restore, timeout=10)


def test_config_requires_auth():
    r = requests.post(f"{BASE_URL}/api/ai/config",
                      json={"lean_prompt": True}, timeout=10)
    assert r.status_code in (401, 403), f"unexpected status {r.status_code}"


# ---------------- Lessons Conflicts ----------------

def _cleanup_test_lessons(auth_headers, tag_prefix="ITEST_"):
    r = requests.get(f"{BASE_URL}/api/ai/lessons", timeout=10)
    if r.status_code != 200:
        return
    lessons = r.json().get("lessons", r.json() if isinstance(r.json(), list) else [])
    for l in lessons:
        title = l.get("title", "")
        if title.startswith(tag_prefix):
            lid = l.get("id") or l.get("_id")
            if lid:
                requests.delete(f"{BASE_URL}/api/ai/lessons/{lid}",
                                headers=auth_headers, timeout=10)


def _create_lesson(auth_headers, title, content="lorem ipsum"):
    r = requests.post(f"{BASE_URL}/api/ai/lessons", headers=auth_headers,
                      json={"title": title, "detail": content, "content": content}, timeout=10)
    assert r.status_code in (200, 201), f"create lesson failed: {r.status_code} {r.text}"
    body = r.json()
    return body.get("id") or body.get("_id") or body.get("lesson", {}).get("id")


def test_leverage_conflict_detection(auth_headers):
    _cleanup_test_lessons(auth_headers)
    older_id = _create_lesson(auth_headers, "ITEST_STRIKTE NUTZUNG DES AUTO-LEVERAGE")
    time.sleep(1.1)  # ensure timestamp ordering
    newer_id = _create_lesson(auth_headers, "ITEST_HEBEL 10 STRIKT")

    r = requests.get(f"{BASE_URL}/api/ai/lessons/conflicts", timeout=10)
    assert r.status_code == 200, r.text
    data = r.json()
    conflicts = data.get("conflicts", data if isinstance(data, list) else [])
    lev_conflicts = [c for c in conflicts if c.get("topic") == "leverage"]
    assert lev_conflicts, f"no leverage conflict detected, got: {conflicts}"

    # Verify newest wins in GET /api/ai/lessons (superseded flag persisted)
    all_l = requests.get(f"{BASE_URL}/api/ai/lessons", timeout=10).json()
    lessons = all_l.get("lessons", all_l if isinstance(all_l, list) else [])
    by_id = {(l.get("id") or l.get("_id")): l for l in lessons}
    older = by_id.get(older_id)
    newer = by_id.get(newer_id)
    assert older is not None and newer is not None, "created lessons missing"
    # Nothing deleted
    assert older is not None
    # Older should be superseded, newer active
    assert older.get("superseded") is True or older.get("status") == "superseded" \
        or older.get("active") is False, f"older not superseded: {older}"
    assert older.get("superseded_by") == newer_id or older.get("superseded_by") is not None
    assert newer.get("superseded", False) is False
    _cleanup_test_lessons(auth_headers)


def test_break_even_conflict_detection(auth_headers):
    _cleanup_test_lessons(auth_headers)
    _create_lesson(auth_headers, "ITEST_BREAK-EVEN bei 30 Prozent")
    time.sleep(1.1)
    _create_lesson(auth_headers, "ITEST_BREAK-EVEN bei 40 Prozent (neu)")

    r = requests.get(f"{BASE_URL}/api/ai/lessons/conflicts", timeout=10)
    assert r.status_code == 200
    data = r.json()
    conflicts = data.get("conflicts", data if isinstance(data, list) else [])
    be_conflicts = [c for c in conflicts if c.get("topic") == "break_even"]
    assert be_conflicts, f"no break_even conflict detected: {conflicts}"
    _cleanup_test_lessons(auth_headers)


def test_unrelated_lessons_no_conflict(auth_headers):
    _cleanup_test_lessons(auth_headers)
    _create_lesson(auth_headers, "ITEST_REGIME-4-FOKUS")
    time.sleep(0.3)
    _create_lesson(auth_headers, "ITEST_VOLUMEN-VALIDIERUNG")

    r = requests.get(f"{BASE_URL}/api/ai/lessons/conflicts", timeout=10)
    assert r.status_code == 200
    data = r.json()
    conflicts = data.get("conflicts", data if isinstance(data, list) else [])
    # Filter out any that reference ITEST_ titles
    itest_conflicts = []
    for c in conflicts:
        titles = str(c)
        if "REGIME-4-FOKUS" in titles or "VOLUMEN-VALIDIERUNG" in titles:
            itest_conflicts.append(c)
    assert not itest_conflicts, f"unexpected conflicts for unrelated lessons: {itest_conflicts}"
    _cleanup_test_lessons(auth_headers)


# ---------------- Regression ----------------

def test_lessons_crud_regression(auth_headers):
    _cleanup_test_lessons(auth_headers)
    lid = _create_lesson(auth_headers, "ITEST_CRUD_ONE", content="c1")
    # PATCH
    r = requests.patch(f"{BASE_URL}/api/ai/lessons/{lid}", headers=auth_headers,
                      json={"detail": "c1-updated", "content": "c1-updated"}, timeout=10)
    assert r.status_code in (200, 204), r.text
    # GET verify
    all_l = requests.get(f"{BASE_URL}/api/ai/lessons", timeout=10).json()
    lessons = all_l.get("lessons", all_l if isinstance(all_l, list) else [])
    found = next((l for l in lessons if (l.get("id") or l.get("_id")) == lid), None)
    assert found is not None
    assert "c1-updated" in (found.get("detail") or found.get("content") or "")
    # DELETE
    r = requests.delete(f"{BASE_URL}/api/ai/lessons/{lid}", headers=auth_headers, timeout=10)
    assert r.status_code in (200, 204)


@pytest.mark.parametrize("path", [
    "/api/ai/master-prompt",
    "/api/ai/validation",
    "/api/ai/schedule",
    "/api/ai/providers/health",
])
def test_regression_endpoints_200(path):
    r = requests.get(f"{BASE_URL}{path}", timeout=15)
    assert r.status_code == 200, f"{path} -> {r.status_code}: {r.text[:200]}"
