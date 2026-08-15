"""Tests for Fix 0.6 (Boot-Backfill) and Audit-Log endpoints + regressions."""
import os
import time
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL").rstrip("/")
ADMIN_USER = "Admin"
ADMIN_PASS = "Dean06Greif!/Admin"


@pytest.fixture(scope="module")
def token():
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={"username": ADMIN_USER, "password": ADMIN_PASS},
                      timeout=15)
    assert r.status_code == 200, f"Login failed: {r.status_code} {r.text}"
    data = r.json()
    tok = data.get("token") or data.get("access_token")
    assert tok, f"No token in response: {data}"
    return tok


@pytest.fixture(scope="module")
def auth_headers(token):
    return {"Authorization": f"Bearer {token}"}


# ============ Boot-Backfill ============

def test_boot_backfill_status_public():
    r = requests.get(f"{BASE_URL}/api/system/boot-backfill", timeout=30)
    assert r.status_code == 200, r.text
    d = r.json()
    assert "state" in d and d["state"] in ("idle", "running", "done", "error", "disabled")
    assert "symbols_done" in d
    assert "candles_loaded" in d
    assert "cache" in d and "total_candles" in d["cache"]
    print(f"boot-backfill status: state={d['state']} symbols_done={d['symbols_done']}"
          f" candles_loaded={d['candles_loaded']} cache_total={d['cache']['total_candles']}")


def test_boot_backfill_run_requires_admin():
    r = requests.post(f"{BASE_URL}/api/system/boot-backfill/run", timeout=30)
    assert r.status_code in (401, 403), f"Expected 401/403, got {r.status_code}"


def test_boot_backfill_run_with_admin(auth_headers):
    r = requests.post(f"{BASE_URL}/api/system/boot-backfill/run",
                      headers=auth_headers, timeout=15)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d.get("status") in ("started", "already_running")


# ============ Audit-Log ============

def test_audit_log_requires_admin():
    r = requests.get(f"{BASE_URL}/api/audit-log", timeout=10)
    assert r.status_code in (401, 403)


def test_audit_log_with_admin(auth_headers):
    r = requests.get(f"{BASE_URL}/api/audit-log", headers=auth_headers, timeout=10)
    assert r.status_code == 200, r.text
    d = r.json()
    assert "entries" in d
    assert isinstance(d["entries"], list)


def test_analytics_clear_creates_audit(auth_headers):
    # Anzahl vor Aktion
    r0 = requests.get(f"{BASE_URL}/api/audit-log", headers=auth_headers, timeout=10)
    before = len(r0.json().get("entries", []))

    r = requests.post(f"{BASE_URL}/api/analytics/clear",
                      headers=auth_headers,
                      json={"range": "hour", "scope": "all"},
                      timeout=20)
    assert r.status_code == 200, f"analytics/clear failed: {r.status_code} {r.text}"
    time.sleep(0.6)

    r1 = requests.get(f"{BASE_URL}/api/audit-log", headers=auth_headers, timeout=10)
    entries = r1.json().get("entries", [])
    assert len(entries) > before, "No new audit entry after analytics/clear"
    # Neuester Eintrag sollte analytics_clear sein
    latest = entries[0]
    actions = [e.get("action") for e in entries[:3]]
    assert "analytics_clear" in actions, f"analytics_clear not found in latest actions: {actions}"
    matching = next((e for e in entries if e.get("action") == "analytics_clear"), None)
    assert matching is not None
    details = matching.get("details") or {}
    assert details.get("range") == "hour"
    assert details.get("scope") == "all"
    print(f"analytics_clear audit ok: latest={latest.get('action')} details={details}")


def test_ai_rewards_clear_creates_audit(auth_headers):
    r0 = requests.get(f"{BASE_URL}/api/audit-log", headers=auth_headers, timeout=10)
    before_entries = r0.json().get("entries", [])
    before = len(before_entries)

    r = requests.delete(f"{BASE_URL}/api/ai/rewards",
                        headers=auth_headers, timeout=15)
    assert r.status_code == 200, f"ai/rewards delete failed: {r.status_code} {r.text}"
    time.sleep(0.6)

    r1 = requests.get(f"{BASE_URL}/api/audit-log", headers=auth_headers, timeout=10)
    entries = r1.json().get("entries", [])
    assert len(entries) > before
    assert any(e.get("action") == "ai_rewards_clear" for e in entries[:3]), \
        f"ai_rewards_clear not found in latest: {[e.get('action') for e in entries[:3]]}"


def test_audit_sorted_desc(auth_headers):
    r = requests.get(f"{BASE_URL}/api/audit-log", headers=auth_headers, timeout=10)
    entries = r.json().get("entries", [])
    if len(entries) >= 2:
        ts = [e.get("ts") for e in entries if e.get("ts")]
        assert ts == sorted(ts, reverse=True), "audit entries not sorted desc by ts"


# ============ Regression core endpoints ============

def test_health():
    r = requests.get(f"{BASE_URL}/api/health", timeout=10)
    assert r.status_code == 200


def test_signals():
    r = requests.get(f"{BASE_URL}/api/signals", timeout=15)
    assert r.status_code == 200
    d = r.json()
    assert isinstance(d, (list, dict))


def test_strategies():
    r = requests.get(f"{BASE_URL}/api/strategies", timeout=15)
    assert r.status_code == 200


def test_auto_trades():
    r = requests.get(f"{BASE_URL}/api/autotrade/trades", timeout=15)
    assert r.status_code in (200, 401, 403), f"got {r.status_code}"


def test_analytics_or_performance():
    r = requests.get(f"{BASE_URL}/api/analytics", timeout=20)
    if r.status_code == 404:
        r = requests.get(f"{BASE_URL}/api/performance", timeout=20)
    assert r.status_code == 200, f"Neither /api/analytics nor /api/performance ok: {r.status_code}"


def test_login_wrong_pw_rejected():
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={"username": ADMIN_USER, "password": "wrong"},
                      timeout=10)
    assert r.status_code in (400, 401, 403)
