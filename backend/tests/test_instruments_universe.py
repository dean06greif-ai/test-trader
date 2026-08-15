"""Regressionstests für das erweiterte Asset-Universum (Forex/Indices/Resources).

Deckt ab:
  * Rückwärtskompatibilität von core.config (alte Konstanten bleiben gültig)
  * Gruppierung für die Sidebar
  * Auflösung der Historien-Quelle je Anlageklasse
  * Bitunix-Symbol-Mapping + Handelbarkeit
"""
import pytest

from core import config, instruments
from services import bitunix_trade, history_sources


def test_legacy_config_exports_unchanged():
    assert config.TOP_10_COINS[:3] == ["BTCUSDT", "ETHUSDT", "BNBUSDT"]
    assert len(config.TOP_10_COINS) == 10
    for legacy in ("GOLD", "SILVER", "OIL"):
        assert legacy in config.ALL_SYMBOLS
        assert legacy in config.OTHER_YAHOO
    # Neue Assets sind Teil des Universums
    for sym in ("QQQUSDT", "SPYUSDT", "EURUSD", "GBPUSD"):
        assert sym in config.ALL_SYMBOLS


def test_groups_order_and_membership():
    groups = {g["name"]: [s["symbol"] for s in g["symbols"]] for g in instruments.groups()}
    assert list(groups) == ["TOP 10 COINS", "RESOURCES", "INDICES", "FOREX"]
    assert groups["RESOURCES"] == ["GOLD", "SILVER", "OIL"]
    assert groups["INDICES"] == ["QQQUSDT", "SPYUSDT"]
    assert "EURUSD" in groups["FOREX"] and len(groups["FOREX"]) >= 5


def test_every_symbol_is_backtestable():
    assert set(config.BACKTEST_SYMBOLS) == set(config.ALL_SYMBOLS)
    for sym in config.ALL_SYMBOLS:
        assert history_sources.source_of(sym) in ("binance", "bitunix", "yahoo")


@pytest.mark.parametrize("symbol,source", [
    ("BTCUSDT", "binance"),
    ("GOLD", "bitunix"),
    ("QQQUSDT", "bitunix"),
    ("EURUSD", "yahoo"),
])
def test_history_source_routing(symbol, source):
    assert history_sources.source_of(symbol) == source


def test_unknown_symbol_falls_back_to_binance():
    assert history_sources.source_of("SOMENEWUSDT") == "binance"


def test_bitunix_symbol_mapping():
    client = bitunix_trade.BitunixTradeClient()
    assert client.to_bitunix_symbol("GOLD") == "XAUUSDT"
    assert client.to_bitunix_symbol("SILVER") == "XAGUSDT"
    assert client.to_bitunix_symbol("OIL") == "CLUSDT"
    assert client.to_bitunix_symbol("BTCUSDT") == "BTCUSDT"
    assert client.to_bitunix_symbol("QQQUSDT") == "QQQUSDT"


def test_tradability_flags():
    assert instruments.is_tradable("BTCUSDT")
    assert instruments.is_tradable("QQQUSDT")
    assert instruments.is_tradable("GOLD")
    # Bitunix listet keine FX-Kontrakte -> nur Analyse/Backtest/Paper
    assert not instruments.is_tradable("EURUSD")
    assert "EURUSD" not in config.TRADABLE_SYMBOLS


def test_history_days_cap():
    assert instruments.history_days_cap("BTCUSDT", 3000) == 3000
    assert instruments.history_days_cap("EURUSD", 3000) == 30
    assert instruments.history_days_cap("QQQUSDT", 3000) == 110
    assert instruments.history_days_cap("QQQUSDT", 10) == 10


def test_parallel_support_only_for_range_paging_sources():
    assert history_sources.supports_parallel("BTCUSDT")
    assert history_sources.supports_parallel("QQQUSDT")
    assert not history_sources.supports_parallel("EURUSD")
