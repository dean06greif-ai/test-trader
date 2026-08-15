"""
EMA Pullback Scalping Strategie (Tim Flossbach - 1 Min Timeframe)

Quelle: "2300€ in nur 1 Stunde - BESTE Scalping Strategie für 1min Time Frame"

Setup:
  3 EMAs: EMA 25 (schnell/grün), EMA 50 (mittel/rosa), EMA 100 (langsam/türkis)

Entry-Logik (Pullback in Trend):
  1. Trend       - Alle 3 EMAs zeigen in die gleiche Richtung + genug Abstand
  2. Position    - Preis über EMA 25 (Long) bzw. unter EMA 25 (Short)
  3. Pullback    - Preis fällt unter EMA 25 (und idealerweise EMA 50), NICHT unter EMA 100
  4. Rückkehr    - Preis kehrt über EMA 25 zurück → Entry-Bestätigung

Stop-Loss: Knapp unter/über dem letzten Tiefpunkt/Hochpunkt des Pullbacks
Take-Profit: 2:1 oder 3:1 CRV (Risk/Reward)

WICHTIG: Kein Trade wenn Preis unter EMA 100 fällt (= Trendwechsel)
         Kein Trade bei massiven Abverkäufen (nur kontinuierliche Trends)
"""
from typing import Dict, List, Optional
from strategies.base_strategy import BaseStrategy


class EMAPullbackScalpingStrategy(BaseStrategy):
    STRATEGY_ID = "ema_pullback_scalping"
    STRATEGY_NAME = "Scalping 2 (EMA Pullback)"
    STRATEGY_DESCRIPTION = (
        "Tim Flossbach 1-Min Scalping: 3 EMAs (25/50/100) + Pullback-Entry. "
        "Einstieg wenn Preis nach Pullback unter EMA 25/50 zurück über EMA 25 kommt."
    )
    STRATEGY_TIMEFRAME = "1m"

    DEFAULT_PARAMS = {
        "ema_fast_period": {
            "value": 25, "min": 10, "max": 50, "step": 1,
            "label": "EMA Schnell (Grün)",
            "description": "Schnellste EMA-Linie – Entry-Trigger & Trendrichtung",
        },
        "ema_mid_period": {
            "value": 50, "min": 20, "max": 100, "step": 1,
            "label": "EMA Mittel (Rosa)",
            "description": "Mittlere EMA – Pullback-Ziel & Trendbestätigung",
        },
        "ema_slow_period": {
            "value": 100, "min": 50, "max": 300, "step": 1,
            "label": "EMA Langsam (Türkis)",
            "description": "Langsamste EMA – Trend-Filter (Preis darf NICHT darunter/darüber fallen)",
        },
        "ema_slope_lookback": {
            "value": 5, "min": 3, "max": 15, "step": 1,
            "label": "Trend-Slope Kerzen",
            "description": "Anzahl Kerzen für EMA-Richtungserkennung",
        },
        "ema_min_spacing_pct": {
            "value": 0.02, "min": 0.005, "max": 0.2, "step": 0.005,
            "label": "Min. EMA Abstand %",
            "description": "Mindestabstand zwischen EMAs in % (genug Spielraum = Trend)",
        },
        "pullback_lookback": {
            "value": 10, "min": 3, "max": 30, "step": 1,
            "label": "Pullback Lookback",
            "description": "Kerzen zurückschauen für Pullback-Erkennung",
        },
        "crv_target": {
            "value": 2.0, "min": 1.5, "max": 5.0, "step": 0.1,
            "label": "CRV Ziel",
            "description": "Risk/Reward Ziel (2.0 = 2:1, 3.0 = 3:1)",
        },
        "structure_lookback": {
            "value": 10, "min": 3, "max": 30, "step": 1,
            "label": "Struktur Lookback",
            "description": "Kerzen für Stop-Loss Platzierung (letzter Tiefpunkt/Hochpunkt)",
        },
        "atr_period": {
            "value": 14, "min": 5, "max": 30, "step": 1,
            "label": "ATR Period",
            "description": "Volatilitäts-Periode für Stop-Puffer",
        },
        "atr_sl_multiplier": {
            "value": 0.8, "min": 0.2, "max": 3.0, "step": 0.1,
            "label": "ATR SL Puffer",
            "description": "ATR-Puffer hinter dem Struktur-Stop (Anti-Stop-Hunt)",
        },
        "require_mid_ema_touch": {
            "value": 0, "min": 0, "max": 1, "step": 1,
            "label": "EMA 50 Touch nötig",
            "description": "1 = Pullback muss bis EMA 50 reichen (konservativer, weniger Trades)",
        },
    }

    def analyze(self, candles: List[Dict], symbol: str, params: Dict) -> Optional[Dict]:
        ema_fast_p = int(params["ema_fast_period"])
        ema_mid_p = int(params["ema_mid_period"])
        ema_slow_p = int(params["ema_slow_period"])
        slope_lb = int(params["ema_slope_lookback"])
        pb_lookback = int(params["pullback_lookback"])
        crv_target = float(params["crv_target"])
        struct_lb = int(params["structure_lookback"])
        atr_period = int(params["atr_period"])
        atr_mult = float(params["atr_sl_multiplier"])
        min_spacing = float(params["ema_min_spacing_pct"]) / 100.0
        require_mid = int(params.get("require_mid_ema_touch", 0)) == 1

        min_candles = max(ema_slow_p + slope_lb + 5, pb_lookback + ema_slow_p, 120)
        if len(candles) < min_candles:
            return None

        ti = self.indicators
        closes = [c["close"] for c in candles]
        price = closes[-1]

        # Calculate 3 EMAs
        ema_fast_arr = ti.calculate_ema(closes, ema_fast_p)
        ema_mid_arr = ti.calculate_ema(closes, ema_mid_p)
        ema_slow_arr = ti.calculate_ema(closes, ema_slow_p)
        atr_arr = ti.calculate_atr(candles, atr_period)

        ema_fast = ema_fast_arr[-1]
        ema_mid = ema_mid_arr[-1]
        ema_slow = ema_slow_arr[-1]
        atr = atr_arr[-1]

        if None in (ema_fast, ema_mid, ema_slow):
            return None

        # ── Rule 1: Trend – alle 3 EMAs zeigen in gleiche Richtung ──
        ema_fast_prev = ema_fast_arr[-(slope_lb + 1)] if len(ema_fast_arr) > slope_lb else None
        ema_mid_prev = ema_mid_arr[-(slope_lb + 1)] if len(ema_mid_arr) > slope_lb else None
        ema_slow_prev = ema_slow_arr[-(slope_lb + 1)] if len(ema_slow_arr) > slope_lb else None

        if None in (ema_fast_prev, ema_mid_prev, ema_slow_prev):
            return None

        ema_fast_rising = ema_fast > ema_fast_prev
        ema_mid_rising = ema_mid > ema_mid_prev
        ema_slow_rising = ema_slow > ema_slow_prev

        ema_fast_falling = ema_fast < ema_fast_prev
        ema_mid_falling = ema_mid < ema_mid_prev
        ema_slow_falling = ema_slow < ema_slow_prev

        all_rising = ema_fast_rising and ema_mid_rising and ema_slow_rising
        all_falling = ema_fast_falling and ema_mid_falling and ema_slow_falling

        r1_long = all_rising
        r1_short = all_falling

        # ── Rule 2: EMA Spacing – genug Abstand zwischen EMAs (kein Durcheinander) ──
        spacing_fast_mid = abs(ema_fast - ema_mid) / price
        spacing_mid_slow = abs(ema_mid - ema_slow) / price

        # For uptrend: EMA 25 > EMA 50 > EMA 100
        ema_ordered_long = ema_fast > ema_mid > ema_slow
        ema_ordered_short = ema_fast < ema_mid < ema_slow

        r2_long = ema_ordered_long and spacing_fast_mid >= min_spacing
        r2_short = ema_ordered_short and spacing_fast_mid >= min_spacing

        # ── Rule 3: Pullback Detection ──
        # Check recent candles for a pullback below EMA 25 (and optionally EMA 50)
        # that did NOT breach EMA 100
        pb_long, pb_short = self._detect_pullback(
            candles, closes, ema_fast_arr, ema_mid_arr, ema_slow_arr,
            pb_lookback, require_mid
        )

        r3_long = pb_long
        r3_short = pb_short

        # ── Rule 4: Rückkehr – Preis ist zurück über EMA 25 (Long) / unter EMA 25 (Short) ──
        r4_long = price > ema_fast
        r4_short = price < ema_fast

        # ── Keine massive Bewegung (Anti-Abverkauf) ──
        # Prüfe ob die letzte Kerze extrem groß ist vs ATR
        last_range = candles[-1]["high"] - candles[-1]["low"]
        is_massive_move = (atr and atr > 0 and last_range > atr * 4.0)

        rules = [
            {
                "id": "rule1_ema_trend",
                "label": f"EMA Trend ({ema_fast_p}/{ema_mid_p}/{ema_slow_p})",
                "description": f"Alle 3 EMAs zeigen in gleiche Richtung",
                "long": r1_long, "short": r1_short,
            },
            {
                "id": "rule2_ema_spacing",
                "label": "EMA Ordnung & Abstand",
                "description": f"EMAs korrekt geordnet mit genug Spielraum (≥{params['ema_min_spacing_pct']}%)",
                "long": r2_long, "short": r2_short,
            },
            {
                "id": "rule3_pullback",
                "label": "Pullback erkannt",
                "description": f"Preis war unter EMA {ema_fast_p}" + (f"/EMA {ema_mid_p}" if require_mid else "") + f", nicht unter EMA {ema_slow_p}",
                "long": r3_long, "short": r3_short,
            },
            {
                "id": "rule4_reclaim",
                "label": f"Rückkehr über/unter EMA {ema_fast_p}",
                "description": f"Preis zurück über EMA {ema_fast_p} (Long) / unter (Short) = Entry",
                "long": r4_long, "short": r4_short,
            },
        ]

        long_flags = [r1_long, r2_long, r3_long, r4_long]
        short_flags = [r1_short, r2_short, r3_short, r4_short]
        long_cnt = sum(long_flags)
        short_cnt = sum(short_flags)

        bias = "LONG" if long_cnt > short_cnt else ("SHORT" if short_cnt > long_cnt else None)

        signal_type = None
        is_pre = False

        if all(long_flags) and not is_massive_move:
            signal_type = "LONG"
        elif all(short_flags) and not is_massive_move:
            signal_type = "SHORT"
        else:
            # Pre-Signal: Trend + Spacing + Pullback vorhanden, Preis noch nicht zurück
            if r1_long and r2_long and r3_long and not r4_long and not is_massive_move:
                # Price is still below EMA fast but pullback is active
                if price > ema_mid:  # approaching reclaim
                    signal_type, is_pre = "LONG", True
            elif r1_short and r2_short and r3_short and not r4_short and not is_massive_move:
                if price < ema_mid:
                    signal_type, is_pre = "SHORT", True

        levels = None
        if signal_type:
            levels = self._compute_levels(
                candles, price, signal_type, atr, atr_mult, crv_target, struct_lb
            )

        return {
            "indicators": {
                "ema_fast": round(ema_fast, 6),
                "ema_mid": round(ema_mid, 6),
                "ema_slow": round(ema_slow, 6),
                "price": round(price, 6),
                "atr": round(atr, 6) if atr else 0,
                "spacing_fast_mid_pct": round(spacing_fast_mid * 100, 4),
                "spacing_mid_slow_pct": round(spacing_mid_slow * 100, 4),
            },
            "rules": rules,
            "bias": bias,
            "long_count": long_cnt,
            "short_count": short_cnt,
            "rules_total": 4,
            "signal_type": signal_type,
            "is_pre_signal": is_pre,
            "levels": levels,
        }

    def _detect_pullback(
        self, candles, closes, ema_fast_arr, ema_mid_arr, ema_slow_arr,
        lookback, require_mid
    ) -> tuple:
        """
        Detect if a pullback happened in the recent `lookback` candles.

        LONG Pullback:
          - Price dipped below EMA 25 (and optionally EMA 50)
          - Price did NOT go below EMA 100
          - Currently price is recovering (approaching or above EMA 25)

        SHORT Pullback:
          - Price spiked above EMA 25 (and optionally EMA 50)
          - Price did NOT go above EMA 100
          - Currently price is falling back (approaching or below EMA 25)
        """
        n = len(candles)
        start = max(0, n - lookback)

        pb_long = False
        pb_short = False

        dipped_below_fast_long = False
        dipped_below_mid_long = False
        breached_slow_long = False

        spiked_above_fast_short = False
        spiked_above_mid_short = False
        breached_slow_short = False

        for i in range(start, n - 1):  # exclude current candle
            c = closes[i]
            low_price = candles[i]["low"]
            high_price = candles[i]["high"]
            ef = ema_fast_arr[i]
            em = ema_mid_arr[i]
            es = ema_slow_arr[i]

            if None in (ef, em, es):
                continue

            # Long pullback detection
            if low_price < ef:
                dipped_below_fast_long = True
            if low_price < em:
                dipped_below_mid_long = True
            if low_price < es:
                breached_slow_long = True

            # Short pullback detection
            if high_price > ef:
                spiked_above_fast_short = True
            if high_price > em:
                spiked_above_mid_short = True
            if high_price > es:
                breached_slow_short = True

        # Long: dipped below fast (and optionally mid), NOT below slow
        if dipped_below_fast_long and not breached_slow_long:
            if require_mid:
                pb_long = dipped_below_mid_long
            else:
                pb_long = True

        # Short: spiked above fast (and optionally mid), NOT above slow
        if spiked_above_fast_short and not breached_slow_short:
            if require_mid:
                pb_short = spiked_above_mid_short
            else:
                pb_short = True

        return pb_long, pb_short

    def _compute_levels(self, candles, entry, side, atr, atr_mult, crv_target, lookback):
        """Stop-Loss at pullback structure + ATR buffer, TP at CRV target."""
        buffer = (atr * atr_mult) if atr else (entry * 0.0003 * lookback)

        if side == "LONG":
            low = self.indicators.get_recent_low(candles, lookback)
            sl = low - buffer
            risk = entry - sl
            if risk <= 0:
                risk = buffer or entry * 0.002
                sl = entry - risk
            tp1 = entry + risk  # 1:1
            tpf = entry + risk * crv_target
        else:
            high = self.indicators.get_recent_high(candles, lookback)
            sl = high + buffer
            risk = sl - entry
            if risk <= 0:
                risk = buffer or entry * 0.002
                sl = entry + risk
            tp1 = entry - risk  # 1:1
            tpf = entry - risk * crv_target

        return {
            "entry": round(entry, 6),
            "stop_loss": round(sl, 6),
            "take_profit_1": round(tp1, 6),
            "take_profit_full": round(tpf, 6),
            "crv": round(self.indicators.calculate_crv(entry, sl, tpf), 2),
        }

    # ----------------------- Vectorized Fast-Path -----------------------
    @staticmethod
    def vectorized_signals(fs, params: Dict) -> Optional[Dict]:
        """3-EMA-Trend + Pullback + Rückkehr (inkl. Pre-Signale) -- vektorisiert."""
        import numpy as np
        import pandas as pd
        ema_f_p = int(params["ema_fast_period"])
        ema_m_p = int(params["ema_mid_period"])
        ema_s_p = int(params["ema_slow_period"])
        slope_lb = int(params["ema_slope_lookback"])
        pb_lb = int(params["pullback_lookback"])
        min_spacing = float(params["ema_min_spacing_pct"]) / 100.0
        require_mid = int(params.get("require_mid_ema_touch", 0)) == 1
        need = max(ema_s_p + slope_lb + 5, pb_lb + ema_s_p, 120)

        close, low, high = fs.close, fs.low, fs.high
        ef = fs._cached_ema(ema_f_p)
        em = fs._cached_ema(ema_m_p)
        es = fs._cached_ema(ema_s_p)
        atr = fs.get("atr", {"atr_period": int(params["atr_period"])})

        def shift(a, k):
            return np.concatenate([np.full(k, np.nan), a[:-k]])

        ef_p, em_p, es_p = shift(ef, slope_lb), shift(em, slope_lb), shift(es, slope_lb)

        with np.errstate(invalid="ignore", divide="ignore"):
            r1_long = (ef > ef_p) & (em > em_p) & (es > es_p)
            r1_short = (ef < ef_p) & (em < em_p) & (es < es_p)

            spacing = np.abs(ef - em) / close
            r2_long = (ef > em) & (em > es) & (spacing >= min_spacing)
            r2_short = (ef < em) & (em < es) & (spacing >= min_spacing)

            # Pullback über die letzten (pb_lb - 1) Kerzen VOR der aktuellen
            def rolled_any(cond):
                s = pd.Series(np.where(np.isnan(cond.astype(float)), 0.0,
                                       cond.astype(float)))
                win = max(pb_lb - 1, 1)
                return (s.rolling(win, min_periods=1).max().shift(1)
                        .to_numpy() >= 0.5)

            dip_fast = rolled_any(low < ef)
            dip_mid = rolled_any(low < em)
            brk_slow_l = rolled_any(low < es)
            spike_fast = rolled_any(high > ef)
            spike_mid = rolled_any(high > em)
            brk_slow_s = rolled_any(high > es)

            r3_long = dip_fast & ~brk_slow_l & (dip_mid if require_mid
                                                else np.ones(fs.n, dtype=bool))
            r3_short = spike_fast & ~brk_slow_s & (spike_mid if require_mid
                                                   else np.ones(fs.n, dtype=bool))

            r4_long = close > ef
            r4_short = close < ef

            massive = (~np.isnan(atr)) & (atr > 0) & ((high - low) > atr * 4.0)

            long_ok = r1_long & r2_long & r3_long & r4_long & ~massive
            short_ok = r1_short & r2_short & r3_short & r4_short & ~massive
            long_pre = (r1_long & r2_long & r3_long & ~r4_long & ~massive
                        & (close > em))
            short_pre = (r1_short & r2_short & r3_short & ~r4_short & ~massive
                         & (close < em))

        valid = (~np.isnan(ef) & ~np.isnan(em) & ~np.isnan(es)
                 & ~np.isnan(ef_p) & ~np.isnan(em_p) & ~np.isnan(es_p))
        long_ok &= valid
        short_ok &= valid
        long_pre &= valid
        short_pre &= valid
        return {"long": long_ok, "short": short_ok,
                "long_pre": long_pre, "short_pre": short_pre,
                "warmup": need, "rules_total": 4, "rsi": None}
