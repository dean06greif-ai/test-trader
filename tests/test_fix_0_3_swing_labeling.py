"""Isolierter Test für Fix 0.3: Swing-Signale werden über Tagesgrenzen gelabelt."""
import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/app/backend")
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")
from motor.motor_asyncio import AsyncIOMotorClient


def ev(symbol, days_old, tp1=110.0, sl=90.0):
    return {"id": f"t_{uuid.uuid4().hex[:8]}", "symbol": symbol, "type": "LONG",
            "tp1": tp1, "sl": sl, "strategy_id": "test",
            "ts": (datetime.now(timezone.utc) - timedelta(days=days_old)).isoformat()}


async def main():
    db = AsyncIOMotorClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]]
    from core import state
    from core import pipeline
    state.db = db

    # Szenario: 3-Tage-altes Swing-Signal (vorher: um Mitternacht verworfen),
    # 1 frisches Signal, 1 abgelaufenes (20 Tage)
    swing = ev("SWINGUSDT", days_old=3)
    fresh = ev("SWINGUSDT", days_old=0)
    expired = ev("SWINGUSDT", days_old=20)
    await db.signals.delete_many({"symbol": "SWINGUSDT"})
    await db.signals.insert_many([{"id": e["id"], "symbol": e["symbol"], "type": e["type"],
                                   "strategy_id": "test", "timestamp": e["ts"],
                                   "result": None} for e in (swing, fresh, expired)])
    state.open_signal_evals[:] = [swing, fresh, expired]

    # Preis berührt TP1 -> Swing + frisches Signal werden 'win', expired fliegt raus
    await pipeline.evaluate_open_signals("SWINGUSDT", 111.0)

    s1 = await db.signals.find_one({"id": swing["id"]})
    s2 = await db.signals.find_one({"id": fresh["id"]})
    s3 = await db.signals.find_one({"id": expired["id"]})
    assert s1["result"] == "win", f"Swing-Signal (3 Tage alt) nicht gelabelt: {s1['result']}"
    assert s2["result"] == "win"
    assert s3["result"] is None, "abgelaufenes Signal darf nicht gelabelt werden"
    assert len(state.open_signal_evals) == 0, state.open_signal_evals
    print("PASS 1: 3-Tage-Swing-Signal wird win gelabelt, 20-Tage-Signal expired (result=None)")

    # SL-Fall über Tagesgrenze
    swing2 = ev("SWINGUSDT", days_old=5)
    await db.signals.insert_one({"id": swing2["id"], "symbol": "SWINGUSDT", "type": "LONG",
                                 "strategy_id": "test", "timestamp": swing2["ts"], "result": None})
    state.open_signal_evals[:] = [swing2]
    await pipeline.evaluate_open_signals("SWINGUSDT", 89.0)
    s4 = await db.signals.find_one({"id": swing2["id"]})
    assert s4["result"] == "loss", s4["result"]
    print("PASS 2: 5-Tage-Swing-Signal wird loss gelabelt (SL berührt)")

    # Scheduler: Mitternachts-Clear entfernt?
    import inspect
    from core import scheduler
    src = inspect.getsource(scheduler)
    assert "open_signal_evals.clear()" not in src
    print("PASS 3: Mitternachts-Reset (open_signal_evals.clear) entfernt")

    await db.signals.delete_many({"symbol": "SWINGUSDT"})
    await db.performance.delete_many({"symbol": "SWINGUSDT"})
    state.open_signal_evals.clear()
    print("Cleanup OK")


asyncio.run(main())
