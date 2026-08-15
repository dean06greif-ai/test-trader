"""Isolierter Test für den ai_rewards-Fix (RCA 'Prod leer'): lückenfüllender
Backfill, cleared_at-Semantik, Entry-Regime-Priorität."""
import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/app/backend")
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")
from motor.motor_asyncio import AsyncIOMotorClient

SYM = "REWUSDT"


def _trade(closed_offset_min=0, pnl=-1.0, with_entry_snap=False):
    now = datetime.now(timezone.utc)
    t = {"id": f"rw_{uuid.uuid4().hex[:8]}", "symbol": SYM, "side": "LONG",
         "mode": "paper", "strategy_id": "ai_trader", "status": "closed",
         "result": "win" if pnl > 0 else "loss", "realized_pnl": pnl,
         "max_capital": 100,
         "opened_at": (now - timedelta(minutes=closed_offset_min + 60)).isoformat(),
         "closed_at": (now - timedelta(minutes=closed_offset_min)).isoformat()}
    if with_entry_snap:
        t["entry_market_snapshot"] = {"source": "live", "ts": t["opened_at"],
                                      "features": {"regime": "trend_up", "rsi": 55}}
    return t


async def cleanup(db):
    await db.auto_trades.delete_many({"symbol": SYM})
    await db.ai_rewards.delete_many({"symbol": SYM})
    await db.settings.delete_one({"_id": "ai_rewards_state"})


async def main():
    db = AsyncIOMotorClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]]
    from services import ai_rewards
    await cleanup(db)

    # ---- 1) Lückenfüllender Backfill: Trade ohne Reward wird nachbewertet --
    t1 = _trade(closed_offset_min=120, pnl=2.0)
    t2 = _trade(closed_offset_min=90, pnl=-1.0)
    await db.auto_trades.insert_many([dict(t1), dict(t2)])
    # t1 hat schon einen Reward (Hook lief), t2 nicht (simulierter Hook-Ausfall)
    await ai_rewards.on_trade_closed(db, t1)
    assert await db.ai_rewards.count_documents({"symbol": SYM}) == 1
    n = await ai_rewards.backfill_missing(db)
    assert n == 1, f"erwartet 1 nachbewertet, got {n}"
    assert await db.ai_rewards.count_documents({"symbol": SYM}) == 2
    n2 = await ai_rewards.backfill_missing(db)
    assert n2 == 0, "Backfill muss idempotent sein"
    print("PASS 1: backfill_missing füllt Lücken idempotent")

    # ---- 2) cleared_at: Auto-Backfill nur für Trades NACH dem Löschen ------
    await ai_rewards.clear(db)
    assert await db.ai_rewards.count_documents({"symbol": SYM}) == 0
    t3 = _trade(closed_offset_min=0, pnl=1.5)  # NACH cleared_at geschlossen
    await db.auto_trades.insert_one(dict(t3))
    n = await ai_rewards.backfill_missing(db)
    assert n == 1, f"nur t3 (nach cleared_at) erwartet, got {n}"
    rows = await db.ai_rewards.find({"symbol": SYM}).to_list(10)
    assert len(rows) == 1 and rows[0]["trade_id"] == t3["id"], rows
    print("PASS 2: cleared_at respektiert (alte Trades bleiben unbewertet)")

    # ---- 3) include_cleared=True hebt Löschung auf, bewertet historisch ----
    n = await ai_rewards.backfill_missing(db, include_cleared=True)
    assert n >= 2, f"t1+t2 historisch erwartet, got {n}"  # >=: Backfill ist global
    st = await db.settings.find_one({"_id": "ai_rewards_state"}) or {}
    assert not st.get("cleared_at"), st
    assert await db.ai_rewards.count_documents({"symbol": SYM}) == 3
    print("PASS 3: include_cleared bewertet historische Trades + hebt Sperre auf")

    # ---- 4) Regime-Priorität: entry_market_snapshot gewinnt ----------------
    await cleanup(db)
    t4 = _trade(closed_offset_min=5, pnl=1.0, with_entry_snap=True)
    await db.auto_trades.insert_one(dict(t4))
    r = await ai_rewards.on_trade_closed(db, t4)
    assert r and r["regime"] == "trend_up", r
    print("PASS 4: Regime kommt vom Entry-Snapshot (trend_up), nicht live")

    await cleanup(db)
    print("Cleanup OK")


asyncio.run(main())
