"""KI-Labor-Endpoints: Forschungs-Analyst, ML-Labor (Optuna/XGBoost),
Markt-Beobachter und KI-Gedächtnis (MongoDB/Supabase).

Bewusst als eigener Router, damit routers/ai.py (KI Trader) unverändert bleibt.
GET-Endpunkte sind wie im Rest der App öffentlich, schreibende Aktionen
erfordern Admin-Auth.
"""
import logging
from typing import Dict, Optional

from fastapi import APIRouter, Depends, HTTPException

from core.auth import require_admin
from services.ai_closed_loop import closed_loop
from services.ai_engine import ai_engine
from services.ai_market_observer import market_observer
from services.ai_memory import memory, KINDS
from services.ai_ml_lab import ml_lab
from services.ai_research import research_analyst
from services.ai_trade_manager import trade_manager

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ai-lab"])


@router.get("/api/ai/lab/status")
async def lab_status():
    """Gesamtstatus des KI-Ökosystems (für das KI-Labor-Panel)."""
    return {
        "research": research_analyst.status(),
        "ml": ml_lab.status(),
        "observer": market_observer.status(),
        "memory": await memory.stats(),
        "trade_manager": trade_manager.status(),
        "closed_loop": closed_loop.status(),
        "kinds": KINDS,
    }


# ---------------- KI-Trade-Steuerung ----------------
@router.get("/api/ai/trade/status")
async def trade_status(limit: int = 20):
    return {"status": trade_manager.status(),
            "actions": await trade_manager.recent_actions(limit)}


@router.post("/api/ai/trade/settings")
async def trade_settings(updates: Dict, _: bool = Depends(require_admin)):
    return {"status": "success", "settings": await trade_manager.update_settings(updates)}


@router.post("/api/ai/trade/review")
async def trade_review(_: bool = Depends(require_admin)):
    """KI prüft jetzt alle offenen Trades und passt sie an."""
    return await trade_manager.review(manual=True)


@router.post("/api/ai/trade/action")
async def trade_action(body: Dict, _: bool = Depends(require_admin)):
    """Einzelne Aktion ausführen – von der KI oder manuell aus dem UI.

    body: {trade_id, action, value?, pct?, target?: tp1|tpf, reason?}
    Manuelle Aufrufe (source=manuell) umgehen die KI-Limits bewusst."""
    trade_id = str(body.get("trade_id") or "")
    action = str(body.get("action") or "")
    if not trade_id or not action:
        raise HTTPException(status_code=400, detail="trade_id und action erforderlich")
    source = str(body.get("source") or "manuell")
    return await trade_manager.apply_action(
        trade_id, action, value=body.get("value"), pct=body.get("pct"),
        target=str(body.get("target") or "tp1"), reason=str(body.get("reason") or ""),
        source=source, enforce_limits=(source == "ki"))


@router.post("/api/ai/trade/open")
async def trade_open(body: Dict, _: bool = Depends(require_admin)):
    """Custom-Trade eröffnen (Symbol, Seite, SL/TP in %, Hebel, Kapitalanteil)."""
    return await trade_manager.open_trade(body, source=str(body.get("source") or "manuell"))


# ---------------- Closed Loop (Selbstoptimierung) ----------------
@router.get("/api/ai/closed_loop/status")
async def closed_loop_status():
    return closed_loop.status()


@router.post("/api/ai/closed_loop/settings")
async def closed_loop_settings(updates: Dict, _: bool = Depends(require_admin)):
    return {"status": "success", "settings": await closed_loop.update_settings(updates)}


@router.post("/api/ai/closed_loop/run")
async def closed_loop_run(_: bool = Depends(require_admin)):
    return await closed_loop.run_now(trigger="manual")


# ---------------- Forschungs-Analyst ----------------
@router.get("/api/ai/research/report")
async def research_report():
    doc = research_analyst.report
    if doc is None and ai_engine.db is not None:
        doc = await ai_engine.db.settings.find_one({"_id": "ai_research_report"})
        if doc:
            doc.pop("_id", None)
    return {"report": doc, "status": research_analyst.status()}


@router.post("/api/ai/research/run")
async def research_run(_: bool = Depends(require_admin)):
    return await research_analyst.run(manual=True, trigger="manual")


@router.post("/api/ai/research/reset")
async def research_reset(_: bool = Depends(require_admin)):
    """Forschungs-Daten (Report + Zustand) löschen."""
    return await research_analyst.reset()


@router.get("/api/ai/research/data")
async def research_data():
    """Aufbereitete Rohdaten (Digests), die der Forschungs-Analyst auswertet."""
    return await research_analyst.collect()


# ---------------- ML-Labor ----------------
@router.get("/api/ai/ml/status")
async def ml_status():
    return ml_lab.status()


@router.post("/api/ai/ml/train")
async def ml_train(body: Optional[Dict] = None, _: bool = Depends(require_admin)):
    body = body or {}
    trials = body.get("n_trials")
    return await ml_lab.train(manual=True, n_trials=int(trials) if trials else None)


@router.post("/api/ai/ml/settings")
async def ml_settings(updates: Dict, _: bool = Depends(require_admin)):
    return {"status": "success", "settings": await ml_lab.update_settings(updates)}


@router.post("/api/ai/ml/reset")
async def ml_reset(_: bool = Depends(require_admin)):
    """ML-Trainingsdaten (gespeichertes Modell + Status) löschen."""
    return await ml_lab.reset()


@router.get("/api/ai/ml/dataset")
async def ml_dataset():
    """Datenlage für das Training (ohne Training zu starten)."""
    try:
        _X, y, meta = await ml_lab.load_training_data()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])
    return {"dataset": meta, "labels": len(y), "features": ml_lab.model_meta
            and ml_lab.model_meta.get("features")}


@router.get("/api/ai/ml/predict")
async def ml_predict(symbol: str = "BTCUSDT", confidence: float = 70.0):
    feats = market_observer.features_for(symbol)
    if not feats:
        raise HTTPException(status_code=404,
                            detail="Kein Marktzustand für dieses Symbol – "
                                   "Markt-Beobachter zuerst laufen lassen")
    return {"symbol": symbol, "market_state": feats,
            "win_probability": ml_lab.predict_sides(feats, confidence)}


# ---------------- Markt-Beobachter ----------------
@router.get("/api/ai/observer/status")
async def observer_status():
    return market_observer.status()


@router.post("/api/ai/observer/run")
async def observer_run(_: bool = Depends(require_admin)):
    return await market_observer.run_check(manual=True)


@router.get("/api/ai/observer/snapshots")
async def observer_snapshots(limit: int = 30, symbol: Optional[str] = None):
    limit = max(1, min(500, limit))
    q: Dict = {"symbol": symbol} if symbol else {}
    rows = []
    if ai_engine.db is not None:
        rows = await ai_engine.db.ai_market_snapshots.find(q) \
            .sort("ts", -1).limit(limit).to_list(limit)
        for r in rows:
            r.pop("_id", None)
    return {"snapshots": rows, "latest": list(market_observer.snapshots.values())}


# ---------------- KI-Gedächtnis ----------------
@router.get("/api/ai/memory/stats")
async def memory_stats(health: bool = False):
    return await (memory.health() if health else memory.stats())


@router.get("/api/ai/memory/entries")
async def memory_entries(kind: Optional[str] = None, limit: int = 30):
    return {"entries": await memory.recall(kind=kind, limit=limit), "kinds": KINDS}
