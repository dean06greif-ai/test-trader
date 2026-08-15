"""Autotrade-Endpoints: Konfiguration, Trades, Kapital, Balance."""
import logging
import time as _time
from typing import Dict, Optional

from fastapi import APIRouter, Depends, HTTPException

from core import state
from core.auth import require_admin
from core.defaults import DEFAULT_STRATEGY_OVERRIDE, DEFAULT_STRATEGY_COIN_CFG
from core.state import scanner, autotrader, trade_client
from core.utils import _enrich_trade
from services.bitunix_trade import DEFAULT_COIN_CFG

logger = logging.getLogger(__name__)

router = APIRouter(tags=["autotrade"])

# ---- Bitunix-Positions-Cache (10s): liefert den ECHTEN uPnL der Börse für
# offene Live-Trades, statt ihn aus Scanner-Preisen zu schätzen (Bug-Report:
# grosse PnL-Abweichungen, z.B. Gold GC=F vs. XAUUSDT). ----
_POS_CACHE = {"ts": 0.0, "by_id": {}, "by_key": {}}


async def _live_position_map() -> tuple:
    if not trade_client.configured():
        return {}, {}
    now = _time.time()
    if now - _POS_CACHE["ts"] > 10:
        _POS_CACHE["ts"] = now  # auch bei Fehler nicht pro Request hämmern
        try:
            res = await trade_client.get_positions()
            data = res.get("data") if isinstance(res, dict) else None
            rows = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
            by_id, by_key = {}, {}
            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                raw_side = str(row.get("side") or row.get("positionSide") or "").upper()
                side = "LONG" if raw_side in ("BUY", "LONG") else \
                    ("SHORT" if raw_side in ("SELL", "SHORT") else "")
                upnl = None
                for k in ("unrealizedPNL", "unrealizedPnl", "unrealisedPNL", "upnl"):
                    if row.get(k) not in (None, ""):
                        try:
                            upnl = float(row[k])
                            break
                        except (TypeError, ValueError):
                            pass
                qty = 0.0
                for k in ("qty", "positionAmt", "amount", "size", "total", "available"):
                    try:
                        qty = abs(float(row.get(k)))
                    except (TypeError, ValueError):
                        qty = 0.0
                    if qty:
                        break
                if upnl is None or not side:
                    continue
                info = {"upnl": upnl, "qty": qty}
                pid = row.get("positionId") or row.get("id")
                if pid:
                    by_id[str(pid)] = info
                if row.get("symbol"):
                    by_key[(str(row["symbol"]), side)] = info
            _POS_CACHE["by_id"], _POS_CACHE["by_key"] = by_id, by_key
        except Exception as e:
            logger.debug(f"Bitunix-Positions-Cache nicht aktualisierbar: {e}")
    return _POS_CACHE["by_id"], _POS_CACHE["by_key"]


def _exchange_pos_for(t: Dict, by_id: Dict, by_key: Dict) -> Optional[Dict]:
    """Passende Börsen-Position zu einem offenen Live-Trade (ID vor Symbol+Seite)."""
    if t.get("status") != "open" or t.get("mode") != "live":
        return None
    pid = str(t.get("bitunix_position_id") or "")
    if pid and pid in by_id:
        return by_id[pid]
    try:
        b_sym = trade_client.to_bitunix_symbol(t.get("symbol"))
    except Exception:
        b_sym = t.get("symbol")
    return by_key.get((str(b_sym), t.get("side")))


@router.get("/api/autotrade/config")
async def get_autotrade_config():
    return {"config": autotrader.config, "defaults": DEFAULT_COIN_CFG,
            "bitunix_configured": trade_client.configured(),
            "strategy_overrides": autotrader.config.get("strategy_overrides", {})}


@router.post("/api/autotrade/config")
async def set_autotrade_config(config: Dict, _: bool = Depends(require_admin)):
    if "mode" not in config:
        config["mode"] = autotrader.config.get("mode", "paper")
    config.setdefault("coins", autotrader.config.get("coins", {}))
    config.setdefault("strategy_overrides", autotrader.config.get("strategy_overrides", {}))
    autotrader.set_config(config)
    await state.db.settings.update_one({"_id": "autotrade_config"},
                                       {"$set": {"mode": config["mode"], "coins": config["coins"],
                                                 "strategy_overrides": config.get("strategy_overrides", {})}},
                                       upsert=True)
    return {"status": "success", "config": autotrader.config}


@router.post("/api/autotrade/coin/{symbol}")
async def set_coin_config(symbol: str, cfg: Dict, _: bool = Depends(require_admin)):
    coins = dict(autotrader.config.get("coins", {}))
    merged = dict(DEFAULT_COIN_CFG)
    merged.update(coins.get(symbol, {}))
    merged.update(cfg)
    coins[symbol] = merged
    new_cfg = {"mode": autotrader.config.get("mode", "paper"), "coins": coins,
               "strategy_overrides": autotrader.config.get("strategy_overrides", {})}
    autotrader.set_config(new_cfg)
    await state.db.settings.update_one({"_id": "autotrade_config"},
                                       {"$set": {"mode": new_cfg["mode"], "coins": coins,
                                                 "strategy_overrides": new_cfg.get("strategy_overrides", {})}}, upsert=True)
    return {"status": "success", "coin": symbol, "config": merged}


@router.post("/api/autotrade/strategy/{strategy_id}")
async def set_strategy_autotrade(strategy_id: str, cfg: Dict, _: bool = Depends(require_admin)):
    """Set auto-trade configuration for a specific strategy.
    This overrides the global mode and coin-level settings when this strategy fires."""
    overrides = dict(autotrader.config.get("strategy_overrides", {}))
    current = overrides.get(strategy_id, dict(DEFAULT_STRATEGY_OVERRIDE))
    current.update(cfg)
    overrides[strategy_id] = current

    new_cfg = {
        "mode": autotrader.config.get("mode", "paper"),
        "coins": autotrader.config.get("coins", {}),
        "strategy_overrides": overrides
    }
    autotrader.set_config(new_cfg)
    await state.db.settings.update_one(
        {"_id": "autotrade_config"},
        {"$set": {"mode": new_cfg["mode"], "coins": new_cfg["coins"],
                  "strategy_overrides": overrides}},
        upsert=True
    )
    return {"status": "success", "strategy_id": strategy_id, "config": current}


@router.get("/api/autotrade/strategy/{strategy_id}")
async def get_strategy_autotrade(strategy_id: str):
    """Get auto-trade configuration for a specific strategy."""
    overrides = autotrader.config.get("strategy_overrides", {})
    cfg = overrides.get(strategy_id, dict(DEFAULT_STRATEGY_OVERRIDE))
    return {"strategy_id": strategy_id, "config": cfg, "defaults": DEFAULT_STRATEGY_OVERRIDE}


@router.get("/api/autotrade/strategy/{strategy_id}/coin/{symbol}")
async def get_strategy_coin_autotrade(
    strategy_id: str,
    symbol: str,
):
    doc = await state.db.strategy_coin_configs.find_one({"_id": f"{strategy_id}_{symbol}"})
    saved = doc.get("config", {}) if doc else {}
    merged = {**DEFAULT_STRATEGY_COIN_CFG, **saved}
    return {"config": merged}


@router.post("/api/autotrade/strategy/{strategy_id}/coin/{symbol}")
async def set_strategy_coin_autotrade(
    strategy_id: str,
    symbol: str,
    body: dict,
    _=Depends(require_admin)
):
    key = f"{strategy_id}_{symbol}"
    await state.db.strategy_coin_configs.replace_one(
        {"_id": key},
        {"_id": key, "config": body},
        upsert=True
    )
    # Sync to in-memory autotrader config
    autotrader.config.setdefault("strategy_coin_configs", {})[key] = body
    logger.info(f"[AutoTrade] Per-coin config saved: strategy={strategy_id} coin={symbol} mode={body.get('mode')}")
    return {"ok": True}


@router.get("/api/autotrade/strategy_coin_configs")
async def list_strategy_coin_autotrade():
    """Return ALL per-strategy per-coin auto-trade configs as a nested dict:
        { strategy_id: { symbol: { mode, enabled, ... } } }
    Used by the frontend to reflect the active mode on the strategy blitz icon.
    Public read-only: contains no secrets, only paper/live/off status per pair –
    so logged-out visitors also see the correct blue/yellow lightning state.
    """
    docs = await state.db.strategy_coin_configs.find().to_list(2000)
    out: Dict[str, Dict[str, Dict]] = {}
    for d in docs:
        key = d.get("_id") or ""
        if "_" not in key:
            continue
        # split on the LAST underscore so strategy ids with underscores still work
        strategy_id, symbol = key.rsplit("_", 1)
        out.setdefault(strategy_id, {})[symbol] = d.get("config", {})
    return {"configs": out}


@router.get("/api/autotrade/trades")
async def get_trades(status: str = None, limit: int = 50, mode: str = None):
    q = {}
    if status:
        q["status"] = status
    if mode in ("live", "paper"):
        q["mode"] = mode
    trades = await state.db.auto_trades.find(q).sort("opened_at", -1).limit(limit).to_list(limit)
    if not status:
        # Offene Trades (z.B. ältere manuelle Bitunix-Trades) dürfen nie aus dem
        # Limit-Fenster fallen – sonst "verschwinden" sie in der UI (Bug-Report).
        seen_ids = {t.get("id") for t in trades}
        extra_open = await state.db.auto_trades.find({**q, "status": "open"}) \
            .sort("opened_at", -1).to_list(500)
        trades.extend(t for t in extra_open if t.get("id") not in seen_ids)
    ex_by_id, ex_by_key = ({}, {})
    if any(t.get("status") == "open" and t.get("mode") == "live" for t in trades):
        ex_by_id, ex_by_key = await _live_position_map()
    return {"trades": [
        _enrich_trade(t, scanner.current_price(t["symbol"]) if t.get("status") == "open" else None,
                      exchange=_exchange_pos_for(t, ex_by_id, ex_by_key))
        for t in trades
    ]}


@router.get("/api/autotrade/trades/{trade_id}")
async def get_trade_detail(trade_id: str):
    t = await state.db.auto_trades.find_one({"id": trade_id})
    if not t:
        raise HTTPException(status_code=404, detail="Trade not found")
    cur = scanner.current_price(t["symbol"]) if t.get("status") == "open" else None
    ex = None
    if t.get("status") == "open" and t.get("mode") == "live":
        ex_by_id, ex_by_key = await _live_position_map()
        ex = _exchange_pos_for(t, ex_by_id, ex_by_key)
    return {"trade": _enrich_trade(t, cur, exchange=ex)}


@router.post("/api/autotrade/close/{trade_id}")
async def close_trade(trade_id: str, _: bool = Depends(require_admin)):
    t = await state.db.auto_trades.find_one({"id": trade_id})
    if not t:
        raise HTTPException(status_code=404, detail="Trade not found")
    price = scanner.current_price(t["symbol"]) or t["entry"]
    res = await autotrader.manual_close(trade_id, price)
    if res is None:
        raise HTTPException(status_code=409, detail="Trade ist nicht (mehr) offen")
    # Live-Close an der Börse fehlgeschlagen -> Trade bleibt offen, Fehler zeigen.
    if isinstance(res, dict) and res.get("error"):
        raise HTTPException(status_code=502, detail=res["error"])
    return {"status": "success", "result": res}


@router.get("/api/autotrade/trade/{trade_id}/explain")
async def trade_explain(trade_id: str):
    """Ausführliche Trade-Erklärung ohne LLM-Kosten: Fakten aus dem Trade-Doc
    (SL/TP-Distanzen, CRV, Risiko, Fees, Positionsgröße) + volle KI-Entscheidung
    aus ai_decisions (Begründung, size/levels_reason, Regime beim Entry)."""
    t = await state.db.auto_trades.find_one({"id": trade_id}, {"_id": 0})
    if not t:
        raise HTTPException(status_code=404, detail="Trade not found")
    entry = float(t.get("entry") or 0)
    qty = float(t.get("qty") or 0)
    sl0 = float(t.get("initial_sl") or t.get("sl") or 0)
    tp1 = float(t.get("tp1") or 0)
    tpf = float(t.get("tpf") or 0)
    margin = float(t.get("max_capital") or 0)
    lev = float(t.get("leverage") or 1)
    fee_pct = float(t.get("fee_percent", 0.06) or 0.06)
    sl_dist = abs(entry - sl0) / entry * 100 if entry and sl0 else None
    tp1_dist = abs(tp1 - entry) / entry * 100 if entry and tp1 else None
    tpf_dist = abs(tpf - entry) / entry * 100 if entry and tpf else None
    risk_usd = abs(entry - sl0) * qty if entry and sl0 and qty else None
    roundtrip = 2 * entry * qty * fee_pct / 100 if entry and qty else None
    from services.ai_engine import ai_engine
    from services.bitunix_trade import fee_guard_min_sl_pct
    fg_mult = float((ai_engine.config or {}).get("fee_guard_mult", 4.0) or 0)
    facts = {
        "entry": entry, "initial_sl": sl0 or None, "tp1": tp1 or None,
        "tpf": tpf or None,
        "sl_dist_pct": round(sl_dist, 3) if sl_dist is not None else None,
        "tp1_dist_pct": round(tp1_dist, 3) if tp1_dist is not None else None,
        "tpf_dist_pct": round(tpf_dist, 3) if tpf_dist is not None else None,
        "crv_tp1": round(tp1_dist / sl_dist, 2) if sl_dist and tp1_dist else None,
        "crv_tpf": round(tpf_dist / sl_dist, 2) if sl_dist and tpf_dist else None,
        "risk_usd": round(risk_usd, 2) if risk_usd is not None else None,
        "risk_pct_of_margin": (round(risk_usd / margin * 100, 1)
                               if margin and risk_usd else None),
        "margin_usdt": margin or None, "leverage": lev,
        "notional_usdt": round(margin * lev, 2) if margin else None,
        "fee_percent": fee_pct,
        "roundtrip_fees_usdt": round(roundtrip, 4) if roundtrip else None,
        "fees_vs_risk_pct": (round(roundtrip / risk_usd * 100, 1)
                             if risk_usd and roundtrip else None),
        "fee_guard_min_sl_pct": round(fee_guard_min_sl_pct(fee_pct, fg_mult), 3),
    }
    # Aktueller Stand (offen) bzw. Ergebnis-Zerlegung (geschlossen) – nutzt
    # dieselbe Anreicherung wie die Trade-Karte (_enrich_trade).
    t = _enrich_trade(t, scanner.current_price(t["symbol"]) if t.get("status") == "open" else None)
    comp = t.get("computed") or {}
    trade_state = {"status": t.get("status"),
                   "r_multiple": comp.get("r_multiple"),
                   "duration_seconds": comp.get("duration_seconds")}
    if t.get("status") == "open":
        trade_state.update({
            "current_price": comp.get("current_price"),
            "unrealized_pnl": comp.get("unrealized_pnl"),
            "live_pnl": comp.get("live_pnl"),
            "upnl_pct_margin": comp.get("upnl_pct_margin"),
            "sl_distance_pct": comp.get("sl_distance_pct"),
            "tp1_distance_pct": comp.get("tp1_distance_pct"),
            "tpf_distance_pct": comp.get("tpf_distance_pct"),
        })
    else:
        fees_total = float(t.get("fees_paid") or 0)
        net = float(t.get("realized_pnl") or 0)
        trade_state.update({
            "exit_price": t.get("exit_price"), "result": t.get("result"),
            "realized_pnl_net": round(net, 4),
            "fees_paid": round(fees_total, 4),
            "gross_pnl": round(net + fees_total, 4),
            "pnl_pct_margin": comp.get("pnl_pct_margin"),
            "closed_at": t.get("closed_at"),
        })
    decision = None
    if t.get("decision_id") and state.db is not None:
        d = await state.db.ai_decisions.find_one({"id": t["decision_id"]}, {"_id": 0})
        if d:
            snapf = ((d.get("entry_market_snapshot") or {}).get("features")) or {}
            decision = {
                "reasoning": d.get("reasoning"),
                "size_reason": d.get("size_reason"),
                "levels_reason": d.get("levels_reason"),
                "confidence": d.get("confidence"),
                "news_impact": d.get("news_impact"),
                "setup": d.get("setup"),
                "model": d.get("model"),
                "capital_pct": d.get("capital_pct"),
                "gate_p_win": (d.get("gate_shadow") or {}).get("p_win"),
                "regime": snapf.get("regime"),
                "regime_features": {k: snapf.get(k) for k in
                                    ("rsi", "vol_rank", "vol_basis", "daily_bias",
                                     "trend_1d_pct", "trend_3d_pct", "range_pos",
                                     "volatility_pct") if snapf.get(k) is not None},
            }
    keys = ("id", "symbol", "side", "mode", "status", "setup", "ai_reasoning",
            "ai_news_impact", "ai_confidence", "ai_size_reason", "ai_levels_reason",
            "opened_at", "strategy_id", "data_collection", "rethink_note", "rethink_ts")
    return {"trade": {k: t.get(k) for k in keys}, "facts": facts,
            "state": trade_state, "decision": decision}


@router.post("/api/autotrade/trade/{trade_id}/rethink")
async def trade_rethink(trade_id: str, _: bool = Depends(require_admin)):
    """'Trade überdenken': fokussierter Einzel-Review dieses offenen Trades durch
    die Trade-Manager-KI (1 kleiner LLM-Call, Cooldown 15 min pro Trade).
    Empfohlene Aktionen werden über die geprüfte apply_action-Schicht direkt
    ausgeführt (User-Freigabe 15.06.) – alle bestehenden Guards gelten."""
    from services.ai_trade_manager import trade_manager
    res = await trade_manager.review_single(trade_id)
    if res.get("status") == "cooldown":
        raise HTTPException(status_code=429, detail=res.get("detail"))
    if res.get("status") == "error":
        raise HTTPException(status_code=400, detail=res.get("detail", "Review fehlgeschlagen"))
    return res


@router.post("/api/autotrade/trade/{trade_id}/action")
async def manual_trade_action(trade_id: str, body: Dict, _: bool = Depends(require_admin)):
    """Manuelle Aktion auf einem offenen Trade (Trades → Offene Trades).

    Erlaubt: partial_close (value=1-99 %), adjust_sl / adjust_tp (value=Preis,
    target=tp1|tpf). Nutzt dieselbe geprüfte Ausführungs-/Audit-Schicht wie die
    KI (services/ai_trade_manager.py), aber ohne KI-Limits (Cooldown etc.)."""
    from services.ai_trade_manager import trade_manager
    action = str(body.get("action") or "").lower()
    if action not in ("partial_close", "adjust_sl", "adjust_tp"):
        raise HTTPException(status_code=400,
                            detail="action muss partial_close|adjust_sl|adjust_tp sein")
    try:
        value = float(body.get("value"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Ungültiger Wert")
    if action == "partial_close" and not (1 <= value <= 99):
        raise HTTPException(status_code=400, detail="Teilschließen erwartet 1-99 %")
    if action in ("adjust_sl", "adjust_tp") and value <= 0:
        raise HTTPException(status_code=400, detail="Preis muss größer als 0 sein")
    res = await trade_manager.apply_action(
        trade_id, action, value=value,
        target=str(body.get("target") or "tp1"),
        reason=str(body.get("reason") or "Manuell über Trades → Offene Trades")[:200],
        source="manual", enforce_limits=False)
    if res.get("status") == "error" and "nicht gefunden" in str(res.get("detail", "")):
        raise HTTPException(status_code=404, detail=res["detail"])
    if res.get("status") in ("error", "rejected", "blocked"):
        raise HTTPException(status_code=502, detail=res.get("detail") or "Aktion fehlgeschlagen")
    return {"status": "success", "result": res}


@router.post("/api/autotrade/sync-bitunix")
async def sync_bitunix_positions(_: bool = Depends(require_admin)):
    """Manueller Abgleich: extern (in Bitunix) geschlossene Live-Positionen
    sofort auch lokal schließen. Läuft zusätzlich automatisch jede Minute."""
    if not autotrader.client.configured():
        return {"status": "skipped", "detail": "Keine Bitunix-API-Keys konfiguriert",
                "synced": 0}
    synced = await autotrader.sync_live_positions()
    return {"status": "success", "synced": synced}


@router.get("/api/autotrade/sync-status")
async def get_bitunix_sync_status():
    """Zeitpunkt & Ergebnis des letzten Bitunix-Positions-Abgleichs."""
    doc = await state.db.settings.find_one({"_id": "bitunix_sync_status"}) or {}
    doc.pop("_id", None)
    return {"configured": autotrader.client.configured(), **doc}


@router.get("/api/autotrade/watchdog/status")
async def get_watchdog_status():
    """Status + Einstellungen des Positions-Watchdogs (fehlende SL, Übernahmen)."""
    from services.position_watchdog import watchdog
    doc = await state.db.settings.find_one({"_id": "position_watchdog_status"}) or {}
    doc.pop("_id", None)
    return {"configured": trade_client.configured(),
            "settings": dict(watchdog.settings), **doc}


@router.post("/api/autotrade/watchdog/run")
async def run_watchdog(_: bool = Depends(require_admin)):
    """Manueller Watchdog-Lauf: alle Bitunix-Positionen sofort prüfen."""
    from services.position_watchdog import watchdog
    manage = watchdog.settings.get("enabled", True)
    if not manage and not watchdog.settings.get("adopt_unknown", True):
        return {"status": "skipped",
                "detail": "Watchdog und Sichtbarkeits-Sync sind ausgeschaltet"}
    if not trade_client.configured():
        return {"status": "skipped",
                "detail": "Keine Bitunix-API-Keys konfiguriert"}
    return {"status": "success", "manage": manage,
            "result": await watchdog.check(manage=manage)}


@router.post("/api/autotrade/watchdog/clear")
async def clear_watchdog_data(_: bool = Depends(require_admin)):
    """Watchdog-Verlauf/Statistik löschen (Status-Report + Extern-Trades)."""
    from services.position_watchdog import watchdog
    return {"status": "success", **await watchdog.clear_data()}


@router.post("/api/autotrade/watchdog/config")
async def set_watchdog_config(body: Dict, _: bool = Depends(require_admin)):
    """Watchdog-Einstellungen ändern (enabled, interval_sec, fallback_sl_percent,
    max_sl_retries, emergency_close, adopt_unknown)."""
    from services.position_watchdog import watchdog
    return {"status": "success", "settings": await watchdog.update_settings(body)}


@router.get("/api/autotrade/capital")
async def get_capital_allocation():
    """Kapital-Zuweisung für Live & Paper inkl. aktuell zugewiesenem/freiem Kapital."""
    total = await autotrader._live_total_balance()
    out = {}
    for scope in ("live", "paper"):
        a = autotrader.capital_allocation(scope)
        allocated = await autotrader.allocated_capital(
            scope, total=total if scope == "live" else None)
        used = await autotrader.used_margin(scope)
        out[scope] = {
            **a,
            "allocated": round(allocated, 2) if allocated is not None else None,
            "used_margin": round(used, 2),
            "free": round(allocated - used, 2) if allocated is not None else None,
        }
    return {"allocation": out,
            "live_total_balance": round(total, 2) if total is not None else None,
            "bitunix_configured": trade_client.configured()}


@router.post("/api/autotrade/capital")
async def set_capital_allocation(body: Dict, _: bool = Depends(require_admin)):
    """Kapital-Zuweisung speichern: scope=live|paper, mode=full|fixed|percent, value."""
    scope = body.get("scope")
    if scope not in ("live", "paper"):
        raise HTTPException(status_code=400, detail="scope muss live|paper sein")
    mode = body.get("mode")
    if mode not in ("full", "fixed", "percent"):
        raise HTTPException(status_code=400, detail="mode muss full|fixed|percent sein")
    try:
        value = float(body.get("value") or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Ungültiger Wert")
    if mode == "fixed" and value <= 0:
        raise HTTPException(status_code=400, detail="Fester Betrag muss größer als 0 sein")
    if mode == "percent" and not (0 < value <= 100):
        raise HTTPException(status_code=400, detail="Prozentsatz muss zwischen 1 und 100 liegen")
    if mode == "fixed" and scope == "live":
        total = await autotrader._live_total_balance()
        if total is not None and value > total:
            raise HTTPException(
                status_code=400,
                detail=f"Fester Betrag ({value:.2f} USDT) übersteigt das Gesamtguthaben ({total:.2f} USDT)")
    alloc = dict(autotrader.config.get("capital_allocation", {}) or {})
    entry = dict(alloc.get(scope, {}))
    entry.update({"mode": mode, "value": value})
    if scope == "paper" and body.get("base_balance") is not None:
        try:
            bb = float(body["base_balance"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Ungültiges Simulations-Guthaben")
        if bb <= 0:
            raise HTTPException(status_code=400, detail="Simulations-Guthaben muss größer als 0 sein")
        entry["base_balance"] = bb
    alloc[scope] = entry
    autotrader.config["capital_allocation"] = alloc
    await state.db.settings.update_one({"_id": "capital_allocation"},
                                       {"$set": alloc}, upsert=True)
    logger.info(f"[Capital] Allocation saved: {scope} -> {entry}")
    return {"status": "success", "allocation": alloc}


@router.get("/api/autotrade/balance")
async def get_balance():
    # Current mode (live or paper)
    mode = autotrader.config.get("mode", "paper")

    # ---- Primary mode stats (live or paper) ----
    open_ct = await state.db.auto_trades.count_documents({"status": "open"})
    closed = await state.db.auto_trades.find({"status": "closed"}).to_list(1000)
    pnl = round(sum(t.get("realized_pnl", 0) for t in closed), 4)

    result = {
        "mode": mode,
        "realized_pnl": pnl,
        "open_trades": open_ct,
        "closed_trades": len(closed),
        "bitunix_configured": trade_client.configured(),
    }

    # ---- Live mode: fetch Bitunix balance ----
    if trade_client.configured():

        try:
            bal = await trade_client.get_balance()
            data = bal.get("data") if isinstance(bal, dict) else None
            if isinstance(data, list) and data:
                data = data[0]
            if isinstance(data, dict):
                def _num(v):
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        return 0.0
                available = _num(data.get("available") or data.get("availableBalance"))
                frozen = _num(data.get("frozen"))
                used_margin = _num(data.get("margin"))
                upnl = _num(data.get("crossUnrealizedPNL")) + _num(data.get("isolationUnrealizedPNL"))
                # Wallet balance = frei verfügbar + in Orders geblockt + in Positionen gebundene Margin
                wallet_balance = available + frozen + used_margin
                # Bitunix liefert kein marginBalance/equity-Feld → Equity selbst berechnen:
                # Margin Balance (Equity) = Wallet Balance + unrealisierter PnL
                mb = data.get("marginBalance") or data.get("equity")
                margin_balance = _num(mb) if mb is not None else wallet_balance + upnl
                result["available"] = round(available, 2)
                result["margin_balance"] = round(margin_balance, 2)
                result["wallet_balance"] = round(wallet_balance, 2)
                result["unrealized_pnl"] = round(upnl, 2)
            result["bitunix_code"] = bal.get("code") if isinstance(bal, dict) else None
        except Exception as e:
            result["bitunix_error"] = str(e)[:120]

    # ---- Paper overlay: paper trade stats alongside live ----
    # Only add paper stats if mode is live AND there are paper trades in DB
    if mode == "live":
        try:
            paper_open = await state.db.auto_trades.count_documents(
                {"status": "open", "mode": "paper"}
            )
            paper_closed = await state.db.auto_trades.find(
                {"status": "closed", "mode": "paper"}
            ).to_list(500)
            paper_pnl = round(sum(t.get("realized_pnl", 0) for t in paper_closed), 4)
            # Only include if there's actual paper activity
            if paper_open > 0 or paper_pnl != 0 or len(paper_closed) > 0:
                result["paper_pnl"] = paper_pnl
                result["paper_open_trades"] = paper_open
                result["paper_closed_trades"] = len(paper_closed)
        except Exception:
            pass  # Don't break the main balance if paper query fails

    # ---- Kapital-Zuweisung (für Balance-Widget) ----
    try:
        live_total = result.get("wallet_balance")
        alloc_out = {}
        for scope in ("live", "paper"):
            a = autotrader.capital_allocation(scope)
            allocated = await autotrader.allocated_capital(
                scope, total=live_total if scope == "live" else None)
            used = await autotrader.used_margin(scope)
            alloc_out[scope] = {
                "mode": a.get("mode", "full"),
                "value": a.get("value", 0),
                "base_balance": a.get("base_balance"),
                "allocated": round(allocated, 2) if allocated is not None else None,
                "used_margin": round(used, 2),
                "free": round(allocated - used, 2) if allocated is not None else None,
            }
        result["allocation"] = alloc_out
    except Exception as e:
        logger.warning(f"balance allocation info failed: {e}")

    return result
