"""Trend-Surfer – regelbasierte Preset-Strategie (2h Donchian-Trend-Breakout).

Entstehung (Juni 2026): Aus 9 Experiment-Runden mit dem Plattform-Backtester
auf ECHTEN Binance-1m-Daten (730 Tage, 12 Coins, Walk-Forward mit 4 Folds,
Out-of-Universe-Check auf 4 nie im Tuning benutzten Coins, Fees 0.06%/Seite
immer eingerechnet). Kernbefunde:
  - 1m/5m/15m-Scalping-Regeln verlieren nach Fees fast immer (Fees = 40-60%
    des Trade-Risikos bei engen Stops).
  - Robust ist Trendfolge auf 2h mit WEITEN ATR-Stops (Fees < 10% des Risikos)
    und einem Runner (Trailing) für die großen Trend-Beine.
  - Der wichtigste Schutz gegen Chop-Märkte: Kaufman Efficiency Ratio als
    kurzes Filter (ER20) UND als langes Asset-Regime-Gate (ER120) – Coins in
    zäher Seitwärtsphase werden automatisch pausiert.

Regeln (alle müssen für ein Signal erfüllt sein):
  1. AUSBRUCH   Close über dem Hoch/unter dem Tief der letzten N=48 2h-Kerzen
  2. TREND      EMA50 über/unter EMA200 UND Close über/unter EMA200
  3. STÄRKE     ADX(14) >= 20 und DI-Richtung passt zur Trade-Richtung
  4. EFFIZIENZ  Kaufman ER(20) >= 0.40 (gerichtete, saubere Bewegung)
  5. REGIME     ER(120) >= 0.15 (Asset trendet auf ~10-Tage-Sicht) UND
                Fee-Guard: Trade-Risiko (ATR-Stop) >= 8x Roundtrip-Fee

Empfohlene Trade-Einstellungen (werden beim ersten Start als Strategie-Override
vorbelegt, siehe server.py): SL = 3xATR, TP1 = 1.5R (40% schließen), TP = 5R,
Break-Even ab TP1, danach ATR-Trailing x1.5 – exakt die Konfiguration aus den
Backtests. Validierung 730d/12 Coins inkl. Fees: Gesamt-PnL positiv, 3/4
Walk-Forward-Folds positiv, Chop-Coins durch das Regime-Gate stark gedämpft.
"""
from typing import Dict, List, Optional

import numpy as np

from strategies.base_strategy import BaseStrategy


def _f(params: Dict, key: str, default: float) -> float:
    try:
        v = params.get(key, default)
        return float(default if v is None else v)
    except (TypeError, ValueError):
        return float(default)


def _i(params: Dict, key: str, default: int) -> int:
    return int(_f(params, key, default))


def _efficiency_ratio(close: np.ndarray, n: int) -> np.ndarray:
    """Kaufman Efficiency Ratio: |Netto-Bewegung| / Summe |Einzelbewegungen|."""
    import pandas as pd
    delta = pd.Series(close).diff().abs().rolling(n, min_periods=n).sum().to_numpy()
    move = np.abs(close - np.roll(close, n))
    move[:n] = 0.0
    with np.errstate(invalid="ignore", divide="ignore"):
        er = move / np.maximum(delta, 1e-12)
    return er


class TrendSurferStrategy(BaseStrategy):
    STRATEGY_ID = "trend_surfer"
    STRATEGY_NAME = "Trend-Surfer"
    STRATEGY_DESCRIPTION = (
        "2h Trend-Breakout mit Chop-Schutz: Donchian(48)-Ausbruch nur in Richtung "
        "des EMA50/200-Trends, bestätigt durch ADX und Kaufman-Effizienz (kurz + "
        "lang als Asset-Regime-Gate). Weite ATR-Stops (3x) + Runner (TP 5R, "
        "Trailing) machen die Strategie fee-robust. Auf 730 Tagen / 12 Coins "
        "inkl. Fees backtest-validiert (Walk-Forward)."
    )
    STRATEGY_TIMEFRAME = "2h"

    DEFAULT_PARAMS = {
        "donchian_period": {"value": 48, "min": 12, "max": 120, "step": 1,
                            "label": "Ausbruchs-Range (Kerzen)",
                            "description": "Close über Hoch/unter Tief der letzten N Kerzen = Ausbruch"},
        "ema_fast_period": {"value": 50, "min": 10, "max": 100, "step": 1,
                            "label": "Trend-EMA schnell",
                            "description": "Muss über/unter der langsamen EMA liegen"},
        "ema_slow_period": {"value": 200, "min": 50, "max": 200, "step": 1,
                            "label": "Trend-EMA langsam (Anker)",
                            "description": "Preis & schnelle EMA müssen auf der Trendseite liegen"},
        "adx_period": {"value": 14, "min": 5, "max": 40, "step": 1,
                       "label": "ADX Periode", "description": "Periode für ADX/DI"},
        "adx_min": {"value": 20, "min": 0, "max": 50, "step": 1,
                    "label": "ADX Minimum",
                    "description": "Trendstärke-Filter; DI-Richtung muss zusätzlich passen"},
        "er_period": {"value": 20, "min": 5, "max": 60, "step": 1,
                      "label": "Effizienz Periode (kurz)",
                      "description": "Kaufman Efficiency Ratio über N Kerzen"},
        "er_min": {"value": 0.4, "min": 0.0, "max": 1.0, "step": 0.05,
                   "label": "Effizienz Minimum (kurz)",
                   "description": "0 = aus; höher = nur sehr saubere Bewegungen"},
        "gate_er_period": {"value": 120, "min": 40, "max": 200, "step": 5,
                           "label": "Regime-Gate Periode",
                           "description": "Langes ER-Fenster (~10 Tage bei 2h) gegen Chop-Coins"},
        "gate_er_min": {"value": 0.15, "min": 0.0, "max": 0.6, "step": 0.01,
                        "label": "Regime-Gate Minimum",
                        "description": "0 = aus; Coin pausiert automatisch in zähen Seitwärtsphasen"},
        "atr_period": {"value": 14, "min": 5, "max": 60, "step": 1,
                       "label": "ATR Periode", "description": "Volatilität für Stops & Fee-Guard"},
        "atr_sl_mult": {"value": 3.0, "min": 0.5, "max": 6.0, "step": 0.1,
                        "label": "ATR Stop",
                        "description": "Weiter Stop (3x ATR) hält Fees klein relativ zum Risiko"},
        "tp1_rr": {"value": 1.5, "min": 0.3, "max": 5.0, "step": 0.1,
                   "label": "TP1 (R)", "description": "Erstes Ziel als Vielfaches des Risikos"},
        "tp_rr": {"value": 5.0, "min": 1.0, "max": 15.0, "step": 0.5,
                  "label": "TP voll (R)",
                  "description": "Weites Endziel – der Runner zahlt die Trendfolge"},
        "fee_guard_mult": {"value": 8.0, "min": 0.0, "max": 30.0, "step": 0.5,
                           "label": "Fee-Guard (x Roundtrip)",
                           "description": "Trade-Risiko muss >= X mal die Roundtrip-Fee (0.12%) sein; 0 = aus"},
        "allow_long": {"value": 1, "min": 0, "max": 1, "step": 1,
                       "label": "LONG erlaubt", "description": "1 = Long-Signale zulassen"},
        "allow_short": {"value": 1, "min": 0, "max": 1, "step": 1,
                        "label": "SHORT erlaubt", "description": "1 = Short-Signale zulassen"},
    }

    # ---------------- Kernberechnung (live + vektorisierter Backtest) --------
    def _series(self, fs, params: Dict) -> Dict:
        n = fs.n
        don_p = _i(params, "donchian_period", 48)
        ema_f_p = _i(params, "ema_fast_period", 50)
        ema_s_p = _i(params, "ema_slow_period", 200)
        adx_p = _i(params, "adx_period", 14)
        atr_p = _i(params, "atr_period", 14)
        d = {"ema_fast_period": ema_f_p, "ema_slow_period": ema_s_p,
             "adx_period": adx_p, "atr_period": atr_p, "donchian_period": don_p}

        close = fs.close
        ef = fs.get("ema_fast", d)
        es = fs.get("ema_slow", d)
        atr = fs.get("atr", d)
        adx = fs.get("adx", d)
        pdi = fs.get("plus_di", d)
        mdi = fs.get("minus_di", d)
        hh = fs.get("donchian_high", d)   # Hoch der letzten N Kerzen (shift 1)
        ll = fs.get("donchian_low", d)

        er_p = _i(params, "er_period", 20)
        gate_p = _i(params, "gate_er_period", 120)
        er = _efficiency_ratio(close, er_p)
        gate_er = _efficiency_ratio(close, gate_p)

        with np.errstate(invalid="ignore", divide="ignore"):
            brk_l = close > np.nan_to_num(hh, nan=np.inf)
            brk_s = close < np.nan_to_num(ll, nan=-np.inf)

            trend_l = (ef > es) & (close > es)
            trend_s = (ef < es) & (close < es)

            adx_min = _f(params, "adx_min", 20)
            if adx_min > 0:
                adx_v = np.nan_to_num(adx, nan=0.0)
                di_l = np.nan_to_num(pdi, nan=0.0) > np.nan_to_num(mdi, nan=0.0)
                str_l = (adx_v >= adx_min) & di_l
                str_s = (adx_v >= adx_min) & ~di_l
            else:
                str_l = str_s = np.ones(n, dtype=bool)

            er_min = _f(params, "er_min", 0.4)
            eff = (np.nan_to_num(er, nan=0.0) >= er_min) if er_min > 0 \
                else np.ones(n, dtype=bool)

            gate_min = _f(params, "gate_er_min", 0.15)
            gate = (np.nan_to_num(gate_er, nan=0.0) >= gate_min) if gate_min > 0 \
                else np.ones(n, dtype=bool)
            fee_mult = _f(params, "fee_guard_mult", 8.0)
            if fee_mult > 0:
                risk_pct = np.nan_to_num(atr, nan=0.0) / np.maximum(close, 1e-12) \
                    * 100 * _f(params, "atr_sl_mult", 3.0)
                gate = gate & (risk_pct >= fee_mult * 0.12)

        allow_l = bool(_i(params, "allow_long", 1))
        allow_s = bool(_i(params, "allow_short", 1))
        long_ok = brk_l & trend_l & str_l & eff & gate
        short_ok = brk_s & trend_s & str_s & eff & gate
        if not allow_l:
            long_ok = np.zeros(n, dtype=bool)
        if not allow_s:
            short_ok = np.zeros(n, dtype=bool)

        rules = [
            ("breakout", "Ausbruch", f"Close über Hoch / unter Tief der letzten {don_p} Kerzen",
             brk_l, brk_s),
            ("trend", "Trend", f"EMA{ema_f_p} & Close auf der Seite der EMA{ema_s_p}",
             trend_l, trend_s),
            ("strength", "Trendstärke", f"ADX({adx_p}) >= {adx_min:g} mit passender DI-Richtung",
             str_l, str_s),
            ("efficiency", "Effizienz", f"Kaufman ER({er_p}) >= {er_min:g}", eff, eff),
            ("regime", "Regime-Gate", f"ER({gate_p}) >= {gate_min:g} + Fee-Guard", gate, gate),
        ]
        warmup = min(max(ema_s_p, gate_p, don_p + 1, adx_p * 2) + 5, n)
        return {"long": long_ok, "short": short_ok, "rules": rules,
                "warmup": warmup,
                "extra": {"er_short": er, "er_gate": gate_er, "adx": adx}}

    def vectorized_signals(self, fs, params: Dict) -> Optional[Dict]:
        s = self._series(fs, params)
        warm = min(int(s["warmup"]), fs.n)
        long_ok = np.asarray(s["long"], dtype=bool).copy()
        short_ok = np.asarray(s["short"], dtype=bool).copy()
        long_ok[:warm] = False
        short_ok[:warm] = False
        return {"long": long_ok, "short": short_ok, "warmup": warm,
                "rules_total": 5, "rsi": None}

    def analyze(self, candles: List[Dict], symbol: str, params: Dict) -> Optional[Dict]:
        from services.fast_sim import FastSeries
        if not candles or len(candles) < 60:
            return None
        fs = FastSeries(candles)
        s = self._series(fs, params)
        i = fs.n - 1
        if i < s["warmup"]:
            return None
        rules = [{"id": rid, "label": lbl, "description": desc,
                  "long": bool(la[i]), "short": bool(sa[i])}
                 for (rid, lbl, desc, la, sa) in s["rules"]]
        long_ok = bool(s["long"][i])
        short_ok = bool(s["short"][i])
        signal_type = "LONG" if long_ok else ("SHORT" if short_ok else None)
        long_count = sum(1 for r in rules if r["long"])
        short_count = sum(1 for r in rules if r["short"])
        bias = ("LONG" if long_count > short_count else
                ("SHORT" if short_count > long_count else None))
        price = float(fs.close[i])
        atr = fs.get("atr", {"atr_period": _i(params, "atr_period", 14)})
        atr_v = float(np.nan_to_num(atr[i], nan=price * 0.005)) or price * 0.005
        levels = None
        if signal_type:
            risk = max(atr_v * _f(params, "atr_sl_mult", 3.0), price * 0.0005)
            tp1, tpf = _f(params, "tp1_rr", 1.5), _f(params, "tp_rr", 5.0)
            if signal_type == "LONG":
                levels = self._lv(price, price - risk, price + risk * tp1,
                                  price + risk * tpf)
            else:
                levels = self._lv(price, price + risk, price - risk * tp1,
                                  price - risk * tpf)
        extra = {k: (round(float(v[i]), 4) if v is not None and np.isfinite(v[i])
                     else None)
                 for k, v in (s.get("extra") or {}).items() if v is not None}
        return {"indicators": {"price": round(price, 6), "atr": round(atr_v, 6),
                               **extra},
                "rules": rules, "bias": bias,
                "long_count": long_count, "short_count": short_count,
                "rules_total": len(rules),
                "signal_type": signal_type, "is_pre_signal": False,
                "levels": levels}

    def _lv(self, entry, sl, tp1, tpf):
        return {"entry": round(entry, 6), "stop_loss": round(sl, 6),
                "take_profit_1": round(tp1, 6), "take_profit_full": round(tpf, 6),
                "crv": round(self.indicators.calculate_crv(entry, sl, tpf), 2)}
