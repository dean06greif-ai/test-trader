"""Lokaler Worker v1.9.0 – führt Rechen- und Daten-Jobs der Website lokal aus.

Verbindet sich per Outbound-Polling (alle ~2s) mit dem Server, claimt Jobs
und rechnet sie mit EXAKT denselben Modulen wie die Cloud (services/*,
strategies/*, core/*, im ZIP enthalten). Ergebnisse werden gzip-komprimiert
zurückgeladen; bei Verbindungsabbruch rechnet der Worker weiter und lädt das
Ergebnis nach der Wiederverbindung hoch.

Neu in 1.9.0: Endlos-Suche (Optimizer mode="explore") inkl. sanftem Stop
("stop"-Flag in der Progress-Antwort: Suche beenden, Bestes behalten).
"""
import argparse
import asyncio
import gzip
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path

VERSION = "1.9.0"
SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "worker_config.json"
POLL_INTERVAL = 2.0

sys.path.insert(0, str(SCRIPT_DIR))


# ---------------- Konfiguration ----------------
def load_config():
    cfg = {}
    if CONFIG_PATH.is_file():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cfg = {}
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default=None)
    ap.add_argument("--token", default=None)
    ap.add_argument("--name", default=None)
    ap.add_argument("--data-dir", default=None)
    args, _ = ap.parse_known_args()
    server = (args.server or os.environ.get("WORKER_SERVER_URL")
              or cfg.get("server_url") or "")
    token = (args.token or os.environ.get("WORKER_TOKEN")
             or cfg.get("token") or "")
    if not server:
        server = input("Server-URL der Website (z.B. https://meine-app.example): ").strip()
    if not token:
        token = input("Worker-Token (Website: Ausführung → Lokal → ⚙ Verwalten): ").strip()
    cfg["server_url"] = server.rstrip("/")
    cfg["token"] = token
    cfg["name"] = args.name or cfg.get("name") or os.environ.get(
        "COMPUTERNAME") or os.uname().nodename if hasattr(os, "uname") else "Worker"
    cfg.setdefault("worker_id", "w" + uuid.uuid4().hex[:11])
    cfg["data_dir"] = args.data_dir or cfg.get("data_dir") or str(SCRIPT_DIR / "worker_data")
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except OSError:
        pass
    return cfg


CONFIG = load_config()
os.environ.setdefault("CANDLE_CACHE_DISK", "1")
os.environ.setdefault("CANDLE_CACHE_DIR", CONFIG["data_dir"])
os.makedirs(CONFIG["data_dir"], exist_ok=True)

import requests  # noqa: E402


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def api(path):
    return f"{CONFIG['server_url']}{path}"


def hdrs():
    return {"X-Worker-Token": CONFIG["token"]}


# ---------------- Job-Verwaltung ----------------
RUNNING = {}          # job_id -> {"kind","thread","job"(dict im JOBS-Store)}
SETTINGS = {}
_last_auto_update = 0.0


def apply_settings(settings):
    global SETTINGS
    SETTINGS = settings or {}
    cores = int(SETTINGS.get("cpu_cores") or 0)
    os.environ["SIM_WORKERS"] = str(cores)
    os.environ["USE_GPU"] = "1" if SETTINGS.get("use_gpu") else "0"
    ram_mb = int(SETTINGS.get("ram_limit_mb") or 4096)
    os.environ["CANDLE_CACHE_MAX_CANDLES"] = str(max(ram_mb, 512) * 1024 * 1024 // 64)


def make_registry(custom_definitions):
    from strategies.registry import StrategyRegistry
    reg = StrategyRegistry()
    try:
        reg.load_custom(custom_definitions or [])
    except Exception as e:  # noqa: BLE001
        log(f"Custom-Strategien laden fehlgeschlagen: {e}")
    return reg


def new_job_dict(kind_store, job_id, params=None):
    """Job-Eintrag im lokalen JOBS-Store anlegen (gleiche Form wie der Server)."""
    from datetime import datetime, timezone
    d = {"id": job_id, "status": "running", "progress": 0, "phase": "Startet",
         "params": params or {}, "best": None, "cancel": False,
         "created_at": datetime.now(timezone.utc).isoformat(),
         "result": None, "error": None}
    kind_store[job_id] = d
    return d


class JobCancelledLocal(Exception):
    pass


def progress_reporter(job_id, jobd, stop_evt):
    """Meldet Fortschritt alle 2s; Antwort steuert Abbruch/sanften Stop."""
    last = None
    while not stop_evt.is_set():
        try:
            body = {"progress": jobd.get("progress"), "phase": jobd.get("phase")}
            if jobd.get("best") is not None and jobd.get("best") != last:
                body["best"] = jobd["best"]
                last = jobd["best"]
            r = requests.post(api(f"/api/worker/job/{job_id}/progress"),
                              headers=hdrs(), json=body, timeout=10)
            if r.status_code == 200:
                resp = r.json()
                if resp.get("cancel"):
                    jobd["cancel"] = True
                if resp.get("stop"):
                    jobd["stop_explore"] = True
        except requests.RequestException:
            pass  # offline -> weiterrechnen, Ergebnis kommt später
        stop_evt.wait(2.0)


def upload_result(job_id, payload):
    raw = gzip.compress(json.dumps(payload, default=str).encode())
    for attempt in range(90):  # bis ~3 min Reconnect-Toleranz
        try:
            r = requests.post(api(f"/api/worker/job/{job_id}/result"),
                              headers={**hdrs(), "Content-Type": "application/json",
                                       "Content-Encoding": "gzip"},
                              data=raw, timeout=60)
            if r.status_code in (200, 404):
                return
        except requests.RequestException:
            pass
        time.sleep(2)
    log(f"Ergebnis-Upload für {job_id} endgültig fehlgeschlagen")


# ---------------- Rechen-Jobs ----------------
def run_backtest_job(job_id, payload):
    from services import backtester as bt
    args = payload.get("args") or {}
    reg = make_registry(payload.get("custom_definitions"))
    jobd = new_job_dict(bt.JOBS, job_id, args)
    stop_evt = threading.Event()
    rep = threading.Thread(target=progress_reporter, args=(job_id, jobd, stop_evt),
                           daemon=True)
    rep.start()
    try:
        asyncio.run(bt.run_backtest(
            job_id, args.get("strategy_ids") or [], args.get("symbols") or [],
            int(args.get("days") or 1), args.get("cfg") or {}, reg,
            args.get("settings") or {}, None, args.get("strategy_configs") or {},
            args.get("default_timeframe"), args.get("date_from"), args.get("date_to")))
    except Exception as e:  # noqa: BLE001
        jobd["status"] = "error"
        jobd["error"] = str(e)[:300]
    finally:
        stop_evt.set()
    upload_result(job_id, {
        "kind": "backtest", "status": jobd.get("status") or "error",
        "error": jobd.get("error"), "result": jobd.get("result"),
        "export_trades": jobd.get("export_trades") or []})


def run_optimizer_job(job_id, payload):
    from services import optimizer as opt
    args = payload.get("args") or {}
    body = args.get("body") or {}
    reg = make_registry(payload.get("custom_definitions"))
    jobd = new_job_dict(opt.JOBS, job_id, body)
    stop_evt = threading.Event()
    rep = threading.Thread(target=progress_reporter, args=(job_id, jobd, stop_evt),
                           daemon=True)
    rep.start()
    try:
        asyncio.run(opt.run_optimizer(job_id, body, reg,
                                      args.get("settings") or {},
                                      args.get("default_cfg") or {}, None))
    except Exception as e:  # noqa: BLE001
        jobd["status"] = "error"
        jobd["error"] = str(e)[:300]
    finally:
        stop_evt.set()
    upload_result(job_id, {
        "kind": "optimizer", "status": jobd.get("status") or "error",
        "error": jobd.get("error"), "result": jobd.get("result"),
        "best": jobd.get("best"),
        "export_trades": jobd.get("export_trades") or []})


def run_regime_job(job_id, payload):
    from services import regime_lab as rlab
    from services import regime_opt
    args = payload.get("args") or {}
    fn = args.get("fn")
    body = args.get("body") or {}
    settings = args.get("settings") or {}
    default_cfg = args.get("default_cfg") or {}
    reg = make_registry(payload.get("custom_definitions"))
    jobd = new_job_dict(rlab.JOBS, job_id, body)
    jobd["kind"] = fn
    stop_evt = threading.Event()
    rep = threading.Thread(target=progress_reporter, args=(job_id, jobd, stop_evt),
                           daemon=True)
    rep.start()
    try:
        if fn == "analysis":
            asyncio.run(rlab.run_analysis(job_id, body, None))
        elif fn == "calibrate":
            asyncio.run(rlab.run_calibration(job_id, body, None))
        elif fn == "regime_opt":
            asyncio.run(regime_opt.run_regime_optimizer(
                job_id, body, reg, settings, default_cfg, None))
        elif fn == "walkforward":
            asyncio.run(regime_opt.run_walkforward(
                job_id, body, reg, settings, default_cfg, None))
        else:
            raise RuntimeError(f"Unbekannter Regime-Lab-Job: {fn}")
    except Exception as e:  # noqa: BLE001
        jobd["status"] = "error"
        jobd["error"] = str(e)[:300]
    finally:
        stop_evt.set()
    upload_result(job_id, {
        "kind": "regime_lab", "status": jobd.get("status") or "error",
        "error": jobd.get("error"), "result": jobd.get("result")})


# ---------------- Daten-Jobs ----------------
async def _download_symbols(jobd, symbols, days):
    import aiohttp
    from services import candle_cache
    done = []
    async with aiohttp.ClientSession() as session:
        for i, sym in enumerate(symbols):
            if jobd.get("cancel"):
                raise JobCancelledLocal()
            jobd["phase"] = f"Lade {sym} ({days} Tage)..."
            jobd["progress"] = round(i / max(len(symbols), 1) * 100)
            candles = await candle_cache.get_candles(session, sym, days, job=jobd)
            await candle_cache.persist_symbol_async(sym)
            done.append({"symbol": sym, "candles": len(candles)})
    return done


def run_data_job(job_id, kind, params):
    from services import candle_cache
    jobd = {"id": job_id, "status": "running", "progress": 0,
            "phase": "Startet", "cancel": False}
    stop_evt = threading.Event()
    rep = threading.Thread(target=progress_reporter, args=(job_id, jobd, stop_evt),
                           daemon=True)
    rep.start()
    status, error, summary = "done", None, None
    try:
        if kind == "data_download":
            done = asyncio.run(_download_symbols(
                jobd, params.get("symbols") or [], int(params.get("days") or 30)))
            summary = {"symbols": [d["symbol"] for d in done], "detail": done}
        elif kind == "data_update":
            now_ms = int(time.time() * 1000)
            updated = []
            for meta in candle_cache.list_disk_symbols():
                sym = meta.get("symbol")
                dm = candle_cache.disk_meta(sym) or {}
                first = dm.get("first_ts") or now_ms
                days = max(int((now_ms - first) / 86400000) + 1, 2)
                asyncio.run(_download_symbols(jobd, [sym], days))
                updated.append(sym)
            summary = {"symbols": updated}
        elif kind == "data_delete":
            candle_cache.remove_symbol(params.get("symbol"))
            summary = {"symbol": params.get("symbol")}
        else:
            raise RuntimeError(f"Unbekannter Daten-Job: {kind}")
    except JobCancelledLocal:
        status = "cancelled"
    except Exception as e:  # noqa: BLE001
        status, error = "error", str(e)[:300]
    finally:
        stop_evt.set()
    upload_result(job_id, {"kind": kind, "status": status, "error": error,
                           "summary": summary})


# ---------------- Heartbeat / Poll-Loop ----------------
def resources():
    out = {"cores": os.cpu_count() or 1}
    try:
        import psutil
        out["ram_gb"] = round(psutil.virtual_memory().total / 1e9, 1)
        out["ram_free_gb"] = round(psutil.virtual_memory().available / 1e9, 1)
        out["cpu_pct"] = psutil.cpu_percent(interval=None)
    except Exception:  # noqa: BLE001
        pass
    return out


def gpu_info():
    try:
        from services import gpu_accel
        return gpu_accel.info()
    except Exception:  # noqa: BLE001
        return {"available": False}


def data_info():
    try:
        from services import candle_cache
        syms = candle_cache.list_disk_symbols()
        return {"symbols": [s.get("symbol") for s in syms], "detail": syms[:50]}
    except Exception:  # noqa: BLE001
        return {"symbols": []}


def cleanup_finished():
    for jid in list(RUNNING.keys()):
        if not RUNNING[jid]["thread"].is_alive():
            RUNNING.pop(jid, None)


def dispatch(job):
    jid, kind, payload = job["job_id"], job["kind"], job.get("payload") or {}
    if kind == "backtest":
        target, args = run_backtest_job, (jid, payload)
    elif kind == "optimizer":
        target, args = run_optimizer_job, (jid, payload)
    elif kind == "regime_lab":
        target, args = run_regime_job, (jid, payload)
    elif kind in ("data_download", "data_update", "data_delete"):
        target, args = run_data_job, (jid, kind, payload)
    else:
        log(f"Unbekannter Job-Typ: {kind}")
        upload_result(jid, {"kind": kind, "status": "error",
                            "error": f"Worker kennt Job-Typ '{kind}' nicht"})
        return
    t = threading.Thread(target=target, args=args, daemon=True)
    RUNNING[jid] = {"kind": kind, "thread": t}
    t.start()
    log(f"Job übernommen: {jid} ({kind})")


def find_job_dict(jid):
    for mod_name, attr in (("services.backtester", "JOBS"),
                           ("services.optimizer", "JOBS"),
                           ("services.regime_lab", "JOBS")):
        mod = sys.modules.get(mod_name)
        if mod is not None and jid in getattr(mod, attr, {}):
            return getattr(mod, attr)[jid]
    return None


def maybe_auto_update():
    global _last_auto_update
    if not SETTINGS.get("auto_update_enabled"):
        return
    minutes = int(SETTINGS.get("auto_update_minutes") or 60)
    if time.time() - _last_auto_update < minutes * 60 or RUNNING:
        return
    _last_auto_update = time.time()
    log("Auto-Update der Kerzendaten...")
    threading.Thread(target=run_data_job,
                     args=("auto-" + uuid.uuid4().hex[:8], "data_update", {}),
                     daemon=True).start()


def main():
    log(f"Lokaler Worker v{VERSION} · Server: {CONFIG['server_url']}")
    log(f"Daten-Ordner: {CONFIG['data_dir']}")
    while True:
        cleanup_finished()
        max_jobs = int(SETTINGS.get("max_parallel_jobs") or 1)
        compute_running = sum(1 for r in RUNNING.values()
                              if not r["kind"].startswith("data_"))
        try:
            r = requests.post(api("/api/worker/poll"), headers=hdrs(), json={
                "worker_id": CONFIG["worker_id"], "name": CONFIG["name"],
                "version": VERSION, "resources": resources(), "gpu": gpu_info(),
                "data": data_info(), "running_jobs": list(RUNNING.keys()),
                "sim_workers": int(os.environ.get("SIM_WORKERS") or 0) or (os.cpu_count() or 1),
                "want_compute": compute_running < max_jobs,
                "want_data": True,
            }, timeout=10)
            if r.status_code == 401:
                log("FEHLER: Worker-Token ungültig – neues Token auf der Website "
                    "holen und worker_config.json anpassen")
                time.sleep(10)
                continue
            r.raise_for_status()
            resp = r.json()
            apply_settings(resp.get("settings"))
            for jid in resp.get("cancel_ids") or []:
                jd = find_job_dict(jid)
                if jd is not None:
                    jd["cancel"] = True
            if resp.get("job"):
                dispatch(resp["job"])
            maybe_auto_update()
        except requests.RequestException as e:
            log(f"Verbindung zum Server fehlgeschlagen: {e}")
            time.sleep(5)
        except KeyboardInterrupt:
            log("Beendet")
            return
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Beendet")
