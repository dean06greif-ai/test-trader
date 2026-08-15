"""Backtester-Endpoints (Job-Verwaltung, Configs, Equity, CSV-Export, Apply)."""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from core import state
from core.auth import require_admin
from core.config import BACKTEST_SYMBOLS, ALL_SYMBOLS
from core.defaults import DEFAULT_STRATEGY_COIN_CFG
from core.state import scanner, autotrader
from core.utils import _clean, _watch_job_task, _job_public, _equity_points, _rows_to_csv
from services import backtester as bt
from services.bitunix_trade import DEFAULT_COIN_CFG
from strategies.registry import registry as strategy_registry

logger = logging.getLogger(__name__)

router = APIRouter(tags=["backtest"])


@router.post("/api/backtest/run")
async def start_backtest(body: Dict, _: bool = Depends(require_admin)):
    strategy_ids = body.get("strategy_ids") or []
    symbols = body.get("symbols") or []
    days = min(max(int(body.get("days") or 3), 1), 5500)
    # Benutzerdefinierter Datumsbereich: days aus date_from ableiten
    date_from = body.get("date_from") or None
    date_to = body.get("date_to") or None
    if date_from:
        try:
            df = datetime.fromisoformat(str(date_from))
            if df.tzinfo is None:
                df = df.replace(tzinfo=timezone.utc)
            days = min(max((datetime.now(timezone.utc) - df).days + 2, 1), 5500)
        except ValueError:
            date_from = None
    if not strategy_ids or not symbols:
        raise HTTPException(status_code=400, detail="strategy_ids und symbols erforderlich")
    valid_ids = {m["id"] for m in strategy_registry.list_all()}
    strategy_ids = [s for s in strategy_ids if s in valid_ids]
    symbols = [s for s in symbols if s in BACKTEST_SYMBOLS]
    if not strategy_ids or not symbols:
        raise HTTPException(status_code=400, detail="Keine gültigen Strategien/Coins")
    running = [j for j in bt.JOBS.values() if j["status"] == "running"]
    if running:
        raise HTTPException(status_code=409, detail="Es läuft bereits ein Backtest")
    cfg = dict(DEFAULT_COIN_CFG)
    for k in ("max_capital", "leverage", "fee_percent", "tp1_crv", "tp_full_crv",
              "tp1_close_percent", "sl_mode", "sl_fixed_percent", "sl_lookback",
              "atr_sl_multiplier", "atr_period", "min_risk_percent",
              "breakeven_enabled", "trail_after_tp1", "trail_atr_mult",
              "profit_secure_enabled",
              "profit_secure_trigger_pct", "profit_lock_pct",
              "be_mode", "be_trigger_crv", "be_trigger_profit_pct",
              "be_smart_lookback", "require_all_rules", "sessions",
              "tp_mode", "tp1_percent", "tp_full_percent",
              "maintenance_margin_rate", "use_fast_path",
              "auto_leverage_enabled", "auto_lev_mode", "auto_lev_value", "auto_lev_max"):
        if body.get(k) is not None:
            cfg[k] = body[k]
    strategy_configs = body.get("strategy_configs") or {}
    if not isinstance(strategy_configs, dict):
        strategy_configs = {}
    default_tf = body.get("timeframe")
    execution = (body.get("execution") or "cloud").lower()
    params = {"strategy_ids": strategy_ids, "symbols": symbols, "days": days,
              "date_from": date_from, "date_to": date_to,
              "max_capital": cfg["max_capital"], "leverage": cfg["leverage"],
              "fee_percent": cfg["fee_percent"], "timeframe": default_tf,
              "strategy_configs": strategy_configs, "execution": execution}
    # ---- Lokale Ausführung: gleicher Job, Berechnung auf dem lokalen Worker ----
    if execution == "local":
        from services import local_exec
        if not local_exec.worker_online():
            raise HTTPException(status_code=503,
                                detail="Kein lokaler Worker verbunden – Worker starten "
                                       "oder Cloud-Ausführung wählen")
        job_id = bt.create_job(params)
        local_exec.enqueue_compute("backtest", job_id, {
            "kind": "backtest",
            "args": {"strategy_ids": strategy_ids, "symbols": symbols, "days": days,
                     "cfg": cfg, "settings": dict(scanner.settings),
                     "strategy_configs": strategy_configs,
                     "default_timeframe": default_tf,
                     "date_from": date_from, "date_to": date_to},
            "custom_definitions": strategy_registry.list_custom_definitions(),
        })
        return {"status": "started", "job_id": job_id, "execution": "local"}
    job_id = bt.create_job(params)
    task = asyncio.create_task(bt.run_backtest(job_id, strategy_ids, symbols, days, cfg,
                                               strategy_registry, scanner.settings,
                                               state.db, strategy_configs, default_tf,
                                               date_from, date_to))
    _watch_job_task(task, bt.JOBS, job_id)
    return {"status": "started", "job_id": job_id}


@router.get("/api/backtest/status/{job_id}")
async def backtest_status(job_id: str):
    job = bt.JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job nicht gefunden")
    return _job_public(job)


@router.post("/api/backtest/cancel/{job_id}")
async def backtest_cancel(job_id: str, _: bool = Depends(require_admin)):
    job = bt.JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job nicht gefunden")
    job["cancel"] = True
    if job.get("status") == "running":
        job["phase"] = "Wird abgebrochen..."
    from services import local_exec
    local_exec.check_stale()  # wartende lokale Jobs sofort stornieren
    return {"status": "cancelling", "job_id": job_id}


@router.get("/api/backtest/active")
async def backtest_active():
    """Läuft gerade ein Backtest? (für Fortschritt nach erneutem Öffnen des Popups)"""
    running = [j for j in bt.JOBS.values() if j["status"] == "running"]
    if running:
        return {"active": _job_public(running[-1])}
    done = sorted([j for j in bt.JOBS.values() if j["status"] in ("done", "error", "cancelled")],
                  key=lambda x: x["created_at"])
    return {"active": None, "last": _job_public(done[-1]) if done else None}


@router.get("/api/backtest/results")
async def backtest_results(limit: int = 5):
    rows = await state.db.backtests.find().sort("created_at", -1).limit(limit).to_list(limit)
    return {"results": [_clean(r) for r in rows]}


@router.post("/api/backtest/reset")
async def backtest_reset(_: bool = Depends(require_admin)):
    """Notfall-Reset: hängende/geisterhafte Backtests freigeben."""
    n = 0
    for j in bt.JOBS.values():
        if j.get("status") == "running":
            j["cancel"] = True
            j["status"] = "cancelled"
            j["phase"] = "Zurückgesetzt (Notfall-Reset)"
            n += 1
    return {"status": "reset", "cleared": n}


# ---- Backtest-spezifische Strategie-Einstellungen (getrennt von Live/Paper) ----
@router.get("/api/backtest/strategy-configs")
async def get_backtest_strategy_configs():
    doc = await state.db.settings.find_one({"_id": "backtest_strategy_configs"})
    return {"configs": (doc or {}).get("configs", {})}


@router.post("/api/backtest/strategy-configs")
async def set_backtest_strategy_configs(body: Dict, _: bool = Depends(require_admin)):
    configs = body.get("configs")
    if not isinstance(configs, dict):
        raise HTTPException(status_code=400, detail="configs (dict) erforderlich")
    await state.db.settings.update_one({"_id": "backtest_strategy_configs"},
                                       {"$set": {"configs": configs}}, upsert=True)
    return {"status": "success", "configs": configs}


async def _backtest_trade_rows(job_id: str):
    job = bt.JOBS.get(job_id)
    rows = (job or {}).get("export_trades")
    if rows is None:
        doc = await state.db.backtest_trades.find_one({"job_id": job_id})
        rows = (doc or {}).get("rows")
    return rows


@router.get("/api/backtest/equity/{job_id}")
async def backtest_equity(job_id: str):
    """Equity-Kurve (kumulierter PnL Trade für Trade) inkl. Drawdown &
    Liquidations-Markern für das Equity-Chart im Backtester."""
    rows = await _backtest_trade_rows(job_id)
    if rows is None:
        raise HTTPException(status_code=404, detail="Keine Trade-Daten für diesen Backtest gefunden")
    return {"job_id": job_id, "points": _equity_points(rows)}


# ---- CSV-Export der Backtest-Rohdaten (Trades + Kerzen) ----
@router.get("/api/backtest/export/{job_id}")
async def backtest_export(job_id: str, kind: str = "trades"):
    job = bt.JOBS.get(job_id)
    if kind == "candles":
        data = (job or {}).get("export_candles")
        if not data:
            raise HTTPException(status_code=404,
                                detail="Kerzendaten sind nur für den zuletzt gelaufenen Backtest verfügbar")
        rows = []
        for key, candles in data.items():
            sym, tf = key.split("|")
            for c in candles:
                rows.append({"symbol": sym, "timeframe": tf, "timestamp": c["timestamp"],
                             "time_utc": datetime.fromtimestamp(c["timestamp"] / 1000,
                                                                tz=timezone.utc).isoformat(),
                             "open": c["open"], "high": c["high"], "low": c["low"],
                             "close": c["close"], "volume": c.get("volume", 0)})
        csv_str = _rows_to_csv(rows, ["symbol", "timeframe", "timestamp", "time_utc",
                                      "open", "high", "low", "close", "volume"])
    elif kind == "equity":
        rows = await _backtest_trade_rows(job_id)
        if rows is None:
            raise HTTPException(status_code=404, detail="Keine Trade-Daten für diesen Backtest gefunden")
        csv_str = _rows_to_csv(_equity_points(rows),
                               ["t", "equity", "peak", "drawdown", "pnl", "symbol",
                                "strategy_id", "strategy_name", "side", "result",
                                "liquidated"])
    else:
        rows = (job or {}).get("export_trades")
        if rows is None:
            doc = await state.db.backtest_trades.find_one({"job_id": job_id})
            rows = (doc or {}).get("rows")
        if rows is None:
            raise HTTPException(status_code=404, detail="Keine Trade-Daten für diesen Backtest gefunden")
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
                             f'attachment; filename="backtest_{job_id}_{kind}.csv"'})


# ---- Backtest-Einstellungen in Live/Paper-Trading übernehmen ----
@router.post("/api/backtest/apply")
async def backtest_apply(body: Dict, _: bool = Depends(require_admin)):
    """Übernimmt Backtest-Konfiguration einer Strategie als Live/Paper-Setup
    für ausgewählte Coins (schreibt strategy_coin_configs)."""
    sid = body.get("strategy_id")
    if not sid or not strategy_registry.get(sid):
        raise HTTPException(status_code=400, detail="Gültige strategy_id erforderlich")
    symbols = [s for s in (body.get("symbols") or []) if s in ALL_SYMBOLS]
    if not symbols:
        raise HTTPException(status_code=400, detail="Mindestens 1 Coin erforderlich")
    mode = body.get("mode", "paper")
    if mode not in ("paper", "live"):
        raise HTTPException(status_code=400, detail="mode muss paper|live sein")
    cfg_in = body.get("config") or {}
    allowed = ("max_capital", "leverage", "fee_percent", "tp1_crv", "tp_full_crv",
               "tp1_close_percent", "sl_mode", "sl_fixed_percent", "sl_lookback",
               "sl_ticks", "breakeven_enabled", "be_mode", "be_trigger_crv",
               "be_trigger_profit_pct", "be_smart_lookback", "require_all_rules",
               "profit_secure_enabled", "profit_secure_trigger_pct", "profit_lock_pct",
               "tp_mode", "tp1_percent", "tp_full_percent", "trade_pre_signals",
               "maintenance_margin_rate",
               "auto_leverage_enabled", "auto_lev_mode", "auto_lev_value", "auto_lev_max")
    applied = []
    now_iso = datetime.now(timezone.utc).isoformat()
    for sym in symbols:
        key = f"{sid}_{sym}"
        doc = await state.db.strategy_coin_configs.find_one({"_id": key})
        merged = {**DEFAULT_STRATEGY_COIN_CFG, **((doc or {}).get("config", {}))}
        for k in allowed:
            if cfg_in.get(k) is not None:
                merged[k] = cfg_in[k]
        merged["mode"] = mode
        merged["enabled"] = True
        merged["backtest_applied"] = now_iso
        await state.db.strategy_coin_configs.replace_one(
            {"_id": key}, {"_id": key, "config": merged}, upsert=True)
        autotrader.config.setdefault("strategy_coin_configs", {})[key] = merged
        applied.append(sym)
    return {"status": "success", "strategy_id": sid, "mode": mode, "symbols": applied}
