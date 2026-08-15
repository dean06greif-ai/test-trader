"""Regression: Manuelle Website-Trades (NewTradeModal → /api/ai/trade/open)
erzeugen KEIN Signal mehr und zählen als 'Manuell (Website)' statt 'KI Trader'
(Bug-Report). Die Pipeline (Guards/Ausführung) läuft trotzdem."""
import asyncio

import pytest

from core import pipeline


class _Signals:
    def __init__(self):
        self.inserted = []

    async def insert_one(self, doc):
        self.inserted.append(doc)


class _Db:
    def __init__(self):
        self.signals = _Signals()


class _State:
    pass


@pytest.fixture
def _env(monkeypatch):
    db = _Db()
    monkeypatch.setattr(pipeline.state, "db", db, raising=False)
    monkeypatch.setattr(pipeline, "toggle_enabled", lambda *a: True)
    monkeypatch.setitem(pipeline.control_state, "signals_paused", False)
    monkeypatch.setattr(pipeline.scanner, "is_notify_enabled", lambda s: False)

    opened = []

    async def _on_signal(signal, candles):
        opened.append(signal)
        return {"id": "t1"}
    monkeypatch.setattr(pipeline.autotrader, "on_signal", _on_signal)
    monkeypatch.setattr(
        pipeline.autotrader, "config",
        {"strategy_coin_configs": {"ai_trader_BTCUSDT": {"mode": "paper"}}},
        raising=False)

    perf_calls = []

    async def _perf(signal, opened=False, result=None):
        perf_calls.append(signal)
    monkeypatch.setattr(pipeline, "update_performance", _perf)
    return db, opened, perf_calls


def _signal(**kw):
    s = {"symbol": "BTCUSDT", "type": "LONG", "signal_class": "SIGNAL",
         "strategy_id": "ai_trader", "strategy_name": "KI Trader",
         "tp1": 101.0, "sl": 99.0, "timestamp": "2026-06-01T00:00:00+00:00"}
    s.update(kw)
    return s


def test_manual_trade_suppresses_signal(_env):
    db, opened, perf_calls = _env
    sig = _signal(manual_trade=True, suppress_signal=True)
    asyncio.run(pipeline.process_signal(sig, []))
    assert db.signals.inserted == []          # kein Signal gespeichert
    assert len(opened) == 1                   # Trade wurde trotzdem eröffnet
    assert sig["_trade_opened"] is True
    assert perf_calls == []                   # zählt NICHT zur KI-Performance
    assert all(e["id"] != sig["id"] for e in pipeline.open_signal_evals)


def test_normal_signal_unchanged(_env):
    db, opened, perf_calls = _env
    sig = _signal()
    asyncio.run(pipeline.process_signal(sig, []))
    assert len(db.signals.inserted) == 1
    assert len(opened) == 1
    assert len(perf_calls) == 1


def test_trade_doc_maps_manual_to_external():
    # Mapping liegt in bitunix_trade._open_trade: strategy_id/strategy_name
    # werden bei manual_trade=True auf external / 'Manuell (Website)' gesetzt.
    import inspect
    from services import bitunix_trade
    src = inspect.getsource(bitunix_trade)
    assert '"external" if signal.get("manual_trade")' in src
    assert 'Manuell (Website)' in src
