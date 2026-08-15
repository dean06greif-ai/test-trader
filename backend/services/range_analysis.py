"""Range-/Wick-Analyse für den KI-Trader (reine Funktionen, testbar).

Erkennt Seitwärtsranges (mehrfache Touches an Ober-/Unterkante) und
Wick-Rejections (lange Lunten/Dochte an den Range-Grenzen) auf 15m und 1h.
Das Ergebnis geht als Textzeile in den Markt-Snapshot des KI-Traders und
liefert die Datenbasis für range_fade-/mean_reversion-Setups.
"""
from typing import Dict, List, Optional

from services.timeframes import aggregate_candles

RANGE_LOOKBACK = 48        # Kerzen für die Range-Erkennung
TOUCH_TOL_PCT = 0.22       # Toleranzband an den Range-Grenzen (in % der Spanne... s.u.)
MIN_TOUCHES = 2            # mind. Berührungen oben UND unten
MIN_SPAN_PCT = 0.15        # Range-Spanne min (sonst zu eng = Rauschen)
MAX_SPAN_PCT = 8.0         # Range-Spanne max (sonst Trend, keine Range)
WICK_LOOKBACK = 6          # letzte Kerzen für Wick-Rejections
WICK_MIN_RATIO = 0.5       # Lunte/Docht >= 50% der Kerzen-Spanne


def detect_range(candles: List[Dict], lookback: int = RANGE_LOOKBACK) -> Optional[Dict]:
    """Seitwärtsrange in den letzten `lookback` Kerzen finden. None = keine."""
    if len(candles) < max(lookback, 10):
        return None
    win = candles[-lookback:]
    hi = max(c["high"] for c in win)
    lo = min(c["low"] for c in win)
    if lo <= 0 or hi <= lo:
        return None
    span_pct = (hi - lo) / lo * 100
    if not (MIN_SPAN_PCT <= span_pct <= MAX_SPAN_PCT):
        return None
    tol = (hi - lo) * (TOUCH_TOL_PCT / 100) * 100 / max(span_pct, 0.01)
    tol = max(tol, (hi - lo) * 0.12)  # 12% der Spanne als Berührungszone
    top_touches = sum(1 for c in win if c["high"] >= hi - tol)
    bot_touches = sum(1 for c in win if c["low"] <= lo + tol)
    if top_touches < MIN_TOUCHES or bot_touches < MIN_TOUCHES:
        return None
    # Trend-Ausschluss: Schluss der ersten vs. letzten Kerzen – wandert der
    # Markt klar in eine Richtung, ist es keine handelbare Range.
    first = sum(c["close"] for c in win[:5]) / 5
    last = sum(c["close"] for c in win[-5:]) / 5
    if first > 0 and abs(last - first) / first * 100 > span_pct * 0.6:
        return None
    return {"high": hi, "low": lo, "mid": (hi + lo) / 2, "span_pct": round(span_pct, 3),
            "top_touches": top_touches, "bottom_touches": bot_touches, "tol": tol}


def wick_rejections(candles: List[Dict], rng: Dict,
                    lookback: int = WICK_LOOKBACK) -> Dict:
    """Wick-Rejections an den Range-Grenzen in den letzten Kerzen.

    Rückgabe: {"top": Kerzen-Abstand oder None, "bottom": ...} –
    0 = aktuellste Kerze, 1 = eine Kerze davor, usw."""
    out = {"top": None, "bottom": None}
    win = candles[-lookback:]
    for back, c in enumerate(reversed(win)):
        rng_len = c["high"] - c["low"]
        if rng_len <= 0:
            continue
        body_hi = max(c["open"], c["close"])
        body_lo = min(c["open"], c["close"])
        upper = c["high"] - body_hi
        lower = body_lo - c["low"]
        if out["top"] is None and c["high"] >= rng["high"] - rng["tol"] \
                and upper / rng_len >= WICK_MIN_RATIO and c["close"] < rng["high"]:
            out["top"] = back
        if out["bottom"] is None and c["low"] <= rng["low"] + rng["tol"] \
                and lower / rng_len >= WICK_MIN_RATIO and c["close"] > rng["low"]:
            out["bottom"] = back
    return out


def _fmt(rng: Dict, wicks: Dict, tf: str, price: float) -> str:
    pos = "an der Oberkante" if price >= rng["high"] - rng["tol"] else \
        ("an der Unterkante" if price <= rng["low"] + rng["tol"] else "in der Mitte")
    parts = [f"Range-Check ({tf}): Seitwärtsrange {rng['low']:g}-{rng['high']:g} "
             f"(±{rng['span_pct']:g}%, {rng['top_touches']}x oben / "
             f"{rng['bottom_touches']}x unten getestet), Preis {pos}"]
    if wicks.get("top") is not None:
        parts.append(f"Wick-Rejection OBEN vor {wicks['top']} Kerze(n) "
                     f"(langer Docht = Verkäufer verteidigen {rng['high']:g})")
    if wicks.get("bottom") is not None:
        parts.append(f"Wick-Rejection UNTEN vor {wicks['bottom']} Kerze(n) "
                     f"(lange Lunte = Käufer verteidigen {rng['low']:g})")
    if wicks.get("top") is None and wicks.get("bottom") is None:
        parts.append("noch keine frische Wick-Rejection an den Grenzen")
    return " · ".join(parts)


def range_text(candles_1m: List[Dict], price: float) -> str:
    """Kompakte Range-/Wick-Zeilen für den KI-Snapshot ('' = keine Range)."""
    lines = []
    for tf in ("15m", "1h"):
        agg = aggregate_candles(candles_1m, tf, drop_partial=True)
        if len(agg) < 12:
            continue
        rng = detect_range(agg)
        if not rng:
            continue
        lines.append(_fmt(rng, wick_rejections(agg, rng), tf, price))
    return " | ".join(lines)
