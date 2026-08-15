"""Test der persistenten Indikator-Bibliothek (services.indicator_cache) und
des Zusammenspiels mit FastSeries."""
import os
import shutil
import tempfile

import numpy as np
import pytest


@pytest.fixture(autouse=True)
def _isolated_cache_dir(monkeypatch):
    """Jeder Test bekommt ein leeres, temporäres Cache-Verzeichnis."""
    tmp = tempfile.mkdtemp(prefix="ind_cache_test_")
    monkeypatch.setenv("INDICATOR_CACHE_DIR", tmp)
    # Modul neu laden, damit CACHE_DIR die Env-Var frisch aufnimmt.
    import importlib

    from services import indicator_cache as ic
    importlib.reload(ic)
    from services import fast_sim
    importlib.reload(fast_sim)
    yield tmp
    shutil.rmtree(tmp, ignore_errors=True)


def _make_candles(n=200, seed=1):
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0, 0.5, n))
    return [
        {
            "timestamp": 1_700_000_000_000 + i * 60_000,
            "open": float(close[i]),
            "high": float(close[i] + 0.3),
            "low": float(close[i] - 0.3),
            "close": float(close[i]),
            "volume": 10.0 + i,
        }
        for i in range(n)
    ]


def test_fingerprint_is_stable_and_data_sensitive():
    from services import indicator_cache as ic

    a = _make_candles(seed=1)
    b = _make_candles(seed=1)
    c = _make_candles(seed=2)
    assert ic.candles_fingerprint(a) == ic.candles_fingerprint(b)
    assert ic.candles_fingerprint(a) != ic.candles_fingerprint(c)


def test_put_get_roundtrip_disk_and_memory():
    from services import indicator_cache as ic

    fp = "abc123"
    key = ("rsi", 14)
    arr = np.arange(50, dtype=float)
    assert ic.get(fp, key) is None
    ic.put(fp, key, arr)
    out = ic.get(fp, key)
    assert out is not None
    assert np.array_equal(out, arr)
    # zweite Runde: Memory-Hit
    ic.get(fp, key)
    stats = ic.stats()
    assert stats["hits"] >= 2
    assert stats["disk_files"] >= 1


def test_fast_series_uses_and_populates_cache():
    from services import fast_sim, indicator_cache as ic

    candles = _make_candles(n=250, seed=42)
    fs1 = fast_sim.FastSeries(candles)
    rsi1 = fs1.get("rsi", {"rsi_period": 14})
    # nach 1. Aufruf muss mindestens 1 Datei existieren
    stats_after_first = ic.stats()
    assert stats_after_first["disk_files"] >= 1

    # zweite FastSeries auf identischen Kerzen -> Cache-Hit, kein Recompute
    hits_before = ic.stats()["hits"]
    fs2 = fast_sim.FastSeries(candles)
    rsi2 = fs2.get("rsi", {"rsi_period": 14})
    hits_after = ic.stats()["hits"]
    assert hits_after > hits_before, "erwartet: RSI aus Cache geladen"
    assert np.allclose(rsi1, rsi2, equal_nan=True)


def test_clear_removes_files_and_memory():
    from services import indicator_cache as ic

    ic.put("fp1", ("ema", 9), np.zeros(10))
    ic.put("fp2", ("ema", 21), np.ones(10))
    assert ic.stats()["disk_files"] >= 2
    removed = ic.clear()
    assert removed >= 2
    assert ic.stats()["mem_items"] == 0
    assert ic.stats()["disk_files"] == 0
    assert ic.get("fp1", ("ema", 9)) is None


def test_get_ignores_stale_length_gracefully():
    """Wenn eine andere Serie mit selbem Fingerprint aber anderer Länge
    zurückkäme, muss FastSeries neu rechnen statt Müll zu liefern."""
    from services import fast_sim, indicator_cache as ic

    candles = _make_candles(n=120, seed=7)
    fs = fast_sim.FastSeries(candles)
    fp = fs._fingerprint()
    # falscher Eintrag mit anderer Länge
    ic.put(fp, ("rsi", 14), np.zeros(5))
    # FastSeries.get() muss erkennen, dass len != self.n und neu berechnen
    out = fs.get("rsi", {"rsi_period": 14})
    assert len(out) == 120
