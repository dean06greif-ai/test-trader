import asyncio
import json
import websockets
import aiohttp
from typing import Callable, Dict, List
import logging

logger = logging.getLogger(__name__)

# Bitunix public endpoints (no API key required for market data)
WS_URL = "wss://fapi.bitunix.com/public/"
REST_KLINE_URL = "https://fapi.bitunix.com/api/v1/futures/market/kline"


class BitunixWebSocketClient:
    """WebSocket + REST client for Bitunix futures market data."""

    def __init__(self):
        self.ws_url = WS_URL
        self.connections = {}
        self.callbacks = {}
        self.running = False
        # Observability: lets the /api/debug/status endpoint report data flow
        self.status = {
            "connected": False,
            "last_error": None,
            "last_message_at": None,
            "messages_received": 0,
            "reconnects": 0,
        }

    async def fetch_historical_klines(self, symbol: str, interval: str = "1m", limit: int = 200) -> List[Dict]:
        """
        Fetch historical closed candles via REST so indicators work immediately
        (instead of waiting ~60 minutes for the live buffer to fill).

        Bitunix REST response format:
        {"code":0,"data":[{"open","high","low","close","quoteVol","baseVol","time"}, ...]}
        Data is returned newest-first, so we sort ascending (oldest-first).
        """
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(REST_KLINE_URL, params=params, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    payload = await resp.json()
        except Exception as e:
            logger.error(f"Failed to fetch historical klines for {symbol}: {e}")
            return []

        if not payload or payload.get("code") != 0 or not payload.get("data"):
            logger.warning(f"No historical klines returned for {symbol}: {str(payload)[:200]}")
            return []

        candles = []
        for k in payload["data"]:
            try:
                candles.append({
                    "timestamp": int(k["time"]),
                    "open": float(k["open"]),
                    "high": float(k["high"]),
                    "low": float(k["low"]),
                    "close": float(k["close"]),
                    "volume": float(k.get("baseVol", 0) or 0),
                })
            except (KeyError, ValueError, TypeError) as e:
                logger.debug(f"Skipping malformed kline for {symbol}: {e}")

        candles.sort(key=lambda c: c["timestamp"])  # oldest first
        logger.info(f"Fetched {len(candles)} historical candles for {symbol}")
        return candles

    async def connect_symbol(self, symbol: str, callback: Callable):
        """Deprecated single-symbol connect (kept for compatibility)."""
        await self.start([symbol], callback)

    async def _run(self, symbols: List[str], callback: Callable):
        """
        Maintain ONE websocket connection subscribed to all symbols.
        Using a single connection avoids Bitunix HTTP 429 rate limiting that
        occurs when opening many parallel connections.
        """
        backoff = 5
        while self.running:
            try:
                async with websockets.connect(self.ws_url, ping_interval=20, ping_timeout=20) as websocket:
                    self.connections["main"] = websocket
                    backoff = 5  # reset after a successful connect
                    self.status["connected"] = True
                    self.status["last_error"] = None

                    subscribe_message = {
                        "op": "subscribe",
                        "args": [{"ch": "market_kline_1min", "symbol": s} for s in symbols],
                    }
                    await websocket.send(json.dumps(subscribe_message))
                    logger.info(f"Subscribed to {len(symbols)} symbols on single connection")

                    while self.running:
                        try:
                            message = await asyncio.wait_for(websocket.recv(), timeout=30.0)
                            data = json.loads(message)

                            if data.get("ch") != "market_kline_1min":
                                continue

                            symbol = data.get("symbol")
                            k = data.get("data")
                            if not symbol or not isinstance(k, dict) or "c" not in k:
                                continue

                            ts = int(data.get("ts", 0))
                            minute_bucket = (ts // 60000) * 60000

                            candle = {
                                "timestamp": minute_bucket,
                                "open": float(k["o"]),
                                "high": float(k["h"]),
                                "low": float(k["l"]),
                                "close": float(k["c"]),
                                "volume": float(k.get("b", 0) or 0),
                            }

                            self.status["messages_received"] += 1
                            self.status["last_message_at"] = ts
                            await callback(symbol, candle)

                        except asyncio.TimeoutError:
                            try:
                                await websocket.send(json.dumps({"op": "ping"}))
                            except Exception:
                                break

            except Exception as e:
                self.status["connected"] = False
                self.status["last_error"] = str(e)
                logger.error(f"WebSocket connection error: {e}")

            if self.running:
                self.status["reconnects"] += 1
                logger.info(f"Reconnecting in {backoff}s...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)  # exponential backoff, capped

    async def start(self, symbols: List[str], callback: Callable):
        """Start a single WebSocket connection for all symbols."""
        self.running = True
        self.callbacks["main"] = callback
        await self._run(symbols, callback)

    async def stop(self):
        """Stop all WebSocket connections."""
        self.running = False
        for symbol, ws in list(self.connections.items()):
            try:
                await ws.close()
                logger.info(f"Closed connection for {symbol}")
            except Exception as e:
                logger.error(f"Error closing connection for {symbol}: {e}")
        self.connections.clear()
        self.callbacks.clear()
