"""Live-Betrieb dynamischer Strategien: Regime-Refresh, Konfig-Übernahme,
Wechsel-Protokoll und Auto-Umschaltung im Hintergrund.

Wird vom Router (manuelle Buttons) UND vom Hintergrund-Watcher genutzt,
damit beide Wege exakt dieselbe Logik verwenden.
"""
import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

import aiohttp

from core import state
from core.defaults import OPT_TRADE_KEYS
from core.state import autotrader, scanner
from services import regime as rg
from strategies.registry import registry as strategy_registry

logger = logging.getLogger(__name__)

WATCH_TICK_SECONDS = 60


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def detect_current(symbol: str, timeframe: str, days: int, max_regimes: int,
                         lookback_days: float, conf_min: float,
                         min_hold_days: float, engine: str = None,
                         engine_config: Dict = None) -> Dict:
    """Marktphasen für einen Coin frisch bestimmen und die aktuelle Phase melden.
    Unabhängig von gespeicherten dynamischen Strategien – reine Live-Anzeige."""
    from services.backtester import fetch_history
    from services.timeframes import aggregate_candles

    async with aiohttp.ClientSession() as session:
        raw = await fetch_history(session, symbol, days)
    candles = aggregate_candles(raw, timeframe)
    if len(candles) < 200:
        raise RuntimeError("Zu wenig Daten – Zeitraum erhöhen oder kleineren Timeframe wählen")
    cfg = dict(engine_config or {})
    cfg.setdefault("confidence_min", conf_min)
    cfg.setdefault("min_hold_days", min_hold_days)
    model = rg.detect_regimes({symbol: candles}, timeframe, max_regimes, lookback_days,
                              engine=engine, engine_config=cfg)
    if not model:
        raise RuntimeError("Marktphasen konnten nicht bestimmt werden – Zeitraum erhöhen")
    cur = rg.current_regime(model, candles, timeframe, conf_min, min_hold_days)
    labels = rg.classify_series(model, candles, timeframe, conf_min, min_hold_days)
    segs = rg.segments_from_labels(labels)
    timeline = [{"regime": rid,
                 "label": next((r["label"] for r in model["regimes"] if r["id"] == rid), None),
                 "from": int(candles[s]["timestamp"]),
                 "to": int(candles[min(e, len(candles) - 1)]["timestamp"]),
                 "bars": e - s}
                for (s, e, rid) in segs][-12:]
    validation = None
    if rg.is_v2(model):
        from services import regime_engine as eng
        validation = eng.validate_labels(candles, labels, model)
    return {"symbol": symbol, "timeframe": timeframe, "days": days,
            "checked_at": _now_iso(), "current": cur,
            "engine": model.get("engine") or "kmeans",
            "regimes": model["regimes"], "silhouette": model.get("silhouette"),
            "validation": validation,
            "timeline": timeline, "switches": max(len(segs) - 1, 0),
            "bars": len(candles)}



async def refresh_state(doc: Dict, days: int) -> Dict:
    """Aktuelles Regime je Coin bestimmen + Info-Vergleich aller Konfigurationen
    über die letzten Tage (nur Anzeige – die Umschaltung basiert auf der
    Regime-Ähnlichkeit, NICHT auf der jüngsten Performance)."""
    from services.backtester import fetch_history, simulate_pair
    from services.bitunix_trade import DEFAULT_COIN_CFG
    from services.timeframes import aggregate_candles

    model = doc["model"]
    tf = doc.get("timeframe") or model.get("timeframe") or "1m"
    s = doc.get("settings") or {}
    conf_min = float(s.get("confidence_min") or 70) / 100.0
    min_hold = float(s.get("min_hold_days") or 2)
    strategy = strategy_registry.get(doc["strategy_id"])
    if not strategy:
        raise RuntimeError("Basis-Strategie nicht mehr vorhanden")
    configs = {int(k): v for k, v in (doc.get("configs") or {}).items()}
    cfg_base = {**DEFAULT_COIN_CFG}
    per_symbol = {}
    async with aiohttp.ClientSession() as session:
        for sym in doc.get("symbols") or []:
            try:
                raw = await fetch_history(session, sym, days)
                candles = aggregate_candles(raw, tf)
                del raw
                if len(candles) < 50:
                    per_symbol[sym] = {"error": "Zu wenig Daten"}
                    continue
                cur = rg.current_regime(model, candles, tf, conf_min, min_hold)
                rid = cur.get("regime")
                # {} = Baseline-Konfiguration ist gültig – nur bei UNBEKANNTEM Regime Fallback
                cur["active_config"] = configs[rid] if rid in configs \
                    else (doc.get("fallback_config") or {})
                sub = (doc.get("sub_strategies") or {}).get(str(rid))
                cur["active_sub_strategy"] = (sub or {}).get("rules")
                perf = []
                for r in model.get("regimes") or []:
                    c = configs.get(r["id"])
                    if c is None:
                        continue
                    res = await asyncio.to_thread(
                        simulate_pair, strategy, candles, sym,
                        dict(scanner.settings), {**cfg_base, **c}, None, False, None, None)
                    perf.append({"regime": r["id"], "label": r["label"],
                                 "pnl": res.get("pnl"), "trades": res.get("trades"),
                                 "win_rate": res.get("win_rate")})
                perf.sort(key=lambda x: -(x.get("pnl") or 0))
                cur["recent_performance"] = perf
                per_symbol[sym] = cur
            except Exception as e:  # noqa: BLE001 – pro Symbol isolieren
                logger.warning(f"dynamic refresh {sym} failed: {e}")
                per_symbol[sym] = {"error": str(e)[:200]}
    return {"checked_at": _now_iso(), "days": days, "per_symbol": per_symbol}


async def log_switches(doc: Dict, new_state: Dict, auto_applied: bool) -> int:
    """Regime-Wechsel gegenüber dem letzten bekannten Zustand ins
    Wechsel-Protokoll schreiben (Datum, Sicherheit, Ähnlichkeiten, Begründung)."""
    old_per = ((doc.get("last_state") or {}).get("per_symbol") or {})
    model_regimes = {r["id"]: r["label"] for r in (doc.get("model") or {}).get("regimes") or []}
    n = 0
    for sym, st in (new_state.get("per_symbol") or {}).items():
        if st.get("error") or st.get("regime") is None:
            continue
        old = old_per.get(sym) or {}
        if old.get("regime") is None or old.get("regime") == st.get("regime"):
            continue
        sims = " · ".join(f"{x.get('label')}: {x.get('similarity_pct')}%"
                          for x in (st.get("similarities") or []))
        entry = {"id": uuid.uuid4().hex[:10], "dynamic_id": doc["id"],
                 "name": doc.get("name"), "symbol": sym, "at": _now_iso(),
                 "from_regime": old.get("regime"),
                 "from_label": old.get("label") or model_regimes.get(old.get("regime")),
                 "to_regime": st.get("regime"), "to_label": st.get("label"),
                 "confidence": st.get("confidence"),
                 "similarities": st.get("similarities") or [],
                 "reason": f"Ähnlichkeit: {sims}",
                 "auto_applied": bool(auto_applied),
                 "new_config": st.get("active_config") or {}}
        try:
            await state.db.dynamic_switch_log.insert_one(entry)
            n += 1
        except Exception as e:  # noqa: BLE001
            logger.warning(f"switch log failed: {e}")
    return n


async def apply_configs(doc: Dict) -> list:
    """Aktive Regime-Konfiguration je Coin als Coin-Override für Live/Paper
    übernehmen (nutzt den zuletzt bestimmten Zustand)."""
    last = doc.get("last_state") or {}
    per_symbol = last.get("per_symbol") or {}
    if not per_symbol:
        raise RuntimeError("Erst 'Regime aktualisieren' ausführen, dann übernehmen")
    sid = doc["strategy_id"]
    now_iso = _now_iso()
    applied = []
    for sym, st in per_symbol.items():
        cfg_r = (st or {}).get("active_config")
        if cfg_r is None:
            continue
        info = {"symbol": sym, "regime": st.get("regime"),
                "label": st.get("label"), "confidence": st.get("confidence")}
        if not cfg_r:
            applied.append({**info, "baseline": True})
            continue
        key = f"{sid}_{sym}"
        prev = await state.db.strategy_coin_configs.find_one({"_id": key})
        merged = dict((prev or {}).get("config", {}))
        for k in OPT_TRADE_KEYS:
            if cfg_r.get(k) is not None:
                merged[k] = cfg_r[k]
        merged["dynamic_applied"] = now_iso
        merged["dynamic_id"] = doc["id"]
        merged["dynamic_regime"] = st.get("regime")
        await state.db.strategy_coin_configs.replace_one(
            {"_id": key}, {"_id": key, "config": merged}, upsert=True)
        autotrader.config.setdefault("strategy_coin_configs", {})[key] = merged
        applied.append(info)
    await state.db.dynamic_strategies.update_one(
        {"id": doc["id"]}, {"$set": {"last_applied": now_iso, "last_applied_info": applied}})
    return applied


async def _set_toggle(strategy_id: str, symbol: str, enabled: bool):
    from core.state import strategy_coin_toggles
    await state.db.strategy_coin_toggles.update_one(
        {"strategy_id": strategy_id, "symbol": symbol},
        {"$set": {"strategy_id": strategy_id, "symbol": symbol,
                  "enabled": bool(enabled), "updated_at": _now_iso()}}, upsert=True)
    strategy_coin_toggles[(strategy_id, symbol)] = bool(enabled)


async def _apply_strategy_params(strategy_id: str, symbol: str, params: Dict):
    """Regime-spezifische Strategie-Parameter (Perioden, Schwellen) für EINEN Coin
    setzen – wirkt live über scanner.settings['coin_params'] und in Backtests über
    strategy_coin_configs[...]['params']."""
    if not params or not strategy_id:
        return
    from core.state import scanner
    cp = dict(scanner.settings.get("coin_params", {}))
    strat_cp = dict(cp.get(strategy_id, {}))
    strat_cp[symbol] = {**strat_cp.get(symbol, {}), **params}
    cp[strategy_id] = strat_cp
    scanner.update_settings({"coin_params": cp})
    await state.db.settings.update_one({"_id": "scanner_settings"},
                                       {"$set": scanner.settings}, upsert=True)


async def apply_regime_strategies(doc: Dict) -> list:
    """Multi-Strategie-Modus (z.B. NNFX): pro Coin die Strategie des AKTUELLEN
    Regimes aktivieren und die übrigen Regime-Strategien für diesen Coin
    deaktivieren. Zusätzlich werden die Trade-Parameter des Regimes übernommen.

    doc["regime_strategies"] = {"<regime_id>": "<strategy_id>", ...}
    """
    mapping = {str(k): v for k, v in (doc.get("regime_strategies") or {}).items()}
    if not mapping:
        raise RuntimeError("Diese dynamische Strategie hat keine Regime→Strategie-Zuordnung")
    per_symbol = (doc.get("last_state") or {}).get("per_symbol") or {}
    if not per_symbol:
        raise RuntimeError("Erst 'Regime aktualisieren' ausführen, dann übernehmen")
    all_sids = sorted(set(mapping.values()))
    configs = {str(k): v for k, v in (doc.get("configs") or {}).items()}
    now_iso = _now_iso()
    applied = []
    for sym, st in per_symbol.items():
        rid = (st or {}).get("regime")
        if rid is None:
            continue
        want = mapping.get(str(rid))
        for sid in all_sids:
            await _set_toggle(sid, sym, sid == want)
        cfg_r = configs.get(str(rid)) or {}
        params_r = (doc.get("regime_params") or {}).get(str(rid)) or {}
        if want and params_r:
            await _apply_strategy_params(want, sym, params_r)
        if want and (cfg_r or params_r):
            key = f"{want}_{sym}"
            prev = await state.db.strategy_coin_configs.find_one({"_id": key})
            merged = dict((prev or {}).get("config", {}))
            for k in OPT_TRADE_KEYS:
                if cfg_r.get(k) is not None:
                    merged[k] = cfg_r[k]
            if params_r:
                merged["params"] = {**(merged.get("params") or {}), **params_r}
            merged.update({"dynamic_applied": now_iso, "dynamic_id": doc["id"],
                           "dynamic_regime": rid})
            await state.db.strategy_coin_configs.replace_one(
                {"_id": key}, {"_id": key, "config": merged}, upsert=True)
            autotrader.config.setdefault("strategy_coin_configs", {})[key] = merged
        applied.append({"symbol": sym, "regime": rid, "label": st.get("label"),
                        "confidence": st.get("confidence"), "strategy_id": want,
                        "strategy_params": params_r or None,
                        "disabled": [s for s in all_sids if s != want]})
    await state.db.dynamic_strategies.update_one(
        {"id": doc["id"]}, {"$set": {"last_applied": now_iso,
                                     "last_applied_info": applied}})
    return applied


async def apply_active(doc: Dict) -> list:
    """Übernahme je Modus: Multi-Strategie (Regime→Strategie) oder
    Trade-Parameter-Overrides einer einzelnen Basis-Strategie."""
    if doc.get("regime_strategies"):
        return await apply_regime_strategies(doc)
    return await apply_configs(doc)


def _switched_symbols(doc: Dict, new_state: Dict) -> list:
    """Symbole, deren Regime sich gegenüber dem letzten Zustand geändert hat."""
    old_per = ((doc.get("last_state") or {}).get("per_symbol") or {})
    out = []
    for sym, st in (new_state.get("per_symbol") or {}).items():
        if st.get("error") or st.get("regime") is None:
            continue
        old = old_per.get(sym) or {}
        if old.get("regime") is not None and old.get("regime") != st.get("regime"):
            out.append(sym)
    return out


async def transition_protect(doc: Dict, symbols: list, new_state: Dict) -> Optional[Dict]:
    """Übergangsschutz beim Regime-Wechsel: neue Trades auf den betroffenen
    Symbolen für die Sperrzeit blockieren (Standard) – optional zusätzlich
    offene Trades kontrolliert schließen (transition_mode='close_open')."""
    s = doc.get("settings") or {}
    if s.get("transition_protection_enabled") is False or not symbols:
        return None
    mode = str(s.get("transition_mode") or "block_new")
    lock_days = float(s.get("transition_lock_days", 0.5) or 0)
    until = (datetime.now(timezone.utc) + timedelta(days=lock_days)).isoformat() \
        if lock_days > 0 else None
    info = {"mode": mode, "symbols": symbols, "locked_until": until, "closed": []}
    per = new_state.get("per_symbol") or {}
    if until:
        for sym in symbols:
            await state.db.dynamic_transition_locks.update_one(
                {"dynamic_id": doc["id"], "symbol": sym},
                {"$set": {"dynamic_id": doc["id"], "name": doc.get("name"),
                          "symbol": sym, "locked_until": until, "mode": mode,
                          "created_at": _now_iso(),
                          "to_label": (per.get(sym) or {}).get("label")}},
                upsert=True)
    if mode == "close_open":
        rows = await state.db.auto_trades.find(
            {"symbol": {"$in": symbols}, "status": "open"}).to_list(100)
        for t in rows:
            try:
                price = scanner.current_price(t["symbol"]) or t.get("entry")
                res = await autotrader.manual_close(t["id"], price)
                if res is not None and not (isinstance(res, dict) and res.get("error")):
                    info["closed"].append(t["id"])
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Übergangsschutz: Close {t.get('id')} fehlgeschlagen: {e}")
    logger.info(f"Übergangsschutz {doc['id']}: {mode} · {symbols} · Sperre bis {until}")
    return info


async def check_one(doc: Dict, days: int, auto_apply: bool) -> Dict:
    """Kompletter Prüf-Zyklus für EINE dynamische Strategie: Regime bestimmen,
    Wechsel protokollieren, optional automatisch übernehmen.

    settings.require_confirmation = True: Der Wechsel wird NICHT automatisch
    übernommen, sondern als offener Vorschlag (pending_switch) gespeichert und
    muss über /api/dynamic/{id}/confirm bestätigt werden."""
    s = doc.get("settings") or {}
    needs_confirm = bool(s.get("require_confirmation"))
    new_state = await refresh_state(doc, days)
    switched_syms = _switched_symbols(doc, new_state)
    switches = await log_switches(doc, new_state, auto_applied=auto_apply and not needs_confirm)
    transition = None
    if switched_syms:
        try:
            transition = await transition_protect(doc, switched_syms, new_state)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Übergangsschutz fehlgeschlagen: {e}")
    update = {"last_state": new_state}
    pending = None
    if switches and auto_apply and needs_confirm:
        pending = {"at": _now_iso(), "switches": switches,
                   "per_symbol": {sym: {"regime": st.get("regime"),
                                        "label": st.get("label"),
                                        "confidence": st.get("confidence")}
                                  for sym, st in (new_state.get("per_symbol") or {}).items()
                                  if not st.get("error")}}
        update["pending_switch"] = pending
    await state.db.dynamic_strategies.update_one({"id": doc["id"]}, {"$set": update})
    doc["last_state"] = new_state
    applied = None
    if auto_apply and switches and not needs_confirm:
        try:
            applied = await apply_active(doc)
            logger.info(f"dynamic auto-apply {doc['id']}: {switches} Wechsel übernommen")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"dynamic auto-apply failed: {e}")
    return {"state": new_state, "switches": switches, "applied": applied,
            "pending": pending, "transition": transition}


def _due(doc: Dict) -> bool:
    s = doc.get("settings") or {}
    if not s.get("auto_check_enabled"):
        return False
    interval = max(int(s.get("check_interval_minutes") or 60), 5)
    checked = (doc.get("last_state") or {}).get("checked_at")
    if not checked:
        return True
    try:
        last = datetime.fromisoformat(checked)
    except ValueError:
        return True
    return (datetime.now(timezone.utc) - last).total_seconds() >= interval * 60


async def watch_loop():
    """Hintergrund-Watcher: prüft fällige dynamische Strategien automatisch."""
    await asyncio.sleep(20)
    while True:
        try:
            if state.db is not None:
                docs = await state.db.dynamic_strategies.find(
                    {"settings.auto_check_enabled": True}).to_list(50)
                for doc in docs:
                    if not _due(doc):
                        continue
                    s = doc.get("settings") or {}
                    days = int(min(max(int(s.get("check_days") or 30), 7), 90))
                    await check_one(doc, days, bool(s.get("auto_apply_enabled")))
        except Exception as e:  # noqa: BLE001 – Watcher darf nie sterben
            logger.warning(f"dynamic watch loop error: {e}")
        await asyncio.sleep(WATCH_TICK_SECONDS)
