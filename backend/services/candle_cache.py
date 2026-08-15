"""
Hybrid-Kerzen-Cache: In-Memory (spaltenbasiert, numpy) + Disk-Cache.

Historie wird als CandleArray gehalten (48 Byte/Kerze statt ~450 Byte als Dict).
Dadurch passen auch 5400 Tage 1m-Kerzen (7,8 Mio.) mit ~370 MB in den RAM –
vorher waren das >3 GB und der lokale Worker ist beim Laden abgestürzt.

Disk-Format: `<symbol>.npy` (rohes float64-Array, memmap-fähig, Laden in
Millisekunden). Alte `<symbol>.pkl.gz`-Dateien werden beim ersten Zugriff
automatisch konvertiert.

Öffentliche API:
    await get_candles(session, symbol, days, job=None) -> CandleArray
"""
import asyncio
import gzip
import logging
import os
import pickle
import time
from typing import Dict, List, Optional

import numpy as np

from services.candles import CandleArray

logger = logging.getLogger(__name__)

CACHE_DIR = os.environ.get("CANDLE_CACHE_DIR", "/tmp/candle_cache")
# Kerzen sind spaltenbasiert -> ~48 Byte statt ~450 Byte pro Kerze.
# Server-Default 500k Kerzen (~24 MB): passt auch auf 512-MB-Instanzen (Render).
# Ältere Symbole werden auf Disk (.npy) ausgelagert und in Millisekunden
# nachgeladen – kein Leistungsverlust. Der lokale Worker setzt sich sein
# Budget selbst anhand des echten RAMs (siehe local_worker/worker.py).
MAX_CANDLES_IN_MEMORY = int(os.environ.get("CANDLE_CACHE_MAX_CANDLES", "500000"))
DISK_ENABLED = os.environ.get("CANDLE_CACHE_DISK", "1") != "0"
TAIL_TTL_SEC = int(os.environ.get("CANDLE_CACHE_TAIL_TTL", "45"))

_MEM: Dict[str, Dict] = {}  # symbol -> {"candles": CandleArray, "last_refresh", "used_at"}
_LOCK = asyncio.Lock()


def _npy_path(symbol: str) -> str:
    return os.path.join(CACHE_DIR, f"{symbol}.npy")


def _legacy_path(symbol: str) -> str:
    return os.path.join(CACHE_DIR, f"{symbol}.pkl.gz")


def _ensure_dir():
    if DISK_ENABLED:
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
        except OSError as e:
            logger.warning(f"candle_cache: cannot create {CACHE_DIR}: {e}")


def _load_disk(symbol: str) -> Optional[CandleArray]:
    """Lädt `<symbol>.npy`; migriert einmalig ein altes `.pkl.gz`."""
    if not DISK_ENABLED:
        return None
    path = _npy_path(symbol)
    if os.path.exists(path):
        try:
            m = np.load(path, allow_pickle=False)
            if m.ndim == 2 and m.shape[0] and m.shape[1] >= 6:
                return CandleArray.from_matrix(m[:, :6])
            logger.warning(f"candle_cache: {path} hat unerwartete Form {m.shape}")
        except (OSError, ValueError) as e:
            logger.warning(f"candle_cache disk load failed {symbol}: {e}")
        return None
    legacy = _legacy_path(symbol)
    if os.path.exists(legacy):
        try:
            with gzip.open(legacy, "rb") as f:
                data = pickle.load(f)
            if isinstance(data, CandleArray) and len(data):
                _save_disk(symbol, data)
                return data
            if isinstance(data, list) and data:
                ca = CandleArray.from_dicts(data)
                del data
                _save_disk(symbol, ca)
                logger.info(f"candle_cache: {symbol} von .pkl.gz nach .npy migriert "
                            f"({len(ca)} Kerzen)")
                return ca
        except (OSError, pickle.UnpicklingError, EOFError, MemoryError) as e:
            logger.warning(f"candle_cache legacy load failed {symbol}: {e}")
    return None


def _save_disk(symbol: str, candles: CandleArray):
    if not DISK_ENABLED or candles is None or not len(candles):
        return
    _ensure_dir()
    path = _npy_path(symbol)
    tmp = path + ".tmp.npy"
    try:
        np.save(tmp, candles.matrix(), allow_pickle=False)
        os.replace(tmp, path)
        legacy = _legacy_path(symbol)
        if os.path.exists(legacy):
            try:
                os.remove(legacy)
            except OSError:
                pass
    except OSError as e:
        logger.warning(f"candle_cache disk save failed {symbol}: {e}")


def _total_candles() -> int:
    return sum(len(v["candles"]) for v in _MEM.values())


def _evict_if_needed():
    while _total_candles() > MAX_CANDLES_IN_MEMORY and _MEM:
        oldest = min(_MEM.keys(), key=lambda k: _MEM[k]["used_at"])
        entry = _MEM.pop(oldest)
        if DISK_ENABLED:
            _save_disk(oldest, entry["candles"])
        logger.info(f"candle_cache: evicted {oldest} ({len(entry['candles'])} candles)")


async def _evict_if_needed_async():
    while _total_candles() > MAX_CANDLES_IN_MEMORY and _MEM:
        oldest = min(_MEM.keys(), key=lambda k: _MEM[k]["used_at"])
        entry = _MEM.pop(oldest)
        if DISK_ENABLED:
            await asyncio.to_thread(_save_disk, oldest, entry["candles"])
        logger.info(f"candle_cache: evicted {oldest} ({len(entry['candles'])} candles)")


def _merge_tail(existing: CandleArray, new_tail: CandleArray) -> CandleArray:
    """Neue Kerzen anhängen; überlappende Zeitstempel werden ersetzt."""
    if existing is None or not len(existing):
        return new_tail
    if new_tail is None or not len(new_tail):
        return existing
    cut = int(np.searchsorted(existing.ts, new_tail.ts[0], side="left"))
    return CandleArray.concat([existing[:cut], new_tail])


async def _fetch_range(session, symbol: str, start_ms: int, end_ms: int,
                       job: Dict = None, pace: float = None) -> CandleArray:
    """Direkt-Fetch der zuständigen Historien-Quelle (nur fehlender Bereich).

    Welche Quelle für ein Symbol zuständig ist (Binance/Bitunix/Yahoo), steht in
    ``core.instruments`` und wird von ``services.history_sources`` aufgelöst.
    """
    from services import history_sources

    t0 = time.perf_counter()
    blocks = await history_sources.fetch_blocks(session, symbol, start_ms, end_ms,
                                                job=job, pace=pace)
    total = sum(int(b.shape[0]) for b in blocks)
    DOWNLOAD_STATS["candles"] += total
    DOWNLOAD_STATS["seconds"] += time.perf_counter() - t0
    if not blocks:
        return CandleArray.empty()
    return CandleArray.from_matrix(np.concatenate(blocks)).dedup_sorted()


async def _fetch_range_parallel(session, symbol: str, start_ms: int, end_ms: int,
                                job: Dict = None, workers: int = 0) -> CandleArray:
    """Großen Zeitraum in Teilbereiche splitten und parallel laden.
    Die Parallelität wächst mit dem Zeitraum (bis 6 Ströme); das Tempo wird so
    gedrosselt, dass die API-Ratelimits eingehalten werden. Quellen ohne
    Bereichs-Paging (Yahoo) laden sequenziell."""
    from services import history_sources
    span = end_ms - start_ms
    days = span / 86400000.0
    if workers <= 0:
        workers = int(min(4, max(2, days // 400)))
    if not history_sources.supports_parallel(symbol):
        workers = 1
    if span <= 2 * 86400 * 1000 or workers <= 1:
        return await _fetch_range(session, symbol, start_ms, end_ms, job=job)
    pace = history_sources.pace_for(symbol, workers)
    chunk = span // workers
    bounds = [(start_ms + i * chunk,
               end_ms if i == workers - 1 else start_ms + (i + 1) * chunk)
              for i in range(workers)]
    parts = await asyncio.gather(
        *[_fetch_range(session, symbol, a, b, job=job if i == 0 else None, pace=pace)
          for i, (a, b) in enumerate(bounds)])
    return CandleArray.concat(parts).dedup_sorted()


async def get_candles(session, symbol: str, days: int, job: Dict = None) -> CandleArray:
    """1-Minuten-Kerzen der letzten `days` Tage – nutzt Cache aggressiv."""
    from core import instruments
    # Nicht mehr Historie anfordern als die Quelle hergibt, sonst wird bei jedem
    # Lauf erneut ein nie vorhandener Kopf-Bereich gesucht.
    days = max(1, instruments.history_days_cap(symbol, days))
    end = int(time.time() * 1000)
    start = end - days * 86400 * 1000
    async with _LOCK:
        entry = _MEM.get(symbol)
        if entry is None:
            disk = await asyncio.to_thread(_load_disk, symbol)
            if disk is not None and len(disk):
                entry = {"candles": disk, "last_refresh": 0, "used_at": time.time()}
                _MEM[symbol] = entry
                logger.info(f"candle_cache: hydrated {symbol} from disk ({len(disk)})")

    if entry is None:
        logger.info(f"candle_cache MISS {symbol} days={days}")
        candles = await _fetch_range_parallel(session, symbol, start, end, job=job)
        async with _LOCK:
            _MEM[symbol] = {"candles": candles, "last_refresh": time.time(),
                            "used_at": time.time()}
            await _evict_if_needed_async()
        return candles.slice_from_ts(start)

    cached: CandleArray = entry["candles"]
    cached_start = int(cached.ts[0]) if len(cached) else end
    cached_end = int(cached.ts[-1]) if len(cached) else start
    now_ts = time.time()
    needs_head = start < cached_start - 60000
    needs_tail = end > cached_end + 60000 and (now_ts - entry["last_refresh"]) > TAIL_TTL_SEC

    if not needs_head and not needs_tail:
        entry["used_at"] = now_ts
        logger.info(f"candle_cache HIT {symbol} days={days} "
                    f"(cache_span={round((cached_end - cached_start) / 86400000, 1)}d)")
        return cached.slice_from_ts(start)

    if needs_head:
        head = await _fetch_range_parallel(session, symbol, start, cached_start, job=job)
        if len(head):
            head = head[:int(np.searchsorted(head.ts, cached.ts[0], side="left"))]
            cached = CandleArray.concat([head, cached])
            logger.info(f"candle_cache EXTEND-HEAD {symbol} +{len(head)}")

    if needs_tail:
        tail_start = int(cached.ts[-1]) + 60000 if len(cached) else start
        tail = await _fetch_range(session, symbol, tail_start, end, job=job)
        cached = _merge_tail(cached, tail)
        logger.info(f"candle_cache EXTEND-TAIL {symbol} +{len(tail)}")

    async with _LOCK:
        entry["candles"] = cached
        entry["last_refresh"] = now_ts
        entry["used_at"] = now_ts
        await _evict_if_needed_async()
    return cached.slice_from_ts(start)


def stats() -> Dict:
    return {
        "symbols": len(_MEM),
        "total_candles": _total_candles(),
        "per_symbol": {k: len(v["candles"]) for k, v in _MEM.items()},
        "ram_mb": round(sum(v["candles"].nbytes() for v in _MEM.values()) / 1e6, 1),
        "disk_enabled": DISK_ENABLED,
        "cache_dir": CACHE_DIR,
        "max_candles": MAX_CANDLES_IN_MEMORY,
    }


def clear():
    _MEM.clear()


DOWNLOAD_STATS = {"candles": 0, "seconds": 0.0}


def download_stats() -> Dict:
    return dict(DOWNLOAD_STATS)


# ---- Public Helfer für Daten-Verwaltung (lokaler Worker & Server) ----
def cached_meta(symbol: str) -> Optional[Dict]:
    entry = _MEM.get(symbol)
    if not entry or not len(entry["candles"]):
        return None
    c = entry["candles"]
    return {"candles": len(c), "first_ts": int(c.ts[0]), "last_ts": int(c.ts[-1])}


def disk_meta(symbol: str) -> Optional[Dict]:
    """Metadaten direkt von Platte lesen – ohne die Daten in den RAM zu holen."""
    path = _npy_path(symbol)
    if not os.path.exists(path):
        return None
    try:
        m = np.load(path, mmap_mode="r", allow_pickle=False)
        if m.ndim != 2 or not m.shape[0]:
            return None
        return {"candles": int(m.shape[0]), "first_ts": int(m[0, 0]),
                "last_ts": int(m[-1, 0])}
    except (OSError, ValueError):
        return None


def persist_symbol(symbol: str) -> bool:
    entry = _MEM.get(symbol)
    if not entry or not len(entry["candles"]):
        return False
    _save_disk(symbol, entry["candles"])
    return True


async def persist_symbol_async(symbol: str) -> bool:
    entry = _MEM.get(symbol)
    if not entry or not len(entry["candles"]):
        return False
    await asyncio.to_thread(_save_disk, symbol, entry["candles"])
    return True


def remove_symbol(symbol: str):
    _MEM.pop(symbol, None)
    for path in (_npy_path(symbol), _legacy_path(symbol)):
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError as e:
            logger.warning(f"candle_cache remove {symbol}: {e}")


def list_disk_symbols() -> List[Dict]:
    """Alle auf Platte gespeicherten Symbole (neues .npy und altes .pkl.gz)."""
    out: Dict[str, Dict] = {}
    if not os.path.isdir(CACHE_DIR):
        return []
    for fn in sorted(os.listdir(CACHE_DIR)):
        if fn.endswith(".tmp.npy"):
            continue
        if fn.endswith(".npy"):
            sym, prio = fn[:-4], 1
        elif fn.endswith(".pkl.gz"):
            sym, prio = fn[:-7], 0
        else:
            continue
        p = os.path.join(CACHE_DIR, fn)
        try:
            row = {"symbol": sym, "bytes": os.path.getsize(p),
                   "mtime": os.path.getmtime(p), "_prio": prio}
        except OSError:
            continue
        if sym not in out or prio > out[sym]["_prio"]:
            out[sym] = row
    return [{k: v for k, v in r.items() if k != "_prio"}
            for r in sorted(out.values(), key=lambda r: r["symbol"])]
