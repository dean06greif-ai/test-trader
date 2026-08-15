"""Optionale GPU-Beschleunigung (NVIDIA/CuPy) mit Auto-Erkennung & CPU-Fallback.

Beschleunigt werden die vektorisierbaren Rolling-Fenster-Berechnungen der
Indikator-Vorberechnung (SMA, Bollinger-Bänder, Stochastik) in fast_sim.
Rekursive Indikatoren (EMA/RSI/MACD) und die ereignisbasierte Trade-Simulation
bleiben bewusst auf der CPU – dort bringt eine GPU keinen Vorteil.

Aktivierung:
- Website: Lokale Ausführung -> Verwalten -> "GPU nutzen" (setzt USE_GPU=1 im Worker)
- CuPy installieren: pip install cupy-cuda12x (CUDA 12) bzw. cupy-cuda11x (CUDA 11)
Ohne GPU/CuPy oder bei Fehlern läuft automatisch der identische Pandas/NumPy-Pfad.
"""
import logging
import os

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

_cp = None
_checked = False
_gpu_name = None


def _detect():
    global _cp, _checked, _gpu_name
    if _checked:
        return
    _checked = True
    try:
        import cupy as cp
        if cp.cuda.runtime.getDeviceCount() > 0:
            _cp = cp
            props = cp.cuda.runtime.getDeviceProperties(0)
            name = props.get("name")
            _gpu_name = name.decode() if isinstance(name, bytes) else str(name)
            logger.info(f"gpu_accel: NVIDIA-GPU erkannt: {_gpu_name}")
    except Exception as e:  # noqa: BLE001 – kein CuPy/keine GPU -> CPU-Fallback
        logger.debug(f"gpu_accel: keine GPU verfügbar ({e})")


def available() -> bool:
    _detect()
    return _cp is not None


def enabled() -> bool:
    return os.environ.get("USE_GPU") == "1" and available()


def info() -> dict:
    _detect()
    return {"available": _cp is not None, "enabled": enabled(), "name": _gpu_name,
            "backend": "cupy" if _cp is not None else None,
            "accelerates": ["SMA", "Bollinger", "Stochastik"] if _cp is not None else []}


# ---------------- GPU-Kernels (Ergebnis identisch zum Pandas-Pfad) ----------------
def _gpu_rolling_mean(a, w):
    x = _cp.asarray(a, dtype=_cp.float64)
    c = _cp.concatenate([_cp.zeros(1), _cp.cumsum(x)])
    out = _cp.full(x.shape[0], _cp.nan)
    if x.shape[0] >= w:
        out[w - 1:] = (c[w:] - c[:-w]) / w
    return _cp.asnumpy(out)


def _gpu_rolling_std(a, w):
    x = _cp.asarray(a, dtype=_cp.float64)
    c1 = _cp.concatenate([_cp.zeros(1), _cp.cumsum(x)])
    c2 = _cp.concatenate([_cp.zeros(1), _cp.cumsum(x * x)])
    out = _cp.full(x.shape[0], _cp.nan)
    if x.shape[0] >= w:
        s1 = (c1[w:] - c1[:-w]) / w
        s2 = (c2[w:] - c2[:-w]) / w
        out[w - 1:] = _cp.sqrt(_cp.maximum(s2 - s1 * s1, 0.0))
    return _cp.asnumpy(out)


def _gpu_rolling_extreme(a, w, is_max):
    x = _cp.asarray(a, dtype=_cp.float64)
    n = x.shape[0]
    out = _cp.full(n, _cp.nan)
    if n >= w:
        sw = _cp.lib.stride_tricks.sliding_window_view(x, w)
        out[w - 1:] = sw.max(axis=1) if is_max else sw.min(axis=1)
    return _cp.asnumpy(out)


# ---------------- Öffentliche API (mit CPU-Fallback) ----------------
def rolling_mean(a: np.ndarray, w: int) -> np.ndarray:
    if enabled():
        try:
            return _gpu_rolling_mean(a, w)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"gpu_accel mean -> CPU-Fallback: {e}")
    return pd.Series(a).rolling(w).mean().to_numpy()


def rolling_std(a: np.ndarray, w: int) -> np.ndarray:
    """ddof=0 – identisch zum Referenzpfad (Bollinger)."""
    if enabled():
        try:
            return _gpu_rolling_std(a, w)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"gpu_accel std -> CPU-Fallback: {e}")
    return pd.Series(a).rolling(w).std(ddof=0).to_numpy()


def rolling_max(a: np.ndarray, w: int) -> np.ndarray:
    if enabled():
        try:
            return _gpu_rolling_extreme(a, w, True)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"gpu_accel max -> CPU-Fallback: {e}")
    return pd.Series(a).rolling(w).max().to_numpy()


def rolling_min(a: np.ndarray, w: int) -> np.ndarray:
    if enabled():
        try:
            return _gpu_rolling_extreme(a, w, False)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"gpu_accel min -> CPU-Fallback: {e}")
    return pd.Series(a).rolling(w).min().to_numpy()
