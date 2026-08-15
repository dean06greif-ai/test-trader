"""Editierbare Basis-Strategie "Tim Flossbach" (Etappe 5).

Trendfolge mit Markt-Struktur und Liquiditäts-Einstieg:
- Handel NUR in Richtung des übergeordneten Trends (EMA 200 + HH/HL-Struktur)
- Einstieg nach einem Liquidity Grab (Sweep der letzten Tiefs/Hochs mit
  Rückkehr) – der Markt holt sich erst die Liquidität, dann kommt der Move
- Kein Einstieg an FOMC-Meeting-Tagen (Event-Filter)
- Stop hinter der Struktur (letztes Swing-Tief/-Hoch), Ziel = 2R

Wird beim Server-Start EINMALIG als Custom-Strategie angelegt (idempotent):
Anschließend ist sie im Strategie-Builder frei editierbar (Regeln, Perioden,
SL/TP) – Nutzer-Änderungen werden nie überschrieben.
"""
import logging

logger = logging.getLogger(__name__)

FLOSSBACH_ID = "tim_flossbach"

FLOSSBACH_DEFINITION = {
    "id": FLOSSBACH_ID,
    "name": "Tim Flossbach (Basis)",
    "description": ("Trendfolge-Basis: EMA-200-Trend + intakte Markt-Struktur "
                    "(HH/HL bzw. LH/LL), Einstieg nach Liquidity Grab, kein "
                    "Einstieg am FOMC-Tag. Stop hinter der Struktur, Ziel 2R. "
                    "Frei editierbar – Regeln und Parameter anpassen."),
    "timeframe": "1h",
    "indicators": {
        "ema200_period": 200,
        "swing_lookback": 10,
        "struct_pivot_wing": 3,
        "bos_window": 10,
    },
    "long_rules": [
        {"indicator": "dist_ema200_pct", "op": ">", "value": 0,
         "label": "Preis über EMA 200 (Aufwärtstrend)"},
        {"indicator": "market_structure", "op": ">=", "value": 1,
         "label": "Struktur intakt: höhere Hochs + höhere Tiefs"},
        {"indicator": "liq_sweep_low", "op": ">=", "value": 1,
         "label": "Liquidity Grab unter dem letzten Tief (bullisch)"},
        {"indicator": "fomc_today", "op": "<=", "value": 0,
         "label": "Kein FOMC-Meeting-Tag"},
    ],
    "short_rules": [
        {"indicator": "dist_ema200_pct", "op": "<", "value": 0,
         "label": "Preis unter EMA 200 (Abwärtstrend)"},
        {"indicator": "market_structure", "op": "<=", "value": -1,
         "label": "Struktur intakt: tiefere Hochs + tiefere Tiefs"},
        {"indicator": "liq_sweep_high", "op": ">=", "value": 1,
         "label": "Liquidity Grab über dem letzten Hoch (bärisch)"},
        {"indicator": "fomc_today", "op": "<=", "value": 0,
         "label": "Kein FOMC-Meeting-Tag"},
    ],
    "sl_mode": "structure",
    "structure_lookback": 10,
    "sl_ticks": 4,
    "crv_target": 2.0,
    "seeded": True,   # Kennzeichen: von Etappe 5 angelegt
}


async def ensure_flossbach_seed(db) -> bool:
    """Basis-Strategie anlegen, falls (und nur falls) sie noch nicht existiert.
    Nutzer-Anpassungen bleiben unangetastet. True = neu angelegt."""
    try:
        existing = await db.custom_strategies.find_one({"id": FLOSSBACH_ID})
        if existing:
            return False
        await db.custom_strategies.insert_one(dict(FLOSSBACH_DEFINITION))
        logger.info("Basis-Strategie 'Tim Flossbach' angelegt (Etappe 5)")
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Flossbach-Seed fehlgeschlagen: {e}")
        return False
