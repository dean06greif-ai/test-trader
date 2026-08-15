"""Endpoints für die lokale Ausführung.

Zwei Gruppen:
- /api/worker/*       -> vom lokalen Worker aufgerufen (X-Worker-Token)
- /api/localworker/*  -> von der Website-UI aufgerufen (Admin für Schreibaktionen)
"""
import gzip
import io
import json
import logging
import zipfile
from pathlib import Path
from typing import Dict

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from core import state
from core.auth import require_admin
from core.config import BACKTEST_SYMBOLS
from services import local_exec

logger = logging.getLogger(__name__)

router = APIRouter(tags=["local-worker"])

BACKEND_DIR = Path(__file__).resolve().parents[1]
WORKER_DIR = BACKEND_DIR.parent / "local_worker"

# Selbst-Check beim Start: fehlt das Worker-Paket, sofort laut melden
# (genau der wiederkehrende Bug "Worker-Paket unvollständig").
_missing_boot = [f for f in ("worker.py", "requirements.txt", "README.md")
                 if not (WORKER_DIR / f).is_file()]
if _missing_boot:
    logger.error(f"Worker-Paket unvollständig: in {WORKER_DIR} fehlen {_missing_boot} – "
                 "local_worker/ muss im Repo committet bleiben (siehe .gitignore-Whitelist)!")


async def require_worker(request: Request):
    token = request.headers.get("X-Worker-Token")
    expected = await local_exec.get_token(state.db)
    if not token or token != expected:
        raise HTTPException(status_code=401, detail="Ungültiges Worker-Token")
    return True


# ================= Worker-Endpoints =================
@router.post("/api/worker/poll")
async def worker_poll(body: Dict, _: bool = Depends(require_worker)):
    """Heartbeat + Job-Claim in einem Call (Worker pollt alle ~2s)."""
    worker_id = str(body.get("worker_id") or "")
    if not worker_id:
        raise HTTPException(status_code=400, detail="worker_id erforderlich")
    local_exec.heartbeat(worker_id, body)
    local_exec.check_stale()
    job = local_exec.claim(worker_id,
                           want_compute=bool(body.get("want_compute", True)),
                           want_data=bool(body.get("want_data", True)))
    settings = await local_exec.get_settings(state.db)
    return {"job": job, "cancel_ids": local_exec.cancel_ids(), "settings": settings}


@router.post("/api/worker/job/{job_id}/progress")
async def worker_progress(job_id: str, body: Dict, _: bool = Depends(require_worker)):
    return local_exec.apply_progress(job_id, body or {})


@router.post("/api/worker/job/{job_id}/result")
async def worker_result(job_id: str, request: Request, _: bool = Depends(require_worker)):
    raw = await request.body()
    if request.headers.get("Content-Encoding", "").lower() == "gzip" or \
            (raw[:2] == b"\x1f\x8b"):
        try:
            raw = gzip.decompress(raw)
        except OSError:
            raise HTTPException(status_code=400, detail="Ungültige gzip-Daten")
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Ungültiges JSON")
    await local_exec.apply_result(job_id, data, state.db)
    return {"status": "ok"}


# ================= UI-Endpoints =================
@router.get("/api/localworker/status")
async def localworker_status():
    local_exec.check_stale()
    return {
        "online": local_exec.worker_online(),
        "workers": local_exec.workers_public(),
        "queue": local_exec.queue_public(),
        "data_jobs": local_exec.data_jobs_public(),
        "settings": await local_exec.get_settings(state.db),
        "required_version": local_exec.REQUIRED_WORKER_VERSION_STR,
    }


@router.get("/api/localworker/settings")
async def localworker_get_settings():
    return {"settings": await local_exec.get_settings(state.db)}


@router.post("/api/localworker/settings")
async def localworker_set_settings(body: Dict, _: bool = Depends(require_admin)):
    try:
        settings = await local_exec.save_settings(state.db, body or {})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "success", "settings": settings}


@router.get("/api/localworker/token")
async def localworker_token(_: bool = Depends(require_admin)):
    return {"token": await local_exec.get_token(state.db)}


@router.post("/api/localworker/token/regenerate")
async def localworker_token_regenerate(_: bool = Depends(require_admin)):
    return {"token": await local_exec.regenerate_token(state.db)}


# ---- Daten-Verwaltung (Downloads laufen als Jobs auf dem Worker) ----
@router.post("/api/localworker/data/download")
async def localworker_data_download(body: Dict, _: bool = Depends(require_admin)):
    symbols = [s for s in (body.get("symbols") or []) if s in BACKTEST_SYMBOLS]
    if not symbols:
        raise HTTPException(status_code=400, detail="Mindestens 1 gültiger Coin erforderlich")
    days = min(max(int(body.get("days") or 30), 1), 5500)
    if not local_exec.worker_online():
        raise HTTPException(status_code=503, detail="Kein lokaler Worker verbunden")
    job = local_exec.create_data_job("data_download", {"symbols": symbols, "days": days})
    return {"status": "queued", "job": job}


@router.post("/api/localworker/data/update")
async def localworker_data_update(_: bool = Depends(require_admin)):
    if not local_exec.worker_online():
        raise HTTPException(status_code=503, detail="Kein lokaler Worker verbunden")
    job = local_exec.create_data_job("data_update", {})
    return {"status": "queued", "job": job}


@router.post("/api/localworker/data/delete")
async def localworker_data_delete(body: Dict, _: bool = Depends(require_admin)):
    symbol = body.get("symbol")
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol erforderlich")
    if not local_exec.worker_online():
        raise HTTPException(status_code=503, detail="Kein lokaler Worker verbunden")
    job = local_exec.create_data_job("data_delete", {"symbol": symbol})
    return {"status": "queued", "job": job}


@router.post("/api/localworker/data/cancel/{job_id}")
async def localworker_data_cancel(job_id: str, _: bool = Depends(require_admin)):
    if not local_exec.cancel_data_job(job_id):
        raise HTTPException(status_code=404, detail="Daten-Job nicht gefunden")
    return {"status": "cancelling"}


# ---- Worker-Paket zum Download (immer aktueller Code-Stand des Servers) ----
# Diese Dateien MÜSSEN im ZIP liegen, sonst ist das Paket unbrauchbar
# (genau der Fehler: heruntergeladenes Paket ohne worker.py/requirements.txt).
REQUIRED_WORKER_FILES = ("worker.py", "requirements.txt", "README.md")
SKIP_WORKER_FILES = {"worker_config.json"}


def _worker_files():
    """Auszuliefernde Dateien des Worker-Ordners (ohne lokale Configs/Logs)."""
    if not WORKER_DIR.is_dir():
        return []
    out = []
    for p in sorted(WORKER_DIR.iterdir()):
        if not p.is_file() or p.name in SKIP_WORKER_FILES:
            continue
        if p.name.endswith((".log", ".pyc")):
            continue
        out.append(p)
    return out


def _package_manifest() -> Dict:
    files = _worker_files()
    names = {p.name for p in files}
    modules = {sub: len(list((BACKEND_DIR / sub).glob("*.py")))
               for sub in ("core", "services", "strategies", "models")
               if (BACKEND_DIR / sub).is_dir()}
    missing = [f for f in REQUIRED_WORKER_FILES if f not in names]
    return {"required_version": local_exec.REQUIRED_WORKER_VERSION_STR,
            "worker_files": sorted(names), "modules": modules,
            "missing": missing, "complete": not missing and bool(modules)}


@router.get("/api/localworker/package/manifest")
async def localworker_package_manifest():
    """Was steckt im Download-Paket? Zeigt fehlende Pflichtdateien sofort an."""
    return _package_manifest()


@router.get("/api/localworker/package")
async def localworker_package():
    manifest = _package_manifest()
    if manifest["missing"]:
        raise HTTPException(
            status_code=500,
            detail="Worker-Paket unvollständig – auf dem Server fehlen: "
                   + ", ".join(manifest["missing"]))
    buf = io.BytesIO()
    try:
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            # `core` muss mit ins Paket: services/history_sources und
            # candle_cache lösen die Historien-Quelle über core.instruments auf.
            for sub in ("core", "services", "strategies", "models"):
                d = BACKEND_DIR / sub
                if not d.is_dir():
                    continue
                for p in sorted(d.glob("*.py")):
                    z.write(p, f"{sub}/{p.name}")
                if not (d / "__init__.py").is_file():
                    z.writestr(f"{sub}/__init__.py", "")
            for p in _worker_files():
                z.write(p, p.name)
            z.writestr("PACKAGE_INFO.json", json.dumps(manifest, indent=2))
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Paket konnte nicht erstellt werden: {e}")
    return Response(content=buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition":
                             'attachment; filename="krypto_local_worker.zip"'})
