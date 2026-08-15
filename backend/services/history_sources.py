"""Historien-Quellen für 1-Minuten-Kerzen (Binance / Bitunix / Yahoo).

Der Backtester/Optimizer arbeitet ausschließlich mit 1m-Kerzen. Welche Quelle
für ein Symbol zuständig ist, steht in ``core.instruments``:

    binance  Krypto (data-api.binance.vision, 1000 Kerzen/Request, volle Historie)
    bitunix  Metalle/Öl/Indizes (fapi.bitunix.com, 200 Kerzen/Request, endTime-Paging)
    yahoo    Forex (query1.finance.yahoo.com, ~7 Tage/Request, ca. 30 Tage Historie)

Jede Quelle liefert eine Matrix (N,6): [ts_ms, open, high, low, close, volume]
– aufsteigend sortiert, Duplikate werden vom Aufrufer entfernt.
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

import aiohttp
import numpy as np

from core import instruments

logger = logging.getLogger(__name__)

BINANCE_URL = "https://data-api.binance.vision/api/v3/klines"
BITUNIX_URL = "https://fapi.bitunix.com/api/v1/futures/market/kline"
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}"

MINUTE = 60_000
YAHOO_CHUNK_MS = 7 * 86400 * 1000

# Quellen, bei denen sich ein Zeitraum sinnvoll in parallele Teilbereiche
# splitten lässt (eigenständige Requests mit start/end).
PARALLEL_SOURCES = {"binance", "bitunix"}


class HistoryUnavailable(RuntimeError):
    """Netzwerk-/API-Fehler beim Laden – bereits geladene Etappen bleiben gültig."""


def _resolve(symbol: str) -> tuple:
    inst = instruments.get(symbol)
    if inst is None:
        return "binance", symbol
    return inst.hist_source, inst.hist_ref


def _progress(job: Optional[Dict], symbol: str, cur: int, start_ms: int, end_ms: int):
    if job is None:
        return
    span = max(end_ms - start_ms, 1)
    pct = min(max(round((cur - start_ms) / span * 100), 0), 100)
    job["phase"] = f"Lade Daten: {symbol} ({pct}%)"


def activity_volume(high: float, low: float, close: float) -> float:
    """Volumen-Ersatz für Instrumente ohne Volumendaten (Forex-Spotkurse).

    Am Spot-FX-Markt gibt es kein konsolidiertes Volumen; Yahoo liefert 0.
    Ein Volumen-Filter würde damit jedes Signal blockieren. Als Ersatz dient die
    relative Kerzen-Spanne – ein etablierter Aktivitäts-Proxy: überdurchschnittliche
    Spanne = überdurchschnittliche Teilnahme.
    """
    if not close:
        return 0.0
    return max(abs(high - low) / abs(close) * 1_000_000.0, 1e-6)


async def _get_json(session, url: str, params: dict, timeout: int = 30, headers: dict = None):
    async with session.get(url, params=params, headers=headers,
                           timeout=aiohttp.ClientTimeout(total=timeout)) as r:
        return await r.json(content_type=None)


# Yahoo antwortet ohne Browser-User-Agent mit HTML statt JSON.
YAHOO_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "application/json",
}


# --------------------------------------------------------------------------
# Binance (Krypto) – 1000 Kerzen pro Request, vorwärts über startTime
# --------------------------------------------------------------------------
async def fetch_binance(session, ref: str, start_ms: int, end_ms: int,
                        job: Dict = None, pace: float = 0.06) -> List[np.ndarray]:
    blocks: List[np.ndarray] = []
    cur = start_ms
    while cur < end_ms:
        _check_cancel(job)
        params = {"symbol": ref, "interval": "1m", "startTime": cur, "limit": 1000}
        data = None
        for attempt in range(5):
            try:
                data = await _get_json(session, BINANCE_URL, params)
                if isinstance(data, list):
                    break
                data = None
            except Exception as e:  # noqa: BLE001
                logger.warning(f"binance {ref} attempt {attempt + 1} failed: {e}")
                data = None
            await asyncio.sleep(1.5 * (attempt + 1))
        if data is None:
            # NICHT stillschweigend abbrechen, sonst entsteht eine Lücke in der
            # Historie, die später als "im Cache" gilt und nie nachgeladen wird.
            raise HistoryUnavailable(
                f"Download von {ref} bei "
                f"{datetime.fromtimestamp(cur / 1000, timezone.utc):%d.%m.%Y} "
                "abgebrochen (Netzwerk/API nicht erreichbar). Bereits geladene "
                "Etappen sind gespeichert – Download einfach erneut starten.")
        if not data:
            break  # vor Listing-Datum / keine weiteren Kerzen
        blocks.append(np.array([[k[0], k[1], k[2], k[3], k[4], k[5]] for k in data],
                               dtype=np.float64))
        cur = int(data[-1][0]) + MINUTE
        _progress(job, ref, cur, start_ms, end_ms)
        if len(data) < 1000:
            break
        await asyncio.sleep(pace)
    return blocks


# --------------------------------------------------------------------------
# Bitunix (Metalle, Öl, Indizes) – max. 200 Kerzen, rückwärts über endTime
# --------------------------------------------------------------------------
async def fetch_bitunix(session, ref: str, start_ms: int, end_ms: int,
                        job: Dict = None, pace: float = 0.12) -> List[np.ndarray]:
    blocks: List[np.ndarray] = []
    cur_end = end_ms
    empty_streak = 0
    while cur_end > start_ms:
        _check_cancel(job)
        params = {"symbol": ref, "interval": "1m", "limit": 200,
                  "startTime": start_ms, "endTime": cur_end}
        rows = None
        for attempt in range(4):
            try:
                payload = await _get_json(session, BITUNIX_URL, params, timeout=20)
                if isinstance(payload, dict) and payload.get("code") == 0:
                    rows = payload.get("data") or []
                    break
            except Exception as e:  # noqa: BLE001
                logger.warning(f"bitunix {ref} attempt {attempt + 1} failed: {e}")
            await asyncio.sleep(1.0 * (attempt + 1))
        if rows is None:
            raise HistoryUnavailable(
                f"Bitunix-Historie für {ref} nicht erreichbar – bitte erneut starten.")
        if not rows:
            empty_streak += 1
            if empty_streak >= 2:
                break
            cur_end -= 200 * MINUTE
            continue
        empty_streak = 0
        m = np.array([[int(k["time"]), k["open"], k["high"], k["low"], k["close"],
                       k.get("baseVol") or 0.0] for k in rows], dtype=np.float64)
        blocks.append(m)
        oldest = int(m[:, 0].min())
        if oldest >= cur_end:  # kein Fortschritt -> Endlosschleife vermeiden
            break
        cur_end = oldest - MINUTE
        _progress(job, ref, start_ms + (end_ms - cur_end), start_ms, end_ms)
        await asyncio.sleep(pace)
    return blocks


# --------------------------------------------------------------------------
# Yahoo (Forex) – 1m nur ca. 30 Tage, max. 8 Tage pro Request
# --------------------------------------------------------------------------
async def fetch_yahoo(session, ref: str, start_ms: int, end_ms: int,
                      job: Dict = None, pace: float = 0.25) -> List[np.ndarray]:
    blocks: List[np.ndarray] = []
    cur = start_ms
    empty_streak = 0
    while cur < end_ms:
        _check_cancel(job)
        chunk_end = min(cur + YAHOO_CHUNK_MS, end_ms)
        params = {"interval": "1m", "period1": int(cur // 1000),
                  "period2": int(chunk_end // 1000)}
        rows = None
        for attempt in range(3):
            try:
                payload = await _get_json(session, YAHOO_URL.format(ref), params,
                                          timeout=25, headers=YAHOO_HEADERS)
                res = ((payload or {}).get("chart") or {}).get("result") or []
                rows = res[0] if res else {}
                break
            except Exception as e:  # noqa: BLE001
                logger.warning(f"yahoo {ref} attempt {attempt + 1} failed: {e}")
            await asyncio.sleep(1.0 * (attempt + 1))
        ts = (rows or {}).get("timestamp") if isinstance(rows, dict) else None
        if not ts:
            # Yahoo hält 1m-Daten nur ~30 Tage; ältere Fenster sind leer.
            empty_streak += 1
            if empty_streak >= 5:
                break
            cur = chunk_end
            continue
        empty_streak = 0
        q = ((rows.get("indicators") or {}).get("quote") or [{}])[0]
        o, h, l, c, v = (q.get("open"), q.get("high"), q.get("low"),
                         q.get("close"), q.get("volume"))
        vals = []
        for i in range(len(ts)):
            if o is None or c is None or o[i] is None or c[i] is None:
                continue  # Marktpause / Lücke
            hi = h[i] if h and h[i] is not None else o[i]
            lo = l[i] if l and l[i] is not None else o[i]
            vol = v[i] if v and v[i] is not None else 0.0
            vals.append([ts[i] * 1000, o[i], hi, lo, c[i],
                         vol or activity_volume(hi, lo, c[i])])
        if vals:
            blocks.append(np.array(vals, dtype=np.float64))
        cur = chunk_end
        _progress(job, ref, cur, start_ms, end_ms)
        await asyncio.sleep(pace)
    return blocks


SOURCES = {"binance": fetch_binance, "bitunix": fetch_bitunix, "yahoo": fetch_yahoo}

# Wartezeit pro Request und Parallel-Strom, so dass die Summe über alle Ströme
# im Ratelimit der jeweiligen API bleibt (~10-20 Requests/s).
PACE_PER_WORKER = {"binance": 0.05, "bitunix": 0.10, "yahoo": 0.10}


def pace_for(symbol: str, workers: int) -> float:
    return PACE_PER_WORKER.get(_resolve(symbol)[0], 0.1) * max(1, workers)


def _check_cancel(job: Optional[Dict]):
    if job is not None and job.get("cancel"):
        from services.backtester import JobCancelled
        raise JobCancelled()


async def fetch_blocks(session, symbol: str, start_ms: int, end_ms: int,
                       job: Dict = None, pace: float = None) -> List[np.ndarray]:
    """Rohblöcke der zuständigen Quelle (Matrix (N,6) je Block)."""
    source, ref = _resolve(symbol)
    fn = SOURCES[source]
    kwargs = {"job": job}
    if pace is not None:
        kwargs["pace"] = pace
    return await fn(session, ref, start_ms, end_ms, **kwargs)


def supports_parallel(symbol: str) -> bool:
    return _resolve(symbol)[0] in PARALLEL_SOURCES


def source_of(symbol: str) -> str:
    return _resolve(symbol)[0]
