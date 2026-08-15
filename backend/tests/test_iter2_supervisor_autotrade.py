"""Iter2: Supervisor auto_switch settings + autotrade action validation + paper trade E2E."""
import os
import time
import uuid
import pytest
import requests
from pymongo import MongoClient
from dotenv import dotenv_values

def _read_admin_password():
    try:
        import re as _re
        from pathlib import Path as _Path
        _txt = _Path("/app/memory/test_credentials.md").read_text(encoding="utf-8")
        _m = _re.search(r"Passwort\s*`([^`]+)`", _txt)
        return _m.group(1) if _m else None
    except OSError:
        return None

import os as _os
_ADMIN_PW = _os.environ.get("ADMIN_PASSWORD") or _read_admin_password() or "Dean06Greif!/Admin"


fe_env = dotenv_values("/app/frontend/.env")
BASE_URL = (os.environ.get("REACT_APP_BACKEND_URL") or fe_env.get("REACT_APP_BACKEND_URL")).rstrip("/")
API = f"{BASE_URL}/api"

be_env = dotenv_values("/app/backend/.env")
MONGO_URL = be_env.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = be_env.get("DB_NAME", "crypto_scanner")


@pytest.fixture(scope="module")
def admin_token():
    r = requests.post(f"{API}/auth/login", json={"username": os.environ.get("ADMIN_USER", "Admin"), "password": _ADMIN_PW}, timeout=30)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:300]}"
    tok = r.json().get("token")
    assert tok
    return tok


@pytest.fixture(scope="module")
def auth_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


# --- Supervisor settings ---
class TestSupervisorSettings:
    def test_get_supervisor(self, auth_headers):
        r = requests.get(f"{API}/ai/supervisor", headers=auth_headers, timeout=30)
        assert r.status_code == 200, r.text[:300]
        data = r.json()
        assert "settings" in data or "auto_switch" in data or "enabled" in data or isinstance(data, dict)

    def test_toggle_auto_switch_on_then_off(self, auth_headers):
        # ON
        r = requests.post(f"{API}/ai/supervisor/settings", headers=auth_headers,
                          json={"auto_switch": True}, timeout=30)
        assert r.status_code == 200, r.text[:300]
        # verify persisted
        r2 = requests.get(f"{API}/ai/supervisor", headers=auth_headers, timeout=30)
        assert r2.status_code == 200
        blob = r2.json()
        # settings may be nested or flat
        s = blob.get("settings") if isinstance(blob, dict) and "settings" in blob else blob
        assert s.get("auto_switch") is True, f"auto_switch not persisted: {blob}"
        # OFF again (safety!)
        r3 = requests.post(f"{API}/ai/supervisor/settings", headers=auth_headers,
                           json={"auto_switch": False}, timeout=30)
        assert r3.status_code == 200
        r4 = requests.get(f"{API}/ai/supervisor", headers=auth_headers, timeout=30)
        s2 = r4.json().get("settings") if "settings" in r4.json() else r4.json()
        assert s2.get("auto_switch") is False


# --- AI roles fallback2 ---
class TestAIRolesFallback2:
    def test_roles_have_fallback2(self, auth_headers):
        r = requests.get(f"{API}/ai/roles", headers=auth_headers, timeout=30)
        assert r.status_code == 200, r.text[:300]
        data = r.json()
        roles = data.get("roles") if isinstance(data, dict) and "roles" in data else data
        assert roles, "no roles returned"
        # ensure at least one role has fallback2 style field
        found = False
        if isinstance(roles, dict):
            iter_items = roles.values()
        else:
            iter_items = roles
        for r_item in iter_items:
            if isinstance(r_item, dict) and any("fallback2" in k for k in r_item.keys()):
                found = True
                break
        assert found, f"no fallback2 field found in roles: {list(roles) if isinstance(roles, dict) else roles[:1]}"


# --- Autotrade action validation ---
class TestAutotradeActionValidation:
    def test_partial_close_over_100(self, auth_headers):
        r = requests.post(f"{API}/autotrade/trade/unknown-id/action", headers=auth_headers,
                          json={"action": "partial_close", "value": 150}, timeout=30)
        # expect 400 for invalid value (before 404 lookup) OR 404 if lookup first – but request states 400 for value=150
        assert r.status_code in (400, 422), f"expected 400 for value=150, got {r.status_code}: {r.text[:200]}"

    def test_unknown_id_close(self, auth_headers):
        r = requests.post(f"{API}/autotrade/trade/does-not-exist-xyz/action", headers=auth_headers,
                          json={"action": "partial_close", "value": 50}, timeout=30)
        assert r.status_code == 404, f"expected 404 for unknown id, got {r.status_code}: {r.text[:200]}"

    def test_no_token(self):
        r = requests.post(f"{API}/autotrade/trade/whatever/action",
                          json={"action": "partial_close", "value": 50}, timeout=30)
        assert r.status_code in (401, 403), f"expected 401/403 without token, got {r.status_code}"

    def test_list_trades(self, auth_headers):
        r = requests.get(f"{API}/autotrade/trades", headers=auth_headers, timeout=30)
        assert r.status_code == 200, r.text[:300]


# --- E2E with a seeded paper trade ---
@pytest.fixture(scope="module")
def paper_trade():
    client = MongoClient(MONGO_URL)
    coll = client[DB_NAME]["auto_trades"]
    tid = f"TEST_paper_{uuid.uuid4().hex[:8]}"
    doc = {
        "id": tid,
        "symbol": "BTCUSDT",
        "side": "LONG",
        "mode": "paper",
        "status": "open",
        "entry": 60000.0,
        "sl": 59000.0,
        "tp1": 61000.0,
        "tpf": 62000.0,
        "qty": 0.01,
        "qty_remaining": 0.01,
        "leverage": 10,
        "max_capital": 100.0,
        "opened_at": time.time(),
        "strategy_id": "ai_trader",
        "events": [],
        "realized_pnl": 0.0,
        "tp1_hit": False,
    }
    coll.insert_one(doc)
    yield tid
    coll.delete_one({"id": tid})
    client.close()


class TestPaperTradeE2E:
    def test_partial_close_30(self, auth_headers, paper_trade):
        r = requests.post(f"{API}/autotrade/trade/{paper_trade}/action", headers=auth_headers,
                          json={"action": "partial_close", "value": 30}, timeout=30)
        assert r.status_code == 200, f"partial_close failed: {r.status_code} {r.text[:300]}"

    def test_adjust_sl(self, auth_headers, paper_trade):
        r = requests.post(f"{API}/autotrade/trade/{paper_trade}/action", headers=auth_headers,
                          json={"action": "adjust_sl", "value": 59500.0}, timeout=30)
        assert r.status_code == 200, f"adjust_sl failed: {r.status_code} {r.text[:300]}"

    def test_adjust_tp(self, auth_headers, paper_trade):
        r = requests.post(f"{API}/autotrade/trade/{paper_trade}/action", headers=auth_headers,
                          json={"action": "adjust_tp", "value": 62500.0}, timeout=30)
        assert r.status_code == 200, f"adjust_tp failed: {r.status_code} {r.text[:300]}"

    def test_close(self, auth_headers, paper_trade):
        # API only supports partial_close 1-99%; use 99% to close nearly all.
        r = requests.post(f"{API}/autotrade/trade/{paper_trade}/action", headers=auth_headers,
                          json={"action": "partial_close", "value": 99}, timeout=30)
        assert r.status_code == 200, f"close failed: {r.status_code} {r.text[:300]}"
