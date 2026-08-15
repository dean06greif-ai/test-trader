"""
Macro / external context endpoints for the KI Trader.

The AI only needs ONE call:  GET /api/macro/context  ->  get_macro_context()
The remaining endpoints expose each source individually (chart / debugging / UI).
All data is free & keyless (OKX, Yahoo, Coinpaprika, TradingView, trumpstruth.org).
"""
import logging
from typing import Optional

import aiohttp
from fastapi import APIRouter, HTTPException, Query

from services import macro_context as mc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/macro", tags=["macro"])


@router.get("/context")
async def macro_context_endpoint(
    symbols: Optional[str] = Query(None, description="Comma list, e.g. BTCUSDT,ETHUSDT. Default BTC/ETH/SOL"),
    key_levels: bool = Query(True),
    trump: bool = Query(True),
):
    """Full external context (~2 KB) for one analysis cycle — the AI tool-call."""
    syms = [s.strip().upper() for s in symbols.split(",") if s.strip()] if symbols else None
    try:
        return await mc.get_macro_context(symbols=syms, include_key_levels=key_levels,
                                          include_trump=trump)
    except Exception as e:
        logger.error(f"macro context failed: {e}")
        raise HTTPException(status_code=502, detail=str(e)[:200])


@router.get("/market")
async def macro_market():
    """BTC dominance + total market cap + DXY + US 10Y yield (live)."""
    async with aiohttp.ClientSession(headers=mc._HEADERS) as s:
        return await mc.market_regime(s)


@router.get("/calendar")
async def macro_cal(hours_ahead: int = 48):
    """Macro calendar (CPI/FOMC/PMI/speeches, US) + no-trade windows in UTC."""
    async with aiohttp.ClientSession(headers=mc._HEADERS) as s:
        return await mc.macro_calendar(s, hours_ahead=hours_ahead)


@router.get("/funding/{symbol}")
async def macro_funding(symbol: str):
    """Funding rate + open interest + OI-delta (15m/1h/4h) for one symbol."""
    async with aiohttp.ClientSession(headers=mc._HEADERS) as s:
        return await mc.funding_and_oi(s, symbol.upper())


@router.get("/key-levels/{symbol}")
async def macro_key_levels(symbol: str):
    """4h + Daily Support/Resistance/POC/VAH/VAL for one symbol."""
    async with aiohttp.ClientSession(headers=mc._HEADERS) as s:
        return {"symbol": symbol.upper(), "levels": await mc.key_levels(s, symbol.upper())}


@router.get("/trump")
async def macro_trump(limit: int = 5):
    """Latest Donald Trump Truth-Social posts with market-relevance scan."""
    async with aiohttp.ClientSession(headers=mc._HEADERS) as s:
        return await mc.trump_truth_social(s, limit=limit)


@router.get("/history/{symbol}")
async def macro_history(symbol: str, interval: str = "1d", limit: int = 200):
    """Historical candles for coins OR Gold/Silver/Oil (interval 1m..1d)."""
    try:
        return await mc.historical_candles(symbol.upper(), interval=interval, limit=limit)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)[:200])
