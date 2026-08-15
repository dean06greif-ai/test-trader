"""
Strategy Registry - built-in + dynamically loaded custom strategies.
"""
from typing import Dict, List, Optional
from strategies.scalping_strategy import ScalpingStrategy
from strategies.rsi_only_strategy import RSIOnlyStrategy
from strategies.bollinger_reversion_strategy import BollingerReversionStrategy
from strategies.ema_pullback_scalping_strategy import EMAPullbackScalpingStrategy
from strategies.ict_liquidity_sweep_strategy import ICTLiquiditySweepStrategy
from strategies.macd_rsi_strategy import MACDRSIStrategy
from strategies.bollinger_squeeze_strategy import BollingerSqueezeStrategy
from strategies.vwap_reversion_strategy import VWAPReversionStrategy
from strategies.stochastic_reversal_strategy import StochasticReversalStrategy
from strategies.pbd_model_strategy import PBDModelStrategy
from strategies.ai_trader_strategy import AITraderStrategy
from strategies.nnfx_strategies import (NNFXBreakoutStrategy, NNFXReversionStrategy,
                                        NNFXTrendStrategy)
from strategies.trend_surfer_strategy import TrendSurferStrategy
from strategies.custom_strategy import CustomStrategy
from strategies.base_strategy import BaseStrategy


class StrategyRegistry:
    def __init__(self):
        self._strategies: Dict[str, BaseStrategy] = {}
        self._custom_ids = set()
        self._register_defaults()

    def _register_defaults(self):
        self.register(ScalpingStrategy())
        self.register(EMAPullbackScalpingStrategy())
        self.register(RSIOnlyStrategy())
        self.register(BollingerReversionStrategy())
        self.register(ICTLiquiditySweepStrategy())
        self.register(MACDRSIStrategy())
        self.register(BollingerSqueezeStrategy())
        self.register(VWAPReversionStrategy())
        self.register(StochasticReversalStrategy())
        self.register(PBDModelStrategy())
        self.register(AITraderStrategy())
        self.register(NNFXTrendStrategy())
        self.register(NNFXReversionStrategy())
        self.register(NNFXBreakoutStrategy())
        self.register(TrendSurferStrategy())

    def register(self, strategy: BaseStrategy):
        self._strategies[strategy.STRATEGY_ID] = strategy

    def get(self, strategy_id: str) -> Optional[BaseStrategy]:
        return self._strategies.get(strategy_id)

    def list_all(self) -> List[Dict]:
        return [s.get_metadata() for s in self._strategies.values()]

    def list_ids(self) -> List[str]:
        return list(self._strategies.keys())

    def get_default(self) -> BaseStrategy:
        return self._strategies["scalping_4_rules"]

    # ---- custom strategies ----
    def load_custom(self, definitions: List[Dict]):
        # remove existing custom
        for cid in list(self._custom_ids):
            self._strategies.pop(cid, None)
        self._custom_ids.clear()
        for d in definitions:
            strat = CustomStrategy(d)
            self.register(strat)
            self._custom_ids.add(strat.STRATEGY_ID)

    def upsert_custom(self, definition: Dict):
        strat = CustomStrategy(definition)
        self.register(strat)
        self._custom_ids.add(strat.STRATEGY_ID)

    def list_custom_definitions(self) -> List[Dict]:
        """Definitionen aller Custom-Strategien (z.B. für den lokalen Worker)."""
        out = []
        for cid in self._custom_ids:
            s = self._strategies.get(cid)
            if s is not None and getattr(s, "definition", None):
                out.append(s.definition)
        return out

    def remove_custom(self, strategy_id: str):
        if strategy_id in self._custom_ids:
            self._strategies.pop(strategy_id, None)
            self._custom_ids.discard(strategy_id)


registry = StrategyRegistry()
