"""Dynamische Strategien: pro Marktregime eine eigene Sub-Strategie.

Design-Prinzipien (siehe Anforderungen):
- Regime-Erkennung ohne Lookahead (services.regime), Anzahl automatisch bestimmt.
- Pro Regime kann eine VOLLSTÄNDIGE eigene Strategie gesucht werden (eigene
  Regeln + eigene Trade-Parameter) – nicht nur eine Feinjustierung.
- Pro Regime wird NUR optimiert, wenn genügend Trades vorhanden sind; sonst
  greift die statische Fallback-Konfiguration.
- Eine dynamische Strategie wird IMMER gegen die beste statische Konfiguration
  (gleiches Suchbudget) auf unbekannten Testdaten verglichen. Nur wenn sie klar
  besser ist, wird sie empfohlen.
- Beim Regimewechsel im Backtest werden offene Positionen zum Umschaltzeitpunkt
  geschlossen (konservativ, transparent dokumentiert).
"""
import asyncio
import copy
import json
import logging
import random
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from services import fast_sim, regime
from services.backtester import JobCancelled, simulate_pair

logger = logging.getLogger(__name__)

WARMUP_BARS = 300
MIN_TRADES_PER_REGIME_FACTOR = 0.5  # min_trades * Faktor je Regime
MAX_PROVIDER_CACHE = 6              # Regel-Varianten je Segment im Speicher


def _iso(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()


def metrics_from_rows(rows: List[Dict], capital: float) -> Dict:
    """Metriken aus einer Trade-Liste – identische Definitionen wie simulate_pair
    (Winrate aus PnL, Drawdown aus chronologischer Equity-Kurve)."""
    eps = 1e-6
    rows = sorted([r for r in rows if r.get("closed")], key=lambda r: r["closed"])
    wins = sum(1 for r in rows if (r.get("pnl") or 0) > eps)
    losses = sum(1 for r in rows if (r.get("pnl") or 0) < -eps)
    breakevens = len(rows) - wins - losses
    pnl = sum(float(r.get("pnl") or 0) for r in rows)
    fees = sum(float(r.get("fees") or 0) for r in rows)
    eq = peak = dd = 0.0
    for r in rows:
        eq += float(r.get("pnl") or 0)
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    decided = wins + losses
    cap = float(capital or 100)
    return {"trades": len(rows), "wins": wins, "losses": losses,
            "breakevens": breakevens,
            "win_rate": round(wins / decided * 100, 1) if decided else 0.0,
            "pnl": round(pnl, 2), "fees": round(fees, 2),
            "max_drawdown": round(dd, 2),
            "avg_pnl": round(pnl / len(rows), 3) if rows else 0.0,
            "pnl_pct": round(pnl / cap * 100, 1),
            "max_drawdown_pct": round(dd / cap * 100, 1)}


def build_segments(histories: Dict[str, List[Dict]], labels_map: Dict[str, List],
                   offset_map: Dict[str, int] = None) -> Dict[str, List[Dict]]:
    """Pro Symbol: zusammenhängende Regime-Abschnitte inkl. Warmup-Slice.
    offset_map: Label-Index 0 entspricht Kerze offset im (vollen) Kerzen-Array."""
    out = {}
    for sym, candles in histories.items():
        labels = labels_map.get(sym) or []
        off = (offset_map or {}).get(sym, 0)
        segs = []
        for (s, e, rid) in regime.segments_from_labels(labels):
            gs, ge = s + off, e + off
            w0 = max(gs - WARMUP_BARS, 0)
            segs.append({"regime": rid, "start_ts": candles[gs]["timestamp"],
                         "candles": candles[w0:ge], "n_bars": ge - gs})
        out[sym] = segs
    return out


def _provider_for(strategy, candles, settings, sym):
    try:
        fs = fast_sim.FastSeries(candles)
        return fast_sim.provider_for(strategy, fs, settings, sym), fs
    except Exception:  # noqa: BLE001 – Fallback: normale Simulation
        return None, None


def prepare_providers(strategy, segments: Dict[str, List[Dict]], settings: Dict):
    """FastSeries + Signal-Provider je Segment EINMAL bauen. Signale hängen nur
    von den Regeln ab (nicht von den Trade-Parametern) und werden für alle
    Kandidaten wiederverwendet; Regel-Varianten teilen sich die FastSeries.
    Im Multi-Core-Modus passiert das in den Kind-Prozessen (dort gecacht)."""
    if _POOL is not None:
        return
    for sym, segs in segments.items():
        for seg in segs:
            try:
                seg["fs"] = fast_sim.FastSeries(seg["candles"])
            except Exception:  # noqa: BLE001
                seg["fs"] = None
            seg["provider"] = provider_for_seg(strategy, seg, settings, sym)


# ---------------- Multi-Core-Ausführung der Regime-Abschnitte ----------------
_POOL = None          # ProcessPoolExecutor oder None (sequenziell)
_KEY_SEQ = [0]
# Laufzeit-Zähler für die Benchmark-Anzeige (Kerne/Speedup im UI)
BENCH = {"evaluations": 0, "cpu_seconds": 0.0, "sim_seconds": 0.0, "segments": 0}


def reset_bench():
    BENCH.update({"evaluations": 0, "cpu_seconds": 0.0, "sim_seconds": 0.0,
                  "segments": 0})


def set_pool(pool):
    """Prozess-Pool für die Segment-Simulation setzen (None = sequenziell)."""
    global _POOL
    _POOL = pool


def register_segments(*segment_maps) -> Dict[str, object]:
    """Jedem Abschnitt einen eindeutigen Schlüssel geben und die Kerzen-Daten
    sammeln, damit die Kind-Prozesse sie einmalig erhalten."""
    data: Dict[str, object] = {}
    for segments in segment_maps:
        if not segments:
            continue
        for sym, segs in segments.items():
            for seg in segs:
                if not seg.get("_key"):
                    _KEY_SEQ[0] += 1
                    seg["_key"] = f"{sym}#seg{_KEY_SEQ[0]}"
                data[seg["_key"]] = seg["candles"]
    return data


def with_strategy_params(settings: Dict, strategy_id: str, params: Dict) -> Dict:
    """Settings-Kopie mit überschriebenen Strategie-Parametern (Indikator-Perioden,
    Schwellen ...). Wirkt sowohl im sequenziellen als auch im Multi-Core-Pfad,
    weil die Settings je Segment übergeben werden."""
    if not params:
        return settings
    sp = dict(settings.get("strategy_params") or {})
    sp[strategy_id] = {**(sp.get(strategy_id) or {}), **params}
    return {**settings, "strategy_params": sp}


async def _rows_for(strategy, segs: List[tuple], settings, cfg_for,
                    should_stop=None) -> List[Dict]:
    """segs: Liste von (sym, seg). Läuft über alle CPU-Kerne, wenn ein Pool
    gesetzt ist – sonst sequenziell im Thread (identisches Ergebnis).
    `settings` darf ein Dict ODER eine Funktion seg -> Settings sein (z.B. für
    Regime-spezifische Strategie-Parameter)."""
    set_for = settings if callable(settings) else (lambda _s: settings)
    t_wall = time.perf_counter()
    BENCH["evaluations"] += 1
    BENCH["segments"] += len(segs)
    if _POOL is None:
        out = []
        for sym, seg in segs:
            if should_stop and should_stop():
                raise JobCancelled()
            st = strategy(seg) if callable(strategy) else strategy
            t0 = time.perf_counter()
            out.append((seg, await asyncio.to_thread(
                simulate_segment, st, seg, sym, set_for(seg), cfg_for(seg), should_stop)))
            BENCH["cpu_seconds"] += time.perf_counter() - t0
        BENCH["sim_seconds"] += time.perf_counter() - t_wall
        return out
    from services import parallel_sim
    if should_stop and should_stop():
        raise JobCancelled()
    loop = asyncio.get_running_loop()
    futs = []
    for sym, seg in segs:
        st = strategy(seg) if callable(strategy) else strategy
        futs.append(loop.run_in_executor(
            _POOL, parallel_sim.sim_segment_task_timed, parallel_sim.strategy_spec(st),
            seg["_key"], sym, set_for(seg), cfg_for(seg), _iso(seg["start_ts"])))
    timed = await asyncio.gather(*futs)
    BENCH["cpu_seconds"] += sum(d for _, d in timed)
    BENCH["sim_seconds"] += time.perf_counter() - t_wall
    return list(zip([s for _, s in segs], [rows for rows, _ in timed]))


def _def_key(strategy, settings: Dict = None, sym: str = None) -> str:
    """Cache-Schlüssel einer Strategie-VARIANTE. Wichtig: für Built-in-Strategien
    gehören die effektiven Strategie-Parameter dazu, sonst würden verschiedene
    Parameter-Kandidaten denselben (falschen) Signal-Provider wiederverwenden."""
    if getattr(strategy, "IS_CUSTOM", False):
        # Custom-/KI-Strategien: die Regel-Schwellen können jetzt über
        # Strategie-Parameter optimiert werden -> effektive Definition als Key.
        eff = getattr(strategy, "effective_definition", None)
        d = eff(strategy.get_params(settings or {}, sym)) if callable(eff) else strategy.definition
        return json.dumps({"i": d.get("indicators"), "l": d.get("long_rules"),
                           "s": d.get("short_rules")}, sort_keys=True, default=str)
    sid = getattr(strategy, "STRATEGY_ID", "builtin")
    sp = ((settings or {}).get("strategy_params") or {}).get(sid) or {}
    cp = (((settings or {}).get("coin_params") or {}).get(sid) or {}).get(sym) or {}
    if not sp and not cp:
        return sid
    return sid + "|" + json.dumps({**sp, **cp}, sort_keys=True, default=str)


def provider_for_seg(strategy, seg: Dict, settings: Dict, sym: str):
    """Signal-Provider für (Strategie-Variante, Segment) – gecacht.
    Die FastSeries des Segments wird wiederverwendet, dadurch kostet eine neue
    Regel-Variante nur noch die Regel-Auswertung, nicht die Indikator-Berechnung."""
    fs = seg.get("fs")
    if fs is None:
        try:
            fs = fast_sim.FastSeries(seg["candles"])
        except Exception:  # noqa: BLE001
            fs = None
        seg["fs"] = fs
    if fs is None:
        return None
    cache = seg.setdefault("_prov", {})
    key = _def_key(strategy, settings, sym)
    if key in cache:
        return cache[key]
    try:
        prov = fast_sim.provider_for(strategy, fs, settings, sym)
    except Exception:  # noqa: BLE001
        prov = None
    if len(cache) >= MAX_PROVIDER_CACHE:
        for k in list(cache)[:-(MAX_PROVIDER_CACHE - 1)]:
            cache.pop(k, None)
    cache[key] = prov
    return prov


def simulate_segment(strategy, seg: Dict, sym: str, settings: Dict, cfg: Dict,
                     should_stop=None, provider=None) -> List[Dict]:
    """Ein Segment simulieren; nur Trades zählen, die IM Segment geöffnet wurden
    (Warmup-Trades werden verworfen)."""
    if provider is None:
        provider = provider_for_seg(strategy, seg, settings, sym)
    res = simulate_pair(strategy, seg["candles"], sym, settings, cfg,
                        None, True, should_stop, provider)
    start_iso = _iso(seg["start_ts"])
    return [t for t in (res.get("all_trades") or [])
            if (t.get("opened") or "") >= start_iso]


async def eval_regime_config(strategy, segments: Dict[str, List[Dict]], rid: int,
                             settings: Dict, cfg: Dict, should_stop=None) -> Dict:
    """Eine Trade-Konfiguration auf allen Segmenten EINES Regimes bewerten."""
    segs = [(sym, seg) for sym, ss in segments.items() for seg in ss
            if seg["regime"] == rid]
    rows = []
    for _seg, trades in await _rows_for(strategy, segs, settings, lambda s: cfg,
                                        should_stop):
        rows.extend(trades)
    return metrics_from_rows(rows, cfg.get("max_capital", 100))


async def eval_dynamic(strategy, segments: Dict[str, List[Dict]],
                       configs: Dict[int, Dict], base_cfg: Dict, settings: Dict,
                       should_stop=None, strategies_by_regime: Dict = None,
                       settings_by_regime: Dict = None
                       ) -> Tuple[Dict, List[Dict]]:
    """Komplette dynamische Simulation: jedes Segment mit der Sub-Strategie und
    Konfiguration seines Regimes; chronologisch zusammengeführt."""
    segs = [(sym, seg) for sym, ss in segments.items() for seg in ss]
    sym_of = {id(seg): sym for sym, seg in segs}
    by_regime = strategies_by_regime or {}
    by_set = settings_by_regime or {}
    rows = []
    for seg, trades in await _rows_for(
            lambda s: by_regime.get(s["regime"]) or strategy, segs,
            (lambda s: by_set.get(s["regime"], settings)) if by_set else settings,
            lambda s: {**base_cfg, **(configs.get(s["regime"]) or {})}, should_stop):
        for t in trades:
            rows.append({**t, "symbol": sym_of.get(id(seg)), "regime": seg["regime"]})
    return metrics_from_rows(rows, base_cfg.get("max_capital", 100)), rows


def _score(m: Dict, objective: str) -> float:
    wr = m.get("win_rate", 0.0)
    pnl = m.get("pnl", 0.0)
    if objective == "win_rate":
        return wr * 1000 + pnl
    if objective == "pnl":
        return pnl
    return pnl * (0.5 + wr / 100.0)


def sample_config(trade_space: Dict, rng: random.Random) -> Dict:
    tp = {k: rng.choice(v) for k, v in trade_space.items()}
    if isinstance(tp.get("tp_full_crv"), (int, float)) and isinstance(tp.get("tp1_crv"), (int, float)) \
            and tp["tp_full_crv"] < tp["tp1_crv"]:
        tp["tp_full_crv"], tp["tp1_crv"] = tp["tp1_crv"], tp["tp_full_crv"]
    return tp


def split_segments(segments: Dict[str, List[Dict]], train_pct: float
                   ) -> Tuple[Dict[str, List[Dict]], Dict[str, List[Dict]]]:
    """Jeden Regime-Abschnitt chronologisch in Trainings- und Validierungsteil
    schneiden. Beide Teile behalten ihren Warmup-Vorlauf, damit Indikatoren
    korrekt anlaufen. So kann eine Sub-Strategie innerhalb IHRER Marktphase
    auf unbekannten Daten geprüft werden (Walk-Forward je Regime)."""
    pct = min(max(float(train_pct), 40.0), 95.0) / 100.0
    train: Dict[str, List[Dict]] = {}
    val: Dict[str, List[Dict]] = {}
    for sym, segs in segments.items():
        tr, va = [], []
        for seg in segs:
            c = seg["candles"]
            n_bars = int(seg["n_bars"])
            warm = max(len(c) - n_bars, 0)
            cut = int(n_bars * pct)
            if cut > 20:
                tr.append({"regime": seg["regime"], "start_ts": c[warm]["timestamp"],
                           "candles": c[:warm + cut], "n_bars": cut})
            if n_bars - cut > 20:
                s = warm + cut
                w0 = max(s - WARMUP_BARS, 0)
                va.append({"regime": seg["regime"], "start_ts": c[s]["timestamp"],
                           "candles": c[w0:], "n_bars": n_bars - cut})
        if tr:
            train[sym] = tr
        if va:
            val[sym] = va
    return train, val


def validation_passed(m: Dict, min_trades: int) -> bool:
    """Bestanden = auf den unbekannten Daten der eigenen Marktphase profitabel
    und mit genügend Trades (kein Zufallstreffer)."""
    return (m.get("trades") or 0) >= max(min_trades, 3) and (m.get("pnl") or 0) > 0


def _guarded_score(m: Dict, objective: str, min_trades: int) -> float:
    """Score mit Mindest-Trade-Filter (wie im Optimizer)."""
    if (m.get("trades") or 0) < min_trades:
        return -1e9 + (m.get("trades") or 0)
    return _score(m, objective)


async def discover_regime_strategy(segments: Dict[str, List[Dict]], rid: int,
                                   settings: Dict, base_cfg: Dict,
                                   indicators: List[str], base_definition: Optional[Dict],
                                   objective: str, min_trades: int,
                                   max_rules: int = 4, weights: Dict = None,
                                   progress=None, should_stop=None,
                                   val_segments: Dict[str, List[Dict]] = None,
                                   phase_cb=None, deep: bool = False,
                                   tf_options: List[str] = None) -> Dict:
    """Vollständige eigene Strategie für EIN Regime entdecken.

    Gleicher Greedy-Algorithmus wie die globale Discovery, aber ausschließlich
    auf den Kerzen-Abschnitten dieses Regimes bewertet. Ergebnis ist eine
    eigenständige Custom-Strategie (eigene Regeln), die anschließend noch eigene
    Trade-Parameter bekommt.

    Ist `val_segments` gesetzt, wird nach jeder Regel-Runde auf den unbekannten
    Daten DERSELBEN Marktphase geprüft (Walk-Forward je Regime). Zurückgegeben
    wird die letzte Variante, die diese Prüfung bestanden hat – lieber weniger
    Regeln als eine überangepasste Strategie.
    """
    from services.optimizer import _mk_strategy, build_candidates

    cands = build_candidates(indicators or None, tf_options or None)
    w = weights or {}
    cands.sort(key=lambda c: -w.get(c["ind"], 1.0))
    if base_definition:
        definition = copy.deepcopy(base_definition)
        definition.setdefault("indicators", {})
        definition.setdefault("long_rules", [])
        definition.setdefault("short_rules", [])
    else:
        definition = {"name": f"Regime {rid + 1}", "indicators": {},
                      "long_rules": [], "short_rules": []}
    definition["id"] = f"dyn_regime_{rid}"

    min_val_trades = max(int(min_trades * 0.4), 3)

    async def validate(d):
        if not val_segments:
            return None, True
        m = await eval_regime_config(_mk_strategy(d), val_segments, rid,
                                     settings, base_cfg, should_stop)
        return m, validation_passed(m, min_val_trades)

    best_score, best_metrics = -1e18, {"trades": 0}
    steps: List[Dict] = []
    passed: List[Dict] = []   # Snapshots, die die Validierung bestanden haben
    if definition["long_rules"] or definition["short_rules"]:
        m0 = await eval_regime_config(_mk_strategy(definition), segments, rid,
                                      settings, base_cfg, should_stop)
        best_score, best_metrics = _guarded_score(m0, objective, min_trades), m0
        vm, ok = await validate(definition)
        steps.append({"round": 0, "added": "Basis-Strategie", "metrics": m0,
                      "score": round(best_score, 3), "validation": vm,
                      "validation_passed": ok})
        if ok:
            passed.append({"definition": copy.deepcopy(definition), "metrics": m0,
                           "score": best_score, "validation": vm, "rules": []})
    used = set()
    if deep:
        return await _deep_regime_search(
            cands, definition, segments, rid, settings, base_cfg, objective,
            min_trades, max_rules, progress, should_stop, phase_cb, validate,
            steps, passed, best_score, best_metrics, bool(val_segments))
    for round_i in range(max_rules):
        round_best = None
        for ci, cand in enumerate(cands):
            if cand["label"] in used:
                continue
            if should_stop and should_stop():
                raise JobCancelled()
            if phase_cb:
                phase_cb(f"Marktphase {rid + 1}: Regel {round_i + 1}/{max_rules} – "
                         f"teste {ci + 1}/{len(cands)} ({cand['label']})")
            d = {**definition,
                 "long_rules": definition["long_rules"] + [dict(cand["long"])],
                 "short_rules": definition["short_rules"] + [dict(cand["short"])]}
            try:
                m = await eval_regime_config(_mk_strategy(d), segments, rid,
                                             settings, base_cfg, should_stop)
            except JobCancelled:
                raise
            except Exception:  # noqa: BLE001 – einzelne Kandidaten isolieren
                continue
            if progress:
                progress(1)
            sc = _guarded_score(m, objective, min_trades)
            if round_best is None or sc > round_best[0]:
                round_best = (sc, cand, m)
        if round_best is None or round_best[0] <= best_score + 1e-9:
            steps.append({"round": round_i + 1, "added": None,
                          "info": "Keine weitere Regel verbessert diese Marktphase"})
            break
        best_score, cand, best_metrics = round_best[0], round_best[1], round_best[2]
        definition["long_rules"].append(dict(cand["long"]))
        definition["short_rules"].append(dict(cand["short"]))
        used.add(cand["label"])
        vm, ok = await validate(definition)
        steps.append({"round": round_i + 1, "added": cand["label"],
                      "score": round(best_score, 3), "metrics": best_metrics,
                      "validation": vm, "validation_passed": ok})
        if ok:
            passed.append({"definition": copy.deepcopy(definition),
                           "metrics": best_metrics, "score": best_score,
                           "validation": vm,
                           "rules": [s["added"] for s in steps if s.get("added")
                                     and s["added"] != "Basis-Strategie"]})

    if val_segments:
        if not passed:
            return {"definition": None, "metrics": best_metrics, "score": round(best_score, 3),
                    "steps": steps, "rules": [], "validation": None,
                    "validation_passed": False,
                    "note": "Keine Regel-Kombination hat den Walk-Forward dieser "
                            "Marktphase bestanden – Basis-Strategie bleibt aktiv"}
        best = passed[-1]
        return {"definition": best["definition"], "metrics": best["metrics"],
                "score": round(best["score"], 3), "steps": steps,
                "rules": best["rules"], "validation": best["validation"],
                "validation_passed": True}
    return {"definition": definition, "metrics": best_metrics,
            "score": round(best_score, 3), "steps": steps,
            "validation": None, "validation_passed": None,
            "rules": [s["added"] for s in steps if s.get("added")
                      and s["added"] != "Basis-Strategie"]}



async def _deep_regime_search(cands, definition, segments, rid, settings, base_cfg,
                              objective, min_trades, max_rules, progress,
                              should_stop, phase_cb, validate, steps, passed,
                              best_score, best_metrics, has_val) -> Dict:
    """Deep-Test für EIN Regime: Einzeltest -> alle Paare -> Beam-Suche ->
    Austausch. Gleiche Ergebnis-Struktur wie die Greedy-Variante, zusätzlich
    `deep_report` mit Beitrag/Synergie je Regel."""
    from itertools import combinations

    from services.optimizer import _mk_strategy

    base = copy.deepcopy(definition)
    by_label = {c["label"]: c for c in cands}
    beam_width, pair_cap, swap_rounds = 5, 600, 2

    def mk(labels):
        d = copy.deepcopy(base)
        d["long_rules"] = list(base.get("long_rules") or []) \
            + [dict(by_label[l]["long"]) for l in labels]
        d["short_rules"] = list(base.get("short_rules") or []) \
            + [dict(by_label[l]["short"]) for l in labels]
        return d

    async def score_of(labels, phase):
        if should_stop and should_stop():
            raise JobCancelled()
        if phase_cb:
            phase_cb(f"Marktphase {rid + 1}: Deep-Test · {phase}")
        try:
            m = await eval_regime_config(_mk_strategy(mk(labels)), segments, rid,
                                         settings, base_cfg, should_stop)
        except JobCancelled:
            raise
        except Exception:  # noqa: BLE001 – einzelne Kandidaten isolieren
            return None, None
        if progress:
            progress(1)
        return _guarded_score(m, objective, min_trades), m

    # Phase 1: Einzeltest
    singles = []
    for i, c in enumerate(cands):
        sc, m = await score_of([c["label"]], f"Einzeltest {i + 1}/{len(cands)}")
        if sc is not None:
            singles.append({"label": c["label"], "ind": c["ind"], "score": round(sc, 3)})
    if not singles:
        return {"definition": None, "metrics": best_metrics, "score": 0, "steps": steps,
                "rules": [], "validation": None, "validation_passed": False,
                "note": "Deep-Test: kein Kandidat auswertbar"}
    singles.sort(key=lambda x: -x["score"])
    solo = {s["label"]: s["score"] for s in singles}
    steps.append({"round": 1, "added": "Deep-Test Einzeltest",
                  "info": f"{len(singles)} Kandidaten einzeln · bester "
                          f"{singles[0]['label']} ({singles[0]['score']})"})

    # Phase 2: alle Paare (nach Einzelrang priorisiert, gedeckelt)
    order = {s["label"]: i for i, s in enumerate(singles)}
    pairs = sorted(combinations([s["label"] for s in singles], 2),
                   key=lambda ab: order[ab[0]] + order[ab[1]])[:pair_cap]
    pair_scores = []
    for i, (a, b) in enumerate(pairs):
        sc, m = await score_of([a, b], f"Paare {i + 1}/{len(pairs)}")
        if sc is None:
            continue
        pair_scores.append({"a": a, "b": b, "score": round(sc, 3), "metrics": m,
                            "synergy": round(sc - max(solo[a], solo[b]), 3)})
    pair_scores.sort(key=lambda x: -x["score"])
    if not pair_scores:
        pair_scores = [{"a": singles[0]["label"], "b": singles[min(1, len(singles) - 1)]["label"],
                        "score": singles[0]["score"], "metrics": best_metrics, "synergy": 0.0}]
    steps.append({"round": 2, "added": "Deep-Test Paare",
                  "score": pair_scores[0]["score"],
                  "info": f"{len(pairs)} Paare geprüft · bestes "
                          f"{pair_scores[0]['a']} + {pair_scores[0]['b']}"})

    # Phase 3: Beam-Suche
    beam = [{"labels": [x["a"], x["b"]], "score": x["score"], "metrics": x["metrics"]}
            for x in pair_scores[:beam_width]]
    best = max(beam, key=lambda x: x["score"])
    for depth_i in range(3, max_rules + 1):
        nxt = []
        for member in beam:
            for c in cands:
                if c["label"] in member["labels"]:
                    continue
                labels = member["labels"] + [c["label"]]
                sc, m = await score_of(labels, f"{len(labels)} Regeln")
                if sc is not None:
                    nxt.append({"labels": labels, "score": sc, "metrics": m})
        if not nxt:
            break
        seen, uniq = set(), []
        for cand in sorted(nxt, key=lambda x: -x["score"]):
            key = tuple(sorted(cand["labels"]))
            if key not in seen:
                seen.add(key)
                uniq.append(cand)
        beam = uniq[:beam_width]
        if beam[0]["score"] > best["score"] + 1e-9:
            best = beam[0]
            steps.append({"round": depth_i, "added": best["labels"][-1],
                          "score": round(best["score"], 3), "metrics": best["metrics"]})
        else:
            steps.append({"round": depth_i, "added": None,
                          "info": "Keine größere Kombination ist besser"})
            break

    # Phase 4: Austausch
    labels, best_sc, best_m = list(best["labels"]), best["score"], best["metrics"]
    swaps = []
    for sround in range(swap_rounds):
        improved = False
        for pos in range(len(labels)):
            for c in cands:
                if c["label"] in labels:
                    continue
                trial = list(labels)
                trial[pos] = c["label"]
                sc, m = await score_of(trial, f"Austausch Regel {pos + 1}")
                if sc is not None and sc > best_sc + 1e-9:
                    best_sc, best_m, labels = sc, m, trial
                    improved = True
                    swaps.append({"round": sround + 1, "position": pos + 1,
                                  "new_rule": c["label"], "score": round(sc, 3)})
        if not improved:
            break
    if swaps:
        steps.append({"round": max_rules + 1, "added": "Deep-Test Austausch",
                      "score": round(best_sc, 3),
                      "info": f"{len(swaps)} Regel(n) ersetzt"})

    # Beitrag je Regel (Leave-one-out)
    contribution = []
    if len(labels) > 1:
        for pos in range(len(labels)):
            rest = [l for i2, l in enumerate(labels) if i2 != pos]
            sc, _m = await score_of(rest, "Beitrag je Regel")
            if sc is not None:
                contribution.append({"rule": labels[pos], "score_without": round(sc, 3),
                                     "delta": round(best_sc - sc, 3)})
        contribution.sort(key=lambda x: -x["delta"])

    definition = mk(labels)
    valid_pairs = [x for x in pair_scores if x["score"] > -9e8]
    report = {"candidates": len(cands), "pairs_tested": len(pairs),
              "singles": singles[:12], "contribution": contribution,
              "best_synergies": [{k: v for k, v in x.items() if k != "metrics"}
                                 for x in sorted(valid_pairs,
                                                 key=lambda x: -x["synergy"])[:8]],
              "worst_synergies": [{k: v for k, v in x.items() if k != "metrics"}
                                  for x in sorted(valid_pairs,
                                                  key=lambda x: x["synergy"])[:8]],
              "swaps": swaps, "final_rules": labels}
    vm, ok = await validate(definition)
    if has_val and not ok:
        # Fallback: kleinere Kombination, die bestanden hat
        for k in range(len(labels) - 1, 1, -1):
            sub = labels[:k]
            d_sub = mk(sub)
            vm2, ok2 = await validate(d_sub)
            if ok2:
                sc_sub, m_sub = await score_of(sub, "Walk-Forward-Rückfall")
                steps.append({"round": max_rules + 2, "added": None,
                              "info": f"Walk-Forward: nur {k} Regeln bestehen"})
                return {"definition": d_sub, "metrics": m_sub or best_m,
                        "score": round(sc_sub or best_sc, 3), "steps": steps,
                        "rules": sub, "validation": vm2, "validation_passed": True,
                        "deep_report": report}
        return {"definition": None, "metrics": best_m, "score": round(best_sc, 3),
                "steps": steps, "rules": [], "validation": vm,
                "validation_passed": False, "deep_report": report,
                "note": "Deep-Test: keine Kombination hat den Walk-Forward dieser "
                        "Marktphase bestanden"}
    return {"definition": definition, "metrics": best_m, "score": round(best_sc, 3),
            "steps": steps, "rules": labels, "validation": vm,
            "validation_passed": ok if has_val else None,
            "deep_report": report}


async def optimize_regime(job, strategy, segments, rid: int, settings, base_cfg,
                          trade_space, iterations: int, objective: str,
                          min_trades: int, progress=None, should_stop=None,
                          val_segments=None, **kwargs) -> Dict:
    """Random-Search der Trade-Parameter NUR auf den Daten eines Regimes.
    Zu wenig Trades -> Regime als 'insufficient' markiert (Fallback greift).
    Mit `val_segments` muss die gewählte Konfiguration zusätzlich auf den
    unbekannten Daten derselben Marktphase profitabel sein (Walk-Forward)."""
    rng = random.Random(1000 + rid)
    phase_cb = kwargs.get("phase_cb")
    base_m = await eval_regime_config(strategy, segments, rid, settings, base_cfg,
                                      should_stop)
    cands = [({}, base_m, _score(base_m, objective))]
    for it in range(iterations):
        if should_stop and should_stop():
            raise JobCancelled()
        if phase_cb:
            phase_cb(f"Marktphase {rid + 1}: Trade-Parameter {it + 1}/{iterations}")
        tp = sample_config(trade_space, rng)
        m = await eval_regime_config(strategy, segments, rid, settings,
                                     {**base_cfg, **tp}, should_stop)
        if m["trades"] >= min_trades:
            cands.append((tp, m, _score(m, objective)))
        if progress:
            progress(it + 1)
    cands.sort(key=lambda x: (-(x[1]["trades"] >= min_trades), -x[2]))
    best_tp, best_m, best_sc = cands[0]
    validation = None
    validated = None
    if val_segments:
        min_val = max(int(min_trades * 0.4), 3)
        validated = False
        for tp, m, sc in cands[:8]:
            vm = await eval_regime_config(strategy, val_segments, rid, settings,
                                          {**base_cfg, **tp}, should_stop)
            if validation_passed(vm, min_val):
                best_tp, best_m, best_sc, validation, validated = tp, m, sc, vm, True
                break
            if validation is None:
                validation = vm
    insufficient = best_m["trades"] < min_trades
    return {"regime": rid, "config": best_tp, "metrics": best_m,
            "baseline_metrics": base_m, "insufficient": insufficient,
            "validation": validation, "validation_passed": validated,
            "score": round(best_sc, 3)}


async def optimize_static(strategy, full_train: Dict[str, List[Dict]], settings,
                          base_cfg, trade_space, iterations: int, objective: str,
                          min_trades: int, progress=None, should_stop=None,
                          data_keys: Dict[str, str] = None) -> Dict:
    """Statische Benchmark: beste EINZELNE Konfiguration auf den gesamten
    Trainingsdaten (gleiches Suchbudget wie ein Regime).
    Mit Prozess-Pool werden die Konfigurationen über alle Kerne verteilt."""
    rng = random.Random(7)
    cfgs = [{}] + [sample_config(trade_space, rng) for _ in range(iterations)]

    if _POOL is not None and data_keys:
        from services import parallel_sim
        loop = asyncio.get_running_loop()
        spec = parallel_sim.strategy_spec(strategy)
        syms = list(full_train.keys())
        keys = [data_keys[s] for s in syms]
        cap = base_cfg.get("max_capital", 100)
        batch = max(getattr(_POOL, "_max_workers", 4) * 2, 4)
        results = []
        for i in range(0, len(cfgs), batch):
            if should_stop and should_stop():
                raise JobCancelled()
            chunk = cfgs[i:i + batch]
            futs = [loop.run_in_executor(_POOL, parallel_sim.static_metrics_task,
                                         spec, keys, syms, settings,
                                         {**base_cfg, **tp}, cap)
                    for tp in chunk]
            results.extend(await asyncio.gather(*futs))
            if progress:
                progress(len(chunk))
        pairs = list(zip(cfgs, results))
    else:
        providers = {sym: _provider_for(strategy, c, settings, sym)[0]
                     for sym, c in full_train.items()}

        async def _eval(cfg):
            rows = []
            for sym, candles in full_train.items():
                if should_stop and should_stop():
                    raise JobCancelled()
                res = await asyncio.to_thread(simulate_pair, strategy, candles, sym,
                                              settings, cfg, None, True, should_stop,
                                              providers.get(sym))
                rows.extend(res.get("all_trades") or [])
            return metrics_from_rows(rows, cfg.get("max_capital", 100))

        pairs = []
        for i, tp in enumerate(cfgs):
            pairs.append((tp, await _eval({**base_cfg, **tp})))
            if progress:
                progress(i + 1)

    best_tp, best_m = pairs[0]
    best_sc = _score(best_m, objective)
    for tp, m in pairs[1:]:
        if m["trades"] >= min_trades:
            sc = _score(m, objective)
            if sc > best_sc or best_m["trades"] < min_trades:
                best_sc, best_tp, best_m = sc, tp, m
    return {"config": best_tp, "metrics": best_m, "score": round(best_sc, 3)}


async def optimize_regime_rules(strategy, segments, rid: int, settings, base_cfg,
                                config: Dict, indicators: List[str],
                                min_trades: int, base_metrics: Dict,
                                objective: str, weights: Dict[str, float] = None,
                                max_candidates: int = 25, progress=None,
                                should_stop=None,
                                tf_options: List[str] = None) -> Optional[Dict]:
    """Regel-Variante je Regime (nur Custom-Strategien): testet, ob EINE
    zusätzliche Regel aus den gewählten Indikatoren die Performance in DIESEM
    Regime deutlich verbessert (>10%). Kandidaten werden nach dem
    Lern-Gedächtnis sortiert (historisch erfolgreiche Indikatoren zuerst)."""
    if not getattr(strategy, "IS_CUSTOM", False):
        return None
    from services.optimizer import build_candidates, _mk_strategy
    cands = build_candidates(indicators or None, tf_options or None)
    w = weights or {}
    cands.sort(key=lambda c: -w.get(c["ind"], 1.0))
    cands = cands[:max_candidates]
    base_def = strategy.definition
    cfg = {**base_cfg, **(config or {})}
    base_sc = _score(base_metrics, objective)
    best = None
    for c in cands:
        if should_stop and should_stop():
            raise JobCancelled()
        var_def = copy.deepcopy(base_def)
        var_def["id"] = "opt_eval"
        var_def.setdefault("long_rules", []).append(dict(c["long"]))
        var_def.setdefault("short_rules", []).append(dict(c["short"]))
        st_v = _mk_strategy(var_def)
        rows = []
        try:
            for sym, segs in segments.items():
                for seg in segs:
                    if seg["regime"] != rid:
                        continue
                    prov = None
                    if seg.get("fs") is not None:
                        try:
                            prov = fast_sim.build_signal_provider(var_def, seg["fs"])
                        except Exception:  # noqa: BLE001
                            prov = None
                    rows.extend(await asyncio.to_thread(
                        simulate_segment, st_v, seg, sym, settings, cfg,
                        should_stop, prov))
        except JobCancelled:
            raise
        except Exception:  # noqa: BLE001 – einzelne Kandidaten isolieren
            continue
        m = metrics_from_rows(rows, cfg.get("max_capital", 100))
        if progress:
            progress(1)
        if m["trades"] < min_trades:
            continue
        sc = _score(m, objective)
        if sc > base_sc * 1.1 + 1e-9 and (best is None or sc > best["score"]):
            best = {"rule_label": c["label"], "rule_long": c["long"],
                    "rule_short": c["short"], "definition": var_def,
                    "metrics": m, "score": round(sc, 3),
                    "improvement_pct": round((sc - base_sc) / max(abs(base_sc), 1e-9) * 100, 1)}
    return best


def build_verdict(dyn_test: Dict, stat_test: Dict, n_regimes: int,
                  switches: int) -> Dict:
    """Ehrlicher Vergleich: dynamisch nur empfehlen, wenn auf den UNBEKANNTEN
    Testdaten klar besser (PnL höher, Drawdown nicht deutlich schlechter)."""
    dp, sp = float(dyn_test.get("pnl") or 0), float(stat_test.get("pnl") or 0)
    dd_d = float(dyn_test.get("max_drawdown") or 0)
    dd_s = float(stat_test.get("max_drawdown") or 0)
    reasons = []
    positive = dp > 0
    better_pnl = dp > sp * 1.05 if sp > 0 else dp > sp
    dd_ok = dd_d <= max(dd_s * 1.25, dd_s + 0.5)
    enough = (dyn_test.get("trades") or 0) >= 5
    if not positive:
        reasons.append(f"Dynamisches Test-PnL ist negativ ({dp:.2f}) – kein Mehrwert nachweisbar")
    if not enough:
        reasons.append(f"Zu wenige Test-Trades ({dyn_test.get('trades')}) für eine belastbare Aussage")
    if better_pnl:
        reasons.append(f"Test-PnL dynamisch {dp:.2f} vs. statisch {sp:.2f}")
    else:
        reasons.append(f"Dynamisch NICHT klar besser im Test-PnL ({dp:.2f} vs. {sp:.2f})")
    if not dd_ok:
        reasons.append(f"Drawdown dynamisch schlechter ({dd_d:.2f} vs. {dd_s:.2f})")
    dynamic_better = bool(better_pnl and dd_ok and enough and positive)
    rec = ("Dynamische Strategie empfohlen – sie schlägt die statische Benchmark "
           "auf unbekannten Testdaten." if dynamic_better else
           "Statische Strategie bevorzugen – die dynamische Variante bringt auf den "
           "Testdaten keinen nachweisbaren Mehrwert. Komplexität nur erhöhen, wenn "
           "sie nachweislich bessere Ergebnisse liefert.")
    return {"dynamic_better": dynamic_better, "reasons": reasons,
            "recommendation": rec, "regimes": n_regimes, "test_switches": switches}
