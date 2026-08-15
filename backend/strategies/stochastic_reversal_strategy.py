"""Stochastic Reversal: %K/%D-Cross im Extrem + Kerzen- und Volumen-Bestätigung."""
from typing import Dict, List, Optional
import numpy as np
from strategies.base_strategy import BaseStrategy


class StochasticReversalStrategy(BaseStrategy):
    STRATEGY_ID = "stoch_reversal"
    STRATEGY_NAME = "Stochastic Reversal"
    STRATEGY_DESCRIPTION = ("Stochastik %K kreuzt %D im überverkauften/überkauften Bereich, "
                            "Heikin-Ashi Bestätigung + Trend-Sperre gegen starke Impulse. "
                            "Empfohlen auf 5m-15m.")
    STRATEGY_TIMEFRAME = "5m"

    DEFAULT_PARAMS = {
        "stoch_k_period": {"value": 14, "min": 5, "max": 30, "step": 1,
                           "label": "Stochastik %K", "description": "%K Periode"},
        "stoch_d_period": {"value": 3, "min": 2, "max": 10, "step": 1,
                           "label": "Stochastik %D", "description": "%D Glättung"},
        "oversold": {"value": 22, "min": 5, "max": 40, "step": 1,
                     "label": "Überverkauft", "description": "%K unter diesem Wert = Long-Zone"},
        "overbought": {"value": 78, "min": 60, "max": 95, "step": 1,
                       "label": "Überkauft", "description": "%K über diesem Wert = Short-Zone"},
        "ema_trend_period": {"value": 50, "min": 20, "max": 200, "step": 5,
                             "label": "Trend EMA", "description": "EMA für Trend-Sperre"},
        "trend_block_pct": {"value": 0.15, "min": 0.02, "max": 1.0, "step": 0.01,
                            "label": "Trend-Sperre %", "description": "EMA-Steigung ab der Gegen-Trades blockiert werden"},
        "rel_vol_min": {"value": 0.9, "min": 0.3, "max": 3.0, "step": 0.1,
                        "label": "Min. Rel. Volumen", "description": "Teilnahme-Filter"},
        "sl_lookback": {"value": 10, "min": 3, "max": 40, "step": 1,
                        "label": "Struktur Lookback", "description": "Kerzen für Swing-SL"},
        "atr_sl_mult": {"value": 1.0, "min": 0.2, "max": 3.0, "step": 0.1,
                        "label": "ATR SL Puffer", "description": "ATR-Puffer hinter der Struktur"},
        "tp1_rr": {"value": 1.0, "min": 0.5, "max": 5.0, "step": 0.1,
                   "label": "TP1 (R)", "description": "Erstes Ziel in R"},
        "tp_rr": {"value": 2.0, "min": 1.0, "max": 8.0, "step": 0.1,
                  "label": "TP voll (R)", "description": "Endziel in R"},
    }

    def analyze(self, candles: List[Dict], symbol: str, params: Dict) -> Optional[Dict]:
        need = max(int(params["stoch_k_period"]) + int(params["stoch_d_period"]),
                   int(params["ema_trend_period"])) + 10
        if len(candles) < need:
            return None
        ti = self.indicators
        closes = [c["close"] for c in candles]
        price = closes[-1]
        st_k, st_d = ti.calculate_stochastic(candles, int(params["stoch_k_period"]),
                                             int(params["stoch_d_period"]))
        ema_arr = ti.calculate_ema(closes, int(params["ema_trend_period"]))
        atr_arr = ti.calculate_atr(candles, 14)
        ha = ti.calculate_heikin_ashi(candles)
        if not st_k or st_k[-1] is None or st_d[-1] is None or st_k[-2] is None or ema_arr[-1] is None:
            return None
        rel_vol = ti.relative_volume(candles, 20) or 0.0

        k, d = st_k[-1], st_d[-1]
        cross_up = st_k[-2] <= (st_d[-2] or 0) and k > d
        cross_dn = st_k[-2] >= (st_d[-2] or 100) and k < d
        os_zone = min(st_k[-2], k) <= float(params["oversold"])
        ob_zone = max(st_k[-2], k) >= float(params["overbought"])

        prev_e = ema_arr[-6] if len(ema_arr) >= 6 and ema_arr[-6] else None
        slope = (ema_arr[-1] - prev_e) / prev_e if prev_e else 0.0
        thr = float(params["trend_block_pct"]) / 100.0
        strong_bull = price > ema_arr[-1] and slope > thr
        strong_bear = price < ema_arr[-1] and slope < -thr
        trend_ok_l = not strong_bear
        trend_ok_s = not strong_bull

        ha_green = ha[-1]["is_green"]
        vol_ok = rel_vol >= float(params["rel_vol_min"])

        rules = [
            {"id": "stoch_cross", "label": "Stoch Cross", "description": "%K kreuzt %D",
             "long": bool(cross_up), "short": bool(cross_dn)},
            {"id": "zone", "label": "Extrem-Zone", "description": "Cross im überverkauften/überkauften Bereich",
             "long": bool(os_zone), "short": bool(ob_zone)},
            {"id": "trend_filter", "label": "Trend-Sperre", "description": "Kein Gegen-Trade in starke Impulse",
             "long": bool(trend_ok_l), "short": bool(trend_ok_s)},
            {"id": "candle", "label": "HA Bestätigung", "description": "Heikin-Ashi Farbe in Trade-Richtung",
             "long": bool(ha_green), "short": bool(not ha_green)},
            {"id": "volume", "label": "Volumen", "description": "Rel. Volumen über Minimum",
             "long": bool(vol_ok), "short": bool(vol_ok)},
        ]
        signal_long = cross_up and os_zone and trend_ok_l and ha_green and vol_ok
        signal_short = cross_dn and ob_zone and trend_ok_s and (not ha_green) and vol_ok
        signal_type = "LONG" if signal_long else ("SHORT" if signal_short else None)
        long_count = sum(1 for x in rules if x["long"])
        short_count = sum(1 for x in rules if x["short"])
        bias = "LONG" if k < float(params["oversold"]) else ("SHORT" if k > float(params["overbought"]) else None)

        levels = None
        if signal_type:
            last = candles[-1]
            atr_buf = (atr_arr[-1] or 0.0) * float(params["atr_sl_mult"])
            tp1_rr, tp_rr = float(params["tp1_rr"]), float(params["tp_rr"])
            if signal_type == "LONG":
                struct = ti.get_recent_low(candles, int(params["sl_lookback"]))
                struct = min(struct, last["low"]) if struct is not None else last["low"]
                sl = struct - atr_buf
                risk = price - sl
                if risk <= 0:
                    risk = price * 0.003
                    sl = price - risk
                tp1, tpf = price + risk * tp1_rr, price + risk * tp_rr
            else:
                struct = ti.get_recent_high(candles, int(params["sl_lookback"]))
                struct = max(struct, last["high"]) if struct is not None else last["high"]
                sl = struct + atr_buf
                risk = sl - price
                if risk <= 0:
                    risk = price * 0.003
                    sl = price + risk
                tp1, tpf = price - risk * tp1_rr, price - risk * tp_rr
            levels = {"entry": round(price, 6), "stop_loss": round(sl, 6),
                      "take_profit_1": round(tp1, 6), "take_profit_full": round(tpf, 6),
                      "crv": round(ti.calculate_crv(price, sl, tpf), 2)}

        return {
            "indicators": {"rsi": round(k, 2), "ema_fast": round(k, 2), "ema_slow": round(d, 2),
                           "price": round(price, 6), "stoch_k": round(k, 2), "stoch_d": round(d, 2),
                           "rel_vol": round(rel_vol, 2)},
            "rules": rules, "bias": bias,
            "long_count": long_count, "short_count": short_count, "rules_total": len(rules),
            "signal_type": signal_type, "is_pre_signal": False, "levels": levels,
        }

    # ----------------------- Vectorized Fast-Path -----------------------
    @staticmethod
    def vectorized_signals(fs, params: Dict) -> Optional[Dict]:
        """Stoch-Cross im Extrem + Trend-Sperre + HA + Volumen -- vektorisiert."""
        kp = int(params["stoch_k_period"])
        dp = int(params["stoch_d_period"])
        ema_p = int(params["ema_trend_period"])
        need = max(kp + dp, ema_p) + 10

        d = {"stoch_k_period": kp, "stoch_d_period": dp, "volume_sma_period": 20}
        close = fs.close
        n = fs.n
        k = fs.get("stoch_k", d)
        dd = fs.get("stoch_d", d)
        ema = fs.get("ema_slow", {"ema_slow_period": ema_p})
        rel_vol = fs.get("rel_volume", d)
        ha = fs.get("ha_color", d)

        k_prev = np.concatenate([[np.nan], k[:-1]])
        d_prev = np.concatenate([[np.nan], dd[:-1]])
        ema_prev = np.concatenate([np.full(5, np.nan), ema[:-5]])

        with np.errstate(invalid="ignore", divide="ignore"):
            cross_up = (k_prev <= d_prev) & (k > dd)
            cross_dn = (k_prev >= d_prev) & (k < dd)
            prev_ok = ~np.isnan(k_prev) & ~np.isnan(d_prev)
            cross_up &= prev_ok
            cross_dn &= prev_ok
            os_zone = np.minimum(k_prev, k) <= float(params["oversold"])
            ob_zone = np.maximum(k_prev, k) >= float(params["overbought"])
            slope = np.where((~np.isnan(ema_prev)) & (ema_prev != 0),
                             (ema - ema_prev) / ema_prev, 0.0)
            thr = float(params["trend_block_pct"]) / 100.0
            strong_bull = (close > ema) & (slope > thr)
            strong_bear = (close < ema) & (slope < -thr)
            ha_green = ha >= 0.5
            vol_ok = rel_vol >= float(params["rel_vol_min"])

            long_ok = cross_up & os_zone & ~strong_bear & ha_green & vol_ok
            short_ok = cross_dn & ob_zone & ~strong_bull & ~ha_green & vol_ok

        valid = ~np.isnan(k) & ~np.isnan(dd) & prev_ok & ~np.isnan(ema)
        long_ok &= valid
        short_ok &= valid
        return {"long": long_ok, "short": short_ok,
                "warmup": need, "rules_total": 5, "rsi": None}
