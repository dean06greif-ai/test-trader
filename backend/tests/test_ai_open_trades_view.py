"""Test der KI-Sicht auf offene Trades (ai_engine._open_trades_text).

Sicherstellt:
- alle Modi (paper/live) und alle Strategien sichtbar
- Fokus-Filter blendet nicht komplett aus, sondern liefert eine Zeile
  mit den außerhalb liegenden offenen Positionen
- wichtige Zustandsflags (tp1_hit, breakeven, profit_secured) im Text
"""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from services.ai_engine import AIEngine


class _Cursor:
    def __init__(self, rows):
        self._rows = rows

    async def to_list(self, _n):
        return list(self._rows)


class _Coll:
    def __init__(self, rows):
        self._rows = rows

    def find(self, _q):
        return _Cursor(self._rows)


def _mk(engine_rows):
    engine = SimpleNamespace()
    engine.db = SimpleNamespace(auto_trades=_Coll(engine_rows))
    return engine


def _run(coro):
    return asyncio.run(coro)



def test_open_trades_text_shows_all_modes_and_strategies():
    now = datetime.now(timezone.utc)
    rows = [
        {"id": "T1", "symbol": "BTCUSDT", "side": "LONG", "mode": "paper",
         "strategy_name": "AI Trader", "entry": 65000, "sl": 64500, "tp1": 65800,
         "tpf": 66500, "leverage": 5, "qty": 0.1, "qty_remaining": 0.1,
         "realized_pnl": -0.5, "opened_at": (now - timedelta(minutes=42)).isoformat(),
         "tp1_hit": False, "breakeven_moved": False, "profit_secured": False},
        {"id": "T2", "symbol": "ETHUSDT", "side": "SHORT", "mode": "live",
         "strategy_name": "Trend Rider", "entry": 3200, "sl": 3250, "tp1": 3150,
         "tpf": 3080, "leverage": 3, "qty": 2.0, "qty_remaining": 1.0,
         "realized_pnl": 3.2, "opened_at": (now - timedelta(hours=2)).isoformat(),
         "tp1_hit": True, "breakeven_moved": True, "profit_secured": False},
    ]
    text = _run(AIEngine._open_trades_text(_mk(rows), None))
    assert "BTCUSDT" in text and "ETHUSDT" in text
    assert "paper/AI Trader" in text
    assert "live/Trend Rider" in text
    assert "TP1✓" in text and "BE" in text
    assert "Qty 1.0/2.0" in text
    assert "realPnL +3.20USDT" in text



def test_open_trades_text_reports_out_of_focus_positions():
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        {"id": "T1", "symbol": "BTCUSDT", "side": "LONG", "mode": "paper",
         "strategy_name": "AI Trader", "entry": 65000, "sl": 64500, "tp1": 65800,
         "tpf": 66500, "leverage": 5, "qty": 0.1, "qty_remaining": 0.1,
         "opened_at": now},
        {"id": "T2", "symbol": "ETHUSDT", "side": "SHORT", "mode": "live",
         "strategy_name": "Trend Rider", "entry": 3200, "sl": 3250, "tp1": 3150,
         "tpf": 3080, "leverage": 3, "qty": 2.0, "qty_remaining": 2.0,
         "opened_at": now},
        {"id": "T3", "symbol": "SOLUSDT", "side": "LONG", "mode": "paper",
         "strategy_name": "AI Trader", "entry": 74, "sl": 72, "tp1": 76,
         "tpf": 80, "leverage": 4, "qty": 5, "qty_remaining": 5,
         "opened_at": now},
    ]
    # Fokus nur auf BTC – KI muss ETH/SOL zumindest als Hinweis-Zeile bekommen
    text = _run(AIEngine._open_trades_text(_mk(rows), ["BTCUSDT"]))
    assert "BTCUSDT" in text
    assert "WEITERE offene Positionen außerhalb des Fokus: 2" in text
    assert "paper 1" in text and "live 1" in text
    assert "ETHUSDT" in text and "SOLUSDT" in text



def test_open_trades_text_empty():
    text = _run(AIEngine._open_trades_text(_mk([]), None))
    assert text == "(keine offenen Positionen)"



def test_open_trades_text_focus_with_no_matches_still_hints_others():
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        {"id": "T1", "symbol": "ETHUSDT", "side": "SHORT", "mode": "live",
         "strategy_name": "Trend Rider", "entry": 3200, "sl": 3250, "tp1": 3150,
         "tpf": 3080, "leverage": 3, "qty": 2.0, "qty_remaining": 2.0,
         "opened_at": now},
    ]
    text = _run(AIEngine._open_trades_text(_mk(rows), ["BTCUSDT"]))
    assert "keine offenen Positionen im Fokus" in text
    assert "WEITERE offene Positionen außerhalb des Fokus: 1" in text
