"""MACD + RSI Momentum: MACD-Cross mit RSI-Zone, Trend- und Volumen-Filter."""
from typing import Dict, List, Optional
import numpy as np
from strategies.base_strategy import BaseStrategy


class MACDRSIStrategy(BaseStrategy):
    STRATEGY_ID = "macd_rsi_momentum"
    STRATEGY_NAME = "MACD + RSI Momentum"
    STRATEGY_DESCRIPTION = ("MACD kreuzt Signallinie, RSI in gesunder Momentum-Zone, "
                            "Preis über/unter Trend-EMA + Volumen-Bestätigung. "
                            "Empfohlen auf 5m-15m.")
    STRATEGY_TIMEFRAME = "1m"

    DEFAULT_PARAMS = {
        "macd_fast": {"value": 12, "min": 5, "max": 30, "step": 1,
                      "label": "MACD Fast", "description": "Schnelle MACD EMA"},
        "macd_slow": {"value": 26, "min": 15, "max": 60, "step": 1,
                      "label": "MACD Slow", "description": "Langsame MACD EMA"},
        "macd_signal_period": {"value": 9, "min": 3, "max": 20, "step": 1,
                               "label": "MACD Signal", "description": "Signallinien-Periode"},
        "rsi_period": {"value": 14, "min": 5, "max": 30, "step": 1,
                       "label": "RSI Periode", "description": "RSI Berechnungs-Periode"},
        "rsi_long_min": {"value": 42, "min": 30, "max": 55, "step": 1,
                         "label": "RSI Long Min", "description": "RSI muss über diesem Wert sein (Long)"},
        "rsi_long_max": {"value": 68, "min": 55, "max": 85, "step": 1,
                         "label": "RSI Long Max", "description": "RSI muss unter diesem Wert sein (Long)"},
        "rsi_short_min": {"value": 32, "min": 15, "max": 45, "step": 1,
                          "label": "RSI Short Min", "description": "RSI muss über diesem Wert sein (Short)"},
        "rsi_short_max": {"value": 58, "min": 45, "max": 70, "step": 1,
                          "label": "RSI Short Max", "description": "RSI muss unter diesem Wert sein (Short)"},
        "ema_trend_period": {"value": 100, "min": 20, "max": 200, "step": 5,
                             "label": "Trend EMA", "description": "Preis über EMA = Long-Regime"},
        "rel_vol_min": {"value": 1.0, "min": 0.5, "max": 3.0, "step": 0.1,
                        "label": "Min. Rel. Volumen", "description": "Volumen ggü. 20er Durchschnitt"},
        "sl_lookback": {"value": 12, "min": 3, "max": 40, "step": 1,
                        "label": "Struktur Lookback", "description": "Kerzen für Swing-SL"},
        "atr_sl_mult": {"value": 1.0, "min": 0.2, "max": 3.0, "step": 0.1,
                        "label": "ATR SL Puffer", "description": "ATR-Puffer hinter der Struktur"},
        "tp1_rr": {"value": 1.0, "min": 0.5, "max": 5.0, "step": 0.1,
                   "label": "TP1 (R)", "description": "Erstes Ziel in R"},
        "tp_rr": {"value": 2.0, "min": 1.0, "max": 8.0, "step": 0.1,
                  "label": "TP voll (R)", "description": "Endziel in R"},
    }

    def analyze(self, candles: List[Dict], symbol: str, params: Dict) -> Optional[Dict]:
        need = max(int(params["macd_slow"]) + int(params["macd_signal_period"]),
                   int(params["ema_trend_period"]), 30) + 10
        if len(candles) < need:
            return None
        ti = self.indicators
        closes = [c["close"] for c in candles]
        price = closes[-1]
        macd, sig, hist = ti.calculate_macd(closes, int(params["macd_fast"]),
                                            int(params["macd_slow"]), int(params["macd_signal_period"]))
        rsi = ti.calculate_rsi(closes, int(params["rsi_period"]))
        ema_t = ti.calculate_ema(closes, int(params["ema_trend_period"]))
        atr = ti.calculate_atr(candles, 14)
        if not macd or macd[-1] is None or sig[-1] is None or rsi[-1] is None or ema_t[-1] is None:
            return None
        rel_vol = ti.relative_volume(candles, 20) or 0.0

        cross_up = (macd[-2] is not None and sig[-2] is not None
                    and macd[-2] <= sig[-2] and macd[-1] > sig[-1])
        cross_dn = (macd[-2] is not None and sig[-2] is not None
                    and macd[-2] >= sig[-2] and macd[-1] < sig[-1])
        r = rsi[-1]
        rsi_l = params["rsi_long_min"] <= r <= params["rsi_long_max"]
        rsi_s = params["rsi_short_min"] <= r <= params["rsi_short_max"]
        trend_l = price > ema_t[-1]
        trend_s = price < ema_t[-1]
        vol_ok = rel_vol >= float(params["rel_vol_min"])

        rules = [
            {"id": "macd_cross", "label": "MACD Cross", "description": "MACD kreuzt Signallinie",
             "long": bool(cross_up), "short": bool(cross_dn)},
            {"id": "rsi_zone", "label": "RSI Zone", "description": "RSI in Momentum-Zone (nicht überkauft/-verkauft)",
             "long": bool(rsi_l), "short": bool(rsi_s)},
            {"id": "trend", "label": "Trend-Filter", "description": "Preis über/unter Trend-EMA",
             "long": bool(trend_l), "short": bool(trend_s)},
            {"id": "volume", "label": "Volumen", "description": "Rel. Volumen über Minimum",
             "long": bool(vol_ok), "short": bool(vol_ok)},
        ]
        signal_long = cross_up and rsi_l and trend_l and vol_ok
        signal_short = cross_dn and rsi_s and trend_s and vol_ok
        signal_type = "LONG" if signal_long else ("SHORT" if signal_short else None)
        long_count = sum(1 for x in rules if x["long"])
        short_count = sum(1 for x in rules if x["short"])
        bias = "LONG" if long_count > short_count else ("SHORT" if short_count > long_count else None)

        levels = None
        if signal_type:
            levels = self._structure_levels(candles, price, signal_type,
                                            int(params["sl_lookback"]),
                                            (atr[-1] or 0.0) * float(params["atr_sl_mult"]),
                                            float(params["tp1_rr"]), float(params["tp_rr"]))
        return {
            "indicators": {"rsi": round(r, 2), "ema_fast": round(macd[-1], 6),
                           "ema_slow": round(sig[-1], 6), "price": round(price, 6),
                           "macd_hist": round(hist[-1], 6) if hist[-1] is not None else 0,
                           "rel_vol": round(rel_vol, 2)},
            "rules": rules, "bias": bias,
            "long_count": long_count, "short_count": short_count, "rules_total": len(rules),
            "signal_type": signal_type, "is_pre_signal": False, "levels": levels,
        }

    def _structure_levels(self, candles, entry, side, lookback, atr_buf, tp1_rr, tp_rr):
        ti = self.indicators
        last = candles[-1]
        if side == "LONG":
            struct = ti.get_recent_low(candles, lookback)
            struct = min(struct, last["low"]) if struct is not None else last["low"]
            sl = struct - atr_buf
            risk = entry - sl
            if risk <= 0:
                risk = entry * 0.003
                sl = entry - risk
            tp1, tpf = entry + risk * tp1_rr, entry + risk * tp_rr
        else:
            struct = ti.get_recent_high(candles, lookback)
            struct = max(struct, last["high"]) if struct is not None else last["high"]
            sl = struct + atr_buf
            risk = sl - entry
            if risk <= 0:
                risk = entry * 0.003
                sl = entry + risk
            tp1, tpf = entry - risk * tp1_rr, entry - risk * tp_rr
        return {"entry": round(entry, 6), "stop_loss": round(sl, 6),
                "take_profit_1": round(tp1, 6), "take_profit_full": round(tpf, 6),
                "crv": round(ti.calculate_crv(entry, sl, tpf), 2)}

    # ----------------------- Vectorized Fast-Path -----------------------
    @staticmethod
    def vectorized_signals(fs, params: Dict) -> Optional[Dict]:
        """MACD-Cross + RSI-Zone + Trend + Volumen -- vollständig vektorisiert."""
        macd_fast = int(params["macd_fast"])
        macd_slow = int(params["macd_slow"])
        macd_signal = int(params["macd_signal_period"])
        rsi_period = int(params["rsi_period"])
        ema_trend = int(params["ema_trend_period"])
        rel_vol_min = float(params["rel_vol_min"])

        d = {"macd_fast": macd_fast, "macd_slow": macd_slow,
             "macd_signal_period": macd_signal, "rsi_period": rsi_period,
             "volume_sma_period": 20}
        macd = fs.get("macd", d)
        sig = fs.get("macd_signal", d)
        rsi = fs.get("rsi", d)
        # eigener Trend-EMA-Cache: nutze denselben "ema"-Cache-Slot
        ema_t = fs.get("ema_slow", {"ema_slow_period": ema_trend})
        rel_vol = fs.get("rel_volume", d)
        close = fs.close

        # MACD-Cross (Übergangskante an Index i, vergleicht i-1 vs. i)
        with np.errstate(invalid="ignore"):
            macd_prev = np.concatenate([[np.nan], macd[:-1]])
            sig_prev = np.concatenate([[np.nan], sig[:-1]])
            cross_up = (macd_prev <= sig_prev) & (macd > sig)
            cross_dn = (macd_prev >= sig_prev) & (macd < sig)
            cross_up &= ~np.isnan(macd_prev) & ~np.isnan(sig_prev)
            cross_dn &= ~np.isnan(macd_prev) & ~np.isnan(sig_prev)

            rsi_l = (rsi >= float(params["rsi_long_min"])) & (rsi <= float(params["rsi_long_max"]))
            rsi_s = (rsi >= float(params["rsi_short_min"])) & (rsi <= float(params["rsi_short_max"]))
            trend_l = close > ema_t
            trend_s = close < ema_t
            vol_ok = rel_vol >= rel_vol_min

        valid = ~np.isnan(macd) & ~np.isnan(sig) & ~np.isnan(rsi) & ~np.isnan(ema_t)
        long_ok = cross_up & rsi_l & trend_l & vol_ok & valid
        short_ok = cross_dn & rsi_s & trend_s & vol_ok & valid

        warmup = max(macd_slow + macd_signal, ema_trend, 30) + 10
        return {"long": long_ok, "short": short_ok,
                "warmup": warmup, "rules_total": 4, "rsi": rsi}
