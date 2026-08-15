"""Regressionstests: Worker-Paket muss IMMER vollständig ausgeliefert werden.

Hintergrund: local_worker/ (worker.py, requirements.txt, README.md) fehlte
mehrfach im Repo -> Download-Endpoint lieferte 500 "Worker-Paket unvollständig".
Diese Tests schlagen sofort fehl, wenn die Dateien erneut verschwinden.
"""
import io
import re
import zipfile
from pathlib import Path

import requests

BASE = "http://localhost:8001"
ROOT = Path(__file__).resolve().parents[2]
WORKER_DIR = ROOT / "local_worker"
REQUIRED = ("worker.py", "requirements.txt", "README.md")
EXTRAS = ("start_worker.bat", "start_worker.sh")


class TestWorkerPackageFiles:
    def test_local_worker_files_exist_on_disk(self):
        for name in REQUIRED + EXTRAS:
            p = WORKER_DIR / name
            assert p.is_file(), f"{p} fehlt – local_worker/ muss im Repo committet sein!"
            assert p.stat().st_size > 30, f"{p} ist (fast) leer"

    def test_local_worker_not_gitignored(self):
        gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
        assert "!local_worker/" in gi, \
            ".gitignore-Schutz für local_worker/ fehlt (Whitelist-Eintrag)"

    def test_worker_version_matches_server_requirement(self):
        src = (WORKER_DIR / "worker.py").read_text(encoding="utf-8")
        m = re.search(r'VERSION\s*=\s*"([\d.]+)"', src)
        assert m, "VERSION-Konstante in worker.py fehlt"
        r = requests.get(f"{BASE}/api/localworker/status", timeout=10)
        assert r.status_code == 200
        assert m.group(1) == r.json()["required_version"], \
            "worker.py VERSION != REQUIRED_WORKER_VERSION des Servers"


class TestWorkerPackageEndpoint:
    def test_manifest_complete(self):
        r = requests.get(f"{BASE}/api/localworker/package/manifest", timeout=15)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["missing"] == [], f"Im Paket fehlen: {d['missing']}"
        assert d["complete"] is True
        for name in REQUIRED:
            assert name in d["worker_files"]
        for mod in ("core", "services", "strategies"):
            assert d["modules"].get(mod, 0) > 0

    def test_package_zip_contains_required_files(self):
        r = requests.get(f"{BASE}/api/localworker/package", timeout=60)
        assert r.status_code == 200, r.text
        assert r.headers["content-type"] == "application/zip"
        z = zipfile.ZipFile(io.BytesIO(r.content))
        names = set(z.namelist())
        for name in REQUIRED:
            assert name in names, f"{name} fehlt im ZIP"
        assert "services/backtester.py" in names
        assert "services/optimizer.py" in names
        assert "services/deep_explore.py" in names
        assert "strategies/registry.py" in names
        assert "core/config.py" in names
        src = z.read("worker.py").decode("utf-8")
        req = requests.get(f"{BASE}/api/localworker/status", timeout=10).json()
        assert f'VERSION = "{req["required_version"]}"' in src
