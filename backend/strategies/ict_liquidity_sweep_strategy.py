"""
ICT Liquidity Sweep Strategy (Smart Money 2026 Simplified)

Quelle / Inspiration:
  YouTube "The NEW Way to Start Day Trading ICT in 2026 (FULL GUIDE)"
  https://www.youtube.com/watch?v=-iaGMR8j6V8

Diese Strategie fokussiert sich auf die 4 Kernkonzepte, die im Video als "das
Einzige, was wirklich wichtig ist" beschrieben werden. Alles andere ICT-Beiwerk
wird bewusst weggelassen:

  1. LIQUIDITY DETECTION
     Retail-Trader platzieren ihre Stops auf offensichtlichen Leveln
     (Swing High/Low, gleiche Hochs/Tiefs, Trendlinien). Banken müssen genau
     dorthin, um ihre grossen Orders zu fuellen.

  2. FAIR VALUE GAP (FVG)  — 3-Kerzen-Imbalance
     Nach starker Bewegung entsteht eine "Luecke" (Kerze 1 High != Kerze 3 Low).
     Der Markt sucht diese Imbalance spaeter wieder auf, um sie zu schliessen.

  3. DISCOUNT / PREMIUM  — Fibonacci 50 %
     Long nur im Discount (unteren Halbraum) einer Range, Short nur im Premium.
     Nutzt das existierende `range_position()`-Utility.

  4. DAILY BIAS
     Aus welcher High-Timeframe-Liquiditaet liefert der Markt aktuell?
     Welche Draws-on-Liquidity liegen noch offen? Wir mappen das auf einen
     HTF-EMA-Trend + zuletzt genommene Liquiditaet.

Entry-Modell (identisch zum Video):
   Bias LONG  -> warte auf Sell-Side-Sweep (Bank nimmt Stops unter Swing-Low)
                 -> Preis kehrt in eine bullische FVG im Discount zurueck
                 -> Rejection aus FVG = Entry, SL unter Sweep-Low, TP = naechster
                    unerledigter Buy-Side Draw-on-Liquidity
   Bias SHORT -> spiegelverkehrt.

Der Signal-Wert wird als PRE_SIGNAL geliefert, sobald 3 der 4 Bedingungen
erfuellt sind (Bias + Sweep + Zone), und wird zum vollen SIGNAL sobald die
FVG-Interaktion + Rejection dazukommt.
"""
from typing import Dict, List, Optional, Tuple
from strategies.base_strategy import BaseStrategy


class ICTLiquiditySweepStrategy(BaseStrategy):
    STRATEGY_ID = "ict_liquidity_sweep"
    STRATEGY_NAME = "ICT Liquidity Sweep"
    STRATEGY_DESCRIPTION = (
        "Smart-Money 2026: Liquidity Sweep -> Fair Value Gap -> "
        "Discount/Premium -> Daily Bias. 4 Kernkonzepte, ein Entry-Modell."
    )
    STRATEGY_TIMEFRAME = "5m"

    DEFAULT_PARAMS = {
        "htf_ema_period": {
            "value": 50, "min": 20, "max": 200, "step": 1,
            "label": "HTF Trend EMA",
            "description": "EMA fuer High-Timeframe Bias-Bestimmung",
        },
        "range_lookback": {
            "value": 60, "min": 20, "max": 300, "step": 5,
            "label": "Range Lookback",
            "description": "Kerzen fuer aktuelle Range (Discount/Premium & DOL)",
        },
        "discount_zone": {
            "value": 0.45, "min": 0.20, "max": 0.50, "step": 0.05,
            "label": "Discount Zone <=",
            "description": "Long nur wenn Range-Position <= diesem Wert",
        },
        "premium_zone": {
            "value": 0.55, "min": 0.50, "max": 0.80, "step": 0.05,
            "label": "Premium Zone >=",
            "description": "Short nur wenn Range-Position >= diesem Wert",
        },
        "swing_left": {
            "value": 3, "min": 1, "max": 10, "step": 1,
            "label": "Swing Left",
            "description": "Kerzen links fuer Swing-Fractal (Liquiditaets-Level)",
        },
        "swing_right": {
            "value": 3, "min": 1, "max": 10, "step": 1,
            "label": "Swing Right",
            "description": "Kerzen rechts fuer Swing-Fractal",
        },
        "sweep_lookback": {
            "value": 8, "min": 2, "max": 30, "step": 1,
            "label": "Sweep Lookback",
            "description": "Wie viele Kerzen zurueck darf der Liquidity-Sweep sein",
        },
        "equal_level_tol_pct": {
            "value": 0.05, "min": 0.01, "max": 0.5, "step": 0.01,
            "label": "Equal H/L Toleranz %",
            "description": "Preis-Toleranz fuer relative equal highs/lows",
        },
        "fvg_min_size_pct": {
            "value": 0.05, "min": 0.01, "max": 1.0, "step": 0.01,
            "label": "Min. FVG Groesse %",
            "description": "Mindest-Gap in % vom Preis (kleine FVGs = Rauschen)",
        },
        "fvg_max_age": {
            "value": 40, "min": 5, "max": 200, "step": 1,
            "label": "Max. FVG Alter",
            "description": "Wie viele Kerzen alt darf die FVG maximal sein",
        },
        "atr_period": {
            "value": 14, "min": 5, "max": 30, "step": 1,
            "label": "ATR Period",
            "description": "Volatilitaet fuer SL-Puffer",
        },
        "atr_sl_mult": {
            "value": 0.5, "min": 0.1, "max": 3.0, "step": 0.1,
            "label": "ATR SL Puffer",
            "description": "ATR-Puffer hinter dem Sweep-Extremum",
        },
        "tp1_rr": {
            "value": 1.0, "min": 0.5, "max": 5.0, "step": 0.1,
            "label": "TP1 R-Multiple",
            "description": "Erstes Ziel als Vielfaches des Risikos",
        },
        "tp_full_rr": {
            "value": 2.5, "min": 1.0, "max": 8.0, "step": 0.1,
            "label": "TP voll R-Multiple",
            "description": "Vollziel; wird auf naechsten Draw-on-Liquidity gecapped",
        },
        "require_rejection": {
            "value": 1, "min": 0, "max": 1, "step": 1,
            "label": "FVG Rejection nötig",
            "description": "1 = Entry erst nach Rejection aus FVG (konservativ)",
        },
    }

    # ------------------------------------------------------------------
    # main entry
    # ------------------------------------------------------------------
    def analyze(self, candles: List[Dict], symbol: str, params: Dict) -> Optional[Dict]:
        htf_p = int(params["htf_ema_period"])
        lookback = int(params["range_lookback"])
        atr_p = int(params["atr_period"])
        need = max(htf_p, lookback, atr_p) + 10
        if len(candles) < need:
            return None

        ti = self.indicators
        closes = [c["close"] for c in candles]
        price = closes[-1]
        last = candles[-1]

        # --- 1. Daily Bias -------------------------------------------------
        ema_arr = ti.calculate_ema(closes, htf_p)
        ema = self._last(ema_arr)
        ema_prev = ema_arr[-6] if len(ema_arr) >= 6 and ema_arr[-6] else None
        if ema is None:
            return None
        ema_slope = (ema - ema_prev) / ema_prev if ema_prev else 0.0
        bias_long = price > ema and ema_slope > 0
        bias_short = price < ema and ema_slope < 0

        # --- 2. Liquidity map --------------------------------------------
        left = int(params["swing_left"])
        right = int(params["swing_right"])
        swing_highs, swing_lows = ti.find_swings(candles[:-1], left, right)

        tol_pct = float(params["equal_level_tol_pct"]) / 100.0
        equal_highs = self._relative_equal_levels([h for _, h in swing_highs], tol_pct * price)
        equal_lows = self._relative_equal_levels([l for _, l in swing_lows], tol_pct * price)

        # remaining (untaken) draws-on-liquidity above / below
        buy_dol = self._nearest_above([h for _, h in swing_highs], price)
        sell_dol = self._nearest_below([l for _, l in swing_lows], price)

        # --- 3. Liquidity sweep detection --------------------------------
        sweep_lb = int(params["sweep_lookback"])
        sweep_dir, sweep_extreme = self._detect_sweep(
            candles, swing_highs, swing_lows, sweep_lb
        )

        # --- 4. Discount / Premium ---------------------------------------
        rpos = ti.range_position(candles, lookback)
        in_discount = rpos <= float(params["discount_zone"])
        in_premium = rpos >= float(params["premium_zone"])

        # --- 5. Fair Value Gaps -------------------------------------------
        min_size = float(params["fvg_min_size_pct"]) / 100.0 * price
        max_age = int(params["fvg_max_age"])
        fvgs = self._find_fvgs(candles, min_size, max_age)
        # is price currently inside a bullish (support) FVG (below price of gap)
        active_bull_fvg = self._active_fvg(fvgs, "bull", price)
        active_bear_fvg = self._active_fvg(fvgs, "bear", price)

        # --- Rules ---------------------------------------------------------
        r_bias_l = bool(bias_long)
        r_bias_s = bool(bias_short)

        r_sweep_l = sweep_dir == "bullish"   # sell-side sweep = LONG-fuel
        r_sweep_s = sweep_dir == "bearish"   # buy-side sweep  = SHORT-fuel

        r_zone_l = bool(in_discount)
        r_zone_s = bool(in_premium)

        r_fvg_l = active_bull_fvg is not None
        r_fvg_s = active_bear_fvg is not None

        require_rej = int(params["require_rejection"]) == 1
        rejection_long = self._rejection_up(last, active_bull_fvg) if active_bull_fvg else False
        rejection_short = self._rejection_down(last, active_bear_fvg) if active_bear_fvg else False

        rules = [
            {
                "id": "daily_bias", "label": "Daily Bias",
                "description": f"HTF EMA{htf_p} + Slope -> Richtung",
                "long": r_bias_l, "short": r_bias_s,
            },
            {
                "id": "liquidity_sweep", "label": "Liquidity Sweep",
                "description": "Stops unter Swing-Low (Long) / ueber Swing-High (Short) genommen",
                "long": r_sweep_l, "short": r_sweep_s,
            },
            {
                "id": "discount_premium", "label": "Discount / Premium",
                "description": "Long im Discount / Short im Premium der Range",
                "long": r_zone_l, "short": r_zone_s,
            },
            {
                "id": "fvg_tap", "label": "Fair Value Gap",
                "description": "Preis in einer noch offenen FVG (Imbalance)",
                "long": r_fvg_l, "short": r_fvg_s,
            },
        ]

        long_flags = [r_bias_l, r_sweep_l, r_zone_l, r_fvg_l]
        short_flags = [r_bias_s, r_sweep_s, r_zone_s, r_fvg_s]
        long_cnt = sum(long_flags)
        short_cnt = sum(short_flags)
        bias = "LONG" if long_cnt > short_cnt else ("SHORT" if short_cnt > long_cnt else None)

        signal_type: Optional[str] = None
        is_pre = False

        if all(long_flags) and (rejection_long or not require_rej):
            signal_type = "LONG"
        elif all(short_flags) and (rejection_short or not require_rej):
            signal_type = "SHORT"
        else:
            # PRE-SIGNAL: 3 of 4 aligned in one direction
            if long_cnt >= 3 and long_cnt > short_cnt:
                signal_type, is_pre = "LONG", True
            elif short_cnt >= 3 and short_cnt > long_cnt:
                signal_type, is_pre = "SHORT", True

        # --- Levels --------------------------------------------------------
        levels = None
        if signal_type and not is_pre:
            levels = self._compute_levels(
                signal_type, price, candles, sweep_extreme,
                buy_dol, sell_dol, params
            )

        return {
            "indicators": {
                "price": round(price, 6),
                "htf_ema": round(ema, 6),
                "ema_slope_pct": round(ema_slope * 100, 3),
                "range_pos": round(rpos, 3),
                "sweep": sweep_dir or "none",
                "buy_side_dol": round(buy_dol, 6) if buy_dol else None,
                "sell_side_dol": round(sell_dol, 6) if sell_dol else None,
                "equal_highs": len(equal_highs),
                "equal_lows": len(equal_lows),
                "active_fvg_bull": round(active_bull_fvg["mid"], 6) if active_bull_fvg else None,
                "active_fvg_bear": round(active_bear_fvg["mid"], 6) if active_bear_fvg else None,
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

    # ------------------------------------------------------------------
    # helpers - Liquidity
    # ------------------------------------------------------------------
    @staticmethod
    def _relative_equal_levels(levels: List[float], tol: float) -> List[float]:
        """Return levels that appear at least twice within `tol` price distance."""
        if not levels or tol <= 0:
            return []
        result = []
        for i, a in enumerate(levels):
            for b in levels[i + 1:]:
                if abs(a - b) <= tol:
                    result.append((a + b) / 2)
                    break
        return result

    @staticmethod
    def _nearest_above(levels: List[float], price: float) -> Optional[float]:
        above = [l for l in levels if l > price]
        return min(above) if above else None

    @staticmethod
    def _nearest_below(levels: List[float], price: float) -> Optional[float]:
        below = [l for l in levels if l < price]
        return max(below) if below else None

    @staticmethod
    def _detect_sweep(
        candles: List[Dict],
        swing_highs: List[Tuple[int, float]],
        swing_lows: List[Tuple[int, float]],
        lookback: int,
    ) -> Tuple[Optional[str], Optional[float]]:
        """
        Detects an ICT-style liquidity sweep in the last `lookback` candles:
          * bullish sweep = a candle wicks BELOW a prior swing low and closes back above it
          * bearish sweep = a candle wicks ABOVE a prior swing high and closes back below it
        Returns (direction, extreme_price_of_sweep_candle).
        """
        n = len(candles)
        start = max(0, n - lookback)
        recent_lows = [l for _, l in swing_lows[-5:]]
        recent_highs = [h for _, h in swing_highs[-5:]]
        # scan from most-recent backwards for the freshest sweep
        for i in range(n - 1, start - 1, -1):
            c = candles[i]
            for lo in recent_lows:
                if c["low"] < lo and c["close"] > lo:
                    return "bullish", c["low"]
            for hi in recent_highs:
                if c["high"] > hi and c["close"] < hi:
                    return "bearish", c["high"]
        return None, None

    # ------------------------------------------------------------------
    # helpers - Fair Value Gaps
    # ------------------------------------------------------------------
    @staticmethod
    def _find_fvgs(candles: List[Dict], min_size: float, max_age: int) -> List[Dict]:
        """
        Detect 3-candle Fair Value Gaps:
          Bullish FVG:  candle[i].high  < candle[i+2].low   (gap between them)
          Bearish FVG:  candle[i].low   > candle[i+2].high

        Returns the still-valid gaps (mitigation not yet fully filled) as:
          {type, top, bottom, mid, age}
        """
        n = len(candles)
        if n < 3:
            return []
        start = max(0, n - max_age - 3)
        result: List[Dict] = []
        for i in range(start, n - 2):
            c1, c2, c3 = candles[i], candles[i + 1], candles[i + 2]
            # bullish FVG (imbalance up)
            if c1["high"] < c3["low"]:
                gap_bot, gap_top = c1["high"], c3["low"]
                if (gap_top - gap_bot) < min_size:
                    continue
                # check if still open (not fully closed by later candles)
                still_open = True
                for later in candles[i + 3:]:
                    if later["low"] <= gap_bot:
                        still_open = False
                        break
                if still_open:
                    result.append({
                        "type": "bull",
                        "bottom": gap_bot, "top": gap_top,
                        "mid": (gap_bot + gap_top) / 2,
                        "age": n - (i + 2),
                    })
            # bearish FVG (imbalance down)
            if c1["low"] > c3["high"]:
                gap_top, gap_bot = c1["low"], c3["high"]
                if (gap_top - gap_bot) < min_size:
                    continue
                still_open = True
                for later in candles[i + 3:]:
                    if later["high"] >= gap_top:
                        still_open = False
                        break
                if still_open:
                    result.append({
                        "type": "bear",
                        "bottom": gap_bot, "top": gap_top,
                        "mid": (gap_bot + gap_top) / 2,
                        "age": n - (i + 2),
                    })
        return result

    @staticmethod
    def _active_fvg(fvgs: List[Dict], kind: str, price: float) -> Optional[Dict]:
        """Return the fvg of `kind` in which price currently sits (closest by mid)."""
        candidates = [f for f in fvgs if f["type"] == kind and f["bottom"] <= price <= f["top"]]
        if not candidates:
            return None
        return min(candidates, key=lambda f: abs(price - f["mid"]))

    @staticmethod
    def _rejection_up(last: Dict, fvg: Dict) -> bool:
        """Bullish rejection out of FVG: low tapped inside, close above FVG mid, green."""
        if not fvg:
            return False
        return (last["low"] <= fvg["top"]
                and last["close"] > fvg["mid"]
                and last["close"] > last["open"])

    @staticmethod
    def _rejection_down(last: Dict, fvg: Dict) -> bool:
        if not fvg:
            return False
        return (last["high"] >= fvg["bottom"]
                and last["close"] < fvg["mid"]
                and last["close"] < last["open"])

    # ------------------------------------------------------------------
    # helpers - Levels
    # ------------------------------------------------------------------
    def _compute_levels(
        self,
        side: str,
        entry: float,
        candles: List[Dict],
        sweep_extreme: Optional[float],
        buy_dol: Optional[float],
        sell_dol: Optional[float],
        params: Dict,
    ) -> Dict:
        ti = self.indicators
        atr = self._last(ti.calculate_atr(candles, int(params["atr_period"]))) or 0.0
        buf = atr * float(params["atr_sl_mult"])
        tp1_rr = float(params["tp1_rr"])
        tp_rr = float(params["tp_full_rr"])

        if side == "LONG":
            struct = sweep_extreme if sweep_extreme is not None else ti.get_recent_low(candles, 10)
            sl = struct - buf
            risk = entry - sl
            if risk <= 0:
                risk = entry * 0.003
                sl = entry - risk
            tp1 = entry + risk * tp1_rr
            tp_full = entry + risk * tp_rr
            # Cap TP full at next buy-side draw-on-liquidity (ICT: target open liquidity)
            if buy_dol and buy_dol > entry:
                tp_full = min(tp_full, buy_dol)
        else:  # SHORT
            struct = sweep_extreme if sweep_extreme is not None else ti.get_recent_high(candles, 10)
            sl = struct + buf
            risk = sl - entry
            if risk <= 0:
                risk = entry * 0.003
                sl = entry + risk
            tp1 = entry - risk * tp1_rr
            tp_full = entry - risk * tp_rr
            if sell_dol and sell_dol < entry:
                tp_full = max(tp_full, sell_dol)

        return {
            "entry": round(entry, 6),
            "stop_loss": round(sl, 6),
            "take_profit_1": round(tp1, 6),
            "take_profit_full": round(tp_full, 6),
            "crv": round(ti.calculate_crv(entry, sl, tp_full), 2),
        }

    @staticmethod
    def _last(arr):
        return arr[-1] if arr else None
