"""Audit-Log für destruktive Admin-Aktionen (wer/wann/was).

Hintergrund: Am 13.08. verschwanden Trades/Rewards aus Prod, ohne dass
nachvollziehbar war, wer den Lösch-Button geklickt hat. Jede Löschung wird
jetzt in `audit_log` protokolliert (Zeitpunkt, Aktion, IP, Browser, Umfang).
Ein Fehler beim Protokollieren darf die eigentliche Aktion nie blockieren.
"""
import logging
from datetime import datetime, timezone
from typing import Dict, Optional

from fastapi import Request

from core import state
from core.auth import ADMIN_USER

logger = logging.getLogger(__name__)


async def log_action(request: Optional[Request], action: str, details: Dict = None):
    try:
        ip = None
        ua = None
        if request is not None:
            ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or \
                (request.client.host if request.client else None)
            ua = (request.headers.get("user-agent") or "")[:160]
        await state.db.audit_log.insert_one({
            "ts": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "user": ADMIN_USER,
            "ip": ip,
            "user_agent": ua,
            "details": details or {},
        })
    except Exception as e:  # noqa: BLE001 – Audit darf Aktionen nie blockieren
        logger.warning(f"audit_log fehlgeschlagen ({action}): {e}")
