"""Analytics-Endpoints: Performance, Tages-Statistiken, Clear, KI-Review, Vergleich."""
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Dict

from fastapi import APIRouter, Depends, HTTPException, Request

from core import state
from core.audit import log_action
from core.auth import require_admin
from core.config import BERLIN
from core.state import scanner, open_signal_evals
from core.pipeline import broadcast
from core.utils import _clean, _enrich_trade
from strategies.registry import registry as strategy_registry

logger = logging.getLogger(__name__)

router = APIRouter(tags=["analytics"])


@router.get("/api/performance")
async def get_performance():
    """Return per-symbol performance (wins/losses aus auto_trades + Signal-Zählern)."""
    stored = {}
    for p in await state.db.performance.find().to_list(500):
        stored[p["symbol"]] = _clean(p)

    # Aggregate closed auto-trades → real win/loss numbers per symbol
    trade_pipeline = [
        {"$match": {"status": "closed", "result": {"$in": ["win", "loss", "breakeven"]}}},
        {"$group": {
            "_id": "$symbol",
            "trade_wins": {"$sum": {"$cond": [{"$eq": ["$result", "win"]}, 1, 0]}},
            "trade_losses": {"$sum": {"$cond": [{"$eq": ["$result", "loss"]}, 1, 0]}},
            "trade_breakevens": {"$sum": {"$cond": [{"$eq": ["$result", "breakeven"]}, 1, 0]}},
        }},
    ]
    trade_rows = await state.db.auto_trades.aggregate(trade_pipeline).to_list(500)

    result_map: Dict[str, Dict] = {}
    # start with everything we already have stored (keeps total/long/short counts)
    for symbol, p in stored.items():
        result_map[symbol] = {
            "symbol": symbol,
            "total_signals": p.get("total_signals", 0),
            "long_signals": p.get("long_signals", 0),
            "short_signals": p.get("short_signals", 0),
            "wins": p.get("wins", 0),
            "losses": p.get("losses", 0),
            "breakevens": p.get("breakevens", 0),
            "avg_crv": p.get("avg_crv", 0.0),
            "win_rate": p.get("win_rate", 0.0),
            "by_strategy": p.get("by_strategy", {}),
            "last_signal": p.get("last_signal"),
        }

    for tr in trade_rows:
        symbol = tr["_id"]
        p = result_map.setdefault(symbol, {
            "symbol": symbol, "total_signals": 0, "long_signals": 0, "short_signals": 0,
            "wins": 0, "losses": 0, "breakevens": 0, "avg_crv": 0.0, "win_rate": 0.0,
            "by_strategy": {},
        })
        # take the MAX between stored signal-level results and real trade results
        # (avoids double counting while making sure at least the trade outcome shows)
        p["wins"] = max(p.get("wins", 0), tr.get("trade_wins", 0))
        p["losses"] = max(p.get("losses", 0), tr.get("trade_losses", 0))
        p["breakevens"] = max(p.get("breakevens", 0), tr.get("trade_breakevens", 0))

    # recompute win_rate from the merged wins/losses
    for p in result_map.values():
        decided = p.get("wins", 0) + p.get("losses", 0)
        p["win_rate"] = round(p["wins"] / decided * 100, 1) if decided else 0.0

    perf = list(result_map.values())
    perf.sort(key=lambda x: x.get("total_signals", 0), reverse=True)
    return {"performance": perf}


@router.get("/api/analytics/daily")
async def get_daily_analytics(days: int = 30):
    rows = await state.db.analytics_daily.find().sort("date", -1).limit(days).to_list(days)
    trades = await state.db.trade_stats.find().sort("date", -1).limit(days).to_list(days)
    return {"daily": [_clean(r) for r in rows], "trade_stats": [_clean(t) for t in trades]}


async def rebuild_performance():
    """Recompute the cumulative performance collection from remaining signals."""
    await state.db.performance.delete_many({})
    signals = await state.db.signals.find(
        {}, {"_id": 0, "signal_class": 1, "symbol": 1, "type": 1, "crv": 1,
             "strategy_id": 1, "result": 1}).to_list(200000)
    perf_map: Dict[str, Dict] = {}
    for s in signals:
        if s.get("signal_class") == "PRE_SIGNAL":
            continue
        symbol = s.get("symbol")
        if not symbol:
            continue
        p = perf_map.setdefault(symbol, {
            "symbol": symbol, "total_signals": 0, "long_signals": 0, "short_signals": 0,
            "wins": 0, "losses": 0, "breakevens": 0, "avg_crv": 0.0, "win_rate": 0.0,
            "by_strategy": {}, "_crv_sum": 0.0,
        })
        p["total_signals"] += 1
        if s.get("type") == "LONG":
            p["long_signals"] += 1
        else:
            p["short_signals"] += 1
        p["_crv_sum"] += s.get("crv", 0) or 0
        sid = s.get("strategy_id", "unknown")
        st = p["by_strategy"].setdefault(sid, {"total": 0, "wins": 0, "losses": 0, "breakevens": 0})
        st["total"] += 1
        res = s.get("result")
        if res in ("win", "loss", "breakeven"):
            key = {"win": "wins", "loss": "losses", "breakeven": "breakevens"}[res]
            p[key] += 1
            st[key] += 1
    for symbol, p in perf_map.items():
        n = p["total_signals"]
        p["avg_crv"] = round(p.pop("_crv_sum", 0) / n, 3) if n else 0.0
        decided = p["wins"] + p["losses"]
        p["win_rate"] = round(p["wins"] / decided * 100, 1) if decided else 0.0
        await state.db.performance.insert_one(p)


CLEAR_DELTAS = {"hour": timedelta(hours=1), "24h": timedelta(days=1),
                "7d": timedelta(days=7), "4w": timedelta(weeks=4)}

CLEAR_SCOPES = {"all", "coin", "coin_strategy", "strategy"}


def _clear_scope_filter(scope: str, symbol: str = None, strategy_id: str = None) -> Dict:
    """Mongo-Filter je Lösch-Scope (eine Quelle der Wahrheit für Vorschau + Löschen)."""
    if scope == "coin":
        return {"symbol": symbol}
    if scope == "coin_strategy":
        return {"symbol": symbol, "strategy_id": strategy_id}
    if scope == "strategy":
        return {"strategy_id": strategy_id}
    return {}


def _validate_clear(rng: str, scope: str, symbol: str = None, strategy_id: str = None):
    if scope not in CLEAR_SCOPES:
        raise HTTPException(status_code=400, detail="Ungültiger Scope")
    if scope in ("coin", "coin_strategy") and not symbol:
        raise HTTPException(status_code=400, detail="symbol erforderlich für Coin-Scope")
    if scope in ("coin_strategy", "strategy") and not strategy_id:
        raise HTTPException(status_code=400, detail="strategy_id erforderlich für Strategie-Scope")
    if rng != "all" and rng not in CLEAR_DELTAS:
        raise HTTPException(status_code=400, detail="Ungültiger Zeitraum")


async def reaggregate_daily_stats() -> Dict[str, int]:
    """Re-aggregate analytics_daily & trade_stats from the remaining raw data.
    Needed after coin/strategy-scoped deletes, because those aggregated day
    collections carry no symbol/strategy fields and must not be dropped blindly."""
    removed = {"analytics_daily": 0, "trade_stats": 0}

    daily_docs = await state.db.analytics_daily.find({}, {"date": 1}).to_list(10000)
    for doc in daily_docs:
        date = doc.get("date")
        if not date:
            continue
        pipeline = [
            {"$match": {"trade_date": date}},
            {"$group": {"_id": {"strategy": "$strategy_id", "type": "$type"},
                        "total": {"$sum": 1},
                        "wins": {"$sum": {"$cond": [{"$eq": ["$result", "win"]}, 1, 0]}},
                        "losses": {"$sum": {"$cond": [{"$eq": ["$result", "loss"]}, 1, 0]}},
                        "avg_crv": {"$avg": "$crv"}}},
        ]
        rows = await state.db.signals.aggregate(pipeline).to_list(500)
        if not rows:
            await state.db.analytics_daily.delete_one({"date": date})
            removed["analytics_daily"] += 1
            continue
        summary = {"date": date, "generated_at": datetime.now(timezone.utc).isoformat(),
                   "by_strategy_type": [{"strategy": r["_id"]["strategy"], "type": r["_id"]["type"],
                                         "total": r["total"], "wins": r["wins"], "losses": r["losses"],
                                         "avg_crv": round(r.get("avg_crv") or 0, 2)} for r in rows],
                   "total_signals": sum(r["total"] for r in rows)}
        await state.db.analytics_daily.update_one({"date": date}, {"$set": summary})

    tstat_docs = await state.db.trade_stats.find({}, {"date": 1}).to_list(10000)
    for doc in tstat_docs:
        date = doc.get("date")
        if not date:
            continue
        tstats = await state.db.auto_trades.aggregate([
            {"$match": {"trade_date": date, "status": "closed"}},
            {"$group": {"_id": None, "trades": {"$sum": 1},
                        "pnl": {"$sum": "$realized_pnl"},
                        "wins": {"$sum": {"$cond": [{"$eq": ["$result", "win"]}, 1, 0]}}}}],
        ).to_list(1)
        if not tstats or not tstats[0].get("trades"):
            await state.db.trade_stats.delete_one({"date": date})
            removed["trade_stats"] += 1
            continue
        ts = tstats[0]
        await state.db.trade_stats.update_one({"date": date}, {"$set": {
            "date": date, "trades": ts["trades"], "pnl": round(ts.get("pnl") or 0, 4),
            "wins": ts["wins"]}})
    return removed


@router.post("/api/analytics/clear/preview")
async def clear_analytics_preview(body: Dict):
    """Wie viele Einträge würde `POST /api/analytics/clear` löschen?

    Basis für den Bestätigungsdialog („X Analysen betroffen") – rein lesend,
    identische Filter-Logik wie beim echten Löschen.
    """
    rng = (body.get("range") or "all").lower()
    scope = (body.get("scope") or "all").lower()
    symbol = body.get("symbol")
    strategy_id = body.get("strategy_id")
    _validate_clear(rng, scope, symbol, strategy_id)

    scope_filter = _clear_scope_filter(scope, symbol, strategy_id)
    sig_filter: Dict = dict(scope_filter)
    trade_filter: Dict = dict(scope_filter)
    if rng != "all":
        cutoff_iso = (datetime.now(timezone.utc) - CLEAR_DELTAS[rng]).isoformat()
        sig_filter["timestamp"] = {"$gte": cutoff_iso}
        trade_filter["opened_at"] = {"$gte": cutoff_iso}

    signals = await state.db.signals.count_documents(sig_filter)
    trades = await state.db.auto_trades.count_documents(trade_filter)
    symbols = []
    if scope == "strategy":
        symbols = sorted(await state.db.signals.distinct("symbol", sig_filter))
    return {"range": rng, "scope": scope, "symbol": symbol,
            "strategy_id": strategy_id, "signals": signals, "trades": trades,
            "total": signals + trades, "symbols": symbols}


@router.post("/api/analytics/clear")
async def clear_analytics(body: Dict, request: Request,
                          _: bool = Depends(require_admin)):
    """Delete analysis data (signals, performance, daily analytics, trades).
    range: 'hour' | '24h' | '7d' | '4w' | 'all'
    scope: 'all' (alles) | 'coin' (nur symbol) | 'coin_strategy' (symbol + strategy_id)
           | 'strategy' (eine Strategie über ALLE Coins)"""
    rng = (body.get("range") or "all").lower()
    scope = (body.get("scope") or "all").lower()
    symbol = body.get("symbol")
    strategy_id = body.get("strategy_id")
    _validate_clear(rng, scope, symbol, strategy_id)

    scope_filter: Dict = _clear_scope_filter(scope, symbol, strategy_id)

    deleted: Dict[str, int] = {}

    # Fast path: full wipe over everything (previous behaviour)
    if rng == "all" and scope == "all":
        for coll in ["signals", "performance", "analytics_daily", "trade_stats", "auto_trades"]:
            r = await state.db[coll].delete_many({})
            deleted[coll] = r.deleted_count
        open_signal_evals.clear()
        for sym in list(scanner.rule_states.keys()):
            scanner.rule_states[sym] = {}
        await log_action(request, "analytics_clear",
                         {"range": rng, "scope": scope, "deleted": deleted})
        await broadcast({"type": "analytics_cleared", "range": rng, "scope": scope})
        return {"status": "success", "range": rng, "scope": scope, "deleted": deleted}

    if rng == "all":
        sig_filter: Dict = dict(scope_filter)
        trade_filter: Dict = dict(scope_filter)
        cutoff = None
    else:
        cutoff = datetime.now(timezone.utc) - CLEAR_DELTAS[rng]
        cutoff_iso = cutoff.isoformat()
        sig_filter = {"timestamp": {"$gte": cutoff_iso}, **scope_filter}
        trade_filter = {"opened_at": {"$gte": cutoff_iso}, **scope_filter}

    r = await state.db.signals.delete_many(sig_filter)
    deleted["signals"] = r.deleted_count
    r = await state.db.auto_trades.delete_many(trade_filter)
    deleted["auto_trades"] = r.deleted_count

    if scope == "all":
        # time-scoped full delete: drop aggregated day docs directly (previous behaviour)
        cutoff_date = cutoff.astimezone(BERLIN).strftime("%Y-%m-%d")
        r = await state.db.analytics_daily.delete_many({"date": {"$gte": cutoff_date}})
        deleted["analytics_daily"] = r.deleted_count
        r = await state.db.trade_stats.delete_many({"date": {"$gte": cutoff_date}})
        deleted["trade_stats"] = r.deleted_count
    else:
        # coin/strategy scope: day aggregates have no coin/strategy fields
        # -> re-aggregate from remaining signals/auto_trades instead of deleting blindly
        removed = await reaggregate_daily_stats()
        deleted["analytics_daily"] = removed["analytics_daily"]
        deleted["trade_stats"] = removed["trade_stats"]

    await rebuild_performance()
    # drop in-memory evals whose signal was removed
    remaining_ids = {s["id"] for s in await state.db.signals.find({}, {"id": 1}).to_list(200000)}
    open_signal_evals[:] = [ev for ev in open_signal_evals if ev["id"] in remaining_ids]
    await log_action(request, "analytics_clear",
                     {"range": rng, "scope": scope, "symbol": symbol,
                      "strategy_id": strategy_id, "deleted": deleted})
    await broadcast({"type": "analytics_cleared", "range": rng, "scope": scope})
    return {"status": "success", "range": rng, "scope": scope, "deleted": deleted}


# ---------------- KI-Analyse (OpenAI-kompatibler Provider) ----------------
async def _aggregate_ai_stats(strategy_id: str = None) -> Dict:
    """Aggregate signals, trades and strategy definitions for the AI review."""
    q = {"strategy_id": strategy_id} if strategy_id else {}
    signals = await state.db.signals.find(
        q, {"_id": 0, "result": 1, "crv": 1, "strategy_id": 1, "rules_met": 1}) \
        .sort("timestamp", -1).limit(5000).to_list(5000)
    trades = await state.db.auto_trades.find({"status": "closed"}).sort("closed_at", -1).limit(500).to_list(500)

    total = len(signals)
    wins = sum(1 for s in signals if s.get("result") == "win")
    losses = sum(1 for s in signals if s.get("result") == "loss")
    decided = wins + losses
    win_rate = round(wins / decided * 100, 1) if decided else 0.0
    avg_crv = round(sum((s.get("crv") or 0) for s in signals) / total, 2) if total else 0.0

    trade_wins = sum(1 for t in trades if t.get("result") == "win")
    trade_losses = sum(1 for t in trades if t.get("result") == "loss")
    tdec = trade_wins + trade_losses
    trade_win_rate = round(trade_wins / tdec * 100, 1) if tdec else 0.0
    total_pnl = round(sum((t.get("realized_pnl") or 0) for t in trades), 2)

    # Max Drawdown aus kumulierter PnL-Kurve
    sorted_trades = sorted(trades, key=lambda t: t.get("opened_at") or "")
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for t in sorted_trades:
        equity += (t.get("realized_pnl") or 0)
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    # Statistik pro Strategie
    by_strategy: Dict[str, Dict] = {}
    for s in signals:
        sid = s.get("strategy_id", "unknown")
        e = by_strategy.setdefault(sid, {"total": 0, "wins": 0, "losses": 0, "crv_sum": 0.0})
        e["total"] += 1
        if s.get("result") == "win":
            e["wins"] += 1
        elif s.get("result") == "loss":
            e["losses"] += 1
        e["crv_sum"] += (s.get("crv") or 0)
    strat_stats = []
    for sid, e in by_strategy.items():
        strat = strategy_registry.get(sid)
        d = e["wins"] + e["losses"]
        strat_stats.append({
            "id": sid,
            "name": getattr(strat, "STRATEGY_NAME", sid) if strat else sid,
            "total_signals": e["total"],
            "wins": e["wins"],
            "losses": e["losses"],
            "win_rate_prozent": round(e["wins"] / d * 100, 1) if d else 0.0,
            "avg_crv": round(e["crv_sum"] / e["total"], 2) if e["total"] else 0.0,
        })

    # Häufigste Verlust-Setups (Kombination erfüllter Regeln)
    setup_counts: Dict[str, int] = {}
    for s in signals:
        if s.get("result") != "loss":
            continue
        rules = s.get("rules_met") or {}
        met = sorted(k for k, v in rules.items() if v)
        key = " + ".join(met) if met else "(keine Regel als erfüllt geloggt)"
        setup_counts[key] = setup_counts.get(key, 0) + 1
    top_losing = [{"regeln": k, "verluste": v}
                  for k, v in sorted(setup_counts.items(), key=lambda x: -x[1])[:5]]

    # Regel-Definitionen der Strategien
    strategies_meta = strategy_registry.list_all()

    # ---- Detaillierte Einzeltrades für die KI (exakte Werte & Uhrzeiten) ----
    def _berlin(iso):
        if not iso:
            return None
        try:
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            return dt.astimezone(BERLIN).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return iso

    trades_detail = []
    for t in trades[:60]:
        e = _enrich_trade(t)
        comp = e.get("computed", {})
        trades_detail.append({
            "symbol": e.get("symbol"),
            "strategie": e.get("strategy_name") or e.get("strategy_id"),
            "seite": e.get("side"),
            "modus": e.get("mode"),
            "ergebnis": e.get("result"),
            "eroeffnet": _berlin(e.get("opened_at")),
            "geschlossen": _berlin(e.get("closed_at")),
            "dauer_sekunden": comp.get("duration_seconds"),
            "entry": e.get("entry"),
            "initial_sl": e.get("initial_sl"),
            "sl_final": e.get("sl"),
            "tp1": e.get("tp1"),
            "tp_full": e.get("tpf"),
            "exit": e.get("exit_price"),
            "tp1_getroffen": e.get("tp1_hit"),
            "breakeven": e.get("breakeven_moved"),
            "pnl_usdt": e.get("realized_pnl"),
            "r_vielfaches": comp.get("r_multiple"),
            "pnl_prozent_kapital": comp.get("pnl_pct_capital"),
            "sl_abstand_prozent": comp.get("initial_sl_distance_pct"),
            "tp_full_abstand_prozent": comp.get("tpf_distance_pct"),
            "hebel": e.get("leverage"),
            "kapital_usdt": e.get("max_capital"),
            "verlauf": e.get("events", []),
        })

    # Paper vs. Live getrennt
    def _split(mode_val):
        sub = [t for t in trades if t.get("mode") == mode_val]
        w = sum(1 for t in sub if t.get("result") == "win")
        l = sum(1 for t in sub if t.get("result") == "loss")
        d = w + l
        return {
            "anzahl": len(sub), "wins": w, "losses": l,
            "win_rate_prozent": round(w / d * 100, 1) if d else 0.0,
            "pnl_gesamt_usdt": round(sum((t.get("realized_pnl") or 0) for t in sub), 2),
        }

    tp1_hits = sum(1 for t in trades if t.get("tp1_hit"))
    durations = [d.get("duration_seconds") for d in (
        [_enrich_trade(t).get("computed", {}) for t in trades]) if d.get("duration_seconds")]
    avg_dur = round(sum(durations) / len(durations)) if durations else 0

    return {
        "gefiltert_auf_strategie": strategy_id,
        "signale_gesamt": {"anzahl": total, "wins": wins, "losses": losses,
                           "win_rate_prozent": win_rate, "avg_crv": avg_crv},
        "trades_geschlossen": {"anzahl": len(trades), "wins": trade_wins, "losses": trade_losses,
                               "win_rate_prozent": trade_win_rate, "pnl_gesamt_usdt": total_pnl,
                               "max_drawdown_usdt": round(max_dd, 2),
                               "tp1_treffer": tp1_hits,
                               "durchschnittsdauer_sekunden": avg_dur},
        "trades_paper": _split("paper"),
        "trades_live": _split("live"),
        "je_strategie": sorted(strat_stats, key=lambda x: -x["total_signals"]),
        "haeufigste_verlust_setups": top_losing,
        "einzeltrades_detail": trades_detail,
        "strategien_definitionen": strategies_meta,
    }


@router.post("/api/analytics/ai-review")
async def ai_review(body: Dict = None):
    """Aggregierte Trading-Statistiken an ein KI-Modell senden und deutsche
    Coach-Auswertung zurückgeben. Provider frei konfigurierbar via .env
    (OpenAI-kompatibel). Standard: Groq (kostenlos, keine Kreditkarte)."""
    import json as _json
    body = body or {}
    strategy_id = body.get("strategy_id")

    # Abwärtskompatibel: alte GEMINI_*-Variablen werden weiter akzeptiert.
    api_key = (
        os.getenv("AI_API_KEY")
        or os.getenv("GROQ_API_KEY")
        or os.getenv("OPENROUTER_API_KEY")
        or os.getenv("GEMINI_API_KEY")
    )
    if not api_key:
        raise HTTPException(
            status_code=500,
            detail=("Kein KI-API-Key gesetzt. Trage in backend/.env AI_API_KEY ein. "
                    "Kostenlosen Groq-Key holen: https://console.groq.com/keys, "
                    "dann Backend neu starten."),
        )

    base_url = (os.getenv("AI_BASE_URL") or "https://api.groq.com/openai/v1").strip()
    model_name = (os.getenv("AI_MODEL") or os.getenv("GEMINI_MODEL")
                  or "llama-3.3-70b-versatile").strip().strip('"').strip("'").strip()
    if model_name.startswith("models/"):
        model_name = model_name[len("models/"):]
    if not model_name:
        model_name = "llama-3.3-70b-versatile"

    stats = await _aggregate_ai_stats(strategy_id)

    system_msg = (
        "Du bist ein erfahrener Trading-Coach mit Fokus auf Krypto-Daytrading und Scalping. "
        "Analysiere die übergebenen Statistiken sachlich, prägnant und pragmatisch. "
        "Nenne konkret welche Regeln nicht funktionieren und schlage präzise, umsetzbare "
        "Änderungen vor. Antworte auf Deutsch in Markdown mit klaren Sektionen."
    )
    user_text = (
        "Hier sind die aktuellen aggregierten Trading-Statistiken und Regel-Definitionen "
        "als JSON:\n\n```json\n"
        + _json.dumps(stats, ensure_ascii=False, indent=2, default=str)
        + "\n```\n\nAufgabe:\n"
        "1) **Kurz-Fazit** (2-3 Sätze zur Gesamtlage).\n"
        "2) **Problematische Regeln / Setups** - nenne konkret welche Regeln oder "
        "Regelkombinationen unterdurchschnittlich performen und WARUM (Zahlen zitieren).\n"
        "3) **Einzeltrade-Analyse** - nutze `einzeltrades_detail` (exakte Uhrzeiten, "
        "Entry, SL, TP1, Full-TP, Exit, R-Vielfaches, Dauer, paper/live). Finde Muster: "
        "Zu welchen Uhrzeiten/Setups laufen Trades in den SL? Werden TPs zu früh/zu spät "
        "gesetzt? Ist das SL-zu-TP-Verhältnis realistisch? Vergleiche paper vs. live.\n"
        "4) **Konkrete Änderungsvorschläge** - parameterbezogen oder logikbezogen, "
        "so präzise wie möglich (z.B. RSI-Schwelle anpassen, TP/SL-Ratio ändern, "
        "Setup entfernen, Filter hinzufügen, bestimmte Handelszeiten meiden).\n"
        "Antworte auf Deutsch."
    )

    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        completion = await client.chat.completions.create(
            model=model_name,
            temperature=0.4,
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_text},
            ],
        )
        review = (completion.choices[0].message.content or "").strip()
        if not review:
            raise RuntimeError("Leere Antwort vom KI-Modell erhalten.")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("KI-Analyse fehlgeschlagen")
        raise HTTPException(status_code=502, detail=f"KI-Analyse fehlgeschlagen: {e}")

    return {"review": review, "stats": stats, "model": model_name}


@router.get("/api/analytics/time-based/{symbol}")
async def get_time_based(symbol: str, strategy_id: str = None):
    """Zeit-Analyse für einen Coin.

    Rückwärtskompatibel: `time_analytics` + `best_hours` bleiben unverändert
    (Coin gesamt, Stunde+Wochentag kombiniert).
    Neu (genauere Auswertung, optional je Strategie über `?strategy_id=`):
      - by_hour:    nur nach Uhrzeit gruppiert
      - by_weekday: nur nach Wochentag gruppiert
      - by_combo:   Wochentag + Uhrzeit kombiniert
    PRE_SIGNALs werden in den neuen Gruppierungen ausgeschlossen; Win-Rate wird
    aus entschiedenen Signalen (win+loss) berechnet.
    """
    pipeline = [
        {"$match": {"symbol": symbol}},
        {"$group": {"_id": {"hour": "$hour", "weekday": "$weekday"},
                    "total": {"$sum": 1},
                    "wins": {"$sum": {"$cond": [{"$eq": ["$result", "win"]}, 1, 0]}},
                    "losses": {"$sum": {"$cond": [{"$eq": ["$result", "loss"]}, 1, 0]}},
                    "avg_crv": {"$avg": "$crv"}}},
        {"$sort": {"total": -1}},
    ]
    results = await state.db.signals.aggregate(pipeline).to_list(1000)
    weekdays = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
    stats = []
    for r in results:
        total = r["total"]
        wins = r.get("wins", 0)
        stats.append({"hour": r["_id"]["hour"], "weekday": weekdays[r["_id"]["weekday"]],
                      "total_signals": total, "wins": wins, "losses": r.get("losses", 0),
                      "win_rate": round(wins / total * 100, 1) if total else 0,
                      "avg_crv": round(r.get("avg_crv") or 0, 2)})

    # ---- Neue, genauere Gruppierungen (optional je Strategie) ----
    match: Dict = {"symbol": symbol, "signal_class": {"$ne": "PRE_SIGNAL"}}
    if strategy_id:
        match["strategy_id"] = strategy_id

    async def _grouped(group_id: Dict):
        gp = [
            {"$match": match},
            {"$group": {"_id": group_id,
                        "total": {"$sum": 1},
                        "wins": {"$sum": {"$cond": [{"$eq": ["$result", "win"]}, 1, 0]}},
                        "losses": {"$sum": {"$cond": [{"$eq": ["$result", "loss"]}, 1, 0]}},
                        "avg_crv": {"$avg": "$crv"}}},
        ]
        rows = await state.db.signals.aggregate(gp).to_list(1000)
        out = []
        for r in rows:
            gid = r["_id"] or {}
            wins = r.get("wins", 0)
            losses = r.get("losses", 0)
            decided = wins + losses
            entry = {
                "total_signals": r.get("total", 0),
                "wins": wins,
                "losses": losses,
                "decided": decided,
                "win_rate": round(wins / decided * 100, 1) if decided else 0.0,
                "avg_crv": round(r.get("avg_crv") or 0, 2),
            }
            if "hour" in gid and gid["hour"] is not None:
                entry["hour"] = gid["hour"]
            if "weekday" in gid and gid["weekday"] is not None:
                wd = gid["weekday"]
                entry["weekday_index"] = wd
                entry["weekday"] = weekdays[wd] if isinstance(wd, int) and 0 <= wd <= 6 else str(wd)
            out.append(entry)
        return out

    by_hour = sorted(await _grouped({"hour": "$hour"}), key=lambda x: x.get("hour", 0))
    by_weekday = sorted(await _grouped({"weekday": "$weekday"}), key=lambda x: x.get("weekday_index", 0))
    by_combo = sorted(
        await _grouped({"hour": "$hour", "weekday": "$weekday"}),
        key=lambda x: (-(x.get("win_rate", 0) if x.get("decided") else -1), -x.get("total_signals", 0)),
    )

    # ---- Zusätzlich: echter Trade-PnL je Stunde/Wochentag/Kombi ----
    # Basis: geschlossene auto_trades desselben Coins (optional je Strategie).
    # Gruppierung nach OPEN-Zeitpunkt in BERLIN-Zeit, damit die Tages-Rhythmen
    # dem entsprechen, was der Nutzer im UI sieht.
    trade_match: Dict = {"symbol": symbol, "status": "closed", "opened_at": {"$ne": None}}
    if strategy_id:
        trade_match["strategy_id"] = strategy_id

    async def _trade_grouped(group_id: Dict):
        # Berlin ist Europe/Berlin (UTC+1/+2). MongoDB's $hour/$dayOfWeek
        # unterstützen "timezone". opened_at ist als ISO-String gespeichert,
        # daher zuerst per $dateFromString parsen.
        # $dayOfWeek: 1=Sunday..7=Saturday -> normalisieren auf 0=Mo..6=So.
        pipe = [
            {"$match": trade_match},
            {"$addFields": {
                "_openDt": {
                    "$cond": [
                        {"$eq": [{"$type": "$opened_at"}, "date"]},
                        "$opened_at",
                        {"$dateFromString": {"dateString": "$opened_at", "onError": None, "onNull": None}},
                    ]
                }
            }},
            {"$match": {"_openDt": {"$ne": None}}},
            {"$addFields": {
                "_hour": {"$hour": {"date": "$_openDt", "timezone": "Europe/Berlin"}},
                "_dow": {"$dayOfWeek": {"date": "$_openDt", "timezone": "Europe/Berlin"}},
            }},
            {"$addFields": {
                # Mongo dayOfWeek 1=So..7=Sa -> 0=Mo..6=So
                "_wd": {"$mod": [{"$add": [{"$subtract": ["$_dow", 2]}, 7]}, 7]},
            }},
            {"$group": {
                "_id": {
                    **({"hour": "$_hour"} if "hour" in group_id else {}),
                    **({"weekday": "$_wd"} if "weekday" in group_id else {}),
                },
                "trades": {"$sum": 1},
                "wins": {"$sum": {"$cond": [{"$eq": ["$result", "win"]}, 1, 0]}},
                "losses": {"$sum": {"$cond": [{"$eq": ["$result", "loss"]}, 1, 0]}},
                "pnl": {"$sum": {"$ifNull": ["$realized_pnl", 0]}},
                "best_trade": {"$max": {"$ifNull": ["$realized_pnl", 0]}},
                "worst_trade": {"$min": {"$ifNull": ["$realized_pnl", 0]}},
            }},
        ]
        rows = await state.db.auto_trades.aggregate(pipe).to_list(1000)
        out = {}
        for r in rows:
            gid = r.get("_id") or {}
            key_parts = []
            if "hour" in gid:
                key_parts.append(("hour", int(gid["hour"])))
            if "weekday" in gid:
                key_parts.append(("weekday", int(gid["weekday"])))
            key = tuple(key_parts)
            trades = int(r.get("trades", 0))
            wins = int(r.get("wins", 0))
            losses = int(r.get("losses", 0))
            pnl = round(float(r.get("pnl") or 0), 2)
            out[key] = {
                "trades": trades,
                "trade_wins": wins,
                "trade_losses": losses,
                "trade_win_rate": round(wins / (wins + losses) * 100, 1) if (wins + losses) else 0.0,
                "pnl": pnl,
                "avg_pnl": round(pnl / trades, 2) if trades else 0.0,
                "best_trade": round(float(r.get("best_trade") or 0), 2),
                "worst_trade": round(float(r.get("worst_trade") or 0), 2),
            }
        return out

    def _merge(entries, group_id_shape, trade_map):
        """Merge PnL-Daten in Signal-Einträge und ergänze Trade-Only-Buckets."""
        seen = set()
        for e in entries:
            key_parts = []
            if "hour" in group_id_shape:
                key_parts.append(("hour", int(e.get("hour", 0))))
            if "weekday" in group_id_shape:
                key_parts.append(("weekday", int(e.get("weekday_index", 0))))
            key = tuple(key_parts)
            seen.add(key)
            tdata = trade_map.get(key)
            if tdata:
                e.update(tdata)
            else:
                # Keine Trades in diesem Bucket
                e.update({"trades": 0, "trade_wins": 0, "trade_losses": 0,
                          "trade_win_rate": 0.0, "pnl": 0.0, "avg_pnl": 0.0,
                          "best_trade": 0.0, "worst_trade": 0.0})
        # Buckets, die NUR Trades (aber keine Signale) haben, ergänzen
        for key, tdata in trade_map.items():
            if key in seen:
                continue
            entry = {"total_signals": 0, "wins": 0, "losses": 0, "decided": 0,
                     "win_rate": 0.0, "avg_crv": 0.0}
            for name, val in key:
                if name == "hour":
                    entry["hour"] = val
                elif name == "weekday":
                    entry["weekday_index"] = val
                    entry["weekday"] = weekdays[val] if 0 <= val <= 6 else str(val)
            entry.update(tdata)
            entries.append(entry)
        return entries

    trade_by_hour = await _trade_grouped({"hour": "$_hour"})
    trade_by_weekday = await _trade_grouped({"weekday": "$_wd"})
    trade_by_combo = await _trade_grouped({"hour": "$_hour", "weekday": "$_wd"})

    by_hour = sorted(_merge(by_hour, {"hour"}, trade_by_hour), key=lambda x: x.get("hour", 0))
    by_weekday = sorted(_merge(by_weekday, {"weekday"}, trade_by_weekday), key=lambda x: x.get("weekday_index", 0))
    by_combo = sorted(
        _merge(by_combo, {"hour", "weekday"}, trade_by_combo),
        key=lambda x: (-(x.get("pnl", 0) if x.get("trades") else -999999), -(x.get("win_rate", 0) if x.get("decided") else -1)),
    )

    return {"symbol": symbol, "strategy_id": strategy_id,
            "time_analytics": stats,
            "best_hours": sorted(stats, key=lambda x: x["win_rate"], reverse=True)[:5],
            "by_hour": by_hour, "by_weekday": by_weekday, "by_combo": by_combo}


# ---------------- Strategie-Vergleich ----------------
@router.get("/api/analytics/strategy-comparison")
async def strategy_comparison(mode: str = "all", days: int = 0):
    """Vergleicht alle Strategien anhand ihrer geschlossenen Trades:
    Trades, Win-Rate, PnL, Profit-Faktor, Max Drawdown, Ø Dauer, je Coin.
    Manuelle Bitunix-Positionen (Watchdog-Übernahmen + manuell auf der Website
    eröffnete Trades) werden als eigene Pseudo-Strategie 'Manuell (Bitunix)'
    mitverglichen."""
    q: Dict = {"status": "closed"}
    if mode in ("paper", "live"):
        q["mode"] = mode
    if days and days > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        q["opened_at"] = {"$gte": cutoff}
    # Projektion: nur benötigte Felder laden (Trades tragen sonst grosse
    # manage_log/KI-Felder mit -> RAM-Spitzen auf kleinen Servern)
    proj = {"_id": 0, "strategy_id": 1, "strategy_name": 1, "realized_pnl": 1,
            "result": 1, "fees_paid": 1, "max_capital": 1, "side": 1, "mode": 1,
            "symbol": 1, "opened_at": 1, "closed_at": 1, "external_adopted": 1,
            "manual_trade": 1}
    trades = await state.db.auto_trades.find(q, proj).sort("opened_at", 1).to_list(10000)

    MANUAL_ID = "manual"
    MANUAL_NAME = "Manuell (Bitunix)"

    def _is_manual(t: Dict) -> bool:
        return (t.get("strategy_id") or "") == "external" \
            or bool(t.get("manual_trade")) or bool(t.get("external_adopted"))

    open_counts: Dict[str, int] = {}
    async for t in state.db.auto_trades.find(
            {"status": "open"},
            {"_id": 0, "strategy_id": 1, "manual_trade": 1, "external_adopted": 1}):
        sid = MANUAL_ID if _is_manual(t) else (t.get("strategy_id") or "unknown")
        open_counts[sid] = open_counts.get(sid, 0) + 1

    def _dur_min(t):
        try:
            o = datetime.fromisoformat(t["opened_at"].replace("Z", "+00:00"))
            c = datetime.fromisoformat(t["closed_at"].replace("Z", "+00:00"))
            return (c - o).total_seconds() / 60
        except Exception:
            return None

    by_strat: Dict[str, Dict] = {}
    for t in trades:
        manual = _is_manual(t)
        sid = MANUAL_ID if manual else (t.get("strategy_id") or "unknown")
        e = by_strat.setdefault(sid, {
            "strategy_id": sid,
            "strategy_name": MANUAL_NAME if manual else (t.get("strategy_name") or sid),
            "is_manual": manual,
            "trades": 0, "wins": 0, "losses": 0, "breakevens": 0,
            "pnl": 0.0, "fees": 0.0, "gross_win": 0.0, "gross_loss": 0.0,
            "_cap_sum": 0.0,
            "long_trades": 0, "short_trades": 0,
            "paper_trades": 0, "live_trades": 0,
            "_durs": [], "_equity": 0.0, "_peak": 0.0, "max_drawdown": 0.0,
            "best_trade": 0.0, "worst_trade": 0.0,
            "by_symbol": {},
        })
        pnl = float(t.get("realized_pnl") or 0)
        res = t.get("result")
        e["trades"] += 1
        if res == "win":
            e["wins"] += 1
        elif res == "loss":
            e["losses"] += 1
        elif res == "breakeven":
            e["breakevens"] += 1
        e["pnl"] = round(e["pnl"] + pnl, 4)
        e["fees"] = round(e["fees"] + float(t.get("fees_paid") or 0), 4)
        e["_cap_sum"] += float(t.get("max_capital") or 0)
        if pnl > 0:
            e["gross_win"] += pnl
        else:
            e["gross_loss"] += abs(pnl)
        e["best_trade"] = round(max(e["best_trade"], pnl), 4)
        e["worst_trade"] = round(min(e["worst_trade"], pnl), 4)
        if t.get("side") == "LONG":
            e["long_trades"] += 1
        else:
            e["short_trades"] += 1
        if t.get("mode") == "live":
            e["live_trades"] += 1
        else:
            e["paper_trades"] += 1
        d = _dur_min(t)
        if d is not None:
            e["_durs"].append(d)
        e["_equity"] += pnl
        e["_peak"] = max(e["_peak"], e["_equity"])
        e["max_drawdown"] = round(max(e["max_drawdown"], e["_peak"] - e["_equity"]), 4)
        sym = t.get("symbol") or "?"
        s = e["by_symbol"].setdefault(sym, {"symbol": sym, "trades": 0, "wins": 0,
                                            "losses": 0, "pnl": 0.0})
        s["trades"] += 1
        if res == "win":
            s["wins"] += 1
        elif res == "loss":
            s["losses"] += 1
        s["pnl"] = round(s["pnl"] + pnl, 4)

    # 'Manuell (Bitunix)' auch anzeigen, wenn (noch) nur offene manuelle
    # Positionen existieren (z.B. laufende, manuell eröffnete QQQ-Position)
    if open_counts.get(MANUAL_ID) and MANUAL_ID not in by_strat:
        by_strat[MANUAL_ID] = {
            "strategy_id": MANUAL_ID, "strategy_name": MANUAL_NAME, "is_manual": True,
            "trades": 0, "wins": 0, "losses": 0, "breakevens": 0,
            "pnl": 0.0, "fees": 0.0, "gross_win": 0.0, "gross_loss": 0.0,
            "_cap_sum": 0.0, "long_trades": 0, "short_trades": 0,
            "paper_trades": 0, "live_trades": 0,
            "_durs": [], "_equity": 0.0, "_peak": 0.0, "max_drawdown": 0.0,
            "best_trade": 0.0, "worst_trade": 0.0, "by_symbol": {},
        }

    out = []
    for sid, e in by_strat.items():
        decided = e["wins"] + e["losses"]
        e["win_rate"] = round(e["wins"] / decided * 100, 1) if decided else 0.0
        e["avg_pnl"] = round(e["pnl"] / e["trades"], 4) if e["trades"] else 0.0
        # PnL % relativ zur durchschnittlich eingesetzten Marge pro Trade
        cap_sum = e.pop("_cap_sum", 0.0)
        avg_cap = cap_sum / e["trades"] if e["trades"] and cap_sum > 0 else 0.0
        e["pnl_pct"] = round(e["pnl"] / avg_cap * 100, 1) if avg_cap else None
        e["max_drawdown_pct"] = round(e["max_drawdown"] / avg_cap * 100, 1) if avg_cap else None
        gl = e.pop("gross_loss")
        gw = e.pop("gross_win")
        e["profit_factor"] = round(gw / gl, 2) if gl > 0 else (round(gw, 2) if gw else 0.0)
        durs = e.pop("_durs")
        e["avg_duration_min"] = round(sum(durs) / len(durs), 1) if durs else 0.0
        e.pop("_equity", None)
        e.pop("_peak", None)
        e["open_trades"] = open_counts.get(sid, 0)
        for s in e["by_symbol"].values():
            sd = s["wins"] + s["losses"]
            s["win_rate"] = round(s["wins"] / sd * 100, 1) if sd else 0.0
        e["by_symbol"] = sorted(e["by_symbol"].values(), key=lambda x: -x["pnl"])
        strat = strategy_registry.get(sid)
        if strat:
            e["strategy_name"] = getattr(strat, "STRATEGY_NAME", e["strategy_name"])
        out.append(e)
    out.sort(key=lambda x: -x["pnl"])
    return {"mode": mode, "days": days, "comparison": out,
            "total_trades": len(trades)}
