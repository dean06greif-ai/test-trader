"""Regression: Sichtbarkeits-Sync (manuelle Bitunix-Trades übernehmen) läuft
auch bei AUSGESCHALTETEM Watchdog – ohne jegliches Management (kein SL setzen,
kein Dust-/Notfall-Close). Bug-Report: manuelle Trades verschwanden von der
Website, sobald der Watchdog deaktiviert war."""
import asyncio

import pytest

from services.position_watchdog import PositionWatchdog


class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    async def to_list(self, n):
        return self._docs[:n]


class _Coll:
    def __init__(self):
        self.docs = []

    async def find_one(self, q):
        for d in self.docs:
            if all(d.get(k) == v for k, v in q.items() if not isinstance(v, dict)):
                return dict(d)
        return None

    async def insert_one(self, doc):
        self.docs.append(dict(doc))

    async def update_one(self, q, u, upsert=False):
        return None

    async def update_many(self, q, u):
        class R: modified_count = 0
        return R()


class _Settings:
    async def find_one(self, q):
        return None

    async def update_one(self, q, u, upsert=False):
        return None


class _Db:
    def __init__(self):
        self.auto_trades = _Coll()
        self.settings = _Settings()


class _Client:
    """Wirft bei JEDEM Management-Aufruf – Test schlägt fehl, falls der
    Sync-Only-Modus doch an der Börse eingreift."""
    def __init__(self):
        self.calls = []

    def configured(self):
        return True

    async def get_positions(self, symbol=None):
        return {"code": 0, "data": [{
            "positionId": "p1", "symbol": "BTCUSDT", "side": "BUY",
            "qty": "0.5", "avgOpenPrice": "50000", "leverage": "10",
            "margin": "2500", "unrealizedPNL": "0"}]}

    async def get_mark_price(self, symbol):
        return 50000.0

    def contract_meta(self, symbol):
        raise AssertionError("contract_meta darf im Sync-Only-Modus nicht laufen")

    async def flash_close(self, *a, **k):
        raise AssertionError("flash_close darf im Sync-Only-Modus nicht laufen")

    async def place_position_tp_sl(self, *a, **k):
        raise AssertionError("SL-Platzierung darf im Sync-Only-Modus nicht laufen")


def _mk_watchdog():
    wd = PositionWatchdog()
    wd.db = _Db()
    wd.client = _Client()
    wd.settings["enabled"] = False
    wd.settings["adopt_unknown"] = True

    async def _noop(text):
        pass
    wd._notify = _noop
    return wd


def test_sync_only_adopts_without_managing():
    wd = _mk_watchdog()
    status = asyncio.run(wd.check(manage=False))
    assert status["mode"] == "sync-only"
    assert status["adopted"] == 1
    assert status["errors"] == []
    trades = wd.db.auto_trades.docs
    assert len(trades) == 1
    t = trades[0]
    assert t["strategy_id"] == "external"
    assert t["manual_trade"] is True
    assert t["symbol"] == "BTCUSDT" and t["side"] == "LONG"


def test_sync_only_no_duplicate_adoption():
    wd = _mk_watchdog()
    async def _twice():
        await wd.check(manage=False)
        await wd.check(manage=False)
    asyncio.run(_twice())
    assert len(wd.db.auto_trades.docs) == 1


def test_sync_only_respects_adopt_unknown_off():
    wd = _mk_watchdog()
    wd.settings["adopt_unknown"] = False
    status = asyncio.run(wd.check(manage=False))
    assert status["adopted"] == 0
    assert wd.db.auto_trades.docs == []
