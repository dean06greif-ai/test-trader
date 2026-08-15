"""Tests Fee-Wächter: SL-Distanz muss mind. fee_guard_mult × Roundtrip-Fees betragen."""
import asyncio
import os
import random
import sys
import time
from datetime import datetime, timezone

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
    db = AsyncIOMotorClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"] + "_test_fee_guard"]
    from services.bitunix_trade import fee_guard_min_sl_pct, fee_guard_check
    from services.ai_engine import ai_engine, DEFAULT_AI_CONFIG

    # ---------- 1) Mathematik + reine Check-Funktion ----------
    assert abs(fee_guard_min_sl_pct(0.06, 4.0) - 0.48) < 1e-9
    assert abs(fee_guard_min_sl_pct(0.10, 3.0) - 0.60) < 1e-9
    cfg = {"fee_percent": 0.06}
    ok, why = fee_guard_check({"fee_guard_enabled": True, "fee_guard_mult": 4.0},
                              cfg, 100.0, 99.8)  # 0.2% < 0.48%
    assert not ok and "Fee-Wächter" in why and "0.48" in why
    ok, _ = fee_guard_check({"fee_guard_enabled": True, "fee_guard_mult": 4.0},
                            cfg, 100.0, 99.0)  # 1.0% >= 0.48%
    assert ok
    ok, _ = fee_guard_check({"fee_guard_enabled": False, "fee_guard_mult": 4.0},
                            cfg, 100.0, 99.99)  # aus -> alles erlaubt
    assert ok
    ok, _ = fee_guard_check({"fee_guard_enabled": True, "fee_guard_mult": 0},
                            cfg, 100.0, 99.99)  # mult 0 -> aus
    assert ok
    ok, _ = fee_guard_check({}, cfg, 100.0, 99.9)  # Defaults: an, 4x -> 0.1% blockt
    assert not ok
    print("PASS 1: fee_guard_min_sl_pct + fee_guard_check (an/aus/mult 0/Defaults)")

    # ---------- 2) update_config: Keys + Klemmen ----------
    old_db, old_cfg = ai_engine.db, ai_engine.config
    ai_engine.db = db
    ai_engine.config = dict(DEFAULT_AI_CONFIG)
    assert ai_engine.config["fee_guard_enabled"] is True
    assert ai_engine.config["fee_guard_mult"] == 4.0
    await ai_engine.update_config({"fee_guard_enabled": False, "fee_guard_mult": 99})
    assert ai_engine.config["fee_guard_enabled"] is False
    assert ai_engine.config["fee_guard_mult"] == 30.0
    await ai_engine.update_config({"fee_guard_enabled": True, "fee_guard_mult": 4})
    assert ai_engine.config["fee_guard_mult"] == 4.0
    ai_engine.db, ai_engine.config = old_db, old_cfg
    print("PASS 2: update_config übernimmt fee_guard_enabled/fee_guard_mult (geklemmt 0-30)")

    # ---------- 3) on_signal: zu enger SL wird geblockt (use_ai_levels) ----------
    from core.state import autotrader
    autotrader.db = db
    autotrader.set_config({"mode": "paper", "coins": {"TESTUSDT": {"enabled": True}},
                           "strategy_coin_configs": {"ai_trader_TESTUSDT": {"mode": "paper"}}})
    await db.auto_trades.delete_many({})
    await db.settings.update_one(
        {"_id": "ai_trader_config"},
        {"$set": {"fee_guard_enabled": True, "fee_guard_mult": 4.0,
                  "max_trades_per_coin": 1, "collection_max_per_coin": 2}}, upsert=True)
    candles = make_candles()
    entry = candles[-1]["close"]
    base = {"symbol": "TESTUSDT", "type": "LONG", "entry_price": entry,
            "strategy_id": "ai_trader", "strategy_name": "KI Trader",
            "timeframe": "1m", "use_ai_levels": True,
            "trade_date": datetime.now(timezone.utc).date().isoformat()}
    s1 = {**base, "id": f"fg1_{int(time.time())}",
          "stop_loss": entry * 0.998,  # 0.2% < 0.48%
          "take_profit_1": entry * 1.004, "take_profit_full": entry * 1.008}
    t1 = await autotrader.on_signal(s1, candles)
    assert t1 is None and "Fee-Wächter" in str(s1.get("_reject_reason")), s1.get("_reject_reason")
    gov = await db.ai_chat.find_one({"role": "governance", "text": {"$regex": "Fee-Wächter"}})
    assert gov, "Governance-Feed-Eintrag für geblockten Nicht-Sammel-Trade fehlt"
    print("PASS 3: on_signal blockt KI-Trade mit 0.2%-SL (< 4× Fees) + Governance-Eintrag")

    # ---------- 4) on_signal: weiter SL geht durch ----------
    s2 = {**base, "id": f"fg2_{int(time.time())}",
          "stop_loss": entry * 0.99,  # 1.0% >= 0.48%
          "take_profit_1": entry * 1.02, "take_profit_full": entry * 1.04}
    t2 = await autotrader.on_signal(s2, candles)
    assert t2 and t2["mode"] == "paper", f"Weiter SL blockiert: {s2.get('_reject_reason')}"
    print("PASS 4: on_signal öffnet KI-Trade mit 1%-SL normal")

    # ---------- 5) Sammel-Trade geblockt OHNE Governance-Spam; Guard aus -> offen ----------
    await db.auto_trades.delete_many({})
    await db.ai_chat.delete_many({})
    s3 = {**base, "id": f"fg3_{int(time.time())}", "type": "SHORT",
          "data_collection": True, "force_paper": True,
          "stop_loss": entry * 1.002,  # 0.2% < 0.48%
          "take_profit_1": entry * 0.996, "take_profit_full": entry * 0.992}
    t3 = await autotrader.on_signal(s3, candles)
    assert t3 is None and "Fee-Wächter" in str(s3.get("_reject_reason"))
    assert await db.ai_chat.count_documents({}) == 0, "Sammel-Block darf keinen Feed-Spam erzeugen"
    await db.settings.update_one({"_id": "ai_trader_config"},
                                 {"$set": {"fee_guard_enabled": False}})
    s4 = {**s3, "id": f"fg4_{int(time.time())}"}
    t4 = await autotrader.on_signal(s4, candles)
    assert t4 and t4["mode"] == "paper", f"Guard aus, trotzdem geblockt: {s4.get('_reject_reason')}"
    print("PASS 5: Sammel-Trade geblockt ohne Feed-Spam; fee_guard_enabled=False lässt ihn durch")

    # ---------- 6) Andere Strategien bleiben unangetastet ----------
    await db.settings.update_one({"_id": "ai_trader_config"},
                                 {"$set": {"fee_guard_enabled": True}})
    await db.auto_trades.delete_many({})
    autotrader.set_config({"mode": "paper", "coins": {"TESTUSDT": {"enabled": True}},
                           "sl_mode": "fixed", "sl_fixed_percent": 0.2})
    s5 = {"symbol": "TESTUSDT", "type": "LONG", "entry_price": entry,
          "strategy_id": "momentum", "strategy_name": "Momentum", "timeframe": "1m",
          "id": f"fg5_{int(time.time())}",
          "trade_date": datetime.now(timezone.utc).date().isoformat()}
    t5 = await autotrader.on_signal(s5, candles)
    assert t5, f"Nicht-KI-Strategie vom Fee-Wächter blockiert: {s5.get('_reject_reason')}"
    print("PASS 6: Fee-Wächter gilt NUR für den KI-Trader, andere Strategien unberührt")

    await db.client.drop_database(db.name)
    print("Cleanup OK – alle 6 Fee-Wächter-Tests grün")


asyncio.run(main())
