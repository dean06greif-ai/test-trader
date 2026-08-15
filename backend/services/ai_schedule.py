"""Zeitplan für die regelmäßige KI-Analyse (Analyst-Rolle).

Statt eines starren Intervalls kann der Trader Zeitfenster mit eigenen
Intervallen festlegen, z.B.:
    22:00-06:00 -> alle 30 min (Nacht, wenig Bewegung)
    15:00-18:00 -> alle 5 min  (US-Open, hohe Aktivität)
    Rest        -> Standard-Intervall (`interval_min`)

Fenster über Mitternacht (from > to) werden unterstützt. Bei Überlappung gewinnt
das ZUERST passende Fenster (Reihenfolge = Priorität). Alle Funktionen sind rein
und damit direkt testbar; Zeitbasis ist Europe/Berlin (wie im Rest der App).
"""
import logging
import re
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
MAX_WINDOWS = 8


def _to_minutes(hhmm: str) -> Optional[int]:
    m = _HHMM.match(str(hhmm).strip())
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def normalize_schedule(raw) -> List[Dict]:
    """Rohes Fenster-Array säubern (ungültige Einträge fallen weg)."""
    out: List[Dict] = []
    for item in (raw or [])[:MAX_WINDOWS]:
        if not isinstance(item, dict):
            continue
        start, end = _to_minutes(item.get("from")), _to_minutes(item.get("to"))
        if start is None or end is None or start == end:
            continue
        try:
            interval = max(1, min(360, int(float(item.get("interval_min", 10)))))
        except (TypeError, ValueError):
            continue
        out.append({
            "from": item["from"] if isinstance(item["from"], str) else "",
            "to": item["to"] if isinstance(item["to"], str) else "",
            "interval_min": interval,
            "label": str(item.get("label") or "")[:40],
            "enabled": bool(item.get("enabled", True)),
            # Optional: eigenes KI-Modell für dieses Zeitfenster (z.B. starkes
            # Modell zur US-Eröffnung, günstiges nachts). Leer = Haupt-Modell.
            "model": str(item.get("model") or "")[:60],
            "provider": str(item.get("provider") or "")[:20],
        })
    return out


def window_matches(window: Dict, minutes: int) -> bool:
    """Liegt `minutes` (0-1439) im Fenster? Fenster über Mitternacht inklusive."""
    start, end = _to_minutes(window.get("from")), _to_minutes(window.get("to"))
    if start is None or end is None:
        return False
    if start < end:
        return start <= minutes < end
    return minutes >= start or minutes < end      # über Mitternacht


def effective_window(schedule, minutes: int) -> Optional[Dict]:
    """Das aktuell aktive Zeitfenster (oder None = Standard).
    Basis für Intervall UND optionales Fenster-Modell."""
    for w in normalize_schedule(schedule):
        if w.get("enabled", True) and window_matches(w, minutes):
            return w
    return None


def effective_interval(schedule, default_interval: int, minutes: int) -> Tuple[int, str]:
    """Aktives Intervall (Minuten) + Beschriftung für den Zeitpunkt `minutes`."""
    try:
        default_interval = max(1, int(default_interval))
    except (TypeError, ValueError):
        default_interval = 10
    w = effective_window(schedule, minutes)
    if w:
        label = w.get("label") or f"{w['from']}-{w['to']}"
        return int(w["interval_min"]), label
    return default_interval, "Standard"


def schedule_text(schedule, default_interval: int) -> str:
    """Kurzbeschreibung für Prompt/UI."""
    wins = [w for w in normalize_schedule(schedule) if w.get("enabled", True)]
    if not wins:
        return f"durchgehend alle {default_interval} min"
    parts = [f"{w['from']}-{w['to']} alle {w['interval_min']} min"
             + (f" ({w['label']})" if w.get("label") else "") for w in wins]
    return " | ".join(parts) + f" | sonst alle {default_interval} min"
