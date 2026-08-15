"""VWAP Mean Reversion: übertriebene Abweichung vom VWAP + Reversal-Kerze."""
from typing import Dict, List, Optional
import numpy as np
from strategies.base_strategy import BaseStrategy


class VWAPReversionStrategy(BaseStrategy):
    STRATEGY_ID = "vwap_reversion"
    STRATEGY_NAME = "VWAP Mean Reversion"
    STRATEGY_DESCRIPTION = ("Preis weicht stark vom VWAP ab (x·ATR), RSI-Extrem + "
                            "Heikin-Ashi Reversal-Kerze zurück zum fairen Wert. "
                            "Empfohlen auf 5m-15m.")
    STRATEGY_TIMEFRAME = "1m"

    DEFAULT_PARAMS = {
        "dev_atr_mult": {"value": 1.5, "min": 0.5, "max": 5.0, "step": 0.1,
                         "label": "Abweichung (x ATR)", "description": "Mindest-Distanz Preis zu VWAP in ATR"},
        "rsi_period": {"value": 14, "min": 5, "max": 30, "step": 1,
                       "label": "RSI Periode", "description": "RSI Berechnungs-Periode"},
        "rsi_long_max": {"value": 38, "min": 15, "max": 50, "step": 1,
                         "label": "RSI Long Max", "description": "RSI unter diesem Wert = überverkauft"},
        "rsi_short_min": {"value": 62, "min": 50, "max": 85, "step": 1,
                          "label": "RSI Short Min", "description": "RSI über diesem Wert = überkauft"},
        "rel_vol_min": {"value": 1.0, "min": 0.3, "max": 3.0, "step": 0.1,
                        "label": "Min. Rel. Volumen", "description": "Teilnahme-Filter"},
        "atr_period": {"value": 14, "min": 5, "max": 30, "step": 1,
                       "label": "ATR Periode", "description": "Volatilitäts-Basis"},
        "sl_lookback": {"value": 10, "min": 3, "max": 40, "step": 1,
                        "label": "Struktur Lookback", "description": "Kerzen für Swing-SL"},
        "atr_sl_mult": {"value": 1.2, "min": 0.2, "max": 3.0, "step": 0.1,
                        "label": "ATR SL Puffer", "description": "ATR-Puffer hinter der Struktur"},
        "tp1_rr": {"value": 1.0, "min": 0.5, "max": 5.0, "step": 0.1,
                   "label": "TP1 (R)", "description": "Erstes Ziel in R"},
        "tp_rr": {"value": 2.0, "min": 1.0, "max": 8.0, "step": 0.1,
                  "label": "TP voll (R)", "description": "Endziel in R"},
    }

    def analyze(self, candles: List[Dict], symbol: str, params: Dict) -> Optional[Dict]:
        need = max(int(params["rsi_period"]), int(params["atr_period"]), 30) + 10
        if len(candles) < need:
            return None
        ti = self.indicators
        closes = [c["close"] for c in candles]
        price = closes[-1]
        rsi_arr = ti.calculate_rsi(closes, int(params["rsi_period"]))
        atr_arr = ti.calculate_atr(candles, int(params["atr_period"]))
        vwap_arr = ti.calculate_vwap(candles)
        ha = ti.calculate_heikin_ashi(candles)
        rsi = rsi_arr[-1] if rsi_arr else None
        atr = atr_arr[-1] if atr_arr else None
        vwap = vwap_arr[-1] if vwap_arr else None
        if rsi is None or not atr or not vwap:
            return None
        rel_vol = ti.relative_volume(candles, 20) or 0.0

        dev = price - vwap
        min_dev = atr * float(params["dev_atr_mult"])
        below = dev <= -min_dev
        above = dev >= min_dev
        rsi_l = rsi <= float(params["rsi_long_max"])
        rsi_s = rsi >= float(params["rsi_short_min"])
        flip_green = ha[-1]["is_green"] and not ha[-2]["is_green"]
        flip_red = (not ha[-1]["is_green"]) and ha[-2]["is_green"]
        vol_ok = rel_vol >= float(params["rel_vol_min"])

        rules = [
            {"id": "deviation", "label": "VWAP-Abweichung", "description": "Preis weit unter/über VWAP (x ATR)",
             "long": bool(below), "short": bool(above)},
            {"id": "rsi_extreme", "label": "RSI Extrem", "description": "Überverkauft (Long) / überkauft (Short)",
             "long": bool(rsi_l), "short": bool(rsi_s)},
            {"id": "reversal", "label": "HA Reversal", "description": "Heikin-Ashi Farbwechsel als Umkehr-Trigger",
             "long": bool(flip_green), "short": bool(flip_red)},
            {"id": "volume", "label": "Volumen", "description": "Rel. Volumen über Minimum",
             "long": bool(vol_ok), "short": bool(vol_ok)},
        ]
        signal_long = below and rsi_l and flip_green and vol_ok
        signal_short = above and rsi_s and flip_red and vol_ok
        signal_type = "LONG" if signal_long else ("SHORT" if signal_short else None)
        long_count = sum(1 for x in rules if x["long"])
        short_count = sum(1 for x in rules if x["short"])
        bias = "LONG" if below else ("SHORT" if above else None)

        levels = None
        if signal_type:
            last = candles[-1]
            atr_buf = atr * float(params["atr_sl_mult"])
            tp1_rr, tp_rr = float(params["tp1_rr"]), float(params["tp_rr"])
            if signal_type == "LONG":
                struct = ti.get_recent_low(candles, int(params["sl_lookback"]))
                struct = min(struct, last["low"]) if struct is not None else last["low"]
                sl = struct - atr_buf
                risk = price - sl
                if risk <= 0:
                    risk = price * 0.003
                    sl = price - risk
                levels = {"entry": round(price, 6), "stop_loss": round(sl, 6),
                          "take_profit_1": round(price + risk * tp1_rr, 6),
                          "take_profit_full": round(price + risk * tp_rr, 6),
                          "crv": round(ti.calculate_crv(price, sl, price + risk * tp_rr), 2)}
            else:
                struct = ti.get_recent_high(candles, int(params["sl_lookback"]))
                struct = max(struct, last["high"]) if struct is not None else last["high"]
                sl = struct + atr_buf
                risk = sl - price
                if risk <= 0:
                    risk = price * 0.003
                    sl = price + risk
                levels = {"entry": round(price, 6), "stop_loss": round(sl, 6),
                          "take_profit_1": round(price - risk * tp1_rr, 6),
                          "take_profit_full": round(price - risk * tp_rr, 6),
                          "crv": round(ti.calculate_crv(price, sl, price - risk * tp_rr), 2)}

        return {
            "indicators": {"rsi": round(rsi, 2), "ema_fast": round(vwap, 6), "ema_slow": round(vwap, 6),
                           "price": round(price, 6), "vwap": round(vwap, 6),
                           "dev_atr": round(dev / atr, 2) if atr else 0, "rel_vol": round(rel_vol, 2)},
            "rules": rules, "bias": bias,
            "long_count": long_count, "short_count": short_count, "rules_total": len(rules),
            "signal_type": signal_type, "is_pre_signal": False, "levels": levels,
        }

    # ----------------------- Vectorized Fast-Path -----------------------
    @staticmethod
    def vectorized_signals(fs, params: Dict) -> Optional[Dict]:
        """VWAP-Abweichung + RSI-Extrem + HA-Flip + Volumen -- vektorisiert."""
        rsi_p = int(params["rsi_period"])
        atr_p = int(params["atr_period"])
        need = max(rsi_p, atr_p, 30) + 10

        d = {"rsi_period": rsi_p, "atr_period": atr_p, "volume_sma_period": 20}
        close = fs.close
        rsi = fs.get("rsi", d)
        atr = fs.get("atr", d)
        vwap = fs.get("vwap", d)
        rel_vol = fs.get("rel_volume", d)
        ha = fs.get("ha_color", d)
        ha_prev = np.concatenate([[np.nan], ha[:-1]])

        with np.errstate(invalid="ignore", divide="ignore"):
            dev = close - vwap
            min_dev = atr * float(params["dev_atr_mult"])
            below = dev <= -min_dev
            above = dev >= min_dev
            rsi_l = rsi <= float(params["rsi_long_max"])
            rsi_s = rsi >= float(params["rsi_short_min"])
            flip_green = (ha >= 0.5) & (ha_prev < 0.5)
            flip_red = (ha < 0.5) & (ha_prev >= 0.5)
            vol_ok = rel_vol >= float(params["rel_vol_min"])

            long_ok = below & rsi_l & flip_green & vol_ok
            short_ok = above & rsi_s & flip_red & vol_ok

        valid = ~np.isnan(rsi) & ~np.isnan(atr) & (atr > 0) & ~np.isnan(vwap) & ~np.isnan(ha_prev)
        long_ok &= valid
        short_ok &= valid
        return {"long": long_ok, "short": short_ok,
                "warmup": need, "rules_total": 4, "rsi": rsi}
