"""Zentrale Test-Konfiguration: lädt backend/.env + REACT_APP_BACKEND_URL,
damit die E2E-Regressionstests in jeder Umgebung (lokal, CI, Render) laufen,
ohne dass Zugangsdaten hartkodiert werden müssen."""
import os
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent
_ROOT = _BACKEND_DIR.parent


def _load_env_file(path: Path):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


_load_env_file(_BACKEND_DIR / ".env")
_load_env_file(_ROOT / "frontend" / ".env")
