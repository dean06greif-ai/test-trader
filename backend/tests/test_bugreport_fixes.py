"""Regressionstests für die Bug-Report-Fixes (Screenshots 07/2026):

1. 'CLOSE-VERSUCH 1-5 fehlgeschlagen: Insufficient amount' (DOT):
   Börsen-Rest unter dem handelbaren Minimum (Dust) gilt als geschlossen.
2. 'ADJUST FEHLGESCHLAGEN: Please set at least one of TP/Stop Loss' (ADA):
   Modify läuft jetzt über den korrekten Endpoint
   /api/v1/futures/tpsl/position/modify_order mit positionId + slStopType.
3. 'ADJUST SL 0.202349 -> 0.0002' (KI entfernte faktisch den Stop):
   Die KI darf den SL nur enger ziehen, nie das Risiko vergrößern.
"""
import asyncio

from services.bitunix_trade import AutoTradeManager, BitunixTradeClient
from services.ai_trade_manager import AITradeManager

from tests.test_position_watchdog import FakeDB


class DustClient:
    """Position mit Dust-Rest an der Börse; Close würde immer scheitern."""

    def __init__(self, live_qty=0.1, min_qty=1.0):
        self.live_qty = live_qty
        self.min_qty = min_qty
        self.calls = []

    def configured(self):
        return True

    def to_bitunix_symbol(self, s):
        return s

    def contract_meta(self, s):
        return {"min_qty": self.min_qty, "qty_step": 0.1, "price_tick": 0.0001}

    async def get_positions(self, symbol=None):
        self.calls.append(("get_positions", symbol))
        return {"code": 0, "data": [{"symbol": symbol or "DOTUSDT", "side": "BUY",
                                     "qty": str(self.live_qty), "positionId": "p1"}]}

    async def resolve_position_id(self, symbol, side):
        return "p1"

    async def flash_close(self, symbol, position_id, side, qty):
        self.calls.append(("flash_close", qty))
        return {"code": 1, "msg": "Insufficient amount"}

    async def get_mark_price(self, symbol):
        return 0.82


def test_close_dust_position_counts_as_closed():
    client = DustClient(live_qty=0.1, min_qty=1.0)
    at = AutoTradeManager(client)
    at.set_db(FakeDB())
    t = {"id": "t1", "symbol": "DOTUSDT", "side": "LONG", "mode": "live",
         "qty": 1682.0, "qty_remaining": 1682.0, "entry": 0.8252,
         "bitunix_position_id": "p1"}
    res = asyncio.run(at.close_live_position(t, 1682.0, full=True))
    assert res["ok"] is True
    assert "Dust" in res["detail"]
    # Es wurde NICHT sinnlos versucht zu schließen (die alte Endlosschleife)
    assert not any(c[0] == "flash_close" for c in client.calls)


def test_close_normal_position_still_flash_closes():
    client = DustClient(live_qty=1682.0, min_qty=1.0)
    at = AutoTradeManager(client)
    at.set_db(FakeDB())
    t = {"id": "t1", "symbol": "DOTUSDT", "side": "LONG", "mode": "live",
         "qty": 1682.0, "qty_remaining": 1682.0, "entry": 0.8252,
         "bitunix_position_id": "p1"}
    res = asyncio.run(at.close_live_position(t, 1682.0, full=True))
    # flash_close lehnt ab -> Fehler wird sauber gemeldet (kein Dust-Bypass)
    assert res["ok"] is False
    assert any(c[0] == "flash_close" for c in client.calls)


def test_modify_position_tp_sl_uses_documented_endpoint():
    c = BitunixTradeClient()
    captured = {}

    async def fake_post(path, body):
        captured["path"] = path
        captured["body"] = body
        return {"code": 0, "data": {"orderId": "tpsl-9"}}

    c._post = fake_post
    res = asyncio.run(c.modify_position_tp_sl("BTCUSDT", "pos-9",
                                              sl_price=95.0, side="LONG"))
    assert res["code"] == 0
    assert captured["path"] == "/api/v1/futures/tpsl/position/modify_order"
    body = captured["body"]
    assert body["positionId"] == "pos-9"
    assert "orderId" not in body
    assert float(body["slPrice"]) == 95.0
    assert body["slStopType"] == "MARK_PRICE"
    assert "tpPrice" not in body


class MarkClient:
    def __init__(self, mark):
        self.mark = mark

    def configured(self):
        return False

    async def get_mark_price(self, symbol):
        return self.mark


def _tm_with_trade(trade, mark=0.2025):
    at = AutoTradeManager(MarkClient(mark))
    db = FakeDB(trades=[trade])
    at.set_db(db)
    tm = AITradeManager()

    class _Engine:
        pass

    eng = _Engine()
    eng.db = db
    tm.setup(eng, at)
    tm.settings["cooldown_min"] = 0
    return tm


def test_ki_cannot_widen_stop_loss():
    trade = {"id": "t1", "status": "open", "symbol": "ADAUSDT", "side": "LONG",
             "mode": "paper", "entry": 0.2, "sl": 0.202349, "tp1": 0.21,
             "tpf": 0.22, "qty": 6243, "qty_remaining": 6243, "events": []}
    tm = _tm_with_trade(trade)
    # Bug-Report: KI wollte SL 0.202349 -> 0.0002 (Stop faktisch entfernt)
    res = asyncio.run(tm.apply_action("t1", "adjust_sl", value=0.0002,
                                      reason="test", source="ki"))
    assert res["status"] == "blocked"
    assert "SL-Erweiterung blockiert" in res["detail"]


def test_ki_can_tighten_stop_loss():
    trade = {"id": "t1", "status": "open", "symbol": "ADAUSDT", "side": "LONG",
             "mode": "paper", "entry": 0.2, "sl": 0.198, "tp1": 0.21,
             "tpf": 0.22, "qty": 6243, "qty_remaining": 6243, "events": [],
             "fee_percent": 0.06}
    tm = _tm_with_trade(trade)
    res = asyncio.run(tm.apply_action("t1", "adjust_sl", value=0.201,
                                      reason="test", source="ki"))
    assert res["status"] == "ok"


def test_manual_sl_widen_stays_allowed():
    trade = {"id": "t1", "status": "open", "symbol": "ADAUSDT", "side": "LONG",
             "mode": "paper", "entry": 0.2, "sl": 0.198, "tp1": 0.21,
             "tpf": 0.22, "qty": 6243, "qty_remaining": 6243, "events": [],
             "fee_percent": 0.06}
    tm = _tm_with_trade(trade)
    res = asyncio.run(tm.apply_action("t1", "adjust_sl", value=0.19,
                                      reason="test", source="manual",
                                      enforce_limits=False))
    assert res["status"] == "ok"
