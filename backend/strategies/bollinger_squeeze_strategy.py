"""Bollinger Squeeze Breakout: enge Bänder + Ausbruch mit Volumen."""
from typing import Dict, List, Optional
from strategies.base_strategy import BaseStrategy


class BollingerSqueezeStrategy(BaseStrategy):
    STRATEGY_ID = "bollinger_squeeze"
    STRATEGY_NAME = "Bollinger Squeeze Breakout"
    STRATEGY_DESCRIPTION = ("Volatilitäts-Kompression (Squeeze) gefolgt von Band-Ausbruch "
                            "mit Volumen-Spike. Empfohlen auf 5m-30m.")
    STRATEGY_TIMEFRAME = "1m"

    DEFAULT_PARAMS = {
        "bb_period": {"value": 20, "min": 10, "max": 50, "step": 1,
                      "label": "BB Periode", "description": "Bollinger-Band Periode"},
        "bb_std": {"value": 2.0, "min": 1.0, "max": 3.5, "step": 0.1,
                   "label": "BB Std-Abw.", "description": "Standard-Abweichungen"},
        "squeeze_lookback": {"value": 60, "min": 20, "max": 200, "step": 5,
                             "label": "Squeeze Lookback", "description": "Kerzen für Breiten-Vergleich"},
        "squeeze_pct": {"value": 30, "min": 5, "max": 60, "step": 5,
                        "label": "Squeeze Perzentil %", "description": "Bandbreite muss im unteren X% liegen"},
        "rel_vol_min": {"value": 1.3, "min": 0.5, "max": 4.0, "step": 0.1,
                        "label": "Min. Rel. Volumen", "description": "Volumen-Spike beim Ausbruch"},
        "sl_lookback": {"value": 10, "min": 3, "max": 40, "step": 1,
                        "label": "Struktur Lookback", "description": "Kerzen für Swing-SL"},
        "atr_sl_mult": {"value": 1.0, "min": 0.2, "max": 3.0, "step": 0.1,
                        "label": "ATR SL Puffer", "description": "ATR-Puffer hinter der Struktur"},
        "tp1_rr": {"value": 1.0, "min": 0.5, "max": 5.0, "step": 0.1,
                   "label": "TP1 (R)", "description": "Erstes Ziel in R"},
        "tp_rr": {"value": 2.5, "min": 1.0, "max": 8.0, "step": 0.1,
                  "label": "TP voll (R)", "description": "Endziel in R"},
    }

    def analyze(self, candles: List[Dict], symbol: str, params: Dict) -> Optional[Dict]:
        bb_p = int(params["bb_period"])
        lookback = int(params["squeeze_lookback"])
        need = bb_p + lookback + 10
        if len(candles) < need:
            return None
        ti = self.indicators
        closes = [c["close"] for c in candles]
        price = closes[-1]
        bb_u, bb_m, bb_l = ti.calculate_bollinger(closes, bb_p, float(params["bb_std"]))
        atr = ti.calculate_atr(candles, 14)
        if bb_u[-1] is None or bb_m[-1] is None or not bb_m[-1]:
            return None

        widths = []
        for i in range(-lookback - 1, -1):
            if bb_u[i] is not None and bb_l[i] is not None and bb_m[i]:
                widths.append((bb_u[i] - bb_l[i]) / bb_m[i])
        if len(widths) < 10:
            return None
        prev_width = widths[-1]
        sorted_w = sorted(widths)
        thr = sorted_w[max(0, int(len(sorted_w) * float(params["squeeze_pct"]) / 100) - 1)]
        squeeze = prev_width <= thr

        rel_vol = ti.relative_volume(candles, 20) or 0.0
        vol_ok = rel_vol >= float(params["rel_vol_min"])
        prev_close = closes[-2]
        brk_up = price > bb_u[-1] and prev_close <= (bb_u[-2] or prev_close)
        brk_dn = price < bb_l[-1] and prev_close >= (bb_l[-2] or prev_close)
        green = candles[-1]["close"] > candles[-1]["open"]

        rules = [
            {"id": "squeeze", "label": "Squeeze", "description": "Bandbreite in Kompression (unteres Perzentil)",
             "long": bool(squeeze), "short": bool(squeeze)},
            {"id": "breakout", "label": "Band-Ausbruch", "description": "Schlusskurs bricht oberes/unteres Band",
             "long": bool(brk_up), "short": bool(brk_dn)},
            {"id": "volume", "label": "Volumen-Spike", "description": "Rel. Volumen über Minimum",
             "long": bool(vol_ok), "short": bool(vol_ok)},
            {"id": "candle", "label": "Kerzen-Richtung", "description": "Ausbruchskerze in Trade-Richtung",
             "long": bool(green), "short": bool(not green)},
        ]
        signal_long = squeeze and brk_up and vol_ok and green
        signal_short = squeeze and brk_dn and vol_ok and not green
        signal_type = "LONG" if signal_long else ("SHORT" if signal_short else None)
        long_count = sum(1 for x in rules if x["long"])
        short_count = sum(1 for x in rules if x["short"])
        bias = "LONG" if brk_up else ("SHORT" if brk_dn else None)

        levels = None
        if signal_type:
            levels = self._levels(candles, price, signal_type, int(params["sl_lookback"]),
                                  (atr[-1] or 0.0) * float(params["atr_sl_mult"]),
                                  float(params["tp1_rr"]), float(params["tp_rr"]))
        return {
            "indicators": {"rsi": 0, "ema_fast": round(bb_u[-1], 6), "ema_slow": round(bb_l[-1], 6),
                           "price": round(price, 6), "bb_width_pct": round(prev_width * 100, 3),
                           "rel_vol": round(rel_vol, 2)},
            "rules": rules, "bias": bias,
            "long_count": long_count, "short_count": short_count, "rules_total": len(rules),
            "signal_type": signal_type, "is_pre_signal": False, "levels": levels,
        }

    def _levels(self, candles, entry, side, lookback, atr_buf, tp1_rr, tp_rr):
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
        """Squeeze (Perzentil der Bandbreite) + Band-Ausbruch + Volumen -- vektorisiert."""
        import numpy as np
        import pandas as pd
        bb_p = int(params["bb_period"])
        lookback = int(params["squeeze_lookback"])
        need = bb_p + lookback + 10

        d = {"bb_period": bb_p, "bb_std": float(params["bb_std"]),
             "volume_sma_period": 20}
        close = fs.close
        u = fs.get("bb_upper", d)
        m = fs.get("bb_middle", d)
        lo_b = fs.get("bb_lower", d)
        rel_vol = fs.get("rel_volume", d)

        with np.errstate(invalid="ignore", divide="ignore"):
            w = (u - lo_b) / m  # Bandbreite (Anteil, wie Legacy)
            # Schwelle: k-t-kleinster Wert der letzten `lookback` Breiten (bis t-1)
            k = max(0, int(lookback * float(params["squeeze_pct"]) / 100) - 1)
            q = k / (lookback - 1) if lookback > 1 else 0.0
            thr = (pd.Series(w).rolling(lookback, min_periods=lookback)
                   .quantile(q, interpolation="lower").shift(1).to_numpy())
            w_prev = np.concatenate([[np.nan], w[:-1]])
            squeeze = w_prev <= thr

            close_prev = np.concatenate([[np.nan], close[:-1]])
            u_prev = np.concatenate([[np.nan], u[:-1]])
            l_prev = np.concatenate([[np.nan], lo_b[:-1]])
            brk_up = (close > u) & (close_prev <= u_prev)
            brk_dn = (close < lo_b) & (close_prev >= l_prev)
            green = close > fs.open
            vol_ok = rel_vol >= float(params["rel_vol_min"])

            long_ok = squeeze & brk_up & vol_ok & green
            short_ok = squeeze & brk_dn & vol_ok & ~green

        valid = (~np.isnan(u) & ~np.isnan(m) & (m != 0) & ~np.isnan(thr)
                 & ~np.isnan(u_prev) & ~np.isnan(close_prev) & ~np.isnan(rel_vol))
        long_ok &= valid
        short_ok &= valid
        return {"long": long_ok, "short": short_ok,
                "warmup": need, "rules_total": 4, "rsi": None}
