"""Isolierter Test für Fix 0.4: prompt_version an jeder ai_decision."""
import asyncio
import os
import random
import sys
import time

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
    from services.ai_master_prompt import master_prompt
    from services.ai_engine import (ai_engine, prompt_version_info,
                                    ANALYSIS_PROMPT_HASHES)

    # 1) version_hash: deterministisch, inhaltsbasiert, revert-sicher
    h1 = master_prompt.version_hash()
    assert h1 == master_prompt.version_hash() and len(h1) == 10, h1
    orig_text = master_prompt.text
    master_prompt.text = orig_text + "\nTESTZUSATZ 0.4"
    h2 = master_prompt.version_hash()
    assert h2 != h1, "Hash muss sich bei Text-Änderung ändern"
    master_prompt.text = orig_text
    assert master_prompt.version_hash() == h1, "Revert muss Original-Hash ergeben"
    print(f"PASS 1: version_hash deterministisch + revert-sicher ({h1})")

    # 2) prompt_version_info: Struktur + lean/full unterscheidbar
    pv_lean, pv_full = prompt_version_info("lean"), prompt_version_info("full")
    assert pv_lean["analysis"] == ANALYSIS_PROMPT_HASHES["lean"]
    assert pv_full["analysis"] == ANALYSIS_PROMPT_HASHES["full"]
    assert pv_lean["analysis"] != pv_full["analysis"]
    assert pv_lean["master"] == h1 and pv_lean["master_v"] == master_prompt.version
    assert pv_lean["combined"] == f"lean-{pv_lean['analysis']}+{h1}"
    print(f"PASS 2: prompt_version_info ok (combined={pv_lean['combined']})")

    # 3) Integration: run_analysis schreibt prompt_version in jede ai_decision
    sym = "PVTESTUSDT"
    candles = make_candles()

    class _Scanner:
        candle_buffer = {sym: candles}

        def berlin_now(self):
            from core import timeutil
            return timeutil.now_berlin()

        def is_trading_session(self, _sid):
            return False  # kein Signal-Emit noetig, wir testen nur die Decision

    canned = ('{"market_overview": "Test.", "decisions": [{"symbol": "' + sym +
              '", "action": "HOLD", "confidence": 5, "horizon": "scalp", '
              '"news_impact": "neutral", "reasoning": "Testlauf 0.4"}], '
              '"new_strategies": [], "config_changes": []}')

    async def fake_generate_json(prompt, system, **kw):
        return canned, "test-model"

    ai_engine.db = db
    ai_engine.scanner = _Scanner()
    ai_engine.toggle_check = None
    ai_engine.symbols = [sym]
    ai_engine.config["news_enabled"] = False
    ai_engine._generate_json = fake_generate_json

    async def _empty(*a, **k):
        return ""
    ai_engine._analysis_extra_blocks = _empty
    ai_engine._liquidity_block = _empty
    ai_engine._user_directives = _empty
    ai_engine._open_trades_text = _empty

    await db.strategy_coin_configs.update_one(
        {"_id": f"ai_trader_{sym}"},
        {"$set": {"config": {"mode": "paper"}}}, upsert=True)
    await db.ai_decisions.delete_many({"symbol": sym})

    res = await ai_engine.run_analysis(manual=True)
    assert res.get("status") == "ok", res

    doc = await db.ai_decisions.find_one({"symbol": sym})
    assert doc, "keine ai_decision geschrieben"
    pv = doc.get("prompt_version")
    assert pv, f"prompt_version fehlt am Decision-Doc: {sorted(doc.keys())}"
    assert pv["variant"] in ("lean", "full")
    assert pv["analysis"] == ANALYSIS_PROMPT_HASHES[pv["variant"]]
    assert pv["master"] == h1 and pv["master_v"] == master_prompt.version
    assert pv["combined"] == f"{pv['variant']}-{pv['analysis']}+{pv['master']}"
    assert doc.get("model") == "test-model"
    print(f"PASS 3: ai_decision traegt prompt_version (variant={pv['variant']}, "
          f"combined={pv['combined']})")

    await db.ai_decisions.delete_many({"symbol": sym})
    await db.strategy_coin_configs.delete_one({"_id": f"ai_trader_{sym}"})
    print("Cleanup OK")


asyncio.run(main())
