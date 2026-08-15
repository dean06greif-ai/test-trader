"""Regressionstests für den Live-Close-Bugfix (KI Trader + manuelles Schließen).

Bug: `manual_close` / Monitor-Exit haben den Trade in der DB als geschlossen
markiert, obwohl die Bitunix-Position weiterlief (flash_close-Antwort wurde
ignoriert, fehlende positionId führte zu stillem Nichtstun).

Jetzt gilt: erst Börse (inkl. positionId-Auflösung, Retry, Verifikation),
dann DB. Scheitert die Börse, bleibt der Trade offen und der Aufrufer bekommt
einen Fehler.
"""
import asyncio

import pytest

from services.bitunix_trade import AutoTradeManager


class FakeCollection:
    def __init__(self, docs=None):
        self.docs = docs or []
        self.updates = []

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

    async def count_documents(self, q):
        return len([d for d in self.docs if all(d.get(k) == v for k, v in q.items())])


class FakeDB:
    def __init__(self, trades):
        self.auto_trades = FakeCollection(trades)


class FakeClient:
    """Bitunix-Client-Stub: konfigurierbare Antworten + Aufruf-Protokoll."""

    def __init__(self, close_code=0, positions=None, position_id="pos-1"):
        self.close_code = close_code
        self._positions = positions if positions is not None else [[]]
        self.position_id = position_id
        self.calls = []

    def configured(self):
        return True

    async def resolve_position_id(self, symbol, side):
        self.calls.append(("resolve", symbol, side))
        return self.position_id

    async def flash_close(self, symbol, pos_id, side, qty):
        self.calls.append(("flash_close", symbol, pos_id, side, qty))
        if self.close_code == 0:
            return {"code": 0, "msg": "ok"}
        return {"code": self.close_code, "msg": "position not exist"}

    async def get_positions(self, symbol=None):
        self.calls.append(("get_positions", symbol))
        data = self._positions[0] if len(self._positions) == 1 else self._positions.pop(0)
        return {"code": 0, "data": data}

    async def place_position_tp_sl(self, *a, **kw):
        self.calls.append(("tpsl", a, kw))
        return {"code": 0, "data": {"orderId": "tpsl-1"}}

    async def modify_position_tp_sl(self, *a, **kw):
        self.calls.append(("tpsl_modify", a, kw))
        return {"code": 0}


def _trade(**kw):
    t = {"id": "t1", "symbol": "BTCUSDT", "side": "LONG", "status": "open", "mode": "live",
         "entry": 100.0, "qty": 1.0, "qty_remaining": 1.0, "realized_pnl": 0.0,
         "fees_paid": 0.0, "fee_percent": 0.06, "events": [],
         "bitunix_position_id": "pos-1", "sl": 95.0, "tp1": 104.0, "tpf": 110.0}
    t.update(kw)
    return t


def _manager(client, trades):
    m = AutoTradeManager(client)
    m.db = FakeDB(trades)
    m.settings = {}
    m._notify_reject = lambda *a, **kw: asyncio.sleep(0)
    return m


def test_manual_close_fails_when_exchange_rejects():
    trades = [_trade()]
    client = FakeClient(close_code=10001, positions=[[{"side": "BUY", "qty": "1.0"}]])
    m = _manager(client, trades)
    res = asyncio.run(m.manual_close("t1", 101.0))
    assert res and res.get("error")
    assert trades[0]["status"] == "open"          # Trade bleibt offen
    assert trades[0]["live_close_failed"] is True
    assert any(c[0] == "flash_close" for c in client.calls)


def test_manual_close_succeeds_and_verifies_flat_position():
    trades = [_trade()]
    client = FakeClient(close_code=0, positions=[[]])
    m = _manager(client, trades)
    res = asyncio.run(m.manual_close("t1", 101.0))
    assert res.get("result") in ("win", "loss", "breakeven")
    assert trades[0]["status"] == "closed"
    assert trades[0]["qty_remaining"] == 0


def test_missing_position_id_is_resolved_before_closing():
    trades = [_trade(bitunix_position_id=None)]
    # Erst meldet die Börse die offene Menge (1.0), nach dem Close ist sie flat.
    client = FakeClient(close_code=0,
                        positions=[[{"side": "BUY", "qty": "1.0"}], []],
                        position_id="pos-99")
    m = _manager(client, trades)
    res = asyncio.run(m.manual_close("t1", 101.0))
    assert res.get("result")
    assert ("resolve", "BTCUSDT", "LONG") in client.calls
    flash = [c for c in client.calls if c[0] == "flash_close"][0]
    assert flash[2] == "pos-99"                   # aufgelöste positionId benutzt


def test_close_is_ok_when_position_already_gone():
    trades = [_trade(bitunix_position_id=None)]
    client = FakeClient(close_code=0, positions=[[]], position_id=None)
    m = _manager(client, trades)
    res = asyncio.run(m.manual_close("t1", 101.0))
    assert res.get("result")
    assert trades[0]["status"] == "closed"


def test_partial_close_aborts_on_exchange_error():
    trades = [_trade()]
    client = FakeClient(close_code=10001, positions=[[{"side": "BUY", "qty": "1.0"}]])
    m = _manager(client, trades)
    res = asyncio.run(m.partial_close("t1", 50, 101.0))
    assert res.get("error")
    assert trades[0]["qty_remaining"] == 1.0      # nichts verändert


def test_adjust_levels_prefers_modifying_the_existing_tpsl_order():
    trades = [_trade(bitunix_tpsl_order_id="tpsl-7")]
    client = FakeClient(positions=[[{"side": "BUY", "qty": "1.0"}]])
    m = _manager(client, trades)
    m._current_mark = lambda symbol: asyncio.sleep(0, result=100.0)
    res = asyncio.run(m.adjust_levels("t1", sl=98.0))
    assert not res.get("error"), res
    assert any(c[0] == "tpsl_modify" for c in client.calls)
    assert not any(c[0] == "tpsl" for c in client.calls)


def test_adjust_levels_pushes_to_exchange_and_reports_rejection():
    trades = [_trade()]
    client = FakeClient(close_code=0, positions=[[{"side": "BUY", "qty": "1.0"}]])
    m = _manager(client, trades)
    m._current_mark = lambda symbol: asyncio.sleep(0, result=100.0)
    res = asyncio.run(
        m.adjust_levels("t1", sl=98.0, tp1=106.0))
    assert not res.get("error"), res
    # Seit dem Bugfix ('Please set at least one of TP/Stop Loss') läuft die
    # Anpassung bevorzugt über modify via positionId – ohne neue Order.
    assert any(c[0] in ("tpsl_modify", "tpsl") for c in client.calls)
    assert trades[0]["sl"] == 98.0

    # Börse lehnt ab -> Level bleiben unverändert
    trades2 = [_trade()]

    class RejectClient(FakeClient):
        async def place_position_tp_sl(self, *a, **kw):
            self.calls.append(("tpsl", a, kw))
            return {"code": 20001, "msg": "rejected"}

        async def modify_position_tp_sl(self, *a, **kw):
            self.calls.append(("tpsl_modify", a, kw))
            return {"code": 20001, "msg": "rejected"}

    client2 = RejectClient(positions=[[{"side": "BUY", "qty": "1.0"}]])
    m2 = _manager(client2, trades2)
    m2._current_mark = lambda symbol: asyncio.sleep(0, result=100.0)
    res2 = asyncio.run(
        m2.adjust_levels("t1", sl=98.0))
    assert res2.get("error")
    assert trades2[0]["sl"] == 95.0


@pytest.mark.parametrize("rows,expected", [
    ([], 0.0),
    ([{"side": "BUY", "qty": "0.5"}], 0.5),
    ([{"side": "SELL", "qty": "0.5"}], 0.0),
])
def test_live_position_qty_parsing(rows, expected):
    client = FakeClient(positions=[rows])
    m = _manager(client, [_trade()])
    qty = asyncio.run(m._live_position_qty(_trade()))
    assert qty == expected
