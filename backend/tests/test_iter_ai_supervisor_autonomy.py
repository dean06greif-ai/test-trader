"""Regressionstests für die Iteration „KI-Trader-Verbesserungen":

  * Autonomie „auto": geparkte Vorschläge werden erneut geprüft und automatisch
    angewendet, sobald die Datenlage sie bestätigt (keine Bestätigungs-Karten).
  * `actionable_proposals`: im autonomen Modus IMMER leer (Server entscheidet,
    damit im Frontend keine Karten aufblitzen können).
  * `max_lessons` bis 100 wählbar.
  * Aufsicht über das KI-Team (`services/ai_supervisor`): reine Aufbereitung.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.ai_engine import AIEngine
from services.ai_supervisor import (SUPERVISED_ROLES, evidence_text, normalize_report)


# ---------------- Fakes ----------------
class FakeCursor:
    def __init__(self, docs):
        self.docs = docs

    def sort(self, *_a, **_kw):
        return self

    def limit(self, *_a):
        return self

    async def to_list(self, n=None):
        return [dict(d) for d in self.docs[:n]] if n else [dict(d) for d in self.docs]


class FakeCollection:
    def __init__(self):
        self.docs = []

    @staticmethod
    def _match(doc, q):
        for k, v in (q or {}).items():
            if isinstance(v, dict):
                if "$in" in v and doc.get(k) not in v["$in"]:
                    return False
                if "$ne" in v and doc.get(k) == v["$ne"]:
                    return False
                if "$exists" in v:
                    cur = doc
                    for part in k.split("."):
                        cur = (cur or {}).get(part) if isinstance(cur, dict) else None
                    if bool(cur is not None) != bool(v["$exists"]):
                        return False
                if "$gte" in v and str(doc.get(k, "")) < str(v["$gte"]):
                    return False
                continue
            if doc.get(k) != v:
                return False
        return True

    async def insert_one(self, d):
        self.docs.append(dict(d))

    async def find_one(self, q, **_kw):
        for d in self.docs:
            if self._match(d, q):
                return dict(d)
        return None

    def find(self, q=None, *_a, **_kw):
        return FakeCursor([d for d in self.docs if self._match(d, q)])

    async def update_one(self, q, u, upsert=False):
        for d in self.docs:
            if self._match(d, q):
                d.update(u.get("$set", {}))
                return
        if upsert:
            nd = dict(q)
            nd.update(u.get("$set", {}))
            self.docs.append(nd)

    async def update_many(self, q, u):
        for d in self.docs:
            if self._match(d, q):
                d.update(u.get("$set", {}))

    async def count_documents(self, q):
        return len([d for d in self.docs if self._match(d, q)])

    async def replace_one(self, q, d, upsert=False):
        for i, old in enumerate(self.docs):
            if self._match(old, q):
                self.docs[i] = dict(d)
                return
        if upsert:
            self.docs.append(dict(d))


class FakeDB:
    def __init__(self):
        self.settings = FakeCollection()
        self.ai_proposals = FakeCollection()
        self.ai_chat = FakeCollection()
        self.strategy_coin_configs = FakeCollection()


class FakeLearning:
    def __init__(self, closed=50, per_symbol=25):
        self.closed, self.per_symbol = closed, per_symbol

    async def gather_stats(self):
        return {"totals": {"closed_trades": self.closed},
                "by_symbol": {s: {"trades": self.per_symbol}
                              for s in ("BTCUSDT", "ETHUSDT")}}


def _engine(autonomy, closed=50, per_symbol=25):
    e = AIEngine()
    e.db = FakeDB()
    e.symbols = ["BTCUSDT", "ETHUSDT"]
    e.config["autonomy"] = autonomy
    e.learning = FakeLearning(closed, per_symbol)
    return e


def _parked(e, status="needs_data", changes=None):
    e.db.ai_proposals.docs.append({
        "id": "p1", "scope": "coin", "symbol": "BTCUSDT", "source": "analysis",
        "changes": changes or {"sl_lookback": 12},
        "current": {"sl_lookback": 15}, "reason": "Datenlage",
        "status": status, "ts": "2026-01-01T00:00:00+00:00"})


# ---------------- Autonomie-Review ----------------
def test_parked_proposal_is_applied_when_data_arrives():
    e = _engine("auto", closed=60, per_symbol=40)
    _parked(e)
    res = asyncio.run(e.review_parked_proposals())
    assert res == {"reviewed": 1, "applied": 1}
    prop = asyncio.run(e.db.ai_proposals.find_one({"id": "p1"}))
    assert prop["status"] == "auto_applied"
    doc = asyncio.run(e.db.strategy_coin_configs.find_one({"_id": "ai_trader_BTCUSDT"}))
    assert doc["config"]["sl_lookback"] == 12
    # Der Trader wird im Feed informiert (aber muss nichts bestätigen)
    assert e.db.ai_chat.docs[0]["role"] == "config"


def test_parked_proposal_stays_parked_without_data():
    e = _engine("auto", closed=1, per_symbol=0)
    _parked(e)
    res = asyncio.run(e.review_parked_proposals())
    assert res["applied"] == 0
    prop = asyncio.run(e.db.ai_proposals.find_one({"id": "p1"}))
    assert prop["status"] == "needs_data"
    assert e.db.strategy_coin_configs.docs == []


def test_review_is_noop_in_suggest_mode():
    e = _engine("suggest", closed=60, per_symbol=40)
    _parked(e)
    assert asyncio.run(e.review_parked_proposals()) == {"reviewed": 0, "applied": 0}
    prop = asyncio.run(e.db.ai_proposals.find_one({"id": "p1"}))
    assert prop["status"] == "needs_data"   # Trader entscheidet selbst


def test_actionable_proposals_empty_in_auto_mode():
    e = _engine("auto")
    _parked(e)
    e.db.ai_proposals.docs.append({"id": "p2", "status": "pending", "scope": "coin",
                                   "symbol": "ETHUSDT", "changes": {"leverage": 5},
                                   "ts": "2026-01-01T00:00:00+00:00"})
    assert asyncio.run(e.actionable_proposals()) == []


def test_actionable_proposals_in_suggest_mode():
    e = _engine("suggest")
    _parked(e, status="needs_confirmation")
    rows = asyncio.run(e.actionable_proposals())
    assert [r["id"] for r in rows] == ["p1"]


def test_max_lessons_up_to_100():
    e = _engine("suggest")
    e.db.settings.docs.append({"_id": "ai_trader_config"})
    cfg = asyncio.run(e.update_config({"max_lessons": 100}))
    assert cfg["max_lessons"] == 100
    assert asyncio.run(e.update_config({"max_lessons": 500}))["max_lessons"] == 100
    assert asyncio.run(e.update_config({"max_lessons": 1}))["max_lessons"] == 3


# ---------------- Aufsicht über das KI-Team ----------------
def test_supervisor_roles_cover_team():
    for role in ("analyst", "research_analyst", "trade_manager", "learner"):
        assert role in SUPERVISED_ROLES


def test_evidence_text_contains_roles_and_samples():
    txt = evidence_text({
        "analyst": {"model": "gemini/x", "last_run": "2026-01-01T00:00",
                    "metrics": {"intervall_min": 10},
                    "samples": [{"ts": "2026-01-01T00:00", "text": "BTC long weil ..."}]},
        "chat": {"model": "groq/y", "samples": []},
    })
    assert "ROLLE: analyst" in txt and "BTC long" in txt
    assert "(keine Ausgaben im Zeitfenster)" in txt


def test_normalize_report_drops_invalid_model_and_role():
    allowed = {"gemini": ["gemini-3.5-flash"]}
    rep = normalize_report({
        "summary": "ok",
        "roles": [
            {"role": "analyst", "verdict": "schwach", "score": 20,
             "reason": "viele Fehler", "action": "modell_wechseln",
             "suggested_provider": "gemini", "suggested_model": "gemini-3.5-flash"},
            {"role": "analyst2", "verdict": "gut"},
            {"role": "chat", "verdict": "banane", "action": "modell_wechseln",
             "suggested_provider": "gemini", "suggested_model": "gibt-es-nicht"},
        ],
        "recommendations": ["Modell für den Analyst wechseln"],
    }, allowed)
    assert [r["role"] for r in rep["roles"]] == ["analyst", "chat"]
    assert rep["roles"][0]["suggested_model"] == "gemini-3.5-flash"
    # unbekanntes Modell -> keine Empfehlung, Aktion abgeschwächt
    assert rep["roles"][1]["suggested_model"] is None
    assert rep["roles"][1]["action"] == "einstellungen_pruefen"
    assert rep["roles"][1]["verdict"] == "gut"      # ungültiges verdict normalisiert
    assert rep["recommendations"] == ["Modell für den Analyst wechseln"]
