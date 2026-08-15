"""Regime-gezielte Strategie-Suche + finaler Walk-Forward-Test.

- run_regime_optimizer: Strategie-Discovery und/oder Trade-Parameter-Optimierung
  NUR auf den Kerzen-Abschnitten EINES ausgewählten Regimes einer gespeicherten
  Regime-Analyse (alle Einstellungsmöglichkeiten des normalen Optimizers:
  Indikator-Auswahl, Iterationen, Ziel, Min-Trades, Regel-Anzahl, Trade-Räume,
  optional anderer Timeframe). Ergebnis: Top-5-Kandidaten zur Auswahl.
- run_walkforward: die aus den bestätigten Regime-Strategien zusammengestellte
  dynamische Strategie auf dem unangetasteten Holdout testen – Klassifikation
  rein rückblickend, identisch zum Live-/Paper-Verhalten (kein Lookahead).
"""
import logging
from datetime import datetime, timezone
from typing import Dict, List

from services import dynamic_strategy as dyn
from services import regime as rg
from services import regime_lab as lab
from services.backtester import JobCancelled

logger = logging.getLogger(__name__)


def _stop_fn(job):
    def stop():
        return bool(job.get("cancel"))
    return stop


async def _load_doc(body: Dict, db) -> Dict:
    """Analyse-Dokument beschaffen: auf dem Server aus Mongo, auf dem lokalen
    Worker aus dem mitgeschickten Payload (Worker hat keinen DB-Zugriff)."""
    doc = body.get("analysis_doc")
    if doc is None and db is not None:
        doc = await db.regime_analyses.find_one({"id": body.get("analysis_id")})
    if not doc:
        raise RuntimeError("Regime-Analyse nicht gefunden")
    return doc


def _make_seg_pool(*segment_maps):
    """Multi-Core (nur mit SIM_WORKERS>1, d.h. lokaler Worker): Regime-Abschnitte
    auf alle Kerne verteilen – gleicher Mechanismus wie der Dynamik-Modus.
    In der Cloud bleibt alles sequenziell (SIM_WORKERS=1)."""
    try:
        from services import parallel_sim
        n_workers = parallel_sim.workers_configured()
        if n_workers <= 1:
            return None
        seg_data = dyn.register_segments(*[s for s in segment_maps if s])
        if not seg_data:
            return None
        pool = parallel_sim.make_pool(seg_data, n_workers)
        dyn.set_pool(pool)
        logger.info(f"Regime-Lab Multi-Core: {len(seg_data)} Abschnitte auf "
                    f"{n_workers} Kerne verteilt")
        return pool
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Regime-Lab Multi-Core deaktiviert: {e}")
        dyn.set_pool(None)
        return None


def _close_seg_pool(pool, cancelled: bool):
    dyn.set_pool(None)
    if pool is not None:
        try:
            from services import parallel_sim
            parallel_sim.close_pool(pool, kill=cancelled)
        except Exception:  # noqa: BLE001
            pass


def _guarded(m: Dict, objective: str, min_trades: int) -> float:
    from services.dynamic_strategy import _guarded_score
    return _guarded_score(m, objective, min_trades)


async def _build_regime_segments(doc: Dict, scope: str, symbol: str,
                                 regime_id: int, timeframe: str, job: Dict,
                                 only_train: bool = True) -> Dict[str, List[Dict]]:
    """Kerzen laden und die gespeicherten Regime-Zeitbereiche auf Segmente
    abbilden. Bei abweichendem Timeframe werden die Zeitbereiche der Analyse
    auf die neuen Kerzen übertragen (Regime bleiben identisch definiert)."""
    syms = [symbol] if scope == "per_coin" else list(doc.get("symbols") or [])
    end_ts = {s: (doc.get("bounds") or {}).get(s, {}).get("end_ts") for s in syms}
    histories = await lab.fetch_histories(syms, int(doc["days"]), timeframe,
                                          job, end_ts=end_ts)
    segments: Dict[str, List[Dict]] = {}
    for sym, candles in histories.items():
        ranges = lab.regime_ranges(doc, scope, symbol, sym, regime_id, only_train)
        segs = lab.segments_from_ranges(candles, ranges, regime_id, dyn.WARMUP_BARS)
        if segs:
            segments[sym] = segs
    return segments


def allowed_sides_for(bias: str, regime_id: int, mode) -> List[str]:
    """Richtungs-Bias (Etappe 3) -> erlaubte Trade-Seiten.
    'auto' leitet aus der Regime-Richtung ab: Aufwärts -> nur Longs,
    Abwärts -> nur Shorts, Seitwärts -> beide (None). 'long'/'short' = fest,
    alles andere ('off') = None (unverändertes Verhalten)."""
    from services import regime_engine as eng
    b = str(bias or "off").lower()
    if b == "long":
        return ["LONG"]
    if b == "short":
        return ["SHORT"]
    if b != "auto":
        return None
    d = eng.split_id(int(regime_id), eng.norm_mode(mode))[0]
    return ["LONG"] if d == 2 else ["SHORT"] if d == 0 else None


async def run_regime_optimizer(job_id: str, body: Dict, registry, settings: Dict,
                               default_cfg: Dict, db):
    """Discovery/Optimierung für EIN Regime einer gespeicherten Analyse."""
    from services.optimizer import build_trade_space, _mk_strategy
    job = lab.JOBS[job_id]
    stop = _stop_fn(job)
    seg_pool = None
    try:
        doc = await _load_doc(body, db)
        if not doc:
            raise RuntimeError("Regime-Analyse nicht gefunden")
        scope = body.get("scope") or "combined"
        symbol = body.get("symbol")
        regime_id = int(body.get("regime_id"))
        model = lab.model_for(doc, scope, symbol)
        if not model:
            raise RuntimeError("Kein Regime-Modell für diesen Bereich gespeichert")
        reg_meta = next((r for r in model.get("regimes") or []
                         if r["id"] == regime_id), None)
        if not reg_meta:
            raise RuntimeError("Regime nicht im Modell gefunden")

        mode = body.get("mode") or "combo"
        objective = body.get("objective") or "combo"
        iterations = int(min(max(int(body.get("iterations") or 40), 0), 500))
        min_trades = max(int(body.get("min_trades") or 10), 1)
        max_rules = int(min(max(int(body.get("max_rules") or 4), 1), 8))
        indicators = [i for i in (body.get("indicators") or []) if isinstance(i, str)]
        timeframe = body.get("timeframe") or doc.get("timeframe")
        regime_wf = body.get("regime_walk_forward", True)
        regime_train_pct = float(min(max(float(body.get("regime_train_pct") or 75), 40), 95))
        cfg = dict(default_cfg)
        for k in ("max_capital", "leverage", "fee_percent"):
            if body.get(k) is not None:
                cfg[k] = body[k]
        # --- Richtungs-Bias (Etappe 3): Aufwärts-Regime -> nur Longs,
        # Abwärts-Regime -> nur Shorts, Seitwärts -> beide Seiten.
        # 'off' (Default) = unverändertes Verhalten, 'auto' = aus der
        # Regime-Richtung abgeleitet, 'long'/'short' = fest vorgegeben.
        from services import regime_engine as eng
        bias = str(body.get("direction_bias") or "off").lower()
        mode_n = eng.norm_mode((model.get("config") or {}).get(
            "regime_mode", model.get("regime_mode", eng.DEFAULT_REGIME_MODE)))
        reg_dir = eng.split_id(regime_id, mode_n)[0]
        allowed_sides = allowed_sides_for(bias, regime_id, mode_n)
        if allowed_sides:
            cfg["allowed_sides"] = allowed_sides
        trade_space = build_trade_space(body.get("optimize")
                                        or {"tpsl": True, "leverage": True})

        segments = await _build_regime_segments(doc, scope, symbol, regime_id,
                                                timeframe, job)
        if not segments:
            raise RuntimeError("Keine Kerzen-Abschnitte für dieses Regime im "
                               "Trainingsbereich gefunden")
        n_bars = sum(s["n_bars"] for ss in segments.values() for s in ss)
        n_segs = sum(len(ss) for ss in segments.values())
        train_segs, val_segs = ((segments, None) if not regime_wf
                                else dyn.split_segments(segments, regime_train_pct))
        if not train_segs:
            train_segs, val_segs = segments, None
        seg_pool = _make_seg_pool(train_segs, val_segs)

        base_definition = None
        strategy = None
        if mode in ("discovery", "combo"):
            bsid = body.get("base_strategy_id")
            if bsid:
                bstrat = registry.get(bsid)
                if not bstrat or not getattr(bstrat, "IS_CUSTOM", False):
                    raise RuntimeError("Basis-Strategie muss eine Custom-Strategie sein")
                import copy
                base_definition = copy.deepcopy(bstrat.definition)
        else:
            strategy = registry.get(body.get("strategy_id"))
            if not strategy:
                raise RuntimeError("Strategie nicht gefunden")

        from services.optimizer import build_candidates
        n_cands = len(build_candidates(indicators or None))
        deep = bool(body.get("deep_test"))
        if deep:
            # Einzeltest + alle Paare + Beam-Suche + Austausch
            n_pairs = min(n_cands * (n_cands - 1) // 2, 600)
            disc_work = (n_cands + n_pairs + 5 * n_cands * max(max_rules - 2, 0)
                         + 2 * max_rules * n_cands) if mode in ("discovery", "combo") else 0
        else:
            disc_work = n_cands * max_rules if mode in ("discovery", "combo") else 0
        total_work = max(disc_work + iterations, 1)
        done = [0]

        def prog(_n=1):
            done[0] += 1
            job["progress"] = 15 + round(done[0] / total_work * 80)

        def set_phase(txt):
            job["phase"] = txt

        discovery = None
        if mode in ("discovery", "combo"):
            job["phase"] = f"Regime '{reg_meta['label']}': eigene Strategie suchen"
            disc = await dyn.discover_regime_strategy(
                train_segs, regime_id, settings, cfg, indicators,
                base_definition, objective, min_trades, max_rules, {},
                prog, stop, val_segments=val_segs, phase_cb=set_phase, deep=deep)
            if not disc.get("definition") and val_segs:
                # Fallback: keine Variante hat den Phasen-Walk-Forward bestanden.
                # Beste Kombination OHNE diese Hürde trotzdem anbieten – klar als
                # "nicht validiert" markiert, damit der Nutzer selbst entscheidet.
                set_phase(f"Regime '{reg_meta['label']}': beste Kombination ohne "
                          f"bestandenen Walk-Forward ermitteln")
                disc_nv = await dyn.discover_regime_strategy(
                    train_segs, regime_id, settings, cfg, indicators,
                    base_definition, objective, min_trades, max_rules, {},
                    prog, stop, val_segments=None, phase_cb=set_phase, deep=deep)
                if disc_nv.get("definition") and disc_nv.get("rules"):
                    disc_nv["note"] = (disc.get("note") or "") + \
                        " · Angezeigt wird die beste NICHT validierte Kombination"
                    disc_nv["validation_passed"] = False
                    disc = disc_nv
            discovery = {"rules": disc.get("rules") or [],
                         "steps": disc.get("steps") or [],
                         "deep_test": deep,
                         "deep_report": disc.get("deep_report"),
                         "metrics": disc.get("metrics"),
                         "validation": disc.get("validation"),
                         "validation_passed": disc.get("validation_passed"),
                         "note": disc.get("note"),
                         "definition": disc.get("definition")}
            if disc.get("definition"):
                strategy = _mk_strategy(disc["definition"])
            elif base_definition:
                strategy = _mk_strategy(base_definition)
            else:
                strategy = None

        top5 = []
        if strategy is not None:
            import random
            rng = random.Random(1000 + regime_id)
            sid = getattr(strategy, "STRATEGY_ID", "") or ""
            # Optional: auch die Strategie-Parameter (Perioden, Schwellen) für
            # dieses Regime durchsuchen – z.B. um NNFX-Strategien je Marktphase
            # zu justieren. Ohne Flag bleibt das Verhalten wie bisher.
            p_space = {}
            if body.get("optimize_strategy_params") and not getattr(strategy, "IS_CUSTOM", False):
                from services.optimizer import strategy_param_space
                p_space = strategy_param_space(
                    strategy, skip_binary=not body.get("include_flag_params"))
                keys = body.get("strategy_param_keys")
                if keys:
                    p_space = {k: v for k, v in p_space.items() if k in set(keys)}
            job["phase"] = (f"Regime '{reg_meta['label']}': "
                            + ("Strategie- und Trade-Parameter testen" if p_space
                               else "Trade-Parameter testen"))
            base_m = await dyn.eval_regime_config(strategy, train_segs, regime_id,
                                                  settings, cfg, stop)
            cands = [({}, {}, base_m)]
            if (trade_space or p_space) and iterations > 0:
                for it in range(iterations):
                    if stop():
                        raise JobCancelled()
                    set_phase(f"Regime '{reg_meta['label']}': Parameter "
                              f"{it + 1}/{iterations}")
                    tp = dyn.sample_config(trade_space, rng) if trade_space else {}
                    sp = ({k: rng.choice(v) for k, v in p_space.items()}
                          if p_space else {})
                    m = await dyn.eval_regime_config(
                        strategy, train_segs, regime_id,
                        dyn.with_strategy_params(settings, sid, sp),
                        {**cfg, **tp}, stop)
                    cands.append((tp, sp, m))
                    prog()
            cands.sort(key=lambda x: -_guarded(x[2], objective, min_trades))
            seen, uniq = set(), []
            for tp, sp, m in cands:
                key = f"{sorted(tp.items())}|{sorted(sp.items())}"
                if key not in seen:
                    seen.add(key)
                    uniq.append((tp, sp, m))
            min_val = max(int(min_trades * 0.4), 3)
            for tp, sp, m in uniq[:8]:
                entry = {"trade_params": tp, "strategy_params": sp, "metrics": m,
                         "score": round(_guarded(m, objective, min_trades), 3),
                         "validation": None, "validation_passed": None}
                if val_segs:
                    set_phase(f"Regime '{reg_meta['label']}': Walk-Forward-Prüfung")
                    vm = await dyn.eval_regime_config(
                        strategy, val_segs, regime_id,
                        dyn.with_strategy_params(settings, sid, sp),
                        {**cfg, **tp}, stop)
                    entry["validation"] = vm
                    entry["validation_passed"] = dyn.validation_passed(vm, min_val)
                top5.append(entry)
            top5.sort(key=lambda e: (-(1 if e["validation_passed"] else 0),
                                     -e["score"]))
            top5 = top5[:5]
            # Richtungs-Bias in die Trade-Parameter der Kandidaten schreiben –
            # so wandert er über /assign automatisch in die dynamische
            # Strategie (Walk-Forward + Live/Paper nutzen dieselben configs).
            if allowed_sides:
                for e in top5:
                    e["trade_params"] = {**(e["trade_params"] or {}),
                                         "allowed_sides": list(allowed_sides)}

        bpd = rg.bars_per_day(timeframe)
        result = {
            "kind": "regime_opt", "analysis_id": doc["id"], "analysis_name": doc.get("name"),
            "scope": scope, "symbol": symbol, "regime_id": regime_id,
            "regime_label": reg_meta.get("label"), "mode": mode,
            "objective": objective, "min_trades": min_trades,
            "direction_bias": {"mode": bias, "regime_direction": int(reg_dir),
                               "allowed_sides": allowed_sides},
            "timeframe": timeframe, "timeframe_analysis": doc.get("timeframe"),
            "symbols": list(segments.keys()),
            "strategy_id": body.get("strategy_id") if mode == "params" else None,
            "strategy_name": (getattr(strategy, "STRATEGY_NAME", None)
                              if mode == "params" else None),
            "definition": (discovery or {}).get("definition") if discovery else None,
            "discovery": ({k: v for k, v in discovery.items() if k != "definition"}
                          if discovery else None),
            "top5": top5,
            "segments_info": {"segments": n_segs, "bars": n_bars,
                              "days": round(n_bars / max(bpd, 1e-9), 1)},
            "regime_walk_forward": bool(val_segs),
            "regime_train_pct": regime_train_pct if val_segs else None,
            "max_capital": cfg.get("max_capital"),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        if db is not None:
            await db.regime_lab_runs.replace_one(
                {"id": job_id}, {"id": job_id, "result": result,
                                 "created_at": result["created_at"]}, upsert=True)
        job["result"] = result
        job["status"] = "done"
        job["progress"] = 100
        job["phase"] = "Fertig"
    except JobCancelled:
        job["status"] = "cancelled"
        job["phase"] = "Abgebrochen"
    except Exception as e:  # noqa: BLE001
        logger.exception(f"regime opt {job_id} failed")
        job["status"] = "error"
        job["error"] = str(e)[:300]
        job["phase"] = "Fehler"
    finally:
        _close_seg_pool(seg_pool, cancelled=stop())


# ---------------- Finaler Walk-Forward der zusammengestellten Strategie ----------------
def _assignment_items(doc: Dict, scope: str, symbol: str) -> Dict[int, Dict]:
    """Bestätigte Zuordnungen eines Bereichs – verworfene Regime (kept=false)
    werden übersprungen."""
    key = lab.scope_key(scope, symbol)
    kept = doc.get("kept") or {}
    out = {}
    for k, a in (doc.get("assignments") or {}).items():
        if not k.startswith(key + ":"):
            continue
        rid = int(k.rsplit(":", 1)[1])
        if kept.get(f"{key}:{rid}") is False:
            continue
        out[rid] = a
    return out


async def run_walkforward(job_id: str, body: Dict, registry, settings: Dict,
                          default_cfg: Dict, db):
    """Holdout-Test der zusammengestellten dynamischen Strategie:
    Regime-Klassifikation rein rückblickend auf dem GESAMTEN Verlauf, bewertet
    werden aber nur Trades im unangetasteten Testbereich (kein Lookahead)."""
    from services.optimizer import _mk_strategy
    job = lab.JOBS[job_id]
    stop = _stop_fn(job)
    seg_pool = None
    try:
        doc = await _load_doc(body, db)
        scope = body.get("scope") or "combined"
        symbol = body.get("symbol")
        model = lab.model_for(doc, scope, symbol)
        if not model:
            raise RuntimeError("Kein Regime-Modell für diesen Bereich gespeichert")
        assignments = _assignment_items(doc, scope, symbol)
        if not assignments:
            raise RuntimeError("Keine bestätigten Regime-Strategien vorhanden")
        base_strategy = registry.get(body.get("strategy_id") or "")
        needs_base = any(not (a.get("definition"))
                         and not registry.get(a.get("strategy_id") or "")
                         for a in assignments.values())
        if needs_base and not base_strategy:
            raise RuntimeError("Basis-Strategie erforderlich (mindestens ein Regime "
                               "hat keine eigene Regel-Definition)")

        syms = [symbol] if scope == "per_coin" else list(doc.get("symbols") or [])
        tf = doc.get("timeframe")
        s_cfg = doc.get("settings") or {}
        conf_min = float(s_cfg.get("confidence_min") or 70) / 100.0
        min_hold = float(s_cfg.get("min_hold_days") or 2)
        end_ts = {s: (doc.get("bounds") or {}).get(s, {}).get("end_ts") for s in syms}
        histories = await lab.fetch_histories(syms, int(doc["days"]), tf, job,
                                              end_ts=end_ts, progress_span=(0, 20))
        if not histories:
            raise RuntimeError("Zu wenig Daten")

        job["phase"] = "Regime-Klassifikation (rückblickend, wie im Live-Betrieb)"
        job["progress"] = 25
        test_labels, has_holdout = {}, False
        for sym, candles in histories.items():
            labels = rg.classify_series(model, candles, tf, conf_min, min_hold)
            train_end = (doc.get("bounds") or {}).get(sym, {}).get("train_end_ts")
            if not train_end:
                continue
            has_holdout = True
            test_labels[sym] = [labels[i] if candles[i]["timestamp"] > train_end
                                else None for i in range(len(candles))]
        if not has_holdout:
            raise RuntimeError("Diese Analyse hat keinen Holdout (train_pct = 100%) – "
                               "neue Analyse mit z.B. 75% Training erstellen")
        test_segs = dyn.build_segments(histories, test_labels)

        # Benchmark-Segmente: kompletter Holdout als EIN statischer Abschnitt
        stat_segs = {}
        for sym, candles in histories.items():
            train_end = (doc.get("bounds") or {}).get(sym, {}).get("train_end_ts")
            if not train_end:
                continue
            ts = [c["timestamp"] for c in candles]
            import bisect as _b
            si = _b.bisect_right(ts, train_end)
            if si >= len(candles) - 20:
                continue
            w0 = max(si - dyn.WARMUP_BARS, 0)
            stat_segs[sym] = [{"regime": -1, "start_ts": candles[si]["timestamp"],
                               "candles": candles[w0:], "n_bars": len(candles) - si}]
        seg_pool = _make_seg_pool(test_segs, stat_segs)

        configs = {rid: (a.get("trade_params") or {}) for rid, a in assignments.items()}
        strategies_by_regime = {}
        for rid, a in assignments.items():
            if a.get("definition"):
                strategies_by_regime[rid] = _mk_strategy(a["definition"])
            else:
                # Regime-Strategie aus der Registry (z.B. NNFX-Strategien)
                st = registry.get(a.get("strategy_id") or "")
                if st is not None:
                    strategies_by_regime[rid] = st
        # Regime-spezifische Strategie-Parameter (z.B. je Marktphase justierte
        # NNFX-Perioden) fließen über die Settings ein.
        settings_by_regime = {}
        for rid, a in assignments.items():
            sp = a.get("strategy_params") or {}
            st = strategies_by_regime.get(rid)
            sid = getattr(st, "STRATEGY_ID", None) or a.get("strategy_id")
            if sp and sid:
                settings_by_regime[rid] = dyn.with_strategy_params(settings, sid, sp)
        fallback = base_strategy or next(iter(strategies_by_regime.values()))
        cfg = dict(default_cfg)
        if body.get("max_capital") is not None:
            cfg["max_capital"] = body["max_capital"]

        job["phase"] = "Dynamische Strategie auf dem Holdout simulieren"
        job["progress"] = 40
        dyn_m, rows = await dyn.eval_dynamic(fallback, test_segs, configs, cfg,
                                             settings, stop, strategies_by_regime,
                                             settings_by_regime or None)
        switches = sum(max(len(ss) - 1, 0) for ss in test_segs.values())

        # Benchmark: jede bestätigte Regime-Strategie EINZELN statisch auf dem
        # kompletten Holdout – schlägt die Kombination die beste Einzelne?
        singles = []
        for i, (rid, a) in enumerate(sorted(assignments.items())):
            if stop():
                raise JobCancelled()
            job["phase"] = f"Benchmark: Regime-Strategie {i + 1}/{len(assignments)} " \
                           f"einzeln auf dem Holdout"
            job["progress"] = 55 + round(i / max(len(assignments), 1) * 35)
            st = strategies_by_regime.get(rid) or fallback
            m, _ = await dyn.eval_dynamic(st, stat_segs,
                                          {-1: configs.get(rid) or {}}, cfg,
                                          settings_by_regime.get(rid, settings), stop)
            singles.append({"regime": rid, "label": a.get("regime_label"),
                            "metrics": m})
        best_single = max(singles, key=lambda s: s["metrics"].get("pnl") or -1e18) \
            if singles else None
        verdict = dyn.build_verdict(dyn_m, (best_single or {}).get("metrics") or {},
                                    len(assignments), switches)

        # Equity-Punkte für die Anzeige
        rows_c = sorted([r for r in rows if r.get("closed")], key=lambda r: r["closed"])
        eq = peak = 0.0
        points = []
        for r in rows_c:
            eq += float(r.get("pnl") or 0)
            peak = max(peak, eq)
            points.append({"t": r["closed"], "pnl": float(r.get("pnl") or 0),
                           "symbol": r.get("symbol"), "side": r.get("side"),
                           "result": r.get("result"), "regime": r.get("regime"),
                           "equity": round(eq, 4), "peak": round(peak, 4),
                           "drawdown": round(peak - eq, 4)})

        per_regime = {}
        for r in rows_c:
            pr = per_regime.setdefault(r.get("regime"), [])
            pr.append(r)
        per_regime_m = [{"regime": rid,
                         "label": (assignments.get(rid) or {}).get("regime_label"),
                         "metrics": dyn.metrics_from_rows(rs, cfg.get("max_capital", 100))}
                        for rid, rs in sorted(per_regime.items())]

        result = {"kind": "walkforward", "analysis_id": doc["id"], "scope": scope,
                  "symbol": symbol, "symbols": list(histories.keys()),
                  "timeframe": tf, "train_pct": s_cfg.get("train_pct"),
                  "dynamic_test": dyn_m, "switches": switches,
                  "singles": singles, "best_single": best_single,
                  "per_regime": per_regime_m, "verdict": verdict,
                  "points": points[:8000],
                  "created_at": datetime.now(timezone.utc).isoformat()}
        key = lab.scope_key(scope, symbol)
        if db is not None:
            await db.regime_analyses.update_one(
                {"id": doc["id"]},
                {"$set": {f"walkforward.{key}":
                          {k: v for k, v in result.items() if k != "points"}}})
        job["result"] = result
        job["status"] = "done"
        job["progress"] = 100
        job["phase"] = "Fertig"
    except JobCancelled:
        job["status"] = "cancelled"
        job["phase"] = "Abgebrochen"
    except Exception as e:  # noqa: BLE001
        logger.exception(f"regime walkforward {job_id} failed")
        job["status"] = "error"
        job["error"] = str(e)[:300]
        job["phase"] = "Fehler"
    finally:
        _close_seg_pool(seg_pool, cancelled=stop())
