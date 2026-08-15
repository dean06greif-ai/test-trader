"""Regressionstests für die Fixes aus Iteration 4 (Meldung des Traders).

  * Lektionen-Migration: Alt-Bestand ohne `id` bekommt eine – ohne id konnte die
    Lernen-Ansicht abstürzen und Bearbeiten/Löschen ging nicht.
  * Analyse-Zeitplan: `update_config` normalisiert die Fenster, `current_interval`
    liefert automatisch das passende Intervall, ein kürzeres greift sofort.
"""
import asyncio
import time
from datetime import datetime

from services import ai_lessons


class _Coll:
    def __init__(self, doc=None):
        self.doc = doc
        self.saved = []

    async def find_one(self, q):
        return dict(self.doc) if self.doc else None

    async def update_one(self, q, upd, upsert=False):
        self.saved.append(upd)
        self.doc = {**(self.doc or {}), **(upd.get("$set") or {})}


class _DB:
    def __init__(self, doc=None):
        self.settings = _Coll(doc)


def test_legacy_lessons_get_ids_on_load():
    from services.ai_learning import AILearning
    from services.ai_lessons import lesson_store

    legacy = [{"title": f"Alt {i}", "detail": "x", "weight": 3} for i in range(3)]
    db = _DB({"_id": "ai_lessons", "lessons": legacy, "updated_at": "2026-06-01T10:00:00+00:00"})

    class _Engine:
        def __init__(self, db):
            self.db = db

    learning = AILearning(_Engine(db))
    lesson_store.db = db
    asyncio.run(learning.load_state())
    lessons = asyncio.run(learning.get_lessons())
    assert len(lessons) == 3
    assert all(l["id"].startswith("les_") for l in lessons)
    assert all(l["locked"] is False and l["origin"] == "ai" for l in lessons)
    # Migration wurde persistiert
    assert any("lessons" in (u.get("$set") or {}) for u in db.settings.saved)


def test_normalized_lessons_are_stable_on_second_load():
    lessons = ai_lessons.normalize_all([{"title": "A", "detail": "x"}])
    again = ai_lessons.normalize_all(lessons)
    assert again[0]["id"] == lessons[0]["id"]


def test_schedule_is_applied_and_shorter_interval_takes_effect_now():
    from services.ai_engine import AIEngine

    class _FakeScanner:
        settings = {}

        @staticmethod
        def berlin_now():
            return datetime(2026, 6, 30, 16, 30)   # 16:30 -> US-Fenster

    class _Settings:
        async def update_one(self, *a, **kw):
            return None

    class _FakeDB:
        settings = _Settings()

    e = AIEngine()
    e.db = _FakeDB()
    e.scanner = _FakeScanner()
    e._next_due = time.time() + 3600      # eigentlich erst in einer Stunde
    cfg = asyncio.run(e.update_config({
        "interval_min": 20,
        "schedule": [{"from": "22:00", "to": "06:00", "interval_min": 30, "label": "Nacht"},
                     {"from": "15:00", "to": "18:00", "interval_min": 5, "label": "US"},
                     {"from": "99:00", "to": "06:00", "interval_min": 5}],   # ungültig
    }))
    assert len(cfg["schedule"]) == 2                      # ungültiges Fenster verworfen
    assert e.current_interval() == (5, "US")               # automatisch nach Uhrzeit
    assert e._next_due <= time.time() + 5 * 60 + 1         # kürzeres Intervall greift sofort


def test_schedule_falls_back_to_default_outside_windows():
    from services.ai_engine import AIEngine

    class _FakeScanner:
        @staticmethod
        def berlin_now():
            return datetime(2026, 6, 30, 12, 0)

    e = AIEngine()
    e.scanner = _FakeScanner()
    e.config["interval_min"] = 20
    e.config["schedule"] = [{"from": "15:00", "to": "18:00", "interval_min": 5}]
    assert e.current_interval() == (20, "Standard")
