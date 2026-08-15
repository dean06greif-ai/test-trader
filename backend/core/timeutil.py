"""Eine Quelle der Wahrheit für Zeit & Zeitzone (Anzeige = Europe/Berlin).

Regeln der Plattform:
  * GESPEICHERT wird immer UTC (ISO-8601 MIT Offset) – rückwärtskompatibel.
  * ANGEZEIGT / für Zeitpläne & KI-Prompts GERECHNET wird Europe/Berlin
    (inkl. automatischer Sommer-/Winterzeit).

Alle Helfer sind rein und damit direkt testbar.
"""
from datetime import datetime, timezone
from typing import Optional, Union

from core.config import BERLIN  # ZoneInfo("Europe/Berlin")

__all__ = ["BERLIN", "now_utc", "now_berlin", "now_iso", "parse_iso", "to_berlin",
           "berlin_date", "berlin_hhmm", "berlin_minutes", "fmt_berlin"]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_berlin() -> datetime:
    return datetime.now(BERLIN)


def now_iso() -> str:
    """Speicher-Format: UTC mit Offset (von JS/Python eindeutig lesbar)."""
    return now_utc().isoformat()


def parse_iso(value: Union[str, datetime, None]) -> Optional[datetime]:
    """ISO-String -> aware datetime. Naive Strings gelten als UTC (Altdaten)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def to_berlin(value: Union[str, datetime, None]) -> Optional[datetime]:
    dt = parse_iso(value)
    return dt.astimezone(BERLIN) if dt else None


def berlin_date(value: Union[str, datetime, None] = None) -> str:
    """Tages-Schlüssel (YYYY-MM-DD) in deutscher Zeit."""
    dt = to_berlin(value) if value is not None else now_berlin()
    return (dt or now_berlin()).strftime("%Y-%m-%d")


def berlin_hhmm(value: Union[str, datetime, None] = None) -> str:
    dt = to_berlin(value) if value is not None else now_berlin()
    return (dt or now_berlin()).strftime("%H:%M")


def berlin_minutes(value: Union[str, datetime, None] = None) -> int:
    """Minuten seit Mitternacht (deutsche Zeit) – Basis für Zeitpläne."""
    dt = to_berlin(value) if value is not None else now_berlin()
    dt = dt or now_berlin()
    return dt.hour * 60 + dt.minute


def fmt_berlin(value: Union[str, datetime, None], with_date: bool = True,
               fallback: str = "-") -> str:
    """Menschlich lesbare deutsche Zeit für UI-Texte & KI-Prompts."""
    dt = to_berlin(value)
    if not dt:
        return fallback
    return dt.strftime("%d.%m.%Y %H:%M" if with_date else "%H:%M")
