"""Regressionstests: Daten-Frische & Lücken-Backfill des Live-Scanners
(Audit 'Berechnungen/Indikatoren/Datenaktualität', Juni 2026)."""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.scheduler import STALE_AFTER_MS, _is_stale, _needs_backfill  # noqa: E402


def _c(ts_min: int, price: float = 100.0):
    return {"timestamp": ts_min * 60000, "open": price, "high": price,
            "low": price, "close": price, "volume": 1.0}


class TestNeedsBackfill:
    def test_no_buffer_no_backfill(self):
        assert _needs_backfill([], [_c(10)]) is False
        assert _needs_backfill(None, [_c(10)]) is False

    def test_overlap_no_backfill(self):
        buf = [_c(8), _c(9), _c(10)]
        klines = [_c(9), _c(10), _c(11)]
        assert _needs_backfill(buf, klines) is False

    def test_consecutive_no_backfill(self):
        buf = [_c(9), _c(10)]
        klines = [_c(11), _c(12)]
        assert _needs_backfill(buf, klines) is False

    def test_gap_triggers_backfill(self):
        buf = [_c(9), _c(10)]
        klines = [_c(15), _c(16)]  # Minuten 11-14 fehlen
        assert _needs_backfill(buf, klines) is True


class TestIsStale:
    def test_fresh_crypto_not_stale(self):
        now_ms = int(time.time() * 1000)
        assert _is_stale("BTCUSDT", now_ms - 60000, now_ms) is False

    def test_old_crypto_is_stale(self):
        now_ms = int(time.time() * 1000)
        assert _is_stale("BTCUSDT", now_ms - STALE_AFTER_MS - 60000, now_ms) is True

    def test_yahoo_instrument_never_stale(self):
        """Gold/Öl/Forex haben Handelspausen – alte Kerze ist dort normal."""
        from core.instruments import INSTRUMENTS
        yahoo = [i.symbol for i in INSTRUMENTS if i.live_source == "yahoo"]
        if not yahoo:
            return
        now_ms = int(time.time() * 1000)
        assert _is_stale(yahoo[0], now_ms - 3 * 24 * 3600 * 1000, now_ms) is False
