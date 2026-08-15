"""Benachrichtigungs-Bremse für Telegram.

Problem vorher: Läuft ein Setup 5 Minuten lang, feuerte der Scanner bei jedem
Tick ein Signal -> bis zu 10 identische Telegram-Nachrichten. Jetzt gilt eine
Sperrzeit pro (Coin, Strategie, Richtung); zusätzlich werden nahezu identische
Einstiegspreise als Wiederholung erkannt.

Reine Logik in `should_notify`, damit sie testbar bleibt; der Zustand liegt im
Modul (Prozess-lokal, kein DB-Overhead pro Signal).
"""
import logging
import time
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_COOLDOWN_MIN = 15
# Wiederholung, wenn der Entry weniger als X % vom letzten Signal abweicht
SAME_PRICE_TOLERANCE_PCT = 0.15

_last: Dict[Tuple[str, str, str], Dict] = {}
_suppressed = 0


def signal_key(signal: Dict) -> Tuple[str, str, str]:
    return (str(signal.get("symbol")), str(signal.get("strategy_id")),
            str(signal.get("type") or signal.get("side")))


def should_notify(last_entry: Optional[Dict], now: float, cooldown_s: float,
                  price: Optional[float]) -> Tuple[bool, str]:
    """Darf benachrichtigt werden? (rein, testbar)"""
    if cooldown_s <= 0 or not last_entry:
        return True, ""
    age = now - float(last_entry.get("ts", 0))
    if age >= cooldown_s:
        return True, ""
    prev_price = last_entry.get("price")
    if price and prev_price:
        diff_pct = abs(float(price) - float(prev_price)) / float(prev_price) * 100
        if diff_pct > SAME_PRICE_TOLERANCE_PCT:
            return True, ""
        return False, (f"gleiches Setup vor {int(age)}s (Preisabweichung "
                       f"{round(diff_pct, 3)}%)")
    return False, f"Wiederholung innerhalb der Sperrzeit ({int(age)}s)"


def check(signal: Dict, cooldown_min: Optional[float] = None) -> Tuple[bool, str]:
    """Prüft und aktualisiert den Zustand (wird pro Signal einmal aufgerufen)."""
    global _suppressed
    cooldown = DEFAULT_COOLDOWN_MIN if cooldown_min is None else float(cooldown_min)
    price = signal.get("entry") or signal.get("price") or signal.get("entry_price")
    key = signal_key(signal)
    now = time.time()
    ok, reason = should_notify(_last.get(key), now, cooldown * 60,
                              float(price) if price else None)
    if ok:
        _last[key] = {"ts": now, "price": float(price) if price else None}
    else:
        _suppressed += 1
        logger.info(f"Telegram unterdrückt {key}: {reason}")
    return ok, reason


def status() -> Dict:
    return {"tracked": len(_last), "suppressed_total": _suppressed,
            "tolerance_pct": SAME_PRICE_TOLERANCE_PCT}


def reset():
    _last.clear()
