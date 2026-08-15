"""Modell-Wächter: prüft wöchentlich alle konfigurierten Modell-Slugs.

Fragt die Live-Modell-Kataloge aller Provider ab (ai_providers.verify_catalog)
und meldet tote Slugs (Modell beim Anbieter entfernt/umbenannt) per Website-
Benachrichtigung + Telegram. Ergebnis wird in `settings/model_watch` abgelegt
und ist über /api/ai/models/watch abrufbar (manueller Lauf: POST .../run).
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict

from services import ai_providers

logger = logging.getLogger(__name__)

DOC_ID = "model_watch"
CHECK_EVERY_S = 12 * 3600      # Loop prüft 2x täglich, ob ein Lauf fällig ist
INTERVAL_DAYS = 7              # wöchentlicher Voll-Check


class ModelWatch:
    def __init__(self):
        self.running = False

    async def status(self, db) -> Dict:
        doc = await db.settings.find_one({"_id": DOC_ID}) or {}
        doc.pop("_id", None)
        return {"running": self.running, "interval_days": INTERVAL_DAYS, **doc}

    async def run_check(self, db, manual: bool = False) -> Dict:
        if self.running:
            return {"status": "busy", "detail": "Modell-Check läuft bereits"}
        self.running = True
        try:
            result = await ai_providers.verify_catalog()
            dead = result.get("dead") or []
            payload = {"checked_at": datetime.now(timezone.utc).isoformat(),
                       "dead": dead,
                       "unverified": result.get("unverified") or [],
                       "providers": result.get("providers") or {},
                       "manual": bool(manual)}
            await db.settings.update_one({"_id": DOC_ID}, {"$set": payload}, upsert=True)
            if dead:
                lst = ", ".join(dead[:8])
                from core import state
                from services import notifications
                await notifications.website_notify(
                    db, "model_watch", "Modell-Wächter: tote Modell-Slugs erkannt",
                    f"Diese konfigurierten KI-Modelle existieren beim Anbieter nicht mehr: {lst}. "
                    "Die Fallback-Ketten übernehmen automatisch – bitte im KI-Team ein anderes "
                    "Modell wählen.", cooldown_min=60)
                await notifications.telegram_notify(
                    db, state.telegram, "model_watch",
                    f"🛰️ *MODELL-WÄCHTER*\nTote Modell-Slugs erkannt: {lst}\n"
                    "Fallbacks übernehmen – bitte Modelle im KI-Team aktualisieren.",
                    cooldown_min=60)
                logger.warning(f"Modell-Wächter: tote Slugs -> {dead}")
            else:
                logger.info("Modell-Wächter: alle konfigurierten Modelle verfügbar")
            return {"status": "ok", **payload}
        except Exception as e:
            logger.error(f"Modell-Wächter fehlgeschlagen: {e}")
            return {"status": "error", "detail": str(e)[:200]}
        finally:
            self.running = False

    async def run_loop(self):
        """Hintergrund-Loop: wöchentlicher Check (2 Min nach Boot erstmals geprüft)."""
        from core import state
        await asyncio.sleep(120)
        while True:
            try:
                db = state.db
                if db is not None:
                    doc = await db.settings.find_one({"_id": DOC_ID}) or {}
                    due = True
                    last = doc.get("checked_at")
                    if last:
                        try:
                            age = datetime.now(timezone.utc) - datetime.fromisoformat(last)
                            due = age.days >= INTERVAL_DAYS
                        except ValueError:
                            due = True
                    if due:
                        await self.run_check(db)
            except Exception as e:
                logger.warning(f"Modell-Wächter-Loop: {e}")
            await asyncio.sleep(CHECK_EVERY_S)


model_watch = ModelWatch()
