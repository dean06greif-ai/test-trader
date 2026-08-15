"""Datenbasierte Freigabe (Validierung) von KI-Änderungen.

Problem vorher: die KI hat laufend Einstellungs-Vorschläge und neue Lektionen
produziert – auch ohne belastbare Datengrundlage. Jetzt gilt:

  * KI-initiierte Einstellungs-Änderungen brauchen eine Mindest-Stichprobe
    (geschlossene Trades gesamt bzw. pro Coin). Ohne sie landet der Wunsch als
    Vorschlag mit Status `needs_data` und wird NIE automatisch angewendet.
  * Neue/geänderte Lektionen der KI brauchen ebenfalls eine Mindestzahl an
    Ergebnissen, das Verwerfen bestehender Lektionen zusätzlich mehr Daten.
  * Vom Trader gewollte Änderungen laufen IMMER ohne Validierung durch – die KI
    darf dazu aber ihre Meinung sagen (`ai_engine.comment_on_user_change`).

Die Bewertungslogik ist rein und damit direkt testbar.
"""
import logging
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

DOC_ID = "ai_validation"

DEFAULT_SETTINGS = {
    "enabled": True,
    "min_closed_trades": 15,      # Engine-weite Änderungen
    "min_symbol_trades": 8,       # Änderungen an einem Coin
    "min_lesson_results": 5,      # neue/geschärfte Lektion
    "min_lesson_confirmations": 2,  # so oft muss die KI dieselbe Lektion wiedererkennen
    "min_removal_results": 12,    # bestehende Lektion verwerfen
    # Makro-Parameter (SL, CRV, Hebel, Konfidenz ...): deutlich strenger,
    # damit ein einzelner Trade keine Strukturänderung auslöst.
    "macro_min_trades": 25,       # Mindest-Stichprobe für Makro-Änderungen
    "macro_min_confirmations": 3,  # so oft muss die Datenlage dieselbe Richtung zeigen
    "macro_max_step_pct": 20,     # max. Änderung pro Schritt in % des aktuellen Werts
    "macro_confirm_window_days": 14,
}

# Struktur-Parameter des Trade-Verhaltens. `abs_step` = maximale absolute
# Änderung pro Schritt (Prozentpunkte bzw. Einheiten), `min`/`max` = Leitplanken.
MACRO_KEYS: Dict[str, Dict] = {
    "sl_fixed_percent": {"abs_step": 0.15, "min": 0.1, "max": 5.0},
    "sl_atr_mult": {"abs_step": 0.3, "min": 0.5, "max": 5.0},
    "tp1_crv": {"abs_step": 0.2, "min": 0.5, "max": 6.0},
    "tpf_crv": {"abs_step": 0.3, "min": 0.8, "max": 10.0},
    "tp1_close_percent": {"abs_step": 10, "min": 10, "max": 90},
    "leverage": {"abs_step": 3, "min": 1, "max": 50},
    "min_confidence": {"abs_step": 5, "min": 30, "max": 95},
    "trail_atr_mult": {"abs_step": 0.3, "min": 0.3, "max": 5.0},
    "breakeven_offset_percent": {"abs_step": 0.05, "min": 0.0, "max": 1.0},
}


def is_macro_key(key: str) -> bool:
    return key in MACRO_KEYS


def clamp_step(key: str, current, proposed, max_step_pct: float) -> Tuple[float, bool]:
    """Änderung auf eine sichere Schrittweite begrenzen (rein, testbar).

    Rückgabe: (erlaubter Wert, wurde begrenzt?). Verhindert Sprünge wie
    „SL von 0.4 % auf 2.4 %" nach wenigen Trades.
    """
    spec = MACRO_KEYS.get(key)
    try:
        proposed = float(proposed)
    except (TypeError, ValueError):
        return proposed, False
    if spec is None or current is None:
        return proposed, False
    try:
        current = float(current)
    except (TypeError, ValueError):
        return proposed, False
    limit = min(float(spec["abs_step"]),
                abs(current) * max(1.0, float(max_step_pct)) / 100 or float(spec["abs_step"]))
    limit = max(limit, 1e-9)
    delta = proposed - current
    if abs(delta) <= limit:
        value = proposed
        clamped = False
    else:
        value = current + (limit if delta > 0 else -limit)
        clamped = True
    value = max(float(spec["min"]), min(float(spec["max"]), value))
    return round(value, 6), clamped


def evaluate_macro(settings: Dict, sample: int, confirmations: int) -> Dict:
    """Datenlage + Bestätigungen für eine Makro-Änderung prüfen (rein, testbar)."""
    cfg = {**DEFAULT_SETTINGS, **(settings or {})}
    if not cfg.get("enabled", True):
        return {"validated": True, "sample": sample, "required": 0,
                "confirmations": confirmations, "required_confirmations": 0,
                "reason": "Validierung deaktiviert"}
    need_trades = int(cfg["macro_min_trades"])
    need_conf = int(cfg["macro_min_confirmations"])
    if sample < need_trades:
        return {"validated": False, "sample": sample, "required": need_trades,
                "confirmations": confirmations, "required_confirmations": need_conf,
                "reason": f"Struktur-Parameter: nur {sample} von {need_trades} nötigen "
                          f"abgeschlossenen Trades als Grundlage"}
    if confirmations < need_conf:
        return {"validated": False, "sample": sample, "required": need_trades,
                "confirmations": confirmations, "required_confirmations": need_conf,
                "reason": f"Struktur-Parameter: {confirmations} von {need_conf} nötigen "
                          f"Bestätigungen – die Datenlage muss die Änderung mehrfach zeigen"}
    return {"validated": True, "sample": sample, "required": need_trades,
            "confirmations": confirmations, "required_confirmations": need_conf,
            "reason": f"{sample} Trades und {confirmations} Bestätigungen"}



def sample_size(stats: Dict, scope: str, symbol: Optional[str] = None) -> int:
    """Verfügbare Stichprobe für die Bewertung (rein, testbar)."""
    stats = stats or {}
    if scope == "candidate" and symbol:
        d = (stats.get("by_candidate") or {}).get(symbol) or {}
        return int(d.get("trades") or 0)
    if scope == "coin" and symbol:
        d = (stats.get("by_symbol") or {}).get(symbol) or {}
        return int(d.get("trades") or 0)
    totals = stats.get("totals") or {}
    return int(totals.get("closed_trades") or 0)


def evaluate_change(settings: Dict, stats: Dict, scope: str,
                    symbol: Optional[str] = None) -> Dict:
    """Reicht die Datenlage für eine KI-Einstellungs-Änderung? (rein, testbar)"""
    cfg = {**DEFAULT_SETTINGS, **(settings or {})}
    if not cfg.get("enabled", True):
        return {"validated": True, "sample": sample_size(stats, scope, symbol),
                "required": 0, "reason": "Validierung deaktiviert"}
    need = int(cfg["min_symbol_trades"] if scope == "coin" else cfg["min_closed_trades"])
    have = sample_size(stats, scope, symbol)
    if have >= need:
        return {"validated": True, "sample": have, "required": need,
                "reason": f"{have} geschlossene Trades als Grundlage"}
    label = f"für {symbol}" if scope == "coin" and symbol else "insgesamt"
    return {"validated": False, "sample": have, "required": need,
            "reason": f"Nur {have} geschlossene Trades {label} – "
                      f"mindestens {need} nötig, um diese Änderung zu validieren"}


def evaluate_lesson(settings: Dict, stats: Dict, removal: bool = False) -> Dict:
    """Reicht die Datenlage für eine neue Lektion bzw. deren Verwerfen?"""
    cfg = {**DEFAULT_SETTINGS, **(settings or {})}
    totals = (stats or {}).get("totals") or {}
    have = int(totals.get("closed_trades") or 0) + \
        int(totals.get("signal_wins") or 0) + int(totals.get("signal_losses") or 0)
    if not cfg.get("enabled", True):
        return {"validated": True, "sample": have, "required": 0,
                "reason": "Validierung deaktiviert"}
    need = int(cfg["min_removal_results"] if removal else cfg["min_lesson_results"])
    if have >= need:
        return {"validated": True, "sample": have, "required": need,
                "reason": f"{have} ausgewertete Ergebnisse"}
    return {"validated": False, "sample": have, "required": need,
            "reason": f"Nur {have} ausgewertete Ergebnisse – mindestens {need} nötig"}


class ValidationGate:
    """Persistente Einstellungen der Validierungsschwellen."""

    def __init__(self):
        self.db = None
        self.settings: Dict = dict(DEFAULT_SETTINGS)

    def setup(self, db):
        self.db = db

    async def load(self) -> Dict:
        try:
            doc = await self.db.settings.find_one({"_id": DOC_ID})
        except Exception as e:
            logger.warning(f"Validierungs-Einstellungen laden fehlgeschlagen: {e}")
            return dict(self.settings)
        if doc:
            for k in DEFAULT_SETTINGS:
                if k in doc:
                    self.settings[k] = doc[k]
        else:
            await self.db.settings.update_one({"_id": DOC_ID}, {"$set": dict(self.settings)},
                                              upsert=True)
        return dict(self.settings)

    async def update(self, updates: Dict) -> Dict:
        if "enabled" in updates:
            self.settings["enabled"] = bool(updates["enabled"])
        for key, lo, hi in (("min_closed_trades", 0, 200), ("min_symbol_trades", 0, 100),
                            ("min_lesson_results", 0, 200), ("min_removal_results", 0, 300),
                            ("min_lesson_confirmations", 1, 10),
                            ("macro_min_trades", 0, 500), ("macro_min_confirmations", 1, 20),
                            ("macro_max_step_pct", 1, 100),
                            ("macro_confirm_window_days", 1, 90)):
            if key in updates:
                try:
                    self.settings[key] = max(lo, min(hi, int(updates[key])))
                except (TypeError, ValueError):
                    pass
        await self.db.settings.update_one({"_id": DOC_ID}, {"$set": dict(self.settings)},
                                          upsert=True)
        return dict(self.settings)

    def change(self, stats: Dict, scope: str, symbol: Optional[str] = None) -> Dict:
        return evaluate_change(self.settings, stats, scope, symbol)

    def lesson(self, stats: Dict, removal: bool = False) -> Dict:
        return evaluate_lesson(self.settings, stats, removal=removal)

    def macro(self, sample: int, confirmations: int) -> Dict:
        return evaluate_macro(self.settings, sample, confirmations)

    def clamp(self, key: str, current, proposed) -> Tuple[float, bool]:
        return clamp_step(key, current, proposed,
                          self.settings.get("macro_max_step_pct", 20))

    def prompt_block(self) -> str:
        s = self.settings
        if not s.get("enabled", True):
            return ("=== DATEN-VALIDIERUNG (AUS) ===\n"
                    "Deine Änderungen brauchen aktuell keine Mindest-Stichprobe – "
                    "bleibe trotzdem datenbasiert.")
        return (
            "=== DATEN-VALIDIERUNG (AKTIV – Pflicht) ===\n"
            f"Einstellungs-Änderungen brauchen mind. {s['min_closed_trades']} geschlossene "
            f"Trades (Engine) bzw. {s['min_symbol_trades']} pro Coin. Neue/geschärfte "
            f"Lektionen mind. {s['min_lesson_results']} ausgewertete Ergebnisse, das "
            f"Verwerfen einer Lektion mind. {s['min_removal_results']}.\n"
            f"NEUE Lektionen werden zusätzlich erst aktiv, wenn du dieselbe Erkenntnis in "
            f"mind. {s.get('min_lesson_confirmations', 2)} getrennten Lernläufen wiedererkannt "
            "hast – verwende dafür EXAKT denselben Titel wie beim ersten Vorschlag (die "
            "Kandidaten stehen im Prompt). Bis dahin ist die Lektion nur ein Kandidat.\n"
            "Ohne diese Datenbasis: KEINE Vorschläge, KEINE neuen Lektionen, KEIN Verwerfen – "
            "nicht validierte Wünsche werden automatisch als 'needs_data' geparkt.\n"
            f"STRUKTUR-/MAKRO-PARAMETER ({', '.join(sorted(MACRO_KEYS))}) sind besonders "
            f"geschützt: mind. {s['macro_min_trades']} abgeschlossene Trades UND "
            f"{s['macro_min_confirmations']} unabhängige Bestätigungen derselben Richtung, "
            f"und pro Schritt maximal {s['macro_max_step_pct']}% des aktuellen Werts (harte "
            "Obergrenzen zusätzlich). Ein einzelner Verlust-Trade ist NIE ein Grund, den "
            "Stop-Loss oder das CRV zu verschieben – schlage die Richtung stattdessen erneut "
            "vor, sobald weitere Trades dasselbe Bild zeigen (die Bestätigungen werden "
            "automatisch gezählt).\n"
            "Makro-Parameter kannst du auch pro EIGENER STRATEGIE (Kandidat) anpassen: "
            'setze dazu in config_changes "symbol": "cand_xxxx" – die Stichprobe wird dann '
            "aus den Trades genau dieser Strategie berechnet (Schutz vor Overfitting).\n"
            "Änderungen, die der TRADER wünscht, gelten sofort und ohne Validierung. Du sollst "
            "sie aber nicht blind hinnehmen: sage ehrlich deine Meinung, wenn deine Daten "
            "dagegen sprechen."
        )

    def status(self) -> Dict:
        return {"settings": dict(self.settings)}


validation_gate = ValidationGate()
