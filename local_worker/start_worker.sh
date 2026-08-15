#!/usr/bin/env bash
# Lokaler Worker – Start (Linux/macOS)
cd "$(dirname "$0")"
command -v python3 >/dev/null || { echo "Python 3 nicht gefunden"; exit 1; }
python3 -m pip install -r requirements.txt --quiet
python3 worker.py "$@"
