"""Governance-Endpunkte des KI Traders.

Bündelt die Bereiche, in denen der TRADER das letzte Wort hat:
  * MasterPrompt (oberstes Gebot, nur Admin änderbar)
  * Lektionen (anlegen / bearbeiten / löschen – bleiben danach KI-geschützt)
  * Daten-Validierung der KI-Änderungen (Schwellen)
  * Strategie-Labor (Ghost-Phase, Freigabe neuer KI-Strategien)

Bewusst als eigener Router, damit `routers/ai.py` und `routers/ai_lab.py`
unverändert bleiben. GET öffentlich (wie im Rest der App), Schreiben nur Admin.
"""
import asyncio
import logging
from typing import Dict, Optional

from fastapi import APIRouter, Depends, HTTPException

from core.auth import require_admin
from core import state
from core.state import scanner
from services import ai_providers, ai_schedule, notify_guard
from services.ai_engine import ai_engine
from services.ai_lessons import lesson_store
from services.ai_master_prompt import master_prompt
from services.ai_strategy_lab import strategy_lab
from services.ai_validation import validation_gate

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ai-governance"])


def _opinion(topic: str, detail: str):
    """KI im Hintergrund um ihre Meinung zu einer Trader-Änderung bitten."""
    try:
        asyncio.create_task(ai_engine.comment_on_user_change(topic, detail))
    except Exception as e:
        logger.warning(f"KI-Meinung konnte nicht angefragt werden: {e}")


# ---------------- MasterPrompt ----------------
@router.get("/api/ai/master-prompt")
async def get_master_prompt(history: bool = False):
    out = {"master_prompt": master_prompt.snapshot()}
    if history:
        out["history"] = await master_prompt.history()
    return out


@router.post("/api/ai/master-prompt")
async def set_master_prompt(body: Dict, _: bool = Depends(require_admin)):
    if not any(k in body for k in ("text", "rules", "lesson_policy")):
        raise HTTPException(status_code=400,
                            detail="text, rules oder lesson_policy erforderlich")
    snap = await master_prompt.save(text=body.get("text"), rules=body.get("rules"),
                                    lesson_policy=body.get("lesson_policy"))
    # Der MasterPrompt steht über allem: bestehende Lektionen sofort gegen den
    # neuen Stand prüfen und Verstöße löschen (Vorgabe des Traders).
    from services.ai_lessons import lesson_store
    audit = await lesson_store.audit_against_master()
    if audit.get("removed"):
        titles = ", ".join(f"„{r['title']}“" for r in audit["removed"][:5])
        _opinion("MasterPrompt",
                 f"Lektionen-Audit nach MasterPrompt-Änderung: {len(audit['removed'])} "
                 f"Lektion(en) verstießen gegen den neuen MasterPrompt und wurden "
                 f"gelöscht: {titles}")
    _opinion("MasterPrompt", f"Neuer MasterPrompt (v{snap['version']}):\n{snap['text']}\n"
                             f"Grundregeln für Lektionen: {snap['lesson_policy']}\n"
                             f"Harte Regeln: {snap['rules']}")
    return {"status": "success", "master_prompt": snap, "lesson_audit": audit}


# ---------------- Lektionen ----------------
@router.get("/api/ai/lessons")
async def list_lessons():
    return {"lessons": await lesson_store.all()}


@router.get("/api/ai/lessons/conflicts")
async def lesson_conflicts(persist: bool = True):
    """Widersprüchliche Lektionen zum gleichen Thema (z.B. Auto-Leverage vs.
    fester Hebel, Break-Even 30/35/40%) auflösen: Die NEUESTE Trader-Anweisung
    gilt, ältere bleiben gespeichert, sind aber inaktiv (superseded)."""
    res = await lesson_store.conflicts(persist=persist)
    if res.get("conflicts") and ai_engine.learning:
        ai_engine.learning.invalidate_lessons()
    return {"status": "success", **res}


@router.post("/api/ai/lessons")
async def create_lesson(body: Dict, _: bool = Depends(require_admin)):
    title = str(body.get("title") or "").strip()
    detail = str(body.get("detail") or "").strip()
    if not title or not detail:
        raise HTTPException(status_code=400, detail="title und detail erforderlich")
    lesson = await lesson_store.create(title, detail, weight=body.get("weight", 3))
    if ai_engine.learning:
        ai_engine.learning.invalidate_lessons()
    _opinion("Neue Lektion des Traders", f"{title}: {detail}")
    return {"status": "success", "lesson": lesson}


@router.patch("/api/ai/lessons/{lesson_id}")
async def update_lesson(lesson_id: str, body: Dict, _: bool = Depends(require_admin)):
    lesson = await lesson_store.update(lesson_id, body)
    if not lesson:
        raise HTTPException(status_code=404, detail="Lektion nicht gefunden")
    if ai_engine.learning:
        ai_engine.learning.invalidate_lessons()
    _opinion("Vom Trader bearbeitete Lektion",
             f"{lesson['title']}: {lesson['detail']} (ist jetzt für dich unveränderlich)")
    return {"status": "success", "lesson": lesson}


@router.delete("/api/ai/lessons/{lesson_id}")
async def delete_lesson(lesson_id: str, _: bool = Depends(require_admin)):
    ok = await lesson_store.delete(lesson_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Lektion nicht gefunden")
    if ai_engine.learning:
        ai_engine.learning.invalidate_lessons()
    _opinion("Vom Trader gelöschte Lektion", f"Lektion {lesson_id} wurde entfernt")
    return {"status": "success"}


# ---------------- Daten-Validierung ----------------
@router.get("/api/ai/validation")
async def get_validation():
    return validation_gate.status()


@router.post("/api/ai/validation")
async def set_validation(updates: Dict, _: bool = Depends(require_admin)):
    return {"status": "success", "settings": await validation_gate.update(updates)}


# ---------------- Strategie-Labor ----------------
@router.get("/api/ai/strategies")
async def list_candidates(include_rejected: bool = True):
    return {"candidates": await strategy_lab.list_candidates(include_rejected),
            "status": strategy_lab.status()}


@router.post("/api/ai/strategies")
async def create_candidate(body: Dict, _: bool = Depends(require_admin)):
    res = await strategy_lab.create_candidate(body, source=str(body.get("source") or "trader"))
    if res.get("status") != "ok":
        raise HTTPException(status_code=400, detail=res.get("detail"))
    _opinion("Neue Strategie-Vorgabe des Traders",
             f"{res['candidate']['name']}: {res['candidate']['thesis']} "
             f"Regeln: {res['candidate']['rules_text']}")
    return {**res, "status": "success"}


@router.post("/api/ai/strategies/assist")
async def strategy_assist(body: Dict, _: bool = Depends(require_admin)):
    """KI-Hilfe für eigene Strategien: Feedback, Verbesserungen, maschinenlesbare
    Backtest-Regeln. Optional mit cid (bestehender Kandidat) + apply_rules."""
    res = await strategy_lab.assist(body, cid=body.get("cid"),
                                    apply_rules=bool(body.get("apply_rules")))
    if res.get("status") != "ok":
        raise HTTPException(status_code=400, detail=res.get("detail"))
    return res


@router.post("/api/ai/strategies/settings")
async def candidate_settings(updates: Dict, _: bool = Depends(require_admin)):
    return {"status": "success", "settings": await strategy_lab.update_settings(updates)}


@router.post("/api/ai/strategies/dedupe")
async def dedupe_strategy_candidates(_: bool = Depends(require_admin)):
    """Doppelte KI-Strategie-Kandidaten (gleicher Name) sofort aufräumen."""
    return {"status": "success", **await strategy_lab.dedupe_candidates()}


@router.post("/api/ai/strategies/{cid}/decide")
async def decide_candidate(cid: str, body: Dict, _: bool = Depends(require_admin)):
    res = await strategy_lab.decide(cid, str(body.get("action") or ""),
                                    note=str(body.get("note") or ""))
    if res.get("status") != "ok":
        raise HTTPException(status_code=400, detail=res.get("detail"))
    return {"status": "success", **res}


@router.delete("/api/ai/strategies/{cid}")
async def delete_candidate(cid: str, _: bool = Depends(require_admin)):
    """Kandidat endgültig löschen: deregistriert die Custom-Strategie, schließt
    offene Trades des Kandidaten und entfernt Ghost-Trades + Dokument."""
    res = await strategy_lab.delete_candidate(cid)
    if res.get("status") != "ok":
        raise HTTPException(status_code=404, detail=res.get("detail"))
    return {"status": "success", **res}


@router.post("/api/ai/strategies/{cid}/apply-assist")
async def apply_candidate_assist(cid: str, body: Dict = None,
                                 _: bool = Depends(require_admin)):
    """Verbesserungs-Vorschläge der KI in die Strategie übernehmen
    (optional gezielt: {"fields": ["rule_definition"]})."""
    res = await strategy_lab.apply_assist(cid, (body or {}).get("fields"))
    if res.get("status") != "ok":
        raise HTTPException(status_code=400, detail=res.get("detail"))
    return {"status": "success", **res}


@router.get("/api/ai/strategies/{cid}/test-data")
async def candidate_test_data(cid: str):
    """Backtest-/Optimizer-Daten zu genau dieser Strategie (Textaufbereitung,
    identisch zu dem, was die KI im Strategie-Chat sieht)."""
    return {"text": await strategy_lab.test_context(cid),
            "assist_history": await strategy_lab.assist_history(cid, limit=5)}


@router.post("/api/ai/strategies/{cid}/register-test")
async def register_candidate_for_test(cid: str, _: bool = Depends(require_admin)):
    res = await strategy_lab.register_for_testing(cid)
    if res.get("status") == "error":
        raise HTTPException(status_code=404, detail=res.get("detail"))
    return res


@router.post("/api/ai/strategies/{cid}/macro")
async def set_candidate_macro(cid: str, body: Dict, _: bool = Depends(require_admin)):
    """Makro-Parameter (SL, CRV, Hebel ...) einer eigenen KI-Strategie setzen."""
    res = await strategy_lab.update_macro_params(cid, body.get("macro_params") or body)
    if res.get("status") != "ok":
        raise HTTPException(status_code=404, detail=res.get("detail"))
    _opinion("Makro-Parameter einer Strategie (Trader)",
             f"Kandidat {cid}: {res.get('applied')}")
    return {"status": "success", **res}


# ---------------- Analyse-Zeitplan ----------------
@router.get("/api/ai/schedule")
async def get_schedule():
    interval, window = ai_engine.current_interval()
    return {
        "schedule": ai_engine.config.get("schedule") or [],
        "default_interval_min": ai_engine.config.get("interval_min", 10),
        "active": {"interval_min": interval, "window": window},
        "text": ai_schedule.schedule_text(ai_engine.config.get("schedule"),
                                          ai_engine.config.get("interval_min", 10)),
        "max_windows": ai_schedule.MAX_WINDOWS,
    }


@router.post("/api/ai/schedule")
async def set_schedule(body: Dict, _: bool = Depends(require_admin)):
    updates: Dict = {}
    if "schedule" in body:
        updates["schedule"] = body["schedule"]
    if "default_interval_min" in body:
        updates["interval_min"] = body["default_interval_min"]
    if not updates:
        raise HTTPException(status_code=400, detail="schedule oder default_interval_min nötig")
    await ai_engine.update_config(updates)
    interval, window = ai_engine.current_interval()
    return {"status": "success", "schedule": ai_engine.config.get("schedule"),
            "default_interval_min": ai_engine.config.get("interval_min"),
            "active": {"interval_min": interval, "window": window}}


# ---------------- Provider-Zustand (Limit / Fallback) ----------------
@router.get("/api/ai/providers/health")
async def providers_health():
    return ai_providers.health_status()


# ---------------- Kosten-Dashboard: geschätzte Tokens pro Rolle & Tag ----------------
@router.get("/api/ai/token-usage")
async def token_usage(days: int = 7):
    """Geschätzter Token-Verbrauch (Zeichen/4) pro KI-Rolle und Tag (Berlin)."""
    days = max(1, min(31, int(days)))
    rows = await state.db.ai_token_usage.find().sort("date", -1) \
        .limit(days * 12).to_list(days * 12)
    by_day: Dict[str, Dict] = {}
    for r in rows:
        r.pop("_id", None)
        d = by_day.setdefault(r["date"], {"date": r["date"], "tokens": 0,
                                          "calls": 0, "roles": []})
        d["tokens"] += int(r.get("tokens") or 0)
        d["calls"] += int(r.get("calls") or 0)
        d["roles"].append({"role": r.get("role"), "tokens": int(r.get("tokens") or 0),
                           "calls": int(r.get("calls") or 0), "model": r.get("model")})
    out = sorted(by_day.values(), key=lambda d: d["date"], reverse=True)[:days]
    for d in out:
        d["roles"].sort(key=lambda x: -x["tokens"])
    return {"days": out, "note": "Tokens sind eine Schätzung (Zeichen ÷ 4)"}


# ---------------- Telegram-Spam-Bremse ----------------
@router.get("/api/ai/notify-guard")
async def get_notify_guard():
    return {"cooldown_min": scanner.settings.get("notify_cooldown_min",
                                                 notify_guard.DEFAULT_COOLDOWN_MIN),
            "state": notify_guard.status()}


@router.post("/api/ai/notify-guard")
async def set_notify_guard(body: Dict, _: bool = Depends(require_admin)):
    try:
        value = max(0, min(240, int(float(body.get("cooldown_min")))))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="cooldown_min (0-240) erforderlich")
    scanner.settings["notify_cooldown_min"] = value
    await state.db.settings.update_one({"_id": "scanner"},
                                       {"$set": {"notify_cooldown_min": value}}, upsert=True)
    return {"status": "success", "cooldown_min": value}


@router.get("/api/ai/strategies/ghost-trades")
async def ghost_trades(candidate_id: Optional[str] = None, limit: int = 50):
    return {"ghost_trades": await strategy_lab.ghost_trades(candidate_id, limit)}
