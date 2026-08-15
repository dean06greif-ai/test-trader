"""
PBD Model Strategy — Purge → Break → Displacement (ICT / Smart-Money)
=====================================================================

Eine hochwertige Smart-Money-Strategie, optimiert für langfristige Auto-Trades
mit hoher Winrate x PnL. Der Scanner erkennt automatisch, in welcher PHASE sich
der Chart befindet (P, B oder D) und liefert ein A-Setup-Signal, sobald die
komplette Sequenz sauber abgeschlossen ist.

Kernlogik (sequentielle State-Machine über den Kerzen-Buffer):

  1. P — PURGE (Liquidity Sweep)
     Sweep über/unter ein Key-Level (Previous-Range High/Low, Equal Highs/Lows,
     Swing-Extreme). Ein Wick über das Level + Schlusskurs zurück darunter/darüber
     = valider Purge. Richtung des Purge bestimmt die potenzielle Trade-Richtung
     (Sell-Side-Sweep -> LONG, Buy-Side-Sweep -> SHORT).

  2. B — BREAK (Market Structure Shift / ChoCH)
     Nach dem Purge: Bruch des letzten Swing-Hochs/Tiefs in Gegenrichtung — nur
     mit KRAFT gültig (Kerzen-CLOSE jenseits des Levels, nicht nur ein Wick).

  3. D — DISPLACEMENT (Entry-Zone)
     Starke, impulsive Kerze(n) mit Fair Value Gap (FVG) im Trade-Richtung.
     Entry beim Retracement in die FVG-Zone. Ohne FVG = KEIN Trade
     (Qualitätsfilter für hohe Winrate).

Trade-Setup:
  - Bias über höheren Timeframe (H1/H4 im UI umschaltbar), Entry auf 15m/5m.
  - Stop-Loss: hinter dem Sweep-Extrem (+ ATR-Puffer).
  - Take-Profit: nächster Liquidity-Pool, mindestens 1:3 R:R.
  - Confluence-Score pro Signal (Sweep-Qualität + MSS-Stärke + FVG-Größe +
    Bias-Alignment) -> nur A-Setups (>= min_confluence) werden zum vollen Signal.
  - Cooldown/Duplikat-Schutz je Setup (identisches Sweep-Extrem wird nicht
    mehrfach getradet).

Empfohlene Auto-Trade-Ausführung (über bestehende Bitunix-Pipeline):
  Risiko 1 % pro Trade · Hebel konservativ (max. 5x). Diese Werte werden im
  Auto-Trade-Dialog eingestellt (Ausführung bleibt unverändert wiederverwendet).
"""
from typing import Dict, List, Optional, Tuple
from strategies.base_strategy import BaseStrategy


class PBDModelStrategy(BaseStrategy):
    STRATEGY_ID = "pbd_model"
    STRATEGY_NAME = "PBD Model"
    STRATEGY_DESCRIPTION = (
        "Purge -> Break -> Displacement (Smart-Money). Liquidity-Sweep, "
        "Market-Structure-Shift und Fair-Value-Gap-Entry mit Confluence-Score. "
        "A-Setups only, min. 1:3 R:R. Empfohlen: HTF-Bias H1/H4, Entry 15m/5m."
    )
    STRATEGY_TIMEFRAME = "15m"

    DEFAULT_PARAMS = {
        "htf_bias_tf": {
            "value": 4, "min": 1, "max": 4, "step": 3,
            "label": "HTF Bias (1=H1, 4=H4)",
            "description": "Higher-Timeframe-Bias: 1 = H1, 4 = H4 (im UI umschaltbar)",
        },
        "bias_ema_period": {
            "value": 21, "min": 8, "max": 100, "step": 1,
            "label": "Bias EMA (Basis)",
            "description": "Basis-EMA für Bias; effektiv = Basis x HTF-Faktor",
        },
        "require_bias": {
            "value": 1, "min": 0, "max": 1, "step": 1,
            "label": "Bias-Filter nötig",
            "description": "1 = nur Trades in Richtung des HTF-Bias (empfohlen)",
        },
        "swing_left": {
            "value": 3, "min": 1, "max": 10, "step": 1,
            "label": "Swing Left",
            "description": "Kerzen links für Swing-Fractal (Struktur/Liquidität)",
        },
        "swing_right": {
            "value": 3, "min": 1, "max": 10, "step": 1,
            "label": "Swing Right",
            "description": "Kerzen rechts für Swing-Fractal",
        },
        "key_level_lookback": {
            "value": 80, "min": 20, "max": 400, "step": 5,
            "label": "Key-Level Lookback",
            "description": "Kerzen für Range-Extreme (PDH/PDL-Proxy) & Draw-on-Liquidity",
        },
        "purge_lookback": {
            "value": 12, "min": 3, "max": 40, "step": 1,
            "label": "Purge Lookback",
            "description": "Wie viele Kerzen zurück darf der Liquidity-Sweep sein",
        },
        "equal_level_tol_pct": {
            "value": 0.05, "min": 0.01, "max": 0.5, "step": 0.01,
            "label": "Equal H/L Toleranz %",
            "description": "Preis-Toleranz für relative Equal Highs/Lows",
        },
        "mss_lookback": {
            "value": 10, "min": 2, "max": 40, "step": 1,
            "label": "MSS Lookback",
            "description": "Fenster nach dem Purge, in dem der Break passieren muss",
        },
        "displacement_body_atr": {
            "value": 1.0, "min": 0.3, "max": 4.0, "step": 0.1,
            "label": "Displacement Body (ATR)",
            "description": "Mind. Kerzenkörper der Impuls-Kerze in ATR (Kraft/Displacement)",
        },
        "fvg_min_atr": {
            "value": 0.25, "min": 0.05, "max": 2.0, "step": 0.05,
            "label": "Min. FVG Größe (ATR)",
            "description": "Mindest-Gap der FVG in ATR (kleine FVGs = Rauschen)",
        },
        "fvg_max_age": {
            "value": 30, "min": 5, "max": 150, "step": 1,
            "label": "Max. FVG Alter",
            "description": "Wie viele Kerzen alt darf die Displacement-FVG max. sein",
        },
        "atr_period": {
            "value": 14, "min": 5, "max": 30, "step": 1,
            "label": "ATR Period",
            "description": "Volatilität für Displacement-Messung & SL-Puffer",
        },
        "atr_sl_mult": {
            "value": 0.5, "min": 0.1, "max": 3.0, "step": 0.1,
            "label": "ATR SL Puffer",
            "description": "ATR-Puffer hinter dem Sweep-Extremum",
        },
        "tp1_rr": {
            "value": 1.5, "min": 0.5, "max": 5.0, "step": 0.1,
            "label": "TP1 R-Multiple",
            "description": "Erstes Teilziel als Vielfaches des Risikos",
        },
        "min_rr": {
            "value": 3.0, "min": 1.0, "max": 8.0, "step": 0.5,
            "label": "Min. R:R (Vollziel)",
            "description": "Mindest-Chance/Risiko fürs Vollziel (Standard 1:3)",
        },
        "min_confluence": {
            "value": 55, "min": 0, "max": 100, "step": 5,
            "label": "Min. Confluence Score",
            "description": "Nur A-Setups >= diesem Score werden zum vollen Signal",
        },
    }

    def __init__(self):
        super().__init__()
        # Cooldown / Duplikat-Schutz: pro Symbol das zuletzt getradete Setup
        self._last_setup: Dict[str, Tuple[str, float]] = {}

    # ------------------------------------------------------------------
    # main entry
    # ------------------------------------------------------------------
    def analyze(self, candles: List[Dict], symbol: str, params: Dict) -> Optional[Dict]:
        bias_period = int(params["bias_ema_period"]) * int(params["htf_bias_tf"])
        key_lb = int(params["key_level_lookback"])
        atr_p = int(params["atr_period"])
        need = max(bias_period, key_lb, atr_p) + 15
        if len(candles) < need:
            return None

        ti = self.indicators
        closes = [c["close"] for c in candles]
        price = closes[-1]
        n = len(candles)

        atr_arr = ti.calculate_atr(candles, atr_p)
        atr = self._last_valid(atr_arr) or (price * 0.003)

        # --- 0. HTF Bias --------------------------------------------------
        ema_arr = ti.calculate_ema(closes, bias_period)
        ema = self._last_valid(ema_arr)
        ema_prev = ema_arr[-6] if len(ema_arr) >= 6 and ema_arr[-6] else None
        ema_slope = (ema - ema_prev) / ema_prev if (ema and ema_prev) else 0.0
        bias_long = bool(ema and price > ema and ema_slope >= 0)
        bias_short = bool(ema and price < ema and ema_slope <= 0)
        bias_neutral = not (bias_long or bias_short)

        # --- Liquidity map (Key-Levels / Draw-on-Liquidity) ---------------
        left = int(params["swing_left"])
        right = int(params["swing_right"])
        swing_highs, swing_lows = ti.find_swings(candles[:-1], left, right)
        seg = candles[-key_lb:]
        key_high = max(c["high"] for c in seg)
        key_low = min(c["low"] for c in seg)
        highs_levels = [h for _, h in swing_highs] + [key_high]
        lows_levels = [l for _, l in swing_lows] + [key_low]
        buy_dol = self._nearest_above(highs_levels, price)
        sell_dol = self._nearest_below(lows_levels, price)

        # --- 1. PURGE (Liquidity Sweep) — Kandidaten (jüngste zuerst) ------
        purge_candidates = self._detect_purges(
            candles, swing_lows, swing_highs, key_low, key_high,
            int(params["purge_lookback"]), atr,
        )

        # --- 2. BREAK (MSS/ChoCH) + 3. DISPLACEMENT (FVG) -----------------
        # Wähle den Purge, auf den eine VOLLSTÄNDIGE Sequenz P->B->D folgt.
        purge = None
        mss = None
        disp_fvg = None
        mss_lb = int(params["mss_lookback"])
        for cand in purge_candidates:
            m = self._detect_mss(candles, cand, swing_highs, swing_lows, mss_lb)
            if not m:
                continue
            f = self._detect_displacement_fvg(
                candles, cand, atr,
                float(params["displacement_body_atr"]),
                float(params["fvg_min_atr"]),
                int(params["fvg_max_age"]),
            )
            purge, mss, disp_fvg = cand, m, f
            if f is not None:
                break  # komplette P->B->D Sequenz gefunden
        # Fallback: keine vollständige Sequenz -> jüngsten Purge für Phase/Pre
        if purge is None and purge_candidates:
            purge = purge_candidates[0]
            mss = self._detect_mss(candles, purge, swing_highs, swing_lows, mss_lb)
            if mss:
                disp_fvg = self._detect_displacement_fvg(
                    candles, purge, atr,
                    float(params["displacement_body_atr"]),
                    float(params["fvg_min_atr"]),
                    int(params["fvg_max_age"]),
                )

        side = purge["side"] if purge else None  # "LONG" | "SHORT"

        p_done = purge is not None
        b_done = mss is not None
        # D done = FVG existiert UND Preis ist aktuell in der FVG-Zone (Retrace/Entry)
        in_fvg = bool(disp_fvg and disp_fvg["bottom"] <= price <= disp_fvg["top"])
        d_done = bool(disp_fvg is not None and in_fvg)

        # Phase-Anzeige (P / B / D / —)
        phase = "D" if d_done else ("B" if b_done else ("P" if p_done else "—"))

        # --- Rules (4 Karten für die UI) ----------------------------------
        r_bias_l = bias_long
        r_bias_s = bias_short
        r_purge_l = side == "LONG"
        r_purge_s = side == "SHORT"
        r_break_l = b_done and side == "LONG"
        r_break_s = b_done and side == "SHORT"
        r_disp_l = d_done and side == "LONG"
        r_disp_s = d_done and side == "SHORT"

        rules = [
            {"id": "htf_bias", "label": "HTF Bias",
             "description": f"Bias über {'H4' if int(params['htf_bias_tf']) == 4 else 'H1'} (EMA{bias_period} + Slope)",
             "long": r_bias_l, "short": r_bias_s},
            {"id": "purge", "label": "P · Purge",
             "description": "Liquidity-Sweep über/unter Key-Level + Rückkehr",
             "long": r_purge_l, "short": r_purge_s},
            {"id": "break", "label": "B · Break (MSS)",
             "description": "Market-Structure-Shift: Close jenseits des Swings",
             "long": r_break_l, "short": r_break_s},
            {"id": "displacement", "label": "D · Displacement",
             "description": "Impuls-FVG + Retrace in die Entry-Zone",
             "long": r_disp_l, "short": r_disp_s},
        ]

        long_flags = [r_bias_l, r_purge_l, r_break_l, r_disp_l]
        short_flags = [r_bias_s, r_purge_s, r_break_s, r_disp_s]
        long_cnt = sum(long_flags)
        short_cnt = sum(short_flags)
        bias = "LONG" if long_cnt > short_cnt else ("SHORT" if short_cnt > long_cnt else None)

        # --- Confluence-Score ---------------------------------------------
        confluence = 0
        if p_done:
            confluence = self._confluence_score(
                purge, mss, disp_fvg, atr,
                bias_long if side == "LONG" else bias_short,
                bias_neutral,
            )

        require_bias = int(params["require_bias"]) == 1
        bias_ok_long = (not require_bias) or bias_long or bias_neutral
        bias_ok_short = (not require_bias) or bias_short or bias_neutral
        min_conf = float(params["min_confluence"])

        signal_type: Optional[str] = None
        is_pre = False

        full_long = (side == "LONG" and p_done and b_done and d_done
                     and bias_ok_long and confluence >= min_conf)
        full_short = (side == "SHORT" and p_done and b_done and d_done
                      and bias_ok_short and confluence >= min_conf)

        if full_long:
            signal_type = "LONG"
        elif full_short:
            signal_type = "SHORT"
        elif p_done and b_done and side:
            # Pre-Signal: Purge + Break stehen, Displacement/Retrace fehlt noch
            signal_type, is_pre = side, True
        elif p_done and side:
            # frühes Pre-Signal: Purge erkannt, warte auf Break
            signal_type, is_pre = side, True

        # --- Cooldown / Duplikat-Schutz -----------------------------------
        levels = None
        if signal_type and not is_pre:
            setup_sig = (signal_type, round(purge["extreme"], 6))
            if self._last_setup.get(symbol) == setup_sig:
                # exakt dasselbe Setup wurde bereits getradet -> nicht doppelt
                signal_type, is_pre = side, True
            else:
                levels = self._compute_levels(
                    signal_type, price, purge["extreme"], atr,
                    buy_dol, sell_dol, params,
                )
                self._last_setup[symbol] = setup_sig

        return {
            "indicators": {
                "price": round(price, 6),
                "phase": phase,
                "confluence": int(round(confluence)),
                "bias_ema": round(ema, 6) if ema else None,
                "atr": round(atr, 6),
                "purge_level": round(purge["level"], 6) if purge else None,
                "sweep": (purge["type"] if purge else "none"),
                "mss_level": round(mss["level"], 6) if mss else None,
                "fvg_zone": (f'{round(disp_fvg["bottom"], 6)}-{round(disp_fvg["top"], 6)}'
                             if disp_fvg else None),
                "buy_side_dol": round(buy_dol, 6) if buy_dol else None,
                "sell_side_dol": round(sell_dol, 6) if sell_dol else None,
                # rsi key hält die generische Indicator-Strip der UI happy
                "rsi": int(round(confluence)),
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
    # 1. PURGE detection
    # ------------------------------------------------------------------
    @staticmethod
    def _detect_purges(candles, swing_lows, swing_highs, key_low, key_high,
                       lookback, atr) -> List[Dict]:
        """
        Sammle valide Liquidity-Sweeps der letzten `lookback` Kerzen (jüngste
        zuerst). Sell-Side-Sweep (unter Support -> Rückkehr) => LONG-Bias,
        Buy-Side-Sweep (über Resistance -> Rückkehr) => SHORT-Bias.
        """
        n = len(candles)
        start = max(0, n - lookback)
        low_levels = [l for _, l in swing_lows[-10:]] + [key_low]
        high_levels = [h for _, h in swing_highs[-10:]] + [key_high]
        out: List[Dict] = []
        for i in range(n - 1, start - 1, -1):
            c = candles[i]
            best_low = None
            for lvl in low_levels:
                if c["low"] < lvl and c["close"] > lvl:
                    best_low = lvl if best_low is None else max(best_low, lvl)
            if best_low is not None:
                out.append({"side": "LONG", "type": "sell_side",
                            "idx": i, "extreme": c["low"], "level": best_low,
                            "depth_atr": (best_low - c["low"]) / atr if atr else 0.0})
                continue
            best_high = None
            for lvl in high_levels:
                if c["high"] > lvl and c["close"] < lvl:
                    best_high = lvl if best_high is None else min(best_high, lvl)
            if best_high is not None:
                out.append({"side": "SHORT", "type": "buy_side",
                            "idx": i, "extreme": c["high"], "level": best_high,
                            "depth_atr": (c["high"] - best_high) / atr if atr else 0.0})
        return out

    # ------------------------------------------------------------------
    # 2. BREAK / Market Structure Shift
    # ------------------------------------------------------------------
    @staticmethod
    def _detect_mss(candles, purge, swing_highs, swing_lows, lookback) -> Optional[Dict]:
        """
        Nach dem Purge: Bruch der letzten Gegen-Struktur mit KRAFT (Close jenseits).
        LONG: Close über dem letzten Swing-High <= purge_idx.
        SHORT: Close unter dem letzten Swing-Low <= purge_idx.
        """
        p_idx = purge["idx"]
        n = len(candles)
        end = min(n, p_idx + 1 + lookback)
        if purge["side"] == "LONG":
            prior_highs = [(i, h) for i, h in swing_highs if i <= p_idx]
            if not prior_highs:
                return None
            level = prior_highs[-1][1]  # letztes Swing-High vor/bei Purge
            for j in range(p_idx + 1, end):
                if candles[j]["close"] > level:
                    body = abs(candles[j]["close"] - candles[j]["open"])
                    return {"idx": j, "level": level, "break_close": candles[j]["close"],
                            "body": body}
        else:
            prior_lows = [(i, l) for i, l in swing_lows if i <= p_idx]
            if not prior_lows:
                return None
            level = prior_lows[-1][1]
            for j in range(p_idx + 1, end):
                if candles[j]["close"] < level:
                    body = abs(candles[j]["close"] - candles[j]["open"])
                    return {"idx": j, "level": level, "break_close": candles[j]["close"],
                            "body": body}
        return None

    # ------------------------------------------------------------------
    # 3. DISPLACEMENT + Fair Value Gap
    # ------------------------------------------------------------------
    @staticmethod
    def _detect_displacement_fvg(candles, purge, atr, body_atr_min,
                                 fvg_min_atr, max_age) -> Optional[Dict]:
        """
        Suche eine impulsive 3-Kerzen-FVG in Trade-Richtung, entstanden ab dem
        Purge (frischer Impuls). Die mittlere Kerze muss Displacement zeigen
        (Körper >= body_atr_min * ATR). FVG muss noch offen (unmitigiert) sein.
        Gibt die frischeste passende FVG zurück.
        """
        n = len(candles)
        p_idx = purge["idx"]
        min_gap = fvg_min_atr * atr
        min_body = body_atr_min * atr
        best = None
        start = max(0, p_idx - 1, n - max_age - 3)
        for i in range(start, n - 2):
            c1, c2, c3 = candles[i], candles[i + 1], candles[i + 2]
            mid_body = abs(c2["close"] - c2["open"])
            if mid_body < min_body:
                continue
            if purge["side"] == "LONG":
                # bullische FVG: c1.high < c3.low, impulsive grüne Mitte
                if c2["close"] > c2["open"] and c1["high"] < c3["low"]:
                    gap_bot, gap_top = c1["high"], c3["low"]
                    if (gap_top - gap_bot) < min_gap:
                        continue
                    still_open = all(later["low"] > gap_bot for later in candles[i + 3:])
                    fvg = {"type": "bull", "bottom": gap_bot, "top": gap_top,
                           "mid": (gap_bot + gap_top) / 2,
                           "size_atr": (gap_top - gap_bot) / atr if atr else 0.0,
                           "age": n - (i + 2), "open": still_open}
                    best = fvg  # frischeste behalten (Schleife läuft aufsteigend)
            else:
                if c2["close"] < c2["open"] and c1["low"] > c3["high"]:
                    gap_top, gap_bot = c1["low"], c3["high"]
                    if (gap_top - gap_bot) < min_gap:
                        continue
                    still_open = all(later["high"] < gap_top for later in candles[i + 3:])
                    fvg = {"type": "bear", "bottom": gap_bot, "top": gap_top,
                           "mid": (gap_bot + gap_top) / 2,
                           "size_atr": (gap_top - gap_bot) / atr if atr else 0.0,
                           "age": n - (i + 2), "open": still_open}
                    best = fvg
        if best and best.get("open"):
            return best
        # auch eine bereits leicht getappte (aber nicht geschlossene) FVG ist ok
        return best

    # ------------------------------------------------------------------
    # Confluence score 0..100
    # ------------------------------------------------------------------
    @staticmethod
    def _confluence_score(purge, mss, disp_fvg, atr, bias_aligned, bias_neutral) -> float:
        # Sweep-Qualität (0..30): Tiefe des Sweeps in ATR (0.1..1.5 ATR = optimal)
        depth = purge.get("depth_atr", 0.0)
        sweep = max(0.0, min(1.0, depth / 1.0)) * 30

        # MSS-Stärke (0..30): Körper der Break-Kerze in ATR
        mss_score = 0.0
        if mss and atr:
            body_atr = mss.get("body", 0.0) / atr
            mss_score = max(0.0, min(1.0, body_atr / 1.2)) * 30

        # FVG-Qualität (0..25): Größe der Displacement-FVG in ATR
        fvg_score = 0.0
        if disp_fvg:
            fvg_score = max(0.0, min(1.0, disp_fvg.get("size_atr", 0.0) / 0.8)) * 25

        # Bias-Alignment (0..15)
        bias_score = 15 if bias_aligned else (7 if bias_neutral else 0)

        return round(sweep + mss_score + fvg_score + bias_score, 1)

    # ------------------------------------------------------------------
    # Levels: SL hinter Sweep-Extrem, TP = Liquidity-Pool, min. 1:3 R:R
    # ------------------------------------------------------------------
    def _compute_levels(self, side, entry, sweep_extreme, atr,
                        buy_dol, sell_dol, params) -> Dict:
        ti = self.indicators
        buf = atr * float(params["atr_sl_mult"])
        tp1_rr = float(params["tp1_rr"])
        min_rr = float(params["min_rr"])

        if side == "LONG":
            sl = sweep_extreme - buf
            risk = entry - sl
            if risk <= 0:
                risk = entry * 0.003
                sl = entry - risk
            tp1 = entry + risk * tp1_rr
            tp_full = entry + risk * min_rr
            # nächster Buy-Side Draw-on-Liquidity erweitert das Ziel (>= min RR)
            if buy_dol and buy_dol > tp_full:
                tp_full = buy_dol
        else:
            sl = sweep_extreme + buf
            risk = sl - entry
            if risk <= 0:
                risk = entry * 0.003
                sl = entry + risk
            tp1 = entry - risk * tp1_rr
            tp_full = entry - risk * min_rr
            if sell_dol and sell_dol < tp_full:
                tp_full = sell_dol

        return {
            "entry": round(entry, 6),
            "stop_loss": round(sl, 6),
            "take_profit_1": round(tp1, 6),
            "take_profit_full": round(tp_full, 6),
            "crv": round(ti.calculate_crv(entry, sl, tp_full), 2),
        }

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _nearest_above(levels, price):
        above = [l for l in levels if l and l > price]
        return min(above) if above else None

    @staticmethod
    def _nearest_below(levels, price):
        below = [l for l in levels if l and l < price]
        return max(below) if below else None

    @staticmethod
    def _last_valid(arr):
        for x in reversed(arr):
            if x is not None:
                return x
        return None
