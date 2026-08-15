"""Timeframe-Aggregation: 1m-Kerzen -> höhere Timeframes."""
from typing import Dict, List

import numpy as np

from services.candles import CandleArray

TIMEFRAMES: Dict[str, int] = {
    "1m": 1, "2m": 2, "3m": 3, "5m": 5, "10m": 10, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240, "6h": 360, "8h": 480, "12h": 720,
    "24h": 1440, "1d": 1440, "3d": 4320, "1w": 10080, "1M": 43200,
}

TIMEFRAME_ORDER = list(TIMEFRAMES.keys())

# Pro-Regel Timeframe-Override (Custom-/KI-Strategien): wählbare Stufen
RULE_TIMEFRAMES = ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "8h", "12h", "1d"]

_TF_ALIASES = {"24h": "1d", "1440m": "1d", "d": "1d", "1t": "1d", "1tag": "1d",
               "60m": "1h", "120m": "2h", "240m": "4h", "480m": "8h", "720m": "12h"}


def tf_minutes(tf) -> int:
    return TIMEFRAMES.get(str(tf), 0)


def normalize_rule_tf(raw):
    """'15M' / '24h' / '60m' -> kanonischer Regel-TF ('15m'/'1d'/'1h') oder None."""
    if raw is None:
        return None
    key = str(raw).strip().lower()
    key = _TF_ALIASES.get(key, key)
    return key if key in RULE_TIMEFRAMES else None


def valid_rule_tf(rule_tf, base_tf) -> bool:
    """Regel-TF muss >= Strategie-TF und ein Vielfaches davon sein."""
    r, b = tf_minutes(rule_tf), tf_minutes(base_tf)
    return r > 0 and b > 0 and r >= b and r % b == 0


def rule_tf_options(base_tf, lo="1m", hi="4h"):
    """Gültige Regel-TF-Optionen innerhalb eines Rahmens (z.B. 1m–4h)."""
    lo_m = tf_minutes(lo) or 1
    hi_m = tf_minutes(hi) or 240
    return [t for t in RULE_TIMEFRAMES
            if lo_m <= tf_minutes(t) <= hi_m and valid_rule_tf(t, base_tf)]


def _aggregate_array(ca: CandleArray, bucket_ms: int, drop_partial: bool,
                     base_ms: int = 60000) -> CandleArray:
    """Vektorisierte Aggregation – bei Millionen Kerzen ~100x schneller als
    die Schleife und ohne Zwischen-Dicts."""
    n = len(ca)
    if n == 0:
        return ca
    keys = ca.ts // bucket_ms
    starts = np.concatenate([[0], np.flatnonzero(keys[1:] != keys[:-1]) + 1])
    ends = np.concatenate([starts[1:], [n]])
    out = CandleArray(
        keys[starts] * bucket_ms,
        ca.op[starts],
        np.maximum.reduceat(ca.hi, starts),
        np.minimum.reduceat(ca.lo, starts),
        ca.cl[ends - 1],
        np.add.reduceat(ca.vol, starts),
    )
    if drop_partial and len(out):
        last_close_ms = int(ca.ts[-1]) + base_ms
        if last_close_ms < int(out.ts[-1]) + bucket_ms:
            out = out[:-1]
    return out


def aggregate_candles(candles, timeframe: str, drop_partial: bool = False,
                      base_ms: int = 60000):
    minutes = TIMEFRAMES.get(timeframe, 1)
    if minutes * 60000 <= base_ms or not candles:
        return candles
    bucket_ms = minutes * 60000
    if isinstance(candles, CandleArray):
        return _aggregate_array(candles, bucket_ms, drop_partial, base_ms)
    out: List[Dict] = []
    cur_key = None
    bucket = None
    for c in candles:
        key = c["timestamp"] // bucket_ms
        if key != cur_key:
            if bucket is not None:
                out.append(bucket)
            bucket = {"timestamp": key * bucket_ms, "open": c["open"], "high": c["high"],
                      "low": c["low"], "close": c["close"], "volume": c.get("volume", 0.0)}
            cur_key = key
        else:
            bucket["high"] = max(bucket["high"], c["high"])
            bucket["low"] = min(bucket["low"], c["low"])
            bucket["close"] = c["close"]
            bucket["volume"] += c.get("volume", 0.0)
    if bucket is not None:
        if drop_partial:
            last_close_ms = candles[-1]["timestamp"] + base_ms
            if last_close_ms >= bucket["timestamp"] + bucket_ms:
                out.append(bucket)
        else:
            out.append(bucket)
    return out
