"""Integration/API tests for the KI-Team (AI ecosystem) endpoints.

Runs against the public preview URL (REACT_APP_BACKEND_URL) and validates:
  - /api/ai/status shape (roles=6, provider_keys incl. github/cerebras, backup_keys, ...)
  - /api/ai/roles GET + POST (auth required, persistence, sanitisation)
  - deep_analyst schedule_times persist, news_watcher interval clamp
  - /api/ai/news-events, /api/ai/calendar, /api/ai/strategy-performance
  - deep-analyze / news-check / analyze require admin, graceful rate-limit errors
  - unchanged endpoints still 200
"""
import os
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://daytrader-ml.preview.emergentagent.com").rstrip("/")
ADMIN_USER = "Admin"


def _read_admin_password():
    try:
        import re as _re
        from pathlib import Path as _Path
        _txt = _Path("/app/memory/test_credentials.md").read_text(encoding="utf-8")
        _m = _re.search(r"Passwort\s*`([^`]+)`", _txt)
        return _m.group(1) if _m else None
    except OSError:
        return None

ADMIN_PASS = os.environ.get("ADMIN_PASSWORD") or _read_admin_password() or "Dean06Greif!/Admin"

REQUIRED_ROLES = {"analyst", "deep_analyst", "news_watcher", "chat", "learner", "summarizer"}


@pytest.fixture(scope="session")
def admin_token():
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={"username": ADMIN_USER, "password": ADMIN_PASS}, timeout=15)
    if r.status_code != 200:
        pytest.skip(f"Admin login failed ({r.status_code}): {r.text[:200]}")
    tok = r.json().get("token") or r.json().get("access_token")
    if not tok:
        pytest.skip(f"No token in login response: {r.text[:200]}")
    return tok


@pytest.fixture
def admin_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}", "Content-Type": "application/json"}


# ---------------- /api/ai/status ----------------
def test_ai_status_shape():
    r = requests.get(f"{BASE_URL}/api/ai/status", timeout=15)
    assert r.status_code == 200, r.text[:300]
    data = r.json()
    # roles
    roles = data.get("roles") or {}
    assert REQUIRED_ROLES.issubset(set(roles.keys())), f"roles missing: got {set(roles.keys())}"
    # provider keys
    pk = data.get("provider_keys") or {}
    for p in ("gemini", "groq", "openrouter", "mistral", "github", "cerebras"):
        assert p in pk, f"provider_keys missing {p}: {pk}"
    # backup keys info
    assert "backup_keys" in data, "backup_keys missing in status"
    # model weights
    assert "model_weights" in data
    # news_watcher status
    assert "news_watcher" in data
    # deep_last fields
    assert "deep_last" in data or "deep_last_at" in data or "deep_last_result" in data, \
        f"deep_last* field missing: keys={list(data.keys())}"


# ---------------- /api/ai/roles ----------------
def test_get_roles_and_labels():
    r = requests.get(f"{BASE_URL}/api/ai/roles", timeout=15)
    assert r.status_code == 200, r.text[:300]
    data = r.json()
    assert REQUIRED_ROLES.issubset(set((data.get("roles") or {}).keys()))
    labels = data.get("labels") or {}
    assert REQUIRED_ROLES.issubset(set(labels.keys()))


def test_post_roles_requires_admin():
    r = requests.post(f"{BASE_URL}/api/ai/roles", json={"analyst": {"enabled": True}}, timeout=15)
    assert r.status_code in (401, 403), f"expected 401/403, got {r.status_code}: {r.text[:200]}"


def test_post_roles_persists_analyst(admin_headers):
    payload = {"analyst": {
        "active_hours": {"start": "09:00", "end": "21:00"},
        "fallback_provider": "groq",
        "fallback_model": "llama-3.1-8b-instant",
    }}
    r = requests.post(f"{BASE_URL}/api/ai/roles", json=payload, headers=admin_headers, timeout=15)
    assert r.status_code == 200, r.text[:300]
    # verify persistence via GET
    g = requests.get(f"{BASE_URL}/api/ai/roles", timeout=15).json()
    an = g["roles"]["analyst"]
    assert an.get("active_hours") == {"start": "09:00", "end": "21:00"}
    assert an.get("fallback_provider") == "groq"
    assert an.get("fallback_model") == "llama-3.1-8b-instant"


def test_post_roles_invalid_values_ignored(admin_headers):
    payload = {"analyst": {
        "model": "quatsch",
        "active_hours": {"start": "25:99", "end": "22:00"},
        "fallback_model": "auch-quatsch",
    }}
    r = requests.post(f"{BASE_URL}/api/ai/roles", json=payload, headers=admin_headers, timeout=15)
    assert r.status_code == 200, r.text[:300]
    g = requests.get(f"{BASE_URL}/api/ai/roles", timeout=15).json()
    an = g["roles"]["analyst"]
    # invalid active_hours must be reset to None
    assert an.get("active_hours") is None
    # invalid fallback_model must NOT be applied ("quatsch" model doesn't exist in catalog).
    # Spec: ignored OR reset to None. Previous good value may persist.
    assert an.get("fallback_model") != "auch-quatsch"


def test_post_roles_deep_schedule(admin_headers):
    payload = {"deep_analyst": {"schedule_times": ["07:30", "19:00"]}}
    r = requests.post(f"{BASE_URL}/api/ai/roles", json=payload, headers=admin_headers, timeout=15)
    assert r.status_code == 200, r.text[:300]
    g = requests.get(f"{BASE_URL}/api/ai/roles", timeout=15).json()
    assert g["roles"]["deep_analyst"].get("schedule_times") == ["07:30", "19:00"]


def test_post_roles_news_interval_clamped(admin_headers):
    r = requests.post(f"{BASE_URL}/api/ai/roles",
                      json={"news_watcher": {"interval_min": 999}},
                      headers=admin_headers, timeout=15)
    assert r.status_code == 200
    g = requests.get(f"{BASE_URL}/api/ai/roles", timeout=15).json()
    assert g["roles"]["news_watcher"].get("interval_min") == 120

    r2 = requests.post(f"{BASE_URL}/api/ai/roles",
                       json={"news_watcher": {"interval_min": 1}},
                       headers=admin_headers, timeout=15)
    assert r2.status_code == 200
    g2 = requests.get(f"{BASE_URL}/api/ai/roles", timeout=15).json()
    assert g2["roles"]["news_watcher"].get("interval_min") == 5


# ---------------- news events / calendar / strategy performance ----------------
def test_news_events_shape():
    r = requests.get(f"{BASE_URL}/api/ai/news-events", timeout=20)
    assert r.status_code == 200, r.text[:300]
    d = r.json()
    assert "events" in d and "watcher" in d
    assert isinstance(d["events"], list)


def test_calendar_shape():
    r = requests.get(f"{BASE_URL}/api/ai/calendar", timeout=30)
    assert r.status_code == 200, r.text[:300]
    d = r.json()
    assert "calendar" in d


def test_strategy_performance_shape():
    r = requests.get(f"{BASE_URL}/api/ai/strategy-performance", timeout=20)
    assert r.status_code == 200, r.text[:300]
    d = r.json()
    assert "text" in d
    assert isinstance(d["text"], str)


# ---------------- LLM endpoints: require admin + graceful rate-limit ----------------
def test_deep_analyze_requires_admin():
    r = requests.post(f"{BASE_URL}/api/ai/deep-analyze", timeout=15)
    assert r.status_code in (401, 403)


def test_news_check_requires_admin():
    r = requests.post(f"{BASE_URL}/api/ai/news-check", timeout=15)
    assert r.status_code in (401, 403)


def test_analyze_requires_admin():
    r = requests.post(f"{BASE_URL}/api/ai/analyze", timeout=15)
    assert r.status_code in (401, 403)


def test_deep_analyze_graceful_rate_limit(admin_headers):
    r = requests.post(f"{BASE_URL}/api/ai/deep-analyze", headers=admin_headers, timeout=60)
    # Not a crash: either 200 with error status OR a 4xx w/ JSON detail
    assert r.status_code < 500, f"unexpected 5xx: {r.status_code} {r.text[:400]}"
    try:
        d = r.json()
    except Exception:
        pytest.fail(f"non-JSON response: {r.text[:200]}")
    # error field or status=error acceptable
    assert isinstance(d, dict)


def test_news_check_graceful(admin_headers):
    r = requests.post(f"{BASE_URL}/api/ai/news-check", headers=admin_headers, timeout=60)
    assert r.status_code < 500, r.text[:400]
    assert isinstance(r.json(), dict)


def test_analyze_graceful(admin_headers):
    r = requests.post(f"{BASE_URL}/api/ai/analyze", headers=admin_headers, timeout=90)
    assert r.status_code < 500, r.text[:400]
    assert isinstance(r.json(), dict)


def test_chat_history_still_ok():
    r = requests.get(f"{BASE_URL}/api/ai/chat/history", timeout=15)
    assert r.status_code == 200
    assert "messages" in r.json()


# ---------------- existing endpoints unchanged ----------------
@pytest.mark.parametrize("path", [
    "/api/signals",
    "/api/performance",
    "/api/settings",
    "/api/strategies",
    "/api/autotrade/config",
    "/api/ai/insights",
    "/api/ai/proposals",
    "/api/ai/news",
])
def test_existing_endpoints_ok(path):
    r = requests.get(f"{BASE_URL}{path}", timeout=20)
    assert r.status_code == 200, f"{path} -> {r.status_code}: {r.text[:200]}"
