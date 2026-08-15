"""
KI Trader – parameterlose Strategie, gesteuert von der externen KI (ai_engine).
Die Signale werden direkt von der Engine emittiert; analyze() liefert nur den
Live-Zustand für die UI (Regel-Kreise, Bias, Konfidenz).
"""
from typing import Dict, List, Optional
from strategies.base_strategy import BaseStrategy


class AITraderStrategy(BaseStrategy):
    STRATEGY_ID = "ai_trader"
    STRATEGY_NAME = "KI Trader"
    STRATEGY_DESCRIPTION = ("Externe KI analysiert Kurs (Multi-Timeframe) + News und tradet "
                            "eigenständig. Keine Parameter – Steuerung per Chat (⚙-Symbol).")
    STRATEGY_TIMEFRAME = "1m"
    DEFAULT_PARAMS = {}

    def analyze(self, candles: List[Dict], symbol: str, params: Dict) -> Optional[Dict]:
        from services.ai_engine import ai_engine
        price = candles[-1]["close"] if candles else 0
        cfg = ai_engine.config
        d = ai_engine.decisions.get(symbol)
        fresh = ai_engine.is_fresh(d)
        action = (d or {}).get("action") if fresh else None
        conf = int((d or {}).get("confidence", 0)) if fresh else 0
        min_conf = cfg.get("min_confidence", 65)
        news_impact = (d or {}).get("news_impact", "neutral") if fresh else "neutral"
        reasoning = (d or {}).get("reasoning", "") if fresh else ""
        is_long = action == "LONG"
        is_short = action == "SHORT"
        active = fresh
        conf_ok = conf >= min_conf
        news_long_ok = news_impact != "negative"
        news_short_ok = news_impact != "positive"

        rules = [
            {"id": "ai_active", "label": "KI-Analyse",
             "description": ("Frische KI-Analyse vorhanden" if fresh else
                             ("KI aktiv – wartet auf nächste Analyse" if cfg.get("enabled")
                              else "KI Trader ist ausgeschaltet (⚙ öffnen)")),
             "long": active, "short": active},
            {"id": "ai_direction", "label": "KI-Richtung",
             "description": (f"KI sieht {action}: {reasoning[:140]}" if action in ("LONG", "SHORT")
                             else (f"KI: HOLD – {reasoning[:140]}" if reasoning else "Keine Richtung – KI wartet auf Setup")),
             "long": is_long, "short": is_short},
            {"id": "ai_confidence", "label": "Konfidenz",
             "description": f"{conf}% (Minimum {min_conf}%)",
             "long": is_long and conf_ok, "short": is_short and conf_ok},
            {"id": "ai_news", "label": "News-Lage",
             "description": f"News-Einfluss: {news_impact}",
             "long": is_long and news_long_ok, "short": is_short and news_short_ok},
        ]
        long_count = sum(1 for r in rules if r["long"])
        short_count = sum(1 for r in rules if r["short"])
        bias = "LONG" if is_long else ("SHORT" if is_short else None)
        return {
            "indicators": {
                "price": round(price, 6),
                "rsi": (d or {}).get("rsi", 0),
                "confidence": conf,
                "ai_action": action or "HOLD",
            },
            "rules": rules,
            "bias": bias,
            "long_count": long_count,
            "short_count": short_count,
            "rules_total": len(rules),
            "signal_type": None,   # Signale emittiert die Engine direkt
            "is_pre_signal": False,
            "levels": None,
        }

    def get_metadata(self) -> Dict:
        meta = super().get_metadata()
        meta["is_ai"] = True
        return meta
