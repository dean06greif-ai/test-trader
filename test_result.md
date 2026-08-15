#====================================================================================================
# START - Testing Protocol - DO NOT EDIT OR REMOVE THIS SECTION
#====================================================================================================

# THIS SECTION CONTAINS CRITICAL TESTING INSTRUCTIONS FOR BOTH AGENTS
# BOTH MAIN_AGENT AND TESTING_AGENT MUST PRESERVE THIS ENTIRE BLOCK

# Communication Protocol:
# If the `testing_agent` is available, main agent should delegate all testing tasks to it.
#
# You have access to a file called `test_result.md`. This file contains the complete testing state
# and history, and is the primary means of communication between main and the testing agent.
#
# Main and testing agents must follow this exact format to maintain testing data. 
# The testing data must be entered in yaml format Below is the data structure:
# 
## user_problem_statement: {problem_statement}
## backend:
##   - task: "Task name"
##     implemented: true
##     working: true  # or false or "NA"
##     file: "file_path.py"
##     stuck_count: 0
##     priority: "high"  # or "medium" or "low"
##     needs_retesting: false
##     status_history:
##         -working: true  # or false or "NA"
##         -agent: "main"  # or "testing" or "user"
##         -comment: "Detailed comment about status"
##
## frontend:
##   - task: "Task name"
##     implemented: true
##     working: true  # or false or "NA"
##     file: "file_path.js"
##     stuck_count: 0
##     priority: "high"  # or "medium" or "low"
##     needs_retesting: false
##     status_history:
##         -working: true  # or false or "NA"
##         -agent: "main"  # or "testing" or "user"
##         -comment: "Detailed comment about status"
##
## metadata:
##   created_by: "main_agent"
##   version: "1.0"
##   test_sequence: 0
##   run_ui: false
##
## test_plan:
##   current_focus:
##     - "Task name 1"
##     - "Task name 2"
##   stuck_tasks:
##     - "Task name with persistent issues"
##   test_all: false
##   test_priority: "high_first"  # or "sequential" or "stuck_first"
##
## agent_communication:
##     -agent: "main"  # or "testing" or "user"
##     -message: "Communication message between agents"

# Protocol Guidelines for Main agent
#
# 1. Update Test Result File Before Testing:
#    - Main agent must always update the `test_result.md` file before calling the testing agent
#    - Add implementation details to the status_history
#    - Set `needs_retesting` to true for tasks that need testing
#    - Update the `test_plan` section to guide testing priorities
#    - Add a message to `agent_communication` explaining what you've done
#
# 2. Incorporate User Feedback:
#    - When a user provides feedback that something is or isn't working, add this information to the relevant task's status_history
#    - Update the working status based on user feedback
#    - If a user reports an issue with a task that was marked as working, increment the stuck_count
#    - Whenever user reports issue in the app, if we have testing agent and task_result.md file so find the appropriate task for that and append in status_history of that task to contain the user concern and problem as well 
#
# 3. Track Stuck Tasks:
#    - Monitor which tasks have high stuck_count values or where you are fixing same issue again and again, analyze that when you read task_result.md
#    - For persistent issues, use websearch tool to find solutions
#    - Pay special attention to tasks in the stuck_tasks list
#    - When you fix an issue with a stuck task, don't reset the stuck_count until the testing agent confirms it's working
#
# 4. Provide Context to Testing Agent:
#    - When calling the testing agent, provide clear instructions about:
#      - Which tasks need testing (reference the test_plan)
#      - Any authentication details or configuration needed
#      - Specific test scenarios to focus on
#      - Any known issues or edge cases to verify
#
# 5. Call the testing agent with specific instructions referring to test_result.md
#
# IMPORTANT: Main agent must ALWAYS update test_result.md BEFORE calling the testing agent, as it relies on this file to understand what to test next.

#====================================================================================================
# END - Testing Protocol - DO NOT EDIT OR REMOVE THIS SECTION
#====================================================================================================



#====================================================================================================
# Testing Data - Main Agent and testing sub agent both should log testing data below this section
#====================================================================================================
user_problem_statement: >
  Laufender ML-Umbau (Phase 0) einer produktiven Daytrading-Website (Render).
  Fix 0.4: prompt_version (Inhalts-Hash MasterPrompt + Hash Analyse-Systemprompt)
  an jede ai_decision binden, damit ML-Daten nach Prompt-Stand segmentierbar sind.

backend:
  - task: "Fix 0.4: prompt_version an jeder ai_decision"
    implemented: true
    working: true
    file: "backend/services/ai_engine.py, backend/services/ai_master_prompt.py"
    stuck_count: 0
    priority: "high"
    needs_retesting: false
    status_history:
      - working: "NA"
        agent: "main"
        comment: >
          MasterPromptStore.version_hash() (sha256 ueber text+lesson_policy+rules, 10 Zeichen,
          revert-sicher) neu; ai_engine: ANALYSIS_PROMPT_HASHES (lean/full) + prompt_version_info();
          run_analysis schreibt prompt_version-Dict (analysis, variant, master, master_v, combined)
          in jedes Decision-Doc. Isolierter Test /app/tests/test_fix_0_4_prompt_version.py: 3 PASS
          (Hash-Determinismus+Revert, Struktur lean/full, Integration via run_analysis mit gemocktem
          LLM gegen lokale Mongo). Regression: pytest -k "master_prompt or prompt" 49 passed;
          test_ai_team_api/regression 5 Fails identisch mit unveraendertem Stand (git stash
          verifiziert, pre-existing/umgebungsbedingt). Backend-Neustart sauber, /api/ai/status 200.
      - working: true
        agent: "testing"
        comment: >
          ALLE TESTS BESTANDEN. Unit-Tests: (1) version_hash deterministisch, 10 Zeichen, 
          aendert sich bei Text-/Rules-Aenderung, Revert liefert Original-Hash (Hash: 54f7aa0082). 
          (2) prompt_version_info("lean") vs ("full"): unterschiedliche analysis-Hashes 
          (lean: 04096d537b, full unterschiedlich), korrektes combined-Format 
          (lean-04096d537b+54f7aa0082), master == master_prompt.version_hash(), 
          master_v == master_prompt.version (10). (3) Integration: run_analysis mit gemocktem 
          LLM schreibt ai_decision-Doc mit korrektem prompt_version-Feld (variant, analysis, 
          master, master_v, combined) in lokale Mongo. Regression-Tests: /api/ai/status 
          liefert 200 mit plausiblem JSON (enabled: false, provider: gemini, model: 
          gemini-3.5-flash). /api/ai/master-prompt (GET) liefert 200 mit vollstaendigem 
          MasterPrompt (version: 10, text, rules, lesson_policy). Keine Fehler in Backend-Logs. 
          Fix 0.4 funktioniert korrekt und vollstaendig.

frontend: []

metadata:
  created_by: "main_agent"
  version: "1.0"
  test_sequence: 15

test_plan:
  current_focus:
    - "Fix 0.5: Ergebnis-Wahrheit vereinheitlicht (realized_pnl-Vorzeichen kanonisch)"
    - "ai_rewards-Fix (RCA Prod leer): lueckenfuellender Backfill + Entry-Regime + Endpoint"
  stuck_tasks: []
  test_all: false
  test_priority: "high_first"

agent_communication:
  - agent: "main"
    message: >
      Bitte NUR Fix 0.4 testen (Backend). Kern: services/ai_master_prompt.py version_hash(),
      services/ai_engine.py prompt_version_info() + Wiring in run_analysis (~Z.1695).
      WICHTIG: KI Trader NICHT aktivieren, keine echten LLM-Analyse-Laeufe noetig -
      Integrationspruefung wie in /app/tests/test_fix_0_4_prompt_version.py (LLM mocken).
      PROD_MONGO_URL in backend/.env ist NUR-LESEND (nie schreiben!), Dev-DB = lokale MONGO_URL.
      Login-Daten in /app/memory/test_credentials.md. Pre-existing Test-Fails in
      test_ai_team_api/test_ai_team_regression/test_regime_engine NICHT als Regression werten.
  - agent: "testing"
    message: >
      Fix 0.4 vollstaendig getestet und BESTANDEN. Alle Unit-Tests (version_hash, 
      prompt_version_info, Integration mit gemocktem LLM) laufen gruen. Regression-Tests 
      (/api/ai/status, /api/ai/master-prompt) funktionieren korrekt. Keine Fehler gefunden. 
      Implementation ist korrekt: MasterPromptStore.version_hash() generiert inhaltsbasierten 
      10-Zeichen-Hash (revert-sicher), prompt_version_info() liefert korrektes Dict mit 
      analysis/variant/master/master_v/combined, run_analysis schreibt prompt_version an 
      jede ai_decision (Zeile 1740 in ai_engine.py). Backend laeuft stabil, keine Logs-Fehler. 
      Task kann als erfolgreich abgeschlossen markiert werden.

  - task: "Fix 0.5: Ergebnis-Wahrheit vereinheitlicht (realized_pnl-Vorzeichen kanonisch)"
    implemented: true
    working: true
    file: "backend/services/bitunix_trade.py (_after_close), backend/core/pipeline.py (evaluate_open_signals), backend/services/ai_learning.py (sync_outcomes), backend/scripts/migrate_0_5_result_truth.py"
    stuck_count: 0
    priority: "high"
    needs_retesting: false
    status_history:
      - working: "NA"
        agent: "main"
        comment: >
          _after_close schreibt Trade-Ergebnis (Vorzeichen realized_pnl inkl. Fees) kanonisch ans
          Signal (result_source=trade_pnl, trade_id); evaluate_open_signals ueberschreibt
          trade-gelabelte Signale NICHT mehr (Filter result_source!=trade_pnl, labelt Rest
          tp1_touch); sync_outcomes: Trade-Branch setzt outcome kanonisch, tp1-Branch stuft
          trade_pnl-Outcomes nie zurueck. Migrationsskript mit Dry-Run (Default) + --apply
          (verweigert HART gegen PROD_MONGO_URL); Prod-Dry-Run gelaufen: 96 Signale wuerden
          kanonisiert (Flips: 15 win->loss, 6 loss->win, 27 unlabeled->Label), 28 Decisions.
          Isolierter Test /app/tests/test_fix_0_5_result_truth.py: 5 PASS.
      - working: true
        agent: "testing"
        comment: >
          ALLE 5 TESTS BESTANDEN. Referenztest test_fix_0_5_result_truth.py: (1) _after_close 
          schreibt Trade-Ergebnis kanonisch ans Signal (win/tp1 -> loss/trade_pnl bei negativem 
          realized_pnl). (2) evaluate_open_signals respektiert trade_pnl-Labels (Filter 
          result_source!=trade_pnl), labelt nur Rest als tp1_touch. (3) sync_outcomes: 
          trade_pnl-Outcomes gewinnen immer, tp1-Branch stuft nie zurueck. (4) Migrationsskript 
          Dry-Run schreibt nichts, --apply migriert korrekt auf lokaler DB. (5) --prod --apply 
          wird HART verweigert (Fehlermeldung: "ABBRUCH: --prod zusammen mit --apply ist verboten"). 
          Migrationsskript Dry-Run auf lokaler DB erfolgreich (1 Signal schon kanonisch). 
          Keine Fehler in Backend-Logs. Fix 0.5 funktioniert vollstaendig und korrekt.
  - task: "ai_rewards-Fix (RCA Prod leer): lueckenfuellender Backfill + Entry-Regime + Endpoint"
    implemented: true
    working: true
    file: "backend/services/ai_rewards.py, backend/routers/ai.py"
    stuck_count: 0
    priority: "high"
    needs_retesting: false
    status_history:
      - working: "NA"
        agent: "main"
        comment: >
          RCA: In Prod wurde am 13.08. 07:03 UTC DELETE /api/ai/rewards ausgefuehrt (cleared_at
          gesetzt) -> Auto-Backfill dauerhaft aus; alle 28 KI-Trades schlossen davor; Hook selbst
          funktioniert (in Dev bewiesen). Fix: backfill_missing() fuellt Luecken idempotent
          (nur Trades nach cleared_at; include_cleared=True hebt Sperre auf), ensure_backfill =
          rate-limitierter Wrapper; _regime_for prio: entry_market_snapshot (Fix 0.2) > Snapshot
          <= closed_at > Live-Observer (P1 Tech-Debt geloest). Neuer Admin-Endpoint
          POST /api/ai/rewards/backfill?include_cleared=true (401 ohne Auth verifiziert).
          Isolierter Test /app/tests/test_fix_rewards_backfill.py: 4 PASS.
          Hinweis: tests/test_iter3_ai_improvements.py::test_ai_rewards_seed_data ist
          daten-abhaengig (erwartet >=5 geseedete Trades, frische Dev-DB hat 1) - failt auch
          mit altem Code, KEINE Regression.
      - working: true
        agent: "testing"
        comment: >
          ALLE 4 TESTS BESTANDEN. Referenztest test_fix_rewards_backfill.py: (1) backfill_missing 
          fuellt Luecken idempotent (nur Trades ohne Reward-Eintrag). (2) cleared_at wird 
          respektiert (alte Trades vor cleared_at bleiben unbewertet). (3) include_cleared=True 
          hebt Sperre auf und bewertet auch historische Trades. (4) Regime kommt korrekt vom 
          Entry-Snapshot (trend_up), nicht vom Live-Observer. API-Tests: GET /api/ai/rewards 
          liefert 200 mit allen erforderlichen Keys (history: 1 item, by_regime: 1 item, 
          summary mit trades/total/avg/trend/days). POST /api/ai/rewards/backfill ohne Auth 
          liefert korrekt 401. POST /api/ai/rewards/backfill mit Admin-Auth (Login via 
          /api/auth/login) liefert 200 mit {"status":"success","rewarded":0,"include_cleared":false} 
          (idempotent, da bereits bewertet). Regression: GET /api/ai/status liefert 200. 
          Pytest-Suite: 42 passed, 1 failed (test_ai_rewards_seed_data - bekannter 
          daten-abhaengiger Fail, KEINE Regression). Keine Fehler in Backend-Logs. 
          ai_rewards-Fix funktioniert vollstaendig und korrekt.

agent_communication:
  - agent: "main"
    message: >
      Bitte Fix 0.5 + ai_rewards-Fix testen (nur Backend). Referenz-Tests (beide gruen):
      python /app/tests/test_fix_0_5_result_truth.py und python /app/tests/test_fix_rewards_backfill.py.
      Zusaetzlich API: GET /api/ai/rewards (200, keys history/by_regime/summary),
      POST /api/ai/rewards/backfill ohne Auth -> 401, mit Admin-Login (Credentials in
      /app/memory/test_credentials.md, Login via POST /api/auth/login o.ae. - Route pruefen) -> 200.
      LEITPLANKEN: PROD_MONGO_URL NIE beschreiben (nur lesen); KI Trader nicht aktivieren;
      Migrationsskript nur mit Default-Dry-Run oder --apply auf LOKALE DB ausfuehren.
      Pre-existing/daten-abhaengige Fails (test_ai_team_api, test_ai_team_regression,
      test_regime_engine, test_ai_lab*, test_iter_ai_supervisor_auto*, test_iter3 seed_data,
      test_ai_trader_iter10 learn_run) NICHT als Regression werten.
  - agent: "testing"
    message: >
      BEIDE FIXES VOLLSTAENDIG GETESTET UND BESTANDEN. Fix 0.5 (Ergebnis-Wahrheit): Alle 5 
      Referenztests PASS, Migrationsskript funktioniert korrekt (Dry-Run default, --prod --apply 
      wird hart verweigert). ai_rewards-Fix: Alle 4 Referenztests PASS, alle API-Endpoints 
      funktionieren korrekt (GET /api/ai/rewards: 200 mit history/by_regime/summary; 
      POST /api/ai/rewards/backfill: 401 ohne Auth, 200 mit Admin-Auth). Regression-Tests: 
      /api/ai/status liefert 200, pytest-Suite 42 passed (1 bekannter daten-abhaengiger Fail), 
      Backend-Logs sauber. Keine kritischen Fehler gefunden. Beide Fixes sind produktionsreif.

