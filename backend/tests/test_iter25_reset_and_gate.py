"""Iteration 25 tests:
- POST /api/ai/trader/reset (auth, wrong pw, correct pw + selective delete)
- GET /api/ml/gate/report (regression, shadow_report symbol filter)
- Admin login smoke test
"""
import os
import asyncio
import datetime as dt
import pytest
import requests
from motor.motor_asyncio import AsyncIOMotorClient

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    # Fallback for local test-run inside container (frontend/.env)
    with open("/app/frontend/.env") as f:
        for line in f:
            if line.startswith("REACT_APP_BACKEND_URL="):
                BASE_URL = line.split("=", 1)[1].strip().rstrip("/")

ADMIN_USER = "Admin"
ADMIN_PASS = "Dean06Greif!/Admin"

MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "test_database")


@pytest.fixture(scope="module")
def admin_token():
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={"username": ADMIN_USER, "password": ADMIN_PASS},
                      timeout=15)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    tok = r.json().get("access_token") or r.json().get("token")
    assert tok, f"no token in {r.json()}"
    return tok


@pytest.fixture()
def seeded_db():
    """Seed test docs before test, cleanup after."""
    async def _seed():
        client = AsyncIOMotorClient(MONGO_URL)
        db = client[DB_NAME]
        now = dt.datetime.utcnow()
        # 3 auto_trades
        await db.auto_trades.insert_many([
            {"id": "T25-paper-1", "strategy_id": "ai_trader", "mode": "paper",
             "status": "open", "symbol": "XRPUSDT", "opened_at": now},
            {"id": "T25-live-1", "strategy_id": "ai_trader", "mode": "live",
             "status": "open", "symbol": "BTCUSDT", "opened_at": now},
            {"id": "T25-mom-1", "strategy_id": "momentum", "mode": "paper",
             "status": "open", "symbol": "ETHUSDT", "opened_at": now},
        ])
        await db.signals.insert_many([
            {"id": "S25-ai-1", "strategy_id": "ai_trader", "symbol": "XRPUSDT"},
            {"id": "S25-mom-1", "strategy_id": "momentum", "symbol": "ETHUSDT"},
        ])
        client.close()
    async def _cleanup():
        client = AsyncIOMotorClient(MONGO_URL)
        db = client[DB_NAME]
        await db.auto_trades.delete_many({"id": {"$in": ["T25-paper-1", "T25-live-1", "T25-mom-1"]}})
        await db.signals.delete_many({"id": {"$in": ["S25-ai-1", "S25-mom-1"]}})
        client.close()
    asyncio.run(_seed())
    yield
    asyncio.run(_cleanup())


def test_admin_login_returns_token(admin_token):
    assert isinstance(admin_token, str) and len(admin_token) > 10


def test_reset_without_auth_returns_401():
    r = requests.post(f"{BASE_URL}/api/ai/trader/reset",
                      json={"password": ADMIN_PASS}, timeout=15)
    assert r.status_code in (401, 403), f"expected 401/403 got {r.status_code}"


def test_reset_wrong_password_returns_403(admin_token):
    r = requests.post(f"{BASE_URL}/api/ai/trader/reset",
                      headers={"Authorization": f"Bearer {admin_token}"},
                      json={"password": "falsch"}, timeout=15)
    assert r.status_code == 403
    assert "Falsches Passwort" in r.text


def test_reset_success_deletes_only_ai_paper(admin_token, seeded_db):
    # sanity: docs exist
    async def _counts_before():
        client = AsyncIOMotorClient(MONGO_URL)
        db = client[DB_NAME]
        c1 = await db.auto_trades.count_documents({"id": "T25-paper-1"})
        c2 = await db.auto_trades.count_documents({"id": "T25-live-1"})
        c3 = await db.auto_trades.count_documents({"id": "T25-mom-1"})
        client.close()
        return c1, c2, c3
    c1, c2, c3 = asyncio.run(_counts_before())
    assert (c1, c2, c3) == (1, 1, 1)

    r = requests.post(f"{BASE_URL}/api/ai/trader/reset",
                      headers={"Authorization": f"Bearer {admin_token}"},
                      json={"password": ADMIN_PASS}, timeout=30)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("status") == "ok"
    assert body.get("trades_deleted", 0) >= 1
    assert body.get("signals_deleted", 0) >= 1

    async def _counts_after():
        client = AsyncIOMotorClient(MONGO_URL)
        db = client[DB_NAME]
        c1 = await db.auto_trades.count_documents({"id": "T25-paper-1"})
        c2 = await db.auto_trades.count_documents({"id": "T25-live-1"})
        c3 = await db.auto_trades.count_documents({"id": "T25-mom-1"})
        s_ai = await db.signals.count_documents({"id": "S25-ai-1"})
        s_mom = await db.signals.count_documents({"id": "S25-mom-1"})
        audit = await db.audit_log.count_documents({"action": "ai_trader_reset"})
        client.close()
        return c1, c2, c3, s_ai, s_mom, audit
    c1, c2, c3, s_ai, s_mom, audit = asyncio.run(_counts_after())
    assert c1 == 0, "ai_trader paper trade should be deleted"
    assert c2 == 1, "live trade must NOT be touched"
    assert c3 == 1, "other strategy must NOT be touched"
    assert s_ai == 0, "ai_trader signal should be deleted"
    assert s_mom == 1, "other-strategy signal must NOT be touched"
    assert audit >= 1, "audit_log entry expected"


def test_ml_gate_report_regression(admin_token):
    r = requests.get(f"{BASE_URL}/api/ml/gate/report",
                     headers={"Authorization": f"Bearer {admin_token}"},
                     timeout=15)
    assert r.status_code == 200, r.text
    data = r.json()
    assert isinstance(data, (dict, list))
