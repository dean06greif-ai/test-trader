import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from core.state import scanner, websocket_clients

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ws"])


@router.websocket("/api/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    websocket_clients.append(websocket)
    try:
        await websocket.send_json({"type": "connected"})
        # send current rule states snapshot
        for sym, states in scanner.rule_states.items():
            await websocket.send_json({"type": "rule_states", "symbol": sym, "data": states})
        while True:
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
                await websocket.send_json({"type": "pong"})
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "ping"})
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        if websocket in websocket_clients:
            websocket_clients.remove(websocket)
