"""Tests Self-Tuning-Guard + Phase 4 Datensammel-Modus (lokale Mongo, kein Prod-Zugriff)."""
import asyncio
import os
import random
import sys
import time
import uuid
from datetime import datetime, timezone

sys.path.insert(0, "/app/backend")
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")
from motor.motor_asyncio import AsyncIOMotorClient


def make_candles(n=120, price=100.0):
    random.seed(11)
    out, ts = [], int(time.time() * 1000) - n * 60_000
    for i in range(n):
        o = price
        price = max(1.0, price * (1 + random.uniform(-0.002, 0.002)))
        out.append({"timestamp": ts + i * 60_000, "open": o, "close": price,
                    "high": max(o, price) * 1.001, "low": min(o, price) * 0.999,
                    "volume": random.uniform(10, 100)})
    return out


async def main():
    db = AsyncIOMotorClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"] + "_test_phase4"]
    from services.ai_engine import ai_engine, DEFAULT_AI_CONFIG

    # ---------- 1) _tuning_guard: Spanne wird erzwungen ----------
    ai_engine.config = dict(DEFAULT_AI_CONFIG)
    assert ai_engine._tuning_guard({"min_confidence": 70}) == ""
    assert ai_engine._tuning_guard({"min_confidence": 55}) == ""
    assert "außerhalb" in ai_engine._tuning_guard({"min_confidence": 85})
    assert "außerhalb" in ai_engine._tuning_guard({"min_confidence": 50})
    assert ai_engine._tuning_guard({"cooldown_min": 45}) == ""
    assert "über dem Autonomie-Limit" in ai_engine._tuning_guard({"cooldown_min": 60})
    assert ai_engine._tuning_guard({"cooldown_min": 60, "min_confidence": 70}) != ""
    print("PASS 1: _tuning_guard erzwingt Spanne (55–75) und Cooldown-Limit (45)")

    # ---------- 2) update_config: neue Keys + Spannen-Konsistenz ----------
    old_db = ai_engine.db
    ai_engine.db = db
    await ai_engine.update_config({"tune_conf_min": 60, "tune_conf_max": 80,
                                   "collection_min_confidence": 55,
                                   "collection_cooldown_min": 15,
                                   "collection_max_per_coin": 3,
                                   "collection_enabled": False})
    assert ai_engine.config["tune_conf_min"] == 60
    assert ai_engine.config["tune_conf_max"] == 80
    assert ai_engine.config["collection_min_confidence"] == 55
    assert ai_engine.config["collection_enabled"] is False
    await ai_engine.update_config({"tune_conf_min": 90, "tune_conf_max": 70})
    assert ai_engine.config["tune_conf_min"] <= ai_engine.config["tune_conf_max"]
    print("PASS 2: update_config übernimmt/klemmt neue Keys, Spanne bleibt konsistent")

    # ---------- 3) _normalize_auto_tuned: Boot-Heilung ----------
    ai_engine.config = dict(DEFAULT_AI_CONFIG)
    ai_engine.config["min_confidence"] = 85  # von der KI gesetzt (Proposal unten)
    await db.ai_proposals.delete_many({})
    await db.settings.delete_many({"_id": "ai_trader_config"})
    await db.settings.insert_one({"_id": "ai_trader_config", **ai_engine.config})
    await db.ai_proposals.insert_one({
        "id": str(uuid.uuid4()), "ts": datetime.now(timezone.utc).isoformat(),
        "scope": "engine", "symbol": "ENGINE",
        "changes": {"min_confidence": 85}, "status": "auto_applied"})
    await ai_engine._normalize_auto_tuned()
    assert ai_engine.config["min_confidence"] == 75, ai_engine.config["min_confidence"]
    doc = await db.settings.find_one({"_id": "ai_trader_config"})
    assert doc["min_confidence"] == 75
    prop = await db.ai_proposals.find_one({"scope": "engine"})
    assert prop.get("guard_normalized") is True
    # zweiter Lauf: Trader setzt manuell wieder 85 -> KEIN erneutes Zurückholen
    ai_engine.config["min_confidence"] = 85
    await ai_engine._normalize_auto_tuned()
    assert ai_engine.config["min_confidence"] == 85
    print("PASS 3: Boot-Heilung holt KI-gesetzte 85 auf 75 zurück, manuelle 85 bleibt")

    # ---------- 4) _handle_config_changes: Guard parkt out-of-range ----------
    # (Makro-Gate wird gemockt: in Prod validiert es nach genug Bestätigungen,
    #  der Self-Tuning-Guard muss DANACH als letzte Instanz greifen)
    import services.ai_engine as eng_mod
    orig_macro, orig_clamp = eng_mod.validation_gate.macro, eng_mod.validation_gate.clamp
    eng_mod.validation_gate.macro = lambda sample, confirmations: {
        "validated": True, "reason": "test", "sample": sample}
    eng_mod.validation_gate.clamp = lambda key, cur, proposed: (proposed, False)
    try:
        ai_engine.config = dict(DEFAULT_AI_CONFIG)
        ai_engine.config["autonomy"] = "auto"
        ai_engine.learning = None
        await db.ai_proposals.delete_many({})
        res = await ai_engine._handle_config_changes(
            [{"symbol": "ENGINE", "changes": {"min_confidence": 85}, "reason": "test"}])
        assert res and res[0]["status"] == "needs_confirmation", res
        assert "Autonomie-Spanne" in res[0].get("guard_reason", ""), res[0]
        assert ai_engine.config["min_confidence"] == DEFAULT_AI_CONFIG["min_confidence"]
        res2 = await ai_engine._handle_config_changes(
            [{"symbol": "ENGINE", "changes": {"min_confidence": 70}, "reason": "test"}])
        assert res2 and res2[0]["status"] == "auto_applied", res2
        assert ai_engine.config["min_confidence"] == 70
        # review_parked_proposals darf den geparkten 85er NIE auto-anwenden
        await ai_engine.review_parked_proposals()
        parked = await db.ai_proposals.find_one({"changes.min_confidence": 85})
        assert parked["status"] == "needs_confirmation", parked
        assert ai_engine.config["min_confidence"] == 70
    finally:
        eng_mod.validation_gate.macro = orig_macro
        eng_mod.validation_gate.clamp = orig_clamp
    print("PASS 4: KI-Wunsch 85 geparkt (Guard), 70 auto-angewendet, Review wendet 85 nie an")

    # ---------- 5) on_signal: Sammel-Signal wird Paper-Trade trotz Coin AUS ----------
    from core.state import autotrader
    autotrader.db = db
    autotrader.set_config({"mode": "paper", "coins": {"TESTUSDT": {"enabled": True}},
                           "strategy_coin_configs": {"ai_trader_TESTUSDT": {"mode": "off"}}})
    await db.auto_trades.delete_many({})
    await db.settings.update_one({"_id": "ai_trader_config"},
                                 {"$set": {"collection_max_per_coin": 2,
                                           "max_trades_per_coin": 1,
                                           # Fee-Wächter aus: dieser Test prüft Slots/Modi,
                                           # die Fixture-SLs (ATR) liegen unter 4× Fees
                                           "fee_guard_enabled": False}}, upsert=True)
    candles = make_candles()
    base_sig = {"symbol": "TESTUSDT", "type": "LONG",
                "entry_price": candles[-1]["close"], "strategy_id": "ai_trader",
                "strategy_name": "KI Trader", "timeframe": "1m",
                "trade_date": datetime.now(timezone.utc).date().isoformat()}
    # ohne data_collection: Coin steht auf AUS -> kein Trade
    s1 = {**base_sig, "id": f"s1_{int(time.time())}"}
    t1 = await autotrader.on_signal(s1, candles)
    assert t1 is None, "Coin AUS muss Nicht-Sammel-Signal blockieren"
    # mit data_collection: Paper-Trade trotz AUS, force_paper, Flag im Trade-Doc
    s2 = {**base_sig, "id": f"s2_{int(time.time())}", "data_collection": True,
          "force_paper": True, "collection_reason": "below_live_conf"}
    t2 = await autotrader.on_signal(s2, candles)
    assert t2, f"Sammel-Signal lieferte keinen Trade: {s2.get('_reject_reason')}"
    assert t2["mode"] == "paper"
    doc2 = await db.auto_trades.find_one({"id": t2["id"]})
    assert doc2.get("data_collection") is True
    assert doc2.get("collection_reason") == "below_live_conf"
    print("PASS 5: Sammel-Signal öffnet Paper-Trade trotz Coin AUS (data_collection=true)")

    # ---------- 6) Slot-Trennung: Sammel-Slots verbrauchen keine Live-Slots ----------
    # (Anti-Stacking gilt bewusst auch für Sammel-Trades: gleiche Richtung +
    #  gleicher TF < 30 min wird geblockt -> Gegenrichtung/anderer TF im Test)
    s3 = {**base_sig, "id": f"s3_{int(time.time())}", "type": "SHORT",
          "data_collection": True, "force_paper": True}
    t3 = await autotrader.on_signal(s3, candles)
    assert t3, f"2. Sammel-Trade (Gegenrichtung) blockiert: {s3.get('_reject_reason')}"
    s4 = {**base_sig, "id": f"s4_{int(time.time())}", "timeframe": "5m",
          "data_collection": True, "force_paper": True}
    t4 = await autotrader.on_signal(s4, candles)
    assert t4 is None and "Trade-Limit" in str(s4.get("_reject_reason")), \
        f"3. Sammel-Trade muss am collection_max_per_coin=2 scheitern: {s4.get('_reject_reason')}"
    # Live-Slot-Zählung ignoriert die 2 offenen Sammel-Trades
    autotrader.set_config({"mode": "paper", "coins": {"TESTUSDT": {"enabled": True}},
                           "strategy_coin_configs": {"ai_trader_TESTUSDT": {"mode": "paper"}}})
    s5 = {**base_sig, "id": f"s5_{int(time.time())}", "timeframe": "15m"}
    t5 = await autotrader.on_signal(s5, candles)
    assert t5, f"Live/Paper-Slot durch Sammel-Trades blockiert: {s5.get('_reject_reason')}"
    print("PASS 6: Sammel-Slots (max 2) getrennt von normalen Slots (max 1)")

    # ---------- 7) _emit_signal(collection=True): E2E über die Signal-Pipeline ----------
    await db.auto_trades.delete_many({})  # sonst blockt der Cluster-Guard (gleicher Entry)
    class _Scanner:
        candle_buffer = {"TESTUSDT": candles}
        def is_trading_session(self, _=None):
            return True
        def berlin_now(self):
            return datetime.now(timezone.utc)
        def berlin_date(self):
            return datetime.now(timezone.utc).date().isoformat()
        def get_current_session(self):
            return "24/7 Mode"

    emitted_signals = []

    async def _cb(signal):
        emitted_signals.append(signal)
        signal["id"] = str(uuid.uuid4())
        return True

    ai_engine.scanner = _Scanner()
    ai_engine.signal_cb = _cb
    ai_engine.config = dict(DEFAULT_AI_CONFIG)
    dec = {"id": str(uuid.uuid4()), "symbol": "TESTUSDT", "action": "LONG",
           "confidence": 62, "horizon": "scalp", "setup": "pullback",
           "sl_pct": 0.6, "tp1_pct": 1.0, "tpf_pct": 1.8,
           "price": candles[-1]["close"], "rsi": 50.0,
           "reasoning": "test", "ts": datetime.now(timezone.utc).isoformat()}
    ok = await ai_engine._emit_signal(dec, collection=True)
    assert ok and emitted_signals, "Sammel-Emission fehlgeschlagen"
    sig = emitted_signals[-1]
    assert sig["data_collection"] is True and sig["force_paper"] is True
    assert sig["collection_reason"] == "below_live_conf"
    # eigener Cooldown greift sofort
    ok2 = await ai_engine._emit_signal(dec, collection=True)
    assert not ok2, "collection_cooldown_min muss zweite Emission blocken"
    print("PASS 7: _emit_signal(collection) markiert Signal + eigener Cooldown greift")

    ai_engine.db = old_db
    await db.client.drop_database(db.name)
    print("Cleanup OK – alle 7 Tests grün")


asyncio.run(main())
