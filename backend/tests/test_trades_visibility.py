"""Regression: /api/autotrade/trades darf offene Trades (z.B. ältere manuelle
Bitunix-Trades) NIE aus dem Limit-Fenster fallen lassen (Bug-Report: manuelle
Trades "verschwanden", sobald viele neuere KI-Trades existierten)."""
import asyncio

import pytest

from core import state
from routers import autotrade as autotrade_router


class _Cursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, key, direction=-1):
        self._docs.sort(key=lambda d: d.get(key) or "", reverse=direction == -1)
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    async def to_list(self, n):
        return [dict(d) for d in self._docs[:n]]


class _Coll:
    def __init__(self, docs):
        self.docs = docs

    def find(self, q=None):
        q = q or {}
        return _Cursor([d for d in self.docs
                        if all(d.get(k) == v for k, v in q.items())])


class _Db:
    def __init__(self, docs):
        self.auto_trades = _Coll(docs)


def _mk_trade(i, status="closed", **kw):
    t = {
        "id": f"t{i}", "symbol": "BTCUSDT", "side": "LONG", "mode": "paper",
        "status": status, "entry": 100.0, "sl": 99.0, "tp1": None, "tpf": None,
        "qty": 1.0, "qty_remaining": 1.0, "realized_pnl": 0.0, "fees_paid": 0.0,
        "opened_at": f"2026-06-01T00:{i:02d}:00+00:00", "events": [],
    }
    t.update(kw)
    return t


@pytest.fixture
def _db(monkeypatch):
    # 60 neuere geschlossene Trades + 1 ÄLTERER offener manueller Bitunix-Trade
    docs = [_mk_trade(i + 1) for i in range(59)]
    docs.append(_mk_trade(0, status="open", strategy_id="external",
                          strategy_name="Manuell (Bitunix)", mode="live",
                          manual_trade=True, external_adopted=True))
    monkeypatch.setattr(state, "db", _Db(docs))

    async def _no_positions():
        return {}, {}
    monkeypatch.setattr(autotrade_router, "_live_position_map", _no_positions)
    return docs


def test_open_trades_survive_limit_window(_db):
    res = asyncio.run(
        autotrade_router.get_trades(limit=50))
    trades = res["trades"]
    open_ids = [t["id"] for t in trades if t["status"] == "open"]
    assert "t0" in open_ids, "älterer offener Manuell-Trade fehlt in der Liste"
    # kein Duplikat
    assert len([t for t in trades if t["id"] == "t0"]) == 1


def test_status_filter_unchanged(_db):
    res = asyncio.run(
        autotrade_router.get_trades(status="open", limit=50))
    assert [t["id"] for t in res["trades"]] == ["t0"]
    res = asyncio.run(
        autotrade_router.get_trades(status="closed", limit=10))
    assert len(res["trades"]) == 10
    assert all(t["status"] == "closed" for t in res["trades"])


def test_mode_filter_respected_for_open_extra(_db):
    res = asyncio.run(
        autotrade_router.get_trades(mode="paper", limit=50))
    assert all(t["mode"] == "paper" for t in res["trades"])
    assert "t0" not in [t["id"] for t in res["trades"]]
