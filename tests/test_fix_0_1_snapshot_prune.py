"""Isolierter Test für Fix 0.1: zeitbasierter Snapshot-Prune."""
import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/app/backend")
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")
from motor.motor_asyncio import AsyncIOMotorClient
from services.ai_market_observer import market_observer, SNAPSHOT_RETENTION_DAYS


def snap(days_ago: float):
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    return {"id": f"test_{uuid.uuid4().hex[:8]}", "symbol": "TESTUSDT", "ts": ts,
            "features": {"rsi": 50}}


async def main():
    db = AsyncIOMotorClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]]

    class _FakeEngine:
        pass
    _FakeEngine.db = db
    market_observer.engine = _FakeEngine()
    await db.ai_market_snapshots.delete_many({"symbol": "TESTUSDT"})

    docs = [snap(d) for d in (0.1, 10, 199, 201, 250, 400)]
    await db.ai_market_snapshots.insert_many(docs)
    before = await db.ai_market_snapshots.count_documents({"symbol": "TESTUSDT"})
    assert before == 6, before

    await market_observer._prune()

    remaining = await db.ai_market_snapshots.find({"symbol": "TESTUSDT"}).to_list(10)
    ages_kept = sorted(
        (datetime.now(timezone.utc) - datetime.fromisoformat(r["ts"])).days
        for r in remaining)
    assert len(remaining) == 3, f"erwartet 3, bekam {len(remaining)} ({ages_kept})"
    assert max(ages_kept) < SNAPSHOT_RETENTION_DAYS, ages_kept
    print(f"PASS: 6 Test-Snapshots -> {len(remaining)} behalten (Alter in Tagen: {ages_kept}); "
          f"201/250/400 Tage alte geloescht")

    # Idempotenz: zweiter Lauf loescht nichts mehr
    await market_observer._prune()
    still = await db.ai_market_snapshots.count_documents({"symbol": "TESTUSDT"})
    assert still == 3, still
    print("PASS: zweiter Prune-Lauf idempotent")

    # echte Snapshots (vom laufenden Observer) unangetastet
    real = await db.ai_market_snapshots.count_documents({"symbol": {"$ne": "TESTUSDT"}})
    print(f"PASS: {real} echte Snapshots unangetastet")

    await db.ai_market_snapshots.delete_many({"symbol": "TESTUSDT"})
    print("Cleanup OK")


asyncio.run(main())
