"""Endlos-Suche (Deep Explore): sucht so lange neue Indikator-Kombinationen,
bis genügend Kombinationen sowohl die normale Suche (Training) ALS AUCH den
Walk-Forward-Test (unbekannte Testdaten) bestehen – mit ähnlich gutem Ergebnis
auf beiden Datensätzen.

Ablauf pro Runde:
1. Batch zufälliger, noch nie getesteter Kombinationen ziehen
   (gewichtete Auswahl nach bisheriger Trefferquote + 35% reine Exploration,
   damit auch "vermeintlich sinnlose" Indikatoren immer wieder drankommen)
2. Positive Kombinationen werden je `iterations` Mal feinjustiert (Schwellen)
3. Bestandene Kandidaten laufen durch den Walk-Forward auf den Holdout-Daten
4. Champions (Training positiv + Test positiv + Konsistenz >= X%) kommen in
   die Top-5 – die Suche läuft weiter, bis das Ziel erreicht ist, das
   Zeitlimit greift oder der Nutzer stoppt (Stop-Button, Bestes bleibt)

Jeder Lauf nutzt einen frischen Zufalls-Seed -> zwei Läufe liefern nie
dieselbe Suche. Die globale Top-5 wird serverseitig über alle Läufe gemerkt.
"""
import copy
import logging
import math
import random
import time
from datetime import datetime, timezone
from typing import Dict, List

from services import robustness
from services.backtester import JobCancelled

logger = logging.getLogger(__name__)

MAX_SEEN = 250000          # Sicherheitsdeckel für das Duplikat-Gedächtnis
REFINE_PER_BATCH = 3       # wie viele Positive je Batch verfeinert werden
EPSILON = 0.35             # Anteil rein zufälliger Kombis (Exploration)


def _def_with(base: Dict, cands: List[Dict]) -> Dict:
    d = copy.deepcopy(base)
    d["long_rules"] = list(base.get("long_rules") or []) + [dict(c["long"]) for c in cands]
    d["short_rules"] = list(base.get("short_rules") or []) + [dict(c["short"]) for c in cands]
    return d


async def run(opt, job, histories, test_hist, settings, cfg, objective,
              min_trades, max_rules, allowed, progress, iterations,
              fs_map, fs_test, should_stop, pool, workers, dd_max_pct,
              train_days, test_days, explore_cfg, base_definition=None,
              tf_options=None):
    """Rückgabe: (champions_top5, report). `opt` = optimizer-Modul."""
    cands = opt.build_candidates(allowed, tf_options)
    if len(cands) < 2:
        raise RuntimeError("Endlos-Suche braucht mindestens 2 Indikator-Kandidaten")
    if not test_hist:
        raise RuntimeError("Endlos-Suche braucht einen Walk-Forward-Split (Testdaten)")
    ecfg = explore_cfg or {}
    target = min(max(int(ecfg.get("target_champions") or 5), 1), 10)
    try:
        max_minutes = float(ecfg.get("max_minutes") or 0)
    except (TypeError, ValueError):
        max_minutes = 0.0
    max_minutes = min(max(max_minutes, 0.0), 1440.0)
    try:
        min_consistency = float(ecfg.get("min_consistency_pct") or 40)
    except (TypeError, ValueError):
        min_consistency = 40.0
    min_consistency = min(max(min_consistency, 0.0), 95.0)
    # Frischer Seed je Lauf: zwei Läufe erkunden NIE denselben Pfad
    rng = random.Random(ecfg.get("seed") or time.time_ns())

    base = copy.deepcopy(base_definition) if base_definition else {
        "name": "Endlos-Suche", "indicators": {}, "long_rules": [], "short_rules": []}
    base.setdefault("indicators", {})
    base.setdefault("long_rules", [])
    base.setdefault("short_rules", [])
    max_rules = min(max(int(max_rules), 2), 6)
    labels = [c["label"] for c in cands]
    by_label = {c["label"]: c for c in cands}
    sizes = [x for x in (2, 2, 2, 3, 3, 3, 4, 4, 5, 6) if x <= max_rules]

    def score(m):
        return opt._score(m, objective, min_trades, dd_max_pct)

    async def ev(defs, hists, fsm):
        out = []
        chunk = max(int(workers), 1)
        for i in range(0, len(defs), chunk):
            if should_stop and should_stop():
                raise JobCancelled()
            part = defs[i:i + chunk]
            items = [(opt._mk_strategy(d), settings, cfg) for d in part]
            out.extend(await opt._evaluate_batch(job, pool, items, hists, fsm,
                                                 should_stop))
        return out

    stats = {lab: {"n": 0, "pos": 0, "best": -1e18} for lab in labels}
    seen = set()
    champions: List[Dict] = []
    near_misses: List[Dict] = []
    tested = refined = wf_checked = 0
    t0 = time.time()
    stop_reason = None
    total_space = sum(math.comb(len(cands), k)
                      for k in range(2, max_rules + 1))

    def sample_combo():
        for _ in range(60):
            k = min(rng.choice(sizes), len(cands))
            if rng.random() < EPSILON:
                combo = rng.sample(labels, k)
            else:
                pool_l = list(labels)
                pool_w = []
                for lab in pool_l:
                    s = stats[lab]
                    rate = s["pos"] / s["n"] if s["n"] else 0.25
                    pool_w.append(0.5 + 3.0 * rate)
                combo = []
                for _i in range(k):
                    pick = rng.choices(range(len(pool_l)), weights=pool_w)[0]
                    combo.append(pool_l.pop(pick))
                    pool_w.pop(pick)
            key = tuple(sorted(combo))
            if key not in seen:
                seen.add(key)
                return combo
        return None

    def phase_txt():
        el = max(time.time() - t0, 1e-6)
        rate = tested / (el / 60.0)
        best_txt = (f" · Best-WF {champions[0]['wf']['wf_score']:.2f}"
                    if champions else "")
        return (f"Endlos-Suche · {tested} Kombis · {refined} verfeinert · "
                f"{len(champions)}/{target} Champions{best_txt} · "
                f"{rate:.0f} Kombis/min")

    def pct():
        if max_minutes:
            return min((time.time() - t0) / (max_minutes * 60) * 100, 99)
        return min(len(champions) / target * 100, 99)

    def stopping():
        if job.get("stop_explore"):
            return "stopped_by_user"
        if max_minutes and time.time() - t0 > max_minutes * 60:
            return "time_limit"
        if len(champions) >= target:
            return "target_reached"
        return None

    def add_champion(entry):
        key = tuple(sorted(entry["labels"]))
        champions[:] = [c for c in champions
                        if tuple(sorted(c["labels"])) != key]
        champions.append(entry)
        champions.sort(key=lambda x: -x["wf"]["wf_score"])
        del champions[10:]
        job["best"] = {"rules": entry["rules"], "metrics": entry["metrics"],
                       "explore": {"champions": len(champions),
                                   "wf_score": entry["wf"]["wf_score"],
                                   "test_pnl": (entry["test_metrics"] or {}).get("pnl")}}

    batch_n = max(int(workers) * 2, 8)
    while True:
        stop_reason = stopping()
        if stop_reason:
            break
        combos, defs = [], []
        for _ in range(batch_n):
            c = sample_combo()
            if c is None:
                break
            combos.append(c)
            defs.append(_def_with(base, [by_label[l] for l in c]))
        if not combos:
            stop_reason = "space_exhausted"
            break
        ms = await ev(defs, histories, fs_map)
        positives = []
        for combo, d, m in zip(combos, defs, ms):
            tested += 1
            sc = score(m)
            for lab in combo:
                s = stats[lab]
                s["n"] += 1
                if sc > 0:
                    s["pos"] += 1
                if sc > s["best"]:
                    s["best"] = sc
            if sc > 0:
                positives.append({"labels": combo, "definition": d,
                                  "metrics": m, "score": sc})
        progress(pct(), phase_txt())

        positives.sort(key=lambda x: -x["score"])
        for cand in positives[:REFINE_PER_BATCH]:
            if stopping():
                break

            def sub_prog(done, total, phase):
                progress(pct(), f"{phase_txt()} · {phase}")

            d2, m2, _log = await opt._refine(
                job, cand["definition"], cand["score"], cand["metrics"],
                histories, settings, cfg, objective, min_trades, iterations,
                sub_prog, fs_map, should_stop, dd_max_pct, None)
            refined += 1
            m_fin = m2 or cand["metrics"]
            sc2 = score(m_fin)
            if sc2 <= 0:
                continue
            test_m = (await ev([d2], test_hist, fs_test))[0]
            wf_checked += 1
            wf = robustness.walk_forward_eval(m_fin, test_m, train_days, test_days)
            entry = {"labels": list(cand["labels"]), "definition": d2,
                     "metrics": m_fin, "test_metrics": test_m, "wf": wf,
                     "score": round(sc2, 3), "rules": opt._labels(d2),
                     "found_at": datetime.now(timezone.utc).isoformat()}
            if float(test_m.get("pnl") or 0) > 0 and \
                    wf["consistency_pct"] >= min_consistency:
                add_champion(entry)
            else:
                near_misses.append({
                    "labels": entry["labels"], "score": entry["score"],
                    "train_pnl": m_fin.get("pnl"), "test_pnl": test_m.get("pnl"),
                    "consistency_pct": wf["consistency_pct"],
                    "reason": ("Test-PnL negativ"
                               if float(test_m.get("pnl") or 0) <= 0
                               else f"Konsistenz {wf['consistency_pct']}% < {min_consistency}%")})
                near_misses.sort(key=lambda x: -x["score"])
                del near_misses[5:]
            progress(pct(), phase_txt())
        if len(seen) >= MAX_SEEN:
            stop_reason = "space_exhausted"
            break

    elapsed = max(time.time() - t0, 1e-6)
    ind_stats = sorted(
        [{"indicator": by_label[lab]["ind"], "label": lab, "tried": s["n"],
          "positive_pct": round(s["pos"] / s["n"] * 100, 1) if s["n"] else 0.0}
         for lab, s in stats.items() if s["n"]],
        key=lambda x: -x["positive_pct"])[:12]
    top = champions[:5]
    report = {
        "mode": "explore", "tested": tested, "refined": refined,
        "wf_checked": wf_checked, "champions_found": len(champions),
        "champions": top, "near_misses": near_misses,
        "elapsed_seconds": round(elapsed, 1),
        "combos_per_min": round(tested / (elapsed / 60), 1),
        "stop_reason": stop_reason, "target_champions": target,
        "max_minutes": max_minutes, "min_consistency_pct": min_consistency,
        "candidates": len(cands), "total_space": total_space,
        "space_seen_pct": round(len(seen) / total_space * 100, 4) if total_space else 0.0,
        "evaluations": (job.get("_bench") or {}).get("evaluations"),
    }
    return top, report


async def persist_best(db, result):
    """Globale Top-5 über ALLE Endlos-Läufe (dedupe per Regel-Schlüssel)."""
    if db is None:
        return
    champs = ((result or {}).get("explore_report") or {}).get("champions") or []
    if not champs:
        return
    doc = await db.settings.find_one({"_id": "deep_explore_best"}) or {}
    items = doc.get("items") or []

    def wf_score(x):
        return (x.get("wf") or {}).get("wf_score", -1e18)

    by_key = {robustness.rule_key(i.get("definition") or {}): i for i in items}
    for c in champs:
        key = robustness.rule_key(c.get("definition") or {})
        cur = by_key.get(key)
        if cur is None or wf_score(c) > wf_score(cur):
            by_key[key] = c
    merged = sorted(by_key.values(), key=lambda x: -wf_score(x))[:5]
    await db.settings.update_one(
        {"_id": "deep_explore_best"},
        {"$set": {"items": merged,
                  "updated_at": datetime.now(timezone.utc).isoformat()}},
        upsert=True)
