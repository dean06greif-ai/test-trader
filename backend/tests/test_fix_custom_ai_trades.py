"""Regressionstests: Fix für Custom-KI-Strategien / Master-Kill-Switch.

Abgedeckte Bugs (Nutzer-Report):
  1. "Stop All Trades" (Masterpanel) hat neue Trades NICHT blockiert –
     jetzt zentraler Guard in AutoTradeManager.on_signal.
  2. Der KI-Trade-Manager eröffnete Custom-Trades ("KI Trader (Custom)")
     mit 10er Hebel weiter, obwohl der KI Trader deaktiviert war.
  3. Abgelehnte Strategie-Kandidaten blieben als Custom-Strategie registriert
     und ihre offenen Trades liefen weiter.
  4. Kein endgültiges Löschen von Kandidaten möglich.

Alle Tests laufen ohne DB, LLM oder Netzwerk (Stubs wie in test_live_close_fix).
"""
import asyncio

from core.state import control_state, autotrader
from services.ai_trade_manager import AITradeManager, TRADE_MANAGER_SYSTEM
from services.ai_strategy_lab import StrategyLab


# ---------------- Stubs ----------------
class FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, *a, **kw):
        return self

    def limit(self, *a, **kw):
        return self

    async def to_list(self, *a, **kw):
        return [dict(d) for d in self._docs]

    def __aiter__(self):
        self._it = iter([dict(d) for d in self._docs])
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


class FakeCollection:
    def __init__(self, docs=None):
        self.docs = [dict(d) for d in (docs or [])]
        self.deleted = []
        self.inserted = []

    def _match(self, d, q):
        for k, v in q.items():
            if isinstance(v, dict) and "$ne" in v:
                if d.get(k) == v["$ne"]:
                    return False
            elif isinstance(v, dict) and "$in" in v:
                if d.get(k) not in v["$in"]:
                    return False
            elif d.get(k) != v:
                return False
        return True

    async def find_one(self, q, *a, **kw):
        for d in self.docs:
            if self._match(d, q):
                return dict(d)
        return None

    def find(self, q=None, *a, **kw):
        q = q or {}
        return FakeCursor([d for d in self.docs if self._match(d, q)])

    async def update_one(self, q, upd, **kw):
        for d in self.docs:
            if self._match(d, q):
                d.update(upd.get("$set", {}))
                return
        if kw.get("upsert"):
            new = dict(q)
            new.update(upd.get("$set", {}))
            self.docs.append(new)

    async def insert_one(self, doc):
        self.inserted.append(dict(doc))
        self.docs.append(dict(doc))

    async def delete_one(self, q):
        for d in list(self.docs):
            if self._match(d, q):
                self.docs.remove(d)
                self.deleted.append(d)
                return

    async def delete_many(self, q):
        for d in list(self.docs):
            if self._match(d, q):
                self.docs.remove(d)
                self.deleted.append(d)

    async def count_documents(self, q):
        return len([d for d in self.docs if self._match(d, q)])


class FakeDB:
    def __init__(self, **named):
        self._named = named

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._named.setdefault(name, FakeCollection())

    def __getitem__(self, name):
        return self._named.setdefault(name, FakeCollection())


class ScannerStub:
    def __init__(self):
        self.settings = {"enabled_strategies": ["custom_abc123", "scalping_4_rules"]}

    def update_settings(self, updates):
        self.settings.update(updates)

    def current_price(self, symbol):
        return 100.0


class AutotraderStub:
    def __init__(self):
        self.closed = []

    async def manual_close(self, trade_id, price):
        self.closed.append((trade_id, price))
        return {"status": "closed", "id": trade_id}

    async def _current_mark(self, symbol):
        return 100.0


class EngineStub:
    def __init__(self, enabled=True, db=None):
        self.config = {"enabled": enabled}
        self.symbols = ["BTCUSDT"]
        self.key = "test-key"
        self.db = db

    class _Scanner:
        candle_buffer = {"BTCUSDT": [{"close": 100.0}]}

        def berlin_now(self):
            import datetime
            return datetime.datetime.now()

        def berlin_date(self):
            return "2026-06-01"

        def get_current_session(self):
            return "eu"

    scanner = _Scanner()


# ---------------- 1. Master-Kill-Switch blockiert neue Trades ----------------
def test_on_signal_blocked_by_master_kill_switch():
    control_state["trades_paused"] = True
    try:
        res = asyncio.run(autotrader.on_signal(
            {"symbol": "BTCUSDT", "type": "LONG", "strategy_id": "ai_trader"}, []))
        assert res is None
    finally:
        control_state["trades_paused"] = False


def test_trade_manager_open_blocked_by_master_kill_switch():
    tm = AITradeManager()
    control_state["trades_paused"] = True
    try:
        res = asyncio.run(tm.open_trade(
            {"symbol": "BTCUSDT", "side": "LONG"}, source="manuell"))
        assert res["status"] == "blocked"
        assert "Stop All Trades" in res["detail"]
    finally:
        control_state["trades_paused"] = False


# ---------------- 2. Trade-Manager respektiert KI-Trader-Schalter ----------------
def test_trade_manager_open_blocked_when_engine_disabled():
    tm = AITradeManager()
    tm.engine = EngineStub(enabled=False)
    res = asyncio.run(tm.open_trade({"symbol": "BTCUSDT", "side": "LONG"}, source="ki"))
    assert res["status"] == "blocked"
    assert "deaktiviert" in res["detail"]


def test_trade_manager_tick_skips_when_engine_disabled():
    tm = AITradeManager()
    db = FakeDB(auto_trades=FakeCollection([{"status": "open", "id": "t1"}]))
    tm.engine = EngineStub(enabled=False, db=db)
    called = []

    async def fake_review(*a, **kw):
        called.append(1)
        return {"status": "ok"}

    tm.review = fake_review
    tm._next_due = 0.0
    asyncio.run(tm.tick())
    assert not called, "tick darf bei deaktiviertem KI Trader kein Review starten"


def test_trade_manager_tick_runs_when_engine_enabled():
    tm = AITradeManager()
    db = FakeDB(auto_trades=FakeCollection([{"status": "open", "id": "t1"}]))
    tm.engine = EngineStub(enabled=True, db=db)
    called = []

    async def fake_review(*a, **kw):
        called.append(1)
        return {"status": "ok"}

    tm.review = fake_review
    tm._next_due = 0.0
    asyncio.run(tm.tick())
    assert called


# ---------------- 3. Hebel-Begrenzung + Prompt ohne 10x-Anker ----------------
def test_prompt_example_has_no_fixed_10x_leverage():
    assert '"leverage": 10' not in TRADE_MANAGER_SYSTEM
    assert '"leverage": null' in TRADE_MANAGER_SYSTEM


def test_open_trade_clamps_leverage_to_manager_limit(monkeypatch):
    tm = AITradeManager()
    tm.engine = EngineStub(enabled=True)
    tm.autotrader = AutotraderStub()
    captured = {}

    async def fake_signal_cb(signal):
        captured.update(signal)
        signal["_trade_opened"] = True
        return True

    tm.engine.signal_cb = fake_signal_cb

    async def noop_remember(*a, **kw):
        return None

    from services import ai_memory
    monkeypatch.setattr(ai_memory.memory, "remember", noop_remember)

    res = asyncio.run(tm.open_trade(
        {"symbol": "BTCUSDT", "side": "LONG", "leverage": 200}, source="manuell"))
    assert res["status"] == "ok"
    assert captured["ai_leverage"] <= float(tm.settings["max_leverage"])


# ---------------- 4. Ablehnen deregistriert + schließt Trades ----------------
def _lab_with_candidate(stage="live", sid="custom_abc123"):
    lab = StrategyLab()
    cand = {"id": "cand_test1", "name": "Test-Strategie", "stage": stage,
            "custom_strategy_id": sid, "stats": {}}
    db = FakeDB(
        ai_strategy_candidates=FakeCollection([cand]),
        custom_strategies=FakeCollection([{"id": sid, "name": "KI-Kandidat: Test"}]),
        auto_trades=FakeCollection([
            {"id": "tr1", "symbol": "BTCUSDT", "status": "open",
             "ai_candidate_id": "cand_test1", "entry": 100.0},
            {"id": "tr2", "symbol": "ETHUSDT", "status": "open",
             "ai_candidate_id": "andere", "entry": 100.0},
        ]),
    )

    class Eng:
        def __init__(self, db):
            self.db = db

    lab.engine = Eng(db)
    return lab, db


def test_reject_deregisters_strategy_and_closes_trades(monkeypatch):
    lab, db = _lab_with_candidate()
    at = AutotraderStub()
    sc = ScannerStub()
    monkeypatch.setattr("core.state.autotrader", at)
    monkeypatch.setattr("core.state.scanner", sc)

    res = asyncio.run(lab.decide("cand_test1", "reject", note="weg damit"))
    assert res["status"] == "ok"
    assert res["candidate"]["stage"] == "rejected"
    # Custom-Strategie aus DB entfernt
    assert asyncio.run(db.custom_strategies.find_one({"id": "custom_abc123"})) is None
    # aus enabled_strategies entfernt
    assert "custom_abc123" not in sc.settings["enabled_strategies"]
    # NUR der offene Trade des Kandidaten wurde geschlossen
    assert [t[0] for t in at.closed] == ["tr1"]
    assert res["closed_trades"] == 1


def test_delete_candidate_removes_everything(monkeypatch):
    lab, db = _lab_with_candidate(stage="rejected")
    db._named["ai_ghost_trades"] = FakeCollection(
        [{"id": "g1", "candidate_id": "cand_test1"}])
    at = AutotraderStub()
    monkeypatch.setattr("core.state.autotrader", at)
    monkeypatch.setattr("core.state.scanner", ScannerStub())

    res = asyncio.run(lab.delete_candidate("cand_test1"))
    assert res["status"] == "ok"
    assert asyncio.run(db.ai_strategy_candidates.find_one({"id": "cand_test1"})) is None
    assert asyncio.run(db.ai_ghost_trades.find_one({"candidate_id": "cand_test1"})) is None
    assert [t[0] for t in at.closed] == ["tr1"]


def test_register_for_testing_blocked_for_rejected():
    lab, _db = _lab_with_candidate(stage="rejected")
    res = asyncio.run(lab.register_for_testing("cand_test1"))
    assert res["status"] == "blocked"


def test_open_trade_reports_guard_rejection(monkeypatch):
    """BUGFIX: 'ok' obwohl der Guard den Trade still verworfen hat -> jetzt
    kommt 'rejected' mit dem Ablehnungsgrund zurück."""
    tm = AITradeManager()
    tm.engine = EngineStub(enabled=True)
    tm.autotrader = AutotraderStub()

    async def fake_signal_cb(signal):
        # Pipeline hat das Signal gespeichert, aber der Guard hat den Trade verworfen
        signal["_trade_opened"] = False
        signal["_reject_reason"] = "Anti-Stacking: LONG BTCUSDT noch 12 min gesperrt"
        return True

    tm.engine.signal_cb = fake_signal_cb
    res = asyncio.run(tm.open_trade(
        {"symbol": "BTCUSDT", "side": "LONG"}, source="manuell"))
    assert res["status"] == "rejected"
    assert "Anti-Stacking" in res["detail"]


# ---------------- 5. Bitunix-Sync: extern geschlossene Live-Positionen ----------------
class BitunixClientStub:
    def __init__(self, positions):
        self._positions = positions

    def configured(self):
        return True

    async def get_positions(self, symbol=None):
        return {"code": 0, "data": self._positions.get(symbol, [])}


def _autotrader_with(trades, positions):
    from services.bitunix_trade import AutoTradeManager
    at = AutoTradeManager(BitunixClientStub(positions))
    at.db = FakeDB(auto_trades=FakeCollection(trades))

    async def mark(symbol):
        return 100.0

    async def after_close(t):
        after_close.calls.append(t["id"])

    after_close.calls = []
    at._current_mark = mark
    at._after_close = after_close
    return at, after_close


def test_sync_closes_externally_closed_live_position():
    trades = [{"id": "lt1", "symbol": "BTCUSDT", "side": "LONG", "status": "open",
               "mode": "live", "entry": 90.0, "qty": 1.0, "qty_remaining": 1.0}]
    at, hook = _autotrader_with(trades, {"BTCUSDT": []})  # Börse: keine Position mehr
    synced = asyncio.run(at.sync_live_positions())
    assert synced == 1
    doc = asyncio.run(at.db.auto_trades.find_one({"id": "lt1"}))
    assert doc["status"] == "closed"
    assert doc["closed_by"] == "bitunix_sync"
    assert doc["result"] == "win"  # Entry 90 -> Exit 100
    assert hook.calls == ["lt1"]


def test_sync_keeps_position_that_is_still_open_on_bitunix():
    trades = [{"id": "lt2", "symbol": "BTCUSDT", "side": "SHORT", "status": "open",
               "mode": "live", "entry": 90.0, "qty": 1.0, "qty_remaining": 1.0}]
    at, _hook = _autotrader_with(trades, {"BTCUSDT": [
        {"side": "SELL", "positionSide": "SHORT", "qty": "1.0"}]})
    synced = asyncio.run(at.sync_live_positions())
    assert synced == 0
    doc = asyncio.run(at.db.auto_trades.find_one({"id": "lt2"}))
    assert doc["status"] == "open"


def test_sync_ignores_paper_trades():
    trades = [{"id": "pt1", "symbol": "BTCUSDT", "side": "LONG", "status": "open",
               "mode": "paper", "entry": 90.0, "qty": 1.0, "qty_remaining": 1.0}]
    at, _hook = _autotrader_with(trades, {"BTCUSDT": []})
    synced = asyncio.run(at.sync_live_positions())
    assert synced == 0


# ---------------- 6. API-Routen vorhanden ----------------
def test_delete_candidate_route_registered():
    from routers.ai_governance import router
    assert any(getattr(r, "path", "") == "/api/ai/strategies/{cid}"
               and "DELETE" in getattr(r, "methods", set()) for r in router.routes)


def test_control_stop_trades_route_still_exists():
    from routers.control import router
    assert any(getattr(r, "path", "") == "/api/control/stop-trades"
               for r in router.routes)


def test_sync_bitunix_route_registered():
    from routers.autotrade import router
    assert any(getattr(r, "path", "") == "/api/autotrade/sync-bitunix"
               for r in router.routes)


# ---------------- 7. Reconcile bei Live-Fehlern (Position weg) ----------------
def _live_trade(tid="lv1", symbol="ETHUSDT", side="LONG"):
    return {"id": tid, "symbol": symbol, "side": side, "status": "open",
            "mode": "live", "entry": 100.0, "qty": 1.0, "qty_remaining": 1.0,
            "sl": 95.0, "tp1": 105.0, "tpf": 110.0, "leverage": 10}


def _autotrader_for_reconcile(positions, close_detail="Insufficient amount"):
    from services.bitunix_trade import AutoTradeManager
    at = AutoTradeManager(BitunixClientStub(positions))
    at.db = FakeDB(auto_trades=FakeCollection([_live_trade()]))

    async def mark(symbol):
        return 100.0

    async def after_close(t):
        after_close.calls.append(t["id"])

    async def failing_close(t, qty, full=True):
        return {"ok": False, "detail": close_detail}

    async def failing_levels(t, sl=None, tp=None):
        return {"ok": False, "detail": "Position does not exist"}

    async def notify(*a, **kw):
        return None

    after_close.calls = []
    at._current_mark = mark
    at._after_close = after_close
    at.close_live_position = failing_close
    at.sync_live_levels = failing_levels
    at._notify_reject = notify
    return at, after_close


def test_manual_close_books_external_close_when_position_gone():
    """Bug-Report: 'CLOSE ... FEHLGESCHLAGEN: Insufficient amount. Der Trade
    bleibt offen' obwohl die Position an der Börse längst zu war."""
    at, hook = _autotrader_for_reconcile({"ETHUSDT": []})
    res = asyncio.run(at.manual_close("lv1", 100.0))
    assert res and not res.get("error")
    assert res.get("external") is True
    doc = asyncio.run(at.db.auto_trades.find_one({"id": "lv1"}))
    assert doc["status"] == "closed" and doc["closed_by"] == "bitunix_sync"
    assert hook.calls == ["lv1"]


def test_manual_close_keeps_trade_open_when_position_still_exists():
    """'Insufficient amount', aber die Position existiert noch (z.B. Mengen-
    Problem) -> Trade bleibt offen, Fehler wird gemeldet (kein falscher Sync)."""
    at, _ = _autotrader_for_reconcile(
        {"ETHUSDT": [{"side": "BUY", "positionSide": "LONG", "qty": "1.0"}]})
    res = asyncio.run(at.manual_close("lv1", 100.0))
    assert res and res.get("error")
    doc = asyncio.run(at.db.auto_trades.find_one({"id": "lv1"}))
    assert doc["status"] == "open"


def test_adjust_levels_books_external_close_when_position_gone():
    """Bug-Report: 'ADJUST_SL ... Position does not exist – Level unverändert'
    in Endlosschleife -> Trade wird jetzt als extern geschlossen verbucht."""
    at, _ = _autotrader_for_reconcile({"ETHUSDT": []})
    res = asyncio.run(at.adjust_levels("lv1", sl=98.0))
    assert res and not res.get("error")
    assert res.get("external") is True
    doc = asyncio.run(at.db.auto_trades.find_one({"id": "lv1"}))
    assert doc["status"] == "closed"


def test_partial_close_books_external_close_when_position_gone():
    at, _ = _autotrader_for_reconcile({"ETHUSDT": []})
    res = asyncio.run(at.partial_close("lv1", 40, 100.0))
    assert res and not res.get("error")
    assert res.get("external") is True
    doc = asyncio.run(at.db.auto_trades.find_one({"id": "lv1"}))
    assert doc["status"] == "closed"


# ---------------- 8. Label & MasterPrompt/Lektionen ----------------
def test_trade_manager_strategy_name_without_custom_suffix():
    import inspect
    from services import ai_trade_manager as tmod
    src = inspect.getsource(tmod)
    assert "KI Trader (Custom)" not in src
    assert '"strategy_name": "KI Trader"' in src


def test_lesson_audit_deletes_violating_lessons():
    from services.ai_lessons import LessonStore
    from services.ai_master_prompt import master_prompt, normalize_rules
    store = LessonStore()
    store.db = FakeDB(settings=FakeCollection([{
        "_id": "ai_lessons",
        "lessons": [
            {"id": "l1", "title": "Hebel 100x nutzen",
             "detail": "Bei starkem Trend Hebel 100x fahren", "weight": 3},
            {"id": "l2", "title": "Geduld zahlt sich aus",
             "detail": "Nur A-Setups traden", "weight": 3},
            {"id": "l3", "title": "Martingale hilft", "locked": True,
             "detail": "Nach Verlust Einsatz verdoppeln (martingale)", "weight": 2},
        ]}]))
    old_rules = dict(master_prompt.rules)
    try:
        master_prompt.rules = normalize_rules(
            {"max_leverage": 50, "forbidden_terms": ["martingale"]})
        audit = asyncio.run(store.audit_against_master())
        assert audit["checked"] == 3
        removed_ids = {r["id"] for r in audit["removed"]}
        assert removed_ids == {"l1", "l3"}  # auch gesperrte Verstöße werden gelöscht
        left = asyncio.run(store.all())
        assert [l["id"] for l in left] == ["l2"]
    finally:
        master_prompt.rules = old_rules


def test_master_prompt_block_declares_supremacy_over_lessons():
    from services.ai_master_prompt import master_prompt
    block = master_prompt.prompt_block()
    assert "OBERSTES GEBOT" in block
    assert "UNGÜLTIG" in block
