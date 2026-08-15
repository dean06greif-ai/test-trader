#!/bin/bash
# Läuft die Job-startenden Test-Dateien NACHEINANDER (Datei für Datei),
# um 409-Kollisionen zwischen gleichzeitig gestarteten Backtest-/Optimizer-/
# Regime-Jobs zu vermeiden. Innerhalb einer Datei bleibt xdist (-n 2, loadscope).
#
# Nutzung: bash /app/backend/tests/run_suite_serial.sh [logdatei]
set -u
LOG="${1:-/tmp/pytest_serial.log}"
cd /app/backend
export REACT_APP_BACKEND_URL="${REACT_APP_BACKEND_URL:-$(grep REACT_APP_BACKEND_URL /app/frontend/.env | cut -d '=' -f2)}"

# Dateien, die schwere Jobs starten -> seriell
JOB_FILES=(
  tests/test_iter12_dynamic_optimizer.py
  tests/test_iter9_e2e.py
  tests/test_iter5_optimizer_equity.py
  tests/test_iter16_optimizer_pool_regression.py
  tests/test_iter17_new_assets.py
  tests/test_new_features.py
  tests/test_regime_deep_update.py
  tests/test_iter15_refactor_candles.py
)

: > "$LOG"
FAIL=0
for f in "${JOB_FILES[@]}"; do
  echo "==== $f ====" >> "$LOG"
  timeout 900 python -m pytest "$f" -q --tb=line -rf -p no:cacheprovider >> "$LOG" 2>&1 || FAIL=1
done

# Rest der Suite (ohne die seriellen Dateien) parallel
IGNORES=()
for f in "${JOB_FILES[@]}"; do IGNORES+=(--ignore="$f"); done
echo "==== restliche Suite ====" >> "$LOG"
timeout 1200 python -m pytest tests/ -q --tb=line -rf -p no:cacheprovider "${IGNORES[@]}" >> "$LOG" 2>&1 || FAIL=1

echo "==== ZUSAMMENFASSUNG ====" >> "$LOG"
grep -E "passed|failed" "$LOG" | tail -20 >> "$LOG"
exit $FAIL
