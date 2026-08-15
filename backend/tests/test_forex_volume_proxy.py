"""Regressionstests: Instrumente ohne Volumendaten (Forex) blockieren keine Signale.

Spot-FX liefert bei Yahoo durchgehend volume=0. Ohne Ersatz wäre jeder
Volumen-Filter (z.B. `rel_vol_min`) dauerhaft unerfüllt -> 0 Trades.
"""
import numpy as np

from services.fast_sim import FastSeries
from services.history_sources import activity_volume
from services.technical_indicators import TechnicalIndicators as TI


def _candles(n=60, base=1.10, volume=0.0):
    out = []
    for i in range(n):
        close = base + (i % 5) * 0.0002
        out.append({"timestamp": 1_700_000_000_000 + i * 60_000,
                    "open": close, "high": close + 0.0003,
                    "low": close - 0.0003, "close": close, "volume": volume})
    return out


def test_activity_volume_is_positive_and_scales_with_range():
    small = activity_volume(1.1003, 1.1000, 1.1000)
    big = activity_volume(1.1030, 1.1000, 1.1000)
    assert 0 < small < big
    assert activity_volume(1.10, 1.10, 1.10) > 0  # Doji darf nicht 0 werden
    assert activity_volume(1.0, 1.0, 0.0) == 0.0  # kein Division-durch-0-Crash


def test_relative_volume_neutral_without_volume_data():
    assert TI.relative_volume(_candles(volume=0.0), 20) == 1.0


def test_relative_volume_unchanged_with_volume_data():
    candles = _candles(volume=100.0)
    candles[-1]["volume"] = 200.0
    assert TI.relative_volume(candles, 20) == 200.0 / ((19 * 100 + 200) / 20)


def test_relative_volume_needs_enough_candles():
    assert TI.relative_volume(_candles(n=5), 20) is None


def test_fast_series_rel_volume_neutral_without_volume():
    candles = _candles(volume=0.0)
    fs = FastSeries(candles)
    out = fs.get("rel_volume", {"volume_sma_period": 20})
    assert np.all(out == 1.0)


def test_vwap_falls_back_to_typical_price_without_volume():
    candles = _candles(volume=0.0)
    vwap = TI.calculate_vwap(candles)
    assert len(vwap) == len(candles)
    assert all(v > 0 for v in vwap)
