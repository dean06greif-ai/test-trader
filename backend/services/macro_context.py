"""
Macro / external context provider for the AI trader — 100% free, keyless sources.

Bundles everything the KI Trader asked for into a single ~2 KB JSON per 10-min
analysis cycle, exposed via `get_macro_context()` (one tool-call):

  1. Key-Levels (4h / Daily): Support, Resistance, POC, VAH/VAL  -> precise SL/TP
  2. Funding Rate + OI-Delta (15m / 1h / 4h)                     -> squeeze / trend
  3. Macro calendar (CPI, FOMC, PMI, speeches) with exact UTC     -> no-trade windows
  4. BTC-Dominance + DXY + US 10Y Yield (live)                    -> bias / risk budget
  + Trump / Truth Social latest posts (market-relevance scan)
  + Historical candles for coins and Gold / Silver / Oil

Data sources (all free, no API key, reachable from restricted datacenter IPs):
  - OKX public API            -> klines, funding rate, open interest (+ history)
  - Binance data mirror       -> spot klines fallback
  - Yahoo Finance             -> DXY (DX-Y.NYB), US 10Y (^TNX), metals/oil
  - Coinpaprika               -> BTC dominance / total market cap
  - TradingView econ calendar -> CPI / FOMC / PMI / speeches (UTC)
  - trumpstruth.org RSS       -> Donald Trump Truth-Social posts archive

Every fetch is defensive (returns None / [] on failure) and cached (10 min) so a
single dead source never breaks the whole context. No new dependencies.
"""
import asyncio
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
_HEADERS = {"User-Agent": _UA, "Accept": "application/json"}

CACHE_TTL = 600  # 10 min == one analysis cycle

# Words that make a Trump post potentially market-moving.
_MARKET_KEYWORDS = [
    "tariff", "tariffs", "trade", "china", "fed", "federal reserve", "powell",
    "interest rate", "rates", "inflation", "cpi", "crypto", "bitcoin", "btc",
    "dollar", "oil", "gold", "sanction", "sanctions", "tax", "taxes", "economy",
    "recession", "stock", "market", "war", "russia", "iran", "opec", "energy",
    "gas", "deal", "import", "export", "semiconductor", "chips", "ai",
]

# symbol -> yahoo ticker for commodities / metals
_COMMODITY_YAHOO = {"GOLD": "GC=F", "SILVER": "SI=F", "OIL": "CL=F"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _base_ccy(symbol: str) -> str:
    """BTCUSDT -> BTC ."""
    s = symbol.upper()
    return s[:-4] if s.endswith("USDT") else s


def _okx_instid(symbol: str) -> str:
    return f"{_base_ccy(symbol)}-USDT-SWAP"


# --------------------------------------------------------------------------- #
#  Generic cached HTTP helpers
# --------------------------------------------------------------------------- #
class _Cache:
    def __init__(self):
        self._store: Dict[str, tuple] = {}

    def get(self, key: str):
        v = self._store.get(key)
        if v and (time.time() - v[0]) < CACHE_TTL:
            return v[1]
        return None

    def set(self, key: str, value):
        self._store[key] = (time.time(), value)


_cache = _Cache()


async def _get_json(session: aiohttp.ClientSession, url: str, params: dict = None,
                    headers: dict = None, timeout: int = 12):
    async with session.get(url, params=params, headers=headers,
                           timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}")
        return await resp.json(content_type=None)


async def _get_text(session: aiohttp.ClientSession, url: str, timeout: int = 12) -> str:
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}")
        return await resp.text()


# --------------------------------------------------------------------------- #
#  Candles (OKX primary, Binance mirror fallback) — used for key-levels/history
# --------------------------------------------------------------------------- #
_OKX_BAR = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1H", "4h": "4H", "1d": "1D"}
_BINANCE_INT = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h", "1d": "1d"}


async def fetch_klines(session, symbol: str, interval: str = "4h", limit: int = 200) -> List[Dict]:
    """Unified candles list (oldest-first). OKX first, Binance mirror fallback."""
    # OKX
    try:
        bar = _OKX_BAR.get(interval, "4H")
        data = await _get_json(session, "https://www.okx.com/api/v5/market/candles",
                               {"instId": _okx_instid(symbol), "bar": bar,
                                "limit": min(limit, 300)}, headers=_HEADERS)
        rows = data.get("data") if isinstance(data, dict) else None
        if rows:
            out = [{"timestamp": int(k[0]), "open": float(k[1]), "high": float(k[2]),
                    "low": float(k[3]), "close": float(k[4]), "volume": float(k[5])}
                   for k in rows]
            out.sort(key=lambda c: c["timestamp"])
            return out
    except Exception as e:
        logger.debug(f"OKX klines failed {symbol} {interval}: {e}")
    # Binance mirror
    try:
        bint = _BINANCE_INT.get(interval, "4h")
        data = await _get_json(session, "https://data-api.binance.vision/api/v3/klines",
                               {"symbol": symbol, "interval": bint, "limit": min(limit, 1000)},
                               headers=_HEADERS)
        if isinstance(data, list):
            return [{"timestamp": int(k[0]), "open": float(k[1]), "high": float(k[2]),
                     "low": float(k[3]), "close": float(k[4]), "volume": float(k[5])}
                    for k in data]
    except Exception as e:
        logger.debug(f"Binance klines failed {symbol} {interval}: {e}")
    return []


async def fetch_yahoo_candles(session, yahoo_symbol: str, interval: str = "1d",
                              yrange: str = "3mo") -> List[Dict]:
    """Free Yahoo Finance candles for commodities / indices."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}"
    data = await _get_json(session, url, {"interval": interval, "range": yrange},
                           headers=_HEADERS)
    try:
        res = data["chart"]["result"][0]
        ts = res["timestamp"]
        q = res["indicators"]["quote"][0]
    except (KeyError, IndexError, TypeError):
        return []
    o, h, l, c, v = q.get("open"), q.get("high"), q.get("low"), q.get("close"), q.get("volume")
    out = []
    for i in range(len(ts)):
        if c[i] is None or o[i] is None:
            continue
        out.append({"timestamp": int(ts[i]) * 1000, "open": float(o[i]),
                    "high": float(h[i]), "low": float(l[i]), "close": float(c[i]),
                    "volume": float(v[i] or 0)})
    return out


# --------------------------------------------------------------------------- #
#  1) KEY-LEVELS  (Support / Resistance / POC / VAH / VAL)
# --------------------------------------------------------------------------- #
def _pivot_levels(candles: List[Dict], left: int = 3, right: int = 3,
                  max_levels: int = 4) -> Dict[str, List[float]]:
    """Swing highs / lows split into support (below price) and resistance (above)."""
    if len(candles) < left + right + 1:
        return {"support": [], "resistance": []}
    highs, lows = [], []
    for i in range(left, len(candles) - right):
        win = candles[i - left:i + right + 1]
        c = candles[i]
        if c["high"] == max(w["high"] for w in win):
            highs.append(c["high"])
        if c["low"] == min(w["low"] for w in win):
            lows.append(c["low"])
    price = candles[-1]["close"]

    def _cluster(vals: List[float]) -> List[float]:
        if not vals:
            return []
        vals = sorted(vals)
        tol = price * 0.004  # merge levels within 0.4 %
        clustered, group = [], [vals[0]]
        for v in vals[1:]:
            if v - group[-1] <= tol:
                group.append(v)
            else:
                clustered.append(sum(group) / len(group))
                group = [v]
        clustered.append(sum(group) / len(group))
        return clustered

    res = [round(v, 6) for v in _cluster([h for h in highs if h > price])][:max_levels]
    sup = [round(v, 6) for v in _cluster([l for l in lows if l < price])][-max_levels:]
    return {"support": sorted(sup, reverse=True), "resistance": sorted(res)}


def _volume_profile(candles: List[Dict], bins: int = 40) -> Dict[str, Optional[float]]:
    """POC + Value Area (70 %) from a simple typical-price volume distribution."""
    if len(candles) < 10:
        return {"poc": None, "vah": None, "val": None}
    lo = min(c["low"] for c in candles)
    hi = max(c["high"] for c in candles)
    if hi <= lo:
        return {"poc": None, "vah": None, "val": None}
    width = (hi - lo) / bins
    vol_bins = [0.0] * bins
    for c in candles:
        tp = (c["high"] + c["low"] + c["close"]) / 3.0
        idx = min(bins - 1, max(0, int((tp - lo) / width)))
        vol_bins[idx] += c.get("volume", 0) or 0
    total = sum(vol_bins)
    if total <= 0:
        return {"poc": None, "vah": None, "val": None}
    poc_idx = max(range(bins), key=lambda i: vol_bins[i])
    # expand value area around POC until 70 % of volume is covered
    covered = vol_bins[poc_idx]
    lo_idx = hi_idx = poc_idx
    while covered < total * 0.70 and (lo_idx > 0 or hi_idx < bins - 1):
        down = vol_bins[lo_idx - 1] if lo_idx > 0 else -1
        up = vol_bins[hi_idx + 1] if hi_idx < bins - 1 else -1
        if up >= down:
            hi_idx += 1
            covered += max(up, 0)
        else:
            lo_idx -= 1
            covered += max(down, 0)

    def _price(i):
        return round(lo + (i + 0.5) * width, 6)

    return {"poc": _price(poc_idx), "vah": _price(hi_idx), "val": _price(lo_idx)}


async def key_levels(session, symbol: str) -> Dict:
    """4h + Daily key structure for one symbol (coins or metals/oil)."""
    ck = f"keylevels:{symbol}"
    cached = _cache.get(ck)
    if cached is not None:
        return cached
    out = {}
    is_commodity = symbol.upper() in _COMMODITY_YAHOO
    for tf, (yint, yrange, klimit) in {
        "4h": ("60m", "1mo", 180),   # yahoo has no native 4h -> 60m
        "1d": ("1d", "6mo", 180),
    }.items():
        try:
            if is_commodity:
                candles = await fetch_yahoo_candles(session, _COMMODITY_YAHOO[symbol.upper()],
                                                    yint, yrange)
            else:
                candles = await fetch_klines(session, symbol, tf, klimit)
            if not candles:
                continue
            piv = _pivot_levels(candles)
            vp = _volume_profile(candles)
            out[tf] = {**piv, **vp, "last": round(candles[-1]["close"], 6)}
        except Exception as e:
            logger.debug(f"key_levels {symbol} {tf}: {e}")
    _cache.set(ck, out)
    return out


# --------------------------------------------------------------------------- #
#  2) FUNDING RATE + OI-DELTA (15m / 1h / 4h)  — OKX public
# --------------------------------------------------------------------------- #
async def funding_and_oi(session, symbol: str) -> Dict:
    ck = f"funding:{symbol}"
    cached = _cache.get(ck)
    if cached is not None:
        return cached
    inst = _okx_instid(symbol)
    ccy = _base_ccy(symbol)
    res: Dict = {"symbol": symbol}
    # current funding rate (OKX settles every 8h)
    try:
        d = await _get_json(session, "https://www.okx.com/api/v5/public/funding-rate",
                            {"instId": inst}, headers=_HEADERS)
        row = (d.get("data") or [{}])[0]
        fr = float(row.get("fundingRate") or 0)
        res["funding_rate"] = round(fr, 8)
        res["funding_annualized_pct"] = round(fr * 3 * 365 * 100, 2)  # 3 settlements/day
        nf = row.get("nextFundingRate")
        res["next_funding_rate"] = round(float(nf), 8) if nf not in (None, "") else None
    except Exception as e:
        logger.debug(f"funding {symbol}: {e}")
    # current open interest (USD)
    try:
        d = await _get_json(session, "https://www.okx.com/api/v5/public/open-interest",
                            {"instType": "SWAP", "instId": inst}, headers=_HEADERS)
        row = (d.get("data") or [{}])[0]
        res["oi_usd"] = round(float(row.get("oiUsd") or 0), 0)
    except Exception as e:
        logger.debug(f"oi {symbol}: {e}")
    # OI history (5-min buckets) -> deltas over 15m / 1h / 4h
    try:
        d = await _get_json(session, "https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-volume",
                            {"ccy": ccy, "period": "5m"}, headers=_HEADERS)
        rows = d.get("data") or []  # newest-first: [ts, oiUsd, volUsd]
        oi = [float(r[1]) for r in rows]
        if len(oi) >= 2:
            def _delta(n):
                if len(oi) > n and oi[n]:
                    return round((oi[0] - oi[n]) / oi[n] * 100, 2)
                return None
            res["oi_delta_15m_pct"] = _delta(3)
            res["oi_delta_1h_pct"] = _delta(12)
            res["oi_delta_4h_pct"] = _delta(48)
    except Exception as e:
        logger.debug(f"oi_hist {symbol}: {e}")
    # squeeze read for the AI
    fr = res.get("funding_rate")
    d1h = res.get("oi_delta_1h_pct")
    if fr is not None and d1h is not None:
        if fr > 0.0005 and d1h > 1.5:
            res["squeeze_bias"] = "long-crowded (long-squeeze risk)"
        elif fr < -0.0005 and d1h > 1.5:
            res["squeeze_bias"] = "short-crowded (short-squeeze fuel)"
        else:
            res["squeeze_bias"] = "balanced"
    _cache.set(ck, res)
    return res


# --------------------------------------------------------------------------- #
#  3) MACRO CALENDAR (TradingView econ calendar, free)
# --------------------------------------------------------------------------- #
_CAL_IMPORTANCE = {1: "high", 0: "medium", -1: "low"}


async def macro_calendar(session, hours_ahead: int = 48) -> Dict:
    ck = f"calendar:{hours_ahead}"
    cached = _cache.get(ck)
    if cached is not None:
        return cached
    now = datetime.now(timezone.utc)
    frm = (now - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    to = (now + timedelta(hours=hours_ahead)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    out = {"generated_utc": _now_iso(), "no_trade_windows_utc": [], "upcoming": []}
    try:
        d = await _get_json(session, "https://economic-calendar.tradingview.com/events",
                            {"from": frm, "to": to, "countries": "US"},
                            headers={**_HEADERS, "Origin": "https://www.tradingview.com",
                                     "Referer": "https://www.tradingview.com/"})
        events = d.get("result") or []
        parsed = []
        for e in events:
            imp = e.get("importance")
            title = e.get("title") or e.get("indicator") or ""
            t = e.get("date")
            if not t or imp is None:
                continue
            parsed.append({"time_utc": t, "event": title,
                           "importance": _CAL_IMPORTANCE.get(int(imp), "low"),
                           "country": e.get("country")})
        parsed.sort(key=lambda x: x["time_utc"])
        out["upcoming"] = parsed[:12]
        # no-trade windows: +/- 15 min around every HIGH-impact event still in the future
        for e in parsed:
            if e["importance"] != "high":
                continue
            try:
                dt = datetime.fromisoformat(e["time_utc"].replace("Z", "+00:00"))
            except Exception:
                continue
            if dt < now - timedelta(minutes=15):
                continue
            out["no_trade_windows_utc"].append({
                "event": e["event"],
                "start_utc": (dt - timedelta(minutes=15)).isoformat(timespec="seconds"),
                "end_utc": (dt + timedelta(minutes=15)).isoformat(timespec="seconds"),
            })
        out["no_trade_windows_utc"] = out["no_trade_windows_utc"][:6]
    except Exception as e:
        logger.debug(f"macro_calendar: {e}")
    _cache.set(ck, out)
    return out


# --------------------------------------------------------------------------- #
#  4) MARKET REGIME:  BTC-Dominance + DXY + US 10Y Yield
# --------------------------------------------------------------------------- #
async def _yahoo_quote(session, yahoo_symbol: str) -> Optional[Dict]:
    try:
        candles = await fetch_yahoo_candles(session, yahoo_symbol, "1d", "5d")
        if len(candles) >= 2:
            last, prev = candles[-1]["close"], candles[-2]["close"]
            chg = (last - prev) / prev * 100 if prev else 0
            return {"value": round(last, 3), "chg_pct": round(chg, 2)}
        if candles:
            return {"value": round(candles[-1]["close"], 3), "chg_pct": None}
    except Exception as e:
        logger.debug(f"yahoo quote {yahoo_symbol}: {e}")
    return None


async def market_regime(session) -> Dict:
    ck = "market_regime"
    cached = _cache.get(ck)
    if cached is not None:
        return cached
    out: Dict = {"generated_utc": _now_iso()}
    # BTC dominance / total mcap (Coinpaprika, no key)
    try:
        d = await _get_json(session, "https://api.coinpaprika.com/v1/global", headers=_HEADERS)
        out["btc_dominance_pct"] = round(float(d.get("bitcoin_dominance_percentage") or 0), 2)
        out["total_mcap_usd"] = int(d.get("market_cap_usd") or 0)
        out["mcap_change_24h_pct"] = d.get("market_cap_change_24h")
    except Exception as e:
        logger.debug(f"dominance: {e}")
    dxy, us10y = await asyncio.gather(
        _yahoo_quote(session, "DX-Y.NYB"),
        _yahoo_quote(session, "%5ETNX"),
    )
    out["dxy"] = dxy
    out["us10y_yield"] = us10y
    # simple risk read for the AI
    bias = []
    if dxy and dxy.get("chg_pct") is not None:
        bias.append("USD stark → Risk-off" if dxy["chg_pct"] > 0.2 else
                    ("USD schwach → Risk-on" if dxy["chg_pct"] < -0.2 else "USD neutral"))
    if us10y and us10y.get("chg_pct") is not None:
        bias.append("Yields steigen → Gegenwind" if us10y["chg_pct"] > 1 else
                    ("Yields fallen → Rückenwind" if us10y["chg_pct"] < -1 else "Yields neutral"))
    out["risk_bias"] = " | ".join(bias) if bias else "neutral"
    _cache.set(ck, out)
    return out


# --------------------------------------------------------------------------- #
#  5) TRUMP / TRUTH SOCIAL  (trumpstruth.org RSS archive)
# --------------------------------------------------------------------------- #
def _strip_html(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


async def trump_truth_social(session, limit: int = 5) -> Dict:
    ck = f"trump:{limit}"
    cached = _cache.get(ck)
    if cached is not None:
        return cached
    out = {"generated_utc": _now_iso(), "count": 0, "market_relevant": False, "latest": []}
    try:
        text = await _get_text(session, "https://trumpstruth.org/feed")
        root = ET.fromstring(text)
        posts = []
        for item in root.iter("item"):
            body = _strip_html(item.findtext("description") or "")
            if len(body) < 12 or body.lower().startswith("rt:"):
                continue
            low = body.lower()
            kws = sorted({k for k in _MARKET_KEYWORDS
                          if re.search(r"\b" + re.escape(k) + r"\b", low)})
            posts.append({
                "time_utc": (item.findtext("pubDate") or "").strip(),
                "text": body[:400],
                "market_keywords": kws,
                "market_relevant": bool(kws),
                "link": (item.findtext("link") or "").strip(),
            })
            if len(posts) >= limit:
                break
        out["latest"] = posts
        out["count"] = len(posts)
        out["market_relevant"] = any(p["market_relevant"] for p in posts)
    except Exception as e:
        logger.debug(f"trump feed: {e}")
        out["error"] = str(e)[:120]
    _cache.set(ck, out)
    return out


# --------------------------------------------------------------------------- #
#  MAIN:  get_macro_context()  — one tool-call, ~2 KB JSON
# --------------------------------------------------------------------------- #
DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


async def get_macro_context(symbols: Optional[List[str]] = None,
                            include_key_levels: bool = True,
                            include_trump: bool = True) -> Dict:
    """Assemble the full external context the KI Trader consumes each cycle.

    Kept compact (~2 KB) by defaulting to BTC/ETH/SOL. Every source is optional
    and fails soft, so the call always returns a usable dict.
    """
    symbols = [s.upper() for s in (symbols or DEFAULT_SYMBOLS)]
    async with aiohttp.ClientSession(headers=_HEADERS) as session:
        tasks = {
            "market_regime": market_regime(session),
            "macro_calendar": macro_calendar(session),
        }
        if include_trump:
            tasks["trump_truth_social"] = trump_truth_social(session, limit=5)
        for s in symbols:
            tasks[f"funding::{s}"] = funding_and_oi(session, s)
            if include_key_levels:
                tasks[f"levels::{s}"] = key_levels(session, s)

        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        data = dict(zip(tasks.keys(), results))

    def _safe(key):
        v = data.get(key)
        return None if isinstance(v, Exception) else v

    ctx = {
        "generated_utc": _now_iso(),
        "cycle_min": 10,
        "symbols": symbols,
        "market_regime": _safe("market_regime") or {},
        "macro_calendar": _safe("macro_calendar") or {},
        "funding_oi": {},
        "key_levels": {},
    }
    if include_trump:
        ctx["trump_truth_social"] = _safe("trump_truth_social") or {}
    for s in symbols:
        f = _safe(f"funding::{s}")
        if f:
            ctx["funding_oi"][s] = f
        if include_key_levels:
            k = _safe(f"levels::{s}")
            if k:
                ctx["key_levels"][s] = k
    return ctx


async def historical_candles(symbol: str, interval: str = "1d", limit: int = 200) -> Dict:
    """Historical candles for coins OR Gold/Silver/Oil (free)."""
    async with aiohttp.ClientSession(headers=_HEADERS) as session:
        sym = symbol.upper()
        if sym in _COMMODITY_YAHOO:
            yint = "1d" if interval in ("1d", "4h") else "60m"
            yrange = "6mo" if interval == "1d" else "1mo"
            candles = await fetch_yahoo_candles(session, _COMMODITY_YAHOO[sym], yint, yrange)
        else:
            candles = await fetch_klines(session, sym, interval, limit)
        return {"symbol": sym, "interval": interval,
                "candles": candles[-limit:], "count": len(candles[-limit:])}
