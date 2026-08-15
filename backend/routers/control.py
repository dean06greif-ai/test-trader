"""Admin control toggles (Stop All Trades / Stop All Signals)."""
import logging

from fastapi import APIRouter, Depends

from core import state
from core.auth import require_admin
from core.state import control_state
from core.pipeline import broadcast

logger = logging.getLogger(__name__)

router = APIRouter(tags=["control"])


async def _persist_control_state():
    await state.db.settings.update_one(
        {"_id": "control_state"},
        {"$set": {"trades_paused": control_state["trades_paused"],
                  "signals_paused": control_state["signals_paused"]}},
        upsert=True,
    )


@router.get("/api/control/state")
async def get_control_state():
    return {"trades_paused": control_state["trades_paused"],
            "signals_paused": control_state["signals_paused"]}


@router.post("/api/control/stop-trades")
async def toggle_stop_trades(_: bool = Depends(require_admin)):
    """Toggle 'Stop All Trades'. When switched ON, the bot is ONLY blocked from
    opening NEW trades – already open positions stay untouched and keep being
    managed (SL/TP/Break-Even) until they close on their own.
    Manual trades placed by the user directly on Bitunix are never affected.
    When switched OFF, the bot resumes with the previous per-coin config."""
    new_val = not control_state["trades_paused"]
    control_state["trades_paused"] = new_val
    await _persist_control_state()
    await broadcast({"type": "control_state", "data": dict(control_state)})
    return {"status": "success", "trades_paused": new_val}


@router.post("/api/control/stop-signals")
async def toggle_stop_signals(_: bool = Depends(require_admin)):
    """Toggle 'Stop All Signals'. When ON, signals are not emitted, saved or
    broadcast. When OFF, signal emission resumes exactly with the previously
    enabled strategies (no config touched)."""
    new_val = not control_state["signals_paused"]
    control_state["signals_paused"] = new_val
    await _persist_control_state()
    await broadcast({"type": "control_state", "data": dict(control_state)})
    return {"status": "success", "signals_paused": new_val}
