"""Regressionstests: Lokale Ausführung (Worker-Queue, Daten-Verwaltung, Endpoints).

Läuft gegen den laufenden Backend-Server (localhost:8001). Simuliert einen
lokalen Worker über die /api/worker/*-Endpoints und prüft, dass Ergebnisse
über die BESTEHENDEN Backtest-/Optimizer-Endpoints ankommen.
"""
import gzip
import json
import time
import uuid

import os
import requests

BASE = "http://localhost:8001"


def _token():
    r = requests.post(f"{BASE}/api/auth/login",
                      json={"username": os.environ.get("ADMIN_USER", "Admin"), "password": os.environ.get("ADMIN_PASSWORD", "admin")}, timeout=10)
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _hdr():
    return {"Authorization": f"Bearer {_token()}"}


def _worker_token(hdr):
    r = requests.get(f"{BASE}/api/localworker/token", headers=hdr, timeout=10)
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _poll(wtoken, worker_id, want_compute=True, want_data=True):
    r = requests.post(f"{BASE}/api/worker/poll", headers={"X-Worker-Token": wtoken},
                      json={"worker_id": worker_id, "name": "PytestWorker",
                            "version": "test", "resources": {"cores": 4},
                            "gpu": {"available": False}, "data": {"symbols": []},
                            "running_jobs": [], "want_compute": want_compute,
                            "want_data": want_data}, timeout=10)
    assert r.status_code == 200, r.text
    return r.json()


class TestLocalWorkerEndpoints:
    def test_status_endpoint_shape(self):
        r = requests.get(f"{BASE}/api/localworker/status", timeout=10)
        assert r.status_code == 200
        d = r.json()
        for key in ("online", "workers", "queue", "data_jobs", "settings"):
            assert key in d

    def test_worker_endpoints_require_token(self):
        r = requests.post(f"{BASE}/api/worker/poll", json={"worker_id": "x"}, timeout=10)
        assert r.status_code == 401
        r = requests.post(f"{BASE}/api/worker/poll", json={"worker_id": "x"},
                          headers={"X-Worker-Token": "falsch"}, timeout=10)
        assert r.status_code == 401

    def test_settings_write_requires_admin_and_roundtrip(self):
        r = requests.post(f"{BASE}/api/localworker/settings",
                          json={"ram_limit_mb": 2048}, timeout=10)
        assert r.status_code == 401
        hdr = _hdr()
        r = requests.post(f"{BASE}/api/localworker/settings", headers=hdr,
                          json={"ram_limit_mb": 2048, "cpu_cores": 4,
                                "max_parallel_jobs": 2}, timeout=10)
        assert r.status_code == 200, r.text
        s = r.json()["settings"]
        assert s["ram_limit_mb"] == 2048 and s["cpu_cores"] == 4
        r = requests.get(f"{BASE}/api/localworker/settings", timeout=10)
        assert r.json()["settings"]["ram_limit_mb"] == 2048

    def test_token_requires_admin(self):
        r = requests.get(f"{BASE}/api/localworker/token", timeout=10)
        assert r.status_code == 401

    def test_package_zip(self):
        r = requests.get(f"{BASE}/api/localworker/package", timeout=30)
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/zip"
        assert len(r.content) > 10000

    def test_local_run_without_worker_rejected(self):
        """Ohne verbundenen Worker muss ein lokaler Start sauber abgelehnt werden
        (eigene worker_id, die nie gepollt hat -> offline)."""
        hdr = _hdr()
        # sicherstellen, dass kein Worker als online gilt: Status lesen
        st = requests.get(f"{BASE}/api/localworker/status", timeout=10).json()
        if st["online"]:
            return  # echter Worker verbunden -> Test nicht anwendbar
        r = requests.post(f"{BASE}/api/backtest/run", headers=hdr, timeout=10,
                          json={"strategy_ids": ["rsi_only"], "symbols": ["BTCUSDT"],
                                "days": 1, "execution": "local"})
        assert r.status_code == 503
        r = requests.post(f"{BASE}/api/optimizer/run", headers=hdr, timeout=10,
                          json={"mode": "params", "strategy_id": "rsi_only",
                                "symbols": ["BTCUSDT"], "days": 1, "execution": "local"})
        assert r.status_code == 503


class TestLocalJobRoundtrip:
    """Kompletter Roundtrip: Job einreihen -> Worker claimt -> Progress -> Ergebnis
    -> Ergebnis über bestehende Endpoints sichtbar."""

    def test_backtest_roundtrip(self):
        hdr = _hdr()
        wtoken = _worker_token(hdr)
        wid = "pytest-" + uuid.uuid4().hex[:8]
        _poll(wtoken, wid)  # Worker online melden

        r = requests.post(f"{BASE}/api/backtest/run", headers=hdr, timeout=10,
                          json={"strategy_ids": ["rsi_only"], "symbols": ["BTCUSDT"],
                                "days": 1, "execution": "local"})
        if r.status_code == 409:  # anderer Lauf aktiv -> überspringen
            return
        assert r.status_code == 200, r.text
        job_id = r.json()["job_id"]
        assert r.json()["execution"] == "local"

        # Status: Job wartet/läuft und blockiert weitere Starts (wie Cloud)
        st = requests.get(f"{BASE}/api/backtest/status/{job_id}", timeout=10).json()
        assert st["status"] == "running"
        assert st.get("execution") == "local"

        # Worker claimt den Job
        job = None
        for _ in range(5):
            resp = _poll(wtoken, wid)
            if resp.get("job"):
                job = resp["job"]
                break
            time.sleep(0.3)
        assert job and job["job_id"] == job_id and job["kind"] == "backtest"
        args = job["payload"]["args"]
        assert args["strategy_ids"] == ["rsi_only"] and args["symbols"] == ["BTCUSDT"]
        assert "settings" in args and "cfg" in args

        # Fortschritt melden
        r = requests.post(f"{BASE}/api/worker/job/{job_id}/progress",
                          headers={"X-Worker-Token": wtoken},
                          json={"progress": 50, "phase": "Test-Phase"}, timeout=10)
        assert r.status_code == 200 and r.json()["cancel"] is False
        st = requests.get(f"{BASE}/api/backtest/status/{job_id}", timeout=10).json()
        assert st["progress"] == 50 and st["phase"] == "Test-Phase"

        # Ergebnis (gzip) melden – Struktur wie run_backtest
        rows = [{"strategy_id": "rsi_only", "strategy_name": "Test", "symbol": "BTCUSDT",
                 "timeframe": "1m", "side": "LONG", "opened": "2026-01-01T00:00:00+00:00",
                 "closed": "2026-01-01T01:00:00+00:00", "pnl": 5.0, "result": "win"}]
        result = {"days": 1, "per_pair": [], "per_strategy": [
            {"strategy_id": "rsi_only", "strategy_name": "Test", "trades": 1,
             "wins": 1, "losses": 0, "pnl": 5.0}], "best_per_symbol": {}}
        payload = gzip.compress(json.dumps({
            "kind": "backtest", "status": "done", "error": None,
            "result": result, "export_trades": rows}).encode())
        r = requests.post(f"{BASE}/api/worker/job/{job_id}/result",
                          headers={"X-Worker-Token": wtoken,
                                   "Content-Type": "application/json",
                                   "Content-Encoding": "gzip"},
                          data=payload, timeout=10)
        assert r.status_code == 200, r.text

        # Über BESTEHENDE Endpoints prüfen
        st = requests.get(f"{BASE}/api/backtest/status/{job_id}", timeout=10).json()
        assert st["status"] == "done" and st["progress"] == 100
        assert st["result"]["per_strategy"][0]["pnl"] == 5.0
        eq = requests.get(f"{BASE}/api/backtest/equity/{job_id}", timeout=10).json()
        assert len(eq["points"]) == 1 and eq["points"][0]["pnl"] == 5.0
        csv = requests.get(f"{BASE}/api/backtest/export/{job_id}?kind=trades", timeout=10)
        assert csv.status_code == 200 and "rsi_only" in csv.text
        res = requests.get(f"{BASE}/api/backtest/results?limit=3", timeout=10).json()
        assert any(x["id"] == job_id for x in res["results"])

    def test_optimizer_roundtrip_with_cancel(self):
        hdr = _hdr()
        wtoken = _worker_token(hdr)
        wid = "pytest-" + uuid.uuid4().hex[:8]
        _poll(wtoken, wid)

        r = requests.post(f"{BASE}/api/optimizer/run", headers=hdr, timeout=10,
                          json={"mode": "params", "strategy_id": "rsi_only",
                                "symbols": ["BTCUSDT"], "days": 1, "iterations": 5,
                                "execution": "local"})
        if r.status_code == 409:
            return
        assert r.status_code == 200, r.text
        job_id = r.json()["job_id"]

        job = None
        for _ in range(5):
            resp = _poll(wtoken, wid)
            if resp.get("job"):
                job = resp["job"]
                break
            time.sleep(0.3)
        assert job and job["kind"] == "optimizer"
        assert "custom_definitions" in job["payload"]

        # Abbrechen -> Progress-Antwort muss cancel=True liefern
        r = requests.post(f"{BASE}/api/optimizer/cancel/{job_id}", headers=hdr, timeout=10)
        assert r.status_code == 200
        r = requests.post(f"{BASE}/api/worker/job/{job_id}/progress",
                          headers={"X-Worker-Token": wtoken},
                          json={"progress": 10, "phase": "x"}, timeout=10)
        assert r.json()["cancel"] is True
        # Worker meldet cancelled
        r = requests.post(f"{BASE}/api/worker/job/{job_id}/result",
                          headers={"X-Worker-Token": wtoken},
                          json={"kind": "optimizer", "status": "cancelled",
                                "error": None, "result": None}, timeout=10)
        assert r.status_code == 200
        st = requests.get(f"{BASE}/api/optimizer/status/{job_id}", timeout=10).json()
        assert st["status"] == "cancelled"

    def test_data_job_roundtrip(self):
        hdr = _hdr()
        wtoken = _worker_token(hdr)
        wid = "pytest-" + uuid.uuid4().hex[:8]
        _poll(wtoken, wid)

        r = requests.post(f"{BASE}/api/localworker/data/download", headers=hdr,
                          json={"symbols": ["BTCUSDT"], "days": 7}, timeout=10)
        assert r.status_code == 200, r.text
        jid = r.json()["job"]["id"]

        job = None
        for _ in range(5):
            resp = _poll(wtoken, wid, want_compute=False)
            if resp.get("job"):
                job = resp["job"]
                break
            time.sleep(0.3)
        assert job and job["kind"] == "data_download"
        assert job["payload"]["symbols"] == ["BTCUSDT"] and job["payload"]["days"] == 7
        r = requests.post(f"{BASE}/api/worker/job/{jid}/result",
                          headers={"X-Worker-Token": wtoken},
                          json={"kind": "data_download", "status": "done",
                                "summary": {"symbols": ["BTCUSDT"]}}, timeout=10)
        assert r.status_code == 200
        d = requests.get(f"{BASE}/api/localworker/status", timeout=10).json()
        assert any(j["id"] == jid and j["status"] == "done"
                   for j in d["data_jobs"]["recent"])

    def test_data_endpoints_validate(self):
        hdr = _hdr()
        r = requests.post(f"{BASE}/api/localworker/data/download", headers=hdr,
                          json={"symbols": ["FAKECOIN"], "days": 7}, timeout=10)
        assert r.status_code == 400
        r = requests.post(f"{BASE}/api/localworker/data/download",
                          json={"symbols": ["BTCUSDT"]}, timeout=10)
        assert r.status_code == 401


class TestCloudRegression:
    """Cloud-Pfad unverändert: Start ohne execution-Feld läuft wie bisher."""

    def test_cloud_backtest_still_works(self):
        hdr = _hdr()
        r = requests.post(f"{BASE}/api/backtest/run", headers=hdr, timeout=15,
                          json={"strategy_ids": ["rsi_only"], "symbols": ["BTCUSDT"],
                                "days": 1})
        if r.status_code == 409:
            return
        assert r.status_code == 200, r.text
        job_id = r.json()["job_id"]
        for _ in range(60):
            st = requests.get(f"{BASE}/api/backtest/status/{job_id}", timeout=10).json()
            if st["status"] != "running":
                break
            time.sleep(2)
        assert st["status"] == "done", st.get("error")
        assert st.get("params", {}).get("execution", "cloud") == "cloud"
        assert st["result"]["per_strategy"]

    def test_existing_endpoints_unchanged(self):
        for path in ("/api/backtest/active", "/api/optimizer/active",
                     "/api/strategies", "/api/coins", "/api/system/ram"):
            r = requests.get(f"{BASE}{path}", timeout=15)
            assert r.status_code == 200, path
