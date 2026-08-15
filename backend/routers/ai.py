"""KI Trader (AI Trading Engine) Endpoints."""
import json
from typing import Dict

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from core.audit import log_action
from core.auth import require_admin
from services.ai_engine import ai_engine
from services.ai_news_watcher import news_watcher
from services.ai_roles import role_manager, ROLE_LABELS
from services.news_feed import news_feed

router = APIRouter(tags=["ai"])


@router.get("/api/ai/status")
async def ai_status():
    return ai_engine.status()


@router.get("/api/ai/playbook")
async def ai_playbook_status():
    """Strategie-Playbook: Setups, echte Performance pro Setup, Sperren."""
    from services import ai_playbook
    return await ai_playbook.status(ai_engine.db)


@router.get("/api/ai/fee-guard/stats")
async def ai_fee_guard_stats(days: int = 7):
    """Blockier-Statistik des Fee-Wächters (Anzahl + geschätzte vermiedene Fees)."""
    from datetime import datetime, timezone, timedelta
    days = max(1, min(90, int(days)))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = await ai_engine.db.fee_guard_blocks.find(
        {"ts": {"$gte": cutoff}}, {"_id": 0}).sort("ts", -1).to_list(1000)
    return {"days": days,
            "blocked_total": len(rows),
            "blocked_collection": sum(1 for r in rows if r.get("collection")),
            "est_fees_saved_usdt": round(sum(float(r.get("est_fees_usdt") or 0)
                                             for r in rows), 2),
            "recent": rows[:10]}


@router.post("/api/ai/config")
async def ai_config(updates: Dict, _: bool = Depends(require_admin)):
    cfg = await ai_engine.update_config(updates)
    return {"status": "success", "config": cfg}


@router.post("/api/ai/analyze")
async def ai_analyze_now(_: bool = Depends(require_admin)):
    result = await ai_engine.run_analysis(manual=True)
    return result


@router.get("/api/ai/insights")
async def ai_insights():
    """Performance-Statistik + gelernte Lektionen des KI Traders."""
    stats, lessons = {}, []
    if ai_engine.learning:
        try:
            stats = await ai_engine.learning.gather_stats()
        except Exception as e:
            stats = {"error": str(e)[:120]}
    # Lektionen immer frisch & konsolidiert aus dem LessonStore – inkl. der
    # fortlaufenden Nummer (`no`), von der die KI spricht ("Lektion 6").
    from services.ai_lessons import lesson_store
    try:
        lessons = await lesson_store.all()
    except Exception:
        pass
    doc = await ai_engine.db.settings.find_one({"_id": "ai_lessons"}) or {}
    candidates = []
    if ai_engine.learning:
        candidates = await ai_engine.learning.lesson_candidates()
    from services.ai_master_prompt import master_prompt
    from services.ai_validation import validation_gate
    return {"stats": stats, "lessons": lessons,
            "lesson_candidates": candidates,
            "assessment": doc.get("assessment"),
            "skipped": doc.get("skipped") or [],
            "skipped_items": _skipped_wish_items(doc),
            "last_learn": doc.get("updated_at"), "trigger": doc.get("trigger"),
            "master_prompt": master_prompt.snapshot(),
            "validation": validation_gate.status(),
            "learning": ai_engine.learning.summary() if ai_engine.learning else None}


@router.post("/api/ai/learn")
async def ai_learn_now(_: bool = Depends(require_admin)):
    """Manueller Lernlauf: KI wertet ihre Signal-/Trade-Historie aus."""
    if not ai_engine.learning:
        raise HTTPException(status_code=503, detail="Lern-Modul nicht initialisiert")
    await ai_engine.learning.sync_outcomes()
    return await ai_engine.learning.run_learning(trigger="manual")


@router.get("/api/ai/rewards")
async def ai_rewards_data(days: int = 30):
    """Belohnungssystem: Reward-Verlauf, Auswertung pro Regime + Zusammenfassung."""
    from services import ai_rewards
    db = ai_engine.db
    await ai_rewards.ensure_backfill(db)
    return {"history": await ai_rewards.history(db, days),
            "by_regime": await ai_rewards.by_regime(db, days),
            "summary": await ai_rewards.summary(db, days)}


@router.delete("/api/ai/rewards")
async def ai_rewards_clear(request: Request, _: bool = Depends(require_admin)):
    """Belohnungssystem-Daten komplett löschen (mit Backfill-Sperre)."""
    from services import ai_rewards
    deleted = await ai_rewards.clear(ai_engine.db)
    await log_action(request, "ai_rewards_clear", {"deleted": deleted})
    return {"status": "success", "deleted": deleted}


@router.post("/api/ai/rewards/backfill")
async def ai_rewards_backfill(include_cleared: bool = False,
                              _: bool = Depends(require_admin)):
    """Fehlende Rewards für geschlossene KI-Trades nachbewerten (idempotent).
    include_cleared=true hebt eine frühere Löschung (cleared_at) auf und
    bewertet auch die historischen Trades vor dem Löschzeitpunkt."""
    from services import ai_rewards
    n = await ai_rewards.backfill_missing(ai_engine.db,
                                          include_cleared=include_cleared)
    return {"status": "success", "rewarded": n,
            "include_cleared": include_cleared}


@router.post("/api/ai/trader/reset")
async def ai_trader_reset(body: Dict, request: Request,
                          _: bool = Depends(require_admin)):
    """KI-Trader auf 0 zurücksetzen: löscht ALLE ai_trader-Paper-Trades (inkl.
    Sammel-Trades), ai_trader-Signale und Rewards. Live-Trades bleiben immer
    unangetastet. Erfordert zusätzlich das Admin-Passwort (User-Wunsch)."""
    from core.auth import ADMIN_PASSWORD
    from services import ai_rewards
    if str(body.get("password") or "") != ADMIN_PASSWORD:
        raise HTTPException(status_code=403, detail="Falsches Passwort")
    db = ai_engine.db
    live_untouched = await db.auto_trades.count_documents(
        {"strategy_id": "ai_trader", "mode": "live"})
    trades = await db.auto_trades.delete_many(
        {"strategy_id": "ai_trader", "mode": {"$ne": "live"}})
    signals = await db.signals.delete_many({"strategy_id": "ai_trader"})
    rewards = await ai_rewards.clear(db)
    details = {"trades_deleted": trades.deleted_count,
               "signals_deleted": signals.deleted_count,
               "rewards_deleted": rewards,
               "live_trades_untouched": live_untouched}
    await log_action(request, "ai_trader_reset", details)
    return {"status": "ok", **details}


@router.get("/api/ai/regime/{symbol}")
async def ai_regime(symbol: str):
    """Aktuelles Markt-Regime (v2) eines Symbols – frisch aus dem Kerzen-Puffer
    gerechnet (für das Regime-Badge über dem Haupt-Chart)."""
    from services.ai_market_observer import market_observer
    snap = market_observer.entry_snapshot(symbol.upper())
    return {"symbol": symbol.upper(),
            "features": (snap or {}).get("features"),
            "ts": (snap or {}).get("ts"),
            "source": (snap or {}).get("source")}


def _skipped_wish_items(doc: Dict) -> list:
    """Zurückgestellte Lektions-Wünsche als Objekte (mit stabiler id).
    Alt-Format (reine Strings) wird on-the-fly konvertiert."""
    items = doc.get("skipped_items") or []
    if items:
        return items
    import hashlib
    out = []
    for s in doc.get("skipped") or []:
        s = str(s)
        title, _, rest = s.partition(":")
        out.append({"id": hashlib.sha1(s.encode()).hexdigest()[:8],
                    "title": title.strip()[:120], "detail": "",
                    "reason": rest.strip()[:300], "approvable": True,
                    "ts": doc.get("updated_at")})
    return out


async def _save_skipped_wishes(items: list):
    await ai_engine.db.settings.update_one(
        {"_id": "ai_lessons"},
        {"$set": {"skipped_items": items,
                  "skipped": [f"{i.get('title')}: {i.get('reason')}" for i in items]}},
        upsert=True)


@router.post("/api/ai/lessons/skipped/approve")
async def approve_skipped_wish(body: Dict, _: bool = Depends(require_admin)):
    """Zurückgestellten Lektions-Wunsch der KI bestätigen -> wird sofort
    aktive, vom Trader gesperrte Lektion (umgeht das Validierungs-Gate)."""
    sid = str((body or {}).get("id") or "")
    doc = await ai_engine.db.settings.find_one({"_id": "ai_lessons"}) or {}
    items = _skipped_wish_items(doc)
    item = next((i for i in items if i.get("id") == sid), None)
    if not item:
        raise HTTPException(status_code=404, detail="Lektions-Wunsch nicht gefunden")
    from services.ai_lessons import lesson_store
    lesson = await lesson_store.create(
        item["title"], item.get("detail") or item.get("reason") or item["title"])
    if ai_engine.learning:
        ai_engine.learning.invalidate_lessons()
    await _save_skipped_wishes([i for i in items if i.get("id") != sid])
    return {"status": "success", "lesson": lesson}


@router.post("/api/ai/lessons/skipped/delete")
async def delete_skipped_wish(body: Dict, _: bool = Depends(require_admin)):
    """Zurückgestellten Lektions-Wunsch der KI endgültig löschen."""
    sid = str((body or {}).get("id") or "")
    doc = await ai_engine.db.settings.find_one({"_id": "ai_lessons"}) or {}
    items = _skipped_wish_items(doc)
    if not any(i.get("id") == sid for i in items):
        raise HTTPException(status_code=404, detail="Lektions-Wunsch nicht gefunden")
    await _save_skipped_wishes([i for i in items if i.get("id") != sid])
    return {"status": "success"}


@router.get("/api/ai/models/watch")
async def ai_model_watch_status():
    """Modell-Wächter: letztes Prüfergebnis (tote Modell-Slugs)."""
    from services.ai_model_watch import model_watch
    return await model_watch.status(ai_engine.db)


@router.post("/api/ai/models/watch/run")
async def ai_model_watch_run(_: bool = Depends(require_admin)):
    """Modell-Wächter sofort laufen lassen (alle Slugs live prüfen)."""
    from services.ai_model_watch import model_watch
    return await model_watch.run_check(ai_engine.db, manual=True)


@router.get("/api/ai/proposals")
async def ai_proposals(status: str = None, limit: int = 40):
    """Einstellungs-Vorschläge der KI (pending + Historie)."""
    return {"proposals": await ai_engine.list_proposals(status=status, limit=limit)}


@router.get("/api/ai/proposals/actionable")
async def ai_proposals_actionable(limit: int = 30):
    """Nur Vorschläge, die eine Entscheidung des Traders brauchen.

    Im autonomen Modus immer leer – die KI verwaltet ihre Wünsche selbst und
    wendet sie an, sobald die Validierung sie freigibt."""
    return {"proposals": await ai_engine.actionable_proposals(limit=limit),
            "autonomy": ai_engine.config.get("autonomy", "suggest")}


@router.post("/api/ai/proposals/{pid}")
async def ai_proposal_decide(pid: str, body: Dict, _: bool = Depends(require_admin)):
    action = (body.get("action") or "").lower()
    if action not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="action muss approve|reject sein")
    prop = await ai_engine.decide_proposal(pid, action == "approve")
    if not prop:
        raise HTTPException(status_code=404, detail="Vorschlag nicht gefunden oder bereits entschieden")
    return {"status": "success", "proposal": prop}


@router.get("/api/ai/chat/history")
async def ai_chat_history(limit: int = 80):
    return {"messages": await ai_engine.chat_history(limit)}


@router.post("/api/ai/chat")
async def ai_chat(body: Dict, _: bool = Depends(require_admin)):
    text = (body.get("message") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Nachricht fehlt")

    # Coin-Filter für den Chat-Kontext (Feature: Coin-Auswahl im KI-Chat).
    # Erlaubt eine Liste von Symbolen; leer / "ALL" => alle Coins.
    coins = body.get("coins")
    if isinstance(coins, str):
        coins = [coins]
    elif not isinstance(coins, list):
        coins = None

    async def gen():
        try:
            async for token in ai_engine.chat_stream(text, coins=coins):
                yield f"data: {json.dumps({'t': token})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)[:200]})}\n\n"
        yield f"data: {json.dumps({'done': True})}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@router.delete("/api/ai/chat")
async def ai_chat_clear(request: Request, _: bool = Depends(require_admin)):
    await ai_engine.clear_chat()
    await log_action(request, "ai_chat_clear", {})
    return {"status": "success"}


@router.post("/api/ai/summary")
async def ai_summary_now(_: bool = Depends(require_admin)):
    """Erzwingt manuell einen Tages-Reset inkl. Archivierung und generiert eine
    neue markierte Tages-Zusammenfassung (role='summary', pinned)."""
    result = await ai_engine.force_daily_summary()
    return {"status": "success", **result}


@router.get("/api/ai/news")
async def ai_news(limit: int = 20):
    return {"headlines": await news_feed.get_headlines(limit)}


# ---------------- KI-Team (Rollen) ----------------
@router.get("/api/ai/roles")
async def ai_roles():
    """Konfiguration des KI-Teams (Rollen, Modelle, Handelszeiten, Fallback-KI)."""
    return {"roles": role_manager.snapshot(), "labels": ROLE_LABELS}


@router.post("/api/ai/roles")
async def ai_roles_update(updates: Dict, _: bool = Depends(require_admin)):
    roles = await role_manager.update(ai_engine.db, updates)
    return {"status": "success", "roles": roles}


@router.post("/api/ai/roles/{role}/reset")
async def ai_role_reset(role: str, _: bool = Depends(require_admin)):
    """Rolle auf die empfohlene Voreinstellung zurücksetzen."""
    if role not in ROLE_LABELS:
        raise HTTPException(status_code=404, detail="Rolle unbekannt")
    roles = await role_manager.reset_role(ai_engine.db, role)
    return {"status": "success", "roles": roles}


@router.post("/api/ai/deep-analyze")
async def ai_deep_analyze(_: bool = Depends(require_admin)):
    """Manuell eine Tiefenanalyse (deep_analyst-Rolle) starten."""
    return await ai_engine.run_deep_analysis(manual=True)


@router.get("/api/ai/news-events")
async def ai_news_events(limit: int = 15):
    """Letzte Bewertungen des News-Wächters (24/7)."""
    return {"events": await news_watcher.latest_events(limit),
            "watcher": news_watcher.status()}


@router.post("/api/ai/news-check")
async def ai_news_check(_: bool = Depends(require_admin)):
    """Manuell einen News-Wächter-Durchlauf starten."""
    return await news_watcher.run_check(manual=True)


@router.get("/api/ai/calendar")
async def ai_calendar():
    """Weltwirtschaftskalender (nächste 48h, UTC) – Quelle des News-Wächters."""
    import aiohttp
    from services import macro_context
    async with aiohttp.ClientSession() as session:
        cal = await macro_context.macro_calendar(session)
    return {"calendar": cal}


@router.get("/api/ai/strategy-performance")
async def ai_strategy_performance():
    """Performance aller Strategien (Winrate/PnL) – KI-Leserechte-Ansicht."""
    return {"text": await ai_engine._strategy_performance_text()}


# ---------------- Aufsicht über das KI-Team ----------------
@router.get("/api/ai/supervisor")
async def ai_supervisor_status():
    """Letzter Prüfbericht des Haupt-Modells über das KI-Team."""
    from services.ai_supervisor import supervisor
    return supervisor.status()


@router.post("/api/ai/supervisor/review")
async def ai_supervisor_review(_: bool = Depends(require_admin)):
    """Manuelle Stichproben-Prüfung aller KI-Team-Rollen durch das Haupt-Modell.

    Läuft im Hintergrund (kann je Modell über eine Minute dauern); den Fortschritt
    und das Ergebnis liefert `GET /api/ai/supervisor`."""
    from services.ai_supervisor import supervisor
    return await supervisor.start_review(manual=True)


@router.get("/api/ai/supervisor/history")
async def ai_supervisor_history(limit: int = 10):
    """Verlauf der Prüfberichte (neueste zuerst)."""
    from services.ai_supervisor import supervisor
    return {"reports": await supervisor.history(limit=limit)}


@router.post("/api/ai/supervisor/settings")
async def ai_supervisor_settings(updates: Dict, _: bool = Depends(require_admin)):
    """Automatische Prüfung (täglich) und automatische Modell-Umschaltung steuern."""
    from services.ai_supervisor import supervisor
    return {"status": "success", "settings": await supervisor.update_settings(updates)}


@router.post("/api/ai/supervisor/rollback")
async def ai_supervisor_rollback(_: bool = Depends(require_admin)):
    """Letzte automatische Modell-Umschaltung zurücknehmen."""
    from services.ai_supervisor import supervisor
    res = await supervisor.rollback_switches()
    if res.get("status") != "ok":
        raise HTTPException(status_code=400, detail=res.get("detail"))
    return res


# ---------------- Schnellauswahl der Chat-Vorschläge ----------------
QUICK_PROMPTS_ID = "ai_quick_prompts"
DEFAULT_QUICK_PROMPTS = [
    "Wie ist deine aktuelle Performance?",
    "Was hast du zuletzt gelernt?",
    "Sei heute defensiv",
    "Begründe deine letzte Entscheidung",
]


@router.get("/api/ai/quick-prompts")
async def ai_quick_prompts():
    """Vom Trader gepflegte Chat-Vorschläge (geräteübergreifend gespeichert)."""
    doc = await ai_engine.db.settings.find_one({"_id": QUICK_PROMPTS_ID})
    prompts = (doc or {}).get("prompts")
    return {"prompts": prompts if isinstance(prompts, list) and prompts
            else list(DEFAULT_QUICK_PROMPTS),
            "customized": bool(isinstance(prompts, list) and prompts)}


@router.post("/api/ai/quick-prompts")
async def ai_quick_prompts_save(body: Dict, _: bool = Depends(require_admin)):
    """Reihenfolge/Inhalt der Schnellauswahl speichern (max. 30 Einträge)."""
    raw = body.get("prompts")
    if not isinstance(raw, list):
        raise HTTPException(status_code=400, detail="prompts muss eine Liste sein")
    prompts = [str(p).strip()[:160] for p in raw if str(p).strip()][:30]
    await ai_engine.db.settings.update_one({"_id": QUICK_PROMPTS_ID},
                                          {"$set": {"prompts": prompts}}, upsert=True)
    return {"status": "success", "prompts": prompts}
