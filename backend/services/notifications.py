"""Zentrale Benachrichtigungen: Website-Meldungen + Telegram (per Toggle).

Telegram-Toggles (Reiter Telegram in den Einstellungen):
  ai_failure      – KI-Ausfall (Primär + Backup gescheitert, Fallback übernimmt)
  backtest_done   – Backtest fertig
  optimizer_done  – Optimizer fertig
  trade_opened    – Trade eröffnet
  trade_closed    – Trade geschlossen (SL/TP/manuell)
  kill_switch     – Kill-Switch / Risiko-Notbremse ausgelöst
  daily_summary   – tägliche Zusammenfassung um Mitternacht

Website-Meldungen landen in `app_notifications` (Frontend pollt /api/notifications).
"""
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, Optional

logger = logging.getLogger(__name__)

CONFIG_ID = "telegram_notify_config"
DEFAULT_CONFIG = {
    "ai_failure": True,
    "backtest_done": True,
    "optimizer_done": True,
    "trade_opened": True,
    "trade_closed": True,
    "kill_switch": True,
    "watchdog": True,             # Positions-Watchdog (fehlender SL, Übernahmen)
    "model_watch": True,          # Modell-Wächter (tote Modell-Slugs erkannt)
    "daily_summary": True,
    "token_alert": True,          # Token-Kosten-Wächter (ungewöhnlich hoher Verbrauch)
    "website_ai_failure": True,   # Meldung auf der Website bei KI-Ausfall
}

# Deutsche Rollen-Namen (identisch zum KI-Team im Frontend) für Meldungstexte
ROLE_LABELS = {
    "analyst": "Analyst",
    "deep_analyst": "Tiefen-Analyst",
    "research_analyst": "Forschungs-Analyst",
    "market_observer": "Markt-Beobachter",
    "trade_manager": "Trade-Manager",
    "news_watcher": "News-Wächter",
    "chat": "Chat-Assistent",
    "learner": "Lern-Modul",
    "summarizer": "Tages-Reporter",
}
REASON_TEXT = {
    "rate_limited": "Rate-Limit erreicht",
    "error": "Ausfall (Fehler/Timeout)",
    "skipped_too_large": "Prompt zu groß fürs Token-Budget",
}

_cfg_cache: Optional[Dict] = None
_cfg_ts = 0.0
# Cooldown gegen Meldungs-Spam (z.B. KI-Ausfall bei jedem Call)
_last_sent: Dict[str, float] = {}


async def get_config(db) -> Dict:
    global _cfg_cache, _cfg_ts
    if _cfg_cache is not None and time.time() - _cfg_ts < 30:
        return _cfg_cache
    cfg = dict(DEFAULT_CONFIG)
    try:
        doc = await db.settings.find_one({"_id": CONFIG_ID})
        if doc:
            doc.pop("_id", None)
            cfg.update({k: bool(v) for k, v in doc.items() if k in DEFAULT_CONFIG})
    except Exception as e:
        logger.warning(f"notify config load failed: {e}")
    _cfg_cache, _cfg_ts = cfg, time.time()
    return cfg


async def update_config(db, updates: Dict) -> Dict:
    global _cfg_cache, _cfg_ts
    clean = {k: bool(v) for k, v in (updates or {}).items() if k in DEFAULT_CONFIG}
    if clean:
        await db.settings.update_one({"_id": CONFIG_ID}, {"$set": clean}, upsert=True)
    _cfg_cache, _cfg_ts = None, 0.0
    return await get_config(db)


async def website_notify(db, ntype: str, title: str, message: str,
                         cooldown_min: float = 30,
                         dedupe_key: Optional[str] = None,
                         source: Optional[str] = None,
                         meta: Optional[Dict] = None) -> bool:
    """Meldung für die Website (einmalig, mit Cooldown pro Typ).
    `source`/`meta` = Herkunfts-Analyse für die Glocke (wer hat's gemeldet,
    welches Modell/welche Rolle, Ursache). `popped=False` -> Popup nur 1×."""
    key = dedupe_key or f"web:{ntype}:{title}"
    now = time.time()
    if now - _last_sent.get(key, 0) < cooldown_min * 60:
        return False
    _last_sent[key] = now
    try:
        await db.app_notifications.insert_one({
            "id": uuid.uuid4().hex[:12], "type": ntype, "title": title,
            "message": message, "read": False, "popped": False,
            "source": source, "meta": meta or {},
            "created_at": datetime.now(timezone.utc).isoformat()})
        return True
    except Exception as e:
        logger.warning(f"website notify failed: {e}")
        return False


async def telegram_notify(db, telegram, ntype: str, text: str,
                          cooldown_min: float = 0,
                          dedupe_key: Optional[str] = None) -> bool:
    """Telegram-Nachricht, wenn der Toggle für `ntype` an ist."""
    cfg = await get_config(db)
    if not cfg.get(ntype, True):
        return False
    if cooldown_min > 0:
        key = dedupe_key or f"tg:{ntype}"
        now = time.time()
        if now - _last_sent.get(key, 0) < cooldown_min * 60:
            return False
        _last_sent[key] = now
    bot = getattr(telegram, "bot", None)
    chat_id = getattr(telegram, "chat_id", None)
    if not bot or not chat_id:
        return False
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown",
                               disable_web_page_preview=True)
        return True
    except Exception as e:
        logger.warning(f"telegram notify ({ntype}) failed: {e}")
        return False


def _md_safe(text: str) -> str:
    """Markdown-kritische Zeichen entschärfen (sonst scheitert Telegram-Send)."""
    return (str(text or "").replace("*", "").replace("_", " ").replace("`", "'")
            .replace("[", "(").replace("]", ")"))


def _fail_lines(failures: Optional[list], limit: int = 6) -> list:
    """Kompakte Ursachen-Zeilen: Ausfälle werden nach Provider + Ursache
    GRUPPIERT (statt einer Riesen-Zeile pro Modell), die betroffenen Modelle
    bleiben aber einzeln benannt."""
    groups: Dict = {}
    order = []
    for f in (failures or []):
        model = str(f.get("model") or "?")
        prov, _, name = model.partition("/")
        if not name:
            prov, name = "?", model
        key = (prov, f.get("reason"))
        g = groups.get(key)
        if g is None:
            g = groups[key] = {"models": [], "detail": str(f.get("detail") or "").strip()}
            order.append(key)
        if name not in g["models"]:
            g["models"].append(name)
    lines = []
    for key in order[:limit]:
        prov, reason = key
        g = groups[key]
        reason_text = REASON_TEXT.get(reason, reason or "Fehler")
        models = ", ".join(g["models"][:6])
        if len(g["models"]) > 6:
            models += f" (+{len(g['models']) - 6} weitere)"
        line = f"{prov} – {reason_text}: {models}"
        detail = _md_safe(g["detail"])[:110]
        if detail:
            line += f" ({detail})" if len(g["models"]) == 1 else f" (z.B. {detail})"
        lines.append(line)
    return lines


def _models_short(models: list, limit: int = 4) -> str:
    """Modell-Liste für die Überschrift kompakt halten (ab `limit` nur noch
    'provider (n Modelle)' statt jedes Modell einzeln)."""
    models = [str(m) for m in (models or [])]
    if len(models) <= limit:
        return ", ".join(models)
    by_prov: Dict[str, int] = {}
    for m in models:
        by_prov[m.partition("/")[0]] = by_prov.get(m.partition("/")[0], 0) + 1
    return ", ".join(f"{p} ({n} Modelle)" for p, n in by_prov.items())


async def notify_ai_failure(role: str, failed_models: list,
                            fallback_model: Optional[str],
                            failures: Optional[list] = None):
    """KI-Ausfall: Primär + Backup gescheitert -> Website + Telegram (Toggle).
    `failures` = Detail-Analyse pro Modell (Ursache + Fehlertext), damit
    nachvollziehbar ist, WARUM jedes Modell ausgefallen ist.
    Lazy imports, damit ai_providers keine harten Abhängigkeiten bekommt."""
    try:
        from core import state
        db = state.db
        if db is None:
            return
        role_label = ROLE_LABELS.get(role, role)
        failed = _models_short(failed_models)
        if fallback_model:
            title = f"KI-Ausfall: {role_label}"
            msg = (f"Primär- und Backup-KI ausgefallen ({failed}). "
                   f"Notfall-Fallback übernimmt: {fallback_model}.")
        else:
            title = f"KI komplett ausgefallen: {role_label}"
            msg = f"Alle Modelle gescheitert ({failed}). Keine Antwort möglich."
        lines = _fail_lines(failures)
        detail_txt = ""
        if lines:
            detail_txt = "\nUrsachen im Detail:\n" + "\n".join(f"• {ln}" for ln in lines)
        try:
            from core.timeutil import now_berlin
            ts_txt = now_berlin().strftime("%d.%m.%Y %H:%M:%S")
        except Exception:
            ts_txt = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M:%S UTC")
        cfg = await get_config(db)
        if cfg.get("website_ai_failure", True):
            await website_notify(db, "ai_failure", title, msg + detail_txt,
                                 cooldown_min=30,
                                 source="KI-Team (Fallback-Kette)",
                                 meta={"role": role_label,
                                       "failed_models": failed_models[:6],
                                       "failures": (failures or [])[:6],
                                       "fallback": fallback_model})
        await telegram_notify(db, state.telegram, "ai_failure",
                              f"🤖⚠️ *{title}*\n{msg}{detail_txt}\n_{ts_txt} Uhr_",
                              cooldown_min=30)
    except Exception as e:
        logger.warning(f"notify_ai_failure failed: {e}")


def summarize_model_failures(items: list) -> tuple:
    """Gesammelte Modell-Ausfälle zu EINER kompakten Meldung zusammenfassen
    (rein, testbar). Rückgabe: (title, message, meta).

    Format bei mehreren Ausfällen – pro Provider+Ursache eine Zeile, dahinter
    WELCHER Assistent mit WELCHEN Modellen betroffen war:
      • groq – Rate-Limit erreicht → Analyst: gpt-oss-120b, llama-3.3-70b · Trade-Manager: gpt-oss-20b
    """
    seen = set()
    uniq = []
    for it in (items or []):
        k = (it.get("provider"), it.get("model"), it.get("reason"), it.get("role"))
        if k in seen:
            continue
        seen.add(k)
        uniq.append(it)
    if not uniq:
        return "", "", {}
    if len(uniq) == 1:
        it = uniq[0]
        reason_text = REASON_TEXT.get(it.get("reason"), it.get("reason") or "Fehler")
        msg = f"{it.get('provider')}/{it.get('model')} – {reason_text}."
        if it.get("detail") and it.get("reason") not in ("rate_limited", "skipped_too_large"):
            msg += f" Details: {_md_safe(it['detail'])[:120]}."
        msg += (f" Fallback übernimmt: {it['fallback']}." if it.get("fallback")
                else " Die Fallback-Kette übernimmt automatisch.")
        return f"KI-Warnung: {it.get('role') or 'unbekannte Rolle'}", msg, {
            "role": it.get("role"), "provider": it.get("provider"),
            "model": it.get("model"),
            "reason": REASON_TEXT.get(it.get("reason"), it.get("reason")),
            "detail": str(it.get("detail") or "")[:200], "fallback": it.get("fallback")}
    groups: Dict = {}
    order = []
    for it in uniq:
        key = (it.get("provider") or "?", it.get("reason"))
        g = groups.get(key)
        if g is None:
            g = groups[key] = {"roles": {}}
            order.append(key)
        role = it.get("role") or "?"
        models = g["roles"].setdefault(role, [])
        if it.get("model") not in models:
            models.append(it.get("model"))
    lines = []
    for prov, reason in order:
        g = groups[(prov, reason)]
        reason_text = REASON_TEXT.get(reason, reason or "Fehler")
        parts = []
        for role, models in g["roles"].items():
            m_txt = ", ".join(str(m) for m in models[:5])
            if len(models) > 5:
                m_txt += f" (+{len(models) - 5})"
            parts.append(f"{role}: {m_txt}")
        lines.append(f"• {prov} – {reason_text} → " + " · ".join(parts))
    roles = sorted({str(it.get("role")) for it in uniq if it.get("role")})
    title = f"KI-Warnung: {len(uniq)} Modell-Ausfälle (zusammengefasst)"
    msg = "\n".join(lines) + "\nDie Fallback-Kette übernimmt automatisch."
    return title, msg, {"roles": roles, "failures": uniq[:12], "aggregated": True}


# Sammel-Puffer für Modell-Ausfälle: statt einer Glocken-Meldung pro Modell
# werden alle Ausfälle innerhalb des Fensters zu EINER Meldung zusammengefasst.
MODEL_FAILURE_AGGREGATE_S = 75
_pending_fail: list = []
_pending_task = None


async def notify_model_failure(role: Optional[str], provider: str, model: str,
                               reason: str, detail: str = "",
                               fallback: Optional[str] = None):
    """Einzelne Modell-Ausfälle sammeln und als EINE zusammengefasste Meldung
    in die Website-Glocke spiegeln (betroffener Assistent + Modelle bleiben
    einzeln benannt). Cooldown 15 min pro Provider+Ursache gegen Spam."""
    global _pending_task
    try:
        import asyncio

        from core import state
        db = state.db
        if db is None:
            return
        cfg = await get_config(db)
        if not cfg.get("website_ai_failure", True):
            return
        _pending_fail.append({
            "role": ROLE_LABELS.get(role or "", role or "unbekannte Rolle"),
            "provider": provider, "model": model, "reason": reason,
            "detail": str(detail)[:200], "fallback": fallback})
        if _pending_task is None or _pending_task.done():
            _pending_task = asyncio.get_running_loop().create_task(
                _flush_model_failures(db))
    except Exception as e:
        logger.warning(f"notify_model_failure failed: {e}")


async def _flush_model_failures(db):
    import asyncio
    try:
        await asyncio.sleep(MODEL_FAILURE_AGGREGATE_S)
        items = list(_pending_fail)
        _pending_fail.clear()
        if not items:
            return
        title, msg, meta = summarize_model_failures(items)
        if not title:
            return
        key_sig = ",".join(sorted({f"{i.get('provider')}:{i.get('reason')}" for i in items}))
        await website_notify(
            db, "ai_failure", title, msg, cooldown_min=15,
            dedupe_key=f"web:ai_fail_agg:{key_sig}",
            source="Modell-Verwaltung (ai_providers)", meta=meta)
    except Exception as e:
        logger.warning(f"notify_model_failure flush failed: {e}")


def _de_num(n: int) -> str:
    return f"{int(n):,}".replace(",", ".")


async def notify_token_spike(role: str, today_tokens: int, baseline: int, days: int):
    """Token-Kosten-Wächter: Website-Glocke + Telegram (Toggle `token_alert`),
    max. 1× pro Rolle und Tag."""
    try:
        from core import state
        db = state.db
        if db is None:
            return
        role_label = ROLE_LABELS.get(role, role)
        title = f"Hoher Token-Verbrauch: {role_label}"
        if baseline > 0:
            msg = (f"Heute bereits ~{_de_num(today_tokens)} Tokens – das "
                   f"{today_tokens / baseline:.1f}-fache des Schnitts der letzten "
                   f"{days} Tage (~{_de_num(baseline)}/Tag). Modell/Intervall der "
                   f"Rolle im KI-Team prüfen.")
        else:
            msg = (f"Heute bereits ~{_de_num(today_tokens)} Tokens (noch keine "
                   f"Vergleichstage). Modell/Intervall der Rolle im KI-Team prüfen.")
        day = datetime.now(timezone.utc).date().isoformat()
        await website_notify(db, "token_alert", title, msg, cooldown_min=1440,
                             dedupe_key=f"web:token:{role}:{day}",
                             source="Token-Kosten-Wächter",
                             meta={"role": role_label,
                                   "today_tokens": int(today_tokens),
                                   "baseline": int(baseline)})
        await telegram_notify(db, state.telegram, "token_alert",
                              f"📈⚠️ *{title}*\n{msg}", cooldown_min=1440,
                              dedupe_key=f"tg:token:{role}:{day}")
    except Exception as e:
        logger.warning(f"notify_token_spike failed: {e}")
