"""
Smart Money RSI Reversal strategy.

Fixes the classic "RSI oversold/overbought" loss trap: it no longer blindly
shorts into a strong bull run (or longs a falling knife). Instead an RSI extreme
is only ONE trigger and every entry is filtered through:

  * Trend regime (EMA fast/slow + slope)  -> never fight a strong impulse
  * Liquidity sweep (stop-hunt reversal)   -> smart-money entry timing
  * Discount / premium range position      -> buy low / sell high
  * Volume + buying-power confirmation      -> real participation, not noise

Stops are placed at MARKET STRUCTURE (swing + ATR buffer) instead of a naive
fixed %, and targets use a risk-multiple (R) so winners pay for losers.
`min_confluence` keeps trade frequency high (default: 1 extra confirmation).
"""
from typing import Dict, List, Optional
import numpy as np
import pandas as pd
from strategies.base_strategy import BaseStrategy


class RSIOnlyStrategy(BaseStrategy):
    STRATEGY_ID = "rsi_only"  # kept stable for backward compatibility with saved settings
    STRATEGY_NAME = "Smart Money RSI Reversal"
    STRATEGY_DESCRIPTION = ("RSI-Reversal mit Trend-Filter, Liquidity-Sweep, Volumen & "
                            "Kaufkraft. Shortet NICHT in starke Bull-Impulse. "
                            "Struktur-basierte Smart-Money Stops.")
    STRATEGY_TIMEFRAME = "1m"

    DEFAULT_PARAMS = {
        "rsi_period": {"value": 14, "min": 5, "max": 30, "step": 1,
                       "label": "RSI Period", "description": "RSI Berechnungs-Periode"},
        "rsi_long_threshold": {"value": 32, "min": 10, "max": 45, "step": 1,
                               "label": "RSI LONG (Oversold)", "description": "RSI unter diesem Wert = LONG-Trigger"},
        "rsi_short_threshold": {"value": 68, "min": 55, "max": 90, "step": 1,
                                "label": "RSI SHORT (Overbought)", "description": "RSI über diesem Wert = SHORT-Trigger"},
        "ema_fast_period": {"value": 21, "min": 5, "max": 100, "step": 1,
                            "label": "EMA schnell", "description": "Trend-Filter schnelle EMA"},
        "ema_slow_period": {"value": 50, "min": 20, "max": 200, "step": 1,
                            "label": "EMA langsam", "description": "Trend-Filter langsame EMA"},
        "trend_block_pct": {"value": 0.12, "min": 0.02, "max": 1.0, "step": 0.01,
                            "label": "Trend-Sperre %", "description": "EMA-Steigung ab der ein Gegen-Trade blockiert wird"},
        "rel_vol_min": {"value": 1.2, "min": 0.5, "max": 5.0, "step": 0.1,
                        "label": "Min. Rel. Volumen", "description": "Volumen ggü. Durchschnitt (Kaufkraft/Teilnahme)"},
        "discount_zone": {"value": 0.40, "min": 0.1, "max": 0.5, "step": 0.05,
                          "label": "Discount-Zone (Long)", "description": "Range-Position unter der LONG bevorzugt wird"},
        "premium_zone": {"value": 0.60, "min": 0.5, "max": 0.9, "step": 0.05,
                         "label": "Premium-Zone (Short)", "description": "Range-Position über der SHORT bevorzugt wird"},
        "min_confluence": {"value": 1, "min": 0, "max": 4, "step": 1,
                           "label": "Min. Bestätigungen", "description": "Zusätzliche Confluences nötig (klein = mehr Trades)"},
        "block_counter_trend": {"value": 1, "min": 0, "max": 1, "step": 1,
                                "label": "Gegen-Trend sperren", "description": "1 = nicht in starke Impulse shorten/longen"},
        "atr_period": {"value": 14, "min": 5, "max": 30, "step": 1,
                       "label": "ATR Period", "description": "Volatilität für Smart-Money Stop"},
        "atr_sl_mult": {"value": 1.0, "min": 0.2, "max": 3.0, "step": 0.1,
                        "label": "ATR SL Puffer", "description": "ATR-Puffer hinter der Struktur (Anti Stop-Hunt)"},
        "sl_lookback": {"value": 12, "min": 3, "max": 40, "step": 1,
                        "label": "Struktur Lookback", "description": "Kerzen für Swing-Tief/-Hoch (Stop-Struktur)"},
        "tp1_rr": {"value": 1.0, "min": 0.5, "max": 5.0, "step": 0.1,
                   "label": "TP1 (R-Vielfaches)", "description": "Erstes Ziel als Vielfaches des Risikos"},
        "tp_rr": {"value": 2.0, "min": 1.0, "max": 8.0, "step": 0.1,
                  "label": "TP voll (R-Vielfaches)", "description": "Endziel als Vielfaches des Risikos"},
    }

    def analyze(self, candles: List[Dict], symbol: str, params: Dict) -> Optional[Dict]:
        rsi_period = int(params["rsi_period"])
        ema_slow_p = int(params["ema_slow_period"])
        atr_period = int(params["atr_period"])
        sl_lookback = int(params["sl_lookback"])
        need = max(rsi_period, ema_slow_p, atr_period, sl_lookback) + 10
        if len(candles) < need:
            return None

        ti = self.indicators
        closes = [c["close"] for c in candles]
        price = closes[-1]
        last = candles[-1]

        rsi = self._last(ti.calculate_rsi(closes, rsi_period))
        ema_f_arr = ti.calculate_ema(closes, int(params["ema_fast_period"]))
        ema_s_arr = ti.calculate_ema(closes, ema_slow_p)
        ema_f, ema_s = self._last(ema_f_arr), self._last(ema_s_arr)
        if rsi is None or ema_f is None or ema_s is None:
            return None

        # EMA slope over ~5 candles = momentum strength of the current impulse
        prev_f = None
        if len(ema_f_arr) >= 6 and ema_f_arr[-6]:
            prev_f = ema_f_arr[-6]
        slope = (ema_f - prev_f) / prev_f if prev_f else 0.0

        vwap = self._last(ti.calculate_vwap(candles)) or price
        rel_vol = ti.relative_volume(candles, 20) or 0.0
        sweep = ti.liquidity_sweep(candles)
        rpos = ti.range_position(candles, 20)
        atr = self._last(ti.calculate_atr(candles, atr_period)) or 0.0

        rng = last["high"] - last["low"]
        close_pos = (last["close"] - last["low"]) / rng if rng > 0 else 0.5  # 1=buyers, 0=sellers

        trend_thr = float(params["trend_block_pct"]) / 100.0
        strong_bull = (price > ema_s) and (ema_f > ema_s) and (slope > trend_thr)
        strong_bear = (price < ema_s) and (ema_f < ema_s) and (slope < -trend_thr)
        block = int(params["block_counter_trend"]) == 1

        rsi_long = rsi < params["rsi_long_threshold"]
        rsi_short = rsi > params["rsi_short_threshold"]

        # KEY FIX: never short a strong bull impulse / never long a strong bear impulse
        trend_ok_long = (not strong_bear) if block else True
        trend_ok_short = (not strong_bull) if block else True

        rel_ok = rel_vol >= float(params["rel_vol_min"])
        discount = rpos <= float(params["discount_zone"])
        premium = rpos >= float(params["premium_zone"])

        # confluence flags (current live state -> also used for UI circles)
        c_sweep_l = sweep == "bullish"
        c_sweep_s = sweep == "bearish"
        c_vol_l = (close_pos >= 0.55 or price <= vwap)   # buyers stepping in / discount vs fair value
        c_vol_s = (close_pos <= 0.45 or price >= vwap)   # sellers in control / premium vs fair value

        long_conf = sum([c_sweep_l, discount, c_vol_l])
        short_conf = sum([c_sweep_s, premium, c_vol_s])
        min_conf = int(params["min_confluence"])

        # FIX: Min. Rel. Volumen ist jetzt ein HARTER Filter (vorher nur weiche
        # Confluence -> Änderungen am Wert hatten kaum/keinen Effekt auf Trades)
        signal_long = rsi_long and trend_ok_long and rel_ok and long_conf >= min_conf
        signal_short = rsi_short and trend_ok_short and rel_ok and short_conf >= min_conf
        signal_type = "LONG" if signal_long else ("SHORT" if signal_short else None)
        bias = "LONG" if rsi_long else ("SHORT" if rsi_short else None)

        rules = [
            {"id": "rsi_extreme", "label": "RSI Extrem",
             "description": f"RSI < {params['rsi_long_threshold']} (Long) / > {params['rsi_short_threshold']} (Short)",
             "long": bool(rsi_long), "short": bool(rsi_short)},
            {"id": "trend_filter", "label": "Trend-Filter",
             "description": "Kein Gegen-Trade in starke Impulse (Anti-Bull-Short)",
             "long": bool(trend_ok_long and (rsi_long or True)), "short": bool(trend_ok_short)},
            {"id": "liquidity_sweep", "label": "Liquidity Sweep",
             "description": "Stop-Hunt Reversal (Smart Money)",
             "long": bool(c_sweep_l), "short": bool(c_sweep_s)},
            {"id": "volume_power", "label": "Volumen & Kaufkraft",
             "description": "Überdurchschnittl. Volumen (Pflicht-Filter) + Käufer/Verkäufer-Druck",
             "long": bool(rel_ok and c_vol_l), "short": bool(rel_ok and c_vol_s)},
            {"id": "zone", "label": "Discount / Premium",
             "description": "Long im Discount, Short im Premium",
             "long": bool(discount), "short": bool(premium)},
        ]
        long_count = sum(1 for r in rules if r["long"])
        short_count = sum(1 for r in rules if r["short"])

        levels = None
        atr_buf = atr * float(params["atr_sl_mult"])
        tp1_rr, tp_rr = float(params["tp1_rr"]), float(params["tp_rr"])
        if signal_type == "LONG":
            struct = ti.get_recent_low(candles, sl_lookback)
            struct = min(struct, last["low"]) if struct is not None else last["low"]
            sl = struct - atr_buf
            risk = price - sl
            if risk <= 0:
                risk = price * 0.003
                sl = price - risk
            levels = self._lv(price, sl, price + risk * tp1_rr, price + risk * tp_rr)
        elif signal_type == "SHORT":
            struct = ti.get_recent_high(candles, sl_lookback)
            struct = max(struct, last["high"]) if struct is not None else last["high"]
            sl = struct + atr_buf
            risk = sl - price
            if risk <= 0:
                risk = price * 0.003
                sl = price + risk
            levels = self._lv(price, sl, price - risk * tp1_rr, price - risk * tp_rr)

        return {
            "indicators": {"rsi": round(rsi, 2), "ema_fast": round(ema_f, 6),
                           "ema_slow": round(ema_s, 6), "price": round(price, 6),
                           "rel_vol": round(rel_vol, 2), "range_pos": round(rpos, 2),
                           "vwap": round(vwap, 6), "atr": round(atr, 6),
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
        """RSI-Reversal komplett vektorisiert inkl. Liquidity-Sweep (O(N))."""
        rsi_p = int(params["rsi_period"])
        ema_slow_p = int(params["ema_slow_period"])
        atr_p = int(params["atr_period"])
        sl_lookback = int(params["sl_lookback"])
        need = max(rsi_p, ema_slow_p, atr_p, sl_lookback) + 10

        d = {"rsi_period": rsi_p, "ema_fast_period": int(params["ema_fast_period"]),
             "ema_slow_period": ema_slow_p, "atr_period": atr_p,
             "volume_sma_period": 20}
        close = fs.close
        high = fs.high
        low = fs.low
        n = fs.n
        rsi = fs.get("rsi", d)
        ema_f = fs.get("ema_fast", d)
        ema_s = fs.get("ema_slow", d)
        rel_vol = fs.get("rel_volume", d)
        vwap = fs.get("vwap", d)

        # EMA-Slope über 5 Kerzen (Momentum)
        prev_f = np.concatenate([np.full(5, np.nan), ema_f[:-5]])
        with np.errstate(invalid="ignore", divide="ignore"):
            slope = np.where(prev_f != 0, (ema_f - prev_f) / prev_f, 0.0)

        # Range-Position (20er Lookback)
        s_high = pd.Series(high)
        s_low = pd.Series(low)
        rh = s_high.rolling(20, min_periods=1).max().to_numpy()
        rl = s_low.rolling(20, min_periods=1).min().to_numpy()
        rng_span = rh - rl
        with np.errstate(invalid="ignore", divide="ignore"):
            rpos = np.where(rng_span > 0, (close - rl) / np.where(rng_span > 0, rng_span, 1), 0.5)

        # Close-Position innerhalb der aktuellen Kerze
        cand_rng = high - low
        with np.errstate(invalid="ignore", divide="ignore"):
            close_pos = np.where(cand_rng > 0, (close - low) / np.where(cand_rng > 0, cand_rng, 1), 0.5)

        # Fraktale Swings (left=right=2) + Sweep O(N)
        window_max = s_high.rolling(5, center=True, min_periods=5).max().to_numpy()
        window_min = s_low.rolling(5, center=True, min_periods=5).min().to_numpy()
        # bool: True nur wenn window_max nicht NaN und high == max (Vergleich mit NaN -> False)
        fh = (high >= window_max)
        fl = (low <= window_min)

        sweep_bull = np.zeros(n, dtype=bool)
        sweep_bear = np.zeros(n, dtype=bool)
        fh_buf: List[float] = []
        fl_buf: List[float] = []
        for i in range(n):
            # Sweep an i mit Fraktalen deren Zentrum j <= i-3 liegt (right=2, prior=[:-1])
            if fh_buf:
                rh_ref = max(fh_buf[-3:])
                if high[i] > rh_ref and close[i] < rh_ref:
                    sweep_bear[i] = True
            if fl_buf:
                rl_ref = min(fl_buf[-3:])
                if low[i] < rl_ref and close[i] > rl_ref:
                    sweep_bull[i] = True
            # Fraktal an Index j = i-2 wird jetzt bekannt (2 Kerzen später)
            j = i - 2
            if j >= 2:
                if fh[j]:
                    fh_buf.append(high[j])
                if fl[j]:
                    fl_buf.append(low[j])

        with np.errstate(invalid="ignore"):
            trend_thr = float(params["trend_block_pct"]) / 100.0
            strong_bull = (close > ema_s) & (ema_f > ema_s) & (slope > trend_thr)
            strong_bear = (close < ema_s) & (ema_f < ema_s) & (slope < -trend_thr)
            block = int(params["block_counter_trend"]) == 1

            rsi_long = rsi < float(params["rsi_long_threshold"])
            rsi_short = rsi > float(params["rsi_short_threshold"])
            trend_ok_long = ~strong_bear if block else np.ones(n, dtype=bool)
            trend_ok_short = ~strong_bull if block else np.ones(n, dtype=bool)
            rel_ok = rel_vol >= float(params["rel_vol_min"])
            discount = rpos <= float(params["discount_zone"])
            premium = rpos >= float(params["premium_zone"])

            c_vol_l = (close_pos >= 0.55) | (close <= vwap)
            c_vol_s = (close_pos <= 0.45) | (close >= vwap)
            long_conf = sweep_bull.astype(int) + discount.astype(int) + c_vol_l.astype(int)
            short_conf = sweep_bear.astype(int) + premium.astype(int) + c_vol_s.astype(int)
            min_conf = int(params["min_confluence"])

            long_ok = rsi_long & trend_ok_long & rel_ok & (long_conf >= min_conf)
            short_ok = rsi_short & trend_ok_short & rel_ok & (short_conf >= min_conf)

        valid = ~np.isnan(rsi) & ~np.isnan(ema_f) & ~np.isnan(ema_s)
        long_ok &= valid
        short_ok &= valid
        return {"long": long_ok, "short": short_ok,
                "warmup": need, "rules_total": 5, "rsi": rsi}
