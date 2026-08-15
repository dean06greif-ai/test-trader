"""Regressionstests – Iteration 2 der KI-Trader-Verbesserungen.

Abgedeckt:
  1. Break-Even = ECHTES Break-Even inkl. Entry+Exit-Gebühren (Bug-Report)
  2. Watchdog fasst manuelle Bitunix-Trades (extern, nicht über die Website)
     NICHT mehr an – kein Dust-Close, kein SL-Zwang, kein Notfall-Close
  3. GEMESSENE Liquidations-Verteilung (echte Force-Orders) statt Modell-Formel
  4. KI-Zeitplan: eigenes Modell pro Zeitfenster + korrekte Fenster-Auflösung
  5. Token-Tracking pro Rolle/Tag (Kosten-Dashboard)
  6. 20-Trade-Review nach dem Heatmap-Fix
  7. Lektionen: LessonStore.all() liefert immer konsolidierte superseded-Flags
  8. Zeiten: Berechnung/Anzeige in deutscher Zeit (Europe/Berlin)
"""
import asyncio
import inspect
import time

from core import timeutil
from services import ai_schedule
from services.ai_engine import AIEngine
from services.ai_lessons import LessonStore
from services.bitunix_trade import AutoTradeManager as AutoTrader
from services.liquidity_data import _LiqBuffer, DIST_WINDOW_SEC
from services.position_watchdog import DEFAULT_SETTINGS, PositionWatchdog


# --------------------------------------------------------------------------- #
#  Fakes
# --------------------------------------------------------------------------- #
class _Recorder:
    def __init__(self):
        self.calls = []

    async def update_one(self, flt, update, upsert=False):
        self.calls.append((flt, update, upsert))

    async def insert_one(self, doc):
        self.calls.append(("insert", doc))


class _FailIfTouched:
    def __getattr__(self, name):
        raise AssertionError(f"Externe Position wurde angefasst: .{name} aufgerufen")


# --------------------------------------------------------------------------- #
#  1) Break-Even inkl. Gebühren
# --------------------------------------------------------------------------- #
def _net_pnl_long(entry, exit_p, qty, fee):
    return (exit_p - entry) * qty - fee * entry * qty - fee * exit_p * qty


def _net_pnl_short(entry, exit_p, qty, fee):
    return (entry - exit_p) * qty - fee * entry * qty - fee * exit_p * qty


def test_be_formula_covers_entry_and_exit_fees_long():
    entry, qty, fee = 100.0, 3.0, 0.0006
    be = entry * (1 + fee) / (1 - fee)
    assert abs(_net_pnl_long(entry, be, qty, fee)) < 1e-9


def test_be_formula_covers_entry_and_exit_fees_short():
    entry, qty, fee = 100.0, 3.0, 0.0006
    be = entry * (1 - fee) / (1 + fee)
    assert abs(_net_pnl_short(entry, be, qty, fee)) < 1e-9


def test_old_be_formula_was_slightly_below_true_be():
    """Alte Formel entry*(1+2*fee) deckte die Exit-Gebühr nicht exakt."""
    entry, qty, fee = 100.0, 3.0, 0.0006
    old_be = entry * (1 + 2 * fee)
    assert _net_pnl_long(entry, old_be, qty, fee) < 0


def test_live_be_uses_exact_fee_formula():
    src = inspect.getsource(AutoTrader)
    assert "(1 + fee) / (1 - fee)" in src
    assert "(1 - fee) / (1 + fee)" in src


def test_backtester_be_uses_exact_fee_formula():
    import services.backtester as bt
    src = inspect.getsource(bt)
    assert "(1 + fee_pct) / (1 - fee_pct)" in src


def test_live_sl_sync_only_when_improved():
    """Exchange-SL darf beim BE-Trigger nicht verschlechtert werden."""
    src = inspect.getsource(AutoTrader)
    assert "if improved and t.get(\"mode\") == \"live\":" in src


# --------------------------------------------------------------------------- #
#  2) Watchdog: manuelle Bitunix-Trades nicht anfassen
# --------------------------------------------------------------------------- #
def test_watchdog_default_manage_external_off():
    assert DEFAULT_SETTINGS["manage_external"] is False


def _make_watchdog(local_trade, adopt_unknown=False, manage_external=False):
    wd = PositionWatchdog()
    wd.settings = dict(DEFAULT_SETTINGS)
    wd.settings.update({"adopt_unknown": adopt_unknown,
                        "manage_external": manage_external})

    class _AutoTrades:
        async def find_one(self, flt):
            return local_trade
    class _DB:
        auto_trades = _AutoTrades()
    wd.db = _DB()
    wd._reverse_map = lambda: {}
    wd.client = _FailIfTouched()
    wd.autotrader = _FailIfTouched()
    return wd


def _pos():
    return {"bitunix_symbol": "BTCUSDT", "symbol": "BTCUSDT", "side": "LONG",
            "qty": 0.5, "entry": 50000.0, "position_id": "p1"}


def test_watchdog_skips_manual_position_without_local_trade():
    wd = _make_watchdog(local_trade=None)
    status = {"adopted": 0, "dust_closed": 0}
    asyncio.run(wd._check_position(_pos(), status))  # darf client NIE anfassen
    assert status == {"adopted": 0, "dust_closed": 0}


def test_watchdog_skips_adopted_external_position():
    wd = _make_watchdog(local_trade={"external_adopted": True,
                                     "strategy_id": "external", "sl": 0})
    asyncio.run(wd._check_position(_pos(), {"adopted": 0, "dust_closed": 0}))


def test_watchdog_clears_fail_counter_for_external():
    wd = _make_watchdog(local_trade=None)
    wd._sl_fail["p1"] = 2
    asyncio.run(wd._check_position(_pos(), {"adopted": 0, "dust_closed": 0}))
    assert "p1" not in wd._sl_fail


def test_watchdog_update_settings_accepts_manage_external():
    wd = PositionWatchdog()
    wd.settings = dict(DEFAULT_SETTINGS)
    src = inspect.getsource(PositionWatchdog)
    assert '"manage_external"' in src


# --------------------------------------------------------------------------- #
#  3) Gemessene Liquidations-Verteilung
# --------------------------------------------------------------------------- #
def test_distribution_empty_without_events():
    buf = _LiqBuffer()
    out = buf.distribution("BTCUSDT", 50000.0)
    assert out["measured"] is True
    assert out["below_price"] == [] and out["above_price"] == []
    assert out["total_usd"] == 0


def test_distribution_buckets_real_events_by_side():
    buf = _LiqBuffer()
    for px in (49000, 49010, 49020):           # Long-Liqs (Preis fiel dorthin)
        buf.add("BTCUSDT", "long", 200_000, px)
    buf.add("BTCUSDT", "short", 51_000, 51000)  # eine Short-Liq
    out = buf.distribution("BTCUSDT", 50000.0)
    assert out["events"] == 4
    assert out["total_usd"] == 651_000
    assert out["below_price"], "Long-Liqs müssen als Zonen erscheinen"
    top = out["below_price"][0]
    assert 48900 < top["price"] < 49100 and top["usd"] >= 200_000
    assert out["above_price"][0]["usd"] == 51_000


def test_distribution_window_is_hours_not_minutes():
    assert DIST_WINDOW_SEC >= 3600


def test_liq_window_still_compatible():
    buf = _LiqBuffer()
    buf.add("ETHUSDT", "long", 10_000, 2500)
    buf.add("ETHUSDT", "short", 5_000, 2510)
    w = buf.window("ETHUSDT", 300)
    assert w["long_usd"] == 10_000 and w["short_usd"] == 5_000 and w["count"] == 2


def test_old_events_fall_out_of_distribution():
    buf = _LiqBuffer()
    buf._buf["BTCUSDT"] = [(time.time() - DIST_WINDOW_SEC - 60, "long", 99_000, 49000.0)]
    out = buf.distribution("BTCUSDT", 50000.0)
    assert out["events"] == 0


def test_prompt_uses_measured_not_model_clusters():
    src = inspect.getsource(AIEngine._liquidity_block)
    assert "liq_clusters_measured" in src
    assert "GEMESSENE LIQUIDATIONEN" in src
    assert "MODELL-SCHÄTZUNG" not in src  # Formel-Cluster raus aus dem Prompt


# --------------------------------------------------------------------------- #
#  4) Zeitplan: Modell pro Fenster + Berlin-Zeit
# --------------------------------------------------------------------------- #
def test_schedule_keeps_model_and_provider():
    sched = ai_schedule.normalize_schedule([
        {"from": "15:30", "to": "18:30", "interval_min": 5, "label": "US-Open",
         "model": "llama-3.3-70b-versatile", "provider": "groq"}])
    assert sched[0]["model"] == "llama-3.3-70b-versatile"
    assert sched[0]["provider"] == "groq"


def test_effective_window_returns_window_with_model():
    sched = [{"from": "15:30", "to": "18:30", "interval_min": 5,
              "label": "US-Open", "model": "m1", "provider": "p1"}]
    w = ai_schedule.effective_window(sched, 16 * 60)     # 16:00
    assert w and w["model"] == "m1" and w["interval_min"] == 5
    assert ai_schedule.effective_window(sched, 12 * 60) is None  # 12:00


def test_effective_interval_unchanged_behaviour():
    sched = [{"from": "22:00", "to": "06:00", "interval_min": 30, "label": "Nacht"}]
    assert ai_schedule.effective_interval(sched, 10, 23 * 60) == (30, "Nacht")
    assert ai_schedule.effective_interval(sched, 10, 12 * 60) == (10, "Standard")


def test_disabled_window_has_no_model_effect():
    sched = [{"from": "15:30", "to": "18:30", "interval_min": 5,
              "model": "m1", "enabled": False}]
    assert ai_schedule.effective_window(sched, 16 * 60) is None


def test_engine_analyst_chain_respects_window_model():
    src = inspect.getsource(AIEngine.generate_for_role)
    assert "current_window" in src and "same_provider_chain" in src


def test_schedule_uses_berlin_time():
    src = inspect.getsource(AIEngine.current_window) + \
        inspect.getsource(AIEngine.current_interval)
    assert "berlin" in src.lower()
    assert str(timeutil.BERLIN.key) == "Europe/Berlin"


# --------------------------------------------------------------------------- #
#  5) Token-Tracking (Kosten-Dashboard)
# --------------------------------------------------------------------------- #
def test_track_tokens_upserts_per_role_and_day():
    eng = AIEngine()
    rec = _Recorder()
    class _DB:
        ai_token_usage = rec
    eng.db = _DB()
    asyncio.run(eng._track_tokens("analyst", "gemini-x", 1234))
    flt, update, upsert = rec.calls[0]
    assert flt == {"date": timeutil.berlin_date(), "role": "analyst"}
    assert update["$inc"] == {"tokens": 1234, "calls": 1}
    assert upsert is True


def test_track_tokens_noop_without_db_or_tokens():
    eng = AIEngine()
    asyncio.run(eng._track_tokens("analyst", "m", 100))   # db=None -> kein Fehler
    rec = _Recorder()
    class _DB:
        ai_token_usage = rec
    eng.db = _DB()
    asyncio.run(eng._track_tokens("analyst", "m", 0))
    assert rec.calls == []


# --------------------------------------------------------------------------- #
#  6) 20-Trade-Review
# --------------------------------------------------------------------------- #
def test_heatmap_review_publishes_after_target_trades():
    eng = AIEngine()
    settings_rec, chat_rec = _Recorder(), _Recorder()
    trades = [{"realized_pnl": 10}, {"realized_pnl": -4}]

    class _Cursor:
        async def to_list(self, n):
            return trades
    class _Settings(_Recorder):
        async def find_one(self, flt):
            return {"_id": "ai_heatmap_review",
                    "start_ts": "2026-06-01T00:00:00+00:00",
                    "done": False, "target_trades": 2}
    class _AutoTrades:
        def find(self, flt):
            return _Cursor()
    class _DB:
        settings = _Settings()
        auto_trades = _AutoTrades()
        ai_chat = chat_rec
    _DB.settings.calls = settings_rec.calls
    eng.db = _DB()
    asyncio.run(eng._check_heatmap_review())
    assert chat_rec.calls, "Review muss in den Feed geschrieben werden"
    _, feed = chat_rec.calls[0]
    assert "20-TRADE-REVIEW" in feed["text"]
    assert feed["stats"]["trades"] == 2 and feed["stats"]["winrate_pct"] == 50
    assert any(u[1].get("$set", {}).get("done") is True for u in settings_rec.calls)


def test_heatmap_review_waits_below_target():
    eng = AIEngine()
    chat_rec = _Recorder()

    class _Cursor:
        async def to_list(self, n):
            return [{"realized_pnl": 5}]
    class _Settings:
        async def find_one(self, flt):
            return {"start_ts": "2026-06-01T00:00:00+00:00",
                    "done": False, "target_trades": 20}
        async def update_one(self, *a, **k):
            raise AssertionError("darf vor Ziel nicht abschließen")
    class _AutoTrades:
        def find(self, flt):
            return _Cursor()
    class _DB:
        settings = _Settings()
        auto_trades = _AutoTrades()
        ai_chat = chat_rec
    eng.db = _DB()
    asyncio.run(eng._check_heatmap_review())
    assert chat_rec.calls == []


# --------------------------------------------------------------------------- #
#  7) LessonStore liefert konsolidierte Flags
# --------------------------------------------------------------------------- #
def test_lesson_store_all_returns_superseded_flags():
    store = LessonStore()

    class _Settings:
        async def find_one(self, flt):
            return {"lessons": [
                {"id": "a", "title": "AUTO-LEVERAGE ALT", "detail": "x",
                 "locked": True, "updated_at": "2026-01-01T00:00:00"},
                {"id": "b", "title": "HEBEL 10 STRIKT", "detail": "y",
                 "locked": True, "updated_at": "2026-06-01T00:00:00"},
            ]}
    class _DB:
        settings = _Settings()
    store.db = _DB()
    lessons = asyncio.run(store.all())
    by_id = {l["id"]: l for l in lessons}
    assert by_id["a"].get("superseded") is True
    assert not by_id["b"].get("superseded")


# --------------------------------------------------------------------------- #
#  8) Deutsche Zeit (Europe/Berlin)
# --------------------------------------------------------------------------- #
def test_timeutil_is_berlin_based():
    assert str(timeutil.BERLIN.key) == "Europe/Berlin"
    assert len(timeutil.berlin_date()) == 10
    assert 0 <= timeutil.berlin_minutes() < 1440


def test_token_tracking_uses_berlin_date():
    src = inspect.getsource(AIEngine._track_tokens)
    assert "berlin_date" in src
