"""Session-Levels (Asia/London/NY) und Umverteilungszonen (Volumen-Cluster).

Reine, testbare Funktionen auf 1m-Kerzen ({timestamp(ms), open, high, low,
close, volume}). Wird vom KI-Trader-Snapshot genutzt, damit die KI Sweeps von
Session-Hochs/-Tiefs und Akkumulations-/Distributionszonen einbeziehen kann.
"""
from datetime import datetime, timezone
from typing import Dict, List, Optional

# UTC-Handelsfenster (bewusst mit Überlappung, wie am Devisenmarkt üblich)
SESSIONS = (("Asia", 0, 8), ("London", 7, 16), ("NY", 12, 21))


def _dt(c: Dict) -> Optional[datetime]:
    try:
        return datetime.fromtimestamp(float(c["timestamp"]) / 1000, tz=timezone.utc)
    except (KeyError, TypeError, ValueError):
        return None


def session_levels(candles: List[Dict]) -> List[Dict]:
    """High/Low der jeweils jüngsten Session je Name (heute, sonst Vortag)."""
    if not candles:
        return []
    buckets: Dict[tuple, Dict] = {}
    for c in candles:
        dt = _dt(c)
        if dt is None:
            continue
        try:
            hi, lo = float(c["high"]), float(c["low"])
        except (KeyError, TypeError, ValueError):
            continue
        if hi <= 0 or lo <= 0:
            continue
        for name, start_h, end_h in SESSIONS:
            if start_h <= dt.hour < end_h:
                key = (name, dt.date())
                b = buckets.get(key)
                if b is None:
                    buckets[key] = {"high": hi, "low": lo, "candles": 1}
                else:
                    b["high"] = max(b["high"], hi)
                    b["low"] = min(b["low"], lo)
                    b["candles"] += 1
    last = _dt(candles[-1])
    out = []
    for name, start_h, end_h in SESSIONS:
        days = sorted((d for (n, d) in buckets if n == name), reverse=True)
        if not days:
            continue
        day, b = days[0], buckets[(name, days[0])]
        # zu wenig Kerzen (Session gerade erst gestartet) -> Vortag bevorzugen
        if b["candles"] < 15 and len(days) > 1:
            day, b = days[1], buckets[(name, days[1])]
        running = bool(last and day == last.date() and start_h <= last.hour < end_h)
        out.append({"session": name, "date": day.isoformat(), "running": running,
                    "high": b["high"], "low": b["low"]})
    return out


def levels_text(candles: List[Dict], price: float = 0) -> str:
    rows = session_levels(candles)
    if not rows:
        return ""
    parts = []
    for r in rows:
        flag = "·läuft" if r["running"] else ""
        hint = ""
        if price:
            if price > r["high"]:
                hint = " Kurs>H(Sweep?)"
            elif price < r["low"]:
                hint = " Kurs<L(Sweep?)"
        parts.append(f"{r['session']}{flag} H/L {r['high']:g}/{r['low']:g}{hint}")
    return "Session-Levels (UTC): " + ", ".join(parts)


def volume_zones(candles: List[Dict], window: int = 1440, bins: int = 30,
                 top: int = 2) -> List[Dict]:
    """Umverteilungs-/Akkumulationszonen: Preisbereiche mit überdurchschnittlich
    viel umgesetztem Volumen (naives Volume-Profile, Top-Bins verschmolzen)."""
    data = candles[-window:]
    if len(data) < 60:
        return []
    try:
        lo = min(float(c["low"]) for c in data)
        hi = max(float(c["high"]) for c in data)
    except (KeyError, TypeError, ValueError):
        return []
    if hi <= lo:
        return []
    step = (hi - lo) / bins
    vols = [0.0] * bins
    total = 0.0
    for c in data:
        try:
            px = (float(c["high"]) + float(c["low"]) + float(c["close"])) / 3
            v = float(c.get("volume") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        idx = min(bins - 1, max(0, int((px - lo) / step)))
        vols[idx] += v
        total += v
    if total <= 0:
        return []
    avg = total / bins
    strong = sorted(i for i, v in enumerate(vols) if v >= avg * 1.6)
    merged: List[Dict] = []
    for i in strong:
        if merged and i == merged[-1]["end"] + 1:
            merged[-1]["end"] = i
            merged[-1]["vol"] += vols[i]
        else:
            merged.append({"start": i, "end": i, "vol": vols[i]})
    merged.sort(key=lambda z: -z["vol"])
    return [{"low": round(lo + z["start"] * step, 8),
             "high": round(lo + (z["end"] + 1) * step, 8),
             "vol_share": round(z["vol"] / total * 100, 1)} for z in merged[:top]]


def zones_text(candles: List[Dict], price: float = 0) -> str:
    zs = volume_zones(candles)
    if not zs:
        return ""
    parts = []
    for z in zs:
        inside = bool(price and z["low"] <= price <= z["high"])
        parts.append(f"{z['low']:g}-{z['high']:g} ({z['vol_share']:g}% Vol"
                     + (", Kurs IN Zone" if inside else "") + ")")
    return "Umverteilungszonen (Vol-Cluster 24h): " + ", ".join(parts)
