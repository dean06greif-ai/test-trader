# PRD – Crypto Scanner / Daytrading-Website (extern deployt auf Render)

## Original-Problemstellung
Bestehende, produktiv laufende Daytrading-Website (GitHub: AntonHeinrich05/ml-implementation-ML_REBUILD_STATUS.md, Branch 0.5) soll verbessert werden, ohne Struktur/Workflows zu brechen (Render-Deploy muss weiter funktionieren). Grundsatz: sauber, modular, rückwärtskompatibel, mit Regressionstests.

## Architektur
- FastAPI-Backend (`/app/backend`, Port 8001, Router in `routers/`, Services in `services/`, Kern in `core/`)
- React-Frontend (`/app/frontend`, CRA/craco, deutschsprachige UI)
- MongoDB (`MONGO_URL`/`DB_NAME` aus env), Supabase-Spiegel fürs KI-Gedächtnis, Telegram-Notify
- Externe Integrationen: Bitunix Futures (Live-Trading), diverse LLM-Provider (Cerebras, Groq, OpenRouter, Gemini, Mistral) mit Failover-Kette
- Bestehende pytest-Suite in `backend/tests/` (teilweise env-abhängig: Failures ohne MISTRAL/GITHUB/GEMINI-Keys sind vorbestehend)

## User-Persona
Einzelner Admin-Trader (deutsch), der die Seite parallel zu manuellem Bitunix-Trading nutzt; KI-Trader läuft autonom.

## Umgesetzt (13.06.2026, „0.6-Verbesserungen")
1. **Unbegrenzte Backup-Keys**: `services/ai_providers.py::_backup_env_names` scannt env dynamisch – `X_API_KEY_BACKUP`, `_BACKUP2`, `_BACKUP3`, … beliebig hoch, numerisch sortiert; gilt für alle Provider (Cerebras, OpenRouter, Groq, …).
2. **Watchdog**: An/Aus-Toggle + Verlauf/Trades-Löschen waren in 0.5 bereits in den Haupteinstellungen vorhanden; übernommene manuelle Bitunix-Positionen heißen jetzt klar **„Manuell (Bitunix)"** (`manual_trade: true`, `strategy_id` bleibt „external" für Rückwärtskompatibilität), inkl. einmaliger DB-Migration alter Trades beim Start; Default `manage_external=false` (Watchdog fasst manuelle Trades nicht an).
3. **PnL-Genauigkeit**: `core/utils.py::_enrich_trade` akzeptiert echte Bitunix-Positionsdaten; `routers/autotrade.py` holt via 10s-Cache (`_live_position_map`) den echten uPnL der Börse für offene Live-Trades (Fix für Gold-Bug: Scanner nutzte Yahoo GC=F statt Bitunix XAUUSDT). Feld `live_pnl_source` = „bitunix"/„scanner".
4. **UI-Cut-Fix**: `.tdc-pnl-pct`/`.tdc-pnl` mit `flex-shrink:0; white-space:nowrap` – Prozentwert in Klammern wird nie mehr abgeschnitten (Coin-Name bekommt Ellipsis).
5. **Glocke↔Blitz-Kopplung**: `StrategyTabs.js` – Glocke kann nur an sein, wenn Auto-Trade aktiv ist; Klick bei Blitz-aus zeigt Info-Toast „Glocke nur möglich, wenn Auto-Trade (Blitz) aktiv ist".
6. **KI-Trader**: neues Modul `services/session_levels.py` (Asia/London/NY Session-H/L + Umverteilungszonen via Volumen-Cluster); Snapshot enthält jetzt 5m-RSI (Headline-RSI = 5m statt 1m-„Gambling"), Session-Levels & Zonen; System-Prompts erweitert (Timeframe-Disziplin, Sweep-/Breakout-Trigger an Session-Levels/Zonen, Konfidenz-Kalibrierung 70–85 für A-Setups gegen Dauer-HOLD); Playbook sperrt schwache Setups weiterhin automatisch.
7. **Regressionstests**: `tests/test_improvements_0_6.py` (13 Tests) + bestehende Watchdog/Backup-Tests grün (44/44 in den Zieldateien).

## Umgesetzt (13.06.2026, Iteration 2)
1. **Strategie-Vergleich Reiter-Reihenfolge**: Mode-Tabs jetzt ALLE → LIVE → PAPER, Zeit-Tabs Gesamt → 30 Tage → 7 Tage (`StrategyComparison.js`).
2. **Watchdog raus aus dem Vergleich**: `strategy-comparison` filtert `strategy_id='external'`/`manual_trade` bereits in der Mongo-Query + doppelte Absicherung im Loop (auch alte 'Extern (Watchdog)'-Trades via `external_adopted`).
3. **RAM-Reduktion (512-MB-Render, ohne Funktionsverlust)**:
   - Mongo-Projektionen: `strategy_comparison` (nur 13 Felder statt kompletter Trade-Dokumente mit manage_log/KI-Feldern), `rebuild_performance` (200k-Signals-Query auf 6 Felder), `_aggregate_ai_stats` (5k-Signals auf 4 Felder) → große transienten RAM-Spitzen beseitigt.
   - Candle-Cache-RAM-Budget: Default 2M → 500k Kerzen (~24 MB), env-übersteuerbar via `CANDLE_CACHE_MAX_CANDLES`; ältere Symbole liegen als `.npy` auf Disk (Reload in ms, kein Leistungsverlust). Lokaler Worker setzt sein Budget weiterhin selbst nach echtem RAM.
4. **Tests**: `tests/test_comparison_and_ram_iter2.py` (4 Tests) + Testing-Agent-Verifikation (iteration_15.json, 100%).

## Umgesetzt (14.06.2026, Multi-Timeframe pro Regel)
Detailplan/Design: **/app/MULTI_TIMEFRAME_PLAN.md** (vom User gewünschte „Plan-Datei in git").
- Hybrid-Ansatz: Basis-TF pro Strategie bleibt, optionales `"timeframe"`-Override je Regel (Stufen 1m…1d; muss ≥ Basis-TF & Vielfaches sein, sonst 422). Default = Basis-TF → 100% rückwärtskompatibel.
- Kern: `fast_sim.FastSeries.htf()` + `_rule_cond_htf()` (letzte GESCHLOSSENE HTF-Kerze, kein Lookahead); Live-Scanner, Backtester, Optimizer, rule-preview nutzen denselben Codepfad → Parität garantiert. `aggregate_candles(..., base_ms=)` für Nicht-1m-Quellen gefixt; `"1d"` in TIMEFRAMES ergänzt.
- Scanner-`buffer_limit` berücksichtigt Regel-TFs (60 Kerzen des höchsten Regel-TF, Deckel 30 Tage/43200 wegen Render-RAM).
- Optimizer: Request-Option `rule_timeframes {enabled,min,max}` (Rahmen z.B. 1m–4h) für params-Modus (Suchraum-Keys `long1_tf`…, via `custom_params.rule_timeframe_space`/`apply_params`, `CustomStrategy.get_params` reicht `*_tf` durch) und Discovery/Combo/Deep/Explore (`build_candidates(..., tf_options)` erzeugt „@TF"-Kandidaten-Varianten). Wird nie erzwungen – Basis-TF bleibt immer Option.
- UI: TF-Dropdown pro Regel im StrategyBuilder (`rule-tf-{long|short}-{i}`, ungültige Stufen disabled), Optimizer-Chip „Regel-Timeframes optimieren" + von/bis (`opt-rule-tf-toggle/-min/-max`). KI-Strategie-Labor-Prompt erklärt das Feld (Filter ja, Cross-Trigger nein) → KI-Trader kann MTF selbst nutzen.
- Tests: `tests/test_rule_timeframes.py` (25 Unit-Tests) + `tests/test_rule_tf_api.py` (9 E2E, vom Testing-Agent) grün; Regressionssuites grün (einziger Fail: `TestOptimizerDynamic::test_per_regime_true` = 900s-Poll-Timeout auf langsamem Test-Pod, dynamic-Modus von Änderungen unberührt).

## Umgesetzt (14.06.2026, Iteration: QQQ-Fix / Chart-Signal-Overlay / KI-Range-Trading)
1. **QQQ „ORDER ABGEBROCHEN"-Spam behoben** (Root Cause: KI-Trade-Manager managte die manuell auf Bitunix eröffnete QQQ-Position; QQQ ist nicht per OpenAPI handelbar → Close/Adjust schlug bei jedem Review fehl → Telegram-Meldung): `ai_trade_manager.review()` schließt manuelle/externe Trades (manual_trade / external_adopted / strategy_id 'external') aus; `apply_action()` blockt sie hart; `bitunix_trade._notify_reject()` hat 30-min-Anti-Spam-Cooldown pro (Symbol+Seite+Grund). Watchdog war korrekt (manage_external=False).
2. **Chart-Signal-Overlay**: Klick auf die Signal-Box im SignalPanel zeigt im Chart Entry/SL/TP-Preislinien + Overlay mit allen Regeln inkl. Timeframe-Badge je Regel (`chart-signal-panel`, `chart-signal-rule-*`). Signale speichern dafür `rules_snapshot` + `strategy_timeframe` (scanner `_maybe_signal`); Regel-States enthalten `timeframe`. SignalPanel zeigt live `@tf`-Badges pro Regel.
3. **KI-Trader Range-Trading**: neues Modul `services/range_analysis.py` (Range-Erkennung mit Touch-Zählung + Wick-Rejections auf 15m/1h, rein/testbar) fließt als „Range-Check"-Zeile in den KI-Snapshot; beide System-Prompts erklären range_fade (Entry an Range-Grenze nach Wick-Rejection, SL dahinter, TP Mitte/Gegenseite). Setup-Typen range_fade/mean_reversion existierten bereits im Schema.
4. **„kein Edge"-Klärung (UI)**: Das Badge erscheint bei JEDER HOLD-Entscheidung (HOLD = bewusst kein Trade); Tooltip präzisiert, HOLD zeigt jetzt zusätzlich die Konfidenz.
- Tests: `tests/test_qqq_range_chart_fixes.py` (10) + `tests/test_qqq_review_query_regression.py` (Testing-Agent) grün; Watchdog-/Rule-Engine-Regressionen grün; UI e2e verifiziert (iteration_17.json, 100%).

## Env-Hinweise (lokale Preview vs. Render)
- Lokal: `MONGO_URL=mongodb://localhost:27017`, Admin/admin123; Bitunix-Key vom User maskiert geliefert → `trade_client.configured()==false` lokal (erwartet)
- Auf Render nutzt der User seine echte env (unverändert kompatibel)

## ML-Umbau Fortsetzung (14.08., 3. Handover)
- Phase 3 = Fix 0.6 ERLEDIGT (14.08.): Rebuild-on-Boot Candle-Backfill (services/boot_backfill.py, non-blocking, 22 Symbole × 14d in ~3 Min, 0 Fehler), Downtime-Signal-Labeling (P1-Tech-Debt geschlossen, trade_pnl-Vorrang), Audit-Log für Löschungen (core/audit.py, GET /api/audit-log, Anzeige im Clear-Modal). Tests: 9 Unit + 14 API + Frontend 100%.
- RCA Trade-Schwund 167→96: analytics/clear ("Verlauf löschen") am 13.08. für Zeitraum 11.–12.08., nur mit Admin-Login möglich → Kumpel sehr wahrscheinlich; kein Deploy-Nebeneffekt. Details in ML_REBUILD_STATUS.md.
- Repo 14.8-timeframe-Update-try nach /app übernommen (alle Fixes 0.1–0.5 + ai_rewards-Fix enthalten), .env selektiv (LLM-Keys, ADMIN, PROD_MONGO_URL nur lesend; Bitunix/Telegram/Supabase bewusst NICHT), App lauffähig verifiziert (Health, Admin-Login, Frontend, Prod-Lesezugriff).
- Phase 2 dokumentiert in /app/memory/ML_REBUILD_STATUS.md: Prod-Freigabe (Migration 0.5 → --apply → Reward-Backfill, Reihenfolge zwingend), RCA-Notiz (User hat nicht gelöscht, Ursache unbestätigt), Gate-Aktivierungskriterium (4 Wochen, ≥150 Entscheidungen, 3 Kriterien gleichzeitig).
- Auffälligkeit Prod (14.08.): auto_trades 167→96 (71 Trades weg), ai_rewards=3 (Hook zeichnet live auf).
- Nächste Phasen (je mit User-Freigabe): 3 = Fix 0.6 Rebuild-on-Boot Candle-Backfill, 4 = Paper-Datensammel-Modus (data_collection=true), 5 = Gate v1 Shadow + kontrafaktisches Logging.

## ML-Umbau Fortsetzung (14.08., 4. Handover)
- RCA "KI-Trader macht kaum Trades": Self-Tuning-Ratchet – die KI hatte per autonomy=auto
  ihre min_confidence 70→75→80→85 hochgeschraubt (Prompts kalibrieren A-Setups auf 70–85 →
  Schwelle 85 filtert fast alles). Dazu: analytics/clear (scope=strategy) am 13.08. löschte
  ALLE ai_trader-Signale/Trades vor dem 14.08. Details: ML_REBUILD_STATUS.md.
- Self-Tuning-Guard: einstellbare Autonomie-Spanne (tune_conf_min/max 55–75, tune_cooldown_max
  45); außerhalb wird jede KI-Engine-Änderung nur Vorschlag; Boot-Heilung setzt KI-gesetzte
  Out-of-Range-Werte einmalig zurück (heilt Prod nach Deploy: 85→75).
- Phase 4 Paper-Datensammel-Modus: collection_enabled/min_confidence 60/cooldown 30/
  max_same_direction 5/max_per_coin 2; Sammel-Trades immer Paper (auch auf AUS-Coins),
  data_collection=true + collection_reason am Trade/Signal, eigene Slots, kein Telegram,
  Badge "DATEN" in der Trade-Karte. Erwartung: ~30–60 Paper-Trades/Tag fürs ML.
- Tests: tests/test_phase4_collection_mode.py (7 PASS) + Regressionen grün.

## ML-Umbau Fortsetzung (14.06.2026, 5. Handover — Phase 5 Gate v1 Shadow)
- Neues Modul `services/ml_gate.py` + `routers/ml_gate.py` (/api/ml/gate/*): Meta-Labeler
  (XGBoost, konservative Fixparams), krypto-only, Datenbasis Prod-Signale+Ghost+Decisions
  (PROD_MONGO_URL nur lesend; auf Render = eigene DB). Purged Walk-Forward (5 Blöcke) +
  24h-Embargo, Platt-Kalibrierung auf OOS, Brier vs. Baseline, Modelle versioniert in
  ml_gate_models (nie überschrieben).
- Shadow-Hook in ai_engine: jede LONG/SHORT-Decision trägt gate_shadow (p_win,
  model_version, threshold, would_block) — blockt NIE. Kontrafaktischer Report
  GET /api/ml/gate/report (Aktivierungskriterien + Threshold-Sweep).
- Erstes Prod-Training: v1, 970 Samples, OOS-Brier kalibriert 0.2393 < Baseline 0.2541,
  AUC 0.552 (ehrlich: noch schwach – Shadow-Phase sammelt Beweise, Collection-Mode
  vergrößert Datenbasis). Indizes decisions_ts/decisions_outcome_ts ergänzt.
- Tests: 7 Unit (test_phase5_gate_shadow.py) + 11 API (test_phase5_ml_gate_api.py,
  Testing-Agent) = 18/18 grün (iteration_20.json, 100%). Regressionen grün.

## ML-Umbau Phase 6 (14.06.2026 — Gate-Dashboard + Auto-Retrain)
- Auto-Retrain-Scheduler (MLGate.tick im Engine-Loop): Erst-Training ab 120 gelabelten
  Samples, Retrain nach 50 neuen Samples oder täglich 4 Uhr Berlin; Settings persistiert.
- UI-Tab "Gate v1" im KI-Labor (GateShadowPanel.js): SHADOW-Badge, Kriterien-Ampel,
  Kontrafaktik + Threshold-Sweep, Kalibrierungs-Chart, Versionsliste, Train-Button.
- Getestet: iteration_21 100% (Backend 17/17, Frontend 8/8), keine Bugs.

## Backlog / Nächste Aufgaben
- P0: User: Render-Deploy des OOM-Fixes + Trade-Manager-Guards (Branch pushen), danach in Prod: Migration 0.5 (Dry-Run → --apply) + Reward-Backfill
- P1: Phase 7 (User entscheidet): Regime-Brücke (=0.7) oder Shadow-Beweis abwarten
- P1: Prod-Befehle nach Deploy (Migration 0.5 Dry-Run → --apply → Reward-Backfill)
- P1: Backfill-Fortschritt als Statusleiste im Dashboard
- P1: Live-Verifikation der Bitunix-uPnL-Anzeige mit echtem API-Key (nur auf Render möglich)
- P2: Per-Setup-Timeframe-Statistik ins Playbook; Session-Levels optional im Chart
 kein Qualitätsverlust), Optuna gc_after_trial,
  gc.collect() nach Training (ml_lab + ml_gate).
- RCA merkwürdige KI-Trades: LLM-Trade-Manager micro-managte die Phase-4-Sammel-Trades
  (SL-Ratchet bis 0,007% an den Kurs, Hebel-Reflexe, 202× adjust_sl in 300 Aktionen).
  Fix: data_collection-Trades komplett vom KI-Management ausgenommen (Review-Filter +
  apply_action-Block, manuell bleibt erlaubt) + SL-Ratchet-Guard min_sl_gap (neuer KI-SL
  mind. max(30% der initialen SL-Distanz, 0,1% vom Kurs) vom Kurs entfernt) + Prompt-Regel.
- Tests: tests/test_ram_and_manager_guards.py 9/9 + Testing-Agent iteration_22 (19/19,
  100% Bugfix-Scope); Regressionen test_swing_and_profit_lock + test_review_bugfix_e2e
  32/32 (nach Bereinigung von Dev-DB-Testresten: trade_guard_state paused_until).

## ML-Umbau Fortsetzung (15.06., 7. Handover — Fee-Wächter)
- Repo Branch 0.69 nach /app geklont; .env selektiv (LLM-Keys, ADMIN, PROD_* nur lesend).
  Vorheriger Agent starb mitten in der Fee-Wächter-Umsetzung ohne Push → sauber neu gebaut.
- **Fee-Wächter** (User: nur Option 1): KI-Trade wird nur eröffnet, wenn SL-Distanz ≥
  fee_guard_mult × Roundtrip-Fees (Default 4× × 0,12% = 0,48%). Enforcement an EINEM Punkt
  in bitunix_trade.on_signal (nach finaler SL-Bestimmung → deckt Live/Sammel/Panel-Trades ab),
  Engine-Keys fee_guard_enabled/fee_guard_mult (nicht KI-änderbar), Prompt-Hinweis im
  Trade-Rahmen, UI-Controls im KI-Setup (ai-fee-guard-enabled-select/-mult-select).
- Tests: tests/test_fee_guard.py 6/6; Regressionen Phase-4 7/7, Guards 9/9, Gate 11/11;
  Testing-Agent iteration_23: Backend 100% + Frontend 100%.

## ML-Umbau Fortsetzung (15.06., Fee-Feedback-Paket)
- **Fee-Lern-Feedback** (ai_rewards.py, kein neuer Malus): Reward-Docs tragen fees +
  fee_share_pct (Anteil der Gebühren am Verlust, gekappt 100%); Lern-Prompt-Block
  "GEBÜHREN-ANTEIL an Verlusten" → KI lernt selbst, weitere Stops zu wählen.
- **Fees-vs-Risiko-Anzeige** (PerformanceAnalytics.js): Meta-Zeile an jeder Trade-Karte
  (trade-fees-vs-risk-{id}), Roundtrip-Fees / geplantes Risiko, farbkodiert (rot ≥50%).
- **Blockier-Statistik**: fee_guard_blocks-Collection (60d-Retention, Index fee_guard_ts),
  GET /api/ai/fee-guard/stats?days=7, Anzeige "Geblockt (7 Tage)" im KI-Setup
  (ai-fee-guard-stats).
- Tests: tests/test_fee_feedback.py 4/4; Regressionen grün; Testing-Agent iteration_24:
  Backend 100% + Frontend 100%.

## ML-Umbau Fortsetzung (15.06., Fix 0.7a+0.7b + User-Wünsche, 8. Session)
- **0.7a Gate-Domain**: shadow_predict/shadow_report krypto-only (TOP_10_COINS) —
  B6-Fix, Shadow-Report wertet keine Out-of-Domain-Symbole mehr.
- **0.7b Regime v2** (ai_market_observer): Vol-Label per Symbol-Perzentil (48h-Historie,
  P80/P30, Fallback Fix-Schwellen), Trend-Hysterese (rein ±0.08 / raus ±0.05),
  breakout nur mit Bestätigung sonst drift, regime_v=2, Mehr-Horizont-Trend
  trend_1d_pct/trend_3d_pct + daily_bias im Snapshot & Prompt.
- **KI-Trader-Reset**: POST /api/ai/trader/reset (Admin-JWT + Passwort-Body, löscht nur
  ai_trader-Paper-Trades/Signale/Rewards, Live bleibt, audit_log); UI AITraderReset.js
  im KI-Setup (ai-trader-reset-open-btn).
- **Trade-Karte**: KI-BEGRÜNDUNG-Block (setup, ai_reasoning, ai_confidence,
  ai_news_impact — neue Felder am Trade-Doc) + Live-Chart-Button (trade-open-chart-{id},
  schaltet Haupt-Chart aufs Trade-Symbol).
- Tests: test_regime_v2_and_gate_domain.py 11/11, Testing-Agent iteration_25:
  Backend 100% (16/16) + Frontend 100%, keine Regressionen.

## ML-Umbau Fortsetzung (15.06., Paket 2: Erklärbarkeit + Kontrolle, 8. Session)
- **CRV-Deckel**: crv_max Standard 4 + einmalige Migration (gespeicherte 0 → 4),
  TP Full zusätzlich auf 2×crv_max gedeckelt (Fix für „TP unrealistisch weit weg")
- **Regime-Badge** über dem Haupt-Chart (GET /api/ai/regime/{symbol}, 60s-Refresh,
  ausführlicher Tooltip, Tages-Bias)
- **Trade-Erklärung**: „Details"-Modal (GET /api/autotrade/trade/{id}/explain, 0 LLM-Kosten):
  volle Begründung, warum Positionsgröße/SL/TP (neue LLM-Felder size_reason/levels_reason),
  Fees, Markt beim Entry; ausführliche Hover-Tooltips auf Setup-/Konfidenz-/News-Badges
- **„Trade überdenken"**: POST /api/autotrade/trade/{id}/rethink (Admin, 15min-Cooldown/Trade,
  1 kleiner LLM-Call, Aktionen laufen durch die geprüfte apply_action-Schicht)
- Tests: iteration_26 alles grün (Backend 9/9, Frontend 100%, echter LLM-Call verifiziert)

## ML-Umbau Fortsetzung (15.06., Paket 3: Werte-Tooltips + Detail-Zerlegung)
- Alle Meta-Werte der Trade-Karte mit ausführlichen Hover-Erklärungen
- Details-Modal: Sektion „AKTUELLER STAND"/„ERGEBNIS" mit PnL-Zerlegung (Brutto − Gebühren = Netto, R)
- „Überdenken" token-sparsamer (max. 1 Aktion, 2-Sätze-Note, weiterhin nur auf Klick)
- iteration_27: Backend 24/24, Frontend 100%

## Backlog / Nächste Aufgaben
- P0: User: Render-Deploy (aktiviert 0.7a/0.7b + Reset + Trade-Karte + Fee-Paket in Prod), danach Reset-Button 1× drücken und Migration 0.5 + Reward-Backfill
- P1: ~1 Woche Prod-Daten, dann Regime-v2-Verteilung nur lesend prüfen; erst danach über 0.7c (regime_engine-Brücke) entscheiden
- P1: Prod-Befehle nach Deploy (Migration 0.5 Dry-Run → --apply → Reward-Backfill)
- P1: Backfill-Fortschritt als Statusleiste im Dashboard
- P1: Live-Verifikation der Bitunix-uPnL-Anzeige mit echtem API-Key (nur auf Render möglich)
- P2: Per-Setup-Timeframe-Statistik ins Playbook; Session-Levels optional im Chart

## Bugfix-Session (Juni 2026, Branch 0.77): Manuelle Trades + Trade-Karten-UI
- BUG 1 (manuelle Bitunix-Trades unsichtbar), Root Cause doppelt:
  a) Positions-Watchdog war in den DB-Settings deaktiviert (enabled=false) → Sichtbarkeits-Sync (adopt_unknown) lief gar nicht mehr. Fix: `services/position_watchdog.py` – run_loop läuft jetzt auch bei enabled=false im **sync-only-Modus** (übernimmt fremde Positionen als "Manuell (Bitunix)", greift aber NICHT an der Börse ein: kein SL, kein Dust-/Notfall-Close). check(manage=...)/_check_position(manage=...), Status-Report enthält `mode: full|sync-only`. `/api/autotrade/watchdog/run` läuft im sync-only-Modus mit.
  b) GET /api/autotrade/trades: offene Trades fielen aus dem limit-Fenster (200 neueste), sobald viele neuere KI-Trades existierten. Fix: `routers/autotrade.py::get_trades` hängt ohne status-Filter IMMER alle offenen Trades an (dedupliziert, mode-Filter respektiert).
- BUG 2 (Coins fehlten in Trade-Karten, PnL/Caret rechts abgeschnitten): `PerformanceAnalytics.css` – `.tdc-main` flex-wrap:wrap + row-gap, `.tdc-coin` min-width:48px (kollabiert nie mehr auf 0).
- FEATURE Multi-Timeframe pro Regel: war in 0.77 bereits vollständig umgesetzt (siehe MULTI_TIMEFRAME_PLAN.md) – per Tests + E2E verifiziert (StrategyBuilder-Dropdown, Optimizer rule_timeframes, Discovery/Combo/Explore, 422 bei ungültigem TF).
- Neue Regressionstests: tests/test_trades_visibility.py (3), tests/test_watchdog_sync_only.py (3); test_improvements_0_6.py auf asyncio.run umgestellt (Loop-Robustheit). Testing-Agent iteration_28.json: alles grün.

## Iteration (Juni 2026): PnL-Sync, Chart-Pin, exakte Bitunix-Verbuchung, TF-Statistik, Filter
- PnL-SYNC: Chart-Badge + Trade-Verlauf-Header (offene Trades) zeigen jetzt EINE Quelle: computed.upnl_pct_margin (Live = echter Bitunix-uPnL, 15s-Poll). MainChart.js/PerformanceAnalytics.js.
- EXAKTE VERBUCHUNG: _book_external_close nutzt jetzt Bitunix GET get_history_positions (closePrice/realizedPNL/fee/funding) → echter Netto-PnL statt Mark-Preis-Schätzung; Flag pnl_exchange_exact; Schutz bei manuell aufgestockten Positionen (>5% qty-Abweichung → Schätzung). parse_closed_position pure + Tests (test_exchange_close_truth.py).
- CHART-PIN: Klick auf Trade-Badge oder Entry-Pfeil pinnt Entry/SL/TP1/TP als Preislinien (nur Preislinien, RAM-schonend; aktualisieren alle 15s). useTradeMarkers.js renderPinned/togglePin/pinAtTime, MainChart subscribeClick, CSS .chart-open-badge.pinned.
- PLAYBOOK TF-STATISTIK: ai_playbook.tf_stats/best_tf_per_setup/tf_context_lines – bester Timeframe pro Setup (echte KI-Trades) in GET /api/ai/playbook (tf_stats+best_tf) und im KI-Prompt-Kontext.
- TRADE-FILTER: Analyse>Trades: Strategie-Dropdown (inkl. 'Manuell (Bitunix)') + 'Nur <ausgewählter Coin>'-Toggle, nur Listen-Anzeige (Statistik unverändert).
- REGEL-TF UI-LÜCKEN (User-Bug): TF-Dropdown pro Regel jetzt auch im SettingsPanel-Regel-Editor (def-rule-long/short-tf-*) und 'Regel-Timeframes optimieren'-Chip im Optimizer-Dynamik-Modus; Backend: rule_timeframes → _run_dynamic → discover_regime_strategy/optimize_regime_rules → build_candidates(tf_options).
- Tests: test_exchange_close_truth.py (7), test_playbook_tf.py (3); test_qqq_range_chart_fixes auf asyncio.run umgestellt. Kombi-Lauf 90 Tests grün. Testing-Agent iteration_29.json: 100% grün, keine Issues.

## Iteration (Juni 2026, #30): Trade-Zeiten, Karten-Layout, Coin-Toggle, manuelle Website-Trades, Chart-Aufräumen
- Uhrzeiten in Trade-Karten: fmtTimeShort ('TT.MM. HH:MM', Berlin-Zeit) rechts in .tdc-strat-line, geschlossene mit '→' (testid trade-times-<id>).
- Karten-Layout 3-zeilig: Z1 Mode/Side/Coin/Result/$PnL/Caret · Z2 SWING/DATEN links + %-PnL rechts unter $PnL (.tdc-sub, padding-right 20px) · Z3 Strategie + Zeiten.
- Coin-Toggle 'Nur <Coin>' jetzt in Titelzeile OFFENE TRADES (filtert offen+geschlossen); Strategie-Dropdown entfernt.
- Chart: permanente Entry-Preislinien rechts entfernt (doppelt zu Badges + Klick-Pin); useTradeMarkers apply() ohne addLine-Block.
- BUG-FIX manuelle Website-Trades: source!='ki' → suppress_signal (kein db.signals-Insert, kein Eval/Telegram/Broadcast, keine update_performance) + Trade als strategy_id 'external' / 'Manuell (Website)' / manual_trade=true (core/pipeline.py, ai_trade_manager.open_trade, bitunix_trade._open_trade). Getrennt von 'Manuell (Bitunix)' (Watchdog-Adopt).
- Tests: tests/test_manual_web_trades.py (3); test_iter26 crv_max-Assertions entpinnt (Nutzer-Setting). Testing-Agent iteration_30.json: 100% grün (E2E inkl. echtem Paper-Manuell-Trade, sofort wieder geschlossen).

## Iteration (Juni 2026, #31): Fee-Doppelzählung, Trade-Karten kompakt, Chart-Polish, Chat-Fenster, neue Modelle, RAM-Guard
- FEE-BUG (User: ETH 15.08 19:11, -8,5$ wurde -12$): Bitunix liefert realizedPNL in get_history_positions in der Praxis BEREITS inkl. Fees (entgegen API-Doku) → parse_closed_position zog die Fee nochmal ab. Fix: Erkennung über reinen Preis-PnL (entryPrice/closePrice/maxQty/side); liegt realizedPNL näher an (Preis-PnL − fee), sind Fees enthalten → net = realizedPNL + funding. Neues Feld fee_included_in_pnl. Betroffener ETH-Trade in Prod-DB repariert (-12.578744 → -8.593632, pnl_repair_note gesetzt); einziger betroffener Trade (alle pnl_exchange_exact-Trades gescannt). Tests: test_exchange_close_truth.py +2 (fee-inkl./fee-exkl.).
- TRADE-KARTEN kompakt (PerformanceAnalytics): OFFEN/G/V/BEP-Badge direkt neben LONG/SHORT (PnL-$ + Caret passen in Zeile 1); Zeilenabstand Z1→Z2 minimiert (.tdc-head gap 1px, padding 6px); %-PnL zentriert unter dem $-PnL (min-width 52px, text-align center); SWING/DATEN → rahmenlose Mini-Tags "S" (·R bei Runner) / "D" (.tdc-mini-tag, Tooltip erklärt).
- CHART: "· N bars" aus Subtitle entfernt; ALLE-TRADES-Button zeigt un-markiert nur noch "ALLE TRADES" (ohne "· 1 offen"), markiert weiterhin Gesamtzahl; Zeit-Button ▾ bleibt neben dem Text (white-space nowrap); Regime-Badge (z.B. "Ausbruch · ruhig") cursor:help → default (kein Fragezeichen-Cursor mehr, Tooltip bleibt).
- KI-PANEL: KI-Chat ist jetzt vollwertiges Fenster wie MasterPrompt – "Chat"-Button in der Status-Row (ai-chat-toggle), Zustand persistiert (localStorage krypto_ai_chat_window_open), andere Panels schließen Chat und umgekehrt; Setup-Panel scrollt (.ai-setup max-height 55vh, overflow-y auto).
- NEUE MODELLE (ai_providers + lib/aiModels): openrouter nvidia/nemotron-3.5-lightning:free (neues Free-Flaggschiff, 1M ctx, Gewicht 3) + deepseek/deepseek-v4-flash (bezahlt ~0,06$/M in / 0,13$/M out, Gewicht 3, live verifiziert). Bezahlte Modelle stehen in PAID_MODELS_NO_FALLBACK und NIE in FALLBACK_ORDER (kein unbemerktes Ausweichen auf Kosten); Test test_fallback_order_and_weights_consistent entsprechend angepasst.
- RAM-GUARD (services/ram_guard.py, in server.py-lifespan): mallopt(M_ARENA_MAX=2) + malloc_trim(0)-Loop alle 180s (gc.collect davor) – glibc-Fragmentierung war Haupttreiber des RSS-Wachstums bei normalem Gebrauch (Render 512MB-Kill). Env: RAM_GUARD_ENABLED, RAM_TRIM_INTERVAL_S. Lokal: RSS 357MB → ~168MB nach Neustart, Trim hält frei gewordenen Heap ans OS zurückgegeben.
- Cerebras-Free-Limits (für Analyst-Betrieb): 5 RPM / 30k TPM / 1M Tokens/Tag PRO KEY (Stand 08/2026).
