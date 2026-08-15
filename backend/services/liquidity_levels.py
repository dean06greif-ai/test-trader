"""Liquiditäts-Level ("Liquidity Levels") – eigener X-Ray-Pro-Ersatz.

Warum eigenbau: TradingView-Skripte (X-Ray Pro, X-Ray Liquidation Heatmap) sind
Pine-Code im Chart und liefern KEINE API-Daten. Die dahinterliegenden Konzepte
lassen sich aber vollständig aus freien Kerzendaten rekonstruieren – ohne
Fremd-Keys und ohne Kosten:

  * Swing-Pivots (Struktur-Hochs/Tiefs) inkl. "unberührt" (untested) – dort
    liegen Stop-Cluster / Liquidität.
  * Equal Highs / Equal Lows (EQH/EQL) – doppelte Hochs/Tiefs als Magnet.
  * Unverfüllte Imbalancen / Fair-Value-Gaps (FVG).
  * Volumen-Profil: POC / VAH / VAL + High-/Low-Volume-Nodes.
  * Runde Preis-Marken (psychologische Level).

Alle Funktionen sind REIN (Kerzen rein, Level raus) und damit direkt testbar.
Kerzen-Format wie überall in der App:
``{"timestamp": ms, "open", "high", "low", "close", "volume"}`` (älteste zuerst).
"""
from typing import Dict, List, Optional

# Gewichtung der Level-Typen für den Stärke-Score (0-100)
TYPE_WEIGHT = {
    "swing_high": 55, "swing_low": 55,
    "eqh": 75, "eql": 75,
    "fvg": 45,
    "ob_bull": 80, "ob_bear": 80,
    "poc": 90, "vah": 70, "val": 70,
    "hvn": 60, "lvn": 35,
    "round": 40,
    "day_high": 65, "day_low": 65,
}


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------------- #
#  Struktur: Swing-Pivots + unberührte Level
# --------------------------------------------------------------------------- #
def swing_pivots(candles: List[Dict], left: int = 3, right: int = 3) -> List[Dict]:
    """Bestätigte Swing-Hochs/-Tiefs (Pivot mit `left`/`right` Nachbarn)."""
    out: List[Dict] = []
    n = len(candles)
    for i in range(left, n - right):
        hi = _f(candles[i].get("high"))
        lo = _f(candles[i].get("low"))
        window = candles[i - left:i + right + 1]
        if hi and hi >= max(_f(c.get("high")) for c in window):
            out.append({"index": i, "price": hi, "type": "swing_high",
                        "timestamp": candles[i].get("timestamp")})
        if lo and lo <= min(_f(c.get("low")) for c in window):
            out.append({"index": i, "price": lo, "type": "swing_low",
                        "timestamp": candles[i].get("timestamp")})
    return out


def untested_pivots(candles: List[Dict], pivots: List[Dict]) -> List[Dict]:
    """Pivots, die seit ihrer Entstehung NICHT mehr durchhandelt wurden.

    Genau dort sammeln sich Stops/Limit-Orders → die Liquidität, die X-Ray Pro
    als „Liquidity Level" zeichnet.
    """
    out = []
    for p in pivots:
        i, price = p["index"], p["price"]
        later = candles[i + 1:]
        if not later:
            continue
        if p["type"] == "swing_high":
            touched = any(_f(c.get("high")) >= price for c in later)
        else:
            touched = any(_f(c.get("low")) <= price for c in later)
        if not touched:
            out.append({**p, "untested": True})
    return out


def equal_levels(pivots: List[Dict], tolerance_pct: float = 0.08) -> List[Dict]:
    """Equal Highs / Equal Lows: mehrere Pivots auf (fast) gleichem Preis."""
    out: List[Dict] = []
    for kind, ltype in (("swing_high", "eqh"), ("swing_low", "eql")):
        group = sorted((p for p in pivots if p["type"] == kind),
                       key=lambda p: p["price"])
        cluster: List[Dict] = []
        for p in group:
            if cluster and abs(p["price"] - cluster[-1]["price"]) / max(cluster[-1]["price"], 1e-9) * 100 <= tolerance_pct:
                cluster.append(p)
                continue
            if len(cluster) >= 2:
                out.append({"price": round(sum(c["price"] for c in cluster) / len(cluster), 6),
                            "type": ltype, "touches": len(cluster)})
            cluster = [p]
        if len(cluster) >= 2:
            out.append({"price": round(sum(c["price"] for c in cluster) / len(cluster), 6),
                        "type": ltype, "touches": len(cluster)})
    return out


def fair_value_gaps(candles: List[Dict], min_gap_pct: float = 0.05,
                    max_open: int = 8) -> List[Dict]:
    """Unverfüllte 3-Kerzen-Imbalancen (FVG). Neueste zuerst."""
    gaps: List[Dict] = []
    for i in range(2, len(candles)):
        a, c = candles[i - 2], candles[i]
        # bullische Imbalance: Tief der Kerze i über Hoch der Kerze i-2
        lo_c, hi_a = _f(c.get("low")), _f(a.get("high"))
        if lo_c > hi_a and hi_a:
            gap_pct = (lo_c - hi_a) / hi_a * 100
            if gap_pct >= min_gap_pct:
                gaps.append({"index": i, "low": hi_a, "high": lo_c, "side": "bull",
                             "gap_pct": round(gap_pct, 3)})
        hi_c, lo_a = _f(c.get("high")), _f(a.get("low"))
        if hi_c < lo_a and lo_a:
            gap_pct = (lo_a - hi_c) / lo_a * 100
            if gap_pct >= min_gap_pct:
                gaps.append({"index": i, "low": hi_c, "high": lo_a, "side": "bear",
                             "gap_pct": round(gap_pct, 3)})
    # Verfüllte Gaps verwerfen (Preis war später komplett drin)
    out = []
    for g in gaps:
        later = candles[g["index"] + 1:]
        filled = any(_f(c.get("low")) <= g["low"] and _f(c.get("high")) >= g["high"]
                     for c in later)
        if not filled:
            out.append({"price": round((g["low"] + g["high"]) / 2, 6), "type": "fvg",
                        "low": round(g["low"], 6), "high": round(g["high"], 6),
                        "side": g["side"], "gap_pct": g["gap_pct"]})
    return out[-max_open:][::-1]


# --------------------------------------------------------------------------- #
#  Volumen-Profil
# --------------------------------------------------------------------------- #
def volume_profile(candles: List[Dict], bins: int = 48,
                   value_area: float = 0.7) -> Dict:
    """POC/VAH/VAL + Bin-Verteilung (Basis für die Heatmap-Darstellung)."""
    if not candles:
        return {"poc": None, "vah": None, "val": None, "bins": []}
    lo = min(_f(c.get("low")) for c in candles)
    hi = max(_f(c.get("high")) for c in candles)
    if hi <= lo:
        return {"poc": None, "vah": None, "val": None, "bins": []}
    width = (hi - lo) / bins
    vols = [0.0] * bins
    for c in candles:
        typical = (_f(c.get("high")) + _f(c.get("low")) + _f(c.get("close"))) / 3
        idx = min(bins - 1, max(0, int((typical - lo) / width)))
        vols[idx] += _f(c.get("volume")) or 1.0
    total = sum(vols) or 1.0
    poc_idx = max(range(bins), key=lambda i: vols[i])
    # Value Area um den POC herum aufbauen
    included = {poc_idx}
    acc = vols[poc_idx]
    lo_i = hi_i = poc_idx
    while acc < total * value_area and (lo_i > 0 or hi_i < bins - 1):
        down = vols[lo_i - 1] if lo_i > 0 else -1
        up = vols[hi_i + 1] if hi_i < bins - 1 else -1
        if up >= down:
            hi_i += 1
            included.add(hi_i)
            acc += max(up, 0)
        else:
            lo_i -= 1
            included.add(lo_i)
            acc += max(down, 0)
    max_vol = max(vols) or 1.0
    return {
        "poc": _round_price(lo + (poc_idx + 0.5) * width),
        "vah": _round_price(lo + (hi_i + 1) * width),
        "val": _round_price(lo + lo_i * width),
        "low": _round_price(lo), "high": _round_price(hi),
        "bins": [{"price": _round_price(lo + (i + 0.5) * width),
                  "volume": round(vols[i], 4),
                  "heat": round(vols[i] / max_vol, 4)} for i in range(bins)],
    }


def volume_nodes(profile: Dict, hvn_pct: float = 0.75,
                 lvn_pct: float = 0.15) -> List[Dict]:
    """High-/Low-Volume-Nodes aus dem Profil (Magnete bzw. Vakuum-Zonen)."""
    out = []
    for b in profile.get("bins") or []:
        if b["heat"] >= hvn_pct:
            out.append({"price": b["price"], "type": "hvn", "heat": b["heat"]})
        elif b["heat"] <= lvn_pct and b["volume"] > 0:
            out.append({"price": b["price"], "type": "lvn", "heat": b["heat"]})
    return out


def round_levels(price: float, count: int = 3) -> List[Dict]:
    """Psychologische Marken (…000 / …500) um den Preis."""
    if not price:
        return []
    magnitude = 10 ** (len(str(int(price))) - 2) or 1
    step = magnitude * 5
    base = int(price / step) * step
    out = []
    for k in range(-count, count + 1):
        lvl = base + k * step
        if lvl > 0:
            out.append({"price": float(lvl), "type": "round"})
    return out


# --------------------------------------------------------------------------- #
#  Aggregation
# --------------------------------------------------------------------------- #
def _round_price(p: float) -> float:
    """Anzeige-taugliche Rundung je Preis-Größenordnung."""
    a = abs(p)
    if a >= 1000:
        return round(p, 2)
    if a >= 10:
        return round(p, 3)
    if a >= 1:
        return round(p, 4)
    return round(p, 6)


def score_level(level: Dict, price: float) -> Dict:
    """Stärke 0-100 aus Typ-Gewicht, Nähe zum Preis und Bestätigungen."""
    base = TYPE_WEIGHT.get(level.get("type"), 40)
    dist_pct = abs(level["price"] - price) / max(price, 1e-9) * 100
    proximity = max(0.0, 1.0 - min(dist_pct, 10.0) / 10.0)      # 0..1
    touches = int(level.get("touches") or 1)
    strength = base * (0.55 + 0.45 * proximity) + min(touches - 1, 3) * 5
    return {**level,
            "price": _round_price(level["price"]),
            "side": "above" if level["price"] > price else "below",
            "dist_pct": round(dist_pct, 3),
            "strength": int(max(0, min(100, round(strength)))) }


def order_blocks(candles: List[Dict], atr_period: int = 14, impulse_mult: float = 1.5,
                 look_ahead: int = 3, max_blocks: int = 6) -> List[Dict]:
    """Smart-Money-Concept Order Blocks: letzte Gegen-Kerze vor einem Impuls.

    Bullish OB = letzte rote Kerze, bevor der Kurs innerhalb von `look_ahead`
    Kerzen impulsiv steigt (>= impulse_mult * ATR) und über ihr Hoch schließt.
    Bearish OB spiegelbildlich. Blöcke, deren Zone der Kurs seitdem komplett
    durchhandelt hat, gelten als invalidiert und werden weggelassen;
    `untested` = Zone wurde seit Entstehung noch nicht wieder angelaufen.
    """
    n = len(candles)
    if n < atr_period + look_ahead + 2:
        return []
    trs = []
    for i in range(1, n):
        h, l = _f(candles[i].get("high")), _f(candles[i].get("low"))
        pc = _f(candles[i - 1].get("close"))
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    out: List[Dict] = []
    for i in range(atr_period, n - look_ahead - 1):
        atr = sum(trs[i - atr_period:i]) / atr_period
        if atr <= 0:
            continue
        o, c = _f(candles[i].get("open")), _f(candles[i].get("close"))
        hi, lo = _f(candles[i].get("high")), _f(candles[i].get("low"))
        fwd = candles[i + 1:i + 1 + look_ahead]
        fwd_close = _f(fwd[-1].get("close"))
        later = candles[i + 1 + look_ahead:]
        if c < o and fwd_close - c >= impulse_mult * atr \
                and max(_f(x.get("close")) for x in fwd) > hi:
            if any(_f(x.get("low")) < lo for x in later):
                continue  # Zone komplett durchhandelt -> invalidiert
            touched = any(_f(x.get("low")) <= max(o, c) for x in later)
            out.append({"price": (lo + max(o, c)) / 2, "type": "ob_bull",
                        "zone_low": lo, "zone_high": max(o, c),
                        "untested": not touched, "index": i})
        elif c > o and c - fwd_close >= impulse_mult * atr \
                and min(_f(x.get("close")) for x in fwd) < lo:
            if any(_f(x.get("high")) > hi for x in later):
                continue
            touched = any(_f(x.get("high")) >= min(o, c) for x in later)
            out.append({"price": (hi + min(o, c)) / 2, "type": "ob_bear",
                        "zone_low": min(o, c), "zone_high": hi,
                        "untested": not touched, "index": i})
    out.sort(key=lambda b: -b["index"])
    for b in out:
        b.pop("index", None)
    return out[:max_blocks]


def liquidity_levels(candles: List[Dict], price: Optional[float] = None,
                     max_levels: int = 24, per_type_cap: int = 4) -> Dict:
    """Kompletter „Liquidity Levels"-Satz für ein Symbol (X-Ray-Pro-Äquivalent)."""
    if not candles:
        return {"price": price, "levels": [], "volume_profile": {}, "counts": {}}
    price = price or _f(candles[-1].get("close"))
    pivots = swing_pivots(candles)
    untested = untested_pivots(candles, pivots)
    profile = volume_profile(candles)

    raw: List[Dict] = []
    raw += [{"price": p["price"], "type": p["type"], "untested": True}
            for p in untested]
    raw += equal_levels(pivots)
    raw += fair_value_gaps(candles)
    raw += order_blocks(candles)
    for key in ("poc", "vah", "val"):
        if profile.get(key):
            raw.append({"price": profile[key], "type": key})
    raw += volume_nodes(profile)
    raw += round_levels(price)
    last = candles[-1]
    raw.append({"price": _f(last.get("high")), "type": "day_high"})
    raw.append({"price": _f(last.get("low")), "type": "day_low"})

    seen = set()
    levels = []
    for lvl in raw:
        if not lvl.get("price"):
            continue
        key = (lvl["type"], round(lvl["price"], 4))
        if key in seen:
            continue
        seen.add(key)
        levels.append(score_level(lvl, price))
    levels.sort(key=lambda x: (-x["strength"], x["dist_pct"]))
    # Vielfalt sichern: ein Typ (z.B. EQL) darf die Liste nicht dominieren.
    per_type: Dict[str, int] = {}
    picked = []
    for lvl in levels:
        t = lvl["type"]
        if per_type.get(t, 0) >= per_type_cap:
            continue
        per_type[t] = per_type.get(t, 0) + 1
        picked.append(lvl)
        if len(picked) >= max_levels:
            break
    levels = picked

    counts: Dict[str, int] = {}
    for lvl in levels:
        counts[lvl["type"]] = counts.get(lvl["type"], 0) + 1
    return {"price": _round_price(price), "levels": levels,
            "volume_profile": profile, "counts": counts,
            "nearest_above": next((x for x in sorted(levels, key=lambda l: l["dist_pct"])
                                   if x["side"] == "above"), None),
            "nearest_below": next((x for x in sorted(levels, key=lambda l: l["dist_pct"])
                                   if x["side"] == "below"), None)}


def heatmap(candles: List[Dict], clusters: Dict, price: Optional[float] = None,
            bins: int = 40) -> Dict:
    """Liquidations-Heatmap: Preis-Buckets mit Hitze aus modellierten
    Liquidations-Clustern + Volumen-Profil + Liquiditäts-Leveln.

    Ersetzt die kostenpflichtige CoinGlass/Hyblock-Heatmap durch eine
    nachvollziehbare Schätzung (frei & keyless).
    """
    if not candles:
        return {"bins": [], "price": price}
    price = price or _f(candles[-1].get("close"))
    lo = min(_f(c.get("low")) for c in candles)
    hi = max(_f(c.get("high")) for c in candles)
    # Cluster können außerhalb der Kerzen-Range liegen -> Range erweitern
    all_cluster = (clusters or {}).get("below_price", []) + (clusters or {}).get("above_price", [])
    for c in all_cluster:
        lo = min(lo, _f(c.get("price")))
        hi = max(hi, _f(c.get("price")))
    if hi <= lo:
        return {"bins": [], "price": price}
    width = (hi - lo) / bins
    heat = [0.0] * bins
    labels: List[List[str]] = [[] for _ in range(bins)]

    def idx_of(p):
        return min(bins - 1, max(0, int((p - lo) / width)))

    strength_w = {"high": 1.0, "medium": 0.6, "low": 0.35}
    for side, sign in (("below_price", "long"), ("above_price", "short")):
        for c in (clusters or {}).get(side, []):
            i = idx_of(_f(c.get("price")))
            heat[i] += strength_w.get(c.get("strength"), 0.5)
            labels[i].append(f"{sign}-liq {c.get('est_leverage', '')}".strip())

    prof = volume_profile(candles, bins=bins)
    for b in prof.get("bins") or []:
        heat[idx_of(b["price"])] += b["heat"] * 0.6

    lvl = liquidity_levels(candles, price)
    for x in lvl["levels"]:
        i = idx_of(x["price"])
        heat[i] += x["strength"] / 100 * 0.5
        labels[i].append(x["type"])

    top = max(heat) or 1.0
    return {
        "price": _round_price(price), "low": _round_price(lo), "high": _round_price(hi),
        "bins": [{"price": _round_price(lo + (i + 0.5) * width),
                  "heat": round(heat[i] / top, 4),
                  "tags": sorted(set(labels[i]))[:4]} for i in range(bins)],
        "levels": lvl["levels"][:12],
    }
