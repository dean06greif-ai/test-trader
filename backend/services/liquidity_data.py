"""
Multi-exchange liquidity & liquidation context for the AI trader — 100% free, keyless.

Aggregates Binance + OKX + Bybit so the KI Trader can see WHERE institutions pick up
liquidity (long/short liquidation clusters, orderbook walls, long/short positioning)
=> better sweep / reversal trades.

Bundles everything into a compact per-symbol dict, exposed via one tool-call:
`await get_liquidity_context(symbols)` (analogous to `get_macro_context()`).

  1. long_short_ratios(symbol)  -> retail + top-trader + taker, aggregated across venues
  2. orderbook_liquidity(symbol)-> merged bid/ask walls + distance to price (liquidity magnets)
  3. liquidation_clusters(symbol)-> modelled long/short liq zones from aggregated OI + lev-tiers
  4. recent_liquidations(symbol)-> live liquidations from the 3-venue WS ring buffers
  5. get_liquidity_context(...) -> orchestrator, compact ~2 KB JSON

Data sources (all free, no API key):
  - Binance Futures  -> globalLongShortAccountRatio / topLongShortPositionRatio /
                        takerlongshortRatio / openInterestHist / fapi depth / WS !forceOrder@arr
  - OKX              -> rubik long-short ratio / public open-interest / market books /
                        WS liquidation-orders
  - Bybit V5         -> account-ratio / open-interest / orderbook / WS allLiquidation.{symbol}

Liquidation heat-map is APPROXIMATED locally (price + common leverage tiers 10/25/50/100x
+ aggregated OI) — the thing CoinGlass/Hyblock charge for, rebuilt keyless.

Every fetch is defensive (returns None / [] / {} on failure) and cached (ratios/OI ~5-10 min,
depth ~30-60 s) so a single dead venue never breaks the whole context. The live-liquidation
WebSocket collectors auto-start lazily inside this module (no scheduler wiring needed) and
auto-reconnect; if a venue is down it is simply skipped. No new dependencies (aiohttp only).
"""
import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
_HEADERS = {"User-Agent": _UA, "Accept": "application/json"}

# --------------------------------------------------------------------------- #
#  Config (env-flag toggles per venue — e.g. disable a venue by region)
# --------------------------------------------------------------------------- #
def _flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() not in ("0", "false", "no", "off")

ENABLED = {
    "binance": _flag("LIQ_ENABLE_BINANCE"),
    "okx": _flag("LIQ_ENABLE_OKX"),
    "bybit": _flag("LIQ_ENABLE_BYBIT"),
}

DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

TTL_RATIOS = 300      # 5 min  — ratios / OI move slowly
TTL_OI = 300          # 5 min
TTL_DEPTH = 45        # 45 s   — orderbook walls
TTL_CLUSTERS = 120    # 2 min  — modelled clusters (depend on price/OI)

LIQ_WINDOW_SEC = 300  # rolling window kept in the WS ring buffer (5 min)
# Größeres Fenster für die GEMESSENE Liquidations-Verteilung (echte Heatmap)
DIST_WINDOW_SEC = 4 * 3600
CASCADE_USD = 3_000_000  # >$3M dominant-side liqs in 5 min => cascade flag

# Common leverage tiers institutions/retail cluster around -> modelled liq zones.
LEVERAGE_TIERS = [
    (100, "high"),   # 100x  -> ~1% away, very dense retail cluster
    (50, "high"),    # 50x   -> ~2% away
    (25, "medium"),  # 25x   -> ~4% away
    (10, "low"),     # 10x   -> ~10% away
]
_MAINT_MARGIN = 0.005  # ~0.5% maintenance margin buffer baked into liq distance

# OKX SWAP contract value (base ccy per contract) for USD sizing; default 1.
_OKX_CTVAL = {
    "BTC": 0.01, "ETH": 0.1, "SOL": 1.0, "XRP": 100.0, "DOGE": 1000.0,
    "BNB": 0.01, "ADA": 100.0, "AVAX": 1.0, "LTC": 1.0, "LINK": 1.0,
    "MATIC": 10.0, "ARB": 10.0, "OP": 1.0, "SUI": 1.0, "TRX": 1000.0,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _base_ccy(symbol: str) -> str:
    """BTCUSDT -> BTC ."""
    s = symbol.upper()
    return s[:-4] if s.endswith("USDT") else s


def _okx_instid(symbol: str) -> str:
    return f"{_base_ccy(symbol)}-USDT-SWAP"


def _okx_ctval(symbol: str) -> float:
    return _OKX_CTVAL.get(_base_ccy(symbol), 1.0)


# --------------------------------------------------------------------------- #
#  Generic cached HTTP helpers (same pattern as macro_context.py)
# --------------------------------------------------------------------------- #
class _Cache:
    def __init__(self):
        self._store: Dict[str, tuple] = {}

    def get(self, key: str, ttl: int):
        v = self._store.get(key)
        if v and (time.time() - v[0]) < ttl:
            return v[1]
        return None

    def set(self, key: str, value):
        self._store[key] = (time.time(), value)


_cache = _Cache()


async def _get_json(session: aiohttp.ClientSession, url: str, params: dict = None,
                    headers: dict = None, timeout: int = 12):
    async with session.get(url, params=params, headers=headers or _HEADERS,
                           timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}")
        return await resp.json(content_type=None)


def _f(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _mean(vals: List[float]) -> Optional[float]:
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 4) if vals else None


# --------------------------------------------------------------------------- #
#  Reference price (Binance -> OKX -> Bybit)
# --------------------------------------------------------------------------- #
async def reference_price(session, symbol: str) -> Optional[float]:
    ck = f"px:{symbol}"
    cached = _cache.get(ck, TTL_DEPTH)
    if cached is not None:
        return cached
    price = None
    if ENABLED["binance"]:
        try:
            d = await _get_json(session, "https://fapi.binance.com/fapi/v1/ticker/price",
                                {"symbol": symbol})
            price = _f(d.get("price"))
        except Exception as e:
            logger.debug(f"binance price {symbol}: {e}")
    if price is None and ENABLED["okx"]:
        try:
            d = await _get_json(session, "https://www.okx.com/api/v5/market/ticker",
                                {"instId": _okx_instid(symbol)})
            price = _f((d.get("data") or [{}])[0].get("last"))
        except Exception as e:
            logger.debug(f"okx price {symbol}: {e}")
    if price is None and ENABLED["bybit"]:
        try:
            d = await _get_json(session, "https://api.bybit.com/v5/market/tickers",
                                {"category": "linear", "symbol": symbol})
            price = _f((d.get("result", {}).get("list") or [{}])[0].get("lastPrice"))
        except Exception as e:
            logger.debug(f"bybit price {symbol}: {e}")
    if price:
        _cache.set(ck, price)
    return price


# --------------------------------------------------------------------------- #
#  1) LONG / SHORT RATIOS  (retail + top-trader + taker, aggregated)
# --------------------------------------------------------------------------- #
async def _binance_ratios(session, symbol: str) -> Dict:
    out = {}
    base = "https://fapi.binance.com/futures/data"
    try:
        d = await _get_json(session, f"{base}/globalLongShortAccountRatio",
                            {"symbol": symbol, "period": "5m", "limit": 1})
        if d:
            out["retail"] = _f(d[-1].get("longShortRatio"))
    except Exception as e:
        logger.debug(f"binance retail {symbol}: {e}")
    try:
        d = await _get_json(session, f"{base}/topLongShortPositionRatio",
                            {"symbol": symbol, "period": "5m", "limit": 1})
        if d:
            out["top_trader_pos"] = _f(d[-1].get("longShortRatio"))
    except Exception as e:
        logger.debug(f"binance top {symbol}: {e}")
    try:
        d = await _get_json(session, f"{base}/takerlongshortRatio",
                            {"symbol": symbol, "period": "5m", "limit": 1})
        if d:
            out["taker_ratio"] = _f(d[-1].get("buySellRatio"))
    except Exception as e:
        logger.debug(f"binance taker {symbol}: {e}")
    return out


async def _okx_ratio(session, symbol: str) -> Optional[float]:
    try:
        d = await _get_json(
            session, "https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio",
            {"ccy": _base_ccy(symbol), "period": "5m"})
        rows = d.get("data") or []
        if rows:
            # rows: [[ts, ratio], ...] newest-first
            return _f(rows[0][1])
    except Exception as e:
        logger.debug(f"okx ratio {symbol}: {e}")
    return None


async def _bybit_ratio(session, symbol: str) -> Optional[float]:
    try:
        d = await _get_json(session, "https://api.bybit.com/v5/market/account-ratio",
                            {"category": "linear", "symbol": symbol, "period": "5min", "limit": 1})
        rows = d.get("result", {}).get("list") or []
        if rows:
            buy = _f(rows[0].get("buyRatio"))
            sell = _f(rows[0].get("sellRatio"))
            if buy is not None and sell:
                return round(buy / sell, 4)
    except Exception as e:
        logger.debug(f"bybit ratio {symbol}: {e}")
    return None


def _ls_bias(retail: Optional[float], top: Optional[float]) -> str:
    if retail is None and top is None:
        return "no positioning data"
    if retail is not None and top is not None:
        if retail > 1.1 and top < 0.95:
            return "retail long / top-trader short → long-squeeze risk"
        if retail < 0.9 and top > 1.05:
            return "retail short / top-trader long → short-squeeze risk"
        if retail > 1.1 and top > 1.1:
            return "retail + top-trader long → crowded long (downside sweep risk)"
        if retail < 0.9 and top < 0.9:
            return "retail + top-trader short → crowded short (upside sweep risk)"
        return "positioning mixed / balanced"
    r = retail if retail is not None else top
    who = "retail" if retail is not None else "top-trader"
    return f"{who} {'long' if r > 1 else 'short'}-leaning"


async def long_short_ratios(session, symbol: str) -> Dict:
    """Retail + top-trader + taker ratios, aggregated (mean) across enabled venues."""
    ck = f"ls:{symbol}"
    cached = _cache.get(ck, TTL_RATIOS)
    if cached is not None:
        return cached

    tasks, keys = [], []
    if ENABLED["binance"]:
        tasks.append(_binance_ratios(session, symbol)); keys.append("binance")
    if ENABLED["okx"]:
        tasks.append(_okx_ratio(session, symbol)); keys.append("okx")
    if ENABLED["bybit"]:
        tasks.append(_bybit_ratio(session, symbol)); keys.append("bybit")
    res = await asyncio.gather(*tasks, return_exceptions=True)
    got = dict(zip(keys, res))

    def _ok(k):
        v = got.get(k)
        return None if isinstance(v, Exception) else v

    binance = _ok("binance") or {}
    retail_vals, sources = [], []
    if binance.get("retail") is not None:
        retail_vals.append(binance["retail"]); sources.append("binance")
    okx_r = _ok("okx")
    if okx_r is not None:
        retail_vals.append(okx_r)
        if "okx" not in sources:
            sources.append("okx")
    bybit_r = _ok("bybit")
    if bybit_r is not None:
        retail_vals.append(bybit_r)
        if "bybit" not in sources:
            sources.append("bybit")

    retail = _mean(retail_vals)
    top = binance.get("top_trader_pos")
    taker = binance.get("taker_ratio")
    out = {
        "retail": retail,
        "top_trader_pos": top,
        "taker_ratio": taker,
        "bias": _ls_bias(retail, top),
        "sources": sources,
    }
    _cache.set(ck, out)
    return out


# --------------------------------------------------------------------------- #
#  Open interest + trend (aggregated magnitude; Binance history for trend)
# --------------------------------------------------------------------------- #
async def _binance_oi_hist(session, symbol: str) -> Dict:
    """Returns {'oi_usd': latest_usd, 'trend': rising/falling/flat}."""
    try:
        d = await _get_json(session, "https://fapi.binance.com/futures/data/openInterestHist",
                            {"symbol": symbol, "period": "5m", "limit": 8})
        if not d or len(d) < 2:
            return {}
        first = _f(d[0].get("sumOpenInterestValue"))
        last = _f(d[-1].get("sumOpenInterestValue"))
        trend = "flat"
        if first and last:
            chg = (last - first) / first
            trend = "rising" if chg > 0.01 else ("falling" if chg < -0.01 else "flat")
        return {"oi_usd": last, "trend": trend}
    except Exception as e:
        logger.debug(f"binance oi hist {symbol}: {e}")
        return {}


async def _okx_oi_usd(session, symbol: str, price: Optional[float]) -> Optional[float]:
    try:
        d = await _get_json(session, "https://www.okx.com/api/v5/public/open-interest",
                            {"instType": "SWAP", "instId": _okx_instid(symbol)})
        rows = d.get("data") or []
        if rows:
            oi_ccy = _f(rows[0].get("oiCcy"))  # in base coin
            if oi_ccy is not None and price:
                return oi_ccy * price
    except Exception as e:
        logger.debug(f"okx oi {symbol}: {e}")
    return None


async def _bybit_oi_usd(session, symbol: str, price: Optional[float]) -> Optional[float]:
    try:
        d = await _get_json(session, "https://api.bybit.com/v5/market/open-interest",
                            {"category": "linear", "symbol": symbol,
                             "intervalTime": "5min", "limit": 1})
        rows = d.get("result", {}).get("list") or []
        if rows:
            oi = _f(rows[0].get("openInterest"))  # in base coin
            if oi is not None and price:
                return oi * price
    except Exception as e:
        logger.debug(f"bybit oi {symbol}: {e}")
    return None


async def aggregated_oi(session, symbol: str, price: Optional[float]) -> Dict:
    """Aggregated OI in USD across venues + trend read (from Binance history)."""
    ck = f"oi:{symbol}"
    cached = _cache.get(ck, TTL_OI)
    if cached is not None:
        return cached
    tasks, keys = [], []
    if ENABLED["binance"]:
        tasks.append(_binance_oi_hist(session, symbol)); keys.append("binance")
    if ENABLED["okx"]:
        tasks.append(_okx_oi_usd(session, symbol, price)); keys.append("okx")
    if ENABLED["bybit"]:
        tasks.append(_bybit_oi_usd(session, symbol, price)); keys.append("bybit")
    res = await asyncio.gather(*tasks, return_exceptions=True)
    got = dict(zip(keys, res))

    total, trend = 0.0, "flat"
    venues = []
    b = got.get("binance")
    if isinstance(b, dict):
        if b.get("oi_usd"):
            total += b["oi_usd"]
            venues.append("binance")
        trend = b.get("trend", "flat")
    for k in ("okx", "bybit"):
        v = got.get(k)
        if isinstance(v, (int, float)):
            total += v
            venues.append(k)
    out = {"oi_usd": round(total) if total else None, "trend": trend,
           "venues": venues}
    _cache.set(ck, out)
    return out


# --------------------------------------------------------------------------- #
#  2) ORDERBOOK LIQUIDITY  (merged bid/ask walls + distance to price)
# --------------------------------------------------------------------------- #
async def _binance_depth(session, symbol: str) -> Dict:
    d = await _get_json(session, "https://fapi.binance.com/fapi/v1/depth",
                        {"symbol": symbol, "limit": 1000})
    return {"bids": [(_f(p), _f(q)) for p, q in d.get("bids", [])],
            "asks": [(_f(p), _f(q)) for p, q in d.get("asks", [])]}


async def _okx_depth(session, symbol: str) -> Dict:
    d = await _get_json(session, "https://www.okx.com/api/v5/market/books",
                        {"instId": _okx_instid(symbol), "sz": 400})
    row = (d.get("data") or [{}])[0]
    ct = _okx_ctval(symbol)  # sz is in contracts -> base coin
    return {"bids": [(_f(p), (_f(q) or 0) * ct) for p, q, *_ in row.get("bids", [])],
            "asks": [(_f(p), (_f(q) or 0) * ct) for p, q, *_ in row.get("asks", [])]}


async def _bybit_depth(session, symbol: str) -> Dict:
    d = await _get_json(session, "https://api.bybit.com/v5/market/orderbook",
                        {"category": "linear", "symbol": symbol, "limit": 200})
    r = d.get("result", {})
    return {"bids": [(_f(p), _f(q)) for p, q in r.get("b", [])],
            "asks": [(_f(p), _f(q)) for p, q in r.get("a", [])]}


def _merge_walls(levels: List[tuple], price: float, side: str,
                 max_dist_pct: float = 3.0, top_n: int = 3) -> List[Dict]:
    """Bucket levels (0.05% bins) across venues, keep biggest USD walls near price."""
    bucket_w = max(price * 0.0005, 1e-9)
    buckets: Dict[int, float] = {}
    for px, qty in levels:
        if px is None or qty is None:
            continue
        dist = abs(px - price) / price * 100
        if dist > max_dist_pct:
            continue
        if side == "bids" and px > price:
            continue
        if side == "asks" and px < price:
            continue
        b = int(px / bucket_w)
        buckets[b] = buckets.get(b, 0.0) + px * qty
    walls = [{"price": round(b * bucket_w, 2), "usd": round(usd),
              "dist_pct": round(abs(b * bucket_w - price) / price * 100, 2)}
             for b, usd in buckets.items()]
    walls.sort(key=lambda w: w["usd"], reverse=True)
    return walls[:top_n]


async def orderbook_liquidity(session, symbol: str, price: Optional[float] = None) -> Dict:
    """Merged bid/ask walls (aggregated across venues) + distance to price."""
    ck = f"ob:{symbol}"
    cached = _cache.get(ck, TTL_DEPTH)
    if cached is not None:
        return cached
    if price is None:
        price = await reference_price(session, symbol)
    if not price:
        return {"bids": [], "asks": [], "sources": []}

    tasks, keys = [], []
    if ENABLED["binance"]:
        tasks.append(_binance_depth(session, symbol)); keys.append("binance")
    if ENABLED["okx"]:
        tasks.append(_okx_depth(session, symbol)); keys.append("okx")
    if ENABLED["bybit"]:
        tasks.append(_bybit_depth(session, symbol)); keys.append("bybit")
    res = await asyncio.gather(*tasks, return_exceptions=True)

    all_bids, all_asks, sources = [], [], []
    for k, r in zip(keys, res):
        if isinstance(r, Exception) or not r:
            logger.debug(f"{k} depth {symbol}: {r}")
            continue
        all_bids += r.get("bids", [])
        all_asks += r.get("asks", [])
        sources.append(k)

    out = {
        "bids": _merge_walls(all_bids, price, "bids"),
        "asks": _merge_walls(all_asks, price, "asks"),
        "sources": sources,
    }
    _cache.set(ck, out)
    return out


# --------------------------------------------------------------------------- #
#  3) LIQUIDATION CLUSTERS  (modelled from price + leverage tiers + aggregated OI)
# --------------------------------------------------------------------------- #
def _model_clusters(price: float, oi_usd: Optional[float]) -> Dict:
    """Approximate long/short liq zones. Longs liq below price, shorts above.

    liq_distance ≈ 1/leverage - maintenance_margin. Strength weights the tier's
    typical crowding by aggregated OI magnitude.
    """
    oi_scale = 1.0
    if oi_usd:
        if oi_usd > 2e9:
            oi_scale = 1.3
        elif oi_usd < 3e8:
            oi_scale = 0.7

    below, above = [], []
    for lev, base_strength in LEVERAGE_TIERS:
        dist = (1.0 / lev) - _MAINT_MARGIN
        if dist <= 0:
            continue
        long_liq = round(price * (1 - dist), 2)   # longs get liquidated below
        short_liq = round(price * (1 + dist), 2)  # shorts get liquidated above
        strength = base_strength
        if oi_scale >= 1.3 and base_strength == "medium":
            strength = "high"
        elif oi_scale <= 0.7 and base_strength == "high":
            strength = "medium"
        below.append({"price": long_liq, "est_leverage": f"{lev}x",
                      "strength": strength, "dist_pct": round(dist * 100, 2),
                      "modelled": True})
        above.append({"price": short_liq, "est_leverage": f"{lev}x",
                      "strength": strength, "dist_pct": round(dist * 100, 2),
                      "modelled": True})
    # WICHTIG: Das sind KEINE gemessenen Liquidationen, sondern eine reine
    # Formel-Schätzung (Preis ± 1/Hebel) – Konsumenten müssen das kennzeichnen.
    return {"below_price": below, "above_price": above, "modelled": True}


async def liquidation_clusters(session, symbol: str, price: Optional[float] = None,
                               oi_usd: Optional[float] = None) -> Dict:
    """Modelled long/short liquidation clusters (CoinGlass-style, keyless approximation)."""
    ck = f"lc:{symbol}"
    cached = _cache.get(ck, TTL_CLUSTERS)
    if cached is not None:
        return cached
    if price is None:
        price = await reference_price(session, symbol)
    if not price:
        return {"below_price": [], "above_price": []}
    out = _model_clusters(price, oi_usd)
    _cache.set(ck, out)
    return out


# --------------------------------------------------------------------------- #
#  4) LIVE LIQUIDATIONS  — 3-venue WS ring buffers (self-contained, lazy-start)
# --------------------------------------------------------------------------- #
class _LiqBuffer:
    """Ring buffer of (ts_sec, side, usd, price) per symbol. side: 'long'|'short'."""
    def __init__(self):
        self._buf: Dict[str, List[tuple]] = {}

    def add(self, symbol: str, side: str, usd: float, price: float = 0.0):
        if not usd or usd <= 0:
            return
        now = time.time()
        b = self._buf.setdefault(symbol.upper(), [])
        b.append((now, side, usd, float(price or 0)))
        cutoff = now - DIST_WINDOW_SEC
        if b and b[0][0] < cutoff:
            self._buf[symbol.upper()] = [e for e in b if e[0] >= cutoff]

    def window(self, symbol: str, seconds: int = LIQ_WINDOW_SEC) -> Dict:
        now = time.time()
        cutoff = now - seconds
        long_usd = short_usd = 0.0
        cnt = 0
        for e in self._buf.get(symbol.upper(), []):
            if e[0] < cutoff:
                continue
            cnt += 1
            if e[1] == "long":
                long_usd += e[2]
            else:
                short_usd += e[2]
        cascade = (max(long_usd, short_usd) >= CASCADE_USD) and cnt >= 3
        return {"long_usd": round(long_usd), "short_usd": round(short_usd),
                "count": cnt, "cascade": cascade}

    def distribution(self, symbol: str, price: float, bins: int = 12,
                     seconds: int = 0) -> Dict:
        """GEMESSENE Liquidations-Verteilung: echte Force-Orders der Börsen zu
        Preis-Buckets verdichtet (Ersatz für die frühere Modell-Formel).
        Long-Liqs (unter dem aktuellen Preis passiert) und Short-Liqs getrennt."""
        seconds = seconds or DIST_WINDOW_SEC
        cutoff = time.time() - seconds
        rows = [e for e in self._buf.get(symbol.upper(), [])
                if e[0] >= cutoff and len(e) > 3 and e[3] > 0]
        out = {"below_price": [], "above_price": [], "measured": True,
               "window_h": round(seconds / 3600, 1),
               "total_usd": round(sum(e[2] for e in rows)), "events": len(rows)}
        if not rows or not price:
            return out
        prices = [e[3] for e in rows]
        lo, hi = min(prices), max(prices)
        width = (hi - lo) / bins if hi > lo else max(price * 0.001, 1e-9)

        def _bucketize(entries):
            buckets: Dict[int, Dict] = {}
            for _, _, usd, px in entries:
                i = min(bins - 1, max(0, int((px - lo) / width)))
                b = buckets.setdefault(i, {"usd": 0.0, "count": 0, "px_sum": 0.0})
                b["usd"] += usd
                b["count"] += 1
                b["px_sum"] += px
            ranked = sorted(buckets.values(), key=lambda b: -b["usd"])[:4]
            return [{"price": round(b["px_sum"] / b["count"], 6),
                     "usd": round(b["usd"]), "count": b["count"]} for b in ranked]

        out["below_price"] = _bucketize([e for e in rows if e[1] == "long"])
        out["above_price"] = _bucketize([e for e in rows if e[1] == "short"])
        return out


_liq_buf = _LiqBuffer()
_collectors_started = False
_collector_symbols = set(DEFAULT_SYMBOLS)


def register_symbols(symbols: List[str]):
    for s in symbols:
        _collector_symbols.add(s.upper())


async def _ws_loop(name: str, coro_factory):
    """Generic auto-reconnect wrapper — a dead venue never blocks the others."""
    backoff = 2
    while True:
        try:
            await coro_factory()
            backoff = 2
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(f"{name} ws reconnect in {backoff}s: {e}")
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60)


async def _binance_liq_ws():
    url = "wss://fstream.binance.com/ws/!forceOrder@arr"
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(url, heartbeat=30, timeout=20) as ws:
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    o = (msg.json() or {}).get("o", {})
                    sym = o.get("s")
                    if not sym:
                        continue
                    price = _f(o.get("ap")) or _f(o.get("p"))
                    qty = _f(o.get("q"))
                    if not price or not qty:
                        continue
                    # S=SELL -> a long position was force-sold (long liquidation)
                    side = "long" if o.get("S") == "SELL" else "short"
                    _liq_buf.add(sym, side, price * qty, price)
                except Exception:
                    continue


async def _okx_liq_ws():
    url = "wss://ws.okx.com:8443/ws/v5/public"
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(url, heartbeat=25, timeout=20) as ws:
            await ws.send_json({"op": "subscribe",
                                "args": [{"channel": "liquidation-orders", "instType": "SWAP"}]})
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    payload = msg.json() or {}
                    for row in payload.get("data", []) or []:
                        inst = row.get("instId", "")
                        base = inst.split("-")[0] if inst else ""
                        sym = f"{base}USDT"
                        ct = _OKX_CTVAL.get(base, 1.0)
                        for d in row.get("details", []) or []:
                            px = _f(d.get("bkPx")) or _f(d.get("px"))
                            sz = _f(d.get("sz"))
                            if not px or not sz:
                                continue
                            # OKX side 'sell' -> long position liquidated
                            side = "long" if d.get("side") == "sell" else "short"
                            _liq_buf.add(sym, side, px * sz * ct, px)
                except Exception:
                    continue


async def _bybit_liq_ws():
    url = "wss://stream.bybit.com/v5/public/linear"
    args = [f"allLiquidation.{s}" for s in sorted(_collector_symbols)]
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(url, heartbeat=20, timeout=20) as ws:
            await ws.send_json({"op": "subscribe", "args": args})
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    payload = msg.json() or {}
                    if payload.get("op") == "subscribe":
                        continue
                    for d in payload.get("data", []) or []:
                        sym = d.get("s")
                        px = _f(d.get("p"))
                        vol = _f(d.get("v"))
                        if not sym or not px or not vol:
                            continue
                        # Bybit 'S' = side of the liquidation order: Sell -> long liquidated
                        side = "long" if d.get("S") == "Sell" else "short"
                        _liq_buf.add(sym, side, px * vol, px)
                except Exception:
                    continue


def _ensure_collectors_started():
    """Lazily launch WS collectors on the running event loop (idempotent)."""
    global _collectors_started
    if _collectors_started:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _collectors_started = True
    if ENABLED["binance"]:
        loop.create_task(_ws_loop("binance-liq", _binance_liq_ws))
    if ENABLED["okx"]:
        loop.create_task(_ws_loop("okx-liq", _okx_liq_ws))
    if ENABLED["bybit"]:
        loop.create_task(_ws_loop("bybit-liq", _bybit_liq_ws))
    logger.info("liquidity_data: live-liquidation WS collectors started (%s)",
                ",".join(k for k, v in ENABLED.items() if v))


async def recent_liquidations(symbol: str, seconds: int = LIQ_WINDOW_SEC) -> Dict:
    """Live liquidations aggregated across the 3 venues over the rolling window."""
    _ensure_collectors_started()
    return _liq_buf.window(symbol, seconds)


async def measured_liq_distribution(symbol: str, price: float = 0.0,
                                    seconds: int = 0) -> Dict:
    """GEMESSENE Liquidations-Verteilung (echte Force-Orders, kein Modell)."""
    _ensure_collectors_started()
    return _liq_buf.distribution(symbol.upper(), price, seconds=seconds)


# --------------------------------------------------------------------------- #
#  MAIN:  get_liquidity_context()  — one tool-call, compact per-symbol dict
# --------------------------------------------------------------------------- #
async def _symbol_block(session, symbol: str) -> Dict:
    price = await reference_price(session, symbol)
    ls, oi, walls = await asyncio.gather(
        long_short_ratios(session, symbol),
        aggregated_oi(session, symbol, price),
        orderbook_liquidity(session, symbol, price),
        return_exceptions=True,
    )
    ls = {} if isinstance(ls, Exception) else ls
    oi = {} if isinstance(oi, Exception) else oi
    walls = {} if isinstance(walls, Exception) else walls
    clusters = await liquidation_clusters(session, symbol, price, oi.get("oi_usd"))
    liqs = await recent_liquidations(symbol)
    measured = await measured_liq_distribution(symbol, price)

    sources = sorted(set((ls.get("sources") or []) + (walls.get("sources") or [])))
    return {
        "price": price,
        "sources": sources or [k for k, v in ENABLED.items() if v],
        "long_short": {
            "retail": ls.get("retail"),
            "top_trader_pos": ls.get("top_trader_pos"),
            "taker_ratio": ls.get("taker_ratio"),
            "bias": ls.get("bias", "no positioning data"),
        },
        "oi_usd": oi.get("oi_usd"),
        "oi_trend": oi.get("trend", "flat"),
        "liq_clusters": clusters,
        "liq_clusters_measured": measured,
        "orderbook_walls": {"bids": walls.get("bids", []), "asks": walls.get("asks", [])},
        "recent_liquidations_5m": {
            "long_usd": liqs.get("long_usd", 0),
            "short_usd": liqs.get("short_usd", 0),
            "cascade": liqs.get("cascade", False),
        },
    }


async def get_liquidity_context(symbols: Optional[List[str]] = None) -> Dict:
    """Assemble the multi-exchange liquidity/liquidation context the KI Trader consumes.

    Analogous to `get_macro_context()`: every source fails soft, so this always
    returns a usable dict. Defaults to BTC/ETH/SOL to stay compact.
    """
    symbols = [s.upper() for s in (symbols or DEFAULT_SYMBOLS)]
    register_symbols(symbols)
    _ensure_collectors_started()

    async with aiohttp.ClientSession(headers=_HEADERS) as session:
        results = await asyncio.gather(
            *[_symbol_block(session, s) for s in symbols], return_exceptions=True)

    ctx = {}
    for s, r in zip(symbols, results):
        if isinstance(r, Exception):
            logger.debug(f"liquidity block {s}: {r}")
            continue
        ctx[s] = r
    ctx["generated_utc"] = _now_iso()
    ctx["venues_enabled"] = [k for k, v in ENABLED.items() if v]
    return ctx


# --------------------------------------------------------------------------- #
#  Manual smoke test:  python -m backend.services.liquidity_data
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import json

    async def _main():
        ctx = await get_liquidity_context(["BTCUSDT", "ETHUSDT"])
        print(json.dumps(ctx, indent=2))
        # give WS collectors a few seconds, then re-read live liqs
        await asyncio.sleep(8)
        print("BTC live liqs:", await recent_liquidations("BTCUSDT"))

    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
