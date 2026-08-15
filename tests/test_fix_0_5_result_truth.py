"""Isolierter Test für Fix 0.5: Ergebnis-Wahrheit vereinheitlicht (realized_pnl-Vorzeichen)."""
import asyncio
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone

sys.path.insert(0, "/app/backend")
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")
from motor.motor_asyncio import AsyncIOMotorClient

SYM = "TRUTHUSDT"


def _sig(sid, tp1=110.0, sl=90.0):
    return {"id": sid, "symbol": SYM, "type": "LONG", "strategy_id": "ai_trader",
            "tp1": tp1, "sl": sl, "timestamp": datetime.now(timezone.utc).isoformat(),
            "result": None}


async def cleanup(db):
    await db.signals.delete_many({"symbol": SYM})
    await db.auto_trades.delete_many({"symbol": SYM})
    await db.ai_decisions.delete_many({"symbol": SYM})


async def main():
    db = AsyncIOMotorClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]]
    await cleanup(db)

    # ---- 1) Trade-Close schreibt kanonisches Ergebnis ans Signal ----------
    from core.state import autotrader
    autotrader.db = db
    sid1 = f"t5a_{uuid.uuid4().hex[:8]}"
    await db.signals.insert_one({**_sig(sid1), "result": "win",
                                 "result_source": "tp1_touch"})
    trade = {"id": f"tr_{uuid.uuid4().hex[:8]}", "symbol": SYM, "side": "LONG",
             "mode": "paper", "strategy_id": "ai_trader", "signal_id": sid1,
             "status": "closed", "result": "loss", "realized_pnl": -1.23,
             "max_capital": 100, "closed_at": datetime.now(timezone.utc).isoformat()}
    await autotrader._after_close(trade)
    s = await db.signals.find_one({"id": sid1})
    assert s["result"] == "loss" and s["result_source"] == "trade_pnl" \
        and s["trade_id"] == trade["id"], s
    print("PASS 1: _after_close -> Signal kanonisch (win/tp1 -> loss/trade_pnl)")

    # ---- 2) TP1-Touch überschreibt Trade-Wahrheit NICHT -------------------
    from core import state, pipeline
    state.db = db
    sid2 = f"t5b_{uuid.uuid4().hex[:8]}"
    await db.signals.insert_one(_sig(sid2))
    ev1 = {"id": sid1, "symbol": SYM, "type": "LONG", "tp1": 110.0, "sl": 90.0,
           "strategy_id": "ai_trader", "ts": datetime.now(timezone.utc).isoformat()}
    ev2 = {**ev1, "id": sid2}
    state.open_signal_evals[:] = [ev1, ev2]
    await pipeline.evaluate_open_signals(SYM, 111.0)  # TP1 beruehrt
    s1 = await db.signals.find_one({"id": sid1})
    s2 = await db.signals.find_one({"id": sid2})
    assert s1["result"] == "loss" and s1["result_source"] == "trade_pnl", s1
    assert s2["result"] == "win" and s2["result_source"] == "tp1_touch", s2
    assert not any(e["id"] in (sid1, sid2) for e in state.open_signal_evals)
    print("PASS 2: evaluate_open_signals respektiert trade_pnl, labelt Rest tp1_touch")

    # ---- 3) sync_outcomes: Trade-Wahrheit gewinnt an der Decision ----------
    from services.ai_learning import AILearning

    class _Eng:
        pass
    eng = _Eng()
    eng.db = db
    learn = AILearning(eng)
    dec_id = str(uuid.uuid4())
    await db.ai_decisions.insert_one({"id": dec_id, "symbol": SYM,
                                      "action": "LONG", "signal_id": sid1,
                                      "outcome": "win",
                                      "outcome_source": "tp1_touch"})
    await db.auto_trades.insert_one(dict(trade))
    await learn.sync_outcomes()
    d = await db.ai_decisions.find_one({"id": dec_id})
    assert d["outcome"] == "loss" and d["outcome_source"] == "trade_pnl" \
        and d["trade_pnl"] == -1.23, d
    # tp1-Sync darf das kanonische Outcome nicht mehr zurückstufen
    await db.signals.update_one({"id": sid1},
                                {"$set": {"result": "win",
                                          "result_source": "tp1_touch",
                                          "ai_learn_synced": False}})
    await learn.sync_outcomes()
    d2 = await db.ai_decisions.find_one({"id": dec_id})
    assert d2["outcome"] == "loss" and d2["outcome_source"] == "trade_pnl", d2
    print("PASS 3: sync_outcomes -> trade_pnl gewinnt, tp1 stuft nie zurueck")

    # ---- 4) Migrationsskript: Dry-Run ändert nichts, Apply migriert -------
    await cleanup(db)
    sid3 = f"t5c_{uuid.uuid4().hex[:8]}"
    await db.signals.insert_one({**_sig(sid3), "result": "win"})  # alt: tp1, ohne source
    await db.ai_decisions.insert_one({"id": str(uuid.uuid4()), "symbol": SYM,
                                      "signal_id": sid3, "outcome": "win"})
    await db.auto_trades.insert_one({"id": f"tr_{uuid.uuid4().hex[:8]}",
                                     "symbol": SYM, "signal_id": sid3,
                                     "status": "closed", "result": "loss",
                                     "realized_pnl": -0.5,
                                     "closed_at": datetime.now(timezone.utc).isoformat()})
    env = dict(os.environ)
    r = subprocess.run([sys.executable, "scripts/migrate_0_5_result_truth.py"],
                       cwd="/app/backend", env=env, capture_output=True, text=True)
    assert r.returncode == 0 and "DRY-RUN" in r.stdout, r.stdout + r.stderr
    s3 = await db.signals.find_one({"id": sid3})
    assert s3["result"] == "win" and "result_source" not in s3, "Dry-Run hat geschrieben!"
    r2 = subprocess.run([sys.executable, "scripts/migrate_0_5_result_truth.py",
                         "--apply"], cwd="/app/backend", env=env,
                        capture_output=True, text=True)
    assert r2.returncode == 0, r2.stdout + r2.stderr
    s3 = await db.signals.find_one({"id": sid3})
    d3 = await db.ai_decisions.find_one({"signal_id": sid3})
    assert s3["result"] == "loss" and s3["result_source"] == "trade_pnl", s3
    assert d3["outcome"] == "loss" and d3["outcome_source"] == "trade_pnl", d3
    print("PASS 4: Migration Dry-Run schreibt nichts, --apply migriert korrekt")

    # ---- 5) Prod-Schreibschutz des Skripts ---------------------------------
    r3 = subprocess.run([sys.executable, "scripts/migrate_0_5_result_truth.py",
                         "--prod", "--apply"], cwd="/app/backend", env=env,
                        capture_output=True, text=True)
    assert r3.returncode != 0 and "verboten" in (r3.stdout + r3.stderr), r3.stdout
    print("PASS 5: --prod --apply wird hart verweigert (Prod bleibt nur-lesend)")

    await cleanup(db)
    print("Cleanup OK")


asyncio.run(main())
