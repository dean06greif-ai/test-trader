"""
Base class for all trading strategies.
Each strategy exposes analyze() which returns the CURRENT rule state (for live
circle pre-fill) plus a full signal when all rules align.
"""
from abc import ABC, abstractmethod
from typing import Dict, List, Optional
from services.technical_indicators import TechnicalIndicators


class BaseStrategy(ABC):
    STRATEGY_ID = "base"
    STRATEGY_NAME = "Base Strategy"
    STRATEGY_DESCRIPTION = "Override this in subclass"
    STRATEGY_TIMEFRAME = "1m"
    IS_CUSTOM = False

    DEFAULT_PARAMS = {}

    def __init__(self):
        self.indicators = TechnicalIndicators()

    @abstractmethod
    def analyze(self, candles: List[Dict], symbol: str, params: Dict) -> Optional[Dict]:
        """
        Return:
        {
          "indicators": {...},
          "rules": [{"id","label","description","long":bool,"short":bool}, ...],
          "bias": "LONG"|"SHORT"|None,      # current majority direction
          "signal_type": "LONG"|"SHORT"|None,  # ALL rules aligned -> full signal
          "is_pre_signal": bool,
          "levels": {"entry","stop_loss","take_profit_1","take_profit_full","crv"} | None
        }
        """
        pass

    def get_params(self, settings: Dict, symbol: str = None) -> Dict:
        """Resolve params: defaults -> global overrides -> per-coin overrides."""
        result = {k: meta["value"] for k, meta in self.DEFAULT_PARAMS.items()}
        user = settings.get("strategy_params", {}).get(self.STRATEGY_ID, {})
        for k, v in user.items():
            if k in result:
                result[k] = v
        if symbol:
            coin = settings.get("coin_params", {}).get(self.STRATEGY_ID, {}).get(symbol, {})
            for k, v in coin.items():
                if k in result:
                    result[k] = v
        return result

    def check_signal(self, candles: List[Dict], symbol: str, settings: Dict) -> Optional[Dict]:
        """Return a full-signal dict (backwards-compatible) or None."""
        params = self.get_params(settings, symbol)
        result = self.analyze(candles, symbol, params)
        if not result or not result.get("signal_type"):
            return None
        levels = result.get("levels") or {}
        rules_met = {r["id"]: (r["long"] if result["signal_type"] == "LONG" else r["short"])
                     for r in result.get("rules", [])}
        ind = result.get("indicators", {})
        return {
            "type": result["signal_type"],
            "signal_class": "PRE_SIGNAL" if result.get("is_pre_signal") else "SIGNAL",
            "entry_price": levels.get("entry"),
            "stop_loss": levels.get("stop_loss"),
            "take_profit_1": levels.get("take_profit_1"),
            "take_profit_full": levels.get("take_profit_full"),
            "crv": levels.get("crv", 0),
            "rsi": ind.get("rsi", 0),
            "ema_fast": ind.get("ema_fast", 0),
            "ema_slow": ind.get("ema_slow", 0),
            "rules_met_count": sum(1 for v in rules_met.values() if v),
            "rules_total": len(rules_met),
            "rules_met": rules_met,
            "used_params": params,
        }

    def get_metadata(self) -> Dict:
        return {
            "id": self.STRATEGY_ID,
            "name": self.STRATEGY_NAME,
            "description": self.STRATEGY_DESCRIPTION,
            "timeframe": self.STRATEGY_TIMEFRAME,
            "is_custom": self.IS_CUSTOM,
            "params": self.DEFAULT_PARAMS,
        }
