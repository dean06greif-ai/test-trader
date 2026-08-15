"""Tests: exakter Bitunix-Abschluss (Positions-Historie) für extern
geschlossene Live-Trades – inkl. manueller Bitunix-Trades (Bug-Report:
PnL auf der Website wich vom echten Bitunix-PnL ab)."""
import asyncio

import pytest

from services.bitunix_trade import parse_closed_position, AutoTradeManager


HIST = {"code": 0, "data": {"positionList": [{
    "positionId": "p1", "symbol": "BTCUSDT", "maxQty": "0.5",
    "entryPrice": "60000", "closePrice": "61000",
    "realizedPNL": "500", "fee": "36.3", "funding": "-1.2",
}], "total": 1}}


class TestParseClosedPosition:
    def test_parses_net_pnl(self):
        r = parse_closed_position(HIST, "p1")
        assert r is not None
        assert r["exit_price"] == 61000.0
        # netto = realizedPNL - fee + funding = 500 - 36.3 - 1.2
        assert r["net_pnl"] == pytest.approx(462.5)
        assert r["fee"] == pytest.approx(36.3)
        assert r["max_qty"] == 0.5

    def test_unknown_position_id(self):
        assert parse_closed_position(HIST, "other") is None

    def test_error_response(self):
        assert parse_closed_position({"code": 1}, "p1") is None
        assert parse_closed_position(None, "p1") is None

    def test_broken_numbers(self):
        bad = {"code": 0, "data": {"positionList": [
            {"positionId": "p1", "realizedPNL": "abc"}]}}
        assert parse_closed_position(bad, "p1") is None

    def test_fee_already_included_not_double_counted(self):
        # Echter Fall (ETH 15.08, Bug-Report): realizedPNL kam von Bitunix
        # BEREITS inkl. Fees (−8.5936 = Preis-PnL −4.6085 − Fee 3.9851).
        # Vorher wurde die Fee nochmal abgezogen -> −12.58 statt −8.59.
        hist = {"code": 0, "data": {"positionList": [{
            "positionId": "eth1", "symbol": "ETHUSDT", "maxQty": "4.228",
            "qty": "4.228", "entryPrice": "1884.56", "closePrice": "1885.65",
            "side": "SELL", "fee": "3.98511197", "funding": "0",
            "realizedPNL": "-8.59363197",
        }]}}
        r = parse_closed_position(hist, "eth1")
        assert r["net_pnl"] == pytest.approx(-8.593632, abs=1e-4)
        assert r["fee_included_in_pnl"] is True

    def test_fee_excluded_gross_pnl_kept(self):
        # Doku-Fall: realizedPNL ist der reine Preis-PnL -> Fee wird abgezogen.
        hist = {"code": 0, "data": {"positionList": [{
            "positionId": "p2", "symbol": "BTCUSDT", "maxQty": "0.5",
            "entryPrice": "60000", "closePrice": "61000", "side": "BUY",
            "fee": "36.3", "funding": "-1.2", "realizedPNL": "500",
        }]}}
        r = parse_closed_position(hist, "p2")
        assert r["net_pnl"] == pytest.approx(462.5)
        assert r["fee_included_in_pnl"] is False


class _Coll:
    def __init__(self):
        self.updates = []

    async def update_one(self, q, u):
        self.updates.append((q, u))


class _Db:
    def __init__(self):
        self.auto_trades = _Coll()


class _Client:
    def __init__(self, res=HIST):
        self.res = res

    def configured(self):
        return True

    async def get_history_positions(self, symbol=None, position_id=None, limit=20):
        return self.res


def _mk_trader(res=HIST):
    tr = AutoTradeManager.__new__(AutoTradeManager)
    tr.db = _Db()
    tr.client = _Client(res)

    async def _mark(sym):
        return 61500.0
    tr._current_mark = _mark

    async def _after(t):
        pass
    tr._after_close = _after
    return tr


def _trade(**kw):
    t = {"id": "t1", "symbol": "BTCUSDT", "side": "LONG", "mode": "live",
         "entry": 60000.0, "qty": 0.5, "qty_remaining": 0.5,
         "realized_pnl": -18.0, "fees_paid": 18.0, "fee_percent": 0.06,
         "bitunix_position_id": "p1", "external_adopted": True, "events": []}
    t.update(kw)
    return t


class TestBookExternalClose:
    def test_exact_bitunix_pnl_used(self):
        tr = _mk_trader()
        res = asyncio.run(tr._book_external_close(_trade()))
        assert res["exit_price"] == 61000.0
        assert res["realized_pnl"] == pytest.approx(462.5)
        _, u = tr.db.auto_trades.updates[0]
        assert u["$set"]["pnl_exchange_exact"] is True
        assert u["$set"]["result"] == "win"

    def test_fallback_estimate_without_position_id(self):
        tr = _mk_trader()
        res = asyncio.run(tr._book_external_close(
            _trade(bitunix_position_id=None)))
        # Schätzung: (61500-60000)*0.5 - fee + bisheriger realized (-18)
        fee = 0.5 * 61500 * 0.0006
        assert res["realized_pnl"] == pytest.approx(-18 + 750 - fee, abs=0.01)
        _, u = tr.db.auto_trades.updates[0]
        assert u["$set"]["pnl_exchange_exact"] is False

    def test_mixed_position_keeps_estimate(self):
        # Börsen-Position deutlich größer als Website-Trade (manuell aufgestockt)
        tr = _mk_trader()
        res = asyncio.run(tr._book_external_close(
            _trade(qty=0.2, qty_remaining=0.2, external_adopted=False)))
        _, u = tr.db.auto_trades.updates[0]
        assert u["$set"]["pnl_exchange_exact"] is False
        assert res["exit_price"] == 61500.0

    def test_paper_trade_never_queries_exchange(self):
        tr = _mk_trader(res={"code": 1})
        res = asyncio.run(tr._book_external_close(_trade(mode="paper")))
        _, u = tr.db.auto_trades.updates[0]
        assert u["$set"]["pnl_exchange_exact"] is False
        assert res is not None
