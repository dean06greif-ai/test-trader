"""Lokale Ausführung: Worker-Registry, Job-Queue und Ergebnis-Übernahme.

Der lokale Worker (local_worker/worker.py) nutzt exakt dieselben Module
(services.backtester / services.optimizer / services.candle_cache) und
verbindet sich per Outbound-Polling (keine Portfreigaben nötig).

Design-Prinzip: Lokale Jobs leben weiterhin in bt.JOBS / opt.JOBS. Dadurch
funktionieren alle bestehenden Status-/Active-/Cancel-/Equity-/Export-/
Apply-Endpoints und die komplette UI unverändert – nur die Berechnung
findet auf dem lokalen Rechner statt.
"""
import asyncio
import logging
import secrets
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

from services import backtester as bt
from services import optimizer as opt
from services import regime_lab as rlab

logger = logging.getLogger(__name__)

WORKER_TIMEOUT = 8         # Sekunden ohne Heartbeat -> offline (Worker pollt alle ~2s;
                           # Rechenlast läuft im Worker in eigenem Thread und
                           # blockiert das Polling nicht)
QUEUED_TIMEOUT = 300       # Job wartet ohne Worker -> Fehler (kurze Reconnects tolerieren)
STALE_TIMEOUT = 300        # Worker online, aber Job meldet keinen Fortschritt mehr -> Fehler
OFFLINE_JOB_TIMEOUT = 180  # laufender Job + Worker offline: so lange darf die Verbindung
                           # abreißen (z.B. WinError 121 / WLAN-Aussetzer), bevor der Job
                           # als Fehler endet – der Worker rechnet währenddessen weiter
                           # und lädt das Ergebnis nach der Wiederverbindung hoch
CANCEL_GRACE = 3           # Abbruch angefordert, Worker bestätigt nicht -> hart abbrechen
MAX_RESULT_TRADES = 50000  # wie Cloud-Persistierung

DEFAULT_SETTINGS = {
    "cpu_cores": 0,             # 0 = alle Kerne
    "ram_limit_mb": 4096,       # Obergrenze Kerzen-RAM-Cache des Workers
    "use_gpu": False,           # GPU (NVIDIA/CuPy) für Indikator-Vorberechnung
    "max_parallel_jobs": 1,     # gleichzeitige Rechen-Jobs auf dem Worker
    "data_dir": "",             # leer = Standardordner des Workers
    "auto_update_enabled": False,
    "auto_update_minutes": 60,
}

WORKERS: Dict[str, Dict] = {}
COMPUTE_QUEUE: List[Dict] = []          # {"job_id","kind","payload"}
LOCAL_JOBS: Dict[str, Dict] = {}        # job_id -> {"kind","state","enqueued_at","last_update","worker_id"}
DATA_JOBS: Dict[str, Dict] = {}         # Daten-Jobs (Download/Update/Löschen)
DATA_QUEUE: List[str] = []
_watchdog_task = None
_settings_cache: Optional[Dict] = None
_token_cache: Optional[str] = None


def _now() -> float:
    return time.time()


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------- Worker-Registry ----------------
REQUIRED_WORKER_VERSION = (1, 9, 0)
REQUIRED_WORKER_VERSION_STR = "1.9.0"


def _ver(v) -> tuple:
    try:
        return tuple(int(x) for x in str(v or "0").split("."))
    except ValueError:
        return (0,)


def worker_online() -> bool:
    return any(_now() - w.get("last_seen", 0) < WORKER_TIMEOUT for w in WORKERS.values())


def worker_supports_dynamic() -> bool:
    """Dynamik-Modus braucht Worker >= 1.3.0 (sonst würde er den Job als
    Discovery interpretieren und ein unbrauchbares Ergebnis liefern)."""
    for w in WORKERS.values():
        if _now() - w.get("last_seen", 0) >= WORKER_TIMEOUT:
            continue
        try:
            parts = tuple(int(x) for x in str(w.get("version") or "0").split("."))
            if parts >= (1, 3):
                return True
        except ValueError:
            continue
    return False


def worker_supports_regime_lab() -> bool:
    """Regime-Lab-Jobs braucht Worker >= 1.5.0 (ältere Worker kennen den
    Job-Typ nicht und würden ihn ignorieren)."""
    for w in WORKERS.values():
        if _now() - w.get("last_seen", 0) >= WORKER_TIMEOUT:
            continue
        if _ver(w.get("version")) >= (1, 3):
            return True
    return False


def worker_supports_explore() -> bool:
    """Endlos-Suche (Optimizer mode='explore') braucht Worker >= 1.9.0
    (sanfter Stop via 'stop'-Flag + deep_explore-Modul im Paket)."""
    for w in WORKERS.values():
        if _now() - w.get("last_seen", 0) >= WORKER_TIMEOUT:
            continue
        if _ver(w.get("version")) >= (1, 9):
            return True
    return False


def heartbeat(worker_id: str, body: Dict):
    w = WORKERS.setdefault(worker_id, {})
    for k in ("name", "version", "resources", "gpu", "data", "running_jobs", "sim_workers"):
        if body.get(k) is not None:
            w[k] = body[k]
    w["last_seen"] = _now()
    w["last_seen_iso"] = _iso()
    if len(WORKERS) > 5:  # alte Worker-Einträge aufräumen
        for wid in sorted(WORKERS, key=lambda x: WORKERS[x].get("last_seen", 0))[:-5]:
            WORKERS.pop(wid, None)


def workers_public() -> List[Dict]:
    # Worker, die sich seit >30 min nicht gemeldet haben, verschwinden aus der
    # Liste – sonst hängt eine alte Sitzung als Phantom-Eintrag im UI.
    for wid in [w for w, v in WORKERS.items()
                if _now() - v.get("last_seen", 0) > 1800]:
        WORKERS.pop(wid, None)
    out = []
    for wid, w in WORKERS.items():
        out.append({
            "worker_id": wid, "name": w.get("name"), "version": w.get("version"),
            "online": _now() - w.get("last_seen", 0) < WORKER_TIMEOUT,
            "last_seen": w.get("last_seen_iso"),
            "resources": w.get("resources") or {},
            "gpu": w.get("gpu") or {},
            "data": w.get("data") or {},
            "sim_workers": w.get("sim_workers"),
            "running_jobs": w.get("running_jobs") or [],
            "outdated": _ver(w.get("version")) < REQUIRED_WORKER_VERSION,
        })
    out.sort(key=lambda x: (not x["online"], x.get("last_seen") or ""), reverse=False)
    return out


# ---------------- Einstellungen & Token (Mongo-persistiert) ----------------
async def get_settings(db) -> Dict:
    global _settings_cache
    if _settings_cache is None:
        doc = None
        if db is not None:
            doc = await db.settings.find_one({"_id": "local_worker_settings"})
        _settings_cache = {**DEFAULT_SETTINGS,
                           **{k: v for k, v in (doc or {}).items() if k in DEFAULT_SETTINGS}}
    return _settings_cache


async def save_settings(db, patch: Dict) -> Dict:
    cur = dict(await get_settings(db))
    for k, v in (patch or {}).items():
        if k in DEFAULT_SETTINGS:
            cur[k] = v
    try:
        cur["cpu_cores"] = min(max(int(cur.get("cpu_cores") or 0), 0), 128)
        cur["ram_limit_mb"] = min(max(int(cur.get("ram_limit_mb") or 4096), 512), 262144)
        cur["max_parallel_jobs"] = min(max(int(cur.get("max_parallel_jobs") or 1), 1), 8)
        cur["auto_update_minutes"] = min(max(int(cur.get("auto_update_minutes") or 60), 5), 1440)
        cur["use_gpu"] = bool(cur.get("use_gpu"))
        cur["auto_update_enabled"] = bool(cur.get("auto_update_enabled"))
        cur["data_dir"] = str(cur.get("data_dir") or "")
    except (TypeError, ValueError):
        raise ValueError("Ungültige Einstellungswerte")
    global _settings_cache
    _settings_cache = cur
    if db is not None:
        await db.settings.update_one({"_id": "local_worker_settings"},
                                     {"$set": cur}, upsert=True)
    return cur


async def get_token(db) -> str:
    global _token_cache
    if _token_cache:
        return _token_cache
    doc = None
    if db is not None:
        doc = await db.settings.find_one({"_id": "local_worker_token"})
    if doc and doc.get("token"):
        _token_cache = doc["token"]
        return _token_cache
    token = secrets.token_hex(24)
    if db is not None:
        await db.settings.update_one({"_id": "local_worker_token"},
                                     {"$set": {"token": token}}, upsert=True)
    _token_cache = token
    return token


async def regenerate_token(db) -> str:
    global _token_cache
    token = secrets.token_hex(24)
    if db is not None:
        await db.settings.update_one({"_id": "local_worker_token"},
                                     {"$set": {"token": token}}, upsert=True)
    _token_cache = token
    return token


# ---------------- Job-Queue ----------------
def _get_job(job_id: str, kind: str) -> Optional[Dict]:
    if kind == "backtest":
        return bt.JOBS.get(job_id)
    if kind == "optimizer":
        return opt.JOBS.get(job_id)
    if kind == "regime_lab":
        return rlab.JOBS.get(job_id)
    return DATA_JOBS.get(job_id)


def enqueue_compute(kind: str, job_id: str, payload: Dict):
    """Rechen-Job (Backtest/Optimizer) für den lokalen Worker einreihen.
    Der Job existiert bereits in bt.JOBS/opt.JOBS (Status 'running')."""
    job = _get_job(job_id, kind)
    if job is not None:
        job["execution"] = "local"
        job["phase"] = "Wartet auf lokalen Worker..."
    COMPUTE_QUEUE.append({"job_id": job_id, "kind": kind, "payload": payload})
    LOCAL_JOBS[job_id] = {"kind": kind, "state": "queued", "enqueued_at": _now(),
                          "last_update": _now(), "worker_id": None}
    ensure_watchdog()
    logger.info(f"local_exec: enqueued {kind} job {job_id}")


def create_data_job(kind: str, params: Dict) -> Dict:
    jid = "d" + uuid.uuid4().hex[:11]
    DATA_JOBS[jid] = {"id": jid, "kind": kind, "params": params, "status": "queued",
                      "progress": 0, "phase": "Wartet auf lokalen Worker...",
                      "cancel": False, "created_at": _iso(), "error": None,
                      "summary": None, "execution": "local"}
    DATA_QUEUE.append(jid)
    LOCAL_JOBS[jid] = {"kind": kind, "state": "queued", "enqueued_at": _now(),
                       "last_update": _now(), "worker_id": None}
    if len(DATA_JOBS) > 20:
        for k in [k for k, v in list(DATA_JOBS.items())
                  if v["status"] in ("done", "error", "cancelled")][:-10]:
            DATA_JOBS.pop(k, None)
    ensure_watchdog()
    return DATA_JOBS[jid]


def claim(worker_id: str, want_compute: bool = True, want_data: bool = True) -> Optional[Dict]:
    """Nächsten Job an den Worker vergeben (Daten-Jobs zuerst, sie sind IO-bound)."""
    if want_data:
        while DATA_QUEUE:
            jid = DATA_QUEUE.pop(0)
            dj = DATA_JOBS.get(jid)
            if not dj or dj.get("cancel"):
                if dj:
                    dj["status"] = "cancelled"
                    dj["phase"] = "Abgebrochen"
                LOCAL_JOBS.pop(jid, None)
                continue
            dj["status"] = "running"
            dj["phase"] = "Vom Worker übernommen..."
            meta = LOCAL_JOBS.setdefault(jid, {"kind": dj["kind"], "enqueued_at": _now()})
            meta.update({"state": "claimed", "worker_id": worker_id, "last_update": _now()})
            return {"job_id": jid, "kind": dj["kind"], "payload": dj["params"]}
    if want_compute:
        w_ver = _ver((WORKERS.get(worker_id) or {}).get("version"))
        skipped = []
        while COMPUTE_QUEUE:
            item = COMPUTE_QUEUE.pop(0)
            # Regime-Lab-Jobs nur an Worker vergeben, die den Job-Typ kennen –
            # ein alter Worker würde ihn sonst stumm liegen lassen.
            if item["kind"] == "regime_lab":
                fn = (((item.get("payload") or {}).get("args") or {}).get("fn"))
                need = (1, 7) if fn == "calibrate" else (1, 5)
                if w_ver < need:
                    skipped.append(item)
                    continue
            # Endlos-Suche nur an Worker >= 1.9 (ältere kennen mode='explore' nicht)
            if item["kind"] == "optimizer":
                _mode = (((((item.get("payload") or {}).get("args") or {})
                           .get("body")) or {}).get("mode"))
                if _mode == "explore" and w_ver < (1, 9):
                    skipped.append(item)
                    continue
            job = _get_job(item["job_id"], item["kind"])
            if not job or job.get("status") != "running" or job.get("cancel"):
                if job is not None and job.get("cancel"):
                    job["status"] = "cancelled"
                    job["phase"] = "Abgebrochen"
                LOCAL_JOBS.pop(item["job_id"], None)
                continue
            meta = LOCAL_JOBS.setdefault(item["job_id"],
                                         {"kind": item["kind"], "enqueued_at": _now()})
            meta.update({"state": "claimed", "worker_id": worker_id, "last_update": _now()})
            job["phase"] = "Vom lokalen Worker übernommen..."
            COMPUTE_QUEUE[:0] = skipped
            return item
        COMPUTE_QUEUE[:0] = skipped
    return None


def cancel_ids() -> List[str]:
    out = []
    for jid, meta in LOCAL_JOBS.items():
        if meta.get("state") != "claimed":
            continue
        job = _get_job(jid, meta["kind"])
        if job is not None and job.get("cancel"):
            out.append(jid)
    return out


def cancel_data_job(job_id: str) -> bool:
    dj = DATA_JOBS.get(job_id)
    if not dj:
        return False
    dj["cancel"] = True
    if dj["status"] == "queued":
        dj["status"] = "cancelled"
        dj["phase"] = "Abgebrochen"
        if job_id in DATA_QUEUE:
            DATA_QUEUE.remove(job_id)
        LOCAL_JOBS.pop(job_id, None)
    else:
        dj["phase"] = "Wird abgebrochen..."
    return True


# ---------------- Fortschritt & Ergebnis vom Worker ----------------
def apply_progress(job_id: str, data: Dict) -> Dict:
    meta = LOCAL_JOBS.get(job_id)
    if not meta:
        return {"cancel": True}  # unbekannter Job -> Worker soll abbrechen
    job = _get_job(job_id, meta["kind"])
    if job is None:
        LOCAL_JOBS.pop(job_id, None)
        return {"cancel": True}
    if isinstance(data.get("progress"), (int, float)):
        job["progress"] = max(0, min(round(data["progress"]), 100))
    if data.get("phase"):
        job["phase"] = str(data["phase"])[:200]
    if data.get("best") is not None:
        job["best"] = data["best"]
    meta["last_update"] = _now()
    # "stop": sanfter Stop der Endlos-Suche (Suche beenden, Bestes behalten)
    return {"cancel": bool(job.get("cancel")),
            "stop": bool(job.get("stop_explore"))}


async def apply_result(job_id: str, data: Dict, db):
    meta = LOCAL_JOBS.pop(job_id, None)
    kind = (meta or {}).get("kind") or data.get("kind")
    status = data.get("status") if data.get("status") in ("done", "error", "cancelled") else "error"
    job = _get_job(job_id, kind) if kind else None
    if job is None:
        logger.warning(f"local_exec: result for unknown job {job_id} ({kind})")
        return
    if meta is None and job.get("status") != "running":
        # Job wurde server-seitig bereits beendet (z.B. hart abgebrochen oder
        # Worker-Trennung erkannt) -> verspätetes Ergebnis verwerfen.
        logger.info(f"local_exec: late result for finished job {job_id} ignored")
        return
    job["status"] = status
    job["error"] = data.get("error")
    # Der Worker kennt seinen eigenen Ausführungsmodus nicht -> hier stempeln,
    # damit die Laufzeit-Anzeige korrekt "lokal" statt "cloud" zeigt.
    res = data.get("result")
    if isinstance(res, dict) and isinstance(res.get("benchmark"), dict):
        res["benchmark"]["execution"] = "local"
        wid = (meta or {}).get("worker_id")
        res["benchmark"]["worker_name"] = (WORKERS.get(wid) or {}).get("name") if wid else None
    if status == "done":
        job["progress"] = 100
        job["phase"] = "Fertig (lokal berechnet)"
    elif status == "cancelled":
        job["phase"] = "Abgebrochen"
    else:
        job["phase"] = "Fehler"

    if kind == "backtest":
        job["result"] = data.get("result")
        rows = data.get("export_trades") or []
        job["export_trades"] = rows[:MAX_RESULT_TRADES]
        if status == "done" and db is not None:
            try:
                await db.backtests.insert_one({"id": job_id, "params": job.get("params"),
                                               "created_at": job.get("created_at"),
                                               "result": job["result"]})
                await db.backtest_trades.insert_one({"job_id": job_id,
                                                     "created_at": job.get("created_at"),
                                                     "rows": rows[:MAX_RESULT_TRADES]})
            except Exception as e:
                logger.warning(f"local backtest persist failed: {e}")
    elif kind == "optimizer":
        job["result"] = data.get("result")
        if data.get("best") is not None:
            job["best"] = data["best"]
        rows_opt = data.get("export_trades") or []
        if rows_opt:
            job["export_trades"] = rows_opt[:25000]
        if status == "done" and db is not None:
            try:
                await db.optimizer_runs.insert_one({"id": job_id, "params": job.get("params"),
                                                    "created_at": job.get("created_at"),
                                                    "result": job["result"]})
                if rows_opt:
                    await db.optimizer_trades.insert_one(
                        {"job_id": job_id, "created_at": job.get("created_at"),
                         "rows": rows_opt[:25000]})
            except Exception as e:
                logger.warning(f"local optimizer persist failed: {e}")
            try:
                from services import learning
                await learning.record_run(db, job["result"])
            except Exception as e:  # noqa: BLE001
                logger.warning(f"learning record failed: {e}")
            try:
                if isinstance(job.get("result"), dict) and job["result"].get("explore"):
                    from services import deep_explore
                    await deep_explore.persist_best(db, job["result"])
            except Exception as e:  # noqa: BLE001
                logger.warning(f"explore persist failed: {e}")
    elif kind == "regime_lab":
        job["result"] = data.get("result")
        if status == "done" and db is not None:
            try:
                await rlab.persist_worker_result(db, job_id, job)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"local regime_lab persist failed: {e}")
    else:  # Daten-Jobs
        if data.get("summary") is not None:
            job["summary"] = data["summary"]
    logger.info(f"local_exec: job {job_id} ({kind}) finished with status {status}")


# ---------------- Watchdog (Geister-Job-Schutz) ----------------
def _mark_error(job: Dict, msg: str):
    job["status"] = "error"
    job["error"] = msg
    job["phase"] = "Fehler"


def _finalize_cancel(jid: str, job: Dict):
    job["status"] = "cancelled"
    job["phase"] = "Abgebrochen"
    COMPUTE_QUEUE[:] = [i for i in COMPUTE_QUEUE if i["job_id"] != jid]
    if jid in DATA_QUEUE:
        DATA_QUEUE.remove(jid)
    LOCAL_JOBS.pop(jid, None)


def check_stale():
    for jid, meta in list(LOCAL_JOBS.items()):
        job = _get_job(jid, meta["kind"])
        if job is None or job.get("status") not in ("running", "queued"):
            LOCAL_JOBS.pop(jid, None)
            continue
        # ---- Abbruch: wartende Jobs sofort, laufende nach kurzer Frist hart ----
        if job.get("cancel"):
            if meta.get("state") == "queued":
                _finalize_cancel(jid, job)
                continue
            if not meta.get("cancel_at"):
                meta["cancel_at"] = _now()
            elif _now() - meta["cancel_at"] > CANCEL_GRACE:
                _finalize_cancel(jid, job)
                continue
        if meta.get("state") == "queued":
            if _now() - meta.get("enqueued_at", 0) > QUEUED_TIMEOUT and not worker_online():
                _mark_error(job, "Kein lokaler Worker verbunden – Job abgebrochen. "
                                 "Worker starten oder Cloud-Ausführung wählen.")
                COMPUTE_QUEUE[:] = [i for i in COMPUTE_QUEUE if i["job_id"] != jid]
                if jid in DATA_QUEUE:
                    DATA_QUEUE.remove(jid)
                LOCAL_JOBS.pop(jid, None)
        elif meta.get("state") == "claimed":
            w = WORKERS.get(meta.get("worker_id") or "")
            offline = not w or _now() - w.get("last_seen", 0) > WORKER_TIMEOUT
            if offline and _now() - meta.get("last_update", 0) > OFFLINE_JOB_TIMEOUT:
                _mark_error(job, "Verbindung zum lokalen Worker verloren – Job abgebrochen. "
                                 "Worker neu starten und Job erneut ausführen.")
                LOCAL_JOBS.pop(jid, None)
            elif _now() - meta.get("last_update", 0) > STALE_TIMEOUT:
                _mark_error(job, "Lokaler Worker antwortet nicht mehr (Verbindung verloren)")
                LOCAL_JOBS.pop(jid, None)


async def _watchdog_loop():
    while True:
        try:
            check_stale()
        except Exception as e:
            logger.warning(f"local_exec watchdog: {e}")
        await asyncio.sleep(5)


def ensure_watchdog():
    global _watchdog_task
    if _watchdog_task is None or _watchdog_task.done():
        try:
            _watchdog_task = asyncio.get_event_loop().create_task(_watchdog_loop())
        except RuntimeError:
            pass  # kein Event-Loop (z.B. in Tests ohne Server)


# ---------------- Status für die UI ----------------
def data_jobs_public() -> Dict:
    active = next((j for j in DATA_JOBS.values() if j["status"] == "running"), None)
    queued = [DATA_JOBS[j] for j in DATA_QUEUE if j in DATA_JOBS]
    recent = sorted([j for j in DATA_JOBS.values()
                     if j["status"] in ("done", "error", "cancelled")],
                    key=lambda x: x["created_at"], reverse=True)[:5]
    return {"active": active, "queued": queued, "recent": recent}


def queue_public() -> List[Dict]:
    return [{"job_id": i["job_id"], "kind": i["kind"]} for i in COMPUTE_QUEUE]
