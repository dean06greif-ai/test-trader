"""Bollinger Mean Reversion (RSI-2) strategy.

Statistisch eine der staerksten Daytrading-Strategien (Connors RSI-2 + Bollinger):
~65-68% Winrate in Multi-Jahres-Backtests. Krypto handelt durch 24/7-Liquiditaet
haeufig in Ranges -> Mean Reversion zurueck zum Mittelwert (BB-Basis / VWAP).

Entry nur bei ECHTEM Extrem + Rueckkehr-Bestaetigung:
  * Close unter unterem Bollinger Band (2 StdDev)   -> statistisches Extrem
  * RSI(2) < 10 (Long) / > 90 (Short)               -> Connors-Trigger
  * Reversal-Kerze (Close dreht zurueck)            -> kein fallendes Messer
  * VWAP-Filter (Long unter Fair Value, Short ueber)-> buy low / sell high
  * Trend-Sperre wie bei den anderen Strategien     -> nie gegen starke Impulse

Stops: Struktur (Swing) + ATR-Puffer. TP1 = Bollinger-Basis (Mittelwert),
TP voll = R-Vielfaches. Timeframe 5m (weniger Noise/Fees als 1m).
"""
from typing import Dict, List, Optional
from strategies.base_strategy import BaseStrategy


class BollingerReversionStrategy(BaseStrategy):
    STRATEGY_ID = "bollinger_reversion"
    STRATEGY_NAME = "Bollinger Mean Reversion (RSI-2)"
    STRATEGY_DESCRIPTION = ("Statistische Mean-Reversion: Bollinger-Extrem + RSI(2) + "
                            "Reversal-Bestaetigung + VWAP. Handelt Rueckkehr zum Mittelwert. "
                            "~65% Winrate in Backtests. Trend-Sperre gegen starke Impulse.")
    STRATEGY_TIMEFRAME = "5m"

    DEFAULT_PARAMS = {
        "bb_period": {"value": 20, "min": 10, "max": 50, "step": 1,
                      "label": "BB Periode", "description": "Bollinger Band Berechnungs-Periode"},
        "bb_std": {"value": 2.0, "min": 1.0, "max": 3.5, "step": 0.1,
                   "label": "BB StdDev", "description": "Standardabweichungen fuer die Baender"},
        "rsi_period": {"value": 2, "min": 2, "max": 14, "step": 1,
                       "label": "RSI Periode", "description": "Connors RSI(2) - kurz = schnelle Extreme"},
        "rsi_long_threshold": {"value": 10, "min": 2, "max": 30, "step": 1,
                               "label": "RSI LONG (Oversold)", "description": "RSI unter diesem Wert = LONG-Trigger"},
        "rsi_short_threshold": {"value": 90, "min": 70, "max": 98, "step": 1,
                                "label": "RSI SHORT (Overbought)", "description": "RSI ueber diesem Wert = SHORT-Trigger"},
        "ema_fast_period": {"value": 21, "min": 5, "max": 100, "step": 1,
                            "label": "EMA schnell", "description": "Trend-Filter schnelle EMA"},
        "ema_slow_period": {"value": 50, "min": 20, "max": 200, "step": 1,
                            "label": "EMA langsam", "description": "Trend-Filter langsame EMA"},
        "trend_block_pct": {"value": 0.15, "min": 0.02, "max": 1.0, "step": 0.01,
                            "label": "Trend-Sperre %", "description": "EMA-Steigung ab der ein Gegen-Trade blockiert wird"},
        "block_counter_trend": {"value": 1, "min": 0, "max": 1, "step": 1,
                                "label": "Gegen-Trend sperren", "description": "1 = nicht in starke Impulse reversen"},
        "require_reversal": {"value": 1, "min": 0, "max": 1, "step": 1,
                             "label": "Reversal-Kerze noetig", "description": "1 = Close muss zurueckdrehen (kein fallendes Messer)"},
        "min_confluence": {"value": 1, "min": 0, "max": 3, "step": 1,
                           "label": "Min. Bestaetigungen", "description": "Zusaetzliche Confluences (VWAP/Volumen/Sweep)"},
        "rel_vol_min": {"value": 1.0, "min": 0.5, "max": 5.0, "step": 0.1,
                        "label": "Min. Rel. Volumen", "description": "Volumen ggue. Durchschnitt"},
        "atr_period": {"value": 14, "min": 5, "max": 30, "step": 1,
                       "label": "ATR Periode", "description": "Volatilitaet fuer Stop-Puffer"},
        "atr_sl_mult": {"value": 1.2, "min": 0.2, "max": 3.0, "step": 0.1,
                        "label": "ATR SL Puffer", "description": "ATR-Puffer hinter der Struktur (Anti Stop-Hunt)"},
        "sl_lookback": {"value": 10, "min": 3, "max": 40, "step": 1,
                        "label": "Struktur Lookback", "description": "Kerzen fuer Swing-Tief/-Hoch"},
        "tp_rr": {"value": 1.8, "min": 1.0, "max": 8.0, "step": 0.1,
                  "label": "TP voll (R-Vielfaches)", "description": "Endziel als Vielfaches des Risikos"},
    }

    def analyze(self, candles: List[Dict], symbol: str, params: Dict) -> Optional[Dict]:
        bb_period = int(params["bb_period"])
        rsi_period = int(params["rsi_period"])
        ema_slow_p = int(params["ema_slow_period"])
        atr_period = int(params["atr_period"])
        sl_lookback = int(params["sl_lookback"])
        need = max(bb_period, ema_slow_p, atr_period, sl_lookback, 14) + 10
        if len(candles) < need:
            return None

        ti = self.indicators
        closes = [c["close"] for c in candles]
        price = closes[-1]
        last = candles[-1]
        prev = candles[-2]

        # Bollinger Bands (SMA + StdDev, inline = keine Zusatz-Abhaengigkeit)
        window = closes[-bb_period:]
        basis = sum(window) / bb_period
        variance = sum((x - basis) ** 2 for x in window) / bb_period
        std = variance ** 0.5
        bb_mult = float(params["bb_std"])
        upper = basis + bb_mult * std
        lower = basis - bb_mult * std

        rsi = self._last(ti.calculate_rsi(closes, rsi_period))
        ema_f_arr = ti.calculate_ema(closes, int(params["ema_fast_period"]))
        ema_s_arr = ti.calculate_ema(closes, ema_slow_p)
        ema_f, ema_s = self._last(ema_f_arr), self._last(ema_s_arr)
        if rsi is None or ema_f is None or ema_s is None or std <= 0:
            return None

        prev_f = ema_f_arr[-6] if len(ema_f_arr) >= 6 and ema_f_arr[-6] else None
        slope = (ema_f - prev_f) / prev_f if prev_f else 0.0

        vwap = self._last(ti.calculate_vwap(candles)) or price
        rel_vol = ti.relative_volume(candles, 20) or 0.0
        sweep = ti.liquidity_sweep(candles)
        atr = self._last(ti.calculate_atr(candles, atr_period)) or 0.0

        # Trend-Sperre (wie Smart Money RSI: nie gegen starke Impulse)
        trend_thr = float(params["trend_block_pct"]) / 100.0
        strong_bull = (price > ema_s) and (ema_f > ema_s) and (slope > trend_thr)
        strong_bear = (price < ema_s) and (ema_f < ema_s) and (slope < -trend_thr)
        block = int(params["block_counter_trend"]) == 1
        trend_ok_long = (not strong_bear) if block else True
        trend_ok_short = (not strong_bull) if block else True

        # Kern-Trigger: statistisches Extrem am Band + RSI(2)
        bb_long = (last["low"] <= lower) or (prev["close"] <= lower)
        bb_short = (last["high"] >= upper) or (prev["close"] >= upper)
        rsi_long = rsi < params["rsi_long_threshold"]
        rsi_short = rsi > params["rsi_short_threshold"]

        # Reversal-Bestaetigung: Preis dreht zurueck (kein fallendes Messer)
        rev_long = last["close"] > prev["close"] or last["close"] > lower
        rev_short = last["close"] < prev["close"] or last["close"] < upper
        if int(params["require_reversal"]) != 1:
            rev_long = rev_short = True

        # Confluences (VWAP fair value, Volumen, Sweep)
        rel_ok = rel_vol >= float(params["rel_vol_min"])
        c_vwap_l = price <= vwap
        c_vwap_s = price >= vwap
        c_sweep_l = sweep == "bullish"
        c_sweep_s = sweep == "bearish"
        long_conf = sum([c_vwap_l, c_sweep_l])
        short_conf = sum([c_vwap_s, c_sweep_s])
        # FIX: Min. Rel. Volumen ist jetzt ein HARTER Filter (vorher nur Confluence)
        min_conf = min(int(params["min_confluence"]), 2)

        signal_long = bb_long and rsi_long and rev_long and trend_ok_long and rel_ok and long_conf >= min_conf
        signal_short = bb_short and rsi_short and rev_short and trend_ok_short and rel_ok and short_conf >= min_conf
        signal_type = "LONG" if signal_long else ("SHORT" if signal_short else None)
        bias = "LONG" if (bb_long and rsi_long) else ("SHORT" if (bb_short and rsi_short) else None)

        rules = [
            {"id": "bb_extreme", "label": "Bollinger Extrem",
             "description": f"Preis am unteren Band (Long) / oberen Band (Short), {bb_mult} StdDev",
             "long": bool(bb_long), "short": bool(bb_short)},
            {"id": "rsi2_extreme", "label": "RSI(2) Extrem",
             "description": f"RSI < {params['rsi_long_threshold']} (Long) / > {params['rsi_short_threshold']} (Short)",
             "long": bool(rsi_long), "short": bool(rsi_short)},
            {"id": "reversal", "label": "Reversal-Kerze",
             "description": "Close dreht zurueck Richtung Mittelwert (kein fallendes Messer)",
             "long": bool(rev_long), "short": bool(rev_short)},
            {"id": "trend_filter", "label": "Trend-Filter",
             "description": "Keine Reversion gegen starke Impulse",
             "long": bool(trend_ok_long), "short": bool(trend_ok_short)},
            {"id": "fair_value", "label": "VWAP / Volumen",
             "description": "Long unter Fair Value, Short darueber + Teilnahme (Volumen = Pflicht)",
             "long": bool(rel_ok and long_conf >= min_conf), "short": bool(rel_ok and short_conf >= min_conf)},
        ]
        long_count = sum(1 for r in rules if r["long"])
        short_count = sum(1 for r in rules if r["short"])

        levels = None
        atr_buf = atr * float(params["atr_sl_mult"])
        tp_rr = float(params["tp_rr"])
        if signal_type == "LONG":
            struct = ti.get_recent_low(candles, sl_lookback)
            struct = min(struct, last["low"]) if struct is not None else last["low"]
            sl = struct - atr_buf
            risk = price - sl
            if risk <= 0:
                risk = price * 0.003
                sl = price - risk
            tp1 = max(basis, price + risk * 0.8)  # TP1 = Rueckkehr zum Mittelwert (BB-Basis)
            levels = self._lv(price, sl, tp1, price + risk * tp_rr)
        elif signal_type == "SHORT":
            struct = ti.get_recent_high(candles, sl_lookback)
            struct = max(struct, last["high"]) if struct is not None else last["high"]
            sl = struct + atr_buf
            risk = sl - price
            if risk <= 0:
                risk = price * 0.003
                sl = price + risk
            tp1 = min(basis, price - risk * 0.8)
            levels = self._lv(price, sl, tp1, price - risk * tp_rr)

        return {
            "indicators": {"rsi": round(rsi, 2), "ema_fast": round(ema_f, 6),
                           "ema_slow": round(ema_s, 6), "price": round(price, 6),
                           "bb_upper": round(upper, 6), "bb_basis": round(basis, 6),
                           "bb_lower": round(lower, 6), "vwap": round(vwap, 6),
                           "rel_vol": round(rel_vol, 2), "atr": round(atr, 6),
                           "sweep": sweep or "none", "slope_pct": round(slope * 100, 3)},
            "rules": rules, "bias": bias,
            "long_count": long_count, "short_count": short_count, "rules_total": len(rules),
            "signal_type": signal_type, "is_pre_signal": False, "levels": levels,
        }

    @staticmethod
    def _last(arr):
        return arr[-1] if arr else None

    def _lv(self, entry, sl, tp1, tpf):
        return {"entry": round(entry, 6), "stop_loss": round(sl, 6),
                "take_profit_1": round(tp1, 6), "take_profit_full": round(tpf, 6),
                "crv": round(self.indicators.calculate_crv(entry, sl, tpf), 2)}

    # ----------------------- Vectorized Fast-Path -----------------------
    @staticmethod
    def vectorized_signals(fs, params: Dict) -> Optional[Dict]:
        """BB-Extrem + RSI(2) + Reversal + Trend-Sperre + VWAP/Volumen -- vektorisiert."""
        import numpy as np
        from services import fast_sim
        bb_p = int(params["bb_period"])
        rsi_p = int(params["rsi_period"])
        ema_f_p = int(params["ema_fast_period"])
        ema_s_p = int(params["ema_slow_period"])
        atr_p = int(params["atr_period"])
        sl_lb = int(params["sl_lookback"])
        need = max(bb_p, ema_s_p, atr_p, sl_lb, 14) + 10

        d = {"bb_period": bb_p, "bb_std": float(params["bb_std"]),
             "rsi_period": rsi_p, "ema_fast_period": ema_f_p,
             "ema_slow_period": ema_s_p, "volume_sma_period": 20}
        close, low, high = fs.close, fs.low, fs.high
        basis = fs.get("bb_middle", d)
        upper = fs.get("bb_upper", d)
        lower = fs.get("bb_lower", d)
        rsi = fs.get("rsi", d)
        ef = fs.get("ema_fast", d)
        es = fs.get("ema_slow", d)
        vwap = fs.get("vwap", d)
        rel_vol = fs.get("rel_volume", d)
        std = (upper - basis) / float(params["bb_std"])
        close_prev = np.concatenate([[np.nan], close[:-1]])
        ef_prev = np.concatenate([np.full(5, np.nan), ef[:-5]])

        with np.errstate(invalid="ignore", divide="ignore"):
            slope = np.where((~np.isnan(ef_prev)) & (ef_prev != 0),
                             (ef - ef_prev) / ef_prev, 0.0)
            thr = float(params["trend_block_pct"]) / 100.0
            strong_bull = (close > es) & (ef > es) & (slope > thr)
            strong_bear = (close < es) & (ef < es) & (slope < -thr)
            block = int(params["block_counter_trend"]) == 1
            ones = np.ones(fs.n, dtype=bool)
            trend_ok_long = ~strong_bear if block else ones
            trend_ok_short = ~strong_bull if block else ones

            bb_long = (low <= lower) | (close_prev <= lower)
            bb_short = (high >= upper) | (close_prev >= upper)
            rsi_long = rsi < float(params["rsi_long_threshold"])
            rsi_short = rsi > float(params["rsi_short_threshold"])
            rev_long = (close > close_prev) | (close > lower)
            rev_short = (close < close_prev) | (close < upper)
            if int(params["require_reversal"]) != 1:
                rev_long = rev_short = ones
            rel_ok = rel_vol >= float(params["rel_vol_min"])

            sweep_bull, sweep_bear = fast_sim.sweep_arrays(fs)
            c_vwap_l = close <= vwap
            c_vwap_s = close >= vwap
            long_conf = c_vwap_l.astype(int) + sweep_bull.astype(int)
            short_conf = c_vwap_s.astype(int) + sweep_bear.astype(int)
            min_conf = min(int(params["min_confluence"]), 2)

            long_ok = (bb_long & rsi_long & rev_long & trend_ok_long
                       & rel_ok & (long_conf >= min_conf))
            short_ok = (bb_short & rsi_short & rev_short & trend_ok_short
                        & rel_ok & (short_conf >= min_conf))

        valid = (~np.isnan(rsi) & ~np.isnan(ef) & ~np.isnan(es) & (std > 0)
                 & ~np.isnan(close_prev) & ~np.isnan(vwap) & ~np.isnan(rel_vol))
        long_ok &= valid
        short_ok &= valid
        return {"long": long_ok, "short": short_ok,
                "warmup": need, "rules_total": 5, "rsi": rsi}
