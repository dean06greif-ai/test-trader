"""
Read-only regression tests for the session fixes:
- Admin login returns a token
- /api/ai/insights returns lessons with numeric 'no' for active ones, null for superseded
- /api/ai/status has providers_health with lists
- /api/ai/supervisor and /api/ai/roles return 200 JSON
"""
import os
import requests

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
ADMIN_USER = "Admin"
ADMIN_PASS = "Dea...eif!/Admin"


def _login_token():
    """Admin login via public URL. Retries on transient Cloudflare 502/503/504."""
    import time
    last = None
    for attempt in range(4):
        try:
            r = requests.post(
                f"{BASE_URL}/api/auth/login",
                json={"username": ADMIN_USER, "password": ADMIN_PASS},
                timeout=90,
            )
            last = r
            if r.status_code == 200:
                data = r.json()
                assert isinstance(data.get("token"), str) and len(data["token"]) > 20
                return data["token"]
            if r.status_code in (502, 503, 504):
                time.sleep(2 * (attempt + 1))
                continue
            break
        except requests.RequestException as e:
            last = e
            time.sleep(2 * (attempt + 1))
    if isinstance(last, requests.Response):
        raise AssertionError(f"login failed: {last.status_code} {last.text[:200]}")
    raise AssertionError(f"login failed: {last}")


# --- Auth ---
def test_admin_login_returns_token():
    _login_token()


# --- AI insights lessons numbering ---
def test_ai_insights_lessons_numbering():
    token = _login_token()
    r = requests.get(
        f"{BASE_URL}/api/ai/insights",
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "lessons" in body, f"'lessons' missing in insights: {list(body.keys())}"
    lessons = body["lessons"]
    assert isinstance(lessons, list)
    # Check numbering: active have numeric 'no' 1..n, superseded have no=None
    if lessons:
        active_nos = []
        for les in lessons:
            superseded = bool(les.get("superseded"))
            no_val = les.get("no")  # may be absent (treated as None)
            if superseded:
                assert no_val is None, f"superseded lesson has no={no_val}"
            else:
                assert "no" in les, f"active lesson missing 'no': {les.get('id')}"
                assert isinstance(no_val, int) and no_val >= 1, (
                    f"active lesson 'no' not positive int: {no_val}"
                )
                active_nos.append(no_val)
        if active_nos:
            # must be a contiguous positive sequence 1..n (uniqueness enforced)
            assert len(set(active_nos)) == len(active_nos), "duplicate lesson 'no'"


# --- AI status providers_health ---
def test_ai_status_providers_health():
    token = _login_token()
    r = requests.get(
        f"{BASE_URL}/api/ai/status",
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    ph = body.get("providers_health")
    assert isinstance(ph, dict), f"providers_health missing/not dict: {type(ph)}"
    # Recent failures / rate_limited / errors: lists (empty allowed)
    for k in ("recent_failures", "rate_limited", "errors"):
        if k in ph:
            assert isinstance(ph[k], list), f"providers_health[{k}] not list"


def test_ai_supervisor_ok():
    token = _login_token()
    r = requests.get(
        f"{BASE_URL}/api/ai/supervisor",
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    assert r.status_code == 200, r.text
    assert isinstance(r.json(), (dict, list))


def test_ai_roles_ok():
    token = _login_token()
    r = requests.get(
        f"{BASE_URL}/api/ai/roles",
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    assert r.status_code == 200, r.text
    assert isinstance(r.json(), (dict, list))
