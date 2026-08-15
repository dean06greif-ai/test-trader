"""Fix 0.6: Rebuild-on-Boot Candle-Backfill + Downtime-Signal-Labeling.

Testet die pure Label-Logik (label_from_candles), das DB-Labeling mit
Vorrang der Trade-Wahrheit (result_source=trade_pnl) und den Audit-Log.
Läuft gegen die lokale Mongo (MONGO_URL) – niemals gegen PROD.
"""
import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.boot_backfill import label_from_candles, _label_downtime_signals, _ts_to_ms  # noqa: E402
from services.candles import CandleArray  # noqa: E402
from core import state  # noqa: E402


def _mk_candles(rows):
    """rows: list of (ts_ms, high, low)"""
    ts = [r[0] for r in rows]
    hi = [r[1] for r in rows]
    lo = [r[2] for r in rows]
    mid = [(h + l) / 2 for h, l in zip(hi, lo)]
    return CandleArray(np.array(ts), np.array(mid), np.array(hi),
                       np.array(lo), np.array(mid), np.zeros(len(ts)))


def test_label_long_win_before_sl():
    ev = {"type": "LONG", "tp1": 110.0, "sl": 90.0}
    candles = _mk_candles([(1000, 105, 99), (2000, 111, 100), (3000, 95, 85)])
    result, amb = label_from_candles(ev, candles, after_ms=500)
    assert result == "win" and amb is False


def test_label_long_loss_before_tp():
    ev = {"type": "LONG", "tp1": 110.0, "sl": 90.0}
    candles = _mk_candles([(1000, 105, 99), (2000, 106, 89), (3000, 115, 100)])
    result, amb = label_from_candles(ev, candles, after_ms=500)
    assert result == "loss" and amb is False


def test_label_short_win():
    ev = {"type": "SHORT", "tp1": 90.0, "sl": 110.0}
    candles = _mk_candles([(1000, 100, 95), (2000, 96, 89)])
    result, amb = label_from_candles(ev, candles, after_ms=500)
    assert result == "win" and amb is False


def test_label_ambiguous_same_candle_is_conservative_loss():
    ev = {"type": "LONG", "tp1": 110.0, "sl": 90.0}
    candles = _mk_candles([(1000, 112, 88)])  # beide Level in einer Kerze
    result, amb = label_from_candles(ev, candles, after_ms=500)
    assert result == "loss" and amb is True


def test_label_none_when_no_level_hit():
    ev = {"type": "LONG", "tp1": 110.0, "sl": 90.0}
    candles = _mk_candles([(1000, 105, 95), (2000, 107, 96)])
    result, amb = label_from_candles(ev, candles, after_ms=500)
    assert result is None


def test_label_ignores_candles_before_signal():
    ev = {"type": "LONG", "tp1": 110.0, "sl": 90.0}
    # TP-Berührung liegt VOR dem Signal-Zeitpunkt -> zählt nicht
    candles = _mk_candles([(1000, 115, 100), (5000, 105, 95)])
    result, amb = label_from_candles(ev, candles, after_ms=2000)
    assert result is None


def test_ts_to_ms():
    iso = "2026-08-14T10:00:00+00:00"
    assert _ts_to_ms(iso) == int(datetime.fromisoformat(iso).timestamp() * 1000)
    assert _ts_to_ms(None) is None
    assert _ts_to_ms("kein-datum") is None


def test_downtime_labeling_db_and_trade_pnl_priority():
    asyncio.run(_run_downtime_labeling_check())


async def _run_downtime_labeling_check():
    """E2E gegen lokale Mongo: unlabeltes Signal wird gelabelt,
    trade_pnl-gelabeltes Signal bleibt unangetastet."""
    from motor.motor_asyncio import AsyncIOMotorClient
    client = AsyncIOMotorClient(os.environ.get("MONGO_URL", "mongodb://localhost:27017"))
    db = client[os.environ.get("DB_NAME", "crypto_scanner")]
    old_db, old_evals = state.db, list(state.open_signal_evals)
    state.db = db
    now = datetime.now(timezone.utc)
    sig_ts = (now - timedelta(hours=2)).isoformat()
    sid1, sid2 = str(uuid.uuid4()), str(uuid.uuid4())
    try:
        await db.signals.insert_many([
            {"id": sid1, "symbol": "TESTUSDT", "type": "LONG", "tp1": 110.0,
             "sl": 90.0, "strategy_id": "t", "timestamp": sig_ts},
            {"id": sid2, "symbol": "TESTUSDT", "type": "LONG", "tp1": 110.0,
             "sl": 90.0, "strategy_id": "t", "timestamp": sig_ts,
             "result": "loss", "result_source": "trade_pnl"},
        ])
        state.open_signal_evals[:] = [
            {"id": sid1, "symbol": "TESTUSDT", "type": "LONG", "tp1": 110.0,
             "sl": 90.0, "strategy_id": "t", "ts": sig_ts},
            {"id": sid2, "symbol": "TESTUSDT", "type": "LONG", "tp1": 110.0,
             "sl": 90.0, "strategy_id": "t", "ts": sig_ts},
        ]
        base = int((now - timedelta(hours=1)).timestamp() * 1000)
        candles = _mk_candles([(base, 105, 99), (base + 60000, 111, 100)])
        out = await _label_downtime_signals("TESTUSDT", candles)
        assert out["win"] == 1  # nur sid1 (sid2 matched nicht -> kein Doppel-Count)
        s1 = await db.signals.find_one({"id": sid1})
        assert s1["result"] == "win"
        assert s1["result_source"] == "tp1_touch"
        assert s1.get("result_backfilled") is True
        s2 = await db.signals.find_one({"id": sid2})
        assert s2["result"] == "loss"                    # unverändert
        assert s2["result_source"] == "trade_pnl"        # Wahrheit bleibt
        # beide Evals aus dem Live-Tracking entfernt
        assert not [e for e in state.open_signal_evals if e["id"] in (sid1, sid2)]
    finally:
        await db.signals.delete_many({"id": {"$in": [sid1, sid2]}})
        await db.performance.delete_one({"symbol": "TESTUSDT"})
        state.db = old_db
        state.open_signal_evals[:] = old_evals
        client.close()


def test_audit_log_written():
    asyncio.run(_run_audit_check())


async def _run_audit_check():
    from motor.motor_asyncio import AsyncIOMotorClient
    from core.audit import log_action
    client = AsyncIOMotorClient(os.environ.get("MONGO_URL", "mongodb://localhost:27017"))
    db = client[os.environ.get("DB_NAME", "crypto_scanner")]
    old_db = state.db
    state.db = db
    try:
        await log_action(None, "test_audit_action", {"foo": 1})
        doc = await db.audit_log.find_one({"action": "test_audit_action"})
        assert doc is not None
        assert doc["user"]
        assert doc["details"] == {"foo": 1}
    finally:
        await db.audit_log.delete_many({"action": "test_audit_action"})
        state.db = old_db
        client.close()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
