"""Deep-Test: erschöpfende Indikator-Suche für den Strategie-Finder.

Der normale Discovery-Modus ist eine Greedy-Vorwärtssuche: pro Runde wird die
Regel angehängt, die den Score am meisten hebt. Das ist schnell, findet aber
keine Kombinationen, deren Einzelteile schwach sind und die erst zusammen
funktionieren (und umgekehrt).

Der Deep-Test ersetzt die Greedy-Suche durch fünf Phasen:

1. EINZELTEST    – jeder Kandidat allein (Referenzwert je Indikator)
2. PAARE         – ALLE Paar-Kombinationen (dort entsteht der Großteil der
                   Synergie/Anti-Synergie)
3. BEAM-SUCHE    – die besten Paare werden parallel weiterverfolgt
                   (Breite B statt nur 1 Pfad) bis `max_rules`
4. FEINTUNING    – die besten Kombinationen bekommen je `iterations`
                   Schwellenwert-Optimierungen (Standard 50)
5. AUSTAUSCH     – im Sieger wird jede Regel gegen jeden anderen Kandidaten
                   getauscht (lokale Suche), bis keine Verbesserung mehr kommt

Zum Schluss wird ausgewertet, WARUM etwas funktioniert: Beitrag jeder Regel
(Leave-one-out), Synergie je Paar und Trefferhäufigkeit der Indikatoren in den
besten Kombinationen.
"""
import copy
import logging
from itertools import combinations
from typing import Dict, List

from services import robustness
from services.backtester import JobCancelled

logger = logging.getLogger(__name__)

# Voreinstellungen der Tiefe. `pair_cap` begrenzt die Paar-Phase, damit die
# Laufzeit auch bei sehr vielen Kandidaten kalkulierbar bleibt.
PRESETS = {
    "deep": {"beam": 6, "pair_cap": 900, "refine_top": 4, "swap_rounds": 3},
    "extreme": {"beam": 10, "pair_cap": 2500, "refine_top": 8, "swap_rounds": 5},
}


def _def_with(base: Dict, cands: List[Dict]) -> Dict:
    d = copy.deepcopy(base)
    d["long_rules"] = list(base.get("long_rules") or []) + [dict(c["long"]) for c in cands]
    d["short_rules"] = list(base.get("short_rules") or []) + [dict(c["short"]) for c in cands]
    return d


class _Progress:
    """Fortschritt über alle Phasen – die Gesamtzahl wird vorab geschätzt und
    bei Bedarf nachgezogen, damit der Balken nie zurückspringt."""

    def __init__(self, cb, total: int):
        self.cb = cb
        self.total = max(total, 1)
        self.done = 0

    def step(self, n: int, phase: str):
        self.done += n
        if self.done > self.total:
            self.total = self.done + 1
        self.cb(self.done, self.total, phase)


async def run(opt, job, histories, settings, cfg, objective, min_trades, max_rules,
              allowed, progress, iterations, base_definition=None, fs_map=None,
              should_stop=None, pool=None, workers=1, dd_max_pct=None,
              tracker=None, depth: str = "deep", tf_options=None):
    """Rückgabe wie `_discover`, zusätzlich ein Auswertungs-Report.
    `opt` ist das optimizer-Modul (vermeidet zyklische Importe)."""
    p = PRESETS.get(depth) or PRESETS["deep"]
    cands = opt.build_candidates(allowed, tf_options)
    if not cands:
        raise RuntimeError("Keine Indikatoren ausgewählt")
    base = copy.deepcopy(base_definition) if base_definition else {
        "name": "Deep-Test", "indicators": {}, "long_rules": [], "short_rules": []}
    base.setdefault("indicators", {})
    base.setdefault("long_rules", [])
    base.setdefault("short_rules", [])
    chunk = max(int(workers), 1)
    n = len(cands)
    max_rules = max(int(max_rules), 2)

    def score(m):
        return opt._score(m, objective, min_trades, dd_max_pct)

    async def ev(defs):
        out = []
        for i in range(0, len(defs), chunk):
            if should_stop and should_stop():
                raise JobCancelled()
            part = defs[i:i + chunk]
            items = [(opt._mk_strategy(d), settings, cfg) for d in part]
            out.extend(await opt._evaluate_batch(job, pool, items, histories,
                                                 fs_map, should_stop))
        return out

    def track(d, m, sc):
        if tracker is not None:
            tracker.add(robustness.rule_key(d),
                        {"definition": copy.deepcopy(d), "trade_params": {},
                         "metrics": m, "score": round(sc, 3)})

    n_pairs = min(n * (n - 1) // 2, p["pair_cap"])
    est = n + n_pairs + max(max_rules - 2, 0) * p["beam"] * n \
        + p["refine_top"] * iterations + p["swap_rounds"] * max_rules * n
    prog = _Progress(progress, est)
    steps, best = [], None

    # ------------------------------------------------ Phase 1: Einzeltest
    singles = []
    defs = [_def_with(base, [c]) for c in cands]
    ms = await ev(defs)
    for c, d, m in zip(cands, defs, ms):
        sc = score(m)
        track(d, m, sc)
        singles.append({"label": c["label"], "ind": c["ind"], "score": round(sc, 3),
                        "trades": m.get("trades"), "win_rate": m.get("win_rate"),
                        "pnl_pct": m.get("total_pnl_pct")})
    prog.step(len(cands), f"Deep-Test 1/5: {n} Einzel-Indikatoren geprüft")
    singles.sort(key=lambda x: -x["score"])
    single_by_label = {s["label"]: s["score"] for s in singles}
    steps.append({"round": 1, "added": "Einzeltest",
                  "info": f"{n} Kandidaten einzeln bewertet · bester: "
                          f"{singles[0]['label']} ({singles[0]['score']})"})

    # ------------------------------------------------ Phase 2: alle Paare
    # Bei sehr vielen Kandidaten werden die schwächsten Einzelwerte
    # zurückgestellt, damit die Paar-Phase im Budget bleibt.
    pool_labels = [s["label"] for s in singles]
    order = {lab: i for i, lab in enumerate(pool_labels)}
    by_label = {c["label"]: c for c in cands}
    pairs = sorted(combinations(pool_labels, 2),
                   key=lambda ab: order[ab[0]] + order[ab[1]])[:p["pair_cap"]]
    pair_scores = []
    for i in range(0, len(pairs), 200):
        block = pairs[i:i + 200]
        defs = [_def_with(base, [by_label[a], by_label[b]]) for a, b in block]
        ms = await ev(defs)
        for (a, b), d, m in zip(block, defs, ms):
            sc = score(m)
            track(d, m, sc)
            solo = max(single_by_label.get(a, -1e18), single_by_label.get(b, -1e18))
            pair_scores.append({"a": a, "b": b, "score": round(sc, 3),
                                "synergy": round(sc - solo, 3),
                                "trades": m.get("trades"), "metrics": m})
        prog.step(len(block), f"Deep-Test 2/5: Paare {min(i + 200, len(pairs))}/{len(pairs)}")
    pair_scores.sort(key=lambda x: -x["score"])
    if not pair_scores:
        raise RuntimeError("Deep-Test: keine Paar-Kombination auswertbar")
    steps.append({"round": 2, "added": "Paar-Kombinationen",
                  "score": pair_scores[0]["score"],
                  "info": f"{len(pairs)} Paare geprüft · bestes: "
                          f"{pair_scores[0]['a']} + {pair_scores[0]['b']} "
                          f"(Synergie {pair_scores[0]['synergy']:+})"})

    # ------------------------------------------------ Phase 3: Beam-Suche
    beam = [{"labels": [x["a"], x["b"]], "score": x["score"], "metrics": x["metrics"]}
            for x in pair_scores[:p["beam"]]]
    best = max(beam, key=lambda x: x["score"])
    for round_i in range(3, max_rules + 1):
        nxt = []
        for member in beam:
            free = [c for c in cands if c["label"] not in member["labels"]]
            defs = [_def_with(base, [by_label[l] for l in member["labels"]] + [c])
                    for c in free]
            ms = await ev(defs)
            for c, d, m in zip(free, defs, ms):
                sc = score(m)
                track(d, m, sc)
                nxt.append({"labels": member["labels"] + [c["label"]],
                            "score": sc, "metrics": m})
            prog.step(len(free), f"Deep-Test 3/5: Runde {round_i} · "
                                 f"{len(member['labels']) + 1} Regeln")
        if not nxt:
            break
        # gleiche Kombination auf verschiedenen Pfaden nur einmal weiterführen
        seen, uniq = set(), []
        for cand in sorted(nxt, key=lambda x: -x["score"]):
            key = tuple(sorted(cand["labels"]))
            if key in seen:
                continue
            seen.add(key)
            uniq.append(cand)
        beam = uniq[:p["beam"]]
        if beam[0]["score"] > best["score"] + 1e-9:
            best = beam[0]
            steps.append({"round": round_i, "added": beam[0]["labels"][-1],
                          "score": round(beam[0]["score"], 3), "metrics": beam[0]["metrics"]})
            job["best"] = {"rules": opt._labels(_def_with(base, [by_label[l]
                                                                for l in best["labels"]])),
                           "metrics": best["metrics"]}
        else:
            steps.append({"round": round_i, "added": None,
                          "info": "Keine Kombination dieser Größe ist besser – "
                                  "kürzere Kombination gewinnt"})

    # ------------------------------------------------ Phase 4: Feintuning
    finalists = []
    seen = set()
    for cand in sorted([best] + beam + [
            {"labels": [x["a"], x["b"]], "score": x["score"], "metrics": x["metrics"]}
            for x in pair_scores[:p["refine_top"]]], key=lambda x: -x["score"]):
        key = tuple(sorted(cand["labels"]))
        if key in seen:
            continue
        seen.add(key)
        finalists.append(cand)
        if len(finalists) >= p["refine_top"]:
            break
    refine_log = []
    tuned = []
    for i, cand in enumerate(finalists):
        d = _def_with(base, [by_label[l] for l in cand["labels"]])

        def sub_prog(done, total, phase, _i=i):
            prog.step(0, f"Deep-Test 4/5: Feintuning {_i + 1}/{len(finalists)} · {phase}")

        d2, m2, log = await opt._refine(job, d, cand["score"], cand["metrics"],
                                        histories, settings, cfg, objective,
                                        min_trades, iterations, sub_prog, fs_map,
                                        should_stop, dd_max_pct, tracker)
        sc2 = score(m2) if m2 else cand["score"]
        prog.step(iterations, f"Deep-Test 4/5: Feintuning {i + 1}/{len(finalists)}")
        tuned.append({"definition": d2, "metrics": m2, "score": sc2,
                      "labels": cand["labels"]})
        refine_log.extend(log or [])
    tuned.sort(key=lambda x: -x["score"])
    winner = tuned[0]
    steps.append({"round": max_rules + 1, "added": "Feintuning",
                  "score": round(winner["score"], 3),
                  "info": f"{len(finalists)} Kombinationen mit je {iterations} "
                          f"Optimierungen nachgezogen"})

    # ------------------------------------------------ Phase 5: Austausch
    definition = winner["definition"]
    best_m, best_sc = winner["metrics"], winner["score"]
    labels = list(winner["labels"])
    swaps = []
    for sround in range(p["swap_rounds"]):
        improved = False
        for pos in range(len(labels)):
            free = [c for c in cands if c["label"] not in labels]
            if not free:
                break
            trials = []
            for c in free:
                new_labels = list(labels)
                new_labels[pos] = c["label"]
                trials.append((new_labels, _def_with(base, [by_label[l] for l in new_labels])))
            ms = await ev([d for _l, d in trials])
            prog.step(len(trials), f"Deep-Test 5/5: Austausch Regel {pos + 1} "
                                   f"(Runde {sround + 1})")
            for (new_labels, d), m in zip(trials, ms):
                sc = score(m)
                track(d, m, sc)
                if sc > best_sc + 1e-9:
                    best_sc, best_m, definition = sc, m, d
                    labels = new_labels
                    improved = True
                    swaps.append({"round": sround + 1, "position": pos + 1,
                                  "new_rule": new_labels[pos], "score": round(sc, 3)})
        if not improved:
            break
    if swaps:
        steps.append({"round": max_rules + 2, "added": "Austausch",
                      "score": round(best_sc, 3),
                      "info": f"{len(swaps)} Regel(n) durch bessere ersetzt"})

    # ------------------------------------------------ Auswertung
    contribution = []
    if len(labels) > 1:
        defs, keep = [], []
        for pos in range(len(labels)):
            rest = [by_label[l] for i2, l in enumerate(labels) if i2 != pos]
            defs.append(_def_with(base, rest))
            keep.append(labels[pos])
        ms = await ev(defs)
        for lab, m in zip(keep, ms):
            contribution.append({"rule": lab, "score_without": round(score(m), 3),
                                 "delta": round(best_sc - score(m), 3)})
        contribution.sort(key=lambda x: -x["delta"])
        prog.step(len(defs), "Deep-Test: Beitrag je Regel")

    freq: Dict[str, int] = {}
    for x in pair_scores[:max(20, p["beam"] * 2)]:
        for lab in (x["a"], x["b"]):
            freq[by_label[lab]["ind"]] = freq.get(by_label[lab]["ind"], 0) + 1
    top_inds = sorted(freq.items(), key=lambda kv: -kv[1])[:8]

    # Kombinationen unter der Mindest-Trade-Zahl tragen eine Strafe von -1e9 und
    # würden jede Synergie-Statistik unbrauchbar machen -> hier ausklammern.
    valid_pairs = [x for x in pair_scores if x["score"] > -9e8]
    report = {
        "depth": depth,
        "candidates": n,
        "evaluations": (job.get("_bench") or {}).get("evaluations"),
        "singles": singles[:15],
        "worst_singles": [s for s in singles if s["score"] > -9e8][-5:],
        "pairs": [{k: v for k, v in x.items() if k != "metrics"}
                  for x in valid_pairs[:15]],
        "best_synergies": [{k: v for k, v in x.items() if k != "metrics"}
                           for x in sorted(valid_pairs, key=lambda x: -x["synergy"])[:10]],
        "worst_synergies": [{k: v for k, v in x.items() if k != "metrics"}
                            for x in sorted(valid_pairs, key=lambda x: x["synergy"])[:10]],
        "pairs_tested": len(pairs),
        "pairs_valid": len(valid_pairs),
        "contribution": contribution,
        "indicator_frequency": [{"indicator": k, "count": v} for k, v in top_inds],
        "swaps": swaps,
        "final_rules": labels,
        "conclusion": _conclusion(labels, contribution, valid_pairs, top_inds, best_sc),
    }
    return definition, best_m, best_sc, steps, report, refine_log


def _conclusion(labels, contribution, pair_scores, top_inds, best_sc) -> str:
    parts = [f"Beste Kombination ({len(labels)} Regeln, Score {best_sc:.2f}): "
             + " + ".join(labels)]
    if contribution:
        strongest = contribution[0]
        weakest = contribution[-1]
        parts.append(f"Wichtigste Regel: {strongest['rule']} "
                     f"(ohne sie {strongest['delta']:+.2f} Score)")
        if weakest["delta"] <= 0.01:
            parts.append(f"'{weakest['rule']}' trägt nichts bei – Kandidat zum Entfernen")
    if pair_scores:
        syn = max(pair_scores, key=lambda x: x["synergy"])
        anti = min(pair_scores, key=lambda x: x["synergy"])
        parts.append(f"Stärkste Synergie: {syn['a']} + {syn['b']} ({syn['synergy']:+.2f})")
        parts.append(f"Schlechteste Paarung: {anti['a']} + {anti['b']} ({anti['synergy']:+.2f})")
    if top_inds:
        parts.append("Häufigste Indikatoren in den Top-Kombis: "
                     + ", ".join(f"{k} ({v}x)" for k, v in top_inds[:4]))
    return " · ".join(parts)
