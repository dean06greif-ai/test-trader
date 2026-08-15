"""Erweiterte Regel-Auswertung für Custom-/KI-Strategien.

Ergänzt das feste Indikator-Vokabular um:
  1. Dynamische Indikatoren mit beliebiger Periode: ``ema(200)``, ``rsi(7)``,
     ``atr(20)``, ``recent_high(20)`` … (KI-Schreibweisen wie ``ema_200`` oder
     ``low_20`` werden kanonisiert).
  2. Zeit-Filter: Indikator ``hour`` (Stunde 0-23, Europe/Berlin) mit
     ``in_range`` / ``not_in_range`` (z.B. value ``[9, 17]`` oder ``"9-17"``).
  3. Mathematische Ausdrücke als Wert oder linke Seite: ``atr_pct * price``,
     ``bb_middle + 2 * atr`` (nur +, -, *, /, Klammern, Zahlen, Indikatoren).
  4. Auto-Fix-Vorschläge für weiterhin nicht auswertbare Regeln (Fuzzy-Match).

Rein & ohne Seiteneffekte – von custom_params (Normalisierung), fast_sim
(Backtest-Fast-Path) und custom_strategy (Live-Pfad) gemeinsam genutzt.
"""
import re
from difflib import get_close_matches
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

# Operatoren zusätzlich zum Basis-Set (<, >, <=, >=, cross_above, cross_below)
EXTRA_OPERATORS = ["==", "!=", "in_range", "not_in_range"]
RANGE_OPS = {"in_range", "not_in_range"}

# Dynamische Basis-Indikatoren -> kanonischer Berechnungs-Name (fast_sim._compute)
DYNAMIC_BASES = {
    "ema": "ema", "sma": "sma", "ma": "sma",
    "rsi": "rsi", "atr": "atr", "atr_pct": "atr_pct",
    "cci": "cci", "adx": "adx",
    "high": "recent_high", "hh": "recent_high", "highest": "recent_high",
    "recent_high": "recent_high", "swing_high": "recent_high",
    "low": "recent_low", "ll": "recent_low", "lowest": "recent_low",
    "recent_low": "recent_low", "swing_low": "recent_low",
    "donchian_high": "donchian_high", "donchian_low": "donchian_low",
    "volume_sma": "volume_sma", "vol_sma": "volume_sma",
    "change": "price_change_pct", "roc": "price_change_pct",
    "price_change_pct": "price_change_pct",
}

_DYN_RE = re.compile(r"^([a-z][a-z_]*?)[_(](\d{1,4})\)?$")

TIME_INDICATOR = "hour"
_TIME_NAMES = {"time", "hour", "hour_of_day", "hour_utc", "session_hour",
               "uhrzeit", "stunde", "time_of_day"}


def normalize_name(name) -> str:
    return str(name or "").strip().lower().replace(" ", "_").replace("-", "_")


def parse_dynamic(name: str) -> Optional[Tuple[str, int]]:
    """"ema(200)" / "ema_200" / "low_20" -> ("ema", 200) / ("recent_low", 20)."""
    if not isinstance(name, str):
        return None
    m = _DYN_RE.match(name.strip().lower())
    if not m:
        return None
    base = DYNAMIC_BASES.get(m.group(1).rstrip("_"))
    if not base:
        return None
    period = int(m.group(2))
    if not 1 <= period <= 2000:
        return None
    return base, period


def dynamic_name(base: str, period: int) -> str:
    return f"{base}({period})"


def canonical_token(name, known_indicators, aliases: Dict[str, str]) -> Tuple[Optional[str], str]:
    """Einzelnen Indikator-Token kanonisieren.

    Rückgabe (canonical, kind) mit kind in known|dynamic|hour|unknown."""
    key = normalize_name(name)
    if key in _TIME_NAMES:
        return TIME_INDICATOR, "hour"
    key = aliases.get(key, key)
    if key in known_indicators:
        return key, "known"
    dyn = parse_dynamic(key)
    if dyn:
        return dynamic_name(*dyn), "dynamic"
    return None, "unknown"


# ------------------------------------------------------------------------- #
#  Mathematische Ausdrücke (Shunting-Yard -> RPN)
# ------------------------------------------------------------------------- #
_TOK_RE = re.compile(
    r"\s*(?:(?P<num>\d+\.?\d*(?:[eE][+-]?\d+)?)"
    r"|(?P<name>[a-zA-Z_][a-zA-Z0-9_]*(?:\(\d{1,4}\))?)"
    r"|(?P<op>[()+\-*/]))")

_PREC = {"+": 1, "-": 1, "*": 2, "/": 2, "neg": 3}


def looks_like_expression(s) -> bool:
    return isinstance(s, str) and bool(re.search(r"[+*/()]| - |\d\s*-\s*[a-zA-Z(]|[a-zA-Z)]\s*-\s*", s))


def tokenize_expression(s: str) -> Optional[List[Tuple[str, str]]]:
    """Liste (typ, wert) mit typ num|name|op oder None bei Syntaxfehler."""
    out, pos = [], 0
    s = s.strip()
    while pos < len(s):
        m = _TOK_RE.match(s, pos)
        if not m or m.end() == pos:
            return None
        if m.group("num"):
            out.append(("num", m.group("num")))
        elif m.group("name"):
            out.append(("name", m.group("name")))
        else:
            out.append(("op", m.group("op")))
        pos = m.end()
    return out or None


def canonicalize_expression(s: str, known_indicators, aliases: Dict[str, str]
                            ) -> Tuple[Optional[str], List[str]]:
    """Ausdruck mit kanonischen Indikator-Namen + Liste unbekannter Tokens."""
    toks = tokenize_expression(s)
    if not toks:
        return None, [str(s)]
    unknown, parts = [], []
    for typ, val in toks:
        if typ == "name":
            canon, kind = canonical_token(val, known_indicators, aliases)
            if kind == "unknown":
                unknown.append(val)
                parts.append(val)
            else:
                parts.append(canon)
        else:
            parts.append(val)
    expr = " ".join(parts).replace("( ", "(").replace(" )", ")")
    return expr, unknown


def parse_expression(s: str) -> Optional[List[Tuple[str, object]]]:
    """Kanonischer Ausdruck -> RPN [("num", f) | ("ind", name) | ("op", o)]."""
    toks = tokenize_expression(s)
    if not toks:
        return None
    output: List[Tuple[str, object]] = []
    stack: List[str] = []
    prev = None  # None | "value" | "op"
    for typ, val in toks:
        if typ == "num":
            output.append(("num", float(val)))
            prev = "value"
        elif typ == "name":
            output.append(("ind", val))
            prev = "value"
        elif val == "(":
            stack.append(val)
            prev = "op"
        elif val == ")":
            while stack and stack[-1] != "(":
                output.append(("op", stack.pop()))
            if not stack:
                return None
            stack.pop()
            prev = "value"
        else:  # binärer Operator oder unäres Minus
            op = val
            if op == "-" and prev != "value":
                op = "neg"
            while stack and stack[-1] != "(" and _PREC.get(stack[-1], 0) >= _PREC[op]:
                output.append(("op", stack.pop()))
            stack.append(op)
            prev = "op"
    while stack:
        if stack[-1] == "(":
            return None
        output.append(("op", stack.pop()))
    return output or None


def eval_rpn(rpn: List[Tuple[str, object]],
             getter: Callable[[str], np.ndarray]) -> Optional[np.ndarray]:
    """RPN auf numpy-Serien auswerten (getter liefert Serie je Indikator)."""
    stack: List[np.ndarray] = []
    try:
        for typ, val in rpn:
            if typ == "num":
                stack.append(np.float64(val))
            elif typ == "ind":
                arr = getter(val)
                if arr is None:
                    return None
                stack.append(np.asarray(arr, dtype=float))
            else:
                if val == "neg":
                    stack.append(-stack.pop())
                    continue
                b, a = stack.pop(), stack.pop()
                with np.errstate(invalid="ignore", divide="ignore"):
                    if val == "+":
                        out = a + b
                    elif val == "-":
                        out = a - b
                    elif val == "*":
                        out = a * b
                    else:
                        out = np.where(b != 0, a / np.where(b != 0, b, 1), np.nan)
                stack.append(out)
        if len(stack) != 1:
            return None
        res = stack[0]
        if np.isscalar(res) or getattr(res, "ndim", 1) == 0:
            return None  # reiner Zahlen-Ausdruck ist kein Indikator
        res = np.asarray(res, dtype=float)
        res[~np.isfinite(res)] = np.nan
        return res
    except (IndexError, TypeError, ValueError):
        return None


# ------------------------------------------------------------------------- #
#  Bereichs-Werte für in_range / not_in_range
# ------------------------------------------------------------------------- #
_RANGE_STR_RE = re.compile(
    r"^\s*(-?\d+\.?\d*)\s*(?:-|\.\.|–|to|bis|,)\s*(-?\d+\.?\d*)\s*$", re.I)


def parse_range(v) -> Optional[Tuple[float, float]]:
    try:
        if isinstance(v, (list, tuple)) and len(v) == 2:
            return float(v[0]), float(v[1])
        if isinstance(v, dict):
            lo = v.get("min", v.get("start", v.get("from")))
            hi = v.get("max", v.get("end", v.get("to")))
            if lo is not None and hi is not None:
                return float(lo), float(hi)
        if isinstance(v, str):
            m = _RANGE_STR_RE.match(v)
            if m:
                return float(m.group(1)), float(m.group(2))
    except (TypeError, ValueError):
        pass
    return None


def range_condition(x: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """Fenster inkl. über Mitternacht (lo > hi, z.B. Stunden 22-6)."""
    with np.errstate(invalid="ignore"):
        if lo <= hi:
            return (x >= lo) & (x <= hi)
        return (x >= lo) | (x <= hi)


# ------------------------------------------------------------------------- #
#  Auto-Fix-Vorschläge
# ------------------------------------------------------------------------- #
def _vocabulary(known_indicators, aliases) -> List[str]:
    vocab = list(known_indicators) + [TIME_INDICATOR]
    vocab += [k for k in aliases]
    vocab += [f"{b}(14)" for b in ("ema", "sma", "rsi", "atr", "cci", "adx")]
    return vocab


def suggest_indicator(name: str, known_indicators, aliases) -> Optional[str]:
    """Bester Ersatz für einen unbekannten Indikator-Token (oder None)."""
    key = normalize_name(name)
    dyn = _DYN_RE.match(key)
    if dyn:  # Periode erhalten, nur Basis fuzzy-matchen: "emaa_200" -> "ema(200)"
        base = get_close_matches(dyn.group(1).rstrip("_"), list(DYNAMIC_BASES), n=1, cutoff=0.75)
        if base:
            return dynamic_name(DYNAMIC_BASES[base[0]], int(dyn.group(2)))
    hits = get_close_matches(key, _vocabulary(known_indicators, aliases), n=1, cutoff=0.6)
    if not hits:
        return None
    canon, kind = canonical_token(hits[0], known_indicators, aliases)
    return canon if kind != "unknown" else None


def suggest_operator(op: str, known_operators, op_aliases) -> Optional[str]:
    key = str(op or "").strip().lower()
    pool = list(known_operators) + list(op_aliases)
    hits = get_close_matches(key, pool, n=1, cutoff=0.55)
    if not hits:
        return None
    return op_aliases.get(hits[0], hits[0])


def fix_expression(expr: str, known_indicators, aliases) -> Optional[str]:
    """Multi-Step-Fix: jeden unbekannten Token im Ausdruck einzeln ersetzen."""
    toks = tokenize_expression(expr)
    if not toks:
        return None
    parts, changed = [], False
    for typ, val in toks:
        if typ == "name":
            canon, kind = canonical_token(val, known_indicators, aliases)
            if kind == "unknown":
                repl = suggest_indicator(val, known_indicators, aliases)
                if not repl:
                    return None
                parts.append(repl)
                changed = True
            else:
                parts.append(canon)
        else:
            parts.append(val)
    if not changed:
        return None
    return " ".join(parts).replace("( ", "(").replace(" )", ")")
