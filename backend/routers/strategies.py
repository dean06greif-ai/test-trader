"""Strategie-Endpoints: Liste, Custom-CRUD, Export/Import, Coin-Toggles."""
import logging
import uuid
from datetime import datetime, timezone
from typing import Dict

from fastapi import APIRouter, Depends, HTTPException, Request

from core import state
from core.audit import log_action
from core.auth import require_admin
from core.config import ALL_SYMBOLS
from core.state import scanner, autotrader, strategy_coin_toggles
from strategies.registry import registry as strategy_registry
from strategies import custom_params
from strategies.custom_strategy import INDICATORS as RULE_INDICATORS, OPERATORS as RULE_OPERATORS
from services.timeframes import TIMEFRAMES

logger = logging.getLogger(__name__)

router = APIRouter(tags=["strategies"])


def validate_custom_definition(definition: Dict, check_meta: bool = True) -> Dict:
    """Strikte Eingabe-Prüfung + Kanonisierung (Alias-Auto-Fix, z.B. ema_200 -> ema(200)).

    Wirft 422 mit `problems` (was falsch ist) und `fixes` (anklickbare
    Korrektur-Vorschläge), wenn die Strategie nicht auswertbar wäre –
    fehlerhafte KI-Strategien werden so sofort abgewiesen statt still 0/0/0
    zu liefern. Gibt die normalisierte Definition zurück."""
    problems = []
    if check_meta:
        if not str(definition.get("name") or "").strip():
            problems.append("name: fehlt oder ist leer")
        if not ((definition.get("long_rules") or []) or (definition.get("short_rules") or [])):
            problems.append("Regeln: mindestens eine long_rule oder short_rule erforderlich")
    tf = definition.get("timeframe")
    if tf and tf not in TIMEFRAMES:
        problems.append(f"timeframe: '{tf}' wird nicht unterstützt "
                        f"(erlaubt: {', '.join(TIMEFRAMES)})")
    norm, rule_problems = custom_params.normalize_definition(
        definition, RULE_INDICATORS, RULE_OPERATORS)
    problems += rule_problems
    if problems:
        raise HTTPException(status_code=422, detail={
            "message": "Strategie abgewiesen: Regeln nicht auswertbar",
            "problems": problems,
            "fixes": custom_params.fix_suggestions(
                definition, RULE_INDICATORS, RULE_OPERATORS),
        })
    return norm


@router.get("/api/strategies")
async def get_strategies():
    out = []
    deleted = set(scanner.settings.get("deleted_strategies", []))
    for meta in strategy_registry.list_all():
        if meta["id"] in deleted:
            continue
        strat = strategy_registry.get(meta["id"])
        item = {**meta, "current_params": strat.get_params(scanner.settings)}
        if getattr(strat, "IS_CUSTOM", False):
            item["definition"] = strat.definition
        out.append(item)
    return {"strategies": out,
            "active": scanner.settings.get("active_strategy", "scalping_4_rules"),
            "enabled": scanner.enabled_strategies(),
            "signals_enabled": scanner.settings.get("strategy_signals_enabled", {})}


@router.get("/api/strategies/{strategy_id}/param-diff")
async def strategy_param_diff(strategy_id: str):
    """Vorher/Nachher-Vergleich der Strategie-Parameter (inkl. Regel-Schwellen).

    „Vorher"  = Ausgangswert der Strategie-Definition (bei KI-Strategien also die
                Werte, die die KI im Strategie-Labor erzeugt hat).
    „Nachher" = aktuell aktive Werte (vom Parameter-Optimierer übernommen bzw.
                manuell gesetzt), global und pro Coin.
    Rein lesend – dient der Transparenz, ändert nichts.
    """
    strat = strategy_registry.get(strategy_id)
    if not strat:
        raise HTTPException(status_code=404, detail="Strategie nicht gefunden")

    meta = dict(getattr(strat, "DEFAULT_PARAMS", {}) or {})
    global_params = dict(scanner.settings.get("strategy_params", {}).get(strategy_id, {}))
    coin_params = dict(scanner.settings.get("coin_params", {}).get(strategy_id, {}))

    params = []
    for key, m in meta.items():
        before = m.get("value")
        after = global_params.get(key, before)
        params.append({
            "key": key, "label": m.get("label") or key,
            "before": before, "after": after,
            "changed": after != before,
            "min": m.get("min"), "max": m.get("max"), "step": m.get("step"),
        })
    params.sort(key=lambda p: (not p["changed"], p["key"]))

    rules = None
    if getattr(strat, "IS_CUSTOM", False):
        before_def = strat.definition
        after_def = strat.effective_definition(global_params)
        rules = {}
        for side in ("long_rules", "short_rules"):
            rows = []
            for i, r in enumerate(before_def.get(side) or []):
                a = (after_def.get(side) or [])[i] if i < len(after_def.get(side) or []) else r
                b_txt, a_txt = custom_params.rule_text(r), custom_params.rule_text(a)
                rows.append({"index": i, "before": b_txt, "after": a_txt,
                             "changed": b_txt != a_txt,
                             "param_key": custom_params.rule_param_key(side, i)})
            rules["long" if side == "long_rules" else "short"] = rows

    coins = []
    for sym, cp in sorted(coin_params.items()):
        if not cp:
            continue
        rows = []
        for key, value in cp.items():
            base = global_params.get(key, (meta.get(key) or {}).get("value"))
            rows.append({"key": key, "label": (meta.get(key) or {}).get("label") or key,
                         "before": base, "after": value, "changed": value != base})
        if rows:
            coins.append({"symbol": sym, "params": rows})

    return {"strategy_id": strategy_id,
            "strategy_name": getattr(strat, "STRATEGY_NAME", strategy_id),
            "is_custom": bool(getattr(strat, "IS_CUSTOM", False)),
            "timeframe": scanner.settings.get("strategy_timeframes", {}).get(
                strategy_id, getattr(strat, "STRATEGY_TIMEFRAME", None)),
            "has_changes": any(p["changed"] for p in params) or bool(coins),
            "params": params, "rules": rules, "coins": coins,
            "rule_problems": list(getattr(strat, "rule_problems", []) or [])}


# ---- custom strategy CRUD ----
@router.post("/api/strategies/custom")
async def create_custom_strategy(definition: Dict, _: bool = Depends(require_admin)):
    definition = validate_custom_definition(definition)
    sid = definition.get("id") or f"custom_{uuid.uuid4().hex[:8]}"
    definition["id"] = sid
    definition.setdefault("timeframe", "1m")
    await state.db.custom_strategies.update_one({"id": sid}, {"$set": definition}, upsert=True)
    strategy_registry.upsert_custom(definition)
    # Timeframe-Konsistenz: definition.timeframe ist die eine Quelle der Wahrheit,
    # strategy_timeframes wird synchron gehalten (Export/Backtester/Scanner).
    tfs = dict(scanner.settings.get("strategy_timeframes", {}))
    if tfs.get(sid) != definition["timeframe"]:
        tfs[sid] = definition["timeframe"]
        scanner.update_settings({"strategy_timeframes": tfs})
    # auto-enable in tabs
    enabled = scanner.settings.get("enabled_strategies", [])
    if sid not in enabled:
        enabled.append(sid)
        scanner.update_settings({"enabled_strategies": enabled})
    await state.db.settings.update_one({"_id": "scanner_settings"}, {"$set": scanner.settings}, upsert=True)
    return {"status": "success", "id": sid, "definition": definition}


@router.post("/api/strategies/{strategy_id}/duplicate")
async def duplicate_strategy(strategy_id: str, body: Dict = None, _: bool = Depends(require_admin)):
    """Strategie duplizieren: legt eine unabhängige Kopie an (inkl. Parameter,
    Timeframe, Zeitfenster und Backtest-Einstellungen). Nur Custom/Discovery-
    Strategien haben eine kopierbare Regel-Definition."""
    body = body or {}
    strat = strategy_registry.get(strategy_id)
    if not strat:
        raise HTTPException(status_code=404, detail="Strategie nicht gefunden")
    if not getattr(strat, "IS_CUSTOM", False):
        raise HTTPException(status_code=400,
                            detail="Nur Custom/Discovery-Strategien können dupliziert werden")
    new_id = f"custom_{uuid.uuid4().hex[:8]}"
    definition = dict(strat.definition)
    definition["id"] = new_id
    base_name = definition.get("name") or strategy_id
    definition["name"] = body.get("name") or f"{base_name} (Kopie)"
    definition.setdefault("timeframe", "1m")
    await state.db.custom_strategies.update_one({"id": new_id}, {"$set": definition}, upsert=True)
    strategy_registry.upsert_custom(definition)

    s = scanner.settings
    updates: Dict = {}
    sp = s.get("strategy_params", {}).get(strategy_id)
    if sp:
        all_sp = dict(s.get("strategy_params", {}))
        all_sp[new_id] = dict(sp)
        updates["strategy_params"] = all_sp
    cp = s.get("coin_params", {}).get(strategy_id)
    if cp:
        all_cp = dict(s.get("coin_params", {}))
        all_cp[new_id] = dict(cp)
        updates["coin_params"] = all_cp
    tfs = dict(s.get("strategy_timeframes", {}))
    tfs[new_id] = tfs.get(strategy_id) or definition.get("timeframe") or "1m"
    updates["strategy_timeframes"] = tfs
    ss = s.get("strategy_sessions", {}).get(strategy_id)
    if ss:
        all_ss = dict(s.get("strategy_sessions", {}))
        all_ss[new_id] = list(ss)
        updates["strategy_sessions"] = all_ss
    enabled = list(s.get("enabled_strategies", []))
    if new_id not in enabled:
        enabled.append(new_id)
    updates["enabled_strategies"] = enabled
    scanner.update_settings(updates)
    await state.db.settings.update_one({"_id": "scanner_settings"},
                                       {"$set": scanner.settings}, upsert=True)

    # Backtest-Einstellungen mitkopieren
    bt_doc = await state.db.settings.find_one({"_id": "backtest_strategy_configs"})
    configs = (bt_doc or {}).get("configs", {})
    if configs.get(strategy_id):
        configs[new_id] = dict(configs[strategy_id])
        await state.db.settings.update_one({"_id": "backtest_strategy_configs"},
                                           {"$set": {"configs": configs}}, upsert=True)

    # Live/Paper-Strategie-Override mitkopieren (Modus bleibt sicherheitshalber 'off')
    override = autotrader.config.get("strategy_overrides", {}).get(strategy_id)
    if override:
        overrides = dict(autotrader.config.get("strategy_overrides", {}))
        copied = dict(override)
        copied["mode"] = "off"
        copied["enabled"] = False
        overrides[new_id] = copied
        new_cfg = {"mode": autotrader.config.get("mode", "paper"),
                   "coins": autotrader.config.get("coins", {}),
                   "strategy_overrides": overrides}
        autotrader.set_config(new_cfg)
        await state.db.settings.update_one(
            {"_id": "autotrade_config"},
            {"$set": {"mode": new_cfg["mode"], "coins": new_cfg["coins"],
                      "strategy_overrides": overrides}}, upsert=True)

    return {"status": "success", "id": new_id, "name": definition["name"],
            "definition": definition}


async def _unlink_candidate(strategy_id: str):
    """Kandidaten-Verknüpfung lösen, wenn seine Custom-Strategie gelöscht wird –
    sonst kann eine spätere Auto-Registrierung dieselbe ID wiederbeleben."""
    try:
        res = await state.db.ai_strategy_candidates.update_many(
            {"custom_strategy_id": strategy_id},
            {"$set": {"custom_strategy_id": None}})
        if getattr(res, "modified_count", 0):
            from services.ai_strategy_lab import strategy_lab
            await strategy_lab._refresh_cache()
    except Exception as e:
        logger.warning(f"Kandidat für {strategy_id} nicht entkoppelt: {e}")


@router.delete("/api/strategies/custom/{strategy_id}")
async def delete_custom_strategy(strategy_id: str, _: bool = Depends(require_admin)):
    await state.db.custom_strategies.delete_one({"id": strategy_id})
    strategy_registry.remove_custom(strategy_id)
    await _unlink_candidate(strategy_id)
    enabled = [s for s in scanner.settings.get("enabled_strategies", []) if s != strategy_id]
    scanner.update_settings({"enabled_strategies": enabled})
    await state.db.settings.update_one({"_id": "scanner_settings"}, {"$set": scanner.settings}, upsert=True)
    return {"status": "success"}


@router.delete("/api/strategies/{strategy_id}")
async def delete_strategy(strategy_id: str, request: Request,
                          _: bool = Depends(require_admin)):
    """Delete ANY strategy permanently. Custom => removed from DB.
    Built-in (predefined) => added to deleted_strategies so it never shows/runs."""
    is_custom = strategy_id in strategy_registry._custom_ids
    await log_action(request, "strategy_delete", {"strategy_id": strategy_id,
                                                  "custom": is_custom})
    if is_custom:
        await state.db.custom_strategies.delete_one({"id": strategy_id})
        strategy_registry.remove_custom(strategy_id)
        await _unlink_candidate(strategy_id)
    else:
        deleted = list(scanner.settings.get("deleted_strategies", []))
        if strategy_id not in deleted:
            deleted.append(strategy_id)
        scanner.update_settings({"deleted_strategies": deleted})
    enabled = [s for s in scanner.settings.get("enabled_strategies", []) if s != strategy_id]
    scanner.update_settings({"enabled_strategies": enabled})
    await state.db.settings.update_one({"_id": "scanner_settings"}, {"$set": scanner.settings}, upsert=True)
    return {"status": "success", "id": strategy_id, "was_custom": is_custom}


@router.post("/api/strategies/restore-defaults")
async def restore_default_strategies(_: bool = Depends(require_admin)):
    """Un-delete all previously deleted built-in strategies."""
    scanner.update_settings({"deleted_strategies": []})
    await state.db.settings.update_one({"_id": "scanner_settings"}, {"$set": scanner.settings}, upsert=True)
    return {"status": "success", "restored": True}


@router.get("/api/strategies/builder-options")
async def builder_options():
    from strategies.custom_strategy import INDICATORS, OPERATORS, INDICATOR_META, PERIOD_FIELDS
    from services.timeframes import RULE_TIMEFRAMES
    return {"indicators": INDICATORS, "operators": OPERATORS,
            "indicator_meta": INDICATOR_META, "period_fields": PERIOD_FIELDS,
            "rule_timeframes": RULE_TIMEFRAMES}


@router.post("/api/strategies/{strategy_id}/apply-fixes")
async def apply_strategy_fixes(strategy_id: str, body: Dict, _: bool = Depends(require_admin)):
    """Auto-Fix: Korrektur-Vorschläge (aus Backtest-Hinweis) per Klick in die
    gespeicherte Custom-/KI-Strategie übernehmen."""
    doc = await state.db.custom_strategies.find_one({"id": strategy_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Custom-Strategie nicht gefunden")
    doc.pop("_id", None)
    applied = []
    for f in (body or {}).get("fixes", []):
        side, idx, field = f.get("side"), f.get("index"), f.get("field")
        if side not in ("long_rules", "short_rules") or field not in ("indicator", "op", "value"):
            continue
        rules = doc.get(side) or []
        try:
            idx = int(idx)
        except (TypeError, ValueError):
            continue
        if 0 <= idx < len(rules) and isinstance(rules[idx], dict):
            rules[idx] = {**rules[idx], field: f.get("to")}
            doc[side] = rules
            applied.append(f.get("label") or f"{side}[{idx}].{field}")
    if not applied:
        raise HTTPException(status_code=400, detail="Keine anwendbaren Fixes übergeben")
    await state.db.custom_strategies.update_one({"id": strategy_id}, {"$set": doc}, upsert=True)
    strategy_registry.upsert_custom(doc)
    # KI-Kandidat (Strategie-Labor) synchron halten
    try:
        await state.db.ai_strategy_candidates.update_one(
            {"custom_strategy_id": strategy_id},
            {"$set": {"rule_definition": {k: doc.get(k) for k in
                                          ("timeframe", "indicators", "long_rules", "short_rules")}}})
    except Exception:
        pass
    strat = strategy_registry.get(strategy_id)
    return {"status": "ok", "applied": applied,
            "remaining_problems": getattr(strat, "rule_problems", [])}


@router.post("/api/strategies/rule-preview")
async def rule_preview(body: Dict, _: bool = Depends(require_admin)):
    """Mini-Backtest (Standard 7 Tage): Wie oft feuert jede Regel / die Strategie?
    Sofort-Feedback im StrategyBuilder, bevor eine Strategie gespeichert wird."""
    import aiohttp
    from services import fast_sim, candle_cache
    from services.timeframes import aggregate_candles
    from strategies import custom_params
    from strategies.custom_strategy import INDICATORS, OPERATORS

    definition = (body or {}).get("definition") or {}
    symbol = (body or {}).get("symbol") or "BTCUSDT"
    days = max(1, min(int((body or {}).get("days") or 7), 30))
    norm, problems = custom_params.normalize_definition(definition, INDICATORS, OPERATORS)
    fixes = custom_params.fix_suggestions(definition, INDICATORS, OPERATORS)

    async with aiohttp.ClientSession() as session:
        candles = await candle_cache.get_candles(session, symbol, days)
    if not candles or len(candles) < 60:
        raise HTTPException(status_code=502, detail=f"Keine Historie für {symbol}")
    tf = norm.get("timeframe") or "1m"
    if tf != "1m":
        candles = aggregate_candles(candles, tf)
    fs = fast_sim.FastSeries(candles)
    d = norm.get("indicators", {}) or {}

    def side_stats(side):
        rules = norm.get(side) or []
        conds, per_rule = [], []
        for r in rules:
            c = fast_sim._rule_cond(r, fs, d)
            conds.append(c)
            per_rule.append({"rule": custom_params.rule_text(r),
                             "label": r.get("label"),
                             "fires": int(c.sum()),
                             "fire_pct": round(float(c.mean()) * 100, 2)})
        combined = None
        if conds:
            combined = conds[0].copy()
            for c in conds[1:]:
                combined &= c
        return per_rule, int(combined.sum()) if combined is not None else 0

    long_rules, long_signals = side_stats("long_rules")
    short_rules, short_signals = side_stats("short_rules")
    return {"symbol": symbol, "days": days, "timeframe": tf, "bars": fs.n,
            "problems": problems, "fixes": fixes,
            "long_rules": long_rules, "long_signals": long_signals,
            "short_rules": short_rules, "short_signals": short_signals}


# ---- Strategie-Backup: kompletter Export/Import pro Strategie ----
@router.get("/api/strategies/{strategy_id}/export")
async def export_strategy(strategy_id: str):
    """Komplette Strategie als Backup exportieren: Definition/Regeln, Parameter,
    Timeframe, Zeitfenster, Live/Paper-Trade-Einstellungen (global + pro Coin)
    und Backtest-Einstellungen. Ziel: 1:1-Wiederherstellung nach Löschung."""
    strat = strategy_registry.get(strategy_id)
    if not strat:
        raise HTTPException(status_code=404, detail="Strategie nicht gefunden")
    s = scanner.settings
    coin_cfgs = {}
    docs = await state.db.strategy_coin_configs.find().to_list(2000)
    prefix = f"{strategy_id}_"
    for d in docs:
        key = d.get("_id") or ""
        if key.startswith(prefix):
            sym = key[len(prefix):]
            if sym in ALL_SYMBOLS:
                coin_cfgs[sym] = d.get("config", {})
    bt_doc = await state.db.settings.find_one({"_id": "backtest_strategy_configs"})
    definition = getattr(strat, "definition", None)
    # BUGFIX Export-Timeframe: eine autoritative Quelle statt drei potenziell
    # widersprüchlicher Werte. Reihenfolge: explizit gesetzter Timeframe
    # (strategy_timeframes) > definition.timeframe > Strategie-Default.
    effective_tf = (s.get("strategy_timeframes", {}).get(strategy_id)
                    or (definition or {}).get("timeframe")
                    or getattr(strat, "STRATEGY_TIMEFRAME", "1m"))
    if isinstance(definition, dict):
        definition = {**definition, "timeframe": effective_tf}
    return {
        "type": "strategy_backup",
        "version": 2,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "strategy_id": strategy_id,
        "name": getattr(strat, "STRATEGY_NAME", strategy_id),
        "is_custom": strategy_id in strategy_registry._custom_ids,
        "definition": definition,
        "strategy_params": s.get("strategy_params", {}).get(strategy_id, {}),
        "coin_params": s.get("coin_params", {}).get(strategy_id, {}),
        "timeframe": effective_tf,
        "strategy_sessions": s.get("strategy_sessions", {}).get(strategy_id, []),
        "strategy_override": autotrader.config.get("strategy_overrides", {}).get(strategy_id, {}),
        "strategy_coin_configs": coin_cfgs,
        "backtest_config": ((bt_doc or {}).get("configs", {})).get(strategy_id, {}),
        "enabled_in_tabs": strategy_id in s.get("enabled_strategies", []),
    }


@router.post("/api/strategies/import")
async def import_strategy(body: Dict, _: bool = Depends(require_admin)):
    """Strategie-Backup importieren: stellt eine gelöschte Strategie inkl. aller
    Parameter/Einstellungen 1:1 wieder her bzw. überschreibt verstellte Werte."""
    if body.get("type") != "strategy_backup":
        raise HTTPException(status_code=400, detail="Keine gültige Strategie-Backup-Datei")
    sid = body.get("strategy_id")
    if not sid:
        raise HTTPException(status_code=400, detail="strategy_id fehlt in der Datei")
    definition = body.get("definition")
    is_custom = bool(body.get("is_custom")) or isinstance(definition, dict)
    # Timeframe-Konsistenz: body.timeframe hat Vorrang, sonst definition.timeframe
    effective_tf = body.get("timeframe") or (definition or {}).get("timeframe") if isinstance(definition, dict) \
        else body.get("timeframe")
    if is_custom and isinstance(definition, dict):
        definition = dict(definition)
        definition["id"] = sid
        if effective_tf:
            definition["timeframe"] = effective_tf
        definition.setdefault("timeframe", "1m")
        # Auch Backups strikt prüfen: kaputte Regel-Definitionen sofort abweisen
        definition = validate_custom_definition(definition, check_meta=False)
        await state.db.custom_strategies.update_one({"id": sid}, {"$set": definition}, upsert=True)
        strategy_registry.upsert_custom(definition)
    elif not strategy_registry.get(sid):
        raise HTTPException(status_code=404,
                            detail="Built-in-Strategie existiert in dieser Version nicht")
    # gelöschte Built-ins reaktivieren
    updates: Dict = {"deleted_strategies":
                     [d for d in scanner.settings.get("deleted_strategies", []) if d != sid]}
    if body.get("strategy_params"):
        sp = dict(scanner.settings.get("strategy_params", {}))
        sp[sid] = body["strategy_params"]
        updates["strategy_params"] = sp
    if body.get("coin_params"):
        cp = dict(scanner.settings.get("coin_params", {}))
        cp[sid] = body["coin_params"]
        updates["coin_params"] = cp
    if effective_tf:
        tfs = dict(scanner.settings.get("strategy_timeframes", {}))
        tfs[sid] = effective_tf
        updates["strategy_timeframes"] = tfs
    if body.get("strategy_sessions"):
        ss = dict(scanner.settings.get("strategy_sessions", {}))
        ss[sid] = body["strategy_sessions"]
        updates["strategy_sessions"] = ss
    if body.get("enabled_in_tabs"):
        en = list(scanner.settings.get("enabled_strategies", []))
        if sid not in en:
            en.append(sid)
        updates["enabled_strategies"] = en
    scanner.update_settings(updates)
    await state.db.settings.update_one({"_id": "scanner_settings"},
                                       {"$set": scanner.settings}, upsert=True)
    if isinstance(body.get("strategy_override"), dict) and body["strategy_override"]:
        overrides = dict(autotrader.config.get("strategy_overrides", {}))
        overrides[sid] = body["strategy_override"]
        new_cfg = {"mode": autotrader.config.get("mode", "paper"),
                   "coins": autotrader.config.get("coins", {}),
                   "strategy_overrides": overrides}
        autotrader.set_config(new_cfg)
        await state.db.settings.update_one(
            {"_id": "autotrade_config"},
            {"$set": {"mode": new_cfg["mode"], "coins": new_cfg["coins"],
                      "strategy_overrides": overrides}}, upsert=True)
    n_coin = 0
    for sym, ccfg in (body.get("strategy_coin_configs") or {}).items():
        if sym not in ALL_SYMBOLS or not isinstance(ccfg, dict):
            continue
        key = f"{sid}_{sym}"
        await state.db.strategy_coin_configs.replace_one(
            {"_id": key}, {"_id": key, "config": ccfg}, upsert=True)
        autotrader.config.setdefault("strategy_coin_configs", {})[key] = ccfg
        n_coin += 1
    if isinstance(body.get("backtest_config"), dict) and body["backtest_config"]:
        doc = await state.db.settings.find_one({"_id": "backtest_strategy_configs"})
        configs = (doc or {}).get("configs", {})
        configs[sid] = body["backtest_config"]
        await state.db.settings.update_one({"_id": "backtest_strategy_configs"},
                                           {"$set": {"configs": configs}}, upsert=True)
    return {"status": "success", "id": sid, "name": body.get("name"),
            "restored_custom": is_custom, "coin_configs": n_coin}


# ---- NEW: per (strategy, coin) enable/disable toggle ----
@router.get("/api/strategies/{strategy_id}/coins")
async def get_strategy_coin_toggles(strategy_id: str):
    """Return {symbol: enabled} map for the given strategy across ALL_SYMBOLS.
    Missing rows default to True (kept enabled)."""
    result: Dict[str, bool] = {}
    for sym in ALL_SYMBOLS:
        result[sym] = strategy_coin_toggles.get((strategy_id, sym), True)
    return {"strategy_id": strategy_id, "coins": result}


@router.put("/api/strategies/{strategy_id}/coins/{symbol}")
async def set_strategy_coin_toggle(strategy_id: str, symbol: str,
                                   body: Dict, _: bool = Depends(require_admin)):
    """Enable/disable auto-trade + signals for ONE (strategy, coin) pair."""
    enabled = bool(body.get("enabled", True))
    now_iso = datetime.now(timezone.utc).isoformat()
    await state.db.strategy_coin_toggles.update_one(
        {"strategy_id": strategy_id, "symbol": symbol},
        {"$set": {"strategy_id": strategy_id, "symbol": symbol,
                  "enabled": enabled, "updated_at": now_iso}},
        upsert=True,
    )
    strategy_coin_toggles[(strategy_id, symbol)] = enabled
    return {"status": "success", "strategy_id": strategy_id,
            "symbol": symbol, "enabled": enabled}
