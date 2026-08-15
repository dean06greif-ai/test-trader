"""Parameter-Suchraum & Regel-Normalisierung für Custom-/KI-Strategien.

Warum dieses Modul existiert
----------------------------
Von der KI erzeugte Strategien landen als ``CustomStrategy`` (Regel-Definition aus
dem Strategie-Labor). Bisher hatten diese Strategien KEINE ``DEFAULT_PARAMS``:

  * Der Parameter-Optimierer fand deshalb einen leeren Suchraum → jede
    Kombination war identisch → im Ergebnis standen überall Nullen.
  * Schrieb die KI einen Indikator-Namen, den die Regel-Auswertung nicht kennt
    (``adx``, ``cci``, ``close`` …), war jede Regel dauerhaft ``False`` →
    0 Trades → Backtest ebenfalls "alles 0".

Dieses Modul löst beides – rein, ohne Seiteneffekte und damit testbar:

  1. ``normalize_definition``  – Alias-Namen (KI-Schreibweisen) auf das
     unterstützte Vokabular abbilden und unbekannte Regeln melden.
  2. ``build_param_meta``      – ``DEFAULT_PARAMS``-kompatibler Suchraum aus der
     Definition (genutzte Indikator-Perioden + numerische Regel-Schwellen).
  3. ``apply_params``          – optimierte Parameter zurück in eine Definition
     schreiben (eine Quelle der Wahrheit für Live-Pfad und Fast-Path).
"""
from typing import Dict, List, Optional, Tuple

from services.timeframes import (RULE_TIMEFRAMES, TIMEFRAMES, normalize_rule_tf,
                                 tf_minutes, valid_rule_tf)
from strategies import rule_engine

# --------------------------------------------------------------------------- #
#  1) Vokabular: Aliase für Indikatoren & Operatoren
# --------------------------------------------------------------------------- #
INDICATOR_ALIASES = {
    "close": "price", "close_price": "price", "last": "price", "c": "price",
    "ema9": "ema_fast", "emafast": "ema_fast", "ema_short": "ema_fast",
    "ema50": "ema_slow", "emaslow": "ema_slow", "ema_long": "ema_slow",
    "rsi14": "rsi", "rsi_14": "rsi", "relative_strength_index": "rsi",
    "macd_line": "macd", "macdline": "macd",
    "macd_histogram": "macd_hist", "macdhist": "macd_hist", "macd_h": "macd_hist",
    "macd_signal_line": "macd_signal",
    "bollinger_upper": "bb_upper", "bb_up": "bb_upper", "upper_band": "bb_upper",
    "bollinger_lower": "bb_lower", "bb_low": "bb_lower", "lower_band": "bb_lower",
    "bollinger_middle": "bb_middle", "bb_mid": "bb_middle", "basis": "bb_middle",
    "bb_width": "bb_width_pct", "bandwidth": "bb_width_pct",
    "atr_percent": "atr_pct", "atrp": "atr_pct",
    "stoch": "stoch_k", "stochastic": "stoch_k", "stoch_%k": "stoch_k",
    "stoch_%d": "stoch_d",
    "volume_avg": "volume_sma", "avg_volume": "volume_sma",
    "relative_volume": "rel_volume", "relvol": "rel_volume", "vol_ratio": "rel_volume",
    "vol": "volume",
    "change_pct": "price_change_pct", "momentum": "price_change_pct",
    "roc": "price_change_pct", "rate_of_change": "price_change_pct",
    "swing_high": "recent_high", "prev_high": "recent_high", "highest": "recent_high",
    "swing_low": "recent_low", "prev_low": "recent_low", "lowest": "recent_low",
    "adx14": "adx", "adx_14": "adx", "average_directional_index": "adx",
    "di_plus": "plus_di", "di+": "plus_di", "dmi_plus": "plus_di",
    "di_minus": "minus_di", "di-": "minus_di", "dmi_minus": "minus_di",
    "commodity_channel_index": "cci",
    "keltner_up": "keltner_upper", "kc_upper": "keltner_upper",
    "keltner_down": "keltner_lower", "kc_lower": "keltner_lower",
    "keltner_mid": "keltner_middle", "kc_middle": "keltner_middle",
    "donchian_upper": "donchian_high", "channel_high": "donchian_high",
    "donchian_lower": "donchian_low", "channel_low": "donchian_low",
    "heikin_ashi": "ha_color", "ha": "ha_color",
    "vwap_session": "vwap",
    "volatility_pct": "atr_pct", "volatility": "atr_pct",
    "volatility_percent": "atr_pct",
}

OPERATOR_ALIASES = {
    "lt": "<", "less": "<", "less_than": "<", "below": "<", "under": "<",
    "gt": ">", "greater": ">", "greater_than": ">", "above": ">", "over": ">",
    "lte": "<=", "le": "<=", "=<": "<=",
    "gte": ">=", "ge": ">=", "=>": ">=",
    "crosses_above": "cross_above", "cross_over": "cross_above",
    "crossover": "cross_above", "crossabove": "cross_above",
    "crosses_below": "cross_below", "cross_under": "cross_below",
    "crossunder": "cross_below", "crossbelow": "cross_below",
    "eq": "==", "=": "==", "equals": "==", "equal": "==",
    "neq": "!=", "<>": "!=", "not_equal": "!=", "not_equals": "!=",
    "between": "in_range", "inside": "in_range", "in range": "in_range",
    "range": "in_range", "within": "in_range",
    "not_between": "not_in_range", "outside": "not_in_range",
    "not in range": "not_in_range", "outside_range": "not_in_range",
}


def canonical_indicator(name) -> str:
    key = rule_engine.normalize_name(name)
    canon, kind = rule_engine.canonical_token(key, (), INDICATOR_ALIASES)
    if kind in ("hour", "dynamic"):
        return canon
    return INDICATOR_ALIASES.get(key, key)


def canonical_operator(op) -> str:
    key = str(op or "").strip().lower()
    return OPERATOR_ALIASES.get(key, key)


def normalize_definition(definition: Dict, known_indicators, known_operators
                         ) -> Tuple[Dict, List[str]]:
    """Definition mit kanonischen Indikator-/Operator-Namen + Liste der Regeln,
    die (auch nach Alias-Auflösung) nicht auswertbar sind.

    Die Original-Definition wird NICHT verändert (rein). Unbekannte Regeln
    bleiben erhalten – sie werden nur gemeldet, damit die UI/KI erklären kann,
    warum ein Backtest keine Trades liefert.
    """
    known_ind = set(known_indicators)
    known_ops = set(known_operators)
    out = dict(definition)
    problems: List[str] = []

    def canon_token_or_expr(raw, where: str) -> Optional[str]:
        """Indikator-Token ODER Mathe-Ausdruck kanonisieren; None = unbekannt."""
        canon, kind = rule_engine.canonical_token(raw, known_ind, INDICATOR_ALIASES)
        if kind != "unknown":
            return canon
        if rule_engine.looks_like_expression(str(raw)):
            expr, unknown = rule_engine.canonicalize_expression(
                str(raw), known_ind, INDICATOR_ALIASES)
            if expr is not None and not unknown \
                    and rule_engine.parse_expression(expr) is not None:
                return expr
            if unknown:
                problems.append(f"{where}: unbekannte Größe(n) im Ausdruck: "
                                f"{', '.join(unknown)}")
                return None
        problems.append(f"{where}: Indikator '{raw}' wird nicht unterstützt")
        return None

    for side in ("long_rules", "short_rules"):
        rules = definition.get(side) or []
        new_rules = []
        for i, r in enumerate(rules):
            if not isinstance(r, dict):
                problems.append(f"{side}[{i}]: keine Regel-Struktur")
                continue
            rule = dict(r)
            if rule.get("op") is None and rule.get("operator") is not None:
                rule["op"] = rule.pop("operator")  # Alias "operator" akzeptieren
            where = f"{side}[{i}]"
            ind = canon_token_or_expr(rule.get("indicator"), where)
            if ind is not None:
                rule["indicator"] = ind
            op = canonical_operator(rule.get("op"))
            rule["op"] = op
            if op not in known_ops:
                problems.append(f"{where}: Operator '{r.get('op')}' "
                                "wird nicht unterstützt")
            val = rule.get("value")
            if op in rule_engine.RANGE_OPS:
                rng = rule_engine.parse_range(val)
                if rng:
                    rule["value"] = [rng[0], rng[1]]
                else:
                    problems.append(f"{where}: '{op}' braucht einen Bereich "
                                    f"(z.B. [9, 17]), nicht '{val}'")
            elif isinstance(val, str):
                canon, kind = rule_engine.canonical_token(val, known_ind, INDICATOR_ALIASES)
                if kind != "unknown":
                    rule["value"] = canon
                elif rule_engine.looks_like_expression(val):
                    expr, unknown = rule_engine.canonicalize_expression(
                        val, known_ind, INDICATOR_ALIASES)
                    if expr is not None and not unknown \
                            and rule_engine.parse_expression(expr) is not None:
                        rule["value"] = expr
                    else:
                        problems.append(f"{where}: Ausdruck '{val}' nicht auswertbar"
                                        + (f" (unbekannt: {', '.join(unknown)})"
                                           if unknown else ""))
                else:
                    try:
                        rule["value"] = float(val)
                    except (TypeError, ValueError):
                        problems.append(f"{where}: Wert '{val}' unbekannt")
            # Optionales Timeframe-Override je Regel (Multi-Timeframe-Filter)
            raw_tf = rule.get("timeframe")
            if raw_tf in (None, ""):
                rule.pop("timeframe", None)
            else:
                ntf = normalize_rule_tf(raw_tf)
                base_tf = out.get("timeframe") or "1m"
                if base_tf not in TIMEFRAMES:
                    base_tf = "1m"
                if not ntf:
                    problems.append(f"{where}: Timeframe '{raw_tf}' wird nicht unterstützt "
                                    f"(erlaubt: {', '.join(RULE_TIMEFRAMES)})")
                elif not valid_rule_tf(ntf, base_tf):
                    problems.append(f"{where}: Regel-Timeframe '{ntf}' muss ≥ Strategie-"
                                    f"Timeframe ('{base_tf}') und ein Vielfaches davon sein")
                elif tf_minutes(ntf) == tf_minutes(base_tf):
                    rule.pop("timeframe", None)  # Override = Basis-TF -> kein Override
                else:
                    rule["timeframe"] = ntf
            new_rules.append(rule)
        if rules:
            out[side] = new_rules
    return out, problems


def fix_suggestions(definition: Dict, known_indicators, known_operators) -> List[Dict]:
    """Klickbare Korrektur-Vorschläge für nicht auswertbare Regeln.

    Jeder Vorschlag: {side, index, field, from, to, label} – anwendbar über
    POST /api/strategies/{id}/apply-fixes."""
    known_ind = set(known_indicators)
    known_ops = set(known_operators)
    out: List[Dict] = []
    for side in ("long_rules", "short_rules"):
        for i, r in enumerate(definition.get(side) or []):
            if not isinstance(r, dict):
                continue
            tag = "LONG" if side == "long_rules" else "SHORT"

            def _token_fix(field: str, raw):
                canon, kind = rule_engine.canonical_token(raw, known_ind, INDICATOR_ALIASES)
                if kind != "unknown":
                    return  # auswertbar (ggf. via Alias/dynamisch)
                if rule_engine.looks_like_expression(str(raw)):
                    expr, unknown = rule_engine.canonicalize_expression(
                        str(raw), known_ind, INDICATOR_ALIASES)
                    if expr is not None and not unknown \
                            and rule_engine.parse_expression(expr) is not None:
                        return
                    fixed = rule_engine.fix_expression(str(raw), known_ind, INDICATOR_ALIASES)
                    if fixed:
                        out.append({"side": side, "index": i, "field": field,
                                    "from": raw, "to": fixed,
                                    "label": f"{tag} {i + 1}: '{raw}' → '{fixed}'"})
                    return
                sug = rule_engine.suggest_indicator(str(raw), known_ind, INDICATOR_ALIASES)
                if sug:
                    out.append({"side": side, "index": i, "field": field,
                                "from": raw, "to": sug,
                                "label": f"{tag} {i + 1}: '{raw}' → '{sug}'"})

            _token_fix("indicator", r.get("indicator"))
            op = canonical_operator(r.get("op"))
            if op not in known_ops:
                sug = rule_engine.suggest_operator(op, known_ops, OPERATOR_ALIASES)
                if sug and sug in known_ops:
                    out.append({"side": side, "index": i, "field": "op",
                                "from": r.get("op"), "to": sug,
                                "label": f"{tag} {i + 1}: Operator '{r.get('op')}' → '{sug}'"})
            val = r.get("value")
            if isinstance(val, str) and op not in rule_engine.RANGE_OPS:
                try:
                    float(val)
                except (TypeError, ValueError):
                    _token_fix("value", val)
    return out


# --------------------------------------------------------------------------- #
#  2) Suchraum: Perioden je genutztem Indikator + numerische Regel-Schwellen
# --------------------------------------------------------------------------- #
# Indikator -> benötigte Perioden-Parameter
INDICATOR_PERIODS = {
    "rsi": ["rsi_period"],
    "ema_fast": ["ema_fast_period"],
    "ema_slow": ["ema_slow_period"],
    "ema_gap_pct": ["ema_fast_period", "ema_slow_period"],
    "sma": ["sma_period"],
    "macd": ["macd_fast", "macd_slow", "macd_signal_period"],
    "macd_signal": ["macd_fast", "macd_slow", "macd_signal_period"],
    "macd_hist": ["macd_fast", "macd_slow", "macd_signal_period"],
    "bb_upper": ["bb_period", "bb_std"],
    "bb_middle": ["bb_period"],
    "bb_lower": ["bb_period", "bb_std"],
    "bb_width_pct": ["bb_period", "bb_std"],
    "atr": ["atr_period"],
    "atr_pct": ["atr_period"],
    "stoch_k": ["stoch_k_period"],
    "stoch_d": ["stoch_k_period", "stoch_d_period"],
    "volume_sma": ["volume_sma_period"],
    "rel_volume": ["volume_sma_period"],
    "price_change_pct": ["change_lookback"],
    "recent_high": ["swing_lookback"],
    "recent_low": ["swing_lookback"],
    "adx": ["adx_period"],
    "plus_di": ["adx_period"],
    "minus_di": ["adx_period"],
    "cci": ["cci_period"],
    "keltner_upper": ["keltner_period", "keltner_mult", "atr_period"],
    "keltner_middle": ["keltner_period"],
    "keltner_lower": ["keltner_period", "keltner_mult", "atr_period"],
    "donchian_high": ["donchian_period"],
    "donchian_low": ["donchian_period"],
}

# Perioden-Parameter -> (default, min, max, step, Label)
PERIOD_RANGES = {
    "ema_fast_period": (9, 3, 30, 1, "EMA Fast Periode"),
    "ema_slow_period": (50, 20, 200, 5, "EMA Slow Periode"),
    "rsi_period": (14, 5, 30, 1, "RSI Periode"),
    "sma_period": (20, 5, 100, 5, "SMA Periode"),
    "macd_fast": (12, 5, 20, 1, "MACD Fast"),
    "macd_slow": (26, 15, 50, 2, "MACD Slow"),
    "macd_signal_period": (9, 3, 20, 1, "MACD Signal"),
    "bb_period": (20, 10, 50, 2, "Bollinger Periode"),
    "bb_std": (2.0, 1.0, 3.0, 0.25, "Bollinger Std-Abw."),
    "atr_period": (14, 5, 30, 1, "ATR Periode"),
    "stoch_k_period": (14, 5, 30, 1, "Stochastik %K Periode"),
    "stoch_d_period": (3, 2, 10, 1, "Stochastik %D Periode"),
    "volume_sma_period": (20, 5, 60, 5, "Volumen Ø Periode"),
    "change_lookback": (5, 2, 30, 1, "Preisänderung Lookback"),
    "swing_lookback": (10, 3, 50, 1, "Hoch/Tief Lookback"),
    "adx_period": (14, 5, 30, 1, "ADX Periode"),
    "cci_period": (20, 10, 40, 2, "CCI Periode"),
    "keltner_period": (20, 10, 40, 2, "Keltner Periode"),
    "keltner_mult": (2.0, 1.0, 3.0, 0.25, "Keltner Multiplikator"),
    "donchian_period": (20, 10, 60, 5, "Donchian Periode"),
}

# Indikatoren mit fester Skala -> Schwellen-Suchraum (min, max, step)
BOUNDED_THRESHOLDS = {
    "rsi": (5, 95, 2), "stoch_k": (5, 95, 2), "stoch_d": (5, 95, 2),
    "adx": (5, 60, 1), "plus_di": (5, 60, 1), "minus_di": (5, 60, 1),
    "cci": (-250, 250, 10),
    "rel_volume": (0.5, 5.0, 0.1),
    "atr_pct": (0.05, 5.0, 0.05),
    "bb_width_pct": (0.1, 10.0, 0.1),
    "ema_gap_pct": (-5.0, 5.0, 0.1),
    "price_change_pct": (-10.0, 10.0, 0.1),
}
# Binär (0/1) bzw. Zeit – nicht optimierbar
BINARY_INDICATORS = {"ha_color", "hour"}

MAX_PARAMS = 12          # Suchraum bewusst begrenzt (Laufzeit + Overfitting)
_WINDOW = 0.35           # relative Fenstergröße für freie Schwellen (±35 %)


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _round_step(v: float, step: float) -> float:
    if step >= 1:
        return round(v / step) * step
    return round(round(v / step) * step, 4)


def _threshold_meta(indicator: str, value: float) -> Optional[Dict]:
    """Suchraum für eine numerische Regel-Schwelle (um den KI-Wert herum)."""
    if indicator in BINARY_INDICATORS:
        return None
    if indicator in BOUNDED_THRESHOLDS:
        lo, hi, step = BOUNDED_THRESHOLDS[indicator]
        span = (hi - lo) * 0.25
        vmin = _clamp(_round_step(value - span, step), lo, hi)
        vmax = _clamp(_round_step(value + span, step), lo, hi)
        if vmax <= vmin:
            vmin, vmax = lo, hi
        return {"value": value, "min": vmin, "max": vmax, "step": step}
    # freie Skala (Preis-Level, Volumen, …): Fenster um den Wert
    if not value:
        return None
    span = abs(value) * _WINDOW
    step = round(span / 10, 6) or 0.0001
    return {"value": value, "min": _round_step(value - span, step),
            "max": _round_step(value + span, step), "step": step}


def used_indicators(definition: Dict) -> set:
    used = set()
    for side in ("long_rules", "short_rules"):
        for r in definition.get(side) or []:
            if not isinstance(r, dict):
                continue
            used.add(canonical_indicator(r.get("indicator")))
            v = r.get("value")
            if isinstance(v, str):
                used.add(canonical_indicator(v))
    return used


def rule_param_key(side: str, index: int) -> str:
    """Parameter-Name einer Regel-Schwelle (stabil, ohne Sonderzeichen)."""
    return f"{'long' if side == 'long_rules' else 'short'}{index + 1}_value"


def rule_tf_param_key(side: str, index: int) -> str:
    """Parameter-Name des Regel-Timeframe-Overrides (Optimizer, opt-in)."""
    return f"{'long' if side == 'long_rules' else 'short'}{index + 1}_tf"


def rule_timeframe_space(definition: Dict, tf_options: List[str]) -> Dict[str, list]:
    """Suchraum 'Timeframe je Regel' für den Parameter-Optimierer (opt-in).

    Erste Option ist immer der Strategie-TF (= kein Override): Multi-Timeframe
    wird nie erzwungen, sondern nur behalten, wenn es den Score verbessert."""
    base_tf = definition.get("timeframe") or "1m"
    if base_tf not in TIMEFRAMES:
        base_tf = "1m"
    opts = [base_tf] + [t for t in (tf_options or [])
                        if valid_rule_tf(t, base_tf)
                        and tf_minutes(t) != tf_minutes(base_tf)]
    if len(opts) < 2:
        return {}
    space: Dict[str, list] = {}
    for side in ("long_rules", "short_rules"):
        for i, r in enumerate(definition.get(side) or []):
            if isinstance(r, dict):
                space[rule_tf_param_key(side, i)] = list(opts)
    return space


def build_param_meta(definition: Dict) -> Dict[str, Dict]:
    """``DEFAULT_PARAMS``-kompatibler Suchraum einer Custom-Definition.

    Format wie bei den eingebauten Strategien:
    ``{key: {"value", "min", "max", "step", "label"}}`` – dadurch funktionieren
    Optimizer (``strategy_param_space``), Regime-Optimierer und die UI ohne
    Sonderfall-Code.
    """
    ind_cfg = definition.get("indicators") or {}
    used = used_indicators(definition)
    meta: Dict[str, Dict] = {}

    # a) Perioden der wirklich genutzten Indikatoren
    for ind in sorted(used):
        for key in INDICATOR_PERIODS.get(ind, []):
            if key in meta or key not in PERIOD_RANGES:
                continue
            default, lo, hi, step, label = PERIOD_RANGES[key]
            try:
                cur = float(ind_cfg.get(key, default) or default)
            except (TypeError, ValueError):
                cur = float(default)
            cur = _clamp(cur, lo, hi)
            meta[key] = {"value": int(cur) if float(step).is_integer() and float(cur).is_integer() else cur,
                         "min": lo, "max": hi, "step": step, "label": label}

    # b) numerische Schwellen der Regeln (das eigentliche "Herz" einer KI-Regel)
    for side in ("long_rules", "short_rules"):
        for i, r in enumerate(definition.get(side) or []):
            if not isinstance(r, dict):
                continue
            v = r.get("value")
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            ind = canonical_indicator(r.get("indicator"))
            tm = _threshold_meta(ind, float(v))
            if not tm:
                continue
            key = rule_param_key(side, i)
            label = (r.get("label")
                     or f"{'LONG' if side == 'long_rules' else 'SHORT'} {i + 1}: "
                        f"{ind} {r.get('op')} …")
            meta[key] = {**tm, "label": label}

    if len(meta) > MAX_PARAMS:
        # Perioden zuerst behalten (stabilere Wirkung), dann Schwellen
        periods = [k for k in meta if k in PERIOD_RANGES]
        thresholds = [k for k in meta if k not in PERIOD_RANGES]
        keep = (periods + thresholds)[:MAX_PARAMS]
        meta = {k: meta[k] for k in keep}
    return meta


def rule_text(rule: Dict) -> str:
    """Kompakte, lesbare Regel-Darstellung („rsi < 35") für UI-Vergleiche."""
    if not isinstance(rule, dict):
        return str(rule)
    val = rule.get("value")
    if isinstance(val, float):
        val = round(val, 4)
    return f"{rule.get('indicator')} {rule.get('op')} {val}" \
        + (f" @{rule['timeframe']}" if rule.get("timeframe") else "")


def apply_params(definition: Dict, params: Dict) -> Dict:
    """Optimierte Parameter in eine Definition schreiben (neue Kopie).

    Nutzen: Live-Pfad (``CustomStrategy.analyze``) und Fast-Path
    (``fast_sim.build_signal_provider``) rechnen damit garantiert identisch.
    """
    if not params:
        return definition
    out = dict(definition)
    ind = dict(out.get("indicators") or {})
    for key in PERIOD_RANGES:
        if key in params and params[key] is not None:
            ind[key] = params[key]
    out["indicators"] = ind
    for side in ("long_rules", "short_rules"):
        rules = out.get(side) or []
        if not rules:
            continue
        base_tf = out.get("timeframe") or "1m"
        if base_tf not in TIMEFRAMES:
            base_tf = "1m"
        new_rules = []
        for i, r in enumerate(rules):
            if not isinstance(r, dict):
                new_rules.append(r)
                continue
            nr = dict(r)
            key = rule_param_key(side, i)
            if key in params and params[key] is not None \
                    and isinstance(nr.get("value"), (int, float)) \
                    and not isinstance(nr.get("value"), bool):
                nr["value"] = params[key]
            tf_key = rule_tf_param_key(side, i)
            if tf_key in params and params[tf_key] is not None:
                ntf = normalize_rule_tf(params[tf_key])
                if ntf and valid_rule_tf(ntf, base_tf):
                    if tf_minutes(ntf) == tf_minutes(base_tf):
                        nr.pop("timeframe", None)
                    else:
                        nr["timeframe"] = ntf
            new_rules.append(nr)
        out[side] = new_rules
    return out
