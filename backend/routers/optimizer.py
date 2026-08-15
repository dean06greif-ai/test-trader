"""Optimizer/Discovery-Endpoints inkl. Apply-Logik (eine Quelle pro Parameter)."""
import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException

from core import state
from core.auth import require_admin
from core.config import BACKTEST_SYMBOLS, ALL_SYMBOLS
from core.defaults import DEFAULT_STRATEGY_OVERRIDE, OPT_TRADE_KEYS
from core.state import scanner, autotrader
from core.utils import _clean, _watch_job_task, _job_public
from services import optimizer as opt
from services.bitunix_trade import DEFAULT_COIN_CFG
from strategies.registry import registry as strategy_registry

logger = logging.getLogger(__name__)

router = APIRouter(tags=["optimizer"])


@router.post("/api/optimizer/run")
async def start_optimizer(body: Dict, _: bool = Depends(require_admin)):
    mode = body.get("mode", "params")
    if mode not in ("params", "discovery", "combo", "dynamic", "explore"):
        raise HTTPException(status_code=400,
                            detail="mode muss params|discovery|combo|dynamic|explore sein")
    symbols = [s for s in (body.get("symbols") or []) if s in BACKTEST_SYMBOLS]
    if not symbols:
        raise HTTPException(status_code=400, detail="Mindestens 1 gültiger Coin erforderlich")
    if mode in ("params", "dynamic"):
        sid = body.get("strategy_id")
        if not sid or not strategy_registry.get(sid):
            raise HTTPException(status_code=400, detail="Gültige strategy_id erforderlich")
    running = [j for j in opt.JOBS.values() if j["status"] == "running"]
    if running:
        raise HTTPException(status_code=409, detail="Es läuft bereits eine Optimierung")
    body["symbols"] = symbols
    if body.get("base_strategy_id"):
        base = strategy_registry.get(body["base_strategy_id"])
        if not base or not getattr(base, "IS_CUSTOM", False):
            raise HTTPException(status_code=400,
                                detail="Basis-Strategie muss eine Custom-Strategie sein")
    params = {k: body.get(k) for k in ("mode", "strategy_id", "symbols", "days",
                                       "timeframe", "objective", "iterations",
                                       "min_trades", "max_rules", "indicators",
                                       "algorithm", "base_strategy_id", "sessions",
                                       "walk_forward", "dd_filter", "constancy",
                                       "dynamic", "explore")}
    execution = (body.get("execution") or "cloud").lower()
    params["execution"] = execution
    # ---- Lokale Ausführung: gleicher Job, Berechnung auf dem lokalen Worker ----
    if execution == "local":
        from services import local_exec
        if not local_exec.worker_online():
            raise HTTPException(status_code=503,
                                detail="Kein lokaler Worker verbunden – Worker starten "
                                       "oder Cloud-Ausführung wählen")
        if mode == "explore" and not local_exec.worker_supports_explore():
            raise HTTPException(status_code=409,
                                detail="Der verbundene lokale Worker ist veraltet und kennt die "
                                       "Endlos-Suche noch nicht. Bitte das Worker-Paket neu "
                                       "herunterladen (Ausführung → Lokal → ⚙ Verwalten → Download), "
                                       "den Worker neu starten – oder Cloud-Ausführung wählen.")
        if mode == "dynamic" and not local_exec.worker_supports_dynamic():
            raise HTTPException(status_code=409,
                                detail="Der verbundene lokale Worker ist veraltet und kennt den "
                                       "Dynamik-Modus noch nicht. Bitte das Worker-Paket neu "
                                       "herunterladen (Ausführung → Lokal → ⚙ Verwalten → Download), "
                                       "den Worker neu starten – oder Cloud-Ausführung wählen.")
        job_id = opt.create_job(params)
        local_exec.enqueue_compute("optimizer", job_id, {
            "kind": "optimizer",
            "args": {"body": body, "settings": dict(scanner.settings),
                     "default_cfg": dict(DEFAULT_COIN_CFG)},
            "custom_definitions": strategy_registry.list_custom_definitions(),
        })
        return {"status": "started", "job_id": job_id, "execution": "local"}
    job_id = opt.create_job(params)
    task = asyncio.create_task(opt.run_optimizer(job_id, body, strategy_registry,
                                                 scanner.settings, DEFAULT_COIN_CFG,
                                                 state.db))
    _watch_job_task(task, opt.JOBS, job_id)
    return {"status": "started", "job_id": job_id}


@router.post("/api/optimizer/reset")
async def optimizer_reset(_: bool = Depends(require_admin)):
    """Notfall-Reset: hängende/geisterhafte Optimierungen freigeben."""
    n = 0
    for j in opt.JOBS.values():
        if j.get("status") == "running":
            j["cancel"] = True
            j["status"] = "cancelled"
            j["phase"] = "Zurückgesetzt (Notfall-Reset)"
            n += 1
    return {"status": "reset", "cleared": n}


@router.post("/api/optimizer/explore/stop/{job_id}")
async def explore_stop(job_id: str, _: bool = Depends(require_admin)):
    """Sanfter Stop der Endlos-Suche: Suche beenden, beste Ergebnisse behalten
    (im Gegensatz zu 'cancel', das den Job komplett verwirft)."""
    job = opt.JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job nicht gefunden")
    if job.get("status") != "running":
        raise HTTPException(status_code=409, detail="Job läuft nicht mehr")
    job["stop_explore"] = True
    return {"status": "stopping",
            "info": "Suche wird beendet – beste Ergebnisse werden behalten"}


@router.get("/api/optimizer/explore/best")
async def explore_best():
    """Globale Top-5 der Endlos-Suche über alle bisherigen Läufe."""
    doc = await state.db.settings.find_one({"_id": "deep_explore_best"}) or {}
    return {"items": _clean(doc).get("items") or [],
            "updated_at": doc.get("updated_at")}


@router.get("/api/optimizer/status/{job_id}")
async def optimizer_status(job_id: str):
    job = opt.JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job nicht gefunden")
    return _job_public(job)


@router.post("/api/optimizer/cancel/{job_id}")
async def optimizer_cancel(job_id: str, _: bool = Depends(require_admin)):
    job = opt.JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job nicht gefunden")
    job["cancel"] = True
    if job.get("status") == "running":
        job["phase"] = "Wird abgebrochen..."
    from services import local_exec
    local_exec.check_stale()  # wartende lokale Jobs sofort stornieren
    return {"status": "cancelling", "job_id": job_id}


@router.get("/api/optimizer/active")
async def optimizer_active():
    """Läuft gerade eine Optimierung? (Fortschritt bleibt nach Schließen sichtbar)"""
    running = [j for j in opt.JOBS.values() if j["status"] == "running"]
    if running:
        return {"active": _job_public(running[-1])}
    done = sorted([j for j in opt.JOBS.values() if j["status"] in ("done", "error", "cancelled")],
                  key=lambda x: x["created_at"])
    return {"active": None, "last": _job_public(done[-1]) if done else None}


@router.get("/api/optimizer/results")
async def optimizer_results(limit: int = 5):
    rows = await state.db.optimizer_runs.find().sort("created_at", -1).limit(limit).to_list(limit)
    return {"results": [_clean(r) for r in rows]}


@router.get("/api/optimizer/history")
async def optimizer_history(limit: int = 30):
    """Kompakter Verlauf aller Optimizer-Läufe inkl. Robustheits-Kennzahlen
    (WF-Score, Konsistenz, Konstanz, DD/PnL) – für die Verlaufs-Ansicht."""
    limit = min(max(limit, 1), 100)
    rows = await state.db.optimizer_runs.find().sort("created_at", -1).limit(limit).to_list(limit)
    out = []
    for r in rows:
        res = r.get("result") or {}
        top = res.get("top5") or []
        best = top[0] if top else {}
        m = best.get("metrics") or res.get("metrics") or (res.get("best") or {}).get("metrics") or {}
        d = res.get("definition") or {}
        checks = best.get("checks") or []
        active = [c for c in checks if c.get("enabled") and c.get("is_filter")]
        out.append({
            "id": r.get("id"), "created_at": r.get("created_at"),
            "mode": res.get("mode"), "days": res.get("days"),
            "timeframe": res.get("timeframe"), "symbols": res.get("symbols") or [],
            "objective": res.get("objective"),
            "strategy": res.get("strategy_name") or d.get("name"),
            "rules_n": len(d.get("long_rules") or []) + len(d.get("short_rules") or []),
            "pnl": m.get("pnl"), "win_rate": m.get("win_rate"),
            "trades": m.get("trades"), "max_drawdown": m.get("max_drawdown"),
            "wf_mode": (res.get("walk_forward") or {}).get("mode"),
            "wf": best.get("wf"),
            "test_pnl": (best.get("test_metrics") or {}).get("pnl"),
            "dd_ratio_pct": best.get("dd_ratio_pct"),
            "constancy_dev": (best.get("constancy") or {}).get("deviation_pct"),
            "passed": best.get("passed"),
            "checks_passed": sum(1 for c in active if c.get("passed")) if active else None,
            "checks_enabled": len(active) if active else None,
            "fail_reasons": best.get("fail_reasons") or [],
            "rank_reason": best.get("rank_reason"),
            "top5_n": len(top),
        })
    return {"history": out}


@router.get("/api/optimizer/result/{job_id}")
async def optimizer_result_by_id(job_id: str):
    """Vollständiges Ergebnis eines früheren Laufs (für 'Verlauf -> Lauf laden')."""
    res = await _load_optimizer_result(job_id)
    if not res:
        raise HTTPException(status_code=404, detail="Ergebnis nicht gefunden")
    return {"result": res}


async def _load_optimizer_result(job_id: str) -> Optional[Dict]:
    """Best-Ergebnis eines Optimizer-Laufs: erst RAM-Cache, dann DB."""
    job = opt.JOBS.get(job_id)
    if job and job.get("result"):
        return job["result"]
    if state.db is not None:
        doc = await state.db.optimizer_runs.find_one({"id": job_id})
        if doc:
            return doc.get("result")
    return None


async def _optimizer_trade_rows(job_id: str):
    """Gespeicherte Trades des besten Kandidaten: erst RAM-Job, dann DB."""
    job = opt.JOBS.get(job_id)
    rows = (job or {}).get("export_trades")
    if rows is None and state.db is not None:
        doc = await state.db.optimizer_trades.find_one({"job_id": job_id})
        rows = (doc or {}).get("rows")
    return rows


@router.get("/api/optimizer/equity/{job_id}")
async def optimizer_equity(job_id: str, scope: str = "optimized"):
    """Equity-Kurve für das beste Optimizer-Ergebnis. scope=optimized (Standard)
    nutzt die beim Lauf gespeicherten Trades (sofort, ohne Neu-Simulation);
    scope=all simuliert alle Top-Coins, um Robustheit außerhalb des
    Trainingssets zu prüfen."""
    from core.utils import _equity_points
    if scope != "all":
        rows = await _optimizer_trade_rows(job_id)
        if rows:
            return {"job_id": job_id, "scope": scope, "source": "stored",
                    "symbols": sorted({r.get("symbol") for r in rows}),
                    "points": _equity_points(list(rows))}
    return await _simulate_equity(job_id, scope)


async def _simulate_equity(job_id: str, scope: str):
    """Fallback/scope=all: Equity per Neu-Simulation (alte Läufe ohne
    gespeicherte Trades bzw. Robustheits-Check auf allen Coins)."""
    result = await _load_optimizer_result(job_id)
    if not result:
        raise HTTPException(status_code=404, detail="Kein Ergebnis für diesen Lauf gefunden")
    _days_guard = int(result.get("days") or 0)
    _syms_guard = result.get("symbols") or []
    _n_syms = 10 if scope == "all" else max(len(_syms_guard), 1)
    if _days_guard > 120 or _days_guard * _n_syms > 900:
        raise HTTPException(status_code=400,
                            detail="Zu viele Daten für die Live-Simulation der Equity-Kurve "
                                   "(RAM-Schutz des Servers). Neue Optimierungen speichern ihre "
                                   "Trades automatisch – bitte den Lauf erneut ausführen (gern "
                                   "lokal mit aktuellem Worker), dann lädt die Kurve sofort aus "
                                   "den gespeicherten Daten.")
    from services.backtester import fetch_history, simulate_pair
    from services import backtester as bt_svc
    result = await _load_optimizer_result(job_id)
    if not result:
        raise HTTPException(status_code=404, detail="Optimierungs-Ergebnis nicht gefunden")

    tf = result.get("timeframe") or "1m"
    days = int(result.get("days") or 3)
    mode = result.get("mode") or "params"
    if mode == "dynamic":
        raise HTTPException(status_code=400,
                            detail="Für dynamische Läufe kommt die Equity-Kurve aus den beim Lauf "
                                   "gespeicherten Trades – dieser Lauf hat keine (vermutlich mit "
                                   "veraltetem lokalen Worker berechnet). Bitte Worker-Paket neu "
                                   "herunterladen und den Lauf wiederholen.")
    opt_symbols = list(result.get("symbols") or [])
    if scope == "all":
        symbols = list(BACKTEST_SYMBOLS)
    else:
        symbols = opt_symbols
    if not symbols:
        raise HTTPException(status_code=400, detail="Keine Coins verfügbar")

    # Best-Parameter + Trade-Parameter aus dem Ergebnis rekonstruieren
    if mode == "params":
        sid = result.get("strategy_id")
        strategy = strategy_registry.get(sid)
        if not strategy:
            raise HTTPException(status_code=404, detail="Strategie nicht mehr verfügbar")
        best = result.get("best") or {}
        best_params = best.get("params") or {}
        best_tp = best.get("trade_params") or {}
        strat_name = getattr(strategy, "STRATEGY_NAME", sid)
    else:
        from strategies.custom_strategy import CustomStrategy
        definition = result.get("definition")
        if not definition:
            raise HTTPException(status_code=400, detail="Keine Regeldefinition im Ergebnis")
        strategy = CustomStrategy({**definition, "id": definition.get("id") or f"opt_{job_id}"})
        sid = strategy.STRATEGY_ID
        best_params = {}
        best_tp = result.get("trade_params") or {}
        strat_name = definition.get("name") or "Entdeckte Strategie"

    eff_settings = dict(scanner.settings)
    if best_params:
        sp = dict(eff_settings.get("strategy_params", {}))
        sp[sid] = {**sp.get(sid, {}), **best_params}
        eff_settings["strategy_params"] = sp

    cap = float(result.get("max_capital") or DEFAULT_COIN_CFG.get("max_capital", 100.0))
    eff_cfg = {**DEFAULT_COIN_CFG, "max_capital": cap, **best_tp}
    if result.get("sessions"):
        eff_cfg.setdefault("sessions", result["sessions"])

    points: List[Dict] = []
    try:
        import aiohttp

        async def _sim_one(session, sym):
            try:
                raw = await fetch_history(session, sym, days)
                candles = bt_svc.aggregate_candles(raw, tf)
                del raw
                if len(candles) <= 100:
                    return []
                res = await asyncio.to_thread(simulate_pair, strategy, candles, sym,
                                              eff_settings, eff_cfg, None, True, None, None)
                out = []
                for t in (res.get("all_trades") or []):
                    if not t.get("closed"):
                        continue
                    out.append({
                        "t": t["closed"], "pnl": float(t.get("pnl") or 0),
                        "symbol": sym, "strategy_id": sid, "strategy_name": strat_name,
                        "side": t.get("side"), "result": t.get("result"),
                        "liquidated": bool(t.get("liquidated")),
                    })
                return out
            except Exception as e:
                logger.warning(f"optimizer_equity {sym} failed: {e}")
                return []

        async with aiohttp.ClientSession() as session:
            # Parallelisiert – gerade für scope=all deutlich schneller (bis zu 10x)
            results_lists = await asyncio.gather(*[_sim_one(session, s) for s in symbols])
            for lst in results_lists:
                points.extend(lst)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Simulation fehlgeschlagen: {e}")

    points.sort(key=lambda p: p["t"])
    # Kumulierten PnL + Peak/Drawdown ergänzen, damit das Frontend die gleichen Felder wie beim Backtest bekommt
    eq, peak = 0.0, 0.0
    out = []
    for p in points:
        eq += p["pnl"]
        peak = max(peak, eq)
        out.append({**p, "equity": round(eq, 4), "peak": round(peak, 4),
                    "drawdown": round(peak - eq, 4)})
    return {"job_id": job_id, "scope": scope, "symbols": symbols, "points": out}


# ---- CSV-Export der Optimizer-Rohdaten (Trades + Equity) wie im Backtester ----
@router.get("/api/optimizer/export/{job_id}")
async def optimizer_export(job_id: str, kind: str = "trades"):
    from fastapi import Response
    from core.utils import _equity_points, _rows_to_csv
    rows = await _optimizer_trade_rows(job_id)
    if rows is None:
        raise HTTPException(status_code=404,
                            detail="Keine Trade-Daten für diesen Lauf gefunden – "
                                   "Export ist ab jetzt für neue Optimierungen verfügbar")
    if kind == "equity":
        csv_str = _rows_to_csv(_equity_points(list(rows)),
                               ["t", "equity", "peak", "drawdown", "pnl", "symbol",
                                "strategy_id", "strategy_name", "side", "result",
                                "liquidated"])
    else:
        kind = "trades"
        fields = ["strategy_id", "strategy_name", "symbol", "timeframe", "side",
                  "opened", "closed", "duration_min", "entry", "exit", "sl_initial",
                  "sl_final", "tp1", "tp_full", "risk", "result", "pnl", "fees", "qty",
                  "leverage", "liquidated", "liq_price",
                  "tp1_done", "breakeven_moved", "crv_signal", "rules_met", "rules_total",
                  "rsi_entry", "ema_fast_entry", "ema_slow_entry", "atr_entry",
                  "entry_candle_open", "entry_candle_high", "entry_candle_low",
                  "entry_candle_close", "entry_candle_volume"]
        csv_str = _rows_to_csv(rows, fields)
    return Response(content=csv_str, media_type="text/csv",
                    headers={"Content-Disposition":
                             f'attachment; filename="optimizer_{job_id}_{kind}.csv"'})


@router.get("/api/optimizer/overrides/{strategy_id}")
async def optimizer_overrides(strategy_id: str):
    """Coins, die Coin-spezifische Optimizer-Einstellungen für diese Strategie haben."""
    param_syms = {s for s, v in
                  scanner.settings.get("coin_params", {}).get(strategy_id, {}).items() if v}
    trade_syms = set()
    prefix = strategy_id + "_"
    docs = await state.db.strategy_coin_configs.find(
        {"_id": {"$regex": f"^{prefix}"}}).to_list(500)
    for d in docs:
        cfg = d.get("config", {})
        if cfg.get("optimizer_applied"):
            trade_syms.add((d.get("_id") or "")[len(prefix):])
    return {"strategy_id": strategy_id, "symbols": sorted(param_syms | trade_syms)}


async def _write_backtest_config(sid: str, params: Dict, trade_params: Dict,
                                 timeframe: str = None):
    """Optimierte Werte als Backtest-Config der Strategie sichern, damit der
    Backtester automatisch mit den Strategie-Einstellungen läuft."""
    doc = await state.db.settings.find_one({"_id": "backtest_strategy_configs"})
    configs = (doc or {}).get("configs", {})
    c = dict(configs.get(sid, {}))
    if params:
        c["params"] = {**c.get("params", {}), **params}
    for k in OPT_TRADE_KEYS:
        if (trade_params or {}).get(k) is not None:
            c[k] = trade_params[k]
    if timeframe:
        c["timeframe"] = timeframe
    configs[sid] = c
    await state.db.settings.update_one({"_id": "backtest_strategy_configs"},
                                       {"$set": {"configs": configs}}, upsert=True)
    return c


async def _write_strategy_override(sid: str, trade_params: Dict):
    """Optimierte Trade-Einstellungen als Live/Paper-Vorauswahl der Strategie
    speichern (Modus bleibt unverändert – nichts wird automatisch scharf geschaltet)."""
    overrides = dict(autotrader.config.get("strategy_overrides", {}))
    current = overrides.get(sid, dict(DEFAULT_STRATEGY_OVERRIDE))
    for k in OPT_TRADE_KEYS:
        if (trade_params or {}).get(k) is not None:
            current[k] = trade_params[k]
    overrides[sid] = current
    new_cfg = {"mode": autotrader.config.get("mode", "paper"),
               "coins": autotrader.config.get("coins", {}),
               "strategy_overrides": overrides}
    autotrader.set_config(new_cfg)
    await state.db.settings.update_one(
        {"_id": "autotrade_config"},
        {"$set": {"mode": new_cfg["mode"], "coins": new_cfg["coins"],
                  "strategy_overrides": overrides}}, upsert=True)


@router.post("/api/optimizer/apply")
async def optimizer_apply(body: Dict, _: bool = Depends(require_admin)):
    """Optimierungs-Ergebnis übernehmen:
    - type=params: beste Parameter in die Live/Paper Strategie-Einstellungen schreiben
    - type=strategy: entdeckte Strategie als Custom-Strategie speichern"""
    apply_type = body.get("type")
    if apply_type == "params":
        sid = body.get("strategy_id")
        if not sid or not strategy_registry.get(sid):
            raise HTTPException(status_code=400, detail="Gültige strategy_id erforderlich")
        params = body.get("params") or {}
        trade_params = body.get("trade_params") or {}
        scope = body.get("scope", "global")

        # ---- scope=coins: Coin-spezifische Overrides nur für optimierte Coins ----
        if scope == "coins":
            symbols = [s for s in (body.get("symbols") or []) if s in ALL_SYMBOLS]
            if not symbols:
                raise HTTPException(status_code=400,
                                    detail="Mindestens 1 Coin erforderlich für scope=coins")
            now_iso = datetime.now(timezone.utc).isoformat()
            if params:
                cp = dict(scanner.settings.get("coin_params", {}))
                strat_cp = dict(cp.get(sid, {}))
                for sym in symbols:
                    strat_cp[sym] = {**strat_cp.get(sym, {}), **params}
                cp[sid] = strat_cp
                scanner.update_settings({"coin_params": cp})
                await state.db.settings.update_one({"_id": "scanner_settings"},
                                                   {"$set": scanner.settings}, upsert=True)
            for sym in symbols:
                key = f"{sid}_{sym}"
                doc = await state.db.strategy_coin_configs.find_one({"_id": key})
                cfg_sc = dict((doc or {}).get("config", {}))
                for k in OPT_TRADE_KEYS:
                    if trade_params.get(k) is not None:
                        cfg_sc[k] = trade_params[k]
                cfg_sc["optimizer_applied"] = now_iso
                await state.db.strategy_coin_configs.replace_one(
                    {"_id": key}, {"_id": key, "config": cfg_sc}, upsert=True)
                autotrader.config.setdefault("strategy_coin_configs", {})[key] = cfg_sc
            # Backtest-Config synchron halten (eine Quelle pro Parameter)
            await _write_backtest_config(sid, params, trade_params, body.get("timeframe"))
            return {"status": "success", "strategy_id": sid, "scope": "coins",
                    "symbols": symbols, "params": params, "trade_params": trade_params}

        # ---- scope=global (Default): Einstellungen für alle Coins ----
        sp = dict(scanner.settings.get("strategy_params", {}))
        sp[sid] = {**sp.get(sid, {}), **params}
        scanner.update_settings({"strategy_params": sp})
        # BUGFIX Zeitfenster/Timeframe: Optimierungs-Timeframe der Kerzen immer
        # mit übernehmen, sonst lief die Strategie danach auf dem alten TF (z.B. 1m
        # statt 5m). strategy_timeframes ist die eine Quelle für den Kerzen-TF.
        if body.get("timeframe"):
            tfs = dict(scanner.settings.get("strategy_timeframes", {}))
            tfs[sid] = body["timeframe"]
            scanner.update_settings({"strategy_timeframes": tfs})
        await state.db.settings.update_one({"_id": "scanner_settings"},
                                           {"$set": scanner.settings}, upsert=True)
        if trade_params:
            await _write_strategy_override(sid, trade_params)
        # Backtest-Config synchron halten (eine Quelle pro Parameter)
        await _write_backtest_config(sid, params, trade_params, body.get("timeframe"))
        return {"status": "success", "strategy_id": sid, "params": sp[sid],
                "trade_params": trade_params, "scope": "global"}
    if apply_type == "backtest":
        # Beste Parameter in die Backtest-Strategie-Einstellungen übernehmen,
        # damit optimierte Strategien direkt im Backtester getestet werden können
        sid = body.get("strategy_id")
        if not sid or not strategy_registry.get(sid):
            raise HTTPException(status_code=400, detail="Gültige strategy_id erforderlich")
        c = await _write_backtest_config(sid, body.get("params") or {},
                                         body.get("trade_params") or {},
                                         body.get("timeframe"))
        return {"status": "success", "strategy_id": sid, "config": c}
    if apply_type == "strategy":
        definition = body.get("definition")
        if not isinstance(definition, dict) or not definition.get("long_rules"):
            raise HTTPException(status_code=400, detail="Gültige definition erforderlich")
        # bestehende Custom-Strategie aktualisieren statt neue anzulegen
        update_id = body.get("update_strategy_id")
        if update_id:
            existing = strategy_registry.get(update_id)
            if not existing or not getattr(existing, "IS_CUSTOM", False):
                raise HTTPException(status_code=400,
                                    detail="update_strategy_id muss eine Custom-Strategie sein")
            sid = update_id
            definition["name"] = body.get("name") or existing.definition.get("name") or "Optimizer Strategie"
        else:
            sid = f"custom_{uuid.uuid4().hex[:8]}"
            definition["name"] = body.get("name") or definition.get("name") or "Optimizer Strategie"
        definition["id"] = sid
        definition.setdefault("description", "Vom Optimizer entdeckte Strategie")
        # BUGFIX Timeframe-Export: Der Optimierungs-Timeframe MUSS die Definition
        # überschreiben (vorher setdefault -> alter 1m-Wert blieb kleben und die
        # Strategie lief im Backtester auf dem falschen Timeframe).
        tf = body.get("timeframe") or definition.get("timeframe") or "1m"
        definition["timeframe"] = tf
        await state.db.custom_strategies.update_one({"id": sid}, {"$set": definition}, upsert=True)
        strategy_registry.upsert_custom(definition)
        enabled = scanner.settings.get("enabled_strategies", [])
        if sid not in enabled:
            enabled.append(sid)
            scanner.update_settings({"enabled_strategies": enabled})
        # strategy_timeframes immer synchron zur Definition halten
        tfs = dict(scanner.settings.get("strategy_timeframes", {}))
        tfs[sid] = tf
        scanner.update_settings({"strategy_timeframes": tfs})
        # Festes Optimierungs-Zeitfenster als Session der Strategie übernehmen
        if body.get("sessions"):
            ss = dict(scanner.settings.get("strategy_sessions", {}))
            ss[sid] = [body["sessions"]] if isinstance(body["sessions"], str) else body["sessions"]
            scanner.update_settings({"strategy_sessions": ss})
        await state.db.settings.update_one({"_id": "scanner_settings"},
                                           {"$set": scanner.settings}, upsert=True)
        # Optimierte Trade-Einstellungen als Backtest-Config UND als Live/Paper-
        # Vorauswahl der neuen Strategie sichern (eine Quelle pro Parameter).
        trade_params = body.get("trade_params") or {}
        if trade_params:
            await _write_backtest_config(sid, {}, trade_params, tf)
            await _write_strategy_override(sid, trade_params)
        return {"status": "success", "id": sid, "definition": definition,
                "updated": bool(body.get("update_strategy_id"))}
    raise HTTPException(status_code=400, detail="type muss params|strategy|backtest sein")
