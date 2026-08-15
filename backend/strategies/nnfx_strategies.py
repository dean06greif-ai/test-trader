"""NNFX-Framework: drei Strategien für die drei NNFX-Regime.

Idee (No Nonsense Forex, an Krypto angepasst): Man hat nicht EINE Strategie,
sondern drei, die abhängig vom Marktregime umgeschaltet werden:

    Regime (aus services.regime_engine)      Strategie
    Trend      (Auf-/Abwärtstrend)           NNFX Trend      -> nnfx_trend
    Seitwärts  (Range, niedrige/mittl. Vola) NNFX Mean-Rev.  -> nnfx_reversion
    Volatilität(Seitwärts + hohe Vola)       NNFX Breakout   -> nnfx_breakout

Aufbau jeder Strategie wie im NNFX-Algorithmus:
  Baseline (EMA)  +  Bestätigung 1 (ADX/DI)  +  Bestätigung 2 (MACD/CCI/RSI)
  +  Volatilitäts-/Volumen-Filter  +  ATR-basierte Stops.

Alles ist über Parameter einstellbar (Perioden, Schwellen, Filter an/aus), damit
im Regime-Lab pro Regime optimiert werden kann. Die Signalberechnung liegt
EINMAL in `_series()` und wird sowohl für die Live-Analyse (letzte Kerze) als
auch für den vektorisierten Backtest-Pfad genutzt – kein doppelter Code.
"""
from typing import Dict, List, Optional

import numpy as np

from strategies.base_strategy import BaseStrategy

COMMON_PARAMS = {
    "baseline_period": {"value": 50, "min": 10, "max": 300, "step": 1,
                        "label": "Baseline EMA", "description": "NNFX-Baseline: "
                        "Preis über/unter dieser EMA bestimmt die Richtung"},
    "adx_period": {"value": 14, "min": 5, "max": 60, "step": 1,
                   "label": "ADX Periode", "description": "Periode für ADX/DI"},
    "atr_period": {"value": 14, "min": 5, "max": 60, "step": 1,
                   "label": "ATR Periode", "description": "Volatilität für Stops"},
    "atr_sl_mult": {"value": 1.5, "min": 0.3, "max": 5.0, "step": 0.1,
                    "label": "ATR Stop", "description": "Stop-Abstand in ATR"},
    "tp1_rr": {"value": 1.0, "min": 0.3, "max": 5.0, "step": 0.1,
               "label": "TP1 (R)", "description": "Erstes Ziel als Vielfaches des Risikos"},
    "tp_rr": {"value": 2.0, "min": 0.5, "max": 10.0, "step": 0.1,
              "label": "TP voll (R)", "description": "Endziel als Vielfaches des Risikos"},
    "rel_vol_min": {"value": 0.0, "min": 0.0, "max": 5.0, "step": 0.1,
                    "label": "Min. rel. Volumen", "description": "0 = Filter aus"},
    "allow_long": {"value": 1, "min": 0, "max": 1, "step": 1,
                   "label": "LONG erlaubt", "description": "1 = Long-Signale zulassen"},
    "allow_short": {"value": 1, "min": 0, "max": 1, "step": 1,
                    "label": "SHORT erlaubt", "description": "1 = Short-Signale zulassen"},
}


def _f(params: Dict, key: str, default: float) -> float:
    try:
        v = params.get(key, default)
        return float(default if v is None else v)
    except (TypeError, ValueError):
        return float(default)


def _i(params: Dict, key: str, default: int) -> int:
    return int(_f(params, key, default))


class NNFXBase(BaseStrategy):
    """Gemeinsame Basis: Serien berechnen, Regeln auswerten, Levels bauen."""
    NNFX_REGIME = "trend"
    STRATEGY_TIMEFRAME = "1h"

    # ---- von den Unterklassen implementiert ----
    def _series(self, fs, params: Dict) -> Dict:
        """{"long": bool[], "short": bool[], "rules": [(id,label,desc,long[],short[])],
        "warmup": int, "extra": {name: array}}"""
        raise NotImplementedError

    # ---- gemeinsame Bausteine ----
    @staticmethod
    def _base_filters(fs, params: Dict):
        """Volumen-/Richtungsfilter, die für alle drei NNFX-Varianten gelten."""
        n = fs.n
        rel_vol = fs.get("rel_volume", {"volume_sma_period": 20})
        vmin = _f(params, "rel_vol_min", 0.0)
        vol_ok = (np.nan_to_num(rel_vol, nan=1.0) >= vmin) if vmin > 0 \
            else np.ones(n, dtype=bool)
        long_allowed = bool(_i(params, "allow_long", 1))
        short_allowed = bool(_i(params, "allow_short", 1))
        return vol_ok, long_allowed, short_allowed, rel_vol

    def vectorized_signals(self, fs, params: Dict) -> Optional[Dict]:
        s = self._series(fs, params)
        warm = min(int(s["warmup"]), fs.n)
        long_ok = np.asarray(s["long"], dtype=bool).copy()
        short_ok = np.asarray(s["short"], dtype=bool).copy()
        long_ok[:warm] = False
        short_ok[:warm] = False
        return {"long": long_ok, "short": short_ok, "warmup": warm,
                "rules_total": len(s["rules"]),
                "rsi": s.get("extra", {}).get("rsi")}

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
            risk = max(atr_v * _f(params, "atr_sl_mult", 1.5), price * 0.0005)
            tp1, tpf = _f(params, "tp1_rr", 1.0), _f(params, "tp_rr", 2.0)
            if signal_type == "LONG":
                levels = self._lv(price, price - risk, price + risk * tp1,
                                  price + risk * tpf)
            else:
                levels = self._lv(price, price + risk, price - risk * tp1,
                                  price - risk * tpf)
        extra = {k: (round(float(v[i]), 4) if v is not None
                     and np.isfinite(v[i]) else None)
                 for k, v in (s.get("extra") or {}).items() if v is not None}
        return {"indicators": {"price": round(price, 6), "atr": round(atr_v, 6),
                               "nnfx_regime": self.NNFX_REGIME, **extra},
                "rules": rules, "bias": bias,
                "long_count": long_count, "short_count": short_count,
                "rules_total": len(rules),
                "signal_type": signal_type, "is_pre_signal": False,
                "levels": levels}

    def _lv(self, entry, sl, tp1, tpf):
        return {"entry": round(entry, 6), "stop_loss": round(sl, 6),
                "take_profit_1": round(tp1, 6), "take_profit_full": round(tpf, 6),
                "crv": round(self.indicators.calculate_crv(entry, sl, tpf), 2)}

    def get_metadata(self) -> Dict:
        return {**super().get_metadata(), "nnfx_regime": self.NNFX_REGIME,
                "framework": "nnfx"}


# --------------------------------------------------------------- Trend
class NNFXTrendStrategy(NNFXBase):
    STRATEGY_ID = "nnfx_trend"
    STRATEGY_NAME = "NNFX Trend"
    STRATEGY_DESCRIPTION = ("NNFX für Trend-Regime: Baseline-EMA + ADX/DI-Bestätigung "
                            "+ MACD-Momentum. Handelt MIT dem Trend, ATR-Stops.")
    NNFX_REGIME = "trend"
    DEFAULT_PARAMS = {
        **COMMON_PARAMS,
        "adx_min": {"value": 20, "min": 5, "max": 50, "step": 1,
                    "label": "ADX Minimum", "description": "Trendstärke-Filter"},
        "macd_fast": {"value": 12, "min": 3, "max": 40, "step": 1,
                      "label": "MACD schnell", "description": "MACD Fast-EMA"},
        "macd_slow": {"value": 26, "min": 10, "max": 100, "step": 1,
                      "label": "MACD langsam", "description": "MACD Slow-EMA"},
        "macd_signal_period": {"value": 9, "min": 2, "max": 30, "step": 1,
                               "label": "MACD Signal", "description": "MACD-Signallinie"},
        "require_macd": {"value": 1, "min": 0, "max": 1, "step": 1,
                         "label": "MACD-Bestätigung", "description": "1 = MACD muss passen"},
        "pullback_atr": {"value": 0.0, "min": 0.0, "max": 3.0, "step": 0.1,
                         "label": "Max. Abstand zur Baseline (ATR)",
                         "description": "0 = aus; sonst nur Einstiege nahe der Baseline"},
    }

    def _series(self, fs, params: Dict) -> Dict:
        n = fs.n
        base_p = _i(params, "baseline_period", 50)
        adx_p = _i(params, "adx_period", 14)
        atr_p = _i(params, "atr_period", 14)
        d = {"ema_slow_period": base_p, "adx_period": adx_p, "atr_period": atr_p,
             "macd_fast": _i(params, "macd_fast", 12),
             "macd_slow": _i(params, "macd_slow", 26),
             "macd_signal_period": _i(params, "macd_signal_period", 9)}
        base = fs.get("ema_slow", d)
        adx = fs.get("adx", d)
        pdi = fs.get("plus_di", d)
        mdi = fs.get("minus_di", d)
        hist = fs.get("macd_hist", d)
        atr = fs.get("atr", d)
        close = fs.close
        vol_ok, long_allowed, short_allowed, rel_vol = self._base_filters(fs, params)

        above = close > base
        below = close < base
        adx_ok = np.nan_to_num(adx, nan=0.0) >= _f(params, "adx_min", 20)
        di_long = np.nan_to_num(pdi, nan=0.0) > np.nan_to_num(mdi, nan=0.0)
        di_short = ~di_long
        if _i(params, "require_macd", 1):
            macd_long = np.nan_to_num(hist, nan=0.0) > 0
            macd_short = np.nan_to_num(hist, nan=0.0) < 0
        else:
            macd_long = macd_short = np.ones(n, dtype=bool)
        pb = _f(params, "pullback_atr", 0.0)
        if pb > 0:
            dist = np.abs(close - base) / np.maximum(np.nan_to_num(atr, nan=np.inf), 1e-9)
            near = dist <= pb
        else:
            near = np.ones(n, dtype=bool)

        rules = [("baseline", "Preis über/unter Baseline",
                  f"EMA{base_p} als NNFX-Baseline", above, below),
                 ("adx", "Trendstärke (ADX)",
                  f"ADX ≥ {_f(params, 'adx_min', 20):g}", adx_ok, adx_ok),
                 ("di", "Richtung (DI+/DI-)", "DI+ > DI- für Long", di_long, di_short),
                 ("macd", "Momentum (MACD)", "MACD-Histogramm bestätigt",
                  macd_long, macd_short),
                 ("near", "Abstand zur Baseline", "Einstieg nicht überdehnt",
                  near, near)]
        long_ok = above & adx_ok & di_long & macd_long & near & vol_ok
        short_ok = below & adx_ok & di_short & macd_short & near & vol_ok
        if not long_allowed:
            long_ok = np.zeros(n, dtype=bool)
        if not short_allowed:
            short_ok = np.zeros(n, dtype=bool)
        warmup = max(base_p, adx_p * 3, d["macd_slow"] + d["macd_signal_period"]) + 5
        return {"long": long_ok, "short": short_ok, "rules": rules, "warmup": warmup,
                "extra": {"adx": adx, "plus_di": pdi, "minus_di": mdi,
                          "baseline": base, "rel_volume": rel_vol}}


# --------------------------------------------------------------- Mean Reversion
class NNFXReversionStrategy(NNFXBase):
    STRATEGY_ID = "nnfx_reversion"
    STRATEGY_NAME = "NNFX Mean-Reversion"
    STRATEGY_DESCRIPTION = ("NNFX für Seitwärts-Regime: Kauf am unteren, Verkauf am "
                            "oberen Bollinger-/Keltner-Rand, nur bei niedrigem ADX "
                            "(kein Trend), RSI-Bestätigung.")
    NNFX_REGIME = "range"
    DEFAULT_PARAMS = {
        **COMMON_PARAMS,
        "adx_max": {"value": 25, "min": 8, "max": 45, "step": 1,
                    "label": "ADX Maximum", "description": "Nur handeln wenn KEIN Trend"},
        "band": {"value": 0, "min": 0, "max": 1, "step": 1,
                 "label": "Band (0=Bollinger, 1=Keltner)",
                 "description": "Welches Band als Range-Rand dient"},
        "bb_period": {"value": 20, "min": 5, "max": 100, "step": 1,
                      "label": "Band Periode", "description": "Periode des Bandes"},
        "bb_std": {"value": 2.0, "min": 0.5, "max": 4.0, "step": 0.1,
                   "label": "Bollinger Std", "description": "Standardabweichungen"},
        "keltner_mult": {"value": 2.0, "min": 0.5, "max": 5.0, "step": 0.1,
                         "label": "Keltner ATR-Faktor", "description": "Bandbreite in ATR"},
        "rsi_period": {"value": 14, "min": 5, "max": 40, "step": 1,
                       "label": "RSI Periode", "description": "RSI-Bestätigung"},
        "rsi_long": {"value": 38, "min": 10, "max": 50, "step": 1,
                     "label": "RSI Long unter", "description": "Long nur bei RSI darunter"},
        "rsi_short": {"value": 62, "min": 50, "max": 90, "step": 1,
                      "label": "RSI Short über", "description": "Short nur bei RSI darüber"},
        "require_rsi": {"value": 1, "min": 0, "max": 1, "step": 1,
                        "label": "RSI-Bestätigung", "description": "1 = RSI muss passen"},
    }

    def _series(self, fs, params: Dict) -> Dict:
        n = fs.n
        per = _i(params, "bb_period", 20)
        adx_p = _i(params, "adx_period", 14)
        atr_p = _i(params, "atr_period", 14)
        rsi_p = _i(params, "rsi_period", 14)
        d = {"bb_period": per, "bb_std": _f(params, "bb_std", 2.0),
             "keltner_period": per, "keltner_mult": _f(params, "keltner_mult", 2.0),
             "adx_period": adx_p, "atr_period": atr_p, "rsi_period": rsi_p}
        if _i(params, "band", 0) == 1:
            upper = fs.get("keltner_upper", d)
            lower = fs.get("keltner_lower", d)
        else:
            upper = fs.get("bb_upper", d)
            lower = fs.get("bb_lower", d)
        adx = fs.get("adx", d)
        rsi = fs.get("rsi", d)
        close = fs.close
        vol_ok, long_allowed, short_allowed, rel_vol = self._base_filters(fs, params)

        at_low = close <= np.nan_to_num(lower, nan=-np.inf)
        at_high = close >= np.nan_to_num(upper, nan=np.inf)
        no_trend = np.nan_to_num(adx, nan=99.0) <= _f(params, "adx_max", 25)
        if _i(params, "require_rsi", 1):
            rsi_long = np.nan_to_num(rsi, nan=50.0) <= _f(params, "rsi_long", 38)
            rsi_short = np.nan_to_num(rsi, nan=50.0) >= _f(params, "rsi_short", 62)
        else:
            rsi_long = rsi_short = np.ones(n, dtype=bool)

        rules = [("band", "Preis am Range-Rand", "Unteres Band = Long, oberes = Short",
                  at_low, at_high),
                 ("no_trend", "Kein Trend (ADX)",
                  f"ADX ≤ {_f(params, 'adx_max', 25):g}", no_trend, no_trend),
                 ("rsi", "RSI-Extrem", "RSI bestätigt die Gegenbewegung",
                  rsi_long, rsi_short)]
        long_ok = at_low & no_trend & rsi_long & vol_ok
        short_ok = at_high & no_trend & rsi_short & vol_ok
        if not long_allowed:
            long_ok = np.zeros(n, dtype=bool)
        if not short_allowed:
            short_ok = np.zeros(n, dtype=bool)
        warmup = max(per, adx_p * 3, rsi_p) + 5
        return {"long": long_ok, "short": short_ok, "rules": rules, "warmup": warmup,
                "extra": {"rsi": rsi, "adx": adx, "band_upper": upper,
                          "band_lower": lower, "rel_volume": rel_vol}}


# --------------------------------------------------------------- Breakout
class NNFXBreakoutStrategy(NNFXBase):
    STRATEGY_ID = "nnfx_breakout"
    STRATEGY_NAME = "NNFX Breakout"
    STRATEGY_DESCRIPTION = ("NNFX für Volatilitäts-Regime: Donchian-Ausbruch mit "
                            "ATR-Expansion und ADX-Bestätigung – fängt explosive "
                            "Bewegungen aus Ranges ab.")
    NNFX_REGIME = "breakout"
    DEFAULT_PARAMS = {
        **COMMON_PARAMS,
        "donchian_period": {"value": 20, "min": 5, "max": 200, "step": 1,
                            "label": "Donchian Periode",
                            "description": "Fenster für Hoch/Tief-Ausbruch"},
        "adx_min": {"value": 18, "min": 5, "max": 50, "step": 1,
                    "label": "ADX Minimum", "description": "Mindest-Trendstärke"},
        "atr_expand": {"value": 1.15, "min": 1.0, "max": 3.0, "step": 0.05,
                       "label": "ATR-Expansion", "description": "ATR ggü. ATR-Durchschnitt"},
        "atr_ref_period": {"value": 50, "min": 10, "max": 300, "step": 1,
                           "label": "ATR-Referenz", "description": "Vergleichsfenster für ATR"},
        "require_baseline": {"value": 1, "min": 0, "max": 1, "step": 1,
                             "label": "Baseline-Filter",
                             "description": "1 = Ausbruch muss zur Baseline passen"},
    }

    def _series(self, fs, params: Dict) -> Dict:
        import pandas as pd
        n = fs.n
        don_p = _i(params, "donchian_period", 20)
        adx_p = _i(params, "adx_period", 14)
        atr_p = _i(params, "atr_period", 14)
        base_p = _i(params, "baseline_period", 50)
        ref_p = _i(params, "atr_ref_period", 50)
        d = {"donchian_period": don_p, "adx_period": adx_p, "atr_period": atr_p,
             "ema_slow_period": base_p}
        dh = fs.get("donchian_high", d)
        dl = fs.get("donchian_low", d)
        adx = fs.get("adx", d)
        atr = fs.get("atr", d)
        base = fs.get("ema_slow", d)
        close = fs.close
        vol_ok, long_allowed, short_allowed, rel_vol = self._base_filters(fs, params)

        atr_ref = pd.Series(atr).rolling(ref_p, min_periods=max(ref_p // 2, 5)) \
            .mean().to_numpy()
        with np.errstate(invalid="ignore", divide="ignore"):
            expand = np.nan_to_num(atr, nan=0.0) >= _f(params, "atr_expand", 1.15) \
                * np.nan_to_num(atr_ref, nan=np.inf)
        break_up = close > np.nan_to_num(dh, nan=np.inf)
        break_dn = close < np.nan_to_num(dl, nan=-np.inf)
        adx_ok = np.nan_to_num(adx, nan=0.0) >= _f(params, "adx_min", 18)
        if _i(params, "require_baseline", 1):
            base_long = close > np.nan_to_num(base, nan=np.inf)
            base_short = close < np.nan_to_num(base, nan=-np.inf)
        else:
            base_long = base_short = np.ones(n, dtype=bool)

        rules = [("breakout", "Donchian-Ausbruch",
                  f"Schluss über/unter {don_p}-Perioden-Extrem", break_up, break_dn),
                 ("expand", "Volatilitäts-Expansion",
                  "ATR über dem Durchschnitt", expand, expand),
                 ("adx", "Trendstärke (ADX)",
                  f"ADX ≥ {_f(params, 'adx_min', 18):g}", adx_ok, adx_ok),
                 ("baseline", "Baseline-Richtung", "Ausbruch passt zur Baseline",
                  base_long, base_short)]
        long_ok = break_up & expand & adx_ok & base_long & vol_ok
        short_ok = break_dn & expand & adx_ok & base_short & vol_ok
        if not long_allowed:
            long_ok = np.zeros(n, dtype=bool)
        if not short_allowed:
            short_ok = np.zeros(n, dtype=bool)
        warmup = max(don_p, adx_p * 3, base_p, ref_p) + 5
        return {"long": long_ok, "short": short_ok, "rules": rules, "warmup": warmup,
                "extra": {"adx": adx, "donchian_high": dh, "donchian_low": dl,
                          "baseline": base, "rel_volume": rel_vol}}


# Regime-Zuordnung (NNFX-Label aus services.regime_engine -> Strategie-ID)
NNFX_STRATEGY_BY_REGIME = {"trend": NNFXTrendStrategy.STRATEGY_ID,
                           "range": NNFXReversionStrategy.STRATEGY_ID,
                           "breakout": NNFXBreakoutStrategy.STRATEGY_ID}
