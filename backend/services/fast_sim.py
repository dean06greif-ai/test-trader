"""
Schneller Simulations-Pfad für Custom-Strategien:
Alle Indikator-Serien werden EINMAL über die gesamte Historie berechnet
(statt pro Kerze über ein 260er-Fenster). Die Regel-Auswertung ist danach
ein reiner Array-Vergleich. Ergebnis: identische Regel-Logik, 50-100x schneller.
"""
import numpy as np
import pandas as pd
from typing import Callable, Dict, List, Optional

from services import gpu_accel, indicator_cache, vec
from services.candles import CandleArray
from services.technical_indicators import TechnicalIndicators as TI
from services.timeframes import TIMEFRAMES, aggregate_candles, normalize_rule_tf
from strategies import rule_engine
from strategies.custom_strategy import INDICATORS

VWAP_WINDOW = 261  # entspricht dem 260er-Fenster (+aktuelle Kerze) des Referenzpfads


def _nan(arr) -> np.ndarray:
    return np.array([np.nan if v is None else v for v in arr], dtype=float)


class FastSeries:
    """Lazy-berechnete Indikator-Serien über die volle Kerzen-Historie."""

    def __init__(self, candles):
        self.candles = candles
        self.n = len(candles)
        if isinstance(candles, CandleArray):
            # Spaltenbasiert: keine Millionen Dict-Zugriffe, direkte numpy-Views
            self.open, self.close = candles.op, candles.cl
            self.high, self.low, self.vol = candles.hi, candles.lo, candles.vol
            self.ts = candles.ts
        else:
            self.open = np.array([c["open"] for c in candles], dtype=float)
            self.close = np.array([c["close"] for c in candles], dtype=float)
            self.high = np.array([c["high"] for c in candles], dtype=float)
            self.low = np.array([c["low"] for c in candles], dtype=float)
            self.vol = np.array([c.get("volume", 0) or 0 for c in candles], dtype=float)
            self.ts = np.array([c.get("timestamp", 0) or 0 for c in candles], dtype=np.int64)
        self._cache: Dict[tuple, np.ndarray] = {}
        self._fp: Optional[str] = None
        self._htf: Dict[str, tuple] = {}
        self._base_ms: Optional[int] = None

    def _fingerprint(self) -> str:
        if self._fp is None:
            self._fp = indicator_cache.candles_fingerprint(self.candles)
        return self._fp

    def base_tf_ms(self) -> int:
        """Basis-Timeframe (ms) aus den Timestamps ableiten (kleinster Abstand)."""
        if self._base_ms is None:
            if self.n >= 2:
                d = np.diff(self.ts)
                d = d[d > 0]
                self._base_ms = int(d.min()) if len(d) else 60000
            else:
                self._base_ms = 60000
        return self._base_ms

    def htf(self, tf: str):
        """Höherer Timeframe für Regel-Overrides: (FastSeries des TF, Mapping).

        Mapping[i] = Index der letzten GESCHLOSSENEN TF-Kerze zum Zeitpunkt der
        Basis-Kerze i (-1 = noch keine geschlossen). Ohne Lookahead: identische
        Semantik für Live-Scanner und Backtester."""
        if tf in self._htf:
            return self._htf[tf]
        tf_ms = TIMEFRAMES.get(tf, 1) * 60000
        base_ms = self.base_tf_ms()
        agg = aggregate_candles(self.candles, tf, drop_partial=True, base_ms=base_ms)
        sub = FastSeries(agg)
        base_close = self.ts + base_ms
        htf_close = sub.ts + tf_ms
        map_idx = np.searchsorted(htf_close, base_close, side="right") - 1
        self._htf[tf] = (sub, map_idx)
        return self._htf[tf]

    def get(self, name: str, d: Dict) -> np.ndarray:
        def p(key, default):
            try:
                return int(d.get(key, default) or default)
            except (TypeError, ValueError):
                return default

        def pf(key, default):
            try:
                return float(d.get(key, default) or default)
            except (TypeError, ValueError):
                return default

        if name == "price":
            return self.close
        if name == "volume":
            return self.vol

        dyn = rule_engine.parse_dynamic(name) if name not in INDICATORS else None
        if name == "hour":
            key = ("hour",)
        elif dyn:
            # Dynamische Indikatoren mit expliziter Periode: ema(200), rsi(7), ...
            key = dyn
        elif name == "rsi":
            key = ("rsi", p("rsi_period", 14))
        elif name == "ema_fast":
            key = ("ema", p("ema_fast_period", 9))
        elif name == "ema_slow":
            key = ("ema", p("ema_slow_period", 50))
        elif name == "ema_gap_pct":
            key = ("ema_gap_pct", p("ema_fast_period", 9), p("ema_slow_period", 50))
        elif name == "sma":
            key = ("sma", p("sma_period", 20))
        elif name == "ha_color":
            key = ("ha_color",)
        elif name in ("macd", "macd_signal", "macd_hist"):
            key = (name, p("macd_fast", 12), p("macd_slow", 26), p("macd_signal_period", 9))
        elif name in ("bb_upper", "bb_middle", "bb_lower", "bb_width_pct"):
            key = (name, p("bb_period", 20), pf("bb_std", 2.0))
        elif name == "atr":
            key = ("atr", p("atr_period", 14))
        elif name in ("adx", "plus_di", "minus_di"):
            key = (name, p("adx_period", 14))
        elif name == "cci":
            key = ("cci", p("cci_period", 20))
        elif name in ("keltner_upper", "keltner_middle", "keltner_lower"):
            key = (name, p("keltner_period", 20), p("atr_period", 14),
                   pf("keltner_mult", 2.0))
        elif name in ("donchian_high", "donchian_low"):
            key = (name, p("donchian_period", 20))
        elif name == "atr_pct":
            key = ("atr_pct", p("atr_period", 14))
        elif name == "vwap":
            key = ("vwap",)
        elif name in ("stoch_k", "stoch_d"):
            key = (name, p("stoch_k_period", 14), p("stoch_d_period", 3))
        elif name == "volume_sma":
            key = ("volume_sma", p("volume_sma_period", 20))
        elif name == "rel_volume":
            key = ("rel_volume", p("volume_sma_period", 20))
        elif name == "price_change_pct":
            key = ("price_change_pct", p("change_lookback", 5))
        elif name == "recent_high":
            key = ("recent_high", p("swing_lookback", 10))
        elif name == "recent_low":
            key = ("recent_low", p("swing_lookback", 10))
        # --- Etappe 5: Struktur / Liquidität / Kanal / Events -------------
        elif name == "market_structure":
            key = ("market_structure", p("struct_pivot_wing", 3))
        elif name in ("bos_up", "bos_dn"):
            key = (name, p("struct_pivot_wing", 3), p("bos_window", 10))
        elif name in ("dist_support_pct", "dist_resistance_pct",
                      "eq_high_dist_pct", "eq_low_dist_pct"):
            key = (name, p("struct_pivot_wing", 3))
        elif name in ("liq_sweep_low", "liq_sweep_high"):
            key = (name, p("swing_lookback", 10), p("bos_window", 10))
        elif name in ("channel_pos", "channel_slope_pct"):
            key = (name, p("channel_period", 100))
        elif name == "range_pos":
            key = ("range_pos", p("donchian_period", 20))
        elif name == "dist_ema200_pct":
            key = ("dist_ema200_pct", p("ema200_period", 200))
        elif name in ("fomc_today", "days_to_fomc"):
            key = (name,)
        else:
            return np.full(self.n, np.nan)

        if key in self._cache:
            return self._cache[key]
        # Persistente Bibliothek: identische (Serie, Indikator+Parameter)
        # muss nicht neu berechnet werden – speziell hilfreich für Deep Tests.
        fp = self._fingerprint()
        cached = indicator_cache.get(fp, key)
        if cached is not None and len(cached) == self.n:
            self._cache[key] = cached
            return cached
        arr = self._compute(name, key)
        self._cache[key] = arr
        indicator_cache.put(fp, key, arr)
        return arr

    def _compute(self, name: str, key: tuple) -> np.ndarray:
        n = self.n
        if key[0] == "hour":
            hours = pd.to_datetime(self.ts, unit="ms", utc=True) \
                .tz_convert("Europe/Berlin").hour
            return np.asarray(hours, dtype=float)
        if key[0] == "rsi":
            return vec.rsi(self.close, key[1])
        if key[0] == "ema":
            return vec.ema(self.close, key[1])
        if key[0] == "ema_gap_pct":
            ef = self._cached_ema(key[1])
            es = self._cached_ema(key[2])
            with np.errstate(invalid="ignore", divide="ignore"):
                out = (ef - es) / es * 100
            out[es == 0] = np.nan
            return out
        if key[0] == "sma":
            return gpu_accel.rolling_mean(self.close, key[1])
        if key[0] == "ha_color":
            return vec.heikin_ashi_green(self.open, self.high, self.low, self.close)
        if key[0] in ("macd", "macd_signal", "macd_hist"):
            fast_p, slow_p, sig_p = key[1], key[2], key[3]
            macd = self._cached_ema(fast_p) - self._cached_ema(slow_p)
            sig = np.full(n, np.nan)
            first = int(np.argmax(~np.isnan(macd))) if not np.isnan(macd).all() else n
            if first < n:
                sig[first:] = vec.ema(macd[first:], sig_p)
            self._cache[("macd", fast_p, slow_p, sig_p)] = macd
            self._cache[("macd_signal", fast_p, slow_p, sig_p)] = sig
            self._cache[("macd_hist", fast_p, slow_p, sig_p)] = macd - sig
            return self._cache[key]
        if key[0] in ("bb_upper", "bb_middle", "bb_lower", "bb_width_pct"):
            period, std = key[1], key[2]
            s = pd.Series(self.close)
            m = s.rolling(period).mean().to_numpy()
            sd = s.rolling(period).std(ddof=0).to_numpy()
            u, lo = m + std * sd, m - std * sd
            with np.errstate(invalid="ignore", divide="ignore"):
                w = (u - lo) / m * 100
            self._cache[("bb_upper", period, std)] = u
            self._cache[("bb_middle", period, std)] = m
            self._cache[("bb_lower", period, std)] = lo
            self._cache[("bb_width_pct", period, std)] = w
            return self._cache[key]
        if key[0] == "atr":
            return vec.atr(self.high, self.low, self.close, key[1])
        if key[0] in ("adx", "plus_di", "minus_di"):
            adx, pdi, mdi = vec.adx_di(self.high, self.low, self.close, key[1])
            self._cache[("adx", key[1])] = adx
            self._cache[("plus_di", key[1])] = pdi
            self._cache[("minus_di", key[1])] = mdi
            return self._cache[key]
        if key[0] == "cci":
            return vec.cci(self.high, self.low, self.close, key[1])
        if key[0] in ("keltner_upper", "keltner_middle", "keltner_lower"):
            per, atr_p, mult = key[1], key[2], key[3]
            mid = vec.ema(self.close, per)
            a = self.get("atr", {"atr_period": atr_p})
            self._cache[("keltner_middle", per, atr_p, mult)] = mid
            self._cache[("keltner_upper", per, atr_p, mult)] = mid + mult * a
            self._cache[("keltner_lower", per, atr_p, mult)] = mid - mult * a
            return self._cache[key]
        if key[0] == "donchian_high":
            return pd.Series(self.high).shift(1).rolling(key[1], min_periods=key[1]).max().to_numpy()
        if key[0] == "donchian_low":
            return pd.Series(self.low).shift(1).rolling(key[1], min_periods=key[1]).min().to_numpy()
        if key[0] == "atr_pct":
            atr = self.get("atr", {"atr_period": key[1]})
            with np.errstate(invalid="ignore", divide="ignore"):
                out = atr / self.close * 100
            return out
        if key[0] == "vwap":
            tp = (self.high + self.low + self.close) / 3
            pv = tp * self.vol
            cpv = np.concatenate([[0.0], np.cumsum(pv)])
            cv = np.concatenate([[0.0], np.cumsum(self.vol)])
            idx = np.arange(n)
            start = np.maximum(idx - VWAP_WINDOW + 1, 0)
            sum_pv = cpv[idx + 1] - cpv[start]
            sum_v = cv[idx + 1] - cv[start]
            with np.errstate(invalid="ignore", divide="ignore"):
                out = np.where(sum_v > 0, sum_pv / np.where(sum_v > 0, sum_v, 1), tp)
            return out
        if key[0] in ("stoch_k", "stoch_d"):
            kp, dp = key[1], key[2]
            hi = gpu_accel.rolling_max(self.high, kp)
            lo = gpu_accel.rolling_min(self.low, kp)
            rng = hi - lo
            with np.errstate(invalid="ignore", divide="ignore"):
                k = np.where(rng == 0, 50.0, (self.close - lo) / np.where(rng == 0, 1, rng) * 100)
            k[np.isnan(hi)] = np.nan
            dser = pd.Series(k).rolling(dp).mean().to_numpy()
            self._cache[("stoch_k", kp, dp)] = k
            self._cache[("stoch_d", kp, dp)] = dser
            return self._cache[key]
        if key[0] == "volume_sma":
            return pd.Series(self.vol).rolling(key[1], min_periods=1).mean().to_numpy()
        if key[0] == "rel_volume":
            avg = self.get("volume_sma", {"volume_sma_period": key[1]})
            with np.errstate(invalid="ignore", divide="ignore"):
                out = np.where(avg > 0, self.vol / np.where(avg > 0, avg, 1), np.nan)
            # Instrumente ohne Volumendaten (Forex): Filter neutral halten (1.0),
            # sonst blockiert ein Volumen-Kriterium jedes Signal dauerhaft.
            if not np.any(self.vol > 0):
                out = np.ones(n)
            return out
        if key[0] == "price_change_pct":
            lb = key[1]
            out = np.full(n, np.nan)
            if n > lb:
                prev = self.close[:-lb]
                with np.errstate(invalid="ignore", divide="ignore"):
                    out[lb:] = np.where(prev != 0, (self.close[lb:] - prev) / prev * 100, np.nan)
            return out
        if key[0] == "recent_high":
            return pd.Series(self.high).shift(1).rolling(key[1], min_periods=1).max().to_numpy()
        if key[0] == "recent_low":
            return pd.Series(self.low).shift(1).rolling(key[1], min_periods=1).min().to_numpy()
        # --- Etappe 5: Struktur / Liquidität / Kanal / Events -------------
        if key[0] == "market_structure":
            from services import structure_indicators as si
            f = si.structure_features(self.high, self.low, self.close,
                                      key[1], 10)
            return f["market_structure"]
        if key[0] in ("bos_up", "bos_dn"):
            from services import structure_indicators as si
            f = si.structure_features(self.high, self.low, self.close,
                                      key[1], key[2])
            self._cache[("bos_up", key[1], key[2])] = f["bos_up"]
            self._cache[("bos_dn", key[1], key[2])] = f["bos_dn"]
            return self._cache[key]
        if key[0] in ("dist_support_pct", "dist_resistance_pct",
                      "eq_high_dist_pct", "eq_low_dist_pct"):
            from services import structure_indicators as si
            f = si.sr_features(self.high, self.low, self.close, key[1])
            for k2, arr in f.items():
                self._cache[(k2, key[1])] = arr
            return self._cache[key]
        if key[0] in ("liq_sweep_low", "liq_sweep_high"):
            from services import structure_indicators as si
            f = si.sweep_features(self.high, self.low, self.close,
                                  key[1], key[2])
            self._cache[("liq_sweep_low", key[1], key[2])] = f["liq_sweep_low"]
            self._cache[("liq_sweep_high", key[1], key[2])] = f["liq_sweep_high"]
            return self._cache[key]
        if key[0] in ("channel_pos", "channel_slope_pct"):
            from services import structure_indicators as si
            f = si.channel_features(self.close, key[1])
            self._cache[("channel_pos", key[1])] = f["channel_pos"]
            self._cache[("channel_slope_pct", key[1])] = f["channel_slope_pct"]
            return self._cache[key]
        if key[0] == "range_pos":
            from services import structure_indicators as si
            return si.range_pos(self.high, self.low, self.close, key[1])
        if key[0] == "dist_ema200_pct":
            e = self._cached_ema(key[1])
            with np.errstate(invalid="ignore", divide="ignore"):
                out = (self.close - e) / np.where(e != 0, e, np.nan) * 100
            return out
        if key[0] in ("fomc_today", "days_to_fomc"):
            from services import structure_indicators as si
            f = si.fomc_features(self.ts)
            self._cache[("fomc_today",)] = f["fomc_today"]
            self._cache[("days_to_fomc",)] = f["days_to_fomc"]
            return self._cache[key]
        return np.full(n, np.nan)

    def _cached_ema(self, period: int) -> np.ndarray:
        key = ("ema", period)
        if key not in self._cache:
            self._cache[key] = _nan(TI.calculate_ema(list(self.close), period))
        return self._cache[key]


def _token_series(token, fs: FastSeries, d: Dict) -> Optional[np.ndarray]:
    """Indikator-Name, dynamischer Indikator, Mathe-Ausdruck oder Zahl -> Serie."""
    if isinstance(token, bool):
        return None
    if isinstance(token, (int, float)):
        return np.full(fs.n, float(token))
    if not isinstance(token, str):
        return None
    if token in INDICATORS or token == "hour" or rule_engine.parse_dynamic(token):
        return fs.get(token, d)
    if rule_engine.looks_like_expression(token):
        rpn = rule_engine.parse_expression(token)
        if rpn is not None:
            return rule_engine.eval_rpn(rpn, lambda nm: fs.get(nm, d))
    try:
        return np.full(fs.n, float(token))
    except (TypeError, ValueError):
        return None


def _rule_cond_htf(rule: Dict, fs: FastSeries, d: Dict, tf) -> Optional[np.ndarray]:
    """Regel mit Timeframe-Override auf dem höheren TF auswerten und das
    Ergebnis auf die Basis-Kerzen zurückmappen. None = Override nicht
    anwendbar (gleicher/ungültiger TF) -> normaler Basis-TF-Pfad."""
    tf = normalize_rule_tf(tf) or (str(tf) if str(tf) in TIMEFRAMES else None)
    if not tf:
        return None
    tf_ms = TIMEFRAMES[tf] * 60000
    base_ms = fs.base_tf_ms()
    if tf_ms == base_ms or tf_ms < base_ms or tf_ms % base_ms != 0:
        return None
    sub, map_idx = fs.htf(tf)
    out = np.zeros(fs.n, dtype=bool)
    if sub.n == 0:
        return out
    cond_h = _rule_cond({k: v for k, v in rule.items() if k != "timeframe"}, sub, d)
    valid = map_idx >= 0
    out[valid] = cond_h[map_idx[valid]]
    return out


def _rule_cond(rule: Dict, fs: FastSeries, d: Dict) -> np.ndarray:
    n = fs.n
    rule_tf = rule.get("timeframe")
    if rule_tf:
        htf_cond = _rule_cond_htf(rule, fs, d, rule_tf)
        if htf_cond is not None:
            return htf_cond
    op = rule.get("op")
    left = _token_series(rule.get("indicator"), fs, d)
    if left is None:
        return np.zeros(n, dtype=bool)
    v = rule.get("value")
    if op in rule_engine.RANGE_OPS:
        rng = rule_engine.parse_range(v)
        if not rng:
            return np.zeros(n, dtype=bool)
        ok = rule_engine.range_condition(left, rng[0], rng[1])
        if op == "not_in_range":
            ok = ~ok
        ok &= ~np.isnan(left)
        return ok
    right = _token_series(v, fs, d)
    if right is None:
        return np.zeros(n, dtype=bool)
    with np.errstate(invalid="ignore"):
        if op == "<":
            ok = left < right
        elif op == ">":
            ok = left > right
        elif op == "<=":
            ok = left <= right
        elif op == ">=":
            ok = left >= right
        elif op == "==":
            ok = np.isclose(left, right, rtol=1e-9, atol=1e-12)
        elif op == "!=":
            ok = ~np.isclose(left, right, rtol=1e-9, atol=1e-12)
        elif op in ("cross_above", "cross_below"):
            lp = np.concatenate([[np.nan], left[:-1]])
            rp = np.concatenate([[np.nan], right[:-1]])
            if op == "cross_above":
                ok = (lp <= rp) & (left > right)
            else:
                ok = (lp >= rp) & (left < right)
            ok &= ~np.isnan(lp) & ~np.isnan(rp)
        else:
            return np.zeros(n, dtype=bool)
    ok &= ~np.isnan(left) & ~np.isnan(right)
    return ok


def provider_for(strategy, fs: FastSeries, settings: Optional[Dict] = None,
                 symbol: Optional[str] = None) -> Callable[[int], Optional[Dict]]:
    """EINE Stelle, an der der Fast-Path-Provider einer Strategie entsteht.

    Wichtig für Custom-/KI-Strategien: die aktuell gültigen Strategie-Parameter
    (Optimizer-Ergebnis, Coin-Overrides) werden auf die Regel-Definition
    angewendet, damit Fast-Path und Live-Pfad garantiert identisch rechnen.
    """
    settings = settings or {}
    if getattr(strategy, "IS_CUSTOM", False):
        definition = strategy.definition
        eff = getattr(strategy, "effective_definition", None)
        if callable(eff):
            definition = eff(strategy.get_params(settings, symbol))
        return build_signal_provider(definition, fs)
    return build_builtin_signal_provider(strategy, fs, settings, symbol)


def build_signal_provider(definition: Dict, fs: FastSeries) -> Callable[[int], Optional[Dict]]:
    """Erzeugt provider(i) -> Signal-Dict (kompatibel zu check_signal) oder None."""
    d = definition.get("indicators", {})
    n = fs.n
    long_rules = definition.get("long_rules", [])
    short_rules = definition.get("short_rules", [])

    long_ok = np.zeros(n, dtype=bool)
    short_ok = np.zeros(n, dtype=bool)
    if long_rules:
        long_ok = np.ones(n, dtype=bool)
        for r in long_rules:
            long_ok &= _rule_cond(r, fs, d)
    if short_rules:
        short_ok = np.ones(n, dtype=bool)
        for r in short_rules:
            short_ok &= _rule_cond(r, fs, d)

    # gleiche Guards wie CustomStrategy.analyze
    try:
        slow = int(d.get("ema_slow_period", 50) or 50)
    except (TypeError, ValueError):
        slow = 50
    need = max(slow + 10, 60)
    rsi = fs.get("rsi", d)
    es = fs.get("ema_slow", d)
    valid = ~np.isnan(rsi) & ~np.isnan(es)
    long_ok &= valid
    short_ok &= valid

    nl, ns = len(long_rules), len(short_rules)
    close = fs.close

    def provider(i: int) -> Optional[Dict]:
        if i < need:
            return None
        if long_ok[i]:
            return {"type": "LONG", "signal_class": "SIGNAL",
                    "entry_price": float(close[i]),
                    "rules_met_count": nl, "rules_total": nl,
                    "rsi": float(rsi[i]) if not np.isnan(rsi[i]) else None}
        if short_ok[i]:
            return {"type": "SHORT", "signal_class": "SIGNAL",
                    "entry_price": float(close[i]),
                    "rules_met_count": ns, "rules_total": ns,
                    "rsi": float(rsi[i]) if not np.isnan(rsi[i]) else None}
        return None

    return provider


# --------------------------------------------------------------------------
# Fast-Path für Built-in-Strategien
# --------------------------------------------------------------------------
# Konvention: Eine Built-in-Strategie kann optional eine Klassenmethode
#   vectorized_signals(fs: FastSeries, params: Dict) -> Dict
# anbieten, die {"long": bool_array, "short": bool_array,
#                "warmup": int, "rules_total": int, "rsi": array_or_None}
# zurückgibt. build_builtin_signal_provider() wickelt das in ein provider(i)
# ein und ist voll kompatibel zu simulate_pair(). Ist die Methode nicht
# vorhanden ODER wirft sie eine Exception, liefert build_builtin_signal_provider()
# None -> simulate_pair fällt automatisch auf strategy.check_signal() zurück.


def build_builtin_signal_provider(strategy, fs: FastSeries, settings: Dict,
                                  symbol: str = None) -> Optional[Callable[[int], Optional[Dict]]]:
    """Nur wenn strategy.vectorized_signals() existiert und Erfolg meldet."""
    fn = getattr(strategy, "vectorized_signals", None)
    if not callable(fn):
        return None
    try:
        params = strategy.get_params(settings, symbol)
        out = fn(fs, params)
    except Exception:  # noqa: BLE001
        return None
    if not out or not isinstance(out, dict):
        return None
    long_ok = out.get("long")
    short_ok = out.get("short")
    if long_ok is None or short_ok is None:
        return None
    warmup = int(out.get("warmup", 60))
    total = int(out.get("rules_total", 0))
    rsi_arr = out.get("rsi")
    long_pre = out.get("long_pre")
    short_pre = out.get("short_pre")
    close = fs.close

    def provider(i: int) -> Optional[Dict]:
        if i < warmup:
            return None
        if long_ok[i]:
            return {"type": "LONG", "signal_class": "SIGNAL",
                    "entry_price": float(close[i]),
                    "rules_met_count": total, "rules_total": total,
                    "rsi": float(rsi_arr[i]) if rsi_arr is not None
                    and not np.isnan(rsi_arr[i]) else None}
        if short_ok[i]:
            return {"type": "SHORT", "signal_class": "SIGNAL",
                    "entry_price": float(close[i]),
                    "rules_met_count": total, "rules_total": total,
                    "rsi": float(rsi_arr[i]) if rsi_arr is not None
                    and not np.isnan(rsi_arr[i]) else None}
        # Pre-Signale (z.B. EMA Pullback: alles außer Rückkehr-Regel erfüllt)
        if long_pre is not None and long_pre[i]:
            return {"type": "LONG", "signal_class": "PRE_SIGNAL",
                    "entry_price": float(close[i]),
                    "rules_met_count": max(total - 1, 0), "rules_total": total,
                    "rsi": float(rsi_arr[i]) if rsi_arr is not None
                    and not np.isnan(rsi_arr[i]) else None}
        if short_pre is not None and short_pre[i]:
            return {"type": "SHORT", "signal_class": "PRE_SIGNAL",
                    "entry_price": float(close[i]),
                    "rules_met_count": max(total - 1, 0), "rules_total": total,
                    "rsi": float(rsi_arr[i]) if rsi_arr is not None
                    and not np.isnan(rsi_arr[i]) else None}
        return None

    return provider


def _cross_up(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    ap = np.concatenate([[np.nan], a[:-1]])
    bp = np.concatenate([[np.nan], b[:-1]])
    out = (ap <= bp) & (a > b)
    out &= ~np.isnan(ap) & ~np.isnan(bp)
    return out


def _cross_down(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    ap = np.concatenate([[np.nan], a[:-1]])
    bp = np.concatenate([[np.nan], b[:-1]])
    out = (ap >= bp) & (a < b)
    out &= ~np.isnan(ap) & ~np.isnan(bp)
    return out


def sweep_arrays(fs: FastSeries, left: int = 2, right: int = 2):
    """Vektorisierte Liquidity-Sweep-Erkennung (identisch zu TI.liquidity_sweep):
    Fraktal-Swings; letzte 3 Swing-Lows/Highs VOR der aktuellen Kerze;
    bullish = Low unterschreitet Recent-Low, Close schließt darüber."""
    n = fs.n
    win = left + right + 1
    lo_s = pd.Series(fs.low)
    hi_s = pd.Series(fs.high)
    is_sl = (fs.low <= lo_s.rolling(win, center=True).min().to_numpy())
    is_sh = (fs.high >= hi_s.rolling(win, center=True).max().to_numpy())
    bull = np.zeros(n, dtype=bool)
    bear = np.zeros(n, dtype=bool)
    lows: List[float] = []   # letzte Swing-Low-Preise
    highs: List[float] = []
    low, high, close = fs.low, fs.high, fs.close
    for t in range(n):
        # Swing bei j ist ab t sichtbar, wenn j + right <= t - 1  (prior = candles[:-1])
        j = t - 1 - right
        if j >= left:
            if is_sl[j]:
                lows.append(low[j])
                if len(lows) > 3:
                    lows.pop(0)
            if is_sh[j]:
                highs.append(high[j])
                if len(highs) > 3:
                    highs.pop(0)
        if t < left + right + 4:  # len(candles) >= left+right+5 Guard
            continue
        if lows:
            rl = min(lows)
            if low[t] < rl and close[t] > rl:
                bull[t] = True
        if highs:
            rh = max(highs)
            if high[t] > rh and close[t] < rh:
                bear[t] = True
    return bull, bear
