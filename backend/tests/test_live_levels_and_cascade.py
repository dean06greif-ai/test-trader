"""Regressionstests: Live-SL/TP-Bugfixes + Supervisor-Fallback-Kaskade.

Bugs (aus Screenshots des Traders):
  1. ADJUST_SL/TP FEHLGESCHLAGEN: 'TP/SL amount must be less than the size of
     the position' – lokale qty_remaining war größer als die reale Börsen-Menge
     und/oder alte TP/SL-Orders stapelten sich (Mengen-Summe > Position).
  2. CLOSE FEHLGESCHLAGEN: 'Insufficient amount' – Close-Menge > reale Menge.
  3. Viele identische SL-Orders bei Bitunix – neue TP/SL-Orders wurden platziert,
     ohne die alten zu stornieren.

Feature: Aufsicht (Supervisor) schaltet bei 'schwach' kaskadierend um:
  Haupt-Modell -> Fallback 1 -> Fallback 2 -> Empfehlung der Aufsicht.
"""
import asyncio

import pytest

from services.bitunix_trade import AutoTradeManager


class FakeCollection:
    def __init__(self, docs=None):
        self.docs = docs or []
        self.updates = []
        self.inserted = []

    async def find_one(self, q, *a, **kw):
        for d in self.docs:
            if all(d.get(k) == v for k, v in q.items()):
                return dict(d)
        return None

    async def update_one(self, q, upd, **kw):
        self.updates.append((q, upd))
        for d in self.docs:
            if all(d.get(k) == v for k, v in q.items()):
                d.update(upd.get("$set", {}))
        return None

    async def insert_one(self, doc):
        self.inserted.append(dict(doc))

    async def count_documents(self, q):
        return len([d for d in self.docs if all(d.get(k) == v for k, v in q.items())])


class FakeDB:
    def __init__(self, trades=None):
        self.auto_trades = FakeCollection(trades or [])
        self.settings = FakeCollection()
        self.ai_chat = FakeCollection()


class FakeClient:
    """Bitunix-Stub mit TP/SL-Order-Verwaltung und konfigurierbaren Antworten."""

    def __init__(self, live_qty="1.0", pending_tpsl=None,
                 modify_code=1, place_codes=None, close_code=0):
        self.live_qty = live_qty
        self.pending = pending_tpsl or []          # [{"id": "..."}]
        self.modify_code = modify_code
        self.place_codes = place_codes or [0]      # Antwort-Codes je place-Versuch
        self.close_code = close_code
        self.calls = []

    def configured(self):
        return True

    async def resolve_position_id(self, symbol, side):
        self.calls.append(("resolve", symbol, side))
        return "pos-1"

    async def get_positions(self, symbol=None):
        self.calls.append(("get_positions", symbol))
        if self.live_qty is None:
            return {"code": 0, "data": []}
        return {"code": 0, "data": [{"side": "BUY", "qty": self.live_qty}]}

    async def get_pending_tpsl(self, symbol, position_id=None):
        self.calls.append(("get_pending_tpsl", symbol, position_id))
        return {"code": 0, "data": list(self.pending)}

    async def cancel_tpsl_order(self, symbol, order_id):
        self.calls.append(("cancel_tpsl", symbol, order_id))
        self.pending = [p for p in self.pending if str(p.get("id")) != str(order_id)]
        return {"code": 0}

    async def modify_position_tp_sl(self, *a, **kw):
        self.calls.append(("tpsl_modify", a, kw))
        return {"code": self.modify_code,
                "msg": "ok" if self.modify_code == 0 else "order not exist"}

    async def place_position_tp_sl(self, symbol, position_id, side,
                                    tp_price=None, tp_qty=None,
                                    sl_price=None, sl_qty=None):
        self.calls.append(("tpsl_place", {"tp_qty": tp_qty, "sl_qty": sl_qty}))
        code = self.place_codes.pop(0) if len(self.place_codes) > 1 else self.place_codes[0]
        if code == 0:
            return {"code": 0, "data": {"orderId": "tpsl-new"}}
        return {"code": code, "msg": "TP/SL amount must be less than the size of the position."}

    async def flash_close(self, symbol, pos_id, side, qty):
        self.calls.append(("flash_close", qty))
        if self.close_code == 0:
            self.live_qty = None
            return {"code": 0}
        return {"code": self.close_code, "msg": "Insufficient amount"}


def _trade(**kw):
    t = {"id": "t1", "symbol": "BTCUSDT", "side": "LONG", "status": "open",
         "mode": "live", "entry": 100.0, "qty": 1.0, "qty_remaining": 1.0,
         "realized_pnl": 0.0, "fees_paid": 0.0, "fee_percent": 0.06, "events": [],
         "bitunix_position_id": "pos-1", "sl": 95.0, "tp1": 104.0, "tpf": 110.0}
    t.update(kw)
    return t


def _manager(client, trades):
    m = AutoTradeManager(client)
    m.db = FakeDB(trades)
    m.settings = {}
    m._notify_reject = lambda *a, **kw: asyncio.sleep(0)
    return m


# ---------------- Fix 1+3: sync_live_levels ----------------
def test_sync_levels_caps_qty_to_exchange_amount():
    """Lokal 1.0, Börse nur 0.6 -> TP/SL wird mit 0.6 gesendet."""
    t = _trade(qty_remaining=1.0)
    client = FakeClient(live_qty="0.6", modify_code=1)
    m = _manager(client, [t])
    res = asyncio.run(m.sync_live_levels(t, sl=95.0))
    assert res["ok"], res
    place = [c for c in client.calls if c[0] == "tpsl_place"][0]
    assert place[1]["sl_qty"] == 0.6


def test_sync_levels_cancels_stale_tpsl_orders_before_placing():
    """Gestapelte alte SL-Orders werden storniert, bevor EINE neue gesetzt wird."""
    t = _trade(bitunix_tpsl_order_id=None)
    client = FakeClient(live_qty="1.0",
                        pending_tpsl=[{"id": "old-1"}, {"id": "old-2"}, {"id": "old-3"}])
    m = _manager(client, [t])
    res = asyncio.run(m.sync_live_levels(t, sl=96.0))
    assert res["ok"], res
    cancels = [c for c in client.calls if c[0] == "cancel_tpsl"]
    assert len(cancels) == 3
    assert client.pending == []
    assert len([c for c in client.calls if c[0] == "tpsl_place"]) == 1
    assert t["bitunix_tpsl_order_id"] == "tpsl-new"


def test_sync_levels_retries_without_qty_when_size_rejected():
    """Börse lehnt Menge ab -> Retry ohne Mengen-Angabe (ganze Position)."""
    t = _trade()
    client = FakeClient(live_qty="1.0", modify_code=1, place_codes=[20015, 0])
    m = _manager(client, [t])
    res = asyncio.run(m.sync_live_levels(t, sl=96.0))
    assert res["ok"], res
    places = [c for c in client.calls if c[0] == "tpsl_place"]
    assert len(places) == 2
    assert places[0][1]["sl_qty"] is not None      # erst mit Menge
    assert places[1][1]["sl_qty"] is None          # dann ohne


def test_sync_levels_prefers_modify_no_cancel_needed():
    """Modify erfolgreich -> weder Cancel noch neue Order (kein Duplikat)."""
    t = _trade(bitunix_tpsl_order_id="tpsl-7")
    client = FakeClient(live_qty="1.0", modify_code=0)
    m = _manager(client, [t])
    res = asyncio.run(m.sync_live_levels(t, sl=96.0))
    assert res["ok"]
    assert not any(c[0] == "cancel_tpsl" for c in client.calls)
    assert not any(c[0] == "tpsl_place" for c in client.calls)


def test_sync_levels_replaces_with_both_sides_after_cancel():
    """Reines SL-Update: nach dem Cancel wird der TP aus dem Trade mitgesetzt,
    damit die Position an der Börse nie ohne TP dasteht."""
    t = _trade(bitunix_tpsl_order_id=None, tpf=110.0)
    client = FakeClient(live_qty="1.0", pending_tpsl=[{"id": "old-1"}])
    m = _manager(client, [t])
    res = asyncio.run(m.sync_live_levels(t, sl=96.0))
    assert res["ok"], res
    place = [c for c in client.calls if c[0] == "tpsl_place"][0]
    assert place[1]["sl_qty"] == 1.0
    assert place[1]["tp_qty"] == 1.0               # TP (tpf) wurde mitgesendet


# ---------------- Fix 2: close_live_position ----------------
def test_close_caps_qty_to_live_amount():
    """Lokal 1.0, Börse 0.4 -> flash_close mit 0.4 statt 'Insufficient amount'."""
    t = _trade(qty_remaining=1.0)
    client = FakeClient(live_qty="0.4", close_code=0)
    m = _manager(client, [t])
    res = asyncio.run(m.close_live_position(t, 1.0, full=True))
    assert res["ok"], res
    flash = [c for c in client.calls if c[0] == "flash_close"][0]
    assert flash[1] == 0.4


def test_close_ok_when_position_already_gone():
    t = _trade()
    client = FakeClient(live_qty=None)
    m = _manager(client, [t])
    res = asyncio.run(m.close_live_position(t, 1.0, full=True))
    assert res["ok"]
    assert "existiert an der Börse nicht mehr" in res["detail"]
    assert not any(c[0] == "flash_close" for c in client.calls)


# ---------------- Supervisor: Fallback-Kaskade ----------------
def _supervisor_with(role_cfg):
    from services.ai_supervisor import supervisor
    from services.ai_roles import role_manager
    role_manager.config["analyst"].update(role_cfg)
    supervisor.settings = {"auto_switch": True}
    supervisor.engine = type("E", (), {"db": FakeDB()})()
    return supervisor, role_manager


@pytest.fixture(autouse=True)
def _restore_analyst():
    from services.ai_roles import role_manager, DEFAULT_ROLES_CONFIG
    backup = dict(role_manager.config["analyst"])
    yield
    role_manager.config["analyst"] = backup or dict(DEFAULT_ROLES_CONFIG["analyst"])


def test_cascade_switches_to_fallback1_first():
    sup, rm = _supervisor_with({
        "provider": "gemini", "model": "gemini-3.5-flash",
        "fallback_provider": "groq", "fallback_model": "llama-3.3-70b-versatile",
        "fallback2_provider": "mistral", "fallback2_model": "mistral-small-latest"})
    switches = asyncio.run(sup._auto_switch([
        {"role": "analyst", "verdict": "schwach", "action": "modell_wechseln"}]))
    assert switches and switches[0]["to"]["model"] == "llama-3.3-70b-versatile"
    assert "Fallback 1" in switches[0]["to"]["via"]
    assert rm.config["analyst"]["model"] == "llama-3.3-70b-versatile"


def test_cascade_switches_to_fallback2_when_fb1_active():
    sup, rm = _supervisor_with({
        "provider": "groq", "model": "llama-3.3-70b-versatile",
        "fallback_provider": "groq", "fallback_model": "llama-3.3-70b-versatile",
        "fallback2_provider": "mistral", "fallback2_model": "mistral-small-latest"})
    switches = asyncio.run(sup._auto_switch([
        {"role": "analyst", "verdict": "schwach"}]))
    assert switches and switches[0]["to"]["model"] == "mistral-small-latest"
    assert "Fallback 2" in switches[0]["to"]["via"]


def test_cascade_uses_suggestion_when_all_fallbacks_active():
    sup, rm = _supervisor_with({
        "provider": "mistral", "model": "mistral-small-latest",
        "fallback_provider": "mistral", "fallback_model": "mistral-small-latest",
        "fallback2_provider": "mistral", "fallback2_model": "mistral-small-latest"})
    switches = asyncio.run(sup._auto_switch([
        {"role": "analyst", "verdict": "schwach",
         "suggested_provider": "gemini", "suggested_model": "gemini-3.5-flash"}]))
    assert switches and switches[0]["to"]["model"] == "gemini-3.5-flash"
    assert "Empfehlung" in switches[0]["to"]["via"]


def test_cascade_no_switch_without_candidates():
    sup, rm = _supervisor_with({
        "provider": "gemini", "model": "gemini-3.5-flash",
        "fallback_provider": None, "fallback_model": None,
        "fallback2_provider": None, "fallback2_model": None})
    switches = asyncio.run(sup._auto_switch([
        {"role": "analyst", "verdict": "schwach"}]))
    assert switches == []
