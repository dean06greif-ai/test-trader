"""Isolierter Test für Fix 0.2: entry_market_snapshot an Trade & Decision."""
import asyncio
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/app/backend")
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")
from motor.motor_asyncio import AsyncIOMotorClient


def make_candles(n=120, price=100.0):
    random.seed(7)
    out, ts = [], int(time.time() * 1000) - n * 60_000
    for i in range(n):
        o = price
        price = max(1.0, price * (1 + random.uniform(-0.002, 0.002)))
        out.append({"timestamp": ts + i * 60_000, "open": o, "close": price,
                    "high": max(o, price) * 1.001, "low": min(o, price) * 0.999,
                    "volume": random.uniform(10, 100)})
    return out


async def main():
    db = AsyncIOMotorClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]]
    from services.ai_market_observer import market_observer, compute_features

    candles = make_candles()

    # 1) compute_features liefert vollständige Features auf synthetischen Kerzen
    feats = compute_features(candles)
    assert feats and "rsi" in feats and "regime" in feats, feats
    print(f"PASS 1: compute_features -> {sorted(feats.keys())}")

    # 2) entry_snapshot: frisch aus dem Puffer
    class _Scanner:
        candle_buffer = {"TESTUSDT": candles}

    class _Engine:
        scanner = _Scanner()
        db = None
    market_observer.engine = _Engine()
    snap = market_observer.entry_snapshot("TESTUSDT")
    assert snap and snap["source"] == "live" and snap["features"]["rsi"] == feats["rsi"], snap
    print("PASS 2: entry_snapshot source=live, Features identisch")

    # 3) entry_snapshot: Fallback auf letzten 15-min-Snapshot
    market_observer.snapshots["FALLUSDT"] = {"ts": "2026-08-13T00:00:00+00:00",
                                             "features": {"rsi": 42.0}}
    snap2 = market_observer.entry_snapshot("FALLUSDT")
    assert snap2 and snap2["source"] == "last_snapshot" and snap2["features"]["rsi"] == 42.0
    snap3 = market_observer.entry_snapshot("NIXUSDT")
    assert snap3 is None
    print("PASS 3: Fallback last_snapshot + None ohne Daten")

    # 4) Integration: AutoTrader.on_signal schreibt entry_market_snapshot ins Trade-Doc
    from core.state import autotrader
    autotrader.db = db
    autotrader.set_config({"mode": "paper", "coins": {"TESTUSDT": {"enabled": True}}})
    await db.auto_trades.delete_many({"symbol": "TESTUSDT"})
    signal = {"id": f"testsig_{int(time.time())}", "symbol": "TESTUSDT", "type": "LONG",
              "entry_price": candles[-1]["close"], "strategy_id": "test_strat",
              "strategy_name": "Test", "timeframe": "1m",
              "trade_date": datetime.now(timezone.utc).date().isoformat()}
    trade = await autotrader.on_signal(signal, candles)
    assert trade, f"on_signal lieferte keinen Trade: {signal.get('_reject_reason')}"
    doc = await db.auto_trades.find_one({"id": trade["id"]})
    ems = doc.get("entry_market_snapshot")
    assert ems and ems["source"] == "signal_candles" and "rsi" in ems["features"], ems
    assert ems["features"]["price"] == feats["price"]
    print(f"PASS 4: Trade-Doc hat entry_market_snapshot (source={ems['source']}, "
          f"regime={ems['features']['regime']})")

    await db.auto_trades.delete_many({"symbol": "TESTUSDT"})
    print("Cleanup OK")


asyncio.run(main())
