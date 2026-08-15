"""Regressionstests: Positions-Watchdog + SL-Verifikation + Ghost-Order-Schutz.

Bug-Report: ADA-/DOT-Positionen liefen an der Börse OHNE Stop-Loss in die
Liquidation und waren auf der Website nicht sichtbar. Diese Tests decken ab:
  * parse_positions / emergency_sl (reine Funktionen)
  * Watchdog übernimmt unbekannte Börsen-Positionen (sichtbar auf der Website)
  * Watchdog zieht fehlende Stop-Losses nach
  * Watchdog-Notfall-Close nach max_sl_retries Fehlzyklen
  * kein Eingriff bei unsicherer TP/SL-API (kein falscher Notfall-Close)
  * AutoTradeManager._ensure_live_sl (Verify -> Retry -> False)
  * Telegram format_signal_message ist None-sicher
"""
import asyncio

from services.bitunix_trade import AutoTradeManager, BitunixTradeClient
from services.position_watchdog import PositionWatchdog, parse_positions, emergency_sl


# --------------------------- Fakes ---------------------------------------

class FakeCollection:
    def __init__(self, docs=None):
        self.docs = [dict(d) for d in (docs or [])]
        self.inserted = []
        self.updates = []

    def _match(self, d, q):
        for k, v in q.items():
            if isinstance(v, dict):
                return False  # komplexe Queries: kein Treffer im Fake
            if d.get(k) != v:
                return False
        return True

    async def find_one(self, q, *a, **kw):
        for d in self.docs:
            if self._match(d, q):
                return dict(d)
        return None

    async def insert_one(self, doc):
        self.inserted.append(dict(doc))
        self.docs.append(dict(doc))

    async def update_one(self, q, upd, **kw):
        self.updates.append((q, upd))
        for d in self.docs:
            if self._match(d, q):
                d.update(upd.get("$set", {}))
        return None

    async def count_documents(self, q):
        return len([d for d in self.docs if self._match(d, q)])

    def find(self, q=None, *a, **kw):
        matches = [dict(d) for d in self.docs if self._match(d, q or {})]

        class _Cursor:
            def __init__(self, rows):
                self.rows = rows

            def sort(self, *a, **kw):
                return self

            def limit(self, *a, **kw):
                return self

            async def to_list(self, n=None):
                return self.rows

            def __aiter__(self):
                self._it = iter(self.rows)
                return self

            async def __anext__(self):
                try:
                    return next(self._it)
                except StopIteration:
                    raise StopAsyncIteration
        return _Cursor(matches)


class FakeDB:
    def __init__(self, trades=None):
        self.auto_trades = FakeCollection(trades)
        self.settings = FakeCollection()
        self.dynamic_transition_locks = FakeCollection()


class FakeClient:
    """Bitunix-Client-Stub: konfigurierbare Antworten + Aufruf-Protokoll."""

    def __init__(self, positions=None, tpsl_rows=None, tpsl_code=0,
                 place_tpsl_code=0, close_code=0, mark=100.0):
        self.positions = positions or []
        self.tpsl_rows = tpsl_rows if tpsl_rows is not None else []
        self.tpsl_code = tpsl_code
        self.place_tpsl_code = place_tpsl_code
        self.close_code = close_code
        self.mark = mark
        self.calls = []

    def configured(self):
        return True

    def to_bitunix_symbol(self, s):
        return s

    async def get_positions(self, symbol=None):
        self.calls.append(("get_positions", symbol))
        return {"code": 0, "data": list(self.positions)}

    async def get_pending_tpsl(self, symbol, position_id=None):
        self.calls.append(("get_pending_tpsl", symbol, position_id))
        return {"code": self.tpsl_code, "data": list(self.tpsl_rows)}

    async def place_position_tp_sl(self, symbol, position_id, side,
                                   tp_price=None, tp_qty=None,
                                   sl_price=None, sl_qty=None):
        self.calls.append(("place_tpsl", symbol, position_id, sl_price))
        if self.place_tpsl_code == 0 and sl_price:
            # Erfolgreiche Platzierung ist danach in der TP/SL-Liste sichtbar
            self.tpsl_rows = [{"slPrice": sl_price}]
        return {"code": self.place_tpsl_code, "data": {"orderId": "tpsl-1"}}

    async def flash_close(self, symbol, position_id, side, qty):
        self.calls.append(("flash_close", symbol, position_id, qty))
        return {"code": self.close_code}

    async def get_mark_price(self, symbol):
        return self.mark


def make_watchdog(db, client):
    at = AutoTradeManager(client)
    at.set_db(db)
    wd = PositionWatchdog()
    wd.setup(db, client, at, telegram=None)
    return wd


# --------------------------- reine Funktionen -----------------------------

def test_parse_positions_normalizes_rows():
    payload = {"code": 0, "data": [
        {"symbol": "ADAUSDT", "side": "BUY", "qty": "500", "avgOpenPrice": "0.5",
         "positionId": "p1", "leverage": "10", "margin": "25"},
        {"symbol": "DOTUSDT", "positionSide": "SHORT", "total": "40",
         "entryPrice": "6.2", "id": "p2"},
        {"symbol": "XY", "side": "BUY", "qty": 0, "positionId": "p3"},  # qty 0 -> raus
    ]}
    rows = parse_positions(payload)
    assert len(rows) == 2
    assert rows[0] == {"bitunix_symbol": "ADAUSDT", "side": "LONG", "qty": 500.0,
                       "entry": 0.5, "position_id": "p1", "leverage": 10.0,
                       "margin": 25.0}
    assert rows[1]["side"] == "SHORT" and rows[1]["qty"] == 40.0


def test_parse_positions_rejects_bad_payload():
    assert parse_positions(None) == []
    assert parse_positions({"code": 1, "data": []}) == []
    assert parse_positions({"code": 0, "data": "kaputt"}) == []


def test_emergency_sl_prefers_local_and_fixes_side():
    # lokaler SL gültig -> übernehmen
    assert emergency_sl("LONG", 100, 100, 2.0, local_sl=98.5) == 98.5
    # lokaler SL auf falscher Seite (>= Kurs) -> auf gültige Seite korrigieren
    assert emergency_sl("LONG", 100, 100, 2.0, local_sl=101) == 98.0
    # ohne lokalen SL: pct vom Entry
    assert emergency_sl("SHORT", 100, 100, 2.0) == 102.0
    # Kurs schon über Entry-SL hinaus (SHORT): relativ zum Mark korrigieren
    assert emergency_sl("SHORT", 100, 110, 2.0) == 112.2
    assert emergency_sl("LONG", 0, 0, 2.0) is None


# --------------------------- Watchdog-Zyklen ------------------------------

def test_watchdog_adopts_unknown_position_without_touching_it():
    """NEU (Trader-Vorgabe): Manuelle Bitunix-Positionen werden nur sichtbar
    gemacht (adoptiert), aber NICHT gemanagt – kein SL-Zwang, kein Close."""
    client = FakeClient(positions=[{"symbol": "ADAUSDT", "side": "BUY",
                                    "qty": "500", "avgOpenPrice": "0.5",
                                    "positionId": "p1"}])
    db = FakeDB()
    wd = make_watchdog(db, client)
    status = asyncio.run(wd.check())
    assert status["positions"] == 1
    assert status["adopted"] == 1
    assert status["sl_fixed"] == 0
    # Trade ist jetzt lokal sichtbar (Website: offene Trades)
    adopted = db.auto_trades.inserted[0]
    assert adopted["symbol"] == "ADAUSDT" and adopted["side"] == "LONG"
    assert adopted["external_adopted"] is True and adopted["status"] == "open"
    # aber die Börsen-Position wurde NICHT angefasst
    assert not any(c[0] in ("place_tpsl", "flash_close") for c in client.calls)


def test_watchdog_manage_external_optin_restores_sl_enforcement():
    """Nur mit manage_external=true werden externe Positionen wie früher
    gemanagt (SL nachziehen etc.)."""
    client = FakeClient(positions=[{"symbol": "ADAUSDT", "side": "BUY",
                                    "qty": "500", "avgOpenPrice": "0.5",
                                    "positionId": "p1"}])
    db = FakeDB()
    wd = make_watchdog(db, client)
    wd.settings["manage_external"] = True
    status = asyncio.run(wd.check())
    assert status["adopted"] == 1
    assert status["sl_fixed"] == 1
    assert any(c[0] == "place_tpsl" for c in client.calls)


def test_watchdog_leaves_position_with_sl_alone():
    client = FakeClient(positions=[{"symbol": "BTCUSDT", "side": "BUY",
                                    "qty": "1", "avgOpenPrice": "50000",
                                    "positionId": "p1"}],
                        tpsl_rows=[{"slPrice": "49000"}])
    db = FakeDB(trades=[{"id": "t1", "status": "open", "mode": "live",
                         "symbol": "BTCUSDT", "side": "LONG", "sl": 49000,
                         "qty": 1, "qty_remaining": 1, "entry": 50000}])
    wd = make_watchdog(db, client)
    status = asyncio.run(wd.check())
    assert status["adopted"] == 0
    assert status["sl_missing"] == 0
    assert not any(c[0] == "place_tpsl" for c in client.calls)
    assert not any(c[0] == "flash_close" for c in client.calls)


def test_watchdog_emergency_close_after_retries():
    client = FakeClient(positions=[{"symbol": "DOTUSDT", "side": "SELL",
                                    "qty": "40", "avgOpenPrice": "6.2",
                                    "positionId": "p2"}],
                        place_tpsl_code=1)  # SL-Platzierung schlägt IMMER fehl
    db = FakeDB(trades=[{"id": "t2", "status": "open", "mode": "live",
                         "symbol": "DOTUSDT", "side": "SHORT", "sl": 6.5,
                         "qty": 40, "qty_remaining": 40, "entry": 6.2,
                         "fee_percent": 0.06, "realized_pnl": 0.0,
                         "fees_paid": 0.0, "events": []}])
    wd = make_watchdog(db, client)
    wd.settings["max_sl_retries"] = 3
    for _ in range(2):
        status = asyncio.run(wd.check())
        assert status["emergency_closed"] == 0
    status = asyncio.run(wd.check())  # 3. Fehlzyklus -> Notfall-Close
    assert status["emergency_closed"] == 1
    assert any(c[0] == "flash_close" for c in client.calls)
    # lokaler Trade wurde als extern geschlossen verbucht
    t = asyncio.run(db.auto_trades.find_one({"id": "t2"}))
    assert t["status"] == "closed"


def test_watchdog_no_action_when_tpsl_api_uncertain():
    client = FakeClient(positions=[{"symbol": "BTCUSDT", "side": "BUY",
                                    "qty": "1", "avgOpenPrice": "50000",
                                    "positionId": "p1"}],
                        tpsl_code=500)  # TP/SL-Liste nicht lesbar
    db = FakeDB(trades=[{"id": "t1", "status": "open", "mode": "live",
                         "symbol": "BTCUSDT", "side": "LONG", "sl": 49000,
                         "qty": 1, "qty_remaining": 1, "entry": 50000}])
    wd = make_watchdog(db, client)
    status = asyncio.run(wd.check())
    assert status["sl_missing"] == 0 and status["emergency_closed"] == 0
    assert not any(c[0] in ("place_tpsl", "flash_close") for c in client.calls)


def test_watchdog_disabled_adoption_leaves_external_untouched():
    """Ohne adopt_unknown und ohne lokalen Website-Trade ist die Position
    extern/manuell – der Watchdog fasst sie NICHT an (Trader-Vorgabe)."""
    client = FakeClient(positions=[{"symbol": "ADAUSDT", "side": "BUY",
                                    "qty": "500", "avgOpenPrice": "0.5",
                                    "positionId": "p1"}])
    db = FakeDB()
    wd = make_watchdog(db, client)
    wd.settings["adopt_unknown"] = False
    status = asyncio.run(wd.check())
    assert status["adopted"] == 0
    assert db.auto_trades.inserted == []
    assert status["sl_fixed"] == 0
    assert not any(c[0] in ("place_tpsl", "flash_close") for c in client.calls)


# --------------------------- _ensure_live_sl ------------------------------

def _manager(client):
    at = AutoTradeManager(client)
    at.set_db(FakeDB())
    return at


def test_ensure_live_sl_true_when_present():
    client = FakeClient(tpsl_rows=[{"slPrice": "95"}])
    at = _manager(client)
    assert asyncio.run(at._ensure_live_sl("BTCUSDT", "LONG", "p1", 95.0)) is True
    assert not any(c[0] == "place_tpsl" for c in client.calls)


def test_ensure_live_sl_places_missing_sl():
    client = FakeClient(tpsl_rows=[])
    at = _manager(client)
    assert asyncio.run(at._ensure_live_sl("BTCUSDT", "LONG", "p1", 95.0)) is True
    assert any(c[0] == "place_tpsl" and c[3] == 95.0 for c in client.calls)


def test_ensure_live_sl_false_after_max_attempts():
    client = FakeClient(tpsl_rows=[], place_tpsl_code=1)
    at = _manager(client)
    assert asyncio.run(at._ensure_live_sl("BTCUSDT", "LONG", "p1", 95.0,
                                          max_attempts=2)) is False
    assert len([c for c in client.calls if c[0] == "place_tpsl"]) == 2


def test_ensure_live_sl_none_when_api_uncertain():
    client = FakeClient(tpsl_code=500)
    at = _manager(client)
    assert asyncio.run(at._ensure_live_sl("BTCUSDT", "LONG", "p1", 95.0)) is None
    assert not any(c[0] in ("place_tpsl", "flash_close") for c in client.calls)


# --------------------------- Ghost-Order-Schutz ---------------------------

def test_untracked_qty_detects_ghost_position():
    client = FakeClient(positions=[{"symbol": "ADAUSDT", "side": "BUY",
                                    "qty": "500", "positionId": "p1"}])
    at = AutoTradeManager(client)
    at.set_db(FakeDB())  # keine lokalen Trades -> alles "untracked"
    assert asyncio.run(at._untracked_qty("ADAUSDT", "LONG")) == 500.0


def test_untracked_qty_zero_when_fully_tracked():
    client = FakeClient(positions=[{"symbol": "ADAUSDT", "side": "BUY",
                                    "qty": "500", "positionId": "p1"}])
    at = AutoTradeManager(client)
    at.set_db(FakeDB(trades=[{"status": "open", "mode": "live",
                              "symbol": "ADAUSDT", "side": "LONG",
                              "qty": 500, "qty_remaining": 500}]))
    assert asyncio.run(at._untracked_qty("ADAUSDT", "LONG")) == 0.0


# --------------------------- Telegram None-Guard --------------------------

def test_format_signal_message_none_safe():
    from services.telegram_bot import TelegramNotifier
    tn = TelegramNotifier()
    # Bug-Report: 'NoneType - NoneType' bei Signalen ohne Entry/SL/TP
    msg = tn.format_signal_message({
        "type": "SHORT", "symbol": "BTCUSDT",
        "entry_price": None, "stop_loss": None,
        "take_profit_1": None, "take_profit_full": None,
    })
    assert "n/a" in msg and "BTCUSDT" in msg


def test_format_signal_message_regular_signal_unchanged():
    from services.telegram_bot import TelegramNotifier
    tn = TelegramNotifier()
    msg = tn.format_signal_message({
        "type": "LONG", "symbol": "ADAUSDT", "entry_price": 0.5,
        "stop_loss": 0.49, "take_profit_1": 0.51, "take_profit_full": 0.52,
        "crv": 2.0, "rsi": 55, "tp1_close_percent": 50,
    })
    assert "$0.5" in msg and "-2.00%" in msg and "+2.00%" in msg
