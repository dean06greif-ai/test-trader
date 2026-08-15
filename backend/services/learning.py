"""Lern-Gedächtnis: sammelt Ergebnisse der Robustheits-Tests je Marktphase
und stellt sie für spätere, gezieltere Suchen bereit.

Gespeichert wird pro Lauf & Marktregime, welche Strategie/Indikatoren wie gut
funktioniert haben. Genutzt wird das u.a. für die Reihenfolge der
Regel-Varianten im Dynamik-Modus und als Übersicht für den Nutzer.
"""
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


def _indicators_of(definition: Optional[Dict]) -> List[str]:
    if not definition:
        return []
    inds = set()
    for r in (definition.get("long_rules") or []) + (definition.get("short_rules") or []):
        for v in (r.get("indicator"), r.get("value")):
            if isinstance(v, str) and v not in ("price",):
                inds.add(v)
    return sorted(inds)


async def record_run(db, result: Dict):
    """Lern-Einträge aus einem fertigen Optimizer-Lauf extrahieren."""
    if db is None or not result:
        return
    rows = []
    now = datetime.now(timezone.utc).isoformat()
    base = {"at": now, "mode": result.get("mode"),
            "timeframe": result.get("timeframe"), "days": result.get("days"),
            "strategy_id": result.get("strategy_id"),
            "strategy_name": result.get("strategy_name")}
    try:
        if result.get("mode") == "dynamic":
            dy = result.get("dynamic") or {}
            inds = _indicators_of((dy.get("base_definition") or {}))
            for r in dy.get("regimes") or []:
                m = r.get("metrics") or {}
                if not m.get("trades"):
                    continue
                rows.append({**base, "regime_label": r.get("label"),
                             "regime_share_pct": r.get("share_pct"),
                             "indicators": inds, "config": r.get("config"),
                             "pnl": m.get("pnl"), "trades": m.get("trades"),
                             "win_rate": m.get("win_rate"),
                             "source": "dynamic_regime"})
        else:
            top5 = result.get("top5") or []
            best = top5[0] if top5 else None
            if best:
                inds = _indicators_of(best.get("definition"))
                wf = (best.get("wf") or {})
                m = best.get("metrics") or {}
                regimes = best.get("regimes") or {}
                for label, rm in regimes.items():
                    if not rm.get("trades"):
                        continue
                    rows.append({**base, "regime_label": label,
                                 "indicators": inds, "pnl": rm.get("pnl"),
                                 "trades": rm.get("trades"),
                                 "win_rate": rm.get("win_rate"),
                                 "wf_score": wf.get("wf_score"),
                                 "source": "regime_analysis"})
                if m.get("trades"):
                    rows.append({**base, "regime_label": None, "indicators": inds,
                                 "pnl": m.get("pnl"), "trades": m.get("trades"),
                                 "win_rate": m.get("win_rate"),
                                 "wf_score": wf.get("wf_score"),
                                 "passed": best.get("passed"),
                                 "source": "run"})
        if rows:
            await db.learning_memory.insert_many(rows)
    except Exception as e:  # noqa: BLE001 – Lernen darf nie einen Lauf killen
        logger.warning(f"learning record failed: {e}")


async def indicator_weights(db) -> Dict[str, float]:
    """Gewicht je Indikator aus der Historie: profitabel & oft erfolgreich = hoch.
    Neutral (1.0), wenn keine/wenig Daten – kein Eintrag wird ausgeschlossen."""
    if db is None:
        return {}
    try:
        rows = await db.learning_memory.find({"indicators": {"$ne": []}}) \
            .sort("at", -1).to_list(500)
    except Exception:  # noqa: BLE001
        return {}
    stats: Dict[str, List[float]] = {}
    for r in rows:
        pnl = r.get("pnl")
        trades = r.get("trades") or 0
        if pnl is None or trades < 3:
            continue
        for ind in r.get("indicators") or []:
            stats.setdefault(ind, []).append(1.0 if pnl > 0 else -1.0)
    weights = {}
    for ind, vals in stats.items():
        if len(vals) < 2:
            continue
        rate = sum(vals) / len(vals)  # -1..1
        weights[ind] = round(min(max(1.0 + rate * 0.8, 0.4), 2.0), 3)
    return weights


async def summary(db, limit: int = 300) -> Dict:
    """Übersicht fürs UI: welche Indikatoren/Strategien liefen je Marktphase
    historisch am besten."""
    if db is None:
        return {"entries": 0, "regimes": []}
    rows = await db.learning_memory.find().sort("at", -1).to_list(limit)
    by_regime: Dict[str, Dict] = {}
    for r in rows:
        label = r.get("regime_label") or "Gesamt (ohne Regime)"
        g = by_regime.setdefault(label, {"label": label, "runs": 0, "profitable": 0,
                                         "indicators": {}, "strategies": {}})
        g["runs"] += 1
        if (r.get("pnl") or 0) > 0:
            g["profitable"] += 1
        for ind in r.get("indicators") or []:
            s = g["indicators"].setdefault(ind, {"n": 0, "pnl": 0.0})
            s["n"] += 1
            s["pnl"] += float(r.get("pnl") or 0)
        name = r.get("strategy_name") or r.get("strategy_id")
        if name:
            s = g["strategies"].setdefault(name, {"n": 0, "pnl": 0.0})
            s["n"] += 1
            s["pnl"] += float(r.get("pnl") or 0)
    out = []
    for g in by_regime.values():
        top_ind = sorted(g["indicators"].items(), key=lambda kv: -kv[1]["pnl"])[:5]
        top_str = sorted(g["strategies"].items(), key=lambda kv: -kv[1]["pnl"])[:3]
        out.append({"label": g["label"], "runs": g["runs"],
                    "profitable_pct": round(g["profitable"] / g["runs"] * 100, 0) if g["runs"] else 0,
                    "top_indicators": [{"indicator": k, "n": v["n"], "pnl": round(v["pnl"], 2)}
                                       for k, v in top_ind],
                    "top_strategies": [{"name": k, "n": v["n"], "pnl": round(v["pnl"], 2)}
                                       for k, v in top_str]})
    out.sort(key=lambda x: -x["runs"])
    return {"entries": len(rows), "regimes": out}
