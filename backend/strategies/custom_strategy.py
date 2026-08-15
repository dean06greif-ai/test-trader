"""
Custom, user-defined strategy engine.
A definition (stored in MongoDB) describes indicators + long/short rules.
Rules: {"indicator", "op", "value", "label"} where
  value: number OR indicator-name string
"""
import re
from typing import Dict, List, Optional

import numpy as np

from strategies.base_strategy import BaseStrategy
from strategies import custom_params

_TF_PARAM_RE = re.compile(r"^(long|short)\d+_tf$")

INDICATORS = [
    "price", "rsi", "ema_fast", "ema_slow", "sma", "ema_gap_pct", "ha_color",
    "macd", "macd_signal", "macd_hist",
    "bb_upper", "bb_middle", "bb_lower", "bb_width_pct",
    "atr", "atr_pct", "vwap",
    "stoch_k", "stoch_d",
    "volume", "volume_sma", "rel_volume",
    "price_change_pct", "recent_high", "recent_low",
    # Trend-/Momentum-Erweiterung (Parität zum Fast-Path in services/fast_sim.py):
    "adx", "plus_di", "minus_di", "cci",
    "keltner_upper", "keltner_middle", "keltner_lower",
    "donchian_high", "donchian_low",
    # Etappe 5 – Struktur / Liquidität / Trendkanal / Range / Events:
    "market_structure", "bos_up", "bos_dn",
    "dist_support_pct", "dist_resistance_pct",
    "eq_high_dist_pct", "eq_low_dist_pct",
    "liq_sweep_low", "liq_sweep_high",
    "channel_pos", "channel_slope_pct", "range_pos",
    "dist_ema200_pct", "fomc_today", "days_to_fomc",
    # Zeit-Filter (Stunde 0-23, Europe/Berlin) – mit in_range/not_in_range
    "hour",
]

INDICATOR_META = {
    "price": {"label": "Preis", "group": "Preis"},
    "rsi": {"label": "RSI", "group": "Momentum"},
    "ema_fast": {"label": "EMA Fast", "group": "Trend"},
    "ema_slow": {"label": "EMA Slow", "group": "Trend"},
    "sma": {"label": "SMA", "group": "Trend"},
    "ema_gap_pct": {"label": "EMA Abstand %", "group": "Trend"},
    "ha_color": {"label": "HA Farbe (1=grün, 0=rot)", "group": "Preis"},
    "macd": {"label": "MACD Linie", "group": "Momentum"},
    "macd_signal": {"label": "MACD Signal", "group": "Momentum"},
    "macd_hist": {"label": "MACD Histogramm", "group": "Momentum"},
    "bb_upper": {"label": "Bollinger Oben", "group": "Volatilität"},
    "bb_middle": {"label": "Bollinger Mitte", "group": "Volatilität"},
    "bb_lower": {"label": "Bollinger Unten", "group": "Volatilität"},
    "bb_width_pct": {"label": "Bollinger Breite %", "group": "Volatilität"},
    "atr": {"label": "ATR", "group": "Volatilität"},
    "atr_pct": {"label": "ATR % vom Preis", "group": "Volatilität"},
    "vwap": {"label": "VWAP", "group": "Volumen"},
    "stoch_k": {"label": "Stochastik %K", "group": "Momentum"},
    "stoch_d": {"label": "Stochastik %D", "group": "Momentum"},
    "volume": {"label": "Volumen", "group": "Volumen"},
    "volume_sma": {"label": "Volumen Ø", "group": "Volumen"},
    "rel_volume": {"label": "Rel. Volumen (x Ø)", "group": "Volumen"},
    "price_change_pct": {"label": "Preisänderung % (Lookback)", "group": "Preis"},
    "recent_high": {"label": "Letztes Hoch (Lookback)", "group": "Struktur"},
    "recent_low": {"label": "Letztes Tief (Lookback)", "group": "Struktur"},
    "adx": {"label": "ADX (Trendstärke)", "group": "Trend"},
    "plus_di": {"label": "DI+ (Aufwärtsdruck)", "group": "Trend"},
    "minus_di": {"label": "DI- (Abwärtsdruck)", "group": "Trend"},
    "cci": {"label": "CCI", "group": "Momentum"},
    "keltner_upper": {"label": "Keltner Oben", "group": "Volatilität"},
    "keltner_middle": {"label": "Keltner Mitte", "group": "Volatilität"},
    "keltner_lower": {"label": "Keltner Unten", "group": "Volatilität"},
    "donchian_high": {"label": "Donchian Hoch", "group": "Struktur"},
    "donchian_low": {"label": "Donchian Tief", "group": "Struktur"},
    "market_structure": {"label": "Markt-Struktur (1=HH/HL, -1=LH/LL, 0=neutral)", "group": "Struktur"},
    "bos_up": {"label": "Struktur-Bruch aufwärts (BOS, 1=aktiv)", "group": "Struktur"},
    "bos_dn": {"label": "Struktur-Bruch abwärts (BOS, 1=aktiv)", "group": "Struktur"},
    "dist_support_pct": {"label": "Abstand Support % (Swing-Level)", "group": "Struktur"},
    "dist_resistance_pct": {"label": "Abstand Widerstand % (Swing-Level)", "group": "Struktur"},
    "eq_high_dist_pct": {"label": "Abstand Equal Highs % (Liquidität)", "group": "Liquidität"},
    "eq_low_dist_pct": {"label": "Abstand Equal Lows % (Liquidität)", "group": "Liquidität"},
    "liq_sweep_low": {"label": "Liquidity Grab unter Tief (1=bullisch)", "group": "Liquidität"},
    "liq_sweep_high": {"label": "Liquidity Grab über Hoch (1=bärisch)", "group": "Liquidität"},
    "channel_pos": {"label": "Trendkanal-Position (0-100)", "group": "Trend"},
    "channel_slope_pct": {"label": "Trendkanal-Steigung %", "group": "Trend"},
    "range_pos": {"label": "Range-Position (0=Tief, 100=Hoch)", "group": "Struktur"},
    "dist_ema200_pct": {"label": "Abstand EMA 200 %", "group": "Trend"},
    "fomc_today": {"label": "FOMC-Meeting-Tag (1=heute)", "group": "Events"},
    "days_to_fomc": {"label": "Tage bis FOMC-Entscheid", "group": "Events"},
    "hour": {"label": "Uhrzeit (Stunde 0-23, Berlin)", "group": "Zeit"},
}

PERIOD_FIELDS = [
    {"key": "ema_fast_period", "label": "EMA Fast Periode", "default": 9},
    {"key": "ema_slow_period", "label": "EMA Slow Periode", "default": 50},
    {"key": "rsi_period", "label": "RSI Periode", "default": 14},
    {"key": "sma_period", "label": "SMA Periode", "default": 20},
    {"key": "macd_fast", "label": "MACD Fast", "default": 12},
    {"key": "macd_slow", "label": "MACD Slow", "default": 26},
    {"key": "macd_signal_period", "label": "MACD Signal", "default": 9},
    {"key": "bb_period", "label": "Bollinger Periode", "default": 20},
    {"key": "bb_std", "label": "Bollinger Std-Abw.", "default": 2.0},
    {"key": "atr_period", "label": "ATR Periode", "default": 14},
    {"key": "stoch_k_period", "label": "Stochastik %K", "default": 14},
    {"key": "stoch_d_period", "label": "Stochastik %D", "default": 3},
    {"key": "volume_sma_period", "label": "Volumen Ø Periode", "default": 20},
    {"key": "change_lookback", "label": "Preisänderung Lookback", "default": 5},
    {"key": "swing_lookback", "label": "Hoch/Tief Lookback", "default": 10},
    {"key": "adx_period", "label": "ADX Periode", "default": 14},
    {"key": "cci_period", "label": "CCI Periode", "default": 20},
    {"key": "keltner_period", "label": "Keltner Periode", "default": 20},
    {"key": "keltner_mult", "label": "Keltner Multiplikator", "default": 2.0},
    {"key": "donchian_period", "label": "Donchian Periode", "default": 20},
    {"key": "struct_pivot_wing", "label": "Struktur-Pivot Flügel (Kerzen)", "default": 3},
    {"key": "bos_window", "label": "BOS/Sweep Gültigkeit (Kerzen)", "default": 10},
    {"key": "channel_period", "label": "Trendkanal Periode", "default": 100},
    {"key": "ema200_period", "label": "EMA-200 Periode", "default": 200},
]

OPERATORS = ["<", ">", "<=", ">=", "==", "!=", "cross_above", "cross_below",
             "in_range", "not_in_range"]


def _fin(v):
    """numpy-Wert -> float oder None (NaN/inf gelten als 'kein Wert')."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (f != f or f in (float("inf"), float("-inf"))) else f


class CustomStrategy(BaseStrategy):
    IS_CUSTOM = True
    STRATEGY_TIMEFRAME = "1m"
    DEFAULT_PARAMS = {}

    def __init__(self, definition: Dict):
        super().__init__()
        # KI-Definitionen werden zuerst normalisiert (Alias-Namen wie "close",
        # "adx14", "crosses_above" → unterstütztes Vokabular). Nicht auswertbare
        # Regeln werden gemeldet, statt still zu 0 Trades zu führen.
        definition, problems = custom_params.normalize_definition(
            definition, INDICATORS, OPERATORS)
        self.definition = definition
        self.rule_problems: List[str] = problems
        self.STRATEGY_ID = definition["id"]
        self.STRATEGY_NAME = definition.get("name", "Custom")
        self.STRATEGY_DESCRIPTION = definition.get("description", "Custom Strategie")
        # Fix: Discovery-/Custom-Strategien nutzen den Timeframe aus der Definition
        # (vorher wurde er ignoriert und immer 1m verwendet)
        self.STRATEGY_TIMEFRAME = definition.get("timeframe") or "1m"
        # Parameter-Suchraum aus der Definition (Perioden + Regel-Schwellen) –
        # dadurch kann der Parameter-Optimierer auch KI-Strategien optimieren.
        self.DEFAULT_PARAMS = custom_params.build_param_meta(definition)

    def effective_definition(self, params: Optional[Dict] = None) -> Dict:
        """Definition inkl. optimierter Parameter (eine Quelle der Wahrheit für
        Live-Pfad und Fast-Path in ``services.fast_sim``)."""
        return custom_params.apply_params(self.definition, params or {})

    def get_params(self, settings: Dict, symbol: str = None) -> Dict:
        """Wie BaseStrategy, zusätzlich werden Regel-Timeframe-Overrides
        (long1_tf, short2_tf, ... aus dem Optimizer) durchgereicht."""
        result = super().get_params(settings, symbol)
        sources = [settings.get("strategy_params", {}).get(self.STRATEGY_ID, {})]
        if symbol:
            sources.append(settings.get("coin_params", {})
                           .get(self.STRATEGY_ID, {}).get(symbol, {}))
        for src in sources:
            for k, v in (src or {}).items():
                if isinstance(k, str) and _TF_PARAM_RE.match(k):
                    result[k] = v
        return result

    def get_metadata(self) -> Dict:
        meta = super().get_metadata()
        meta["rule_problems"] = list(self.rule_problems)
        return meta

    def analyze(self, candles: List[Dict], symbol: str, params: Dict) -> Optional[Dict]:
        """Regel-Auswertung über den Fast-Path (services.fast_sim) – EINE
        Auswertungs-Logik für Live-Scanner und Backtester (garantierte Parität,
        inkl. dynamischer Indikatoren, Zeit-Filter und Mathe-Ausdrücken)."""
        d = self.effective_definition(params)
        try:
            need = max(int(d.get("indicators", {}).get("ema_slow_period", 50)) + 10, 60)
        except (TypeError, ValueError):
            need = 60
        if len(candles) < need:
            return None
        from services import fast_sim  # lazy (fast_sim importiert dieses Modul)
        fs = fast_sim.FastSeries(candles)
        i = fs.n - 1
        ind_cfg = d.get("indicators", {})
        rsi_arr = fs.get("rsi", ind_cfg)
        ema_f_arr = fs.get("ema_fast", ind_cfg)
        ema_s_arr = fs.get("ema_slow", ind_cfg)
        if np.isnan(rsi_arr[i]) or np.isnan(ema_s_arr[i]):
            return None

        long_rules = d.get("long_rules", [])
        short_rules = d.get("short_rules", [])
        rules = []
        long_evals, short_evals = [], []
        for idx, r in enumerate(long_rules):
            ev = bool(fast_sim._rule_cond(r, fs, ind_cfg)[i])
            long_evals.append(ev)
            rules.append({"id": f"L{idx}", "label": r.get("label") or self._auto_label(r),
                          "description": "LONG Bedingung", "long": ev, "short": False,
                          **({"timeframe": r["timeframe"]} if r.get("timeframe") else {})})
        for idx, r in enumerate(short_rules):
            ev = bool(fast_sim._rule_cond(r, fs, ind_cfg)[i])
            short_evals.append(ev)
            rules.append({"id": f"S{idx}", "label": r.get("label") or self._auto_label(r),
                          "description": "SHORT Bedingung", "long": False, "short": ev,
                          **({"timeframe": r["timeframe"]} if r.get("timeframe") else {})})

        signal_type = None
        if long_evals and all(long_evals):
            signal_type = "LONG"
        elif short_evals and all(short_evals):
            signal_type = "SHORT"

        long_cnt = sum(long_evals)
        short_cnt = sum(short_evals)
        bias = "LONG" if long_cnt > short_cnt else ("SHORT" if short_cnt > long_cnt else None)

        price = float(fs.close[i])
        levels = self._levels(candles, price, signal_type) if signal_type else None

        ema_f = None if np.isnan(ema_f_arr[i]) else float(ema_f_arr[i])
        return {
            "indicators": {"rsi": round(float(rsi_arr[i]), 2),
                           "ema_fast": round(ema_f, 6) if ema_f else 0,
                           "ema_slow": round(float(ema_s_arr[i]), 6),
                           "price": round(price, 6)},
            "rules": rules, "bias": bias,
            "long_count": long_cnt, "short_count": short_cnt,
            "rules_total": len(long_rules) if signal_type == "LONG" else (len(short_rules) or len(long_rules)),
            "signal_type": signal_type, "is_pre_signal": False, "levels": levels,
        }

    def _auto_label(self, r):
        meta = INDICATOR_META.get(r.get("indicator"), {})
        v = r.get("value")
        v_label = INDICATOR_META.get(v, {}).get("label", v) if isinstance(v, str) else v
        return f"{meta.get('label', r.get('indicator'))} {r.get('op')} {v_label}" \
            + (f" @{r['timeframe']}" if r.get("timeframe") else "")

    def _levels(self, candles, entry, side):
        d = self.definition
        crv = float(d.get("crv_target", 2.0))
        if d.get("sl_mode", "percent") == "structure":
            lookback = int(d.get("structure_lookback", 10))
            ticks = int(d.get("sl_ticks", 4))
            tick = entry * 0.0001
            if side == "LONG":
                sl = self.indicators.get_recent_low(candles, lookback) - ticks * tick
            else:
                sl = self.indicators.get_recent_high(candles, lookback) + ticks * tick
        else:
            pct = float(d.get("sl_percent", 2.0)) / 100
            sl = entry * (1 - pct) if side == "LONG" else entry * (1 + pct)
        risk = abs(entry - sl)
        if side == "LONG":
            tp1, tpf = entry + risk, entry + risk * crv
        else:
            tp1, tpf = entry - risk, entry - risk * crv
        return {"entry": round(entry, 6), "stop_loss": round(sl, 6),
                "take_profit_1": round(tp1, 6), "take_profit_full": round(tpf, 6),
                "crv": round(self.indicators.calculate_crv(entry, sl, tpf), 2)}
