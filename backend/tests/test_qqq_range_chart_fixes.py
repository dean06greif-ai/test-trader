"""Tests: QQQ-Fix (manuelle Positionen + Telegram-Spam), Range-/Wick-Analyse,
Signal-Regel-Snapshot fürs Chart-Overlay."""
import asyncio
import time

import pytest

from services import range_analysis
from services.bitunix_trade import AutoTradeManager as BitunixAutoTrader
from services.ai_trade_manager import AITradeManager


def mk_candle(ts, o, h, l, c, v=10.0):
    return {"timestamp": ts, "open": o, "high": h, "low": l, "close": c, "volume": v}


# ------------------------------------------------------------ Range/Wicks ---
class TestRangeAnalysis:
    def _range_candles(self, n=60, lo=100.0, hi=102.0):
        out = []
        for i in range(n):
            phase = i % 8
            if phase < 2:      # Test der Oberkante
                o, c = hi - 0.4, hi - 0.2
                h, l = hi, hi - 0.5
            elif phase < 4:    # zurück zur Mitte
                o, c = 101.2, 100.9
                h, l = 101.4, 100.7
            elif phase < 6:    # Test der Unterkante
                o, c = lo + 0.4, lo + 0.2
                h, l = lo + 0.5, lo
            else:
                o, c = 100.8, 101.1
                h, l = 101.3, 100.6
            out.append(mk_candle(i * 60000, o, h, l, c))
        return out

    def test_detect_range(self):
        rng = range_analysis.detect_range(self._range_candles())
        assert rng is not None
        assert rng["low"] == pytest.approx(100.0)
        assert rng["high"] == pytest.approx(102.0)
        assert rng["top_touches"] >= 2 and rng["bottom_touches"] >= 2

    def test_no_range_in_trend(self):
        trend = [mk_candle(i * 60000, 100 + i, 100.6 + i, 99.8 + i, 100.5 + i)
                 for i in range(60)]
        assert range_analysis.detect_range(trend) is None

    def test_wick_rejection_bottom(self):
        candles = self._range_candles()
        rng = range_analysis.detect_range(candles)
        # letzte Kerze: lange Lunte an der Unterkante, Schluss darüber
        candles[-1] = mk_candle(candles[-1]["timestamp"], 100.9, 101.0, 99.95, 100.95)
        wicks = range_analysis.wick_rejections(candles, rng)
        assert wicks["bottom"] == 0

    def test_range_text_smoke(self):
        # 1m-Kerzen, die auf 15m eine Range bilden
        candles = []
        for i in range(15 * 60):
            base = self._range_candles()[(i // 15) % 60]
            candles.append(mk_candle(i * 60000, base["open"], base["high"],
                                     base["low"], base["close"]))
        txt = range_analysis.range_text(candles, 101.0)
        assert isinstance(txt, str)  # darf leer sein, aber kein Fehler

    def test_short_data_safe(self):
        assert range_analysis.range_text([], 100.0) == ""
        assert range_analysis.detect_range([mk_candle(0, 1, 2, 0.5, 1.5)]) is None


# ------------------------------------------------------ Reject-Anti-Spam ---
class _FakeTelegram:
    def __init__(self):
        self.sent = []

    async def send_rejection(self, symbol, side, reason):
        self.sent.append((symbol, side, reason))
        return True


class TestRejectCooldown:
    def test_same_reason_suppressed_30min(self):
        at = BitunixAutoTrader(client=None)
        at.telegram = _FakeTelegram()

        async def _flow():
            await at._notify_reject("QQQUSDT", "SHORT", "not supported via OpenAPI")
            await at._notify_reject("QQQUSDT", "SHORT", "not supported via OpenAPI")
            assert len(at.telegram.sent) == 1
            # andere Meldung geht weiterhin durch
            await at._notify_reject("BTCUSDT", "LONG", "anderer Grund")
            assert len(at.telegram.sent) == 2
            # nach Ablauf des Cooldowns wieder erlaubt
            key = "QQQUSDT:SHORT:not supported via OpenAPI"
            at._reject_sent[key] = time.time() - 1801
            await at._notify_reject("QQQUSDT", "SHORT", "not supported via OpenAPI")
        asyncio.run(_flow())
        assert len(at.telegram.sent) == 3


# ------------------------------------------- KI-Manager: manuelle Trades ---
class _FakeColl:
    def __init__(self, doc):
        self.doc = doc

    async def find_one(self, q):
        return dict(self.doc) if self.doc else None


class _FakeDb:
    def __init__(self, doc):
        self.auto_trades = _FakeColl(doc)


class TestManualTradeGuard:
    def _run(self, doc):
        from types import SimpleNamespace
        mgr = AITradeManager()
        mgr.engine = SimpleNamespace(db=_FakeDb(doc))
        return asyncio.run(
            mgr.apply_action("x", "close", reason="test"))

    def test_manual_trade_blocked(self):
        for extra in ({"manual_trade": True}, {"external_adopted": True},
                      {"strategy_id": "external"}):
            doc = {"id": "x", "status": "open", "symbol": "QQQUSDT",
                   "side": "SHORT", **extra}
            res = self._run(doc)
            assert res["status"] == "blocked"
            assert "Manuelle" in res["detail"]

    def test_normal_trade_not_blocked_by_guard(self):
        doc = {"id": "x", "status": "open", "symbol": "BTCUSDT", "side": "LONG",
               "strategy_id": "scalping"}
        mgr = AITradeManager()
        from types import SimpleNamespace
        mgr.engine = SimpleNamespace(db=_FakeDb(doc))
        res = asyncio.run(
            mgr.apply_action("x", "hold", reason="test"))
        assert res == {"status": "ok", "action": "hold"}


# ------------------------------------------------- Signal-Regel-Snapshot ---
class TestSignalRulesSnapshot:
    def test_custom_rules_state_has_timeframe(self):
        import numpy as np
        from strategies.custom_strategy import CustomStrategy
        d = {"id": "snap", "timeframe": "1m",
             "long_rules": [
                 {"indicator": "price", "op": ">", "value": 0},
                 {"indicator": "rsi", "op": "<", "value": 100, "timeframe": "15m"}],
             "short_rules": [{"indicator": "rsi", "op": ">", "value": 999}]}
        candles = []
        for i in range(400):
            px = 100 + float(np.sin(i / 7.0)) * 2
            candles.append(mk_candle(i * 60000, px, px + 0.3, px - 0.3, px + 0.1))
        res = CustomStrategy(d).analyze(candles, "BTCUSDT", {})
        assert res is not None
        by_id = {r["id"]: r for r in res["rules"]}
        assert "timeframe" not in by_id["L0"]
        assert by_id["L1"]["timeframe"] == "15m"

    def test_maybe_signal_snapshot(self):
        from services.strategy_scanner import StrategyScanner

        class Strat:
            STRATEGY_ID = "snap"
            STRATEGY_NAME = "Snap"

        sc = StrategyScanner()
        res = {"signal_type": "LONG", "is_pre_signal": False,
               "levels": {"entry": 100, "stop_loss": 99, "take_profit_1": 101,
                          "take_profit_full": 102, "crv": 2.0},
               "indicators": {"rsi": 50},
               "rules": [
                   {"id": "L0", "label": "Preis > 0", "long": True, "short": False},
                   {"id": "L1", "label": "RSI < 100 @15m", "long": True,
                    "short": False, "timeframe": "15m"}]}
        sig = sc._maybe_signal("BTCUSDT", Strat(), res)
        assert sig is not None
        snap = sig["rules_snapshot"]
        assert snap[0] == {"id": "L0", "label": "Preis > 0", "met": True}
        assert snap[1]["timeframe"] == "15m" and snap[1]["met"] is True
        assert sig["strategy_timeframe"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
