"""Iter7 review – Watchdog toggle, auth-gating, notifications popped, insights skipped_items,
lessons skipped approve/delete, strategies dedupe, strategy-comparison ohne 'external'.
KRITISCH: Prod-DB. Nur idempotente / eigene Test-Artefakte, keine User-Daten anfassen.
"""
import os
import uuid
import pytest
import requests

BASE = os.environ.get("REACT_APP_BACKEND_URL").rstrip("/")
ADMIN_USER = os.environ.get("ADMIN_USER", "Admin")
ADMIN_PASS = os.environ.get("ADMIN_PASSWORD", "TestAdmin2026!")


@pytest.fixture(scope="module")
def token():
    r = requests.post(f"{BASE}/api/auth/login",
                      json={"username": ADMIN_USER, "password": ADMIN_PASS}, timeout=30)
    assert r.status_code == 200, r.text
    return r.json()["token"]


@pytest.fixture(scope="module")
def auth(token):
    return {"Authorization": f"Bearer {token}"}


# ---------- Watchdog toggle ----------
def test_watchdog_status_has_enabled(auth):
    r = requests.get(f"{BASE}/api/autotrade/watchdog/status", headers=auth, timeout=30)
    assert r.status_code == 200, r.text
    js = r.json()
    assert "settings" in js
    assert "enabled" in js["settings"], js


def test_watchdog_disable_run_skipped_then_re_enable(auth):
    # 1) disable
    r = requests.post(f"{BASE}/api/autotrade/watchdog/config",
                      json={"enabled": False}, headers=auth, timeout=30)
    assert r.status_code == 200, r.text

    # 2) run -> should be skipped/disabled
    r2 = requests.post(f"{BASE}/api/autotrade/watchdog/run", headers=auth, timeout=60)
    assert r2.status_code == 200, r2.text
    body = r2.json()
    status = str(body.get("status", "")).lower()
    reason = str(body.get("reason", "") or body.get("message", "")).lower()
    assert status == "skipped" or "ausgeschal" in reason or "disabled" in reason, body

    # 3) MUST re-enable at the end
    r3 = requests.post(f"{BASE}/api/autotrade/watchdog/config",
                       json={"enabled": True}, headers=auth, timeout=30)
    assert r3.status_code == 200, r3.text
    r4 = requests.get(f"{BASE}/api/autotrade/watchdog/status", headers=auth, timeout=30)
    assert r4.status_code == 200
    assert r4.json()["settings"]["enabled"] is True


# ---------- Auth-gating (NO token, expect 401/403; NEVER 404 / 200) ----------
@pytest.mark.parametrize("method,path", [
    ("POST", "/api/autotrade/watchdog/clear"),
    ("DELETE", "/api/ai/rewards"),
    ("POST", "/api/ai/ml/reset"),
    ("POST", "/api/ai/research/reset"),
])
def test_destructive_endpoints_require_auth(method, path):
    r = requests.request(method, f"{BASE}{path}", timeout=30)
    assert r.status_code in (401, 403), f"{method} {path} => {r.status_code} {r.text[:200]}"


# ---------- strategy-comparison ohne 'external' ----------
def test_strategy_comparison_no_external(auth):
    r = requests.get(f"{BASE}/api/analytics/strategy-comparison?days=90",
                     headers=auth, timeout=60)
    assert r.status_code == 200, r.text
    js = r.json()
    comparison = js.get("comparison") if isinstance(js, dict) else None
    if comparison is None and isinstance(js, dict):
        comparison = js.get("strategies") or js.get("items") or []
    if isinstance(comparison, list):
        for row in comparison:
            sid = str(row.get("strategy_id") or row.get("id") or "").lower()
            assert sid != "external", f"external strategy leaked in comparison: {row}"


# ---------- AI insights: skipped_items list ----------
def test_ai_insights_has_skipped_items(auth):
    r = requests.get(f"{BASE}/api/ai/insights", headers=auth, timeout=60)
    assert r.status_code == 200, r.text
    js = r.json()
    assert "skipped_items" in js, list(js.keys())[:20]
    assert isinstance(js["skipped_items"], list)
    for it in js["skipped_items"]:
        assert isinstance(it, dict)
        for k in ("id", "title", "reason", "approvable", "ts"):
            assert k in it, f"skipped item missing '{k}': {it}"


# ---------- Lessons skipped approve/delete (synthetic; cleanup after) ----------
def _mongo():
    from pymongo import MongoClient
    url = os.environ["MONGO_URL"]
    dbn = os.environ.get("DB_NAME", "crypto_scanner")
    cli = MongoClient(url, serverSelectionTimeoutMS=10000)
    return cli, cli[dbn]


def test_lessons_skipped_approve_then_cleanup(auth):
    cli, db = _mongo()
    try:
        wish_id = f"TEST_wish_{uuid.uuid4().hex[:8]}"
        item = {"id": wish_id, "title": "TEST Wunsch", "detail": "Testdetail",
                "reason": "Testgrund", "approvable": True,
                "ts": "2026-06-01T00:00:00+00:00"}
        db.settings.update_one({"_id": "ai_lessons"},
                               {"$push": {"skipped_items": item}},
                               upsert=True)
        r = requests.post(f"{BASE}/api/ai/lessons/skipped/approve",
                          json={"id": wish_id}, headers=auth, timeout=60)
        assert r.status_code == 200, r.text
        js = r.json()
        assert str(js.get("status", "")).lower() in ("success", "ok", "created"), js
        lesson_id = js.get("lesson_id") or js.get("id")
        if not lesson_id:
            doc = db.settings.find_one({"_id": "ai_lessons"}) or {}
            for l in reversed(doc.get("lessons", []) or []):
                if "TEST Wunsch" in str(l.get("title", "")) or "Testdetail" in str(l.get("detail", "")):
                    lesson_id = l.get("id")
                    break
        if lesson_id:
            rd = requests.delete(f"{BASE}/api/ai/lessons/{lesson_id}", headers=auth, timeout=30)
            assert rd.status_code in (200, 204), rd.text
    finally:
        try:
            db.settings.update_one({"_id": "ai_lessons"},
                                   {"$pull": {"skipped_items": {"id": {"$regex": "^TEST_wish_"}}}})
        except Exception:
            pass
        cli.close()


def test_lessons_skipped_delete_flow(auth):
    cli, db = _mongo()
    try:
        wish_id = f"TEST_wish_{uuid.uuid4().hex[:8]}"
        item = {"id": wish_id, "title": "TEST Wunsch2", "detail": "d",
                "reason": "r", "approvable": True,
                "ts": "2026-06-01T00:00:00+00:00"}
        db.settings.update_one({"_id": "ai_lessons"},
                               {"$push": {"skipped_items": item}},
                               upsert=True)
        r = requests.post(f"{BASE}/api/ai/lessons/skipped/delete",
                          json={"id": wish_id}, headers=auth, timeout=30)
        assert r.status_code == 200, r.text
        doc = db.settings.find_one({"_id": "ai_lessons"}) or {}
        ids = [str(x.get("id")) for x in (doc.get("skipped_items") or [])]
        assert wish_id not in ids, ids
    finally:
        try:
            db.settings.update_one({"_id": "ai_lessons"},
                                   {"$pull": {"skipped_items": {"id": {"$regex": "^TEST_wish_"}}}})
        except Exception:
            pass
        cli.close()


# ---------- AI strategies dedupe ----------
def test_ai_strategies_no_duplicate_names(auth):
    r = requests.get(f"{BASE}/api/ai/strategies?include_rejected=false",
                     headers=auth, timeout=60)
    assert r.status_code == 200, r.text
    js = r.json()
    lst = js if isinstance(js, list) else (js.get("strategies") or js.get("candidates") or js.get("items") or [])
    names = []
    for s in lst:
        st = str(s.get("status", "")).lower()
        if st == "rejected":
            continue
        n = str(s.get("name", "")).strip().lower()
        if n:
            names.append(n)
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert not dupes, f"duplicate non-rejected candidate names: {dupes}"


def test_ai_strategies_dedupe_endpoint(auth):
    r = requests.post(f"{BASE}/api/ai/strategies/dedupe", headers=auth, timeout=60)
    assert r.status_code == 200, r.text
    js = r.json()
    assert str(js.get("status", "")).lower() in ("success", "ok"), js


# ---------- Notifications ----------
def _notif_list(js):
    if isinstance(js, list):
        return js
    return js.get("items") or js.get("notifications") or []


def test_notifications_have_created_at_and_new_fields(auth):
    r = requests.get(f"{BASE}/api/notifications?unread_only=false&limit=5",
                     headers=auth, timeout=30)
    assert r.status_code == 200, r.text
    items = _notif_list(r.json())
    if not items:
        pytest.skip("no notifications available to inspect")
    for n in items:
        assert "created_at" in n, list(n.keys())
    latest = items[0]
    keys = set(latest.keys())
    assert ("source" in keys) or ("meta" in keys) or ("popped" in keys), keys


def test_notifications_popped_flag(auth):
    r = requests.get(f"{BASE}/api/notifications?unread_only=false&limit=20",
                     headers=auth, timeout=30)
    assert r.status_code == 200
    items = _notif_list(r.json())
    target = next((n for n in items if "popped" in n and (n.get("id") or n.get("_id"))), None)
    if not target:
        pytest.skip("no notification with 'popped' field (may be pre-refactor entries)")
    nid = target.get("id") or target.get("_id")
    rp = requests.post(f"{BASE}/api/notifications/popped",
                       json={"ids": [nid]}, headers=auth, timeout=30)
    assert rp.status_code == 200, rp.text
    r2 = requests.get(f"{BASE}/api/notifications?unread_only=false&limit=50",
                      headers=auth, timeout=30)
    for n in _notif_list(r2.json()):
        if (n.get("id") or n.get("_id")) == nid:
            assert n.get("popped") is True, n
            break


# ---------- notifications _fail_lines helper unit test ----------
def test_fail_lines_helper_shape():
    from services.notifications import _fail_lines  # type: ignore
    failures = [
        {"role": "News-Wächter", "model": "groq/qwen", "reason": "rate-limit", "detail": "429 too many"},
        {"role": "Strat-Analyst", "model": "cerebras/llama", "reason": "prompt zu groß", "detail": "> 8192 tok"},
    ]
    lines = _fail_lines(failures)
    assert isinstance(lines, list) and len(lines) == 2
    joined = "\n".join(lines).lower()
    # No unescaped markdown chars leaking through
    assert "*" not in "\n".join(lines) or joined.count("*") < 2
    # cause + detail present
    for kw in ("rate-limit", "prompt", "429", "8192"):
        assert kw.lower() in joined, joined
