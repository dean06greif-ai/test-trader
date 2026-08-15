"""Tests Fee-Feedback: Fee-Anteil in Rewards/Lern-Prompt + Blockier-Statistik."""
import asyncio
import os
import random
import sys
import time
from datetime import datetime, timezone, timedelta

sys.path.insert(0, "/app/backend")
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")
from motor.motor_asyncio import AsyncIOMotorClient


def make_candles(n=120, price=100.0):
    random.seed(3)
    out, ts = [], int(time.time() * 1000) - n * 60_000
    for i in range(n):
        o = price
        price = max(1.0, price * (1 + random.uniform(-0.002, 0.002)))
        out.append({"timestamp": ts + i * 60_000, "open": o, "close": price,
                    "high": max(o, price) * 1.001, "low": min(o, price) * 0.999,
                    "volume": random.uniform(10, 100)})
    return out


def _trade(pnl, fees, result, tid):
    now = datetime.now(timezone.utc)
    return {"id": tid, "strategy_id": "ai_trader", "status": "closed",
            "symbol": "TESTUSDT", "side": "LONG", "mode": "paper",
            "realized_pnl": pnl, "fees_paid": fees, "max_capital": 100.0,
            "result": result,
            "opened_at": (now - timedelta(hours=1)).isoformat(),
            "closed_at": now.isoformat()}


async def main():
    db = AsyncIOMotorClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"] + "_test_fee_fb"]
    from services import ai_rewards

    # ---------- 1) compute_reward: Fee-Anteil am Verlust ----------
    r = ai_rewards.compute_reward(_trade(-1.0, 0.8, "loss", "t1"))
    assert r["fees"] == 0.8 and r["fee_share_pct"] == 80.0, r
    r = ai_rewards.compute_reward(_trade(-0.5, 0.9, "loss", "t2"))
    assert r["fee_share_pct"] == 100.0, "Anteil muss auf 100% gekappt sein"
    r = ai_rewards.compute_reward(_trade(2.0, 0.3, "win", "t3"))
    assert r["fee_share_pct"] is None and r["fees"] == 0.3
    print("PASS 1: compute_reward liefert fees + fee_share_pct (nur bei Verlusten, gekappt)")

    # ---------- 2) context_text: GEBÜHREN-ANTEIL-Block im Lern-Prompt ----------
    for i, (pnl, fees, res) in enumerate([(-1.0, 0.8, "loss"), (-2.0, 0.4, "loss"),
                                          (3.0, 0.3, "win")]):
        await ai_rewards.on_trade_closed(db, _trade(pnl, fees, res, f"fb{i}"))
    assert await db.ai_rewards.count_documents({}) == 3
    txt = await ai_rewards.context_text(db, days=14)
    assert "GEBÜHREN-ANTEIL" in txt, txt
    assert "bei 1 von 2 Verlusten" in txt, txt  # 80% und 20% -> einer >=50%
    hist = await ai_rewards.history(db, days=14)
    assert any(h.get("fee_share_pct") == 80.0 for h in hist)
    print("PASS 2: Lern-Prompt enthält GEBÜHREN-ANTEIL-Feedback, history trägt fee_share_pct")

    # ---------- 3) Fee-Wächter-Block wird in fee_guard_blocks protokolliert ----------
    from core.state import autotrader
    autotrader.db = db
    autotrader.set_config({"mode": "paper", "coins": {"TESTUSDT": {"enabled": True}},
                           "strategy_coin_configs": {"ai_trader_TESTUSDT": {"mode": "paper"}}})
    await db.auto_trades.delete_many({})
    await db.settings.update_one(
        {"_id": "ai_trader_config"},
        {"$set": {"fee_guard_enabled": True, "fee_guard_mult": 4.0}}, upsert=True)
    candles = make_candles()
    entry = candles[-1]["close"]
    s1 = {"symbol": "TESTUSDT", "type": "LONG", "entry_price": entry,
          "strategy_id": "ai_trader", "strategy_name": "KI Trader",
          "timeframe": "1m", "use_ai_levels": True, "id": f"fb_{int(time.time())}",
          "stop_loss": entry * 0.998, "take_profit_1": entry * 1.004,
          "take_profit_full": entry * 1.008,
          "trade_date": datetime.now(timezone.utc).date().isoformat()}
    t1 = await autotrader.on_signal(s1, candles)
    assert t1 is None
    blk = await db.fee_guard_blocks.find_one({"symbol": "TESTUSDT"})
    assert blk and blk["side"] == "LONG" and blk["collection"] is False
    assert 0.15 < blk["sl_dist_pct"] < 0.25 and blk["est_fees_usdt"] > 0, blk
    print("PASS 3: Block landet in fee_guard_blocks (sl_dist_pct + est_fees_usdt)")

    # ---------- 4) Stats-Endpoint-Logik (Aggregation wie routers/ai.py) ----------
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    rows = await db.fee_guard_blocks.find(
        {"ts": {"$gte": cutoff}}, {"_id": 0}).sort("ts", -1).to_list(1000)
    assert len(rows) == 1
    est = round(sum(float(r.get("est_fees_usdt") or 0) for r in rows), 2)
    assert est == round(blk["est_fees_usdt"], 2)
    print("PASS 4: Stats-Aggregation (blocked_total=1, est_fees_saved korrekt)")

    await db.client.drop_database(db.name)
    print("Cleanup OK – alle 4 Fee-Feedback-Tests grün")


asyncio.run(main())
