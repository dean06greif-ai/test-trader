"""Regression: verify AITradeManager.review query excludes manual/external trades
using a real Mongo insert on the local db, then cleanup.
"""
import asyncio
import os
import pytest
from motor.motor_asyncio import AsyncIOMotorClient


MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]


def test_review_query_excludes_manual_and_external():
    asyncio.run(_run())


async def _run():
    client = AsyncIOMotorClient(MONGO_URL)
    db = client[DB_NAME]
    inserted_ids = []
    try:
        # Insert a manual QQQ trade -> should NOT be returned by review query
        r1 = await db.auto_trades.insert_one({
            "status": "open", "manual_trade": True, "symbol": "QQQUSDT",
            "side": "long", "strategy_id": "external",
            "_test_marker": "TEST_qqq_regression",
        })
        r2 = await db.auto_trades.insert_one({
            "status": "open", "external_adopted": True, "symbol": "QQQUSDT",
            "side": "long", "strategy_id": "external",
            "_test_marker": "TEST_qqq_regression",
        })
        # Insert a normal trade -> SHOULD be returned
        r3 = await db.auto_trades.insert_one({
            "status": "open", "symbol": "BTCUSDT",
            "side": "long", "strategy_id": "ai_trader",
            "_test_marker": "TEST_qqq_regression",
        })
        inserted_ids = [r1.inserted_id, r2.inserted_id, r3.inserted_id]

        query = {"status": "open",
                 "manual_trade": {"$ne": True},
                 "external_adopted": {"$ne": True},
                 "strategy_id": {"$ne": "external"}}
        cursor = db.auto_trades.find(query)
        results = await cursor.to_list(length=1000)
        result_ids = [r["_id"] for r in results]
        assert r1.inserted_id not in result_ids, "manual_trade must be excluded"
        assert r2.inserted_id not in result_ids, "external_adopted must be excluded"
        assert r3.inserted_id in result_ids, "normal trade must be returned"
    finally:
        for _id in inserted_ids:
            await db.auto_trades.delete_one({"_id": _id})
        client.close()
