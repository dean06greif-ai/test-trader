"""Liquiditäts-Endpoints: Liquidations-Heatmap + eigene „Liquidity Levels".

Zwei Datenquellen, beide FREI und ohne Fremd-Keys:
  * ``services/liquidity_data.py``  – Multi-Exchange-Kontext (Binance/OKX/Bybit):
    Long/Short-Ratios, Open Interest, Orderbook-Wände, modellierte
    Liquidations-Cluster, Live-Liquidationen.
  * ``services/liquidity_levels.py`` – aus Kerzen berechnete Liquiditäts-Level
    (Swings/EQH/EQL/FVG/Volumen-Profil) als Ersatz für „X-Ray Pro".

Der KI Trader nutzt denselben Kontext über ``AIEngine._liquidity_block()``.
"""
import logging
from typing import Optional

import aiohttp
from fastapi import APIRouter, HTTPException, Query

from services import liquidity_data as ld
from services import liquidity_levels as ll
from services import macro_context as mc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/liquidity", tags=["liquidity"])

DEFAULT_INTERVAL = "15m"
MAX_CANDLES = 400
CANDLE_TTL = 30          # s – Kerzen ändern sich intraday langsamer als Klicks
_candle_cache: dict = {}


async def _candles(symbol: str, interval: str, limit: int):
    """Kerzen mit kurzem TTL-Cache (verhindert Doppel-Requests bei Panel-Wechsel)."""
    import time
    key = (symbol.upper(), interval, min(limit, MAX_CANDLES))
    hit = _candle_cache.get(key)
    if hit and time.time() - hit[0] < CANDLE_TTL:
        return hit[1]
    data = await mc.historical_candles(symbol.upper(), interval=interval,
                                       limit=min(limit, MAX_CANDLES))
    candles = data.get("candles") or []
    if candles:
        _candle_cache[key] = (time.time(), candles)
        if len(_candle_cache) > 40:
            oldest = min(_candle_cache, key=lambda k: _candle_cache[k][0])
            _candle_cache.pop(oldest, None)
    return candles


@router.get("/context")
async def liquidity_context(symbols: Optional[str] = Query(
        None, description="Komma-Liste, z.B. BTCUSDT,ETHUSDT (Default BTC/ETH/SOL)")):
    """Kompletter Liquiditäts-Kontext (~2 KB) – genau der Block, den die KI sieht."""
    syms = [s.strip().upper() for s in symbols.split(",") if s.strip()] if symbols else None
    try:
        return await ld.get_liquidity_context(syms)
    except Exception as e:  # noqa: BLE001
        logger.error(f"liquidity context failed: {e}")
        raise HTTPException(status_code=502, detail=str(e)[:200])


@router.get("/levels/{symbol}")
async def liquidity_levels(symbol: str, interval: str = DEFAULT_INTERVAL,
                           limit: int = 300):
    """„Liquidity Levels" (X-Ray-Pro-Äquivalent) aus freien Kerzendaten."""
    try:
        candles = await _candles(symbol, interval, limit)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e)[:200])
    if not candles:
        raise HTTPException(status_code=502, detail="Keine Kerzendaten verfügbar")
    out = ll.liquidity_levels(candles)
    return {"symbol": symbol.upper(), "interval": interval,
            "candles": len(candles), **out}


@router.get("/heatmap/{symbol}")
async def liquidity_heatmap(symbol: str, interval: str = DEFAULT_INTERVAL,
                            limit: int = 300, bins: int = 40):
    """Liquidations-Heatmap (Eigenbau, keyless): modellierte Liquidations-Cluster
    + Volumen-Profil + Liquiditäts-Level zu Preis-Buckets verdichtet."""
    sym = symbol.upper()
    try:
        candles = await _candles(sym, interval, limit)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e)[:200])
    if not candles:
        raise HTTPException(status_code=502, detail="Keine Kerzendaten verfügbar")
    price = float(candles[-1]["close"])
    clusters, oi, walls, liqs = {}, {}, {}, {}
    async with aiohttp.ClientSession(headers=ld._HEADERS) as session:
        try:
            oi = await ld.aggregated_oi(session, sym, price)
            clusters = await ld.liquidation_clusters(session, sym, price, oi.get("oi_usd"))
            walls = await ld.orderbook_liquidity(session, sym, price)
        except Exception as e:  # noqa: BLE001 – Heatmap bleibt aus Kerzen nutzbar
            logger.warning(f"heatmap venue data failed {sym}: {e}")
    try:
        liqs = await ld.recent_liquidations(sym)
    except Exception:  # noqa: BLE001
        liqs = {}
    # GEMESSENE Liquidations-Verteilung (echte Force-Orders) hat Vorrang vor
    # den modellierten Clustern – letztere bleiben nur als Fallback für die UI.
    measured = {}
    try:
        measured = await ld.measured_liq_distribution(sym, price)
    except Exception:  # noqa: BLE001
        measured = {}
    hm_clusters = measured if (measured.get("below_price") or measured.get("above_price")) \
        else clusters
    hm = ll.heatmap(candles, hm_clusters, price, bins=max(10, min(bins, 80)))
    return {"symbol": sym, "interval": interval, "clusters": clusters,
            "clusters_measured": measured,
            "clusters_source": "measured" if hm_clusters is measured else "model",
            "oi_usd": oi.get("oi_usd"), "oi_trend": oi.get("trend"),
            "oi_venues": oi.get("venues") or [],
            "orderbook_walls": {"bids": walls.get("bids", []),
                                "asks": walls.get("asks", [])},
            "recent_liquidations_5m": liqs, **hm}


@router.get("/live/{symbol}")
async def liquidity_live(symbol: str, seconds: int = 300):
    """Live-Liquidationen (3-Venue-WebSocket-Ringpuffer) der letzten Sekunden."""
    return {"symbol": symbol.upper(), "seconds": seconds,
            **await ld.recent_liquidations(symbol.upper(), seconds)}
