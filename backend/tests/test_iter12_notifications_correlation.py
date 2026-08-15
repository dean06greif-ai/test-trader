"""Iteration 12 review tests: notifications filter/read/retention + correlation_guard config."""
import os
import time
from datetime import datetime, timedelta, timezone
import pytest
import requests
from pymongo import MongoClient

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://daytrader-ml.preview.emergentagent.com").rstrip("/")
MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "crypto_scanner")

ADMIN_USER = "Admin"
ADMIN_PW = "KryptoAdmin!2026"


@pytest.fixture(scope="module")
def s():
    ses = requests.Session()
    ses.headers.update({"Content-Type": "application/json"})
    return ses


@pytest.fixture(scope="module")
def token(s):
    r = s.post(f"{BASE_URL}/api/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PW}, timeout=15)
    assert r.status_code == 200, r.text
    return r.json().get("token") or r.json().get("access_token")


@pytest.fixture(scope="module")
def auth(s, token):
    s.headers.update({"Authorization": f"Bearer {token}"})
    return s


@pytest.fixture(scope="module")
def db():
    c = MongoClient(MONGO_URL)
    return c[DB_NAME]


def _cleanup(db):
    db.app_notifications.delete_many({"title": {"$regex": "^TEST_"}})


# ---------- Notifications ----------
def test_add_and_filter_notifications(s, db):
    _cleanup(db)
    for i in range(3):
        r = s.post(f"{BASE_URL}/api/notifications", json={"title": f"TEST_msg{i}", "message": f"m{i}", "kind": "error"})
        assert r.status_code == 200
    # unread via filter
    r = s.get(f"{BASE_URL}/api/notifications?filter=unread&limit=100")
    assert r.status_code == 200
    unread = [n for n in r.json()["notifications"] if n.get("title", "").startswith("TEST_")]
    assert len(unread) == 3
    # backward compat: no filter -> unread only
    r = s.get(f"{BASE_URL}/api/notifications?limit=100")
    titles = [n["title"] for n in r.json()["notifications"] if n.get("title", "").startswith("TEST_")]
    assert len(titles) == 3
    # all
    r = s.get(f"{BASE_URL}/api/notifications?filter=all&limit=200")
    assert r.status_code == 200


def test_mark_read_persists_read_at(s, db):
    r = s.get(f"{BASE_URL}/api/notifications?filter=unread&limit=100")
    ids = [n["id"] for n in r.json()["notifications"] if n.get("title", "").startswith("TEST_")]
    assert len(ids) >= 2
    # mark specific ids as read
    r = s.post(f"{BASE_URL}/api/notifications/read", json={"ids": ids[:2]})
    assert r.status_code == 200
    assert r.json().get("updated") >= 2
    # verify in DB: read=True + read_at set
    docs = list(db.app_notifications.find({"id": {"$in": ids[:2]}}))
    for d in docs:
        assert d.get("read") is True
        assert d.get("read_at")  # ISO string
        # parse ISO
        datetime.fromisoformat(d["read_at"])
    # they should now appear in filter=read
    r = s.get(f"{BASE_URL}/api/notifications?filter=read&limit=200")
    read_ids = {n["id"] for n in r.json()["notifications"]}
    assert set(ids[:2]).issubset(read_ids)
    # and not in unread
    r = s.get(f"{BASE_URL}/api/notifications?filter=unread&limit=200")
    unread_ids = {n["id"] for n in r.json()["notifications"]}
    assert set(ids[:2]).isdisjoint(unread_ids)


def test_mark_all_read_without_ids(s, db):
    # remaining test notification should still be unread
    r = s.post(f"{BASE_URL}/api/notifications/read", json={})
    assert r.status_code == 200
    r = s.get(f"{BASE_URL}/api/notifications?filter=unread&limit=200")
    titles = [n["title"] for n in r.json()["notifications"] if n.get("title", "").startswith("TEST_")]
    assert titles == []


def test_purge_deletes_old_read(s, db):
    # Insert one 8-days-old read and one fresh read
    old_iso = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    new_iso = datetime.now(timezone.utc).isoformat()
    db.app_notifications.insert_many([
        {"id": "TEST_purge_old", "title": "TEST_purge_old", "message": "old", "kind": "error",
         "read": True, "read_at": old_iso, "created_at": old_iso, "popped": False},
        {"id": "TEST_purge_new", "title": "TEST_purge_new", "message": "new", "kind": "error",
         "read": True, "read_at": new_iso, "created_at": new_iso, "popped": False},
    ])
    # restart backend to reset _last_purge throttle
    os.system("sudo supervisorctl restart backend >/dev/null 2>&1")
    # wait for backend to be reachable
    for _ in range(30):
        time.sleep(1)
        try:
            probe = requests.get(f"{BASE_URL}/api/notifications?filter=read&limit=1", timeout=5)
            if probe.status_code == 200:
                break
        except Exception:
            pass
    # trigger purge via GET
    r = s.get(f"{BASE_URL}/api/notifications?filter=read&limit=500", timeout=30)
    assert r.status_code == 200
    ids = {n["id"] for n in r.json()["notifications"]}
    assert "TEST_purge_old" not in ids, "8-day-old read notification should be purged"
    assert "TEST_purge_new" in ids, "fresh read notification should still exist"


# ---------- Correlation guard config ----------
def test_ai_status_has_correlation_guard(s):
    r = s.get(f"{BASE_URL}/api/ai/status", timeout=20)
    assert r.status_code == 200
    cfg = r.json().get("config") or {}
    assert "correlation_guard" in cfg
    assert cfg["correlation_guard"] in (True, False)


def test_ai_config_update_correlation_guard(auth):
    # set off
    r = auth.post(f"{BASE_URL}/api/ai/config", json={"correlation_guard": False}, timeout=20)
    assert r.status_code == 200, r.text
    r2 = auth.get(f"{BASE_URL}/api/ai/status", timeout=20)
    assert r2.json()["config"]["correlation_guard"] is False
    # back to on (cleanup)
    r = auth.post(f"{BASE_URL}/api/ai/config", json={"correlation_guard": True}, timeout=20)
    assert r.status_code == 200
    r2 = auth.get(f"{BASE_URL}/api/ai/status", timeout=20)
    assert r2.json()["config"]["correlation_guard"] is True


def test_cleanup_final(db):
    _cleanup(db)
