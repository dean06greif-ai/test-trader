"""
Strategie-Optimizer:
- mode "params":    Random-Search ODER Bayes'sche Optimierung (TPE) über
                    Strategie- und Trade-Parameter
- mode "discovery": Greedy Indikator-Discovery (Regel für Regel hinzufügen,
                    nur behalten wenn Score sich verbessert) – optional
                    ausgehend von einer bestehenden Custom-Strategie
- mode "combo":     Discovery + anschließendes Feintuning der Schwellenwerte
"""
import asyncio
import copy
import gc
import logging
import math
import random
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, List

import aiohttp

from services.backtester import JobCancelled, fetch_history, simulate_pair
from services.timeframes import TIMEFRAMES, aggregate_candles, rule_tf_options
from services import fast_sim
from services import robustness
from strategies.custom_strategy import CustomStrategy

logger = logging.getLogger(__name__)

JOBS: Dict[str, Dict] = {}

TRADE_SPACES = {
    "tpsl": {
        "tp1_crv": [0.5, 0.75, 1.0, 1.5, 2.0, 2.5, 3.0],
        "tp_full_crv": [1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0],
        "sl_lookback": [5, 8, 10, 14, 20, 30],
        "tp1_close_percent": [30, 40, 50, 60, 70],
        "sl_mode": ["structure", "atr", "fixed"],
        "sl_fixed_percent": [0.5, 0.8, 1.0, 1.5, 2.0],
        "atr_sl_multiplier": [0.8, 1.0, 1.2, 1.5, 2.0],
    },
    "breakeven": {
        "be_mode": ["off", "tp1", "crv", "profit_pct"],
        "be_trigger_crv": [0.5, 1.0, 1.5, 2.0],
        "be_trigger_profit_pct": [15, 30, 50, 80],
    },
    "profit_secure": {
        "profit_secure_enabled": [False, True],
        "profit_secure_trigger_pct": [20, 30, 50, 80],
        "profit_lock_pct": [30, 50, 70],
    },
    "leverage": {
        "leverage": [3, 5, 10, 15, 20, 28, 40, 50],
    },
    "auto_leverage": {
        "auto_leverage_enabled": [False, True],
        "auto_lev_mode": ["liq_pct", "liq_ticks"],
        "auto_lev_value": [0.1, 0.25, 0.5, 1.0, 3, 5, 10],
        "auto_lev_max": [25, 50, 75, 100],
    },
    "sessions": {
        "sessions": ["", "07:00-22:00", "09:00-12:00", "15:30-18:30",
                     "09:00-12:00,15:30-18:30", "13:00-22:00", "22:00-06:00"],
    },
}

# Back-Compat: alter Gesamtraum
TRADE_PARAM_SPACE = TRADE_SPACES["tpsl"]


def build_trade_space(flags: Dict) -> Dict:
    space = {}
    for group, on in (flags or {}).items():
        if on and group in TRADE_SPACES:
            space.update(TRADE_SPACES[group])
    return space


def create_job(params: Dict) -> str:
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"id": job_id, "status": "running", "progress": 0,
                    "phase": "Startet", "params": params, "best": None, "cancel": False,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "result": None, "error": None}
    if len(JOBS) > 10:
        for k in list(JOBS.keys())[:-10]:
            JOBS.pop(k, None)
    return job_id


def _score(m: Dict, objective: str, min_trades: int, dd_max_pct: float = None) -> float:
    trades = m.get("trades", 0)
    if trades < min_trades:
        return -1e9 + trades
    if dd_max_pct is not None and not robustness.dd_check(m, dd_max_pct)[0]:
        # Drawdown-Filter: aussortieren, aber Reihenfolge unter den Ausgefallenen behalten
        return -5e8 + (m.get("pnl", 0.0) or 0.0)
    wr = m.get("win_rate", 0.0)
    pnl = m.get("pnl", 0.0)
    if objective == "win_rate":
        return wr * 1000 + pnl
    if objective == "pnl":
        return pnl
    return pnl * (0.5 + wr / 100.0)


def _evaluate(strategy, histories: Dict[str, List[Dict]], settings: Dict, cfg: Dict,
              fs_map: Dict = None, should_stop=None) -> Dict:
    agg = {"trades": 0, "wins": 0, "losses": 0, "breakevens": 0,
           "pnl": 0.0, "fees": 0.0, "max_drawdown": 0.0}
    for sym, candles in histories.items():
        provider = None
        if fs_map is not None:
            try:
                provider = fast_sim.provider_for(strategy, fs_map[sym], settings, sym)
            except Exception:
                provider = None
        r = simulate_pair(strategy, candles, sym, settings, cfg,
                          should_stop=should_stop, signal_provider=provider)
        for k in agg:
            agg[k] += r.get(k, 0) or 0
    decided = agg["wins"] + agg["losses"]
    agg["win_rate"] = round(agg["wins"] / decided * 100, 1) if decided else 0.0
    agg["pnl"] = round(agg["pnl"], 2)
    agg["fees"] = round(agg["fees"], 2)
    agg["max_drawdown"] = round(agg["max_drawdown"], 2)
    agg["avg_pnl"] = round(agg["pnl"] / agg["trades"], 3) if agg["trades"] else 0.0
    _cap = float(cfg.get("max_capital", 100) or 100)
    agg["pnl_pct"] = round(agg["pnl"] / _cap * 100, 1)
    agg["max_drawdown_pct"] = round(agg["max_drawdown"] / _cap * 100, 1)
    return agg


# ---------------- Kandidaten-Regeln für Discovery ----------------
def build_candidates(allowed: List[str] = None, tf_options: List[str] = None) -> List[Dict]:
    def ok(ind):
        return not allowed or ind in allowed

    C = []

    def add(ind, label, long_rule, short_rule):
        if ok(ind):
            C.append({"ind": ind, "label": label, "long": long_rule, "short": short_rule})

    for t in (25, 30, 35, 40):
        add("rsi", f"RSI < {t} / > {100 - t}",
            {"indicator": "rsi", "op": "<", "value": t},
            {"indicator": "rsi", "op": ">", "value": 100 - t})
    add("ema_slow", "Trend: Preis vs. EMA slow",
        {"indicator": "price", "op": ">", "value": "ema_slow"},
        {"indicator": "price", "op": "<", "value": "ema_slow"})
    add("ema_fast", "Trend: Preis vs. EMA fast",
        {"indicator": "price", "op": ">", "value": "ema_fast"},
        {"indicator": "price", "op": "<", "value": "ema_fast"})
    add("ema_fast", "EMA fast vs. EMA slow",
        {"indicator": "ema_fast", "op": ">", "value": "ema_slow"},
        {"indicator": "ema_fast", "op": "<", "value": "ema_slow"})
    add("macd_hist", "MACD Momentum (Histogramm)",
        {"indicator": "macd_hist", "op": ">", "value": 0},
        {"indicator": "macd_hist", "op": "<", "value": 0})
    add("macd", "MACD Cross",
        {"indicator": "macd", "op": "cross_above", "value": "macd_signal"},
        {"indicator": "macd", "op": "cross_below", "value": "macd_signal"})
    add("bb_lower", "Bollinger Reversion",
        {"indicator": "price", "op": "<", "value": "bb_lower"},
        {"indicator": "price", "op": ">", "value": "bb_upper"})
    add("bb_upper", "Bollinger Breakout (Cross)",
        {"indicator": "price", "op": "cross_above", "value": "bb_upper"},
        {"indicator": "price", "op": "cross_below", "value": "bb_lower"})
    for t in (20, 30):
        add("stoch_k", f"Stochastik < {t} / > {100 - t}",
            {"indicator": "stoch_k", "op": "<", "value": t},
            {"indicator": "stoch_k", "op": ">", "value": 100 - t})
    add("stoch_k", "Stochastik Cross",
        {"indicator": "stoch_k", "op": "cross_above", "value": "stoch_d"},
        {"indicator": "stoch_k", "op": "cross_below", "value": "stoch_d"})
    add("vwap", "VWAP Trend",
        {"indicator": "price", "op": ">", "value": "vwap"},
        {"indicator": "price", "op": "<", "value": "vwap"})
    add("vwap", "VWAP Reversion",
        {"indicator": "price", "op": "<", "value": "vwap"},
        {"indicator": "price", "op": ">", "value": "vwap"})
    for t in (1.2, 1.5, 2.0):
        add("rel_volume", f"Rel. Volumen > {t}",
            {"indicator": "rel_volume", "op": ">", "value": t},
            {"indicator": "rel_volume", "op": ">", "value": t})
    # Heikin-Ashi wurde als Discovery-Kandidat ENTFERNT: HA ist nur eine
    # geglättete Kerzen-Darstellung, die Farbe dupliziert das Momentum-Signal
    # (price_change_pct) mit Verzögerung. Bestehende Strategien mit ha_color
    # funktionieren weiter – der Indikator wird nur nicht mehr neu vorgeschlagen.
    for t in (0.2, 0.5):
        add("price_change_pct", f"Momentum > {t}%",
            {"indicator": "price_change_pct", "op": ">", "value": t},
            {"indicator": "price_change_pct", "op": "<", "value": -t})
    for t in (0.3, 0.8):
        add("bb_width_pct", f"BB Breite > {t}%",
            {"indicator": "bb_width_pct", "op": ">", "value": t},
            {"indicator": "bb_width_pct", "op": ">", "value": t})
    for t in (0.05, 0.15):
        add("atr_pct", f"ATR > {t}% vom Preis",
            {"indicator": "atr_pct", "op": ">", "value": t},
            {"indicator": "atr_pct", "op": ">", "value": t})
    # --- Etappe 5: Struktur / Liquidität / Trendkanal / Range / Events ---
    add("market_structure", "Markt-Struktur (HH/HL vs. LH/LL)",
        {"indicator": "market_structure", "op": ">=", "value": 1},
        {"indicator": "market_structure", "op": "<=", "value": -1})
    add("bos_up", "Struktur-Bruch (BOS)",
        {"indicator": "bos_up", "op": ">=", "value": 1},
        {"indicator": "bos_dn", "op": ">=", "value": 1})
    for t in (1.0, 2.0):
        add("dist_support_pct", f"Nahe Support/Widerstand (< {t}%)",
            {"indicator": "dist_support_pct", "op": "<", "value": t},
            {"indicator": "dist_resistance_pct", "op": "<", "value": t})
    add("dist_ema200_pct", "Trend: Preis vs. EMA 200",
        {"indicator": "dist_ema200_pct", "op": ">", "value": 0},
        {"indicator": "dist_ema200_pct", "op": "<", "value": 0})
    for t in (15, 25):
        add("channel_pos", f"Trendkanal-Reversion ({t}/{100 - t})",
            {"indicator": "channel_pos", "op": "<", "value": t},
            {"indicator": "channel_pos", "op": ">", "value": 100 - t})
    add("channel_slope_pct", "Trendkanal-Richtung",
        {"indicator": "channel_slope_pct", "op": ">", "value": 0},
        {"indicator": "channel_slope_pct", "op": "<", "value": 0})
    for t in (20, 30):
        add("range_pos", f"Range-Trading ({t}/{100 - t})",
            {"indicator": "range_pos", "op": "<", "value": t},
            {"indicator": "range_pos", "op": ">", "value": 100 - t})
    add("liq_sweep_low", "Liquidity Grab (Sweep + Rückkehr)",
        {"indicator": "liq_sweep_low", "op": ">=", "value": 1},
        {"indicator": "liq_sweep_high", "op": ">=", "value": 1})
    for t in (0.5, 1.0):
        add("eq_low_dist_pct", f"Nahe Equal Lows/Highs (< {t}%)",
            {"indicator": "eq_low_dist_pct", "op": "<", "value": t},
            {"indicator": "eq_high_dist_pct", "op": "<", "value": t})
    add("days_to_fomc", "FOMC-Filter (kein Einstieg am Meeting-Tag)",
        {"indicator": "fomc_today", "op": "<=", "value": 0},
        {"indicator": "fomc_today", "op": "<=", "value": 0})
    # Multi-Timeframe-Varianten (opt-in): jeder Kandidat zusätzlich mit
    # Regel-Timeframe-Override auf höheren TFs (z.B. Trend-Filter auf 1h).
    if tf_options:
        base_c = list(C)
        for tf in tf_options:
            for c in base_c:
                C.append({"ind": c["ind"], "label": f"{c['label']} @{tf}",
                          "timeframe": tf,
                          "long": {**c["long"], "timeframe": tf},
                          "short": {**c["short"], "timeframe": tf}})
    return C


def _mk_strategy(definition: Dict) -> CustomStrategy:
    return CustomStrategy({**definition, "id": definition.get("id") or "opt_eval"})


async def _evaluate_batch(job, pool, items, histories, fs_map, should_stop):
    """items: Liste (strategy, settings, cfg) -> Metriken in gleicher Reihenfolge.
    pool=None -> sequenziell (exakt wie bisher); sonst Prozess-Pool (Multi-Core).
    Zählt Evaluierungen + CPU-/Wall-Zeit in job['_bench'] (Benchmark-Statistik)."""
    b = job.setdefault("_bench", {"evaluations": 0, "cpu_seconds": 0.0,
                                  "sim_seconds": 0.0})
    t0 = time.perf_counter()
    if pool is None:
        out = []
        for st, s, c in items:
            _t = time.perf_counter()
            out.append(await asyncio.to_thread(_evaluate, st, histories, s, c,
                                               fs_map, should_stop))
            b["cpu_seconds"] += time.perf_counter() - _t
    else:
        from services import parallel_sim
        loop = asyncio.get_running_loop()
        futs = [loop.run_in_executor(pool, parallel_sim.evaluate_task_timed,
                                     parallel_sim.strategy_spec(st),
                                     list(histories.keys()), s, c)
                for st, s, c in items]
        timed = await asyncio.gather(*futs)
        out = [m for m, _ in timed]
        b["cpu_seconds"] += sum(d for _, d in timed)
    b["sim_seconds"] += time.perf_counter() - t0
    b["evaluations"] += len(items)
    return out


def _labels(definition: Dict) -> Dict:
    s = _mk_strategy(definition)
    return {"long": [s._auto_label(r) for r in definition.get("long_rules", [])],
            "short": [s._auto_label(r) for r in definition.get("short_rules", [])]}


# ---------------- Bayes (TPE-lite) ----------------
def _tpe_suggest(space: Dict[str, List], history: List[Dict], rng: random.Random,
                 n_candidates: int = 24, gamma: float = 0.25) -> Dict:
    """Tree-structured Parzen Estimator über diskrete Parameter-Räume.
    history: [{"flat": {k: v}, "score": float}]"""
    if len(history) < 8:
        return {k: rng.choice(v) for k, v in space.items()}
    ranked = sorted(history, key=lambda x: -x["score"])
    n_good = max(2, int(len(ranked) * gamma))
    good, bad = ranked[:n_good], ranked[n_good:]

    def dens(obs, value, values):
        cnt = sum(1 for o in obs if o["flat"].get(k) == value)
        return (cnt + 1.0) / (len(obs) + len(values))

    best_cand, best_ratio = None, -1e18
    for _ in range(n_candidates):
        cand = {}
        for k, values in space.items():
            if rng.random() < 0.8:
                # aus der "guten" Verteilung ziehen
                weights = [sum(1 for o in good if o["flat"].get(k) == v) + 1.0 for v in values]
                cand[k] = rng.choices(values, weights=weights)[0]
            else:
                cand[k] = rng.choice(values)
        ratio = 0.0
        for k, values in space.items():
            ratio += math.log(dens(good, cand[k], values)) - math.log(dens(bad, cand[k], values) if bad else 1.0)
        if ratio > best_ratio:
            best_ratio, best_cand = ratio, cand
    return best_cand


def strategy_param_space(strategy, max_values: int = 60,
                         skip_binary: bool = False) -> Dict[str, list]:
    """Suchraum der Strategie-Parameter aus DEFAULT_PARAMS (min/max/step).
    Zu große Räume werden ausgedünnt. Wird vom Optimizer UND vom
    Regime-Optimizer genutzt (eine Quelle der Wahrheit).
    skip_binary=True lässt Ein/Aus-Schalter (0/1) weg – sinnvoll bei kurzen
    Regime-Abschnitten, wo ein zufälliges 'aus' nur Leerläufe erzeugt."""
    space = {}
    for k, mm in (getattr(strategy, "DEFAULT_PARAMS", None) or {}).items():
        try:
            lo, hi = float(mm["min"]), float(mm["max"])
            step = float(mm.get("step") or 1)
            if skip_binary and lo == 0.0 and hi == 1.0 and step == 1.0:
                continue
            vals, v = [], lo
            while v <= hi + 1e-9:
                vals.append(round(v, 4))
                v += step
            if len(vals) > max_values:
                stride = len(vals) // max_values + 1
                vals = vals[::stride]
            if vals:
                space[k] = vals
        except (KeyError, TypeError, ValueError):
            continue
    return space


# ---------------- Modus 1: Parameter-Optimierung ----------------
async def _optimize_params(job, strategy, histories, settings, cfg, objective,
                           min_trades, iterations, trade_space, progress,
                           algorithm="random", fs_map=None, should_stop=None,
                           pool=None, workers=1, dd_max_pct=None,
                           rule_tf_space=None):
    space = strategy_param_space(strategy)
    if rule_tf_space:
        # Regel-Timeframes (Strings) mitoptimieren – opt-in, s. rule_timeframe_space
        space = {**space, **rule_tf_space}
    trade_space = trade_space or {}
    # Flacher Suchraum für Bayes: Strategie-Parameter "p:", Trade-Parameter "t:"
    flat_space = {**{f"p:{k}": v for k, v in space.items()},
                  **{f"t:{k}": v for k, v in trade_space.items()}}
    rng = random.Random()
    base_params = strategy.get_params(settings)
    baseline = (await _evaluate_batch(job, pool, [(strategy, settings, cfg)],
                                      histories, fs_map, should_stop))[0]
    best_score = _score(baseline, objective, min_trades, dd_max_pct)
    best = {"params": {}, "trade_params": {}, "metrics": baseline,
            "score": round(best_score, 3), "is_baseline": True}
    results = []
    history = []
    improvements = []
    algo_tag = "Bayes" if algorithm == "bayes" else "Random"
    it = 0
    batch_size = max(1, int(workers))
    while it < iterations:
        if should_stop and should_stop():
            raise JobCancelled()
        # Kandidaten für den Batch erzeugen (Random: identische Sequenz wie
        # sequenziell; Bayes: Batch-Vorschläge aus der bisherigen Historie)
        flats = []
        for _ in range(min(batch_size, iterations - it)):
            if algorithm == "bayes":
                flats.append(_tpe_suggest(flat_space, history, rng))
            else:
                flats.append({k: rng.choice(v) for k, v in flat_space.items()})
        items, metas = [], []
        for flat in flats:
            p = {k[2:]: v for k, v in flat.items() if k.startswith("p:")}
            tp = {k[2:]: v for k, v in flat.items() if k.startswith("t:")}
            if isinstance(tp.get("tp_full_crv"), (int, float)) and isinstance(tp.get("tp1_crv"), (int, float)) \
                    and tp["tp_full_crv"] < tp["tp1_crv"]:
                tp["tp_full_crv"], tp["tp1_crv"] = tp["tp1_crv"], tp["tp_full_crv"]
            sid = strategy.STRATEGY_ID
            sp = dict(settings.get("strategy_params", {}))
            sp[sid] = {**sp.get(sid, {}), **p}
            items.append((strategy, {**settings, "strategy_params": sp}, {**cfg, **tp}))
            metas.append((flat, p, tp))
        ms = await _evaluate_batch(job, pool, items, histories, fs_map, should_stop)
        for (flat, p, tp), m in zip(metas, ms):
            it += 1
            sc = _score(m, objective, min_trades, dd_max_pct)
            results.append({"params": p, "trade_params": tp, "metrics": m, "score": round(sc, 3)})
            history.append({"flat": flat, "score": sc})
            if sc > best_score:
                best_score = sc
                best = {"params": p, "trade_params": tp, "metrics": m,
                        "score": round(sc, 3), "is_baseline": False}
                job["best"] = best
                improvements.append({"iteration": it, "score": round(sc, 3),
                                     "pnl": m.get("pnl"), "trades": m.get("trades"),
                                     "win_rate": m.get("win_rate")})
            progress(it, iterations,
                     f"{algo_tag} · Kombination {it}/{iterations} · Best Score {round(best_score, 2)}")
    top = sorted(results, key=lambda x: -x["score"])[:10]
    search_stats = {"algorithm": algo_tag, "iterations": iterations,
                    "improvements": improvements[-50:],
                    "improved_n": len(improvements)}
    return {"params": base_params, "metrics": baseline}, best, top, search_stats


# ---------------- Modus 2: Strategie-Discovery ----------------
async def _discover(job, histories, settings, cfg, objective, min_trades,
                    max_rules, allowed, progress, base_definition=None,
                    fs_map=None, should_stop=None, pool=None, workers=1,
                    dd_max_pct=None, tracker=None, tf_options=None):
    cands = build_candidates(allowed, tf_options)
    if not cands:
        raise RuntimeError("Keine Indikatoren ausgewählt")
    if base_definition:
        definition = copy.deepcopy(base_definition)
        definition.setdefault("indicators", {})
        definition.setdefault("long_rules", [])
        definition.setdefault("short_rules", [])
    else:
        definition = {"name": "Discovery", "indicators": {}, "long_rules": [], "short_rules": []}
    used = set()
    best_score = -1e18
    best_metrics = None
    steps = []
    # Basis-Strategie zuerst bewerten (Weiterentwicklung bestehender Strategien)
    if definition["long_rules"] or definition["short_rules"]:
        m0 = (await _evaluate_batch(job, pool, [(_mk_strategy(definition), settings, cfg)],
                                    histories, fs_map, should_stop))[0]
        best_score = _score(m0, objective, min_trades, dd_max_pct)
        best_metrics = m0
        if tracker is not None:
            tracker.add(robustness.rule_key(definition),
                        {"definition": copy.deepcopy(definition), "trade_params": {},
                         "metrics": m0, "score": round(best_score, 3)})
        steps.append({"round": 0, "added": "Basis-Strategie",
                      "score": round(best_score, 3), "metrics": m0})
        job["best"] = {"rules": _labels(definition), "metrics": m0}
    total = len(cands) * max_rules
    done = 0
    chunk_size = max(1, int(workers))
    for round_i in range(max_rules):
        round_best = None
        cand_pool = [c for c in cands if c["label"] not in used]
        done += len(cands) - len(cand_pool)  # übersprungene wie bisher mitzählen
        idx = 0
        while idx < len(cand_pool):
            if should_stop and should_stop():
                raise JobCancelled()
            chunk = cand_pool[idx: idx + chunk_size]
            items, defs = [], []
            for cand in chunk:
                d = {**definition,
                     "long_rules": definition["long_rules"] + [dict(cand["long"])],
                     "short_rules": definition["short_rules"] + [dict(cand["short"])]}
                items.append((_mk_strategy(d), settings, cfg))
                defs.append(d)
            ms = await _evaluate_batch(job, pool, items, histories, fs_map, should_stop)
            for cand, d, m in zip(chunk, defs, ms):
                done += 1
                sc = _score(m, objective, min_trades, dd_max_pct)
                if tracker is not None:
                    tracker.add(robustness.rule_key(d),
                                {"definition": copy.deepcopy(d), "trade_params": {},
                                 "metrics": m, "score": round(sc, 3)})
                progress(done, total, f"Runde {round_i + 1}: teste '{cand['label']}'")
                # Reihenfolge = Kandidaten-Reihenfolge -> identische Greedy-Auswahl
                if round_best is None or sc > round_best[0]:
                    round_best = (sc, cand, m)
            idx += len(chunk)
        if round_best is None:
            break
        sc, cand, m = round_best
        if sc <= best_score + 1e-9:
            steps.append({"round": round_i + 1, "added": None,
                          "info": "Keine Regel verbessert den Score mehr – Stopp"})
            done = (round_i + 1) * len(cands)
            break
        best_score, best_metrics = sc, m
        definition["long_rules"].append(dict(cand["long"]))
        definition["short_rules"].append(dict(cand["short"]))
        used.add(cand["label"])
        steps.append({"round": round_i + 1, "added": cand["label"],
                      "score": round(sc, 3), "metrics": m})
        job["best"] = {"rules": _labels(definition), "metrics": m}
    return definition, best_metrics, best_score, steps


# ---------------- Modus 3: Feintuning der Schwellenwerte ----------------
async def _refine(job, definition, base_score, base_metrics, histories, settings,
                  cfg, objective, min_trades, iterations, progress,
                  fs_map=None, should_stop=None, dd_max_pct=None, tracker=None):
    numeric = [(side, i) for side in ("long_rules", "short_rules")
               for i, r in enumerate(definition.get(side, []))
               if isinstance(r.get("value"), (int, float))]
    if not numeric:
        return definition, base_metrics, []
    best_def, best_score, best_m = definition, base_score, base_metrics
    log = []
    for it in range(iterations):
        if should_stop and should_stop():
            raise JobCancelled()
        side, i = random.choice(numeric)
        d = {**best_def,
             "long_rules": [dict(r) for r in best_def["long_rules"]],
             "short_rules": [dict(r) for r in best_def["short_rules"]]}
        r = d[side][i]
        v = float(r["value"])
        delta = (abs(v) if abs(v) > 0.01 else 1.0) * random.uniform(-0.25, 0.25)
        r["value"] = round(v + delta, 3)
        m = (await _evaluate_batch(job, None, [(_mk_strategy(d), settings, cfg)],
                                   histories, fs_map, should_stop))[0]
        sc = _score(m, objective, min_trades, dd_max_pct)
        if tracker is not None:
            tracker.add(robustness.rule_key(d),
                        {"definition": copy.deepcopy(d), "trade_params": {},
                         "metrics": m, "score": round(sc, 3)})
        progress(it + 1, iterations, f"Feintuning {it + 1}/{iterations}")
        if sc > best_score:
            best_def, best_score, best_m = d, sc, m
            log.append({"iteration": it + 1,
                        "change": f"{r['indicator']} {r['op']} {r['value']}",
                        "score": round(sc, 3), "metrics": m})
            job["best"] = {"rules": _labels(d), "metrics": m}
    return best_def, best_m, log


# ---------------- Trade-Einstellungen für Discovery-Strategien ----------------
async def _optimize_trade_settings(job, definition, base_score, base_metrics,
                                   histories, settings, cfg, objective, min_trades,
                                   iterations, trade_space, progress,
                                   fs_map=None, should_stop=None, pool=None, workers=1,
                                   dd_max_pct=None, tracker=None):
    """Random-Search über Trade-Einstellungen (TP/SL, BE, Gewinnsicherung,
    Hebel, Auto-Leverage, Zeitfenster) für eine (entdeckte) Strategie."""
    rng = random.Random()
    strategy = _mk_strategy(definition)
    best_tp: Dict = {}
    best_score = base_score
    best_m = base_metrics
    it = 0
    batch_size = max(1, int(workers))
    while it < iterations:
        if should_stop and should_stop():
            raise JobCancelled()
        tps = []
        for _ in range(min(batch_size, iterations - it)):
            tp = {k: rng.choice(v) for k, v in trade_space.items()}
            if isinstance(tp.get("tp_full_crv"), (int, float)) and isinstance(tp.get("tp1_crv"), (int, float)) \
                    and tp["tp_full_crv"] < tp["tp1_crv"]:
                tp["tp_full_crv"], tp["tp1_crv"] = tp["tp1_crv"], tp["tp_full_crv"]
            tps.append(tp)
        items = [(strategy, settings, {**cfg, **tp}) for tp in tps]
        ms = await _evaluate_batch(job, pool, items, histories, fs_map, should_stop)
        for tp, m in zip(tps, ms):
            it += 1
            sc = _score(m, objective, min_trades, dd_max_pct)
            if tracker is not None:
                tracker.add(robustness.rule_key(definition, tp),
                            {"definition": copy.deepcopy(definition), "trade_params": dict(tp),
                             "metrics": m, "score": round(sc, 3)})
            progress(it, iterations, f"Trade-Einstellungen {it}/{iterations}")
            if sc > best_score:
                best_score, best_tp, best_m = sc, tp, m
                job["best"] = {"rules": _labels(definition), "metrics": m, "trade_params": tp}
    return best_tp, best_m, best_score


# ---------------- Top-5 + Robustheits-Checks ----------------
async def _finalize_top5(job, mode, candidates, train_hist, test_hist, settings,
                         cfg, robust, fs_map, should_stop, strategy=None,
                         train_days=1.0, test_days=0.0, wf_windows=None):
    """Die besten Kandidaten mit Walk-Forward-Test (single/rolling), Drawdown-
    Filter und Konstanz-Test anreichern und neu sortieren. Läuft immer – ohne
    aktivierte Checks ist es ein reines Top-5-Ranking."""
    if not candidates:
        return []
    rolling = bool(robust["wf_enabled"]
                   and robust.get("wf_mode") in ("rolling", "anchored") and wf_windows)
    anchored = robust.get("wf_mode") == "anchored"
    wf_label = "Anchored" if anchored else "Rolling"
    fs_test = None
    if robust["wf_enabled"] and test_hist and not rolling:
        fs_test = {s: fast_sim.FastSeries(c) for s, c in test_hist.items()}
    # Rolling: FastSeries je Fenster EINMAL bauen und für alle Kandidaten wiederverwenden
    win_fs = []
    if rolling:
        for w in wf_windows:
            win_fs.append({
                "train": {s: fast_sim.FastSeries(c) for s, c in w["train"].items()},
                "test": {s: fast_sim.FastSeries(c) for s, c in w["test"].items()},
            })
    out = []
    n = len(candidates)
    for i, cand in enumerate(candidates):
        if should_stop and should_stop():
            raise JobCancelled()
        job["phase"] = f"Robustheits-Checks: Kandidat {i + 1}/{n}"
        entry = {"metrics": cand.get("metrics"), "score": cand.get("score"),
                 "trade_params": cand.get("trade_params") or {}}
        if mode == "params":
            entry["params"] = cand.get("params") or {}
            st = strategy
            sid = strategy.STRATEGY_ID
            sp = dict(settings.get("strategy_params", {}))
            sp[sid] = {**sp.get(sid, {}), **entry["params"]}
            s_eff = {**settings, "strategy_params": sp}
        else:
            entry["definition"] = cand.get("definition")
            entry["rules"] = _labels(entry["definition"])
            st = _mk_strategy(entry["definition"])
            s_eff = settings
        c_eff = {**cfg, **entry["trade_params"]}
        passed = True
        if robust["dd_enabled"]:
            ok, ratio = robustness.dd_check(entry["metrics"] or {}, robust["dd_max_pct"])
            entry["dd_ratio_pct"] = ratio
            entry["dd_pass"] = ok
            passed = passed and ok
        if rolling:
            # ---- Rolling Walk-Forward: Kandidat über alle Fenster prüfen ----
            win_results = []
            n_win = len(wf_windows)
            for w_i, (win, wfs) in enumerate(zip(wf_windows, win_fs)):
                if should_stop and should_stop():
                    raise JobCancelled()
                job["phase"] = (f"{wf_label} Walk-Forward: Kandidat {i + 1}/{n} · "
                                f"Fenster {w_i + 1}/{n_win} (Test auf unbekannten Daten)")
                # Anchored: Trainingsfenster wächst je Fenster um die Test-Spanne
                td = train_days + (w_i * test_days if anchored else 0.0)
                if w_i == 0:
                    tr_m = entry["metrics"] or {}  # Fenster 1 = Suchdaten (schon bewertet)
                else:
                    tr_m = (await _evaluate_batch(job, None, [(st, s_eff, c_eff)],
                                                  win["train"], wfs["train"], should_stop))[0]
                te_m = (await _evaluate_batch(job, None, [(st, s_eff, c_eff)],
                                              win["test"], wfs["test"], should_stop))[0]
                ev = robustness.walk_forward_eval(tr_m, te_m, td, test_days)
                win_results.append({"window": w_i + 1, "range": win.get("range") or {},
                                    "train_metrics": tr_m, "test_metrics": te_m, **ev})
            entry["wf_windows"] = win_results
            entry["wf"] = robustness.aggregate_rolling(win_results)
            entry["test_metrics"] = robustness.combine_test_metrics(
                [w["test_metrics"] for w in win_results])
            if robust["dd_enabled"]:
                ok_t, ratio_t = robustness.dd_check(entry["test_metrics"], robust["dd_max_pct"])
                entry["wf"]["test_dd_ratio_pct"] = ratio_t
                entry["dd_pass"] = bool(entry.get("dd_pass")) and ok_t
                passed = passed and ok_t
        elif fs_test is not None:
            job["phase"] = (f"Walk-Forward-Test: Kandidat {i + 1}/{n} auf "
                            f"{round(test_days, 1)} Tagen unbekannter Testdaten")
            test_m = (await _evaluate_batch(job, None, [(st, s_eff, c_eff)],
                                            test_hist, fs_test, should_stop))[0]
            entry["test_metrics"] = test_m
            entry["wf"] = robustness.walk_forward_eval(entry["metrics"] or {}, test_m,
                                                       train_days, test_days)
            if robust["dd_enabled"]:
                ok_t, ratio_t = robustness.dd_check(test_m, robust["dd_max_pct"])
                entry["wf"]["test_dd_ratio_pct"] = ratio_t
                entry["dd_pass"] = bool(entry.get("dd_pass")) and ok_t
                passed = passed and ok_t
        # Gemeinsame Trade-Sammlung (EINE Simulation für Konstanz/Monte-Carlo/Regime)
        trades = None
        if robust["ct_enabled"] or robust["mc_enabled"] or robust["rg_enabled"]:
            job["phase"] = f"Trade-Analyse: Kandidat {i + 1}/{n}"
            trades = await robustness.collect_trades_list(
                st, train_hist, s_eff, c_eff, fs_map, should_stop)
        if robust["ct_enabled"]:
            job["phase"] = (f"Konstanz-Test: Kandidat {i + 1}/{n} "
                            f"({robust['ct_chunk_days']}-Tage-Abschnitte)")
            pnls = robustness.chunk_pnls_from_trades(trades, train_hist,
                                                     robust["ct_chunk_days"])
            entry["constancy"] = robustness.evaluate_chunks(pnls, robust["ct_max_dev_pct"])
            passed = passed and entry["constancy"]["passed"]
        # 1. Fee-/Slippage-Stresstest: bleibt der Kandidat mit höheren Kosten profitabel?
        if robust["st_enabled"]:
            job["phase"] = f"Stresstest (Kosten ×{robust['st_mult']}): Kandidat {i + 1}/{n}"
            m_st = (await _evaluate_batch(
                job, None, [(st, s_eff, robustness.stressed_cfg(c_eff, robust["st_mult"]))],
                train_hist, fs_map, should_stop))[0]
            st_ok = float(m_st.get("pnl") or 0) > 0
            entry["stress"] = {"cost_multiplier": robust["st_mult"],
                               "pnl": m_st.get("pnl"), "trades": m_st.get("trades"),
                               "win_rate": m_st.get("win_rate"), "passed": st_ok}
            passed = passed and st_ok
        # 3. Parameter-Stabilität: Schwellen ±X% -> Plateau (robust) oder Spike (Zufall)?
        if robust["sb_enabled"]:
            job["phase"] = f"Stabilitäts-Check (±{robust['sb_var_pct']}%): Kandidat {i + 1}/{n}"
            v = robust["sb_var_pct"] / 100.0
            items = []
            for f in (-v, -v / 2, v / 2, v):
                if mode == "params":
                    p_var = robustness.perturb_params(entry["params"], f)
                    sp2 = dict(s_eff.get("strategy_params", {}))
                    sp2[sid] = {**sp2.get(sid, {}), **p_var}
                    items.append((strategy, {**s_eff, "strategy_params": sp2}, c_eff))
                else:
                    d_var = robustness.perturb_definition(entry["definition"], f)
                    items.append((_mk_strategy(d_var), s_eff, c_eff))
            ms_var = await _evaluate_batch(job, None, items, train_hist, fs_map, should_stop)
            entry["stability"] = robustness.stability_eval(
                float((entry["metrics"] or {}).get("pnl") or 0),
                [m.get("pnl") for m in ms_var], robust["sb_var_pct"])
            passed = passed and entry["stability"]["passed"]
        # 2. Monte-Carlo: Trade-Reihenfolge mischen -> Drawdown-Verteilung
        if robust["mc_enabled"] and trades is not None:
            job["phase"] = f"Monte-Carlo ({robust['mc_runs']} Läufe): Kandidat {i + 1}/{n}"
            entry["monte_carlo"] = robustness.monte_carlo(
                [p for _s, _t, p in trades], robust["mc_runs"], robust["mc_max_dd_pct"])
            passed = passed and entry["monte_carlo"]["passed"]
        # 4. Regime-Aufschlüsselung: PnL je Marktphase (nur Info, kein Filter)
        if robust["rg_enabled"] and trades is not None:
            job["phase"] = f"Regime-Analyse: Kandidat {i + 1}/{n}"
            entry["regimes"] = robustness.regime_breakdown(trades, train_hist)
        if len(train_hist) > 1:
            # Multi-Coin-Check: funktioniert der Kandidat auf jedem Coin einzeln?
            job["phase"] = f"Multi-Coin-Check: Kandidat {i + 1}/{n}"
            per = {}
            for sym, candles in train_hist.items():
                sym_fs = {sym: fs_map[sym]} if fs_map and sym in fs_map else None
                m_s = (await _evaluate_batch(job, None, [(st, s_eff, c_eff)],
                                             {sym: candles}, sym_fs, should_stop))[0]
                per[sym] = {"pnl": m_s.get("pnl"), "trades": m_s.get("trades"),
                            "win_rate": m_s.get("win_rate")}
            entry["per_symbol"] = per
            entry["positive_symbols_pct"] = round(
                sum(1 for v in per.values() if (v.get("pnl") or 0) > 0) / len(per) * 100, 1)
        entry["passed"] = passed
        entry["checks"] = robustness.build_checks_summary(entry, robust)
        entry["fail_reasons"] = robustness.fail_reasons(entry["checks"])
        out.append(entry)
    if robust["wf_enabled"]:
        out.sort(key=lambda e: (not e["passed"],
                                -(e.get("wf") or {}).get("wf_score", -1e18)))
    else:
        out.sort(key=lambda e: (not e["passed"], -(e.get("score") or -1e18)))
    top5 = out[:5]
    for r, e in enumerate(top5):
        e["rank"] = r + 1
        e["rank_reason"] = robustness.rank_reason(e, robust)
    return top5


# ---------------- Trades des besten Kandidaten (Equity + CSV-Export) ----------------
async def _collect_best_trades(job, result, mode, strategy, settings, cfg,
                               histories, tf, should_stop):
    """Beste Konfiguration EINMAL über den gesamten Zeitraum simulieren und alle
    Trades speichern – Equity-Kurve & CSV-Export kommen dann aus diesen Daten
    (kein erneutes Laden/Simulieren beim Anzeigen -> keine Timeouts)."""
    top5 = result.get("top5") or []
    best = top5[0] if top5 else None
    if mode == "params":
        if strategy is None:
            return
        src = best or result.get("best") or {}
        params = src.get("params") or {}
        tp = src.get("trade_params") or {}
        st = strategy
        sid = strategy.STRATEGY_ID
        sp = dict(settings.get("strategy_params", {}))
        sp[sid] = {**sp.get(sid, {}), **params}
        s_eff = {**settings, "strategy_params": sp}
        name = getattr(strategy, "STRATEGY_NAME", sid)
    else:
        definition = (best or {}).get("definition") or result.get("definition")
        if not definition:
            return
        tp = (best or {}).get("trade_params") or result.get("trade_params") or {}
        st = _mk_strategy(definition)
        sid = st.STRATEGY_ID
        s_eff = settings
        name = definition.get("name") or "Entdeckte Strategie"
    c_eff = {**cfg, **tp}
    rows: List[Dict] = []
    syms = list(histories.items())
    for i, (sym, candles) in enumerate(syms):
        if should_stop and should_stop():
            raise JobCancelled()
        job["phase"] = f"Equity/Trades sammeln: {sym} ({i + 1}/{len(syms)})"
        provider = None
        if getattr(st, "IS_CUSTOM", False):
            try:
                provider = fast_sim.build_signal_provider(
                    st.definition, fast_sim.FastSeries(candles))
            except Exception:
                provider = None
        res = await asyncio.to_thread(simulate_pair, st, candles, sym, s_eff, c_eff,
                                      None, True, should_stop, provider)
        for t in (res.get("all_trades") or []):
            rows.append({"strategy_id": sid, "strategy_name": name,
                         "symbol": sym, "timeframe": tf, **t})
        if len(rows) >= 25000:
            break
    job["export_trades"] = rows[:25000]
    result["export_meta"] = {"trades": len(job["export_trades"]),
                             "candidate_rank": (best or {}).get("rank") or 1}


# ---------------- Modus 4: Dynamische Strategie (Regime-basiert) ----------------
async def _run_dynamic(job, body, registry, settings, cfg, robust, full_histories,
                       tf, days, objective, min_trades, iterations, trade_space,
                       should_stop, db=None) -> Dict:
    """Dynamische Strategie: Regime erkennen (ohne Lookahead), pro Regime die
    Trade-Konfiguration optimieren und IMMER gegen die beste statische
    Konfiguration auf unbekannten Testdaten vergleichen."""
    from services import dynamic_strategy as dyn
    from services import regime as rg
    sid = body.get("strategy_id")
    strategy = registry.get(sid)
    if not strategy:
        raise RuntimeError("Strategie nicht gefunden")
    dcfg = body.get("dynamic") or {}
    max_regimes = int(min(max(int(dcfg.get("max_regimes") or 5), 2), 10))
    lookback_days = float(min(max(float(dcfg.get("lookback_days") or 3), 0.5), 30))
    conf_min = float(min(max(float(dcfg.get("confidence_min") or 70), 50), 95)) / 100.0
    min_hold_days = float(min(max(float(dcfg.get("min_hold_days") or 2), 0.25), 30))
    min_share = float(min(max(float(dcfg.get("min_share_pct") or 5), 1), 30))
    rule_variants = bool(dcfg.get("rule_variants"))
    per_regime_strategies = bool(dcfg.get("per_regime_strategies"))
    max_rules_regime = int(min(max(int(dcfg.get("max_rules_per_regime") or 4), 1), 8))
    variant_indicators = [i for i in (body.get("indicators") or []) if isinstance(i, str)]
    # Pro-Regel-Timeframes auch für Discovery/Regel-Varianten im Dynamik-Modus
    rtf = body.get("rule_timeframes") or {}
    tf_options: List[str] = []
    if isinstance(rtf, dict) and rtf.get("enabled"):
        tf_options = [t for t in rule_tf_options(tf, rtf.get("min") or "1m",
                                                 rtf.get("max") or "4h")
                      if TIMEFRAMES.get(t) != TIMEFRAMES.get(tf)]
    train_pct = robust["train_pct"] if robust["wf_enabled"] else 75.0
    if not trade_space:
        trade_space = build_trade_space({"tpsl": True, "leverage": True})

    job["phase"] = "Regime-Erkennung: Marktphasen bestimmen (nur Trainingsdaten)"
    train_hist, test_hist = robustness.split_histories(full_histories, train_pct)
    engine = (dcfg.get("engine") or rg.DEFAULT_ENGINE)
    engine_config = dict(dcfg.get("engine_config") or {})
    engine_config.setdefault("confidence_min", conf_min)
    engine_config.setdefault("min_hold_days", min_hold_days)
    model = rg.detect_regimes(train_hist, tf, max_regimes, lookback_days, min_share,
                              engine=engine, engine_config=engine_config)
    if not model:
        raise RuntimeError("Zu wenig Daten für die Regime-Erkennung – Zeitraum erhöhen")
    # Klassifikation über den GESAMTEN Zeitraum – Features sind rein rückblickend,
    # daher kein Lookahead; der Test-Teil bleibt für die Optimierung unbekannt.
    labels_full = {s: rg.classify_series(model, full_histories[s], tf, conf_min,
                                         min_hold_days) for s in full_histories}
    split_idx = {s: len(train_hist[s]) for s in train_hist}
    train_labels = {s: labels_full[s][:split_idx[s]] for s in labels_full}
    test_labels = {s: labels_full[s][split_idx[s]:] for s in labels_full}
    train_segs = dyn.build_segments(full_histories, train_labels)
    test_segs = dyn.build_segments(full_histories, test_labels, offset_map=split_idx)
    full_segs = dyn.build_segments(full_histories, labels_full)
    stat_test_segs = {}
    for s in full_histories:
        si = split_idx[s]
        if si < len(full_histories[s]):
            stat_test_segs[s] = [{"regime": -1,
                                  "start_ts": full_histories[s][si]["timestamp"],
                                  "candles": full_histories[s][max(si - dyn.WARMUP_BARS, 0):],
                                  "n_bars": len(full_histories[s]) - si}]

    # Walk-Forward INNERHALB jeder Marktphase: die Trainingsabschnitte werden
    # nochmals geteilt. Eine Sub-Strategie/Konfiguration wird nur übernommen,
    # wenn sie auf dem unbekannten Teil DERSELBEN Phase profitabel bleibt.
    regime_wf = dcfg.get("regime_walk_forward", True)
    inner_train, inner_val = (dyn.split_segments(train_segs, dcfg.get("regime_train_pct", 75))
                              if regime_wf else (train_segs, None))

    # Multi-Core: alle Regime-Abschnitte einmalig an die Kind-Prozesse geben.
    # Ohne das lief der komplette Dynamik-Modus auf einem einzigen Kern.
    seg_data = dyn.register_segments(train_segs, test_segs, full_segs,
                                     stat_test_segs, inner_train, inner_val)
    static_keys = {}
    for s, c in train_hist.items():
        static_keys[s] = f"statictrain#{s}"
        seg_data[static_keys[s]] = c
    seg_pool = None
    dyn.reset_bench()
    try:
        from services import parallel_sim
        n_workers = parallel_sim.workers_configured()
        if n_workers > 1:
            seg_pool = parallel_sim.make_pool(seg_data, n_workers)
            dyn.set_pool(seg_pool)
            logger.info(f"Dynamik-Modus: {len(seg_data)} Abschnitte auf "
                        f"{n_workers} Kerne verteilt")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Dynamik-Modus Multi-Core deaktiviert: {e}")
        seg_pool = None
        dyn.set_pool(None)
    try:
        return await _run_dynamic_inner(
            job, dcfg, strategy, sid, settings, cfg, tf, days, objective, min_trades,
            iterations, trade_space, should_stop, db, model, train_hist, full_histories,
            labels_full, split_idx, train_segs, test_segs, full_segs, stat_test_segs,
            inner_train, inner_val, static_keys, max_regimes, lookback_days, conf_min,
            min_hold_days, min_share, train_pct, rule_variants, per_regime_strategies,
            max_rules_regime, variant_indicators, tf_options)
    finally:
        dyn.set_pool(None)
        b = job.setdefault("_bench", {})
        b["sim_seconds"] = b.get("sim_seconds", 0.0) + dyn.BENCH["sim_seconds"]
        b["cpu_seconds"] = b.get("cpu_seconds", 0.0) + dyn.BENCH["cpu_seconds"]
        b["evaluations"] = b.get("evaluations", 0) + dyn.BENCH["evaluations"]
        b["dyn_segments"] = dyn.BENCH["segments"]
        if seg_pool is not None:
            from services import parallel_sim
            parallel_sim.close_pool(seg_pool, kill=bool(should_stop and should_stop()))


async def _run_dynamic_inner(job, dcfg, strategy, sid, settings, cfg, tf, days,
                             objective, min_trades, iterations, trade_space,
                             should_stop, db, model, train_hist, full_histories,
                             labels_full, split_idx, train_segs, test_segs, full_segs,
                             stat_test_segs, inner_train, inner_val, static_keys,
                             max_regimes, lookback_days, conf_min, min_hold_days,
                             min_share, train_pct, rule_variants,
                             per_regime_strategies, max_rules_regime,
                             variant_indicators, tf_options=None) -> Dict:
    from services import dynamic_strategy as dyn
    job["phase"] = "Signal-Vorberechnung je Regime-Abschnitt"
    dyn.prepare_providers(strategy, train_segs, settings)
    dyn.prepare_providers(strategy, test_segs, settings)

    n_regimes = len(model["regimes"])
    min_tr_regime = max(int(min_trades * dyn.MIN_TRADES_PER_REGIME_FACTOR), 3)
    n_cands = len(build_candidates(variant_indicators or None, tf_options or None))
    disc_work = (n_cands * max_rules_regime * n_regimes) if per_regime_strategies else 0
    total_work = (n_regimes + 1) * iterations + disc_work
    done_work = [0]

    def prog(_done):
        done_work[0] += 1
        job["progress"] = 10 + round(done_work[0] / max(total_work, 1) * 80)

    def set_phase(txt):
        job["phase"] = txt

    learn_weights = {}
    if db is not None and (per_regime_strategies or rule_variants):
        from services import learning
        try:
            learn_weights = await learning.indicator_weights(db)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"learning weights failed: {e}")

    base_def_for_discovery = (copy.deepcopy(strategy.definition)
                              if getattr(strategy, "IS_CUSTOM", False)
                              and dcfg.get("start_from_base") else None)

    regime_results = []
    configs: Dict[int, Dict] = {}
    strategies_by_regime: Dict[int, object] = {}
    discovery_info: Dict[str, Dict] = {}
    discovery_note: Dict[str, str] = {}
    for r in model["regimes"]:
        if should_stop and should_stop():
            raise JobCancelled()
        strat_r = strategy
        if per_regime_strategies:
            job["phase"] = (f"Regime {r['id'] + 1}/{n_regimes} ({r['label']}): "
                            f"eigene Strategie suchen")
            disc = await dyn.discover_regime_strategy(
                inner_train, r["id"], settings, cfg, variant_indicators,
                base_def_for_discovery, objective, min_tr_regime,
                max_rules_regime, learn_weights, prog, should_stop,
                val_segments=inner_val, phase_cb=set_phase,
                tf_options=tf_options or None)
            if disc["rules"] and disc.get("definition"):
                strat_r = _mk_strategy(disc["definition"])
                strategies_by_regime[r["id"]] = strat_r
                discovery_info[str(r["id"])] = {
                    "rules": disc["rules"], "steps": disc["steps"],
                    "metrics": disc["metrics"], "validation": disc.get("validation"),
                    "definition": disc["definition"]}
            else:
                discovery_note[str(r["id"])] = disc.get("note") or \
                    "Keine eigene Regel-Kombination gefunden – Basis-Strategie bleibt aktiv"
        job["phase"] = f"Optimiere Regime {r['id'] + 1}/{n_regimes}: {r['label']}"
        res_r = await dyn.optimize_regime(job, strat_r, inner_train, r["id"],
                                          settings, cfg, trade_space, iterations,
                                          objective, min_tr_regime, prog, should_stop,
                                          val_segments=inner_val, phase_cb=set_phase)
        if per_regime_strategies and str(r["id"]) in discovery_info:
            res_r["own_strategy"] = {
                "rules": discovery_info[str(r["id"])]["rules"],
                "validation": discovery_info[str(r["id"])].get("validation")}
        elif per_regime_strategies:
            res_r["own_strategy"] = {"rules": [], "note": discovery_note.get(str(r["id"]))}
        regime_results.append({**r, **res_r})
        if not res_r["insufficient"]:
            configs[r["id"]] = res_r["config"]
        else:
            strategies_by_regime.pop(r["id"], None)
            discovery_info.pop(str(r["id"]), None)
    job["phase"] = "Statische Benchmark: beste Einzel-Konfiguration (Vergleich)"
    static = await dyn.optimize_static(strategy, train_hist, settings, cfg,
                                       trade_space, iterations, objective,
                                       min_trades, prog, should_stop,
                                       data_keys=static_keys)
    # Fallback: Regime ohne genügend Trades nutzen die statische Konfiguration
    for r in regime_results:
        if r["insufficient"]:
            configs[r["regime"]] = static["config"]
            r["fallback"] = True

    # Regel-Varianten je Regime (nur wenn KEINE vollständige Sub-Strategie gesucht
    # wurde): eine zusätzliche Regel testen, Kandidaten nach Lern-Gedächtnis sortiert
    variants_info = {}
    if rule_variants and not per_regime_strategies and getattr(strategy, "IS_CUSTOM", False):
        weights = learn_weights
        for r in regime_results:
            if r.get("insufficient"):
                continue
            job["phase"] = f"Regel-Varianten für Regime {r['regime'] + 1}: {r['label']}"
            var = await dyn.optimize_regime_rules(
                strategy, train_segs, r["regime"], settings, cfg, r["config"],
                variant_indicators, min_tr_regime, r["metrics"], objective,
                weights, 25, None, should_stop, tf_options=tf_options or None)
            if var:
                r["rule_variant"] = {k: var[k] for k in
                                     ("rule_label", "metrics", "score", "improvement_pct")}
                strategies_by_regime[r["regime"]] = _mk_strategy(var["definition"])
                variants_info[str(r["regime"])] = {
                    "rule_label": var["rule_label"], "rule_long": var["rule_long"],
                    "rule_short": var["rule_short"],
                    "improvement_pct": var["improvement_pct"]}
    elif rule_variants:
        variants_info = {"_note": "Regel-Varianten sind nur für Custom-Strategien "
                                  "möglich – Basis-Strategien haben fest programmierte Regeln"}

    job["phase"] = "Out-of-Sample-Test: dynamisch vs. statisch auf unbekannten Daten"
    dyn_train_m, _ = await dyn.eval_dynamic(strategy, train_segs, configs, cfg,
                                            settings, should_stop, strategies_by_regime)
    dyn_test_m, _ = await dyn.eval_dynamic(strategy, test_segs, configs, cfg,
                                           settings, should_stop, strategies_by_regime)
    # Statisch auf Test: gleicher Segment-Mechanismus (fairer Vergleich mit Warmup)
    dyn.prepare_providers(strategy, stat_test_segs, settings)
    stat_test_m, _ = await dyn.eval_dynamic(strategy, stat_test_segs,
                                            {-1: static["config"]}, cfg,
                                            settings, should_stop)
    switches = sum(max(len(segs) - 1, 0) for segs in test_segs.values())
    verdict = dyn.build_verdict(dyn_test_m, stat_test_m, n_regimes, switches)

    # Equity/CSV-Export: dynamischer Verlauf über den GESAMTEN Zeitraum
    job["phase"] = "Equity/Trades sammeln (dynamischer Gesamtverlauf)"
    dyn.prepare_providers(strategy, full_segs, settings)
    full_m, full_rows = await dyn.eval_dynamic(strategy, full_segs, configs, cfg,
                                               settings, should_stop, strategies_by_regime)
    strat_name = getattr(strategy, "STRATEGY_NAME", sid)
    job["export_trades"] = [{"strategy_id": sid, "strategy_name": strat_name,
                             "timeframe": tf, **t} for t in full_rows][:25000]

    # Modell fürs Speichern/Live schlank halten
    for r in regime_results:
        r.pop("provider", None)
    return {
        "strategy_id": sid, "strategy_name": strat_name,
        "metrics": full_m,
        "dynamic": {
            "model": model,
            "settings": {"max_regimes": max_regimes, "lookback_days": lookback_days,
                         "confidence_min": round(conf_min * 100, 0),
                         "min_hold_days": min_hold_days, "min_share_pct": min_share,
                         "train_pct": train_pct,
                         "min_trades_per_regime": min_tr_regime,
                         "per_regime_strategies": per_regime_strategies,
                         "max_rules_per_regime": max_rules_regime,
                         "switch_policy": "Beim Regimewechsel werden offene Positionen geschlossen"},
            "regimes": regime_results,
            "configs": {str(k): v for k, v in configs.items()},
            "rule_variants": variants_info,
            "sub_strategies": discovery_info,
            "per_regime_strategies": per_regime_strategies,
            "regime_walk_forward": bool(inner_val),
            "base_definition": (strategy.definition
                                if getattr(strategy, "IS_CUSTOM", False) else None),
            "static_benchmark": static,
            "comparison": {
                "dynamic": {"train": dyn_train_m, "test": dyn_test_m},
                "static": {"train": static["metrics"], "test": stat_test_m},
            },
            "verdict": verdict,
            "test_switches": switches,
            "export_meta": {"trades": len(job["export_trades"])},
        },
    }


# ---------------- Haupt-Runner ----------------
async def run_optimizer(job_id: str, body: Dict, registry, settings: Dict,
                        default_cfg: Dict, db=None):
    job = JOBS[job_id]

    def cancelled():
        return bool(job.get("cancel"))

    pool = None
    try:
        from services import candle_cache as _cc
        t_start = time.perf_counter()
        dl_before = _cc.download_stats()
        mode = body.get("mode", "params")
        symbols = body.get("symbols") or ["BTCUSDT"]
        days = min(max(int(body.get("days") or 3), 1), 5500)
        tf = body.get("timeframe") or "1m"
        if tf not in TIMEFRAMES:
            tf = "1m"
        objective = body.get("objective") or "combo"
        algorithm = body.get("algorithm") or "random"
        min_trades = max(int(body.get("min_trades") or 10), 1)
        iterations = min(max(int(body.get("iterations") or 40), 5), 500)
        max_rules = min(max(int(body.get("max_rules") or 4), 1), 6)
        allowed = body.get("indicators") or None
        # Pro-Regel-Timeframes (Multi-Timeframe) mitoptimieren – opt-in mit Rahmen
        rtf = body.get("rule_timeframes") or {}
        rtf_options: List[str] = []
        if isinstance(rtf, dict) and rtf.get("enabled"):
            rtf_options = [t for t in rule_tf_options(tf, rtf.get("min") or "1m",
                                                      rtf.get("max") or "4h")
                           if TIMEFRAMES.get(t) != TIMEFRAMES.get(tf)]
        cfg = dict(default_cfg)
        for k in ("max_capital", "leverage", "fee_percent"):
            if body.get(k) is not None:
                cfg[k] = body[k]
        # Festes Zeitfenster (z.B. "15:00-18:00"): Optimierung/Discovery läuft
        # dann nur auf Signalen innerhalb dieses Fensters.
        fixed_sessions = (body.get("sessions") or "").strip() if isinstance(body.get("sessions"), str) \
            else body.get("sessions")
        if fixed_sessions:
            cfg["sessions"] = fixed_sessions

        # Robustheits-Features (Walk-Forward, Drawdown-Filter, Konstanz-Test)
        robust = robustness.parse_config(body)
        if mode == "explore":
            # Endlos-Suche: Walk-Forward-Split ist Pflicht (Champion-Kriterium)
            robust["wf_enabled"] = True
            robust["wf_mode"] = "single"
        dd_max = robust["dd_max_pct"] if robust["dd_enabled"] else None

        # Welche Einstellungs-Gruppen sollen mitoptimiert werden?
        opt_flags = body.get("optimize")
        if not isinstance(opt_flags, dict):
            # Back-Compat: alter Schalter "include_trade_params"
            opt_flags = {"tpsl": bool(body.get("include_trade_params", True))}
        trade_space = build_trade_space(opt_flags)

        # Weiterentwicklung einer bestehenden Custom-Strategie
        base_definition = None
        bsid = body.get("base_strategy_id")
        if mode in ("discovery", "combo", "explore") and bsid:
            bstrat = registry.get(bsid)
            if not bstrat or not getattr(bstrat, "IS_CUSTOM", False):
                raise RuntimeError("Basis-Strategie muss eine Custom-Strategie sein")
            base_definition = copy.deepcopy(bstrat.definition)

        histories: Dict[str, List[Dict]] = {}
        raw_candles = 0
        _t_data = time.perf_counter()
        async with aiohttp.ClientSession() as session:
            for idx, sym in enumerate(symbols):
                if cancelled():
                    raise JobCancelled()
                job["phase"] = f"Lade Daten: {sym}"
                job["progress"] = round(idx / max(len(symbols), 1) * 10)
                raw = await fetch_history(session, sym, days, job=job)
                raw_candles += len(raw)
                candles = aggregate_candles(raw, tf)
                del raw
                gc.collect()
                if len(candles) > 100:
                    histories[sym] = candles
        data_seconds = time.perf_counter() - _t_data
        if not histories:
            raise RuntimeError("Zu wenig Daten für diesen Timeframe/Zeitraum")
        # Vollständige Historien für Equity/Trade-Export merken (WF-Split kürzt histories)
        full_histories = dict(histories)

        # Walk-Forward: Optimierung nur auf Trainingsdaten, Prüfung auf den
        # unbekannten Testdaten. Split VOR fs_map/Pool, damit alle Such-Pfade
        # konsistent auf den Trainingsdaten arbeiten.
        # mode="single":  ein Split (Training vorne, Test hinten)
        # mode="rolling": mehrere gleitende Trainings-/Test-Fenster
        test_hist = None
        wf_windows = None
        train_days, test_days = float(days), 0.0
        wf_prefix = ""
        if robust["wf_enabled"] and mode != "dynamic":
            wf_prefix = "Training · "
            train_days = days * robust["train_pct"] / 100.0
            if robust["wf_mode"] in ("rolling", "anchored"):
                wf_windows = robustness.rolling_windows(
                    histories, robust["train_pct"], robust["wf_windows"],
                    anchored=robust["wf_mode"] == "anchored")
                histories = {s: c for s, c in wf_windows[0]["train"].items() if len(c) > 100}
                ok_windows = all(w["test"] and all(len(c) > 20 for c in w["test"].values())
                                 for w in wf_windows)
                if not histories or not ok_windows:
                    raise RuntimeError("Zu wenig Daten für Rolling Walk-Forward – "
                                       "Zeitraum erhöhen oder weniger Fenster wählen")
                test_days = (float(days) - train_days) / robust["wf_windows"]
            else:
                histories, test_hist = robustness.split_histories(histories, robust["train_pct"])
                histories = {s: c for s, c in histories.items() if len(c) > 100}
                test_hist = {s: c for s, c in (test_hist or {}).items() if len(c) > 20}
                if not histories or not test_hist:
                    raise RuntimeError("Zu wenig Daten für den Walk-Forward-Split – "
                                       "Zeitraum erhöhen oder Trainings-Anteil anpassen")
                test_days = float(days) - train_days

        # Vorberechnete Indikator-Serien für den schnellen Custom-Pfad
        # (Dynamik-Modus baut eigene Provider je Regime-Abschnitt)
        fs_map = {} if mode == "dynamic" else \
            {sym: fast_sim.FastSeries(c) for sym, c in histories.items()}

        # Multi-Core (nur lokaler Worker, SIM_WORKERS>1): Iterationen/Kandidaten
        # werden in Batches über einen Prozess-Pool bewertet – gleiche
        # _evaluate-Logik, Cloud bleibt unverändert sequenziell.
        workers = 1
        try:
            from services import parallel_sim
            workers = parallel_sim.workers_configured()
            # Im Dynamik-Modus wird der Pool in _run_dynamic mit den
            # Regime-Abschnitten gebaut (feinere Aufteilung, alle Kerne).
            if workers > 1 and mode != "dynamic":
                pool = parallel_sim.make_pool(histories, workers)
        except Exception as e:
            logger.warning(f"Multi-Core deaktiviert: {e}")
            pool, workers = None, 1

        result = {"mode": mode, "timeframe": tf, "days": days,
                  "symbols": list(histories.keys()), "objective": objective,
                  "algorithm": algorithm, "min_trades": min_trades,
                  "optimize": opt_flags, "max_capital": cfg.get("max_capital"),
                  "sessions": fixed_sessions or None}
        # Transparenz für KI-Strategien: nicht auswertbare Regeln sind der
        # häufigste Grund für "überall 0" – statt still zu scheitern, melden.
        problems = None
        _sid = body.get("strategy_id")
        if _sid:
            _s = registry.get(_sid)
            problems = getattr(_s, "rule_problems", None) if _s else None
        if problems:
            result["strategy_warnings"] = list(problems)
        result["robustness"] = {k: robust[k] for k in
                                ("wf_enabled", "wf_mode", "wf_windows", "train_pct",
                                 "dd_enabled", "dd_max_pct",
                                 "ct_enabled", "ct_chunk_days", "ct_max_dev_pct",
                                 "st_enabled", "st_mult", "sb_enabled", "sb_var_pct",
                                 "mc_enabled", "mc_runs", "mc_max_dd_pct", "rg_enabled")}
        if robust["wf_enabled"] and mode != "dynamic":
            result["walk_forward"] = {"mode": robust["wf_mode"],
                                      "train_pct": robust["train_pct"],
                                      "train_days": round(train_days, 1),
                                      "test_days": round(test_days, 1)}
            if wf_windows:
                result["walk_forward"]["windows"] = robust["wf_windows"]
                result["walk_forward"]["ranges"] = [w["range"] for w in wf_windows]

        if mode == "dynamic":
            dyn_res = await _run_dynamic(job, body, registry, settings, cfg, robust,
                                         full_histories, tf, days, objective,
                                         min_trades, iterations, trade_space,
                                         cancelled, db)
            result.update(dyn_res)
            candidates, strategy_obj = [], None
        elif mode == "params":
            sid = body.get("strategy_id")
            strategy = registry.get(sid)
            if not strategy:
                raise RuntimeError("Strategie nicht gefunden")

            def prog(done, total, phase):
                job["progress"] = 10 + round(done / max(total, 1) * 89)
                job["phase"] = wf_prefix + phase

            rule_tf_space = None
            if rtf_options and getattr(strategy, "IS_CUSTOM", False):
                from strategies import custom_params as _cp
                rule_tf_space = _cp.rule_timeframe_space(strategy.definition, rtf_options)

            baseline, best, top, search_stats = await _optimize_params(
                job, strategy, histories, settings, cfg, objective, min_trades,
                iterations, trade_space, prog,
                algorithm, fs_map, cancelled, pool, workers, dd_max,
                rule_tf_space)
            result.update({"strategy_id": sid,
                           "strategy_name": getattr(strategy, "STRATEGY_NAME", sid),
                           "baseline": baseline, "best": best, "top": top,
                           "iterations": iterations, "search_stats": search_stats})
            # Nur harte Min-Trades-Ausfälle (-1e9) ausschließen – Filter-Ausfälle
            # (DD/Konstanz) bleiben sichtbar und werden als 'nicht bestanden' markiert
            candidates = [t for t in top if t.get("score", -1e18) > -9e8][:10]
            # Fallback: bestes Ergebnis immer zeigen (auch bei wenigen Trades)
            if not candidates and best:
                candidates = [{"params": best.get("params") or {},
                               "trade_params": best.get("trade_params") or {},
                               "metrics": best.get("metrics") or {},
                               "score": best.get("score")}]
            strategy_obj = strategy
        elif mode == "explore":
            # ---- Endlos-Suche: sucht bis Training UND Walk-Forward bestehen ----
            from services import deep_explore
            fs_test = {s: fast_sim.FastSeries(c) for s, c in (test_hist or {}).items()}

            def prog_e(pct, phase):
                job["progress"] = 10 + round(min(max(pct, 0), 100) * 0.85)
                job["phase"] = phase

            champions, explore_report = await deep_explore.run(
                sys.modules[__name__], job, histories, test_hist, settings, cfg,
                objective, min_trades, max_rules, allowed, prog_e, iterations,
                fs_map, fs_test, cancelled, pool, workers, dd_max,
                train_days, test_days, body.get("explore") or {}, base_definition,
                tf_options=rtf_options or None)
            result.update({"explore": True, "explore_report": explore_report,
                           "steps": [], "refine_log": [], "trade_params": {}})
            if champions:
                result.update({"definition": champions[0]["definition"],
                               "rules": _labels(champions[0]["definition"]),
                               "metrics": champions[0]["metrics"]})
            candidates = [{"definition": c["definition"], "trade_params": {},
                           "metrics": c["metrics"], "score": c["score"]}
                          for c in champions]
            strategy_obj = None
        else:
            tracker = robustness.TopTracker(12)
            deep = bool(body.get("deep_test"))
            deep_depth = (body.get("deep_depth") or "deep").lower()
            if deep_depth not in ("deep", "extreme"):
                deep_depth = "deep"
            do_refine = mode == "combo" and not deep
            do_trade = bool(trade_space)
            span_end = 99
            if (do_refine or deep) and do_trade:
                span_end = 55
            elif do_refine or do_trade or deep:
                span_end = 70

            def prog_d(done, total, phase):
                job["progress"] = 10 + round(done / max(total, 1) * (span_end - 10))
                job["phase"] = wf_prefix + phase

            deep_report = None
            deep_refine_log = []
            if deep:
                from services import deep_search
                (definition, best_m, best_sc, steps, deep_report,
                 deep_refine_log) = await deep_search.run(
                    sys.modules[__name__], job, histories, settings, cfg, objective,
                    min_trades, max_rules, allowed, prog_d, iterations,
                    base_definition, fs_map, cancelled, pool, workers, dd_max,
                    tracker, deep_depth, tf_options=rtf_options or None)
            else:
                definition, best_m, best_sc, steps = await _discover(
                    job, histories, settings, cfg, objective, min_trades,
                    max_rules, allowed, prog_d, base_definition, fs_map, cancelled,
                    pool, workers, dd_max, tracker, rtf_options or None)
            refine_log = list(deep_refine_log)
            refine_end = span_end
            if do_refine and best_m:
                refine_end = 80 if do_trade else 99

                def prog_r(done, total, phase, _s=span_end, _e=refine_end):
                    job["progress"] = _s + round(done / max(total, 1) * (_e - _s))
                    job["phase"] = wf_prefix + phase

                definition, best_m, refine_log = await _refine(
                    job, definition, best_sc, best_m, histories, settings, cfg,
                    objective, min_trades, iterations, prog_r, fs_map, cancelled,
                    dd_max, tracker)
                best_sc = _score(best_m, objective, min_trades, dd_max) if best_m else best_sc
            best_trade_params = {}
            if do_trade and best_m:
                def prog_t(done, total, phase, _s=refine_end):
                    job["progress"] = _s + round(done / max(total, 1) * (99 - _s))
                    job["phase"] = wf_prefix + phase

                best_trade_params, best_m, best_sc = await _optimize_trade_settings(
                    job, definition, best_sc, best_m, histories, settings, cfg,
                    objective, min_trades, iterations, trade_space, prog_t,
                    fs_map, cancelled, pool, workers, dd_max, tracker)
            result.update({"definition": definition, "rules": _labels(definition),
                           "metrics": best_m, "steps": steps, "refine_log": refine_log,
                           "trade_params": best_trade_params,
                           "deep_test": bool(deep), "deep_report": deep_report,
                           "base_strategy_id": bsid if base_definition else None})
            candidates = [t for t in tracker.top(10) if t.get("score", -1e18) > -9e8]
            # Fallback: bestes Such-Ergebnis immer zeigen, auch wenn alle
            # Kandidaten unter Min-Trades lagen (sonst wäre Top-5 leer)
            if not candidates and best_m:
                candidates = [{"definition": copy.deepcopy(definition),
                               "trade_params": dict(best_trade_params or {}),
                               "metrics": best_m, "score": round(best_sc, 3)}]
            strategy_obj = None

        # Top-5 + Robustheits-Checks (Walk-Forward-Test, DD-Filter, Konstanz-Test)
        try:
            result["top5"] = await _finalize_top5(
                job, mode, candidates, histories, test_hist, settings, cfg,
                robust, fs_map, cancelled, strategy_obj, train_days, test_days,
                wf_windows)
        except JobCancelled:
            raise
        except Exception as e:  # noqa: BLE001 – Top-5 darf das Ergebnis nie killen
            logger.warning(f"top5 finalize failed: {e}")
            result["top5"] = []

        # Trades des besten Kandidaten sammeln (Equity-Kurve + CSV wie im Backtester)
        try:
            if mode != "dynamic":  # Dynamik-Modus sammelt seine Trades selbst
                await _collect_best_trades(job, result, mode, strategy_obj, settings,
                                           cfg, full_histories, tf, cancelled)
        except JobCancelled:
            raise
        except Exception as e:  # noqa: BLE001 – Export darf das Ergebnis nie killen
            logger.warning(f"optimizer trade export failed: {e}")

        # Benchmark-Statistik (Zähler aus _evaluate_batch via job['_bench'])
        from services.backtester import _build_benchmark
        b = job.pop("_bench", {})
        result["benchmark"] = _build_benchmark(
            {"data_seconds": data_seconds, "sim_seconds": b.get("sim_seconds", 0.0),
             "cpu_seconds": b.get("cpu_seconds", 0.0), "workers": workers,
             "evaluations": b.get("evaluations", 0),
             "dyn_segments": b.get("dyn_segments", 0),
             "sim_candles": sum(len(c) for c in histories.values()),
             "raw_candles": raw_candles},
            t_start, dl_before, _cc.download_stats(), job.get("execution") or "cloud")

        job["result"] = result
        job["status"] = "done"
        job["progress"] = 100
        job["phase"] = "Fertig"
        try:
            from core import state as _st
            from services import notifications as _nf
            await _nf.telegram_notify(
                _st.db, _st.telegram, "optimizer_done",
                f"🧪 *OPTIMIZER FERTIG*\nStrategie: {job.get('params', {}).get('strategy_id', '-')}"
                f" · Auswertungen: {result.get('evaluations', b.get('evaluations', 0))}")
        except Exception as e:
            logger.warning(f"optimizer_done notify failed: {e}")
        if db is not None:
            try:
                await db.optimizer_runs.insert_one({"id": job_id, "params": job["params"],
                                                    "created_at": job["created_at"],
                                                    "result": result})
                if job.get("export_trades"):
                    await db.optimizer_trades.insert_one(
                        {"job_id": job_id, "created_at": job["created_at"],
                         "rows": job["export_trades"][:25000]})
            except Exception as e:
                logger.warning(f"optimizer persist failed: {e}")
            try:
                from services import learning
                await learning.record_run(db, result)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"learning record failed: {e}")
            if mode == "explore":
                try:
                    from services import deep_explore
                    await deep_explore.persist_best(db, result)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"explore persist failed: {e}")
    except JobCancelled:
        job["status"] = "cancelled"
        job["phase"] = "Abgebrochen"
        job["error"] = None
        logger.info(f"optimizer {job_id} cancelled")
    except Exception as e:
        logger.exception("optimizer failed")
        job["status"] = "error"
        job["error"] = str(e)[:300]
