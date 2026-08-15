"""Plattform-Wissen + Whitelist der KI-veränderbaren Trade-Einstellungen.

Der KI Trader bekommt hierüber (a) ein statisches Verständnis der gesamten
Website (unabhängig vom Chat) und (b) eine strikte, validierte Whitelist,
welche Einstellungen er selbst ändern darf. max_capital / mode sind IMMER tabu.
"""
from typing import Dict, Tuple

PLATFORM_KNOWLEDGE = (
    "Diese Website ist eine private Krypto-Daytrading-Plattform (React + FastAPI, Bitunix-Futures-Anbindung):\n"
    "- Märkte: Top-10 Krypto-Coins (BTC, ETH, BNB, SOL, XRP, ADA, DOGE, AVAX, DOT, POL als USDT-Futures) plus Gold/Silber/Öl.\n"
    "- Scanner: 1-Minuten-Kerzen laufen live durch mehrere Strategien (Scalping, MACD+RSI, Bollinger, VWAP, ICT Liquidity Sweep, EMA-Pullback, eigene Custom-Strategien u.a.). Erfüllte Regeln erzeugen SIGNALE.\n"
    "- Signal-Pipeline: Signal -> Speicherung -> Telegram-Benachrichtigung -> Auto-Trader. Jedes Signal wird automatisch als Win/Loss ausgewertet, sobald TP1 oder SL erreicht wird.\n"
    "- Auto-Trading: Pro (Strategie, Coin) schaltbar: aus / Paper (Simulation mit echter Gebühren-/Hebellogik) / Live (echte Bitunix-Order). Trade-Einstellungen pro Coin: Hebel, Auto-Hebel, SL-Modus (Struktur/Fest %/ATR), TP1 & TP-Full (als CRV), Teilverkauf bei TP1, Break-Even-Verschiebung, Profit-Secure, Gebühren.\n"
    "- Kapital: Der investierte Betrag pro Trade (max_capital) und die globale Kapital-Zuweisung werden AUSSCHLIESSLICH vom Trader festgelegt – du darfst sie NIE ändern oder vorschlagen.\n"
    "- Weitere Module: Backtester, Optimizer, Strategie-Finder, Deep Analytics, lokaler Worker für Rechenjobs.\n"
    "- DEINE Rolle ('KI Trader', strategy_id ai_trader): Du analysierst periodisch alle Coins (Multi-Timeframe + News), triffst LONG/SHORT/HOLD-Entscheidungen mit Konfidenz und SL/TP-Vorschlägen. Entscheidungen über der Mindest-Konfidenz werden als Signale durch die normale Pipeline emittiert und – je nach Paper/Live-Schaltung pro Coin – automatisch getradet.\n"
    "- Du lernst kontinuierlich aus deinen EIGENEN Ergebnissen (Signal-Ausgänge + geschlossene Paper-/Live-Trades) und kannst – je nach Autonomie-Einstellung – deine eigenen Trade-Einstellungen anpassen (nur nicht den investierten Betrag oder den Paper/Live-Modus).\n"
    "- KI-TEAM: Du bist Teil eines Teams spezialisierter KI-Rollen, die zusammenarbeiten: "
    "Analyst (regelmäßige Analysen), Tiefen-Analyst (sehr tiefe Analysen zu festen Uhrzeiten – seine Empfehlungen stark gewichten), "
    "Forschungs-Analyst (wertet ALLE Rechenergebnisse der Website aus – Backtests, Optimizer-Läufe inkl. Walk-Forward/Robustheit, "
    "Regime-Lab, Regime-Analysen – und übergibt dir daraus belastbares Handelswissen: welche Strategien/Parameter in welchen "
    "Marktbedingungen funktionieren), "
    "Markt-Beobachter (sammelt laufend den gemessenen Marktzustand: Trend, Volatilität, ATR, Volumen, Range-Position – "
    "diese Zeitreihe ist die Trainingsgrundlage des ML-Labors), "
    "News-Wächter (überwacht News + Weltwirtschaftskalender 24/7 und meldet marktrelevante Ereignisse), "
    "Chat-Assistent, Lern-Modul und Tages-Reporter. Jede Rolle kann ein eigenes Modell, Handelszeiten und eine Fallback-KI haben.\n"
    "- ML-LABOR: Optuna sucht die besten Hyperparameter, ein XGBoost-Modell lernt aus den echten Ergebnissen, WELCHE "
    "Marktbedingungen Gewinne liefern. Du erhältst Gewinnwahrscheinlichkeiten und Feature-Wichtigkeiten als Zusatzsignal – "
    "bei schwacher Vorhersagegüte (AUC < 0.55) nur schwach gewichten.\n"
    "- KI-GEDÄCHTNIS: Erkenntnisse, ML-Befunde und Ideen des Teams werden dauerhaft gespeichert (MongoDB + optional Supabase) "
    "und dir als Wissensblock mitgegeben – nutze sie aktiv.\n"
    "- GEWICHTUNG: Lektionen und Analysen stärkerer Modelle (Gewicht 'hoch') zählen mehr als die schwächerer Modelle ('basis').\n"
    "- LESERECHTE: Du siehst die Trade-Historie und Winrate ALLER Strategien der Website und sollst aus deren Stärken/Schwächen lernen.\n"
    "- MASTERPROMPT: Der Trader hinterlegt einen MasterPrompt mit harten Regeln. Er ist das OBERSTE GEBOT – "
    "über Lektionen, über Empfehlungen anderer Rollen, über deinen eigenen Analysen. Nur der Trader ändert ihn.\n"
    "- LEKTIONEN: Der Trader kann Lektionen selbst schreiben, bearbeiten oder löschen. Als 'vom Trader "
    "festgelegt' markierte Lektionen sind für dich unveränderlich.\n"
    "- DATEN-VALIDIERUNG: Eigene Einstellungs-Änderungen und neue Lektionen brauchen eine Mindest-Stichprobe "
    "an echten Ergebnissen. Wünsche des Traders gelten sofort – du sollst sie aber ehrlich kommentieren, "
    "wenn deine Daten dagegen sprechen.\n"
    "- STRATEGIE-LABOR: Neue eigene Strategien laufen zuerst als Ghost-Trades (reine Simulation), danach – "
    "nach Freigabe des Traders – Paper und erst dann Live. News-getriebene und live nachjustierte Trades "
    "sind bewusst NICHT backtestbar; bleibe dort dynamisch."
)

# Whitelist der Coin-Trade-Einstellungen (strategy_coin_configs ai_trader_<SYMBOL>),
# die die KI ändern darf – inkl. harter Grenzen (Werte werden geklemmt).
AI_TUNABLE_COIN_KEYS: Dict[str, Dict] = {
    "leverage": {"type": "number", "min": 1, "max": 50, "desc": "Fester Hebel"},
    "auto_leverage_enabled": {"type": "bool", "desc": "Auto-Hebel an/aus"},
    "auto_lev_max": {"type": "number", "min": 2, "max": 100, "desc": "Max. Auto-Hebel"},
    "sl_mode": {"type": "enum", "values": ["structure", "fixed", "atr"], "desc": "Stop-Loss-Modus"},
    "sl_fixed_percent": {"type": "number", "min": 0.2, "max": 5.0, "desc": "Fester SL in %"},
    "sl_lookback": {"type": "int", "min": 3, "max": 60, "desc": "Struktur-SL Lookback (Kerzen)"},
    "sl_ticks": {"type": "int", "min": 1, "max": 20, "desc": "Struktur-SL Puffer-Ticks"},
    "atr_sl_multiplier": {"type": "number", "min": 0.5, "max": 3.0, "desc": "ATR-SL-Multiplikator"},
    "tp1_crv": {"type": "number", "min": 0.5, "max": 5.0, "desc": "TP1 als CRV"},
    "tp_full_crv": {"type": "number", "min": 0.8, "max": 10.0, "desc": "TP-Full als CRV"},
    "tp1_close_percent": {"type": "int", "min": 10, "max": 90, "desc": "Teilverkauf bei TP1 in %"},
    "breakeven_enabled": {"type": "bool", "desc": "Break-Even an/aus"},
    "be_mode": {"type": "enum", "values": ["tp1", "smart", "crv", "profit_pct", "off"], "desc": "Break-Even-Modus"},
    "be_trigger_crv": {"type": "number", "min": 0.3, "max": 3.0, "desc": "BE-Trigger (CRV)"},
    "be_trigger_profit_pct": {"type": "number", "min": 5, "max": 90, "desc": "BE-Trigger (Profit %)"},
    "profit_secure_enabled": {"type": "bool", "desc": "Profit-Secure an/aus"},
    "profit_secure_trigger_pct": {"type": "number", "min": 10, "max": 90, "desc": "Profit-Secure Trigger %"},
    "profit_lock_pct": {"type": "number", "min": 10, "max": 90, "desc": "Gesicherter Profit-Anteil %"},
}

# Engine-Einstellungen des KI Traders selbst (symbol "ENGINE").
AI_TUNABLE_ENGINE_KEYS: Dict[str, Dict] = {
    "min_confidence": {"type": "int", "min": 35, "max": 90, "desc": "Mindest-Konfidenz für Signale"},
    "cooldown_min": {"type": "int", "min": 0, "max": 240, "desc": "Cooldown zwischen Trades pro Coin (Minuten)"},
}

# Diese Keys darf die KI unter KEINEN Umständen anfassen.
FORBIDDEN_KEYS = {"max_capital", "mode", "enabled", "signals_enabled",
                  "order_type", "fee_percent", "margin_mode"}


def validate_changes(changes: Dict, scope: str = "coin") -> Tuple[Dict, Dict]:
    """Validiert & klemmt KI-Änderungswünsche gegen die Whitelist.
    Rückgabe: (gültige_changes, abgelehnte {key: grund})."""
    spec_map = AI_TUNABLE_ENGINE_KEYS if scope == "engine" else AI_TUNABLE_COIN_KEYS
    valid: Dict = {}
    rejected: Dict = {}
    if not isinstance(changes, dict):
        return {}, {"_": "changes muss ein Objekt sein"}
    for k, v in changes.items():
        if k in FORBIDDEN_KEYS:
            rejected[k] = "verboten (nur der Trader darf das ändern)"
            continue
        spec = spec_map.get(k)
        if not spec:
            rejected[k] = "unbekannt/nicht erlaubt"
            continue
        t = spec["type"]
        try:
            if t == "bool":
                valid[k] = v if isinstance(v, bool) else str(v).lower() in ("true", "1", "an", "on", "ja")
            elif t == "enum":
                if str(v) in spec["values"]:
                    valid[k] = str(v)
                else:
                    rejected[k] = f"ungültiger Wert (erlaubt: {'|'.join(spec['values'])})"
            elif t == "int":
                valid[k] = int(max(spec["min"], min(spec["max"], int(float(v)))))
            else:
                valid[k] = round(max(spec["min"], min(spec["max"], float(v))), 4)
        except (TypeError, ValueError):
            rejected[k] = "ungültiger Typ"
    return valid, rejected


def tunable_spec_text() -> str:
    """Kompakte Spezifikation der erlaubten Keys für den LLM-Prompt."""
    def fmt(spec_map: Dict) -> str:
        out = []
        for k, s in spec_map.items():
            if s["type"] == "bool":
                rng = "true/false"
            elif s["type"] == "enum":
                rng = "|".join(s["values"])
            else:
                rng = f"{s['min']}-{s['max']}"
            out.append(f"  - {k} ({rng}): {s['desc']}")
        return "\n".join(out)
    return ("Erlaubte Coin-Einstellungen (config_changes[].changes):\n" + fmt(AI_TUNABLE_COIN_KEYS)
            + "\nErlaubte Engine-Einstellungen (symbol \"ENGINE\"):\n" + fmt(AI_TUNABLE_ENGINE_KEYS))
