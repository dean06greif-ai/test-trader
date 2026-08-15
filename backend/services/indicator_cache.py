"""
Persistente Indikator-Bibliothek für Deep Tests / Optimizer.

Speichert berechnete Indikator-Serien (numpy) so, dass sie über Deep-Test-Läufe
hinweg wiederverwendet werden können. Ein Cache-Eintrag ist eindeutig durch:
    (Fingerprint der Kerzenserie, Indikator-Key inkl. Parameter)

Der Fingerprint basiert auf (erster/letzter Zeitstempel, Anzahl, erster/letzter
Close-Preis) und identifiziert damit eine konkrete Kerzenserie deterministisch,
ohne die kompletten Daten zu hashen.

Genutzt von services.fast_sim.FastSeries.get().
"""
import hashlib
import logging
import os
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

CACHE_DIR = Path(os.environ.get("INDICATOR_CACHE_DIR", "/tmp/indicator_cache"))
_MEM_LIMIT = int(os.environ.get("INDICATOR_CACHE_MEM_ITEMS", "1024"))
_DISK_ENABLED = os.environ.get("INDICATOR_CACHE_DISK", "1") not in ("0", "false", "False")

_MEM: "OrderedDict[Tuple[str, tuple], np.ndarray]" = OrderedDict()
_LOCK = threading.Lock()
_HITS = 0
_MISSES = 0


def _ensure_dir() -> bool:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        return True
    except OSError as e:
        logger.warning(f"indicator_cache: cannot create {CACHE_DIR}: {e}")
        return False


def candles_fingerprint(candles) -> str:
    """Stabile 16-Zeichen-Kennung für eine Kerzenserie."""
    from services.candles import CandleArray

    if isinstance(candles, CandleArray):
        ts = candles.ts
        cl = candles.cl
        n = len(candles)
    else:
        n = len(candles)
        if n == 0:
            return "empty"
        first, last = candles[0], candles[-1]
        raw = f"{int(first['timestamp'])}-{int(last['timestamp'])}-{n}-" \
              f"{float(first['close']):.8f}-{float(last['close']):.8f}"
        return hashlib.md5(raw.encode()).hexdigest()[:16]

    if n == 0:
        return "empty"
    raw = f"{int(ts[0])}-{int(ts[-1])}-{n}-" \
          f"{float(cl[0]):.8f}-{float(cl[-1]):.8f}"
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def _path(fingerprint: str, key: tuple) -> Path:
    key_str = "_".join(repr(x) for x in key)
    h = hashlib.md5(key_str.encode()).hexdigest()[:16]
    return CACHE_DIR / f"{fingerprint}__{h}.npy"


def _mem_put(mem_key: Tuple[str, tuple], arr: np.ndarray) -> None:
    with _LOCK:
        if mem_key in _MEM:
            _MEM.move_to_end(mem_key)
        else:
            if len(_MEM) >= _MEM_LIMIT:
                _MEM.popitem(last=False)  # LRU-Evict
            _MEM[mem_key] = arr


def get(fingerprint: str, key: tuple) -> Optional[np.ndarray]:
    """Indikator-Serie aus Cache holen. None wenn Miss."""
    global _HITS, _MISSES
    if not fingerprint or fingerprint == "empty":
        _MISSES += 1
        return None
    mem_key = (fingerprint, key)
    with _LOCK:
        arr = _MEM.get(mem_key)
        if arr is not None:
            _MEM.move_to_end(mem_key)
            _HITS += 1
            return arr
    if not _DISK_ENABLED:
        _MISSES += 1
        return None
    p = _path(fingerprint, key)
    if p.exists():
        try:
            arr = np.load(str(p), allow_pickle=False)
            _mem_put(mem_key, arr)
            _HITS += 1
            return arr
        except (OSError, ValueError) as e:
            logger.debug(f"indicator_cache: bad file {p}: {e}")
            try:
                p.unlink()
            except OSError:
                pass
    _MISSES += 1
    return None


def put(fingerprint: str, key: tuple, arr: np.ndarray) -> None:
    """Indikator-Serie in Cache schreiben."""
    if not fingerprint or fingerprint == "empty" or arr is None:
        return
    if not isinstance(arr, np.ndarray):
        return
    mem_key = (fingerprint, key)
    _mem_put(mem_key, arr)
    if not _DISK_ENABLED:
        return
    if not _ensure_dir():
        return
    p = _path(fingerprint, key)
    tmp = p.with_suffix(".tmp")
    try:
        np.save(str(tmp), arr, allow_pickle=False)
        os.replace(str(tmp), str(p))
    except OSError as e:
        logger.debug(f"indicator_cache: cannot write {p}: {e}")
        try:
            tmp.unlink()
        except OSError:
            pass


def clear() -> int:
    """Alle Einträge (Memory + Disk) löschen. Gibt Anzahl gelöschter Dateien zurück."""
    global _HITS, _MISSES
    with _LOCK:
        _MEM.clear()
        _HITS = 0
        _MISSES = 0
    n = 0
    if _DISK_ENABLED and CACHE_DIR.is_dir():
        for f in CACHE_DIR.glob("*.npy"):
            try:
                f.unlink()
                n += 1
            except OSError:
                pass
    return n


def stats() -> dict:
    """Cache-Statistiken für Monitoring/Debug."""
    files = 0
    total_bytes = 0
    if _DISK_ENABLED and CACHE_DIR.is_dir():
        for f in CACHE_DIR.glob("*.npy"):
            try:
                total_bytes += f.stat().st_size
                files += 1
            except OSError:
                pass
    with _LOCK:
        mem_items = len(_MEM)
        hits = _HITS
        misses = _MISSES
    total = hits + misses
    return {
        "mem_items": mem_items,
        "mem_limit": _MEM_LIMIT,
        "disk_enabled": _DISK_ENABLED,
        "disk_files": files,
        "disk_bytes": total_bytes,
        "disk_mb": round(total_bytes / 1024 / 1024, 2),
        "dir": str(CACHE_DIR),
        "hits": hits,
        "misses": misses,
        "hit_ratio": round(hits / total, 3) if total else 0.0,
    }
