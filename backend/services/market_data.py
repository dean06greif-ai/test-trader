"""
Multi-exchange market data feed with automatic fallback.

Priority order:
  1. Bitunix  (user's preferred broker)
  2. Binance  (public data mirror - reachable from most datacenter IPs, all coins)
  3. OKX      (secondary fallback)

Uses REST polling (no websocket) so it is robust against Cloudflare WAF blocks
and works for free from any server. All adapters return a UNIFIED candle list:
  [{"timestamp": ms, "open", "high", "low", "close", "volume"}, ...]  oldest-first
The LAST candle is the currently-forming minute; callers treat candles[:-1] as closed.
"""
import aiohttp
import logging
from typing import Dict, List, Optional

from services.history_sources import activity_volume

logger = logging.getLogger(__name__)


def _okx_instid(symbol: str) -> str:
    # BTCUSDT -> BTC-USDT-SWAP
    base = symbol[:-4] if symbol.endswith("USDT") else symbol
    return f"{base}-USDT-SWAP"


async def _get_json(session: aiohttp.ClientSession, url: str, params: dict):
    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}")
        return await resp.json(content_type=None)


async def fetch_bitunix(session, symbol: str, limit: int) -> List[Dict]:
    url = "https://fapi.bitunix.com/api/v1/futures/market/kline"
    payload = await _get_json(session, url, {"symbol": symbol, "interval": "1m", "limit": limit})
    if not payload or payload.get("code") != 0 or not payload.get("data"):
        return []
    out = []
    for k in payload["data"]:
        out.append({
            "timestamp": int(k["time"]),
            "open": float(k["open"]), "high": float(k["high"]),
            "low": float(k["low"]), "close": float(k["close"]),
            "volume": float(k.get("baseVol", 0) or 0),
        })
    out.sort(key=lambda c: c["timestamp"])
    return out


async def fetch_binance(session, symbol: str, limit: int) -> List[Dict]:
    # Public Binance data mirror (spot) - no auth, reachable from most datacenter IPs
    url = "https://data-api.binance.vision/api/v3/klines"
    data = await _get_json(session, url, {"symbol": symbol, "interval": "1m", "limit": limit})
    if not isinstance(data, list):
        return []
    out = []
    for k in data:  # already oldest-first
        out.append({
            "timestamp": int(k[0]),
            "open": float(k[1]), "high": float(k[2]),
            "low": float(k[3]), "close": float(k[4]),
            "volume": float(k[5]),
        })
    return out


async def fetch_okx(session, symbol: str, limit: int) -> List[Dict]:
    url = "https://www.okx.com/api/v5/market/candles"
    data = await _get_json(session, url, {"instId": _okx_instid(symbol), "bar": "1m", "limit": min(limit, 300)})
    rows = data.get("data") if isinstance(data, dict) else None
    if not rows:
        return []
    out = []
    for k in rows:  # newest-first
        out.append({
            "timestamp": int(k[0]),
            "open": float(k[1]), "high": float(k[2]),
            "low": float(k[3]), "close": float(k[4]),
            "volume": float(k[5]),
        })
    out.sort(key=lambda c: c["timestamp"])
    return out


async def fetch_yahoo(session, yahoo_symbol: str, yrange: str = "1d") -> List[Dict]:
    """
    Free Yahoo Finance 1-minute candles for commodities/metals
    (e.g. GC=F gold, SI=F silver, CL=F oil). Requires a browser User-Agent.
    Markets are not 24/7, so use range=5d for bootstrap to guarantee history.
    """
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}"
    data = await _get_json(session, url, {"interval": "1m", "range": yrange})
    try:
        res = data["chart"]["result"][0]
    except (KeyError, IndexError, TypeError):
        return []
    ts = res.get("timestamp")
    if not ts:
        return []
    q = res["indicators"]["quote"][0]
    o, h, l, c, v = q.get("open"), q.get("high"), q.get("low"), q.get("close"), q.get("volume")
    out = []
    for i in range(len(ts)):
        if c[i] is None or o[i] is None:
            continue  # skip gaps (market closed)
        hi, lo = float(h[i]), float(l[i])
        vol = float(v[i] or 0)
        out.append({
            "timestamp": int(ts[i]) * 1000,
            "open": float(o[i]), "high": hi,
            "low": lo, "close": float(c[i]),
            # Spot-FX hat kein Volumen -> Aktivitäts-Proxy wie in history_sources,
            # damit Live-Scanner und Backtester dieselben Indikatoren sehen.
            "volume": vol or activity_volume(hi, lo, float(c[i])),
        })
    return out  # oldest-first


SOURCES = [
    ("bitunix", fetch_bitunix),
    ("binance", fetch_binance),
    ("okx", fetch_okx),
]


class MarketDataFeed:
    """Picks the first reachable exchange and fetches klines from it, with fallback."""

    def __init__(self):
        self.active_source: Optional[str] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self.status = {
            "connected": False,
            "active_source": None,
            "last_error": None,
            "messages_received": 0,
            "reconnects": 0,
        }

    async def _session_get(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers = {
                "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
                "Accept": "application/json",
            }
            self._session = aiohttp.ClientSession(headers=headers)
        return self._session

    async def probe(self, test_symbol: str = "BTCUSDT") -> Optional[str]:
        """Find the first reachable source by fetching a couple of candles."""
        session = await self._session_get()
        for name, fn in SOURCES:
            try:
                candles = await fn(session, test_symbol, 3)
                if candles:
                    self.active_source = name
                    self.status.update({"connected": True, "active_source": name, "last_error": None})
                    logger.info(f"Market data source selected: {name}")
                    return name
            except Exception as e:
                logger.warning(f"Source {name} unavailable: {e}")
        self.active_source = None
        self.status.update({"connected": False, "active_source": None,
                            "last_error": "No reachable market data source"})
        logger.error("No reachable market data source (all blocked)")
        return None

    async def fetch(self, symbol: str, limit: int = 200) -> List[Dict]:
        """Fetch klines from the active source; re-probe and fallback on failure."""
        session = await self._session_get()

        # Ensure we have an active source
        if not self.active_source:
            await self.probe(symbol)
        if not self.active_source:
            return []

        fn = dict(SOURCES)[self.active_source]
        try:
            candles = await fn(session, symbol, limit)
            if candles:
                self.status["messages_received"] += 1
                self.status["connected"] = True
                self.status["last_error"] = None
                return candles
            return []
        except Exception as e:
            self.status["last_error"] = f"{self.active_source}: {e}"
            logger.warning(f"Fetch failed on {self.active_source} for {symbol}: {e} -> re-probing")
            self.status["reconnects"] += 1
            self.active_source = None
            self.status["connected"] = False
            await self.probe(symbol)
            if self.active_source:
                try:
                    return await dict(SOURCES)[self.active_source](session, symbol, limit)
                except Exception as e2:
                    self.status["last_error"] = f"{self.active_source}: {e2}"
            return []

    async def fetch_from(self, source: str, ref: str, limit: int = 200) -> List[Dict]:
        """Kerzen gezielt von EINER Börse holen (ohne Fallback-Kette).

        Nötig für Instrumente, die nur bei einer Quelle gelistet sind
        (z.B. QQQUSDT/SPYUSDT nur bei Bitunix).
        """
        fn = dict(SOURCES).get(source)
        if fn is None:
            logger.warning(f"Unknown market data source '{source}'")
            return []
        session = await self._session_get()
        try:
            candles = await fn(session, ref, limit)
            if candles:
                self.status["messages_received"] += 1
            return candles
        except Exception as e:  # noqa: BLE001
            self.status["last_error"] = f"{source}: {e}"
            logger.warning(f"Fetch failed on {source} for {ref}: {e}")
            return []

    async def fetch_commodity(self, yahoo_symbol: str, yrange: str = "1d") -> List[Dict]:
        """Fetch 1m candles for a commodity/metal from Yahoo Finance (free)."""
        session = await self._session_get()
        try:
            return await fetch_yahoo(session, yahoo_symbol, yrange)
        except Exception as e:
            logger.warning(f"Yahoo fetch failed for {yahoo_symbol}: {e}")
            return []

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
