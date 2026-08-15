"""Gate v1 (Shadow) – Endpoints. GETs öffentlich (App-Konvention),
schreibende Aktionen Admin-only. Das Gate blockt in Phase 5 NICHTS."""
import logging
from typing import Dict, Optional

from fastapi import APIRouter, Depends

from core.auth import require_admin
from services.ml_gate import ml_gate

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ml-gate"])


@router.get("/api/ml/gate/status")
async def gate_status():
    return ml_gate.status()


@router.get("/api/ml/gate/dataset")
async def gate_dataset():
    """Datenlage (krypto-only, Prod nur lesend in Dev) – ohne Training."""
    try:
        _rows, y, _w, _tss, meta = await ml_gate.build_dataset()
    except Exception as e:
        return {"status": "error", "detail": str(e)[:200]}
    return {"status": "ok", "dataset": meta, "labels": len(y)}


@router.post("/api/ml/gate/train")
async def gate_train(_: bool = Depends(require_admin)):
    return await ml_gate.train()


@router.get("/api/ml/gate/report")
async def gate_report(days: int = 28, threshold: Optional[float] = None):
    """Kontrafaktische Shadow-Auswertung inkl. Aktivierungskriterien."""
    return await ml_gate.shadow_report(days=days, threshold=threshold)


@router.get("/api/ml/gate/models")
async def gate_models(limit: int = 20):
    if ml_gate.db is None:
        return {"models": []}
    rows = await ml_gate.db.ml_gate_models.find(
        {}, projection={"_id": 0, "booster_b64": 0}) \
        .sort("version", -1).limit(max(1, min(100, limit))).to_list(100)
    return {"models": rows}


@router.post("/api/ml/gate/settings")
async def gate_settings(updates: Dict, _: bool = Depends(require_admin)):
    return {"status": "success", "settings": await ml_gate.update_settings(updates)}
