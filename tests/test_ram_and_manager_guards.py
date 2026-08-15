"""Fix 0.8/OOM: RAM-Projektionen im ML-Labor + Trade-Manager-Guards
(Sammel-Trades unangetastet, SL-Ratchet-Mindestabstand)."""
import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone

import pytest

sys.path.insert(0, "/app/backend")
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test_database")

from services.ai_trade_manager import min_sl_gap, trade_manager  # noqa: E402
from services.ai_ml_lab import ml_lab, MLLab, feature_row, label_of  # noqa: E402


# ---------------- min_sl_gap (rein) ----------------

def test_min_sl_gap_uses_initial_sl_distance():
    # max(30% der SL-Distanz 2.19495 = 0.6585, 0.1% vom Kurs = 0.73166) = 0.73166
    gap = min_sl_gap(731.65, 733.84495, 731.66)
    assert abs(gap - 0.73166) < 1e-4
    # Weiter SL (2% Distanz): 30%-Anteil dominiert den Floor
    gap2 = min_sl_gap(100.0, 98.0, 100.0)
    assert abs(gap2 - 0.6) < 1e-9


def test_min_sl_gap_floor_without_initial_sl():
    gap = min_sl_gap(None, None, 1000.0)
    assert abs(gap - 1.0) < 1e-9  # 0.1% vom Kurs


def test_min_sl_gap_invalid_mark():
    assert min_sl_gap(100, 99, 0) == 0.0
    assert min_sl_gap(100, 99, None) == 0.0


# ---------------- DB-Fixtures ----------------

@pytest.fixture()
def db():
    from motor.motor_asyncio import AsyncIOMotorClient
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    return client[os.environ["DB_NAME"]]


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class _FakeEngine:
    def __init__(self, db):
        self._db = db
        self.config = {"enabled": False}
        self.key = None

    @property
    def db(self):
        return self._db


def _trade_doc(**kw):
    doc = {
        "id": f"T-{uuid.uuid4().hex[:8]}", "symbol": "QQQUSDT", "side": "SHORT",
        "mode": "paper", "status": "open", "entry": 731.65, "sl": 733.84495,
        "initial_sl": 733.84495, "tp1": 725.79, "tpf": 719.94, "qty": 7.59,
        "qty_remaining": 7.59, "leverage": 25.0, "margin_used": 222.24,
        "strategy_id": "ai_trader", "ai_actions": 0, "ai_last_action_ts": 0,
        "opened_at": datetime.now(timezone.utc).isoformat(),
    }
    doc.update(kw)
    return doc


# ---------------- Sammel-Trades unangetastet ----------------

def test_ki_blocked_on_collection_trade(db):
    async def run():
        engine = _FakeEngine(db)
        trade_manager.engine = engine
        t = _trade_doc(data_collection=True)
        await db.auto_trades.insert_one(dict(t))
        try:
            res = await trade_manager.apply_action(t["id"], "adjust_sl",
                                                   value=732.0, source="ki")
            assert res["status"] == "blocked"
            assert "Datensammel" in res["detail"]
        finally:
            await db.auto_trades.delete_one({"id": t["id"]})
    _run(run())


def test_review_query_excludes_collection_trades(db):
    async def run():
        t_dc = _trade_doc(data_collection=True)
        t_norm = _trade_doc()
        await db.auto_trades.insert_many([dict(t_dc), dict(t_norm)])
        try:
            found = await db.auto_trades.find(
                {"status": "open", "manual_trade": {"$ne": True},
                 "external_adopted": {"$ne": True},
                 "strategy_id": {"$ne": "external"},
                 "data_collection": {"$ne": True}}).to_list(100)
            ids = {t["id"] for t in found}
            assert t_norm["id"] in ids
            assert t_dc["id"] not in ids
        finally:
            await db.auto_trades.delete_many({"id": {"$in": [t_dc["id"], t_norm["id"]]}})
    _run(run())


# ---------------- SL-Ratchet-Guard ----------------

class _FakeAutotrader:
    def __init__(self, mark):
        self._mark = mark
        self.adjusted = []

    async def _current_mark(self, symbol):
        return self._mark

    async def adjust_levels(self, trade_id, sl=None, tp1=None, tpf=None):
        self.adjusted.append(sl)
        return {"ok": True, "sl": sl}


def test_sl_ratchet_blocked_too_close(db):
    async def run():
        engine = _FakeEngine(db)
        trade_manager.engine = engine
        trade_manager.autotrader = _FakeAutotrader(731.66)
        # SHORT: SL bereits eng bei 731.8, KI will 731.7 (Gap 0.04 < 0.658)
        t = _trade_doc(sl=731.8)
        await db.auto_trades.insert_one(dict(t))
        try:
            res = await trade_manager.apply_action(t["id"], "adjust_sl",
                                                   value=731.7, source="ki")
            assert res["status"] == "blocked", res
            assert "Mindestabstand" in res["detail"]
        finally:
            await db.auto_trades.delete_one({"id": t["id"]})
    _run(run())


def test_sl_adjust_allowed_with_enough_gap(db):
    async def run():
        engine = _FakeEngine(db)
        trade_manager.engine = engine
        fake = _FakeAutotrader(731.66)
        trade_manager.autotrader = fake
        # SHORT: KI zieht SL von 733.84 auf 732.5 (Gap 0.84 > 0.658) -> erlaubt
        t = _trade_doc()
        await db.auto_trades.insert_one(dict(t))
        try:
            res = await trade_manager.apply_action(t["id"], "adjust_sl",
                                                   value=732.5, source="ki")
            assert res["status"] == "ok", res
            assert fake.adjusted == [732.5]
        finally:
            await db.auto_trades.delete_one({"id": t["id"]})
    _run(run())


def test_sl_manual_not_gap_restricted(db):
    async def run():
        engine = _FakeEngine(db)
        trade_manager.engine = engine
        fake = _FakeAutotrader(731.66)
        trade_manager.autotrader = fake
        t = _trade_doc(sl=733.0)
        await db.auto_trades.insert_one(dict(t))
        try:
            res = await trade_manager.apply_action(t["id"], "adjust_sl",
                                                   value=731.7, source="user",
                                                   enforce_limits=False)
            assert res["status"] == "ok", res
        finally:
            await db.auto_trades.delete_one({"id": t["id"]})
    _run(run())


# ---------------- ML-Labor Projektionen ----------------

def test_ml_lab_projection_dataset_identical(db):
    async def run():
        marker = f"proj-{uuid.uuid4().hex[:8]}"
        now = datetime.now(timezone.utc).isoformat()
        dec = {"id": marker, "ts": now, "action": "LONG", "outcome": "win",
               "confidence": 72, "sl_pct": 0.8, "tp1_pct": 1.2, "rsi": 55,
               "news_impact": "positive", "symbol": "BTCUSDT",
               # Ballast-Felder, die die Projektion wegfiltern muss:
               "reasoning": "x" * 5000, "entry_market_snapshot": {"features": {"rsi": 55}},
               "prompt_version": {"combined": "lean-abc"}}
        snap = {"symbol": "BTCUSDT", "ts": now,
                "features": {"rsi": 51.0, "trend_pct": 0.5, "atr_pct": 0.2,
                             "volatility_pct": 0.3, "volume_ratio": 1.2,
                             "range_pos": 60.0, "change_60m_pct": 0.4},
                "ballast": "y" * 2000}
        await db.ai_decisions.insert_one(dict(dec))
        await db.ai_market_snapshots.insert_one(dict(snap))
        lab = MLLab()
        lab.engine = _FakeEngine(db)
        lab.setup(lab.engine)
        try:
            X, y, meta = await lab.load_training_data()
            rows = [x for x in X if abs(x["confidence"] - 72) < 1e-9
                    and x["sl_pct"] == 0.8]
            assert rows, "projizierte Decision fehlt im Dataset"
            row = rows[0]
            expected = feature_row(dec, snap)
            assert row == expected, (row, expected)
            assert label_of(dec) == 1
        finally:
            await db.ai_decisions.delete_one({"id": marker})
            await db.ai_market_snapshots.delete_one({"ballast": snap["ballast"],
                                                     "ts": now})
    _run(run())
