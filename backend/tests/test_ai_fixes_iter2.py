"""Regressionstests für die KI-Fixes (2. Iteration):

1. Lektionen-Dedupe: doppelte/nahezu identische Lektionen werden zusammen-
   geführt – nur die am besten VALIDIERTE bleibt; gesperrte nie entfernt.
2. Chat-Kontext 'HEUTIGE AKTIVITÄT': Der Chat bekommt die heutigen Signale
   und Trades (Bug: KI behauptete 'heute keine Signale' trotz Signalen).
"""
import asyncio
from datetime import datetime, timezone

from services.ai_lessons import dedupe_lessons, merge_lessons, LessonStore
from tests.test_position_watchdog import FakeCollection


def L(title, weight=2, confirmations=0, locked=False, updated_at="2026-01-01"):
    return {"title": title, "detail": f"detail {title}", "weight": weight,
            "confirmations": confirmations, "locked": locked,
            "updated_at": updated_at}


# --------------------------- Dedupe ---------------------------------------

def test_dedupe_keeps_most_validated_version():
    lessons = [
        L("SOL Shorts im Uptrend meiden", weight=2, confirmations=1),
        L("SOL Shorts meiden im Uptrend", weight=4, confirmations=3),
    ]
    kept, dropped = dedupe_lessons(lessons)
    assert len(kept) == 1
    assert kept[0]["weight"] == 4 and kept[0]["confirmations"] == 3
    assert dropped[0]["kept"] == "SOL Shorts meiden im Uptrend"


def test_dedupe_never_drops_locked_lessons():
    lessons = [
        L("Hebel über 20x vermeiden bei BTC", weight=4, confirmations=5),
        L("Hebel über 20x vermeiden bei BTC Trades", weight=1, locked=True),
    ]
    kept, dropped = dedupe_lessons(lessons)
    # Die gesperrte (Trader-)Lektion bleibt IMMER – trotz niedrigerem Gewicht
    # wird sie behalten und das KI-Duplikat entfernt.
    assert [l["locked"] for l in kept] == [True]
    assert dropped[0]["title"] == "Hebel über 20x vermeiden bei BTC"


def test_dedupe_leaves_distinct_lessons_alone():
    lessons = [L("Lektion 1"), L("Lektion 2"), L("Neu A"), L("Neu B")]
    kept, dropped = dedupe_lessons(lessons)
    assert len(kept) == 4 and dropped == []


def test_merge_lessons_dedupes_ai_duplicate_of_locked():
    old = [{"title": "ETH Longs nur über EMA200", "detail": "x", "weight": 2,
            "locked": True, "origin": "user", "updated_at": "2026-01-01"}]
    new = [L("ETH Longs nur über EMA200 handeln", weight=3)]
    merged = merge_lessons(old, new, [], 50)
    assert len(merged) == 1
    assert merged[0]["locked"] is True


def test_merge_lessons_existing_behavior_intact():
    old = [L(f"Regel {chr(65 + i)} beachten dringend", weight=1) for i in range(5)]
    new = [L("Völlig neues Muster erkannt", weight=3)]
    merged = merge_lessons(old, new, [], 50)
    assert len(merged) == 6


def test_audit_dedupes_stored_lessons():
    store = LessonStore()

    class _DB:
        settings = FakeCollection([{
            "_id": "ai_lessons",
            "lessons": [
                L("DOT Breakouts nachts meiden", weight=1, confirmations=0),
                L("DOT Breakouts meiden nachts", weight=3, confirmations=2),
            ],
        }])
    store.setup(_DB())
    res = asyncio.run(store.audit_against_master())
    assert len(res["removed"]) == 1
    assert "Doppelung" in res["removed"][0]["why"]
    kept = asyncio.run(store.all())
    assert len(kept) == 1 and kept[0]["weight"] == 3


# --------------------------- Chat: HEUTIGE AKTIVITÄT ----------------------

class _ChatDB:
    def __init__(self, signals, trades):
        self.signals = FakeCollection(signals)
        self.auto_trades = FakeCollection(trades)


def _engine_stub(db):
    from services.ai_engine import AIEngine
    eng = AIEngine.__new__(AIEngine)
    eng.db = db
    return eng


def test_today_block_lists_signals_and_trades():
    from services.ai_engine import AIEngine
    now = datetime.now(timezone.utc).isoformat()
    db = _ChatDB(
        signals=[{"symbol": "ADAUSDT", "type": "LONG", "timestamp": now,
                  "strategy_id": "ai_trader", "strategy_name": "KI Trader",
                  "entry_price": 0.5, "result": None},
                 {"symbol": "BTCUSDT", "type": "SHORT",
                  "timestamp": "2020-01-01T00:00:00+00:00",  # alt -> nicht heute
                  "strategy_id": "pbd_model", "entry_price": 50000}],
        trades=[{"symbol": "ADAUSDT", "side": "LONG", "mode": "live",
                 "status": "open", "opened_at": now,
                 "strategy_name": "KI Trader"},
                {"symbol": "SOLUSDT", "side": "SHORT", "mode": "paper",
                 "status": "closed", "opened_at": now, "closed_at": now,
                 "realized_pnl": 12.5}])
    eng = _engine_stub(db)
    block = asyncio.run(AIEngine._today_activity_block(eng, {"ADAUSDT"}))
    assert "HEUTIGE AKTIVITÄT" in block
    assert "Signale heute: 1 gesamt" in block
    assert "ADAUSDT LONG" in block
    assert "Trades heute eröffnet: 2 | heute geschlossen: 1" in block
    assert "+12.50 USDT" in block
    assert "BTCUSDT" not in block  # altes Signal zählt nicht als heute


def test_today_block_without_activity():
    from services.ai_engine import AIEngine
    eng = _engine_stub(_ChatDB([], []))
    block = asyncio.run(AIEngine._today_activity_block(eng, set()))
    assert "Signale heute: keine" in block
    assert "Trades heute eröffnet: 0" in block
