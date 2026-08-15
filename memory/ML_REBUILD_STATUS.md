# ML Rebuild Status

## Aktueller Stand (Datum: 2026-06-15, 8. Handover, 3. Paket)
- Paket 3 UMGESETZT (User 15.06., iteration_27 alles grün):
  1. Werte-Tooltips KOMPLETT: alle 14+ Meta-Werte der Trade-Karte haben
     ausführliche title-Erklärungen (R-Vielfaches, uPnL/PnL % auf Margin,
     % Positionsgröße/Kapital, Risk, Hebel, Kapital, Menge, TP1 getroffen …)
     – PerformanceAnalytics.js tdc-meta.
  2. Details-Modal genauer: neue erste Sektion "AKTUELLER STAND" (offen:
     Kurs/uPnL/live PnL/R/Abstände zu SL-TP1-TPF) bzw. "ERGEBNIS" (geschlossen:
     Exit, Gewinn/Verlust, Dauer, PnL-Zerlegung "Brutto − Gebühren = Netto (xR)").
     Backend: explain-Endpoint liefert Feld "state" via _enrich_trade
     (Bugfix dabei: _enrich_trade gibt Kopie zurück, Rückgabewert nutzen –
     routers/autotrade.py `t = _enrich_trade(t, ...)`).
  3. "Überdenken" token-sparsamer (User-Wunsch "sparsam und gut"): Prompt
     verlangt HÖCHSTENS EINE Aktion + max. 2-Sätze-Note; Code wertet nur noch
     actions[:1] aus (ai_trade_manager.review_single). Läuft weiterhin NUR auf
     Nutzer-Klick (kein Loop), 15-min-Cooldown pro Trade.
- Testing-Agent iteration_27: Backend 24/24 (inkl. Regression), Frontend 100%.
  Neue testids: trade-explain-state-{id}. Kein LLM-Call verbraucht.
- % geschafft grob: 75%

## Aktueller Stand (Datum: 2026-06-15, 8. Handover, 2. Paket)
- Nach dem 0.7a/0.7b-Paket (unten) im selben Handover UMGESETZT (User-Freigaben 15.06.):
  CRV-Deckel Standard 4, Regime-Badge über dem Haupt-Chart, ausführliche
  Trade-Erklärung ("Details"-Modal), "Trade überdenken"-Button, Hover-Erklärungen.
  Details: Abschnitt "Paket 2 UMGESETZT" direkt hierunter.
- % geschafft grob: 74%

## Paket 2 UMGESETZT (2026-06-15) — CRV-Deckel, Regime-Badge, Trade-Erklärung, Überdenken

### Änderungen Datei für Datei (git-artig erklärt)
1. **services/ai_engine.py**
   - DEFAULT crv_max: 0 → 4.0 (User: "nicht zu streng, weiter viele Trades,
     nur keine komplett komischen Einstiege"). UI-Erklärung: TP1 darf max. 4×
     SL-Distanz sein.
   - `load_config()`: EINMALIGE Migration `crv_max_migrated_v1` – gespeichertes
     crv_max=0 (alte Voreinstellung) wird beim ersten Start nach Deploy auf 4.0
     gehoben. Wer danach bewusst wieder 0 wählt, bleibt bei 0 (Marker verhindert
     Wiederholung). → Greift in Prod automatisch beim Render-Deploy.
   - `_apply_crv_frame()`: NEU wird auch TP Full gedeckelt: tpf ≤ 2× crv_max × SL
     (bei Standard 4 also max. 8R). Vorher war nur TP1 gedeckelt – Ursache der
     "TP Full unrealistisch weit weg"-Screenshots.
   - Entscheidungs-JSON-Schema (beide Prompt-Varianten): 2 neue optionale Felder
     `size_reason` ("warum diese Positionsgröße") und `levels_reason` ("warum
     SL/TP genau dort") – je 1 kurzer Satz, minimale Token-Mehrkosten. Werden in
     der Decision gespeichert (je [:200]) und via Signal ans Trade-Doc kopiert
     (ai_size_reason / ai_levels_reason).
2. **services/ai_trade_manager.py**
   - NEU `review_single(trade_id)`: fokussierter Einzel-Trade-Review ("Trade
     überdenken"). Nutzt dieselbe geprüfte `apply_action`-Schicht wie das normale
     Review (ALLE Guards gelten: manuelle/externe Trades blockiert,
     Datensammel-Trades blockiert, Aktions-Limits, SL-Klemmen). Cooldown 15 min
     pro Trade (rethink_ts am Trade-Doc), Ergebnis-Notiz als rethink_note
     gespeichert. Empfohlene Aktionen werden DIREKT ausgeführt (User-Freigabe;
     Trades sind paper, Live-Close ginge über die verifizierte Börsen-Schicht).
3. **routers/autotrade.py**
   - NEU GET /api/autotrade/trade/{id}/explain (öffentlich, 0 LLM-Kosten):
     Fakten (SL/TP-Distanzen in %, CRV TP1/TPF, Risiko $ und % der Margin,
     Margin×Hebel=Positionswert, Roundtrip-Fees, Fees-vs-Risiko, Fee-Wächter-
     Mindest-SL) + volle Decision aus ai_decisions (reasoning, size/levels_reason,
     Konfidenz, Modell, Gate p_win, Regime-Features beim Entry).
   - NEU POST /api/autotrade/trade/{id}/rethink (Admin): ruft review_single;
     429 bei Cooldown.
4. **routers/ai.py**: NEU GET /api/ai/regime/{symbol} – frische Regime-v2-Features
   aus dem Kerzen-Puffer (fürs Chart-Badge).
5. **services/bitunix_trade.py**: Trade-Doc speichert zusätzlich ai_size_reason,
   ai_levels_reason.
6. **frontend/src/components/TradeAIDetails.js (NEU)**: "Details"-Button + Modal
   (Sektionen: Volle Begründung / Warum diese Positionsgröße / Warum SL-TP dort
   inkl. Fee-Zeile / Markt beim Entry / Letztes Überdenken) + "Trade überdenken"-
   Button (nur offene ai_trader-Trades). Exportiert SETUP_EXPLAIN- und
   REGIME-Erklärungs-Maps (eine Quelle für alle Tooltips).
7. **frontend/src/components/RegimeBadge.js (NEU)**: Badge im Chart-Header
   (Regime deutsch + Vol-Suffix + 24h-Bias, 60s-Refresh, ausführlicher
   Hover-Tooltip). Eingebunden in MainChart.js neben dem Chart-Untertitel.
8. **frontend/src/components/PerformanceAnalytics.js**: Setup-/Konfidenz-/News-
   Badges mit ausführlichen Hover-Erklärungen; TradeAIDetails im KI-Block;
   Block erscheint jetzt für alle ai_trader-Trades (auch alte mit decision_id).
9. **CSS**: PerformanceAnalytics.css (tdx-Modal), MainChart.css (regime-badge).

### data-testids (für Tests)
chart-regime-badge, chart-regime-bias, trade-explain-btn-{id},
trade-explain-modal-{id}, trade-explain-close-{id}, trade-rethink-btn-{id},
trade-rethink-note-{id}, trade-rethink-msg-{id}.

### Testing-Agent iteration_26 (alles grün) + Nacharbeit
- Backend 9/9, Frontend 100%, echter rethink-LLM-Call ok (2.4s, groq), 429-Cooldown ok.
- Nacharbeit: Cooldown-/401-Feedback jetzt INLINE unter dem Button
  (trade-rethink-msg-{id}), weil der zentrale toast-Wrapper error-Popups per
  Design in die Benachrichtigungs-Glocke umleitet (lib/toast.js) – sonst wirkte
  der Klick "stumm".
- Fehlalarm des Testing Agents geklärt: "KI-Trader in Dev aktiv" war NICHT die
  LLM-Engine (enabled=false verifiziert), sondern der normale Paper-Trade-Monitor
  + Kill-Switch, der die Seed-Trades mit unrealistischem Entry sofort per SL
  schloss. Erwartetes Verhalten, keine Leitplanken-Verletzung.

### Bewusste Entscheidungen / Risiken
- crv_max-Migration ist idempotent und respektiert spätere User-Wahl (Marker).
- TPF-Deckel 2×crv_max: bewusst großzügig (Runner-Ziele), passt zur alten
  Prompt-Obergrenze tpf_pct ≤ 8% bei sl bis 3% – kein Bruch fürs Lernen.
- rethink nutzt source="ki" → Datensammel-Trades (DATEN-Badge) werden von
  Aktionen NICHT angefasst (Label-Qualität!), die KI-Notiz kommt trotzdem.
- explain-Endpoint ist GET/öffentlich wie die übrigen Lese-Endpoints.

### Idee des Users, notiert für später (nicht gebaut)
"Prüfer-KI": eine zweite KI, die Entscheidungen der ersten laufend gegenprüft.
Teilweise existiert das schon (Trade-Manager-Review alle X min + ML-Gate-Shadow
als statistischer Prüfer). Ausbaustufe wäre: billiges Modell prüft jede
LONG/SHORT-Decision VOR dem Emit (1 Mini-Call, nur bei Konfidenz nahe der
Schwelle, Token-Budget beachten). Erst nach Gate-v1-Bewertung entscheiden –
das Gate könnte dieselbe Rolle kostenlos übernehmen.

### Offener Plan "Erklärbarkeit Schritt für Schritt" (User 15.06.)
- [x] Tooltips: Setup-Badges, Konfidenz, News, Fees-vs-Risiko, Regime-Badge
- [x] Details-Modal mit Größen-/Level-Begründung + Fee-Zeile
- [ ] Nächster Schritt (P2): Tooltips für ALLE Meta-Werte der Trade-Karte
      (uPnL % auf Margin, R-Vielfaches, PnL % Kapital, Menge, TP1 getroffen …)
      – gleiche Machart, title-Attribute in PerformanceAnalytics.js Meta-Grid
- [ ] P2: Erklär-Seite/Glossar (alle Begriffe zentral), verlinkt aus Tooltips

## Aktueller Stand (Datum: 2026-06-15, 8. Handover)
- Aktive Phase: Fix 0.7a + 0.7b UMGESETZT (Gate-Shadow krypto-only, Regime v2 mit
  Perzentil-Vol/Hysterese/ehrlichem Breakout, Mehr-Horizont-Trend 1d/3d) sowie drei
  User-Wünsche: KI-Trader-Reset (Admin-Passwort), KI-Begründung an der Trade-Karte,
  Live-Chart-Button an der Trade-Karte. Details unten ("Fix 0.7a+0.7b UMGESETZT").
- 8. Handover (2026-06-15): Repo Branch 0.72-regime-analysiert nach /app geklont,
  .env selektiv übernommen (LLM-Keys, ADMIN, PROD_MONGO_URL/PROD_DB_NAME nur lesend;
  Bitunix/Telegram/Supabase bewusst NICHT), App lauffähig (Backend healthy, Admin-Login
  ok, Prod-Lesezugriff ok: auto_trades=134, ml_gate_models=2, ai_rewards=0 → User hat
  Rewards in Prod bereits selbst geleert).
- User-Freigaben (15.06., 8. Session): 0.7a+0.7b+Mehr-Horizont-Trend JA („aber überprüf
  ob das alles Sinn macht"), Label-Bruch regime_v=2 JA, Reset-Button JA (mit
  Passwort-Eingabe), Trade-Karten-Begründung + Chart-Button JA (Chart-Button = Haupt-Chart
  in der Mitte auf das Symbol des Trades umschalten).
- % geschafft grob: 72%

## Fix 0.7a+0.7b UMGESETZT (2026-06-15) — Gate-Domain + Regime v2 + User-Wünsche

### 0.7a — Gate-Shadow krypto-only (B6-Fix, dringend)
- services/ml_gate.py `shadow_predict()`: prädiziert NUR noch für Symbole in
  TOP_10_COINS (Trainings-Domain). OIL/GOLD/SILVER/SPY/Forex → None, kein gate_shadow-Log.
- services/ml_gate.py `shadow_report()`: Query filtert `symbol ∈ TOP_10_COINS` —
  bestehende Out-of-Domain-gate_shadow-Einträge in Prod werden dadurch automatisch
  aus dem Report ausgeschlossen (kein Daten-Löschen nötig). Report ist wieder ehrlich;
  Gate-Aktivierungskriterien werden ab jetzt auf sauberer Domain bewertet.

### 0.7b — Regime v2 (B1+B2-Fix) in services/ai_market_observer.py
- Neue reine Funktion `classify_regime_v2()` (v1 bleibt für Alt-Daten/Tests):
  - Vol-Label per Symbol-Perzentil: aktuelle 60m-Vol wird gegen die eigene bis zu
    48h-Historie gerankt (Fenster 60m, Schritt 15m, mind. 24 Fenster ≈ 6h);
    >= P80 → volatil, <= P30 → ruhig, sonst normal. Ohne genug Historie Fallback
    auf die alten Fix-Schwellen (features["vol_basis"] = "percentile"|"fixed_fallback").
  - Trend-Hysterese gegen Label-Flattern: Eintritt ab ±0.08 (wie v1), bestehender
    Trend wird erst unter ±0.05 verlassen (prev_regime wird durchgereicht:
    collect(), entry_snapshot(), bitunix_trade Entry-Snapshot).
  - „breakout" nur mit Bestätigung: Preis am Range-Rand UND Bewegung in Randrichtung
    UND (|change_60m| >= max(6×vol_pct, 0.05%) ODER volume_ratio >= 1.5) —
    sonst ehrlich „drift_<vol>". Neues Label „drift" wird vom Gate-Encoding wie
    range behandelt (encode_regime unverändert → Modell-Dimensionen stabil).
  - features neu: regime_v=2, vol_rank, vol_basis, trend_1d_pct/trend_3d_pct +
    daily_bias (up/down/flat, Deadzone max(8×vol, 0.15%)) wenn genug Historie
    (Mehr-Horizont-Trend, User-Wunsch gegen „gewürfeltes" Kurzfrist-Regime).
    snapshot_to_text zeigt 24h/3d + Tages-Bias im Prompt.
- Label-Bruch bewusst (User-Freigabe): alte Daten regime_v fehlt (=1), neue =2.
  Gate trainiert laufend neu, kein Backfill der Alt-Labels.

### KI-Trader-Reset (User-Wunsch, „Trades auf 0")
- POST /api/ai/trader/reset (routers/ai.py): erfordert Admin-JWT UND erneute
  Admin-Passwort-Eingabe im Body. Löscht NUR ai_trader-Paper-Trades (mode != live,
  inkl. Sammel-Trades), ai_trader-Signale und Rewards (ai_rewards.clear mit
  cleared_at-Sperre). Live-Trades bleiben IMMER unangetastet. Audit-Log-Eintrag
  "ai_trader_reset" mit Zählern. ai_decisions bleiben (ML-Trainingsdaten!).
- Frontend: components/AITraderReset.js, eingebunden unten im KI-Setup-Grid
  (AITradingPanel). data-testids: ai-trader-reset-open-btn / -password-input /
  -confirm-btn / -cancel-btn. WICHTIG: Wirkt in Dev auf die Dev-DB; in Prod nach
  Render-Deploy einmal vom User zu drücken.

### Trade-Karte: KI-Begründung + Live-Chart-Button (User-Wunsch)
- Backend: signal trägt jetzt ai_news_impact (ai_engine _emit_signal); Trade-Doc
  speichert ai_reasoning, ai_news_impact, ai_confidence zusätzlich zu setup
  (bitunix_trade). Nur neue Trades haben die Felder (User resettet ohnehin auf 0).
- Frontend PerformanceAnalytics.js TradeDetailCard: Block „KI-BEGRÜNDUNG"
  (Setup-Badge, Konfidenz, News-Impact, kurzer Reasoning-Text) +
  Button „Live-Chart <Coin> öffnen" → schaltet den Haupt-Chart (App.js
  setSelectedCoin) auf das Trade-Symbol. data-testids: trade-ai-context-<id>,
  trade-ai-reasoning-<id>, trade-open-chart-<id>.

### Tests (backend/tests/test_regime_v2_and_gate_domain.py)
- 11 Unit-Tests: v2-Perzentil-Basen, Fallback, Hysterese (rein + via prev_regime),
  Breakout-Bestätigung inkl. Richtungs-Check, v1 unverändert, compute_features
  (regime_v/vol_basis/vol_rank/1d-3d-Trend/daily_bias), snapshot_to_text,
  shadow_predict-Domain-Filter. Alle grün.
- Bestehende Suites: keine Regression durch diese Änderung. Bekannte lokale
  Abweichungen (unverändert, NICHT durch 0.7 verursacht): Regime-Lab/Worker-Tests
  (brauchen Live-Worker), ai_lab-Provider-Preset-Tests (Key-abhängig),
  test_phase5_ml_gate_api Versions-Zähler (Dev-DB hat schon v5+ trainiert).

### Kritische Selbst-Prüfung („macht das Sinn?", User-Auftrag)
- Perzentil statt fixer Schwellen: ja — B1 zeigte, dass EIN globaler Schwellwert
  für Krypto+Forex+Rohstoffe unmöglich passt; Perzentil ist per Definition
  selbst-kalibrierend je Symbol. Risiko: „volatil" ist jetzt RELATIV zum eigenen
  Symbol (BTC-ruhig ≠ Forex-ruhig absolut) — für Gate-Feature und Prompt ist genau
  das gewollt. Erwartung: Verteilung ~20% volatil / ~30% ruhig statt 99,97% ruhig.
- 6×vol als Breakout-Move-Schwelle: Random-Walk-Erwartung für 60m ≈ √60 ≈ 7.75×
  1m-Vol; 6× ist bewusst etwas darunter (lieber ein paar echte Breakouts mehr als
  alle verpassen). Nach ~1 Woche Prod-Daten Verteilung prüfen (Ziel: breakout
  deutlich < 10% statt 28%).
- Hysterese nur für Trend-Labels, nicht für Vol-Basis: Vol-Rank ist über 48h
  ohnehin träge. Bewusst simpel gehalten.

## Als Nächstes (nach User-Review + Render-Deploy)
1. User: Render-Deploy, dann Reset-Button einmal drücken (KI-Trades auf 0).
2. ~1 Woche Prod-Daten sammeln, dann Regime-v2-Verteilung nur lesend prüfen
   (volatil/ruhig/drift/breakout-Anteile) — erst danach über 0.7c
   (regime_engine-Brücke) entscheiden.
3. Migration 0.5 + Reward-Backfill in Prod weiterhin OFFEN (Anleitung unten).

## Aktueller Stand (Datum: 2026-06-15, 7. Handover)
- Aktive Phase: Fee-Wächter (iteration_23: 100%) + Fee-Feedback-Paket (Lern-Feedback,
  Fees-vs-Risiko-Anzeige, Blockier-Statistik; iteration_24: Backend 100% + Frontend 100%)
  — beides UMGESETZT + getestet. Wartet auf User-Review + Render-Deploy.
- 7. Handover (2026-06-15): Repo Branch 0.69 nach /app geklont, .env selektiv übernommen
  (LLM-Keys, ADMIN, PROD_MONGO_URL/PROD_DB_NAME nur lesend; Bitunix/Telegram/Supabase
  bewusst NICHT), App lauffähig verifiziert (Backend healthy, Admin-Login ok,
  Prod-Lesezugriff ok: auto_trades=121, ai_rewards=25, ml_gate_models=2 → Auto-Retrain
  hat in Prod bereits v2 trainiert, Sammel-Trades laufen). WICHTIG: Der vorherige Agent
  war mitten in der Fee-Wächter-Umsetzung abgestorben (Credits) und hatte NICHTS
  gepusht — Branch 0.69 enthielt keinerlei Fee-Wächter-Code, daher sauber neu gebaut.
  Migration 0.5 + Reward-Backfill in Prod weiterhin NICHT ausgeführt (Anleitung unten).
- % geschafft grob: 68%

## Fee-Wächter UMGESETZT (2026-06-15) — Physik-Grenze gegen Fee-Verlierer
User-Entscheidung: nur Option 1 (Fee-Wächter); Option 2 wurde direkt danach beauftragt.
- Prinzip: Ein KI-Trader-Trade wird nur eröffnet, wenn seine SL-Distanz mind.
  fee_guard_mult × Roundtrip-Fees (2 × fee_percent des Coins, Standard 0,06%/Seite)
  beträgt. Default 4× → 0,48% Mindest-SL-Distanz. Die KI darf weiter scalpen, so viel
  sie will — nur mathematisch garantierte Fee-Verlierer werden geblockt.
- Neue Engine-Config-Keys (services/ai_engine.py, NICHT in der KI-Whitelist — die KI
  kann den Wächter nie selbst lockern): `fee_guard_enabled`=True, `fee_guard_mult`=4.0
  (update_config klemmt 0–30; mult 0 = aus).
- Enforcement an EINEM Punkt: services/bitunix_trade.py — Modul-Funktionen
  `fee_guard_min_sl_pct()` + `fee_guard_check()` (~Z.44) und Block in `on_signal`
  direkt NACH der finalen SL-Bestimmung (nach dem use_ai_levels-Block).
  Begründung: Erst dort steht der echte SL fest (Coin-Config-_levels ODER
  use_ai_levels) → deckt ALLE KI-Pfade ab (Live-Signale, Sammel-Trades,
  KI-Panel-/Custom-Trades). Andere Strategien sind bewusst NICHT betroffen
  (Guard nur bei strategy_id=ai_trader).
- Geblockte Trades: signal._reject_reason mit Zahlen; Governance-Eintrag im KI-Feed
  NUR für Nicht-Sammel-Trades (Phase-4-Konvention: kein Feed-Spam durch Sammel-
  Signale, Grund steht trotzdem in _reject_reason + Log).
- Prompt-Ergänzung (gleiche Logik wie SL-Ratchet-Regel): frame_lines im Trade-Rahmen
  (ai_engine, FEE-WÄCHTER-Zeile) nennen der KI die Mindest-SL-Distanz, damit sie
  nicht wiederholt Geblocktes vorschlägt. Dynamisch aus fee_guard_mult berechnet.
- UI: KI-Panel → Setup, nach "KI-Limit Cooldown": "Fee-Wächter" an/aus
  (testid ai-fee-guard-enabled-select) + "Fee-Faktor (× Fees)" 2–10×
  (testid ai-fee-guard-mult-select), mit erklärenden Tooltips.
- Tests: /app/tests/test_fee_guard.py (6 PASS). Regressionen: Phase-4 7/7 (Fixtures
  schalten den Wächter dort explizit aus — ATR-SLs der Test-Kerzen liegen unter
  0,48%, bewusst NICHT das Verhalten aufgeweicht), RAM/Manager-Guards 9/9,
  Phase-5+6 11/11, swing/review_e2e/custom_ai 48 passed (8 Errors pre-existing,
  via git stash verifiziert), ai_trader/governance 28 passed (1 Fail
  umgebungsbedingt: frische Dev-DB ohne Analysis-Chat-Historie).
  Testing-Agent iteration_23: 100% Backend + Frontend.

## Fee-Feedback-Paket UMGESETZT (2026-06-15) — Lern-Feedback + Anzeige + Blockier-Statistik
User-Auftrag: alle 3 Vorschläge aus dem Fee-Wächter-Abschluss.
### 1. Fee-Lern-Feedback (services/ai_rewards.py) — KEIN neuer Malus, reine Transparenz
- compute_reward liefert zusätzlich `fees` (fees_paid) und `fee_share_pct` (nur bei
  Verlusten: fees / |realized_pnl| × 100, gekappt bei 100) — landet in jedem
  ai_rewards-Doc; history() reicht beide Felder durch.
- context_text (Reward-Block in JEDEM Lernlauf) enthält neuen Abschnitt "GEBÜHREN-
  ANTEIL an Verlusten": Ø-Anteil + "bei N von M Verlusten machten Fees >=50% aus" +
  Erklärung, dass die KI dann VON SICH AUS weitere SL-Distanzen oder kleinere
  Positionen wählen soll. Bewusst keine Vorschrift (User-Wunsch: wenig Vorgaben).
  Alte Rewards ohne fee_share_pct werden sauber ignoriert.
### 2. Fees-vs-Risiko-Anzeige (frontend/src/components/PerformanceAnalytics.js)
- Neue Meta-Zeile "Fees vs. Risiko" in der aufgeklappten Trade-Karte (nach "Risk",
  testid trade-fees-vs-risk-{id}): Roundtrip-Fees (geschlossen = fees_paid, offen =
  2× Entry-Fee als Schätzung) / geplantes Risiko (|entry − initial_sl| × qty) in %.
  Farbe: grün <25%, gelb 25–50%, rot ≥50%. Tooltip erklärt die Bedeutung. Rein
  clientseitig aus vorhandenen Trade-Feldern — kein Backend, keine Migration.
### 3. Blockier-Statistik (Fee-Wächter)
- bitunix_trade.on_signal protokolliert jeden Fee-Wächter-Block in neuer Collection
  `fee_guard_blocks` (id, ts, symbol, side, collection-Flag, sl_dist_pct,
  est_fees_usdt = Kapital × Hebel × 2 × fee_percent, reason); 60-Tage-Retention
  (Prune bei Insert), Index fee_guard_ts (core/indexes.py).
- Neuer Endpoint GET /api/ai/fee-guard/stats?days=7 (öffentlich wie /api/ai/status,
  days geklemmt 1–90): blocked_total, blocked_collection, est_fees_saved_usdt, recent[10].
- UI: KI-Setup, direkt unter dem Fee-Faktor: Zeile "Geblockt (7 Tage)" (testid
  ai-fee-guard-stats), z.B. "3× (davon 2 Sammel) · ~1.24 $ Fees vermieden".
### Tests
- /app/tests/test_fee_feedback.py (4 PASS: fee_share_pct inkl. 100%-Kappung + None
  bei Win, GEBÜHREN-ANTEIL im Lern-Prompt + history-Felder, fee_guard_blocks-Doc
  über die echte on_signal-Pipeline, Stats-Aggregation). Regressionen: fee_guard 6/6,
  Phase-4 7/7, ai_learning 22/22, rewards_backfill 4/4. Testing-Agent iteration_24:
  Backend 100% + Frontend 100% (Setup-Anzeige live verifiziert; Trade-Karten-Zeile
  per Code-Review + fehlerfreiem Rendern — frische Dev-DB hat keine Trades,
  sichtbar wird sie in Prod an jedem Trade mit qty/entry/sl/fees).

## 🧪 So testest du es selbst (Fee-Wächter + Fee-Feedback, Dev-Preview; Prod nach Deploy)
1. KI Trader → Parameter → Setup: "Fee-Wächter" (an) + "Fee-Faktor (× Fees)" (4×),
   Reload-fest; darunter "Geblockt (7 Tage)" — in Dev "0× · ~0.00 $", in Prod zählt
   sie hoch, sobald der Wächter blockt. API: GET /api/ai/fee-guard/stats?days=7.
2. Analytics → Trades → Trade-Karte aufklappen: neue Zeile "Fees vs. Risiko" mit
   ≈X% (Y $), rot wenn Fees ≥50% des geplanten Risikos (in Dev erst sichtbar, wenn
   Trades existieren; in Prod sofort an jeder Karte).
3. Lern-Feedback: ai_rewards-Docs tragen fees + fee_share_pct; der Lern-Prompt
   enthält nach Verlust-Trades den Block "GEBÜHREN-ANTEIL an Verlusten".
4. Konsole/Dev: `python tests/test_fee_guard.py` → 6/6, `python tests/test_fee_feedback.py` → 4/4.

## Aktueller Stand (Datum: 2026-06-15, 6. Handover)
- Aktive Phase: OOM-Fix + Trade-Manager-Guards (nach User-Deploy von Phase 4+5+6) — UMGESETZT,
  Unit-Tests 9/9 grün, wartet auf Testing-Agent-Verifikation + User-Review + Render-Deploy.
- 6. Handover (2026-06-15): Repo Branch 0.67 nach /app geklont, .env selektiv übernommen
  (LLM-Keys, ADMIN, PROD_MONGO_URL nur lesend; Bitunix/Telegram/Supabase bewusst NICHT),
  App lauffähig verifiziert (Backend healthy, Admin-Login ok, Prod-Lesezugriff ok).
  Prod-Befund: User HAT deployt (ml_gate_models=1 in Prod, trigger 'auto (Erst-Training)',
  ai_rewards=19, auto_trades=114, Sammel-Trades laufen). Migration 0.5 + Reward-Backfill
  in Prod NOCH NICHT ausgeführt (User wusste nicht wie — Anleitung siehe eigener Abschnitt).
- % geschafft grob: 64%

## RCA: Regime-Qualität beider Welten (2026-06-15, nur lesend analysiert) — VORARBEIT ZU 0.7
Skript: /app/backend/scripts/analyze_regime_quality.py (read-only gegen PROD_MONGO_URL).
Kontext: User will Regime-Brücke (0.7), wollte aber ZUERST wissen, ob das Regime
überhaupt korrekt funktioniert. Befunde (Prod, 24.246 Snapshots / 1.432 Decisions 14d):
- B1 VOL-DIMENSION DEGENERIERT: 99,97% aller Snapshots sind "ruhig" oder "normal",
  "volatil" kommt praktisch nie vor (8 von 24.246). Die fixen Schwellen in
  ai_market_observer.classify_regime (volatil >= 0,35% / ruhig <= 0,06% Stdev der
  1m-Returns) sind für 1m-Daten global zu hoch: Median-Vol Forex 0,009% (→ 98%
  "ruhig"), Krypto 0,038%, Rohstoffe 0,048%. Das Gate-Feature regime_vol ist damit
  fast konstant → trägt keine Information.
- B2 "BREAKOUT"-LABEL IRREFÜHREND: 28% aller Snapshots heißen "breakout_*" —
  Definition ist nur range_pos<20 oder >80 der letzten 60 min (Preis am Rand der
  Stunden-Range). Das ist meist Drift, kein Breakout. Zweithäufigstes Label ist
  damit semantisch falsch; die KI liest es im Prompt wörtlich.
- B3 ASSET-KLASSEN-BLIND: identische Schwellen für Krypto/Forex/Indizes/Rohstoffe
  → Verteilungen nicht vergleichbar; ai_rewards.by_regime mischt Klassen.
- B4 ZWEI WELTEN OHNE BRÜCKE (bekannt): regime_engine v2 (3/5/9-Modi, Hysterese,
  per-Symbol-Modelle, ~1.600 Zeilen) läuft NUR im Optimizer/Strategie-/NNFX-Kontext
  (dynamic_live, regime_lab); der komplette KI/ML-Pfad (Snapshots, Prompts, Rewards,
  Gate-Features) nutzt die 12-Label-Heuristik aus B1–B3.
- B5 ENTWARNUNG COVERAGE: entry_market_snapshot-Coverage an LONG/SHORT-Decisions
  seit dem Deploy 100% (letzte 24h: 180/180; die 27% im 14d-Fenster sind Alt-Daten
  von vor dem Deploy). Alle 28 KI-Trades haben Entry-Snapshots. Fix 0.2 wirkt.
- B6 GATE-SHADOW DOMAIN-MISMATCH (NEUER BUG, wichtig): shadow_predict + shadow_report
  filtern NICHT nach Symbol → das krypto-only trainierte Gate v1 prädiziert in Prod
  munter für OIL/GOLD/SILVER/SPY/USDJPY (Out-of-Domain; Nicht-Krypto dominiert die
  hohen Konfidenzen laut RCA 14.08.!). Die 5 Aktivierungskriterien würden auf
  verfälschten Daten bewertet. Muss vor dem Shadow-Beweis gefixt werden.
- B7 REWARDS-REGIME-STATISTIK aktuell wertlos: n=27, fast alles Verluste aus der
  Micro-Management-Ära — kein belastbares Regime-Lernsignal. Wird mit Sammel-Trades
  + den 15.06.-Fixes von selbst besser (kein Code nötig).
- Nebenbefund (bestätigt den Fee-Wächter): 68% der geschlossenen KI-Verlust-Trades
  waren fee-dominiert (Fees >= 50% des Verlusts), Ø Fee-Anteil 71%.

### Was gemacht werden muss (Vorschlag, wartet auf User-Freigabe)
- [0.7a, klein + dringend] Gate-Shadow auf die Trainings-Domain begrenzen:
  shadow_predict nur für TOP_10_COINS (bzw. Symbol-Filter im Hook) + shadow_report
  filtert auf Krypto; bestehende Out-of-Domain-gate_shadow-Einträge im Report
  ausschließen. Kleiner, isoliert testbarer Fix — behebt B6.
- [0.7b, Kern] classify_regime kalibrieren (Welt A bleibt, wird ehrlich):
  Vol-Label über rollierende Perzentile PRO SYMBOL (z.B. P25/P75 der letzten
  N Snapshots aus der DB oder dem Kerzen-Puffer) statt globaler Fix-Schwellen;
  "breakout" nur mit Bestätigung (range_pos-Extrem UND |change_60m_pct|- oder
  volume_ratio-Schwelle), sonst neues Label "drift". Features bekommen regime_v=2
  (Versions-Feld), damit ML alte/neue Labels trennen kann; encode_regime im Gate
  versteht beide. Rückwärtskompatibel: Label bleibt String im selben Feld.
- [0.7c, eigentliche Brücke, NACH 0.7b evaluieren] regime_engine-Klassifikation
  (mode=5, reactive auf 15m-Aggregaten aus dem candle_cache) zusätzlich als
  features.regime_v2 an Snapshot/Entry-Snapshot; Gate bekommt beide Encodings und
  das Training zeigt, was trägt. VORBEHALT: CPU/RAM-Budget auf Render (512 MB)
  und Mehrwert erst messbar, wenn 0.7b-Daten da sind — bewusst als eigener,
  optionaler Schritt (Kosten-Nutzen prüfen, nicht blind bauen).
- Hinweis Label-Bruch: Nach 0.7b mischen sich alte und neue Regime-Labels in den
  Trainingsdaten — Gate-Retrain läuft ohnehin laufend; regime_v-Feld macht den
  Bruch für das Modell sichtbar. Kein Migrations-Bedarf an Alt-Daten.

## RCA: Render-OOM alle ~30 min (15.06., nur lesend analysiert) — GEFIXT

- Symptom: "Instance failed: Ran out of memory (used over 512MB)" alle ~30 Minuten.
- URSACHE: Prod-Settings ai_ml_settings = {auto_train:true, min_new_results:10, n_trials:100,
  lookback_days:365}. Die Phase-4-Sammel-Trades erzeugen laufend neue Labels → das ALTE
  ML-Labor (services/ai_ml_lab.py, Throttle exakt 1800s = 30 min) triggert bei ≥10 neuen
  Labels JEDES Mal ein Training und lud dabei 6000 VOLLE ai_decisions (inkl.
  entry_market_snapshot/reasoning/prompt_version) + 6000 volle Signale (inkl. rules_snapshot)
  + 30000 volle Snapshots OHNE Projektion → ~100–200 MB transiente Python-Spitze
  + Optuna-100-Trials. Baseline (~300–400 MB) + Spike > 512 MB → Kill. Crash-Rhythmus
  = exakt der 30-min-Throttle. (Gate v1 war NICHT die Ursache: dessen build_dataset war
  bereits projiziert; Gate-Retrain-Schwelle 50 neue Samples war noch nicht erreicht.)
- FIX (kein Qualitätsverlust — identische Feature-/Label-Felder):
  * ai_ml_lab.load_training_data: Mongo-Projektionen (_ROW_PROJECTION: nur die 15 Felder,
    die label_of()/feature_row() lesen; Snapshots nur symbol/ts/features) → Spike <15 MB.
  * ai_ml_lab.train_sync: study.optimize(..., gc_after_trial=True).
  * ai_ml_lab.train + ml_gate.train: gc.collect() im finally.
- Getestet: tests/test_ram_and_manager_guards.py::test_ml_lab_projection_dataset_identical
  (Dataset-Row aus projizierten Docs == feature_row(volles Doc, voller Snapshot)).
- OPTIONAL für Prod (User-Entscheidung, ohne Deploy): n_trials im ML-Labor-UI von 100 auf
  ~25–30 senken (CPU-Last ↓, minimaler Qualitätseinfluss; RAM ist durch den Fix gelöst).

## RCA: Merkwürdige KI-Trades (SL-Ratchet, Hebel-Reflex, zu frühe Exits) — GEFIXT
- Befund (Prod, ~18 neue Paper-Trades seit Deploy): Die auffälligen Trades (QQQUSDT SHORT
  22:53 mit 7 KI-Aktionen: SL 733.84→732.2→…→731.6 + HEBEL 55.56→25; SILVER LONG mit
  HEBEL 83.33→25 + SL auf 64.84) sind data_collection=True Sammel-Trades (Phase 4).
  Der LLM-Trade-Manager (Review alle 5 min, Cooldown 3 min, max 8 Aktionen/Trade,
  Prompt-Leitlinie "Gewinne sichern") micro-managte sie: SL-Ratchet bis 0,007% an den
  Kurs → sofortiger Stop durch Rauschen, Fees (0,06%×2 auf große Notionals durch
  auto_leverage) fressen mehr als das geplante Risiko. Letzte 300 KI-Aktionen in Prod:
  202× adjust_sl (davon 143 fehlgeschlagen/geblockt), 70× close, 13× set_leverage.
  Doppelt schlimm: Micro-Management ZERSTÖRT die ML-Label-Qualität der Sammel-Trades
  (Label soll die ENTRY-Entscheidung bewerten, nicht das Manager-Verhalten).
- FIX (services/ai_trade_manager.py, minimal-invasiv):
  1. Sammel-Trades (data_collection=True) komplett vom KI-Management ausgenommen:
     review()-Query filtert sie raus + apply_action blockt source=ki hart
     ("Datensammel-Trade … läuft unangetastet bis SL/TP"). Manuelle Eingriffe (User im UI,
     source=manual/manuell) bleiben erlaubt. AutoTrader-eigene Mechanik (Breakeven, TP1,
     Trailing) läuft unverändert weiter.
  2. SL-Ratchet-Guard für ALLE KI-SL-Anpassungen: neue Funktion min_sl_gap(entry,
     initial_sl, mark) — neuer KI-SL muss mind. max(30% der initialen SL-Distanz,
     0,1% vom Kurs) vom aktuellen Kurs entfernt bleiben, sonst blocked. Gewinn-Sicherung
     bleibt möglich (Abstand wird zum KURS gemessen — ist der Kurs weggelaufen, ist
     Breakeven-SL erlaubt). System-Prompt des Managers um die Regel ergänzt
     (sonst schlägt die KI weiter Geblocktes vor).
  3. Hebel-Senken im Trade (set_leverage): bewusst NICHT entfernt — auf Paper reiner
     Optik-Churn (PnL unverändert, nur Margin/Liq), auf Live legitim (Margin freimachen,
     Liq-Abstand). Die Reflex-Fälle betrafen fast nur Sammel-Trades → durch Fix 1 weg.
     Abschaltbar bleibt es über allow_margin im Trade-Manager-Setup.
- Getestet: tests/test_ram_and_manager_guards.py (9 PASS: min_sl_gap-Mathematik,
  dc-Block ki vs. manuell erlaubt, Review-Query-Filter, Ratchet-Block zu eng,
  Adjust mit genug Abstand ok, manuell ohne Gap-Limit). Regressionen:
  test_swing_and_profit_lock/test_review_bugfix_e2e/test_fix_custom_ai_trades 87 passed
  (3 Fails in test_ai_lab = Provider-Chain-Tests, umgebungsbedingt/pre-existing,
  ohne Änderungen identisch); Phase-4-Tests 7/7 + Roundtrip 7/7 (solo; Kombi-Lauf-Fail
  = bekannte Test-Isolation).

## Prod-Migration 0.5 + Reward-Backfill — NOCH OFFEN (User weiß nicht wie, 15.06.)
Auf Render (Backend-Service → Shell) in dieser Reihenfolge:
1. `python scripts/migrate_0_5_result_truth.py` (Dry-Run, Ausgabe prüfen)
2. `python scripts/migrate_0_5_result_truth.py --apply`
3. Als Admin eingeloggt: `POST /api/ai/rewards/backfill?include_cleared=true`
   (oder per curl mit Admin-Token). Das passiert NICHT automatisch.

## Aktueller Stand (Datum: 2026-06-14, 5. Handover)
- Aktive Phase: Phase 6 (Gate-Dashboard + Auto-Retrain) — UMGESETZT + getestet
  (iteration_21: 100% Backend 17/17 + Frontend 8/8). Wartet auf User-Review + Render-Deploy.
- Aktueller Schritt: Phase 5 (Gate v1 Shadow) und Phase 6 (UI-Dashboard im KI-Labor,
  Tab "Gate v1" + Auto-Retrain-Scheduler) fertig. User kann jederzeit deployen — je
  früher, desto früher sammelt Prod Shadow-Predictions + Sammel-Trades.
  Danach: 2–4 Wochen Shadow-Beweis sammeln (Auto-Retrain läuft), dann Gate-Aktivierung
  NUR nach erfülltem Kriterium + expliziter User-Freigabe. Parallel möglich: Regime-Brücke
  (P1, =0.7) oder Sizer-Vorarbeit.
- % geschafft grob: 62%
- 5. Handover (2026-06-14): Repo AntonHeinrich05/ml-implementation-ML_REBUILD_STATUS.md
  Branch 0.65 nach /app geklont, .env selektiv übernommen (wie gehabt: LLM-Keys, ADMIN,
  PROD_MONGO_URL nur lesend; Bitunix/Telegram/Supabase bewusst NICHT), App lauffähig
  verifiziert (Backend healthy, Frontend lädt, Admin-Login ok, Prod-Lesezugriff ok).
  User-Antworten: Gate-v1-Datenbasis = Prod-Signale+Ghost+Decisions (freigegeben);
  Trade-Schwund 167→96 war der Kumpel (bestätigt, RCA geschlossen).
- 4. Handover (2026-08-14): Repo AntonHeinrich05/ml-implementation-ML_REBUILD_STATUS.md
  Branch 0.6 nach /app geklont, .env selektiv übernommen (LLM-Keys, ADMIN, PROD_MONGO_URL/
  PROD_DB_NAME nur lesend; Bitunix/Telegram/Supabase bewusst NICHT), App lauffähig
  (Backend healthy, Frontend lädt, Prod-Lesezugriff verifiziert).
- 3. Handover (2026-08-14): Repo https://github.com/dean06greif-ai/14.8-timeframe-Update-try
  nach /app geklont (enthält alle Fixes 0.1–0.5 + ai_rewards-Fix verifiziert), .env selektiv
  übernommen (wie unten), App lauffähig (Backend healthy, Admin-Login ok, Frontend lädt).
- Prod-Lesezugriff erneut verifiziert (2026-08-14): auto_trades=96 (⚠️ am 13.08. waren es
  noch 167 → 71 Trades seither aus Prod verschwunden, vermutlich "Verlauf/Trades löschen"
  im UI – beim User ansprechen!), ai_decisions=38.718, ai_rewards=3 (Hook zeichnet in Prod
  jetzt auf – Beweis, dass der Reward-Hook nach Deploy funktioniert), signals=2.464.
- Arbeitsmodus (User-Wunsch): Nach JEDER Phase stoppen und Freigabe abwarten.

## Umgebung & echte Daten (Stand 2026-08-13)
- Prod-.env vom User erhalten. Übernommen nach /app/backend/.env: alle LLM-Keys
  (Groq/Gemini/OpenRouter/Mistral/Cerebras + Backups, alle 5 Provider verifiziert aktiv),
  ADMIN_USER/ADMIN_PASSWORD (→ /app/memory/test_credentials.md), PROD_MONGO_URL/PROD_DB_NAME
  (NUR LESEND für Analysen — die laufende Dev-App bleibt auf lokaler MONGO_URL!).
- BEWUSST NICHT übernommen: BITUNIX_API_KEY/SECRET (keine echten Orders aus Dev),
  TELEGRAM_* (kein Spam an echten Chat), SUPABASE_* (KI-Gedächtnis/Lektionen liegt in
  Supabase! Dev darf Prod-Gedächtnis nicht verschmutzen).
- Echte Prod-Zahlen (Skript /app/scripts/prod_db_stats.py):
  DB 128,7 MB Daten / 48,2 MB Storage; auto_trades=167 (alle closed, Zeitraum 21.07.–12.08.,
  davon ai_trader=28); ai_decisions=35.993 (nur 304 mit outcome); signals=2.183 (622 mit result,
  549 unresolved); ai_market_snapshots=20.000 (Cap exakt erreicht, ältester 02.08. → ~10-Tage-
  Fenster bestätigt); ai_rewards=0 (!) — Reward-System hat in Prod NIE etwas aufgezeichnet;
  ghost=27; backtests=278; optimizer_runs=145.
  Konsequenz: ML-Datenbasis real = ~300–650 gelabelte Beispiele, nur 28 echte KI-Trades →
  Datensammel-/Simulations-Strategie ist kritisch, Meta-Modell v1 braucht signals+ghost+Sim.

## Kontext (einmal lesen, dann Abschnitte unten)
Briefing des Users: Hybrid-Architektur (LLM bleibt Entscheider, ML predictet parallel
Erfolgswahrscheinlichkeit; Eskalation Shadow → Sizer → Gate, jede Stufe nur mit expliziter
User-Freigabe; Haupt-Agent hat Gate-vor-Sizer empfohlen, User-Entscheidung offen).
Beehive (Multi-LLM-Rollen) bleibt, Modell wird als Feature geloggt. Scope: alle 22 Instrumente
(Haupt-Agent empfiehlt Krypto-only für Modell v1, User-Entscheidung offen).
Phase-0-Reihenfolge (vom Haupt-Agent umsortiert, vom User-Briefing abweichend begründet:
irreversibler Datenverlust zuerst):
  0.1 Snapshot-Prune zeitbasiert (stoppt Datenvernichtung)
  0.2 entry_market_snapshot am Trade + an der Decision im Entry-Moment
  0.3 Swing-Labeling DB-basiert (statt RAM open_signal_evals + Midnight-Reset)
  0.4 prompt_version + LLM-Modell an jede Entscheidung binden
  0.5 Ergebnis-Wahrheit vereinheitlichen (kanonisch: Vorzeichen realized_pnl inkl. Fees) + Migration
  0.6 /tmp-Candle-Cache → persistenter Pfad (Render; blockiert auf User-Info zu Persistent Disk)
  0.7 Regime-Brücke Welt A (Observer-Heuristik) → Welt B (regime_engine v2): erst Kosten-Nutzen-Analyse
  0.8 Beehive: keine Modell-Änderung nötig (Analyse 2026-08-13: alle Rollen Free-Tier, Presets gut);
      Rest = UI-Kosten-Panel (Backend existiert: ai_token_usage + GET /api/ai/token-usage)
Danach: Anti-Overfitting-Pipeline (Purged WF + Embargo, Permutationstests, Monte-Carlo/GBM,
Kalibrierung), Meta-Labeler (XGBoost/LightGBM), UI (Kalibrierungs-Chart = Placebo-Detektor,
Hybrid-Scoreboard, Robustheits-Dashboard, SHAP pro Trade).
Stopp-Metrik fürs Tuning: Brier-Score + ökonomischer Uplift (nicht Kalibrierung allein).
Verbote: Paper/Live/Backtest-Trades mischen, geshuffelte CV, Look-Ahead, Deep Learning <10k Trades.

## Erledigt
- [0.1] Snapshot-Prune zeitbasiert — 2026-08-13 — `services/ai_market_observer.py`:
  Anzahl-Cap (20k, ≈10 Tage Fenster) ersetzt durch SNAPSHOT_RETENTION_DAYS=200 (zeitbasiert)
  + Notbremse MAX_SNAPSHOTS=500k; `core/indexes.py`: neuer Index `snapshots_ts` (ts aufsteigend),
  damit der Prune keinen Collection-Scan macht — getestet: Skript-Test gegen lokale Mongo
  (alte/neue Docs eingefügt, Prune löscht nur >200 Tage; Notbremse geprüft) + Backend-Neustart sauber.
  Hinweis: finaler Retention-Wert hängt an User-Antwort zum Mongo-Tier (200d ≈ 140–160 MB bei 22 Symbolen).
- [0.2] entry_market_snapshot am Trade + Decision — 2026-08-13 —
  `services/ai_market_observer.py`: neue Methode `entry_snapshot(symbol)` (frisch aus Kerzen-Puffer,
  Fallback letzter 15-min-Snapshot); `services/bitunix_trade.py` (~Z.1115): Trade-Doc-Feld
  `entry_market_snapshot` (source=signal_candles|live|last_snapshot, ts, features inkl. regime);
  `services/ai_engine.py` (~Z.1702): jede ai_decision (auch HOLD → kontrafaktischer Kontext!)
  bekommt `entry_market_snapshot` — getestet: /app/tests/test_fix_0_2_entry_snapshot.py
  (4 PASS inkl. echter on_signal-Paper-Trade) + pytest observer/snapshot 9/9.
- [0.3] Swing-Labeling über Tagesgrenzen — 2026-08-13 —
  `core/pipeline.py`: SIGNAL_EVAL_MAX_DAYS=14, evaluate_open_signals mit Expiry statt
  Tagesgrenze, eval-Einträge tragen ts; `core/scheduler.py`: Mitternachts-`clear()` entfernt;
  `server.py`: Rehydrierung lädt unresolved Signale der letzten 14 Tage (vorher: nur heute) —
  getestet: /app/tests/test_fix_0_3_swing_labeling.py (3 PASS: 3d-Swing win, 5d-Swing loss,
  20d expired bleibt None) + pytest-Subset 47 passed (3 Fails pre-existing, via git stash verifiziert).
  Bekannte Restlücke: Preisbewegungen WÄHREND Server-Downtime werden nicht nachträglich
  ausgewertet (kein Kerzen-Backfill) — bewusst vertagt, siehe Tech-Debt.

- [0.4] prompt_version an jede ai_decision — 2026-08-13 —
  `services/ai_master_prompt.py`: `MasterPromptStore.version_hash()` (sha256 über
  text+lesson_policy+normalisierte rules, 10 Hex-Zeichen; inhaltsbasiert → erkennt Reverts,
  umgebungsunabhängig im Gegensatz zur Integer-`version`); `services/ai_engine.py`:
  `ANALYSIS_PROMPT_HASHES` (lean/full, ändern sich automatisch bei Prompt-Code-Änderung) +
  `prompt_version_info(variant)`; `run_analysis` bindet an jede Decision das Dict
  `prompt_version = {analysis, variant, master, master_v, combined}` (combined = ML-Groupby-Key,
  Format "lean-<hash10>+<hash10>") — getestet: /app/tests/test_fix_0_4_prompt_version.py
  (3 PASS: Hash-Determinismus+Revert, lean/full-Struktur, Integration run_analysis mit
  gemocktem LLM → Decision-Doc trägt prompt_version) + pytest -k "master_prompt or prompt"
  49 passed; 5 Fails in test_ai_team_api/regression identisch ohne Änderungen (git stash
  verifiziert, pre-existing/umgebungsbedingt).
- [0.5] Ergebnis-Wahrheit vereinheitlicht — 2026-08-13 — kanonisch = Vorzeichen
  auto_trades.realized_pnl (inkl. Fees, wird beim Close ohnehin so klassifiziert):
  `services/bitunix_trade.py` `_after_close` (EIN Hook, alle 5 Close-Pfade laufen durch)
  schreibt result kanonisch ans Signal (result_source=trade_pnl, trade_id, result_ts);
  `core/pipeline.py` evaluate_open_signals überschreibt trade-gelabelte Signale nie mehr
  (Update-Filter result_source!=trade_pnl; TP1-Touch-Labels bekommen result_source=tp1_touch;
  update_performance nur noch wenn wirklich gelabelt wurde); `services/ai_learning.py`
  sync_outcomes: Trade-Branch setzt outcome kanonisch (outcome_source=trade_pnl, gewinnt
  immer), tp1-Branch stuft nie zurück. Migration: `backend/scripts/migrate_0_5_result_truth.py`
  (Dry-Run Default, --apply verweigert HART gegen PROD_MONGO_URL; Prod-Migration später auf
  Render ausführen: dort ist MONGO_URL die Prod-DB). PROD-DRY-RUN-ERGEBNIS (echt, nur lesend):
  96 Signale → kanonisch (Flips: 15 win→loss!, 6 loss→win, 27 unlabeled→Label), 28 Decisions
  (7 Flips, 7 neue Labels), Backfill result_source: 656 Signale / 293 Decisions tp1_touch.
  Getestet: /app/tests/test_fix_0_5_result_truth.py (5 PASS inkl. Prod-Schreibschutz).
- [ai_rewards-RCA + Fix] — 2026-08-13 — URSACHE Prod leer: (a) am 13.08. 07:03 UTC wurde
  DELETE /api/ai/rewards ausgeführt (settings.ai_rewards_state.cleared_at gesetzt; Admin-only —
  vermutlich User im Lern-Panel, beim User nachgefragt), alle 28 KI-Trades schlossen 11.–12.08.
  DAVOR, seither kein Close mehr; (b) Alt-Backfill lief nur bei KOMPLETT leerer Collection und
  nie nach cleared_at → Lücken für immer. Hook selbst funktioniert (Dev-Beweis: Close → Reward).
  FIX `services/ai_rewards.py`: backfill_missing() lückenfüllend + idempotent (Dedupe trade_id),
  respektiert cleared_at (nur Trades danach), include_cleared=True hebt Sperre auf und bewertet
  historisch; ensure_backfill = 10-min-Wrapper; _regime_for-Priorität jetzt
  entry_market_snapshot (Entry-Regime, Fix 0.2) > Snapshot<=closed_at > Live-Observer
  (P1 Tech-Debt "Regime zum Close-Zeitpunkt" gelöst). Neuer Endpoint
  POST /api/ai/rewards/backfill?include_cleared=true (require_admin; für Prod: nach Deploy
  einmal aufrufen → bewertet die 28 historischen KI-Trades nach).
  Getestet: /app/tests/test_fix_rewards_backfill.py (4 PASS).

## In Arbeit
- (nichts)

## Fix 0.6 ERLEDIGT (14.08.) — Candle-Cache Render-Neustart-fest + Lösch-Schutz
- Rebuild-on-Boot: `services/boot_backfill.py`, gestartet non-blocking in server.py
  (nach open_signal_evals-Rehydrierung). Lädt beim Boot 14 Tage 1m-Kerzen je Symbol
  (BOOT_BACKFILL_DAYS, Pause BOOT_BACKFILL_PAUSE=1.5s, abschaltbar BOOT_BACKFILL_ENABLED=0).
  Priorität: Symbole mit offenen Signal-Evals/Trades → TOP_10_COINS → Rest. Rate-limit-
  sicher (sequenziell + history_sources-Pacing). Kein Mongo-Kerzen-Store (512-MB-Budget).
- P1-Tech-Debt GESCHLOSSEN: Downtime-Labeling. Signale, deren TP1/SL während einer
  Downtime erreicht wurde, werden aus dem nachgeladenen Kerzenpfad gelabelt
  (result_source=tp1_touch, result_backfilled=true). trade_pnl (kanonische Wahrheit)
  wird NIE überschrieben; bereits gelabelte Signale nie angefasst. Ambiguität
  (TP1+SL in derselben 1m-Kerze) → konservativ loss + result_ambiguous=true
  (ML kann diese Labels ausschließen).
- Lösch-Schutz/Audit-Log: `core/audit.py` + Collection audit_log (Index audit_ts).
  Protokolliert: analytics_clear (Range/Scope/Anzahl), ai_rewards_clear, ai_chat_clear,
  strategy_delete — je mit ts/user/IP/Browser. GET /api/audit-log (Admin). Frontend:
  Clear-Modal zeigt "Letzte Löschungen (Audit-Log)"; Reward-Löschen-Confirm erwähnt Audit.
- Endpoints: GET /api/system/boot-backfill (Status), POST /api/system/boot-backfill/run
  (Admin, manueller Re-Run).
- Tests: tests/test_fix_0_6_boot_backfill.py (9 grün): Label-Logik long/short/ambig/
  Zeitfenster, DB-Labeling mit trade_pnl-Vorrang, Audit-Schreiben. E2E verifiziert:
  Boot-Backfill lief nach Neustart (22 Symbole, 14d, 0 Fehler), Audit-Eintrag nach
  analytics/clear, 401 ohne Admin.

## 🧪 So testest du es selbst (Fix 0.6)
1. Backend neu starten → sofort erreichbar (Backfill blockiert nicht).
2. `GET /api/system/boot-backfill` → state running→done, symbols_done zählt hoch,
   cache.total_candles wächst (~20.160 Kerzen/Symbol = 14 Tage).
3. Verlauf-löschen-Dialog (Analytics → Papierkorb): unten erscheint
   "Letzte Löschungen (Audit-Log)" mit Zeit/User/IP nach jeder Löschung.
4. `GET /api/audit-log` mit Admin-Token → JSON-Liste; ohne Token → 401.

## RCA Trade-Schwund Prod 167→96 (14.08., nur lesend analysiert)
- Befund: Trades+Signale vom 11.–12.08. fehlen KOMPLETT (Tageszählung springt
  10.08.→13.08.), ältere Trades ab 21.07. unversehrt; die 28 KI-Trades (geschlossen
  11.–12.08.) sind darunter. ai_rewards_state.cleared_at=13.08. 07:03 UTC (= der
  bekannte "Belohnungsdaten löschen"-Klick 09:03 deutscher Zeit).
- Schluss: Muster passt exakt zu POST /api/analytics/clear ("Verlauf löschen") mit
  Zeitraum 24–48h am 13.08. — KEIN Deploy-Nebeneffekt (Deploys löschen keine Mongo-
  Daten, und der Schnitt ist zeitraum-scharf). Beide Endpoints erfordern Admin-Login
  → jemand mit Admin-Zugang (User war es laut eigener Aussage nicht → Kumpel sehr
  wahrscheinlich, gleicher Zeitpunkt wie Reward-Löschung).
- Konsequenz: Ab jetzt protokolliert das Audit-Log jede Löschung (wer/wann/was/IP).
  In Prod greift das nach dem nächsten Render-Deploy.

## Phase 4 UMGESETZT (14.08.) — Self-Tuning-Guard + Paper-Datensammel-Modus
User-Freigabe: Guard-Variante (a) mit einstellbarer Spanne; Sammel-Zahlen = Agent-Vorschlag;
Prod-Heilung automatisch ("mach das").
### Self-Tuning-Guard (Fix für den RCA-Ratchet)
- Neue Engine-Config-Keys (NUR Trader, nicht in KI-Whitelist): `tune_conf_min`=55,
  `tune_conf_max`=75, `tune_cooldown_max`=45. UI: KI-Panel → Setup ("KI-Spanne Konfidenz",
  "KI-Limit Cooldown", testids ai-tune-conf-min/max-select, ai-tune-cooldown-max-select).
- `ai_engine._tuning_guard(changes)`: prüft ENGINE-Änderungen an min_confidence/cooldown_min
  gegen die Spanne. Greift in `_handle_config_changes` (nach Makro-Gate, vor Auto-Apply)
  UND in `review_parked_proposals` → außerhalb der Spanne wird der Wunsch IMMER nur
  Vorschlag (needs_confirmation + guard_reason), nie auto-angewendet. Innerhalb der
  Spanne bleibt die KI voll autonom.
- Boot-Heilung `_normalize_auto_tuned()` (in load_config): Liegt der AKTUELLE Wert außerhalb
  der Spanne UND wurde er nachweislich von der KI gesetzt (auto_applied-Proposal mit exakt
  diesem Wert, noch nicht guard_normalized), wird er auf die Spannen-Grenze zurückgeholt +
  Governance-Meldung im KI-Feed. Manuell gesetzte Werte werden NIE angefasst; jedes Proposal
  max. 1x normalisiert. → Heilt Prod (min_confidence 85→75) automatisch beim nächsten Deploy.
### Paper-Datensammel-Modus (Phase 4)
- Neue Engine-Config-Keys: `collection_enabled`=True, `collection_min_confidence`=60,
  `collection_cooldown_min`=30, `collection_max_same_direction`=5, `collection_max_per_coin`=2.
  UI: KI-Panel → Setup ("Datensammlung (Paper)", "Sammel-Konfidenz", "Sammel-Cooldown",
  "Sammel-Trades/Coin", testids ai-collection-*-select).
- Fluss: `run_analysis` → Entscheidung LONG/SHORT unter Live-Schwelle, aber ≥ Sammel-Schwelle
  → `_emit_signal(dec, collection=True)`: force_paper=True, data_collection=True,
  collection_reason (below_live_conf|live_blocked), eigener Cooldown (_last_collection_ts).
  check_day (Tages-Risiko) wird übersprungen (nur Paper); Diversifikation läuft mit
  collection_max_same_direction; Playbook-Setup-Sperren gelten NICHT (gesperrte Setups als
  Paper-Daten sind fürs ML wertvoll); Anti-Stacking-Guard gilt weiterhin (keine identischen
  Doppel-Entries). Keine Governance-Chat-Spam-Einträge für geblockte Sammel-Signale.
- `core/pipeline.process_signal`: mode=off-Coins lassen Sammel-Signale durch (immer Paper);
  keine Telegram-Signale für Sammel-Signale.
- `bitunix_trade.on_signal`: collection → eff_mode=paper erzwungen (auch bei Coin AUS),
  eigene Slots (collection_max_per_coin, zählt nur data_collection=True) getrennt von
  Live-Slots (zählt nur data_collection≠True); Trade-Doc bekommt data_collection=true +
  collection_reason; KEINE Telegram-Meldung bei Open. Learning/Rewards laufen normal mit
  (User-Wunsch: KI soll daraus lernen), ML-Training gewichtet über das Flag separat.
- Frontend: Trade-Karte zeigt Badge "DATEN" (badge-collection, testid
  trade-collection-badge-{id}) bei data_collection-Trades.
- Tests: /app/tests/test_phase4_collection_mode.py (7 PASS: Guard-Spanne, update_config,
  Boot-Heilung inkl. "manuell bleibt", Guard in handle+review, on_signal off-Bypass +
  Flag, Slot-Trennung, Emission+Cooldown). Regressionen: test_ai_governance/test_ai_trader/
  test_improvements_0_6 41 passed; test_iter_ai_supervisor_autonomy/test_iter2_supervisor
  32 passed; test_iter7_watchdog 15/15 solo grün (1 Fail nur im xdist-Kombilauf =
  Test-Isolation, ohne Änderungen reproduzierbar).
- Erwartung Prod nach Deploy: min_confidence 85→75 (Boot-Heilung), Sammel-Trades starten
  automatisch (collection_enabled Default True) → ~30–60 Paper-Trades/Tag.

## RCA: Warum macht der KI-Trader kaum Trades? (14.08., nur lesend analysiert)
Skript: /app/scripts/analyze_low_trades.py (read-only gegen PROD_MONGO_URL).
- HAUPTURSACHE (Self-Strangling-Loop): autonomy=auto → die KI hat ihre eigene
  min_confidence schrittweise hochgeschraubt und selbst angewendet (ai_proposals,
  alle auto_applied): 70 (11.08. 22:11) → 75 (12.08. 01:14) → 80 (12.08. 09:03) →
  85 + cooldown_min 45→60 (12.08. 22:00) → 80 (13.08.) → 85 (13.08. 17:39, aktueller Stand).
  Die Prompts kalibrieren A-Setups aber auf Konfidenz 70–85 → Schwelle 85 filtert fast
  alles weg. Zahlen 14d: LONG/SHORT-Entscheidungen ≥65 = 1116 (~80/Tag), ≥70 = 607,
  ≥75 = 392, ≥80 = 150, ≥85 = nur 100 (~7/Tag, vor weiteren Guards).
  Sichtbar: signaled/Tag fiel von ~50 (07.08.) auf 2–4 (12.–13.08.) auf ~0.
- SEKUNDÄR: (a) 93% HOLD (19.125/20.519 Decisions 14d) – Prompt-Disziplin, groß aber
  by design; (b) cooldown_min 60 pro Coin; (c) 3 Coins mode=off (ai_trader-Coin-Configs:
  10 live / 5 paper / 3 off); (d) Diversifikations-Guards (max_same_direction=3,
  correlation_guard) – alle nachrangig gegenüber der 85er-Schwelle.
- VERSTÄRKER (Optik): Am 13.08. wurden per analytics/clear (scope=strategy) ALLE
  ai_trader-Signale+Trades vor dem 14.08. gelöscht (nur noch 2 Signale + 1 Trade vom
  14.08. in Prod; andere Strategien haben 1.191 Signale in 14d). Historie sieht dadurch
  noch leerer aus, als die Aktivität war. Passt zum bekannten Lösch-Vorfall (Kumpel).
- NICHT Ursache: Trading-Session (keine Sessions konfiguriert = 24/7), MasterPrompt-Rules
  (alle 0/aus), Governance-Blocks (0 in 14d), Smart-Skip (greift nur ohne Bewegung).
- Sofort-Hebel OHNE Deploy: min_confidence in Prod im KI-Panel manuell auf ~70 zurücksetzen.
- Fix-Vorschlag (wartet auf Freigabe): Self-Tuning-Guard — ENGINE-Änderungen an
  min_confidence/cooldown_min in autonomy=auto nur noch innerhalb sicherer Leitplanken
  auto-anwenden (min_confidence 55–75, cooldown ≤45), alles darüber wird Vorschlag
  (needs_confirmation). Verhindert den Ratchet dauerhaft, KI bleibt sonst autonom.

## Phase-4-Vorschlag (konkrete Zahlen, 14.08., warten auf User-Freigabe)
Datenbasis 14d (Prod, read-only): LONG/SHORT ≥60 = 1.282 (~92/Tag), Verteilung über 20
Symbole (Top: OIL 204, SILVER 155, USDJPY 152, GOLD 105, SOLUSDT 97 – Nicht-Krypto dominiert
die hohen Konfidenzen!). Vorschlag:
- collection_min_confidence = 60 (separat von live-min_confidence, das unverändert bleibt)
- Alle 22 Symbole sammeln (auch Nicht-Krypto: Modell v1 ist krypto-only, aber Daten
  kosten nichts und data_collection-Trades werden ohnehin separat gewichtet/gefiltert)
- collection_cooldown_min = 30 (statt 60), Guards (Diversifikation) im Sammel-Modus lockern:
  max_same_direction 5
- Jeder Sammel-Trade: mode=paper erzwungen, data_collection=true, collection_reason
  (below_live_conf | coin_paper_slot), niemals live, niemals Kapital
- Erwartung: ~30–60 Paper-Trades/Tag (statt heute ~0–2) → ML-Datenbasis wächst in 4 Wochen
  um ~1.000+ gelabelte Trades

## Phase 5 UMGESETZT (2026-06-14) — Gate v1 Shadow-Modus
User-Freigabe: Datenbasis = Prod-Signale + Ghost + Decisions (nur lesend), krypto-only.
### Modul services/ml_gate.py (Neubau, ersetzt NICHT das alte ml_lab — läuft parallel Shadow)
- Dataset-Builder krypto-only (TOP_10_COINS): ai_decisions (outcome, Gewicht 1.0 trade_pnl /
  0.8 sonst, ×0.85 bei data_collection), signals (result, ohne ai_trader-Duplikate, ohne
  result_ambiguous, Gewicht 0.7/0.6), ai_ghost_trades (0.5). Features aus
  entry_market_snapshot (Fix 0.2), Fallback nächster 15-min-Snapshot (≤45 min).
  21 Features inkl. Regime-Encoding (trend/vol/breakout), has_market_state + Quell-Flags.
- Anti-Overfitting (bewusst anders als altes ml_lab): Purged Walk-Forward (5 zeitliche
  Blöcke, Train STRIKT vor Test-Start) + 24h-Embargo, Platt-Kalibrierung auf
  Out-of-Sample-Predictions, Brier vs. Baseline (konstante Win-Rate) als Stopp-Metrik,
  konservative XGBoost-Fixparams (depth 3, kein Optuna bei <1k Samples).
- Versionierung: jedes Training = neues Doc in ml_gate_models (version++, nie überschrieben).
- Shadow-Hook (ai_engine.run_analysis ~Z.1843): jede LONG/SHORT-Decision bekommt
  gate_shadow = {p_win, raw, model_version, threshold, would_block} — nie blockend,
  nie werfend (None bei Fehler/fehlendem Modell). HOLD bekommt keins (keine Seite).
- Kontrafaktik: Da nichts geblockt wird, laufen auch would_block-Trades real zu Ende
  (Paper/Collection) → shadow_report wertet echte Outcomes aus: % geblockte Verlierer/
  Gewinner, ökonomischer Uplift (Ø-R passed vs. alle, R = realized_pnl/risk via
  decision_id-Join), Brier vs. Baseline, Threshold-Sweep 0.30–0.60 + die 3
  Aktivierungskriterien als criteria-Dict.
- Endpoints: GET /api/ml/gate/{status,dataset,models,report}; POST /api/ml/gate/{train,
  settings} (Admin). Settings: threshold (Default 0.45), shadow_enabled.
- Indizes (P2-Backlog mitgenommen): ai_decisions decisions_ts + decisions_outcome_ts.
### Erstes echtes Training (Prod nur lesend, 14.06.)
- v1: 970 Samples (205 Decisions / 760 Signale / 5 Ghost, 806 mit Marktzustand),
  Win-Rate 39.8%. OOS (809 Samples, 5 Folds): AUC 0.552, Brier kalibriert 0.2393 <
  Baseline 0.2541 (beats_baseline=true), roh 0.3183. Top-Features: range_pos 16%,
  weekday 7.9%, side_long 7.1%, atr_pct 6.8%, rsi 6.5%.
- EHRLICHE EINORDNUNG: AUC 0.552 = noch schwache Trennschärfe (erwartbar bei 970
  verrauschten Samples). Kalibrierung funktioniert (Bins 0.3–0.5 nahe Realität).
  Aktivierungskriterien werden damit noch NICHT erreichbar sein — genau dafür ist
  Shadow da. Phase-4-Sammel-Trades vergrößern die Datenbasis (~1.000+/Monat erwartet);
  regelmäßig neu trainieren (POST /api/ml/gate/train, versioniert).
- Tests: /app/tests/test_phase5_gate_shadow.py (7 Unit: Regime-Encoding, Feature-Row,
  Label/Gewichte, Purged-WF ohne Look-Ahead/mit Embargo, Training schlägt Baseline auf
  synthetischem Muster, Shadow-Predict nie-werfend, Report-Kriterien) +
  /app/backend/tests/test_phase5_ml_gate_api.py (11 API, Testing-Agent) — 18/18 grün,
  iteration_20.json 100%. Regressionen: Phase-4-Tests 7/7 solo grün (Kombi-Lauf-Fail =
  bekannte Test-Isolation via Dev-DB-Anti-Stacking, keine Regression), ai_trader/
  ai_governance 28 passed, Phase-4-API-Roundtrip 14 passed.

## Phase 6 UMGESETZT (2026-06-14) — Gate-Dashboard + Auto-Retrain
- Auto-Retrain-Scheduler: MLGate.tick() im Engine-Loop registriert (ai_engine ~Z.3226,
  gleiche Mechanik wie ml_lab/observer; läuft auch bei KI aus). Throttle 30 min.
  _retrain_due (rein/testbar): Erst-Training sobald >=MIN_SAMPLES(120) gelabelt;
  Retrain bei >=retrain_min_new (Default 50) neuen gelabelten Samples ggü. letztem
  Training; täglich um retrain_hour_berlin (Default 4 Uhr, 1h nach ml_lab) wenn
  letztes Training >20h her. Settings: auto_retrain/retrain_hour_berlin/retrain_min_new
  (geklemmt, persistiert in settings/ml_gate_settings). Jedes Training bleibt
  versioniert (trigger-Feld: manuell/auto (…)).
- UI: neuer Tab "Gate v1" im KI-Labor (frontend/src/components/GateShadowPanel.js,
  eingebunden in AILabPanel.js): SHADOW-Badge, Modell-Meta (Version/Samples/AUC/
  Brier-vs-Baseline-Chip), Settings (Schwelle p(win), Shadow-Logging, Auto-Retrain,
  Retrain-ab-N), Kriterien-Ampel (5 Aktivierungskriterien aus /report), Kontrafaktik-
  Block mit Threshold-Sweep-Tabelle, Kalibrierungs-Chart (vorhergesagt vs. tatsächlich
  je Bin), Modell-Versionsliste. Empty-State erklärt, dass Shadow-Daten erst nach
  Render-Deploy/KI-Läufen entstehen. Auto-Refresh 30s.
- Tests: /app/tests/test_phase6_gate_retrain.py (4 Unit: Erst-Training, neue Samples,
  Tageszeit+20h-Sperre, Off/Lock) + Testing-Agent iteration_21 (Backend 17/17 inkl.
  Settings-Persist/Clamp/Admin-Guard, Frontend 8/8 Flows inkl. Threshold-Persist und
  Live-Training v3 über den Button) — 100%, keine Bugs. Dev-DB hat ml_gate_models v1–v3.

## 🧪 So testest du es selbst (Phase 6, in der Dev-Preview)
1. Admin-Login → Strategien → KI Trader → Parameter → "KI-Labor" → Tab "Gate v1".
2. Du siehst Modell-Metriken, die 5-Kriterien-Ampel (in Dev noch rot – erwartbar,
   keine bewerteten Shadow-Entscheidungen), Kalibrierungs-Chart und Versionsliste.
3. "Gate jetzt trainieren" erzeugt eine neue Version (nie überschrieben).
4. Auto-Retrain-Haken + "Retrain ab neuen Samples" steuern den Scheduler.

## 🧪 So testest du es selbst (Phase 5, in Dev-Preview; auf Render erst nach Deploy)
1. GET /api/ml/gate/status → mode=shadow, model_loaded=true, version + Metriken.
2. GET /api/ml/gate/dataset → source=prod_readonly, ~970 Samples krypto-only.
3. Mit Admin-Token: POST /api/ml/gate/train → neue Version (inkrementiert), Metriken
   inkl. beats_baseline; GET /api/ml/gate/models zeigt alle Versionen.
4. GET /api/ml/gate/report?days=28 → in Dev noch evaluated=0 (frische DB); auf Render
   füllt sich das nach Deploy automatisch, sobald Decisions mit outcome + gate_shadow
   existieren (Threshold-Sweep + criteria zeigen den Weg zur Aktivierung).
5. Hinweis: In Dev ist der KI-Trader aus → gate_shadow an echten Decisions entsteht
   erst, wenn er läuft (oder auf Render nach Deploy).

## Als Nächstes (Phasen-Plan, je Phase mit User-Freigabe)
- [Phase 3 = Fix 0.6] ✅ ERLEDIGT 14.08. (siehe Abschnitt oben)
- [Phase 4] ✅ ERLEDIGT 14.08. (Self-Tuning-Guard + Paper-Datensammel-Modus)
- [Phase 5] ✅ UMGESETZT 14.06. (Gate v1 Shadow, siehe Abschnitt oben) — wartet auf
  User-Review + Render-Deploy; danach Shadow-Daten 2–4 Wochen sammeln, periodisch
  neu trainieren.
- [Phase 6] ✅ UMGESETZT 14.06. (Gate-Dashboard im UI + Auto-Retrain-Scheduler,
  iteration_21 100%) — wartet auf User-Review + Render-Deploy.
- [Phase 7, Vorschlag] Regime-Brücke (=0.7, P1: Observer classify_regime vs.
  regime_engine v2 vereinheitlichen — Gate-Feature-Qualität profitiert direkt) ODER
  Shadow-Beweis abwarten. Entscheidung liegt beim User.
- [Später, je mit User-Freigabe] Gate aktiv (nur wenn Aktivierungskriterium über
  4 Wochen erfüllt) → Sizer → regime-abhängige Schwellen.

## Prod-Migration 0.5 + Reward-Backfill — FREIGABE ERTEILT (User, 14.08.)
Ausführung durch User nach seinem nächsten Render-Deploy, Reihenfolge ZWINGEND:
1. Auf Render (Shell/Job): `python scripts/migrate_0_5_result_truth.py` (Dry-Run, Ausgabe prüfen)
2. `python scripts/migrate_0_5_result_truth.py --apply`
3. `POST /api/ai/rewards/backfill?include_cleared=true` (Admin-Auth; bewertet historische
   KI-Trades nach — erst NACH Migration, damit Rewards kanonische Ergebnisse nutzen)
Hinweis: ai_rewards=3 in Prod (14.08.) zeigt, dass der Hook seit Deploy live aufzeichnet.

## Gate-Aktivierungskriterium (dokumentiert 14.08., Aktivierung NUR mit expliziter User-Freigabe)
Gate darf von Shadow auf aktiv, wenn über rollierende 4 Wochen mit ≥150 bewerteten
Entscheidungen ALLE drei gleichzeitig gelten:
1. Hätte ≥35% der Verlierer geblockt bei ≤15% geblockten Gewinnern
2. Ökonomischer Uplift: Ø-R pro Trade der durchgelassenen Menge ≥ +20% vs. ungefiltert (inkl. Fees)
3. Brier-Score besser als Baseline (konstante Win-Rate) — Kalibrierung allein reicht nicht

## Noch nicht getestet (Testing-Backlog)
- (leer)

## Entscheidungen des Users (beantwortet 2026-08-13, 2. Session)
- Phase-0-Fixes (0.1–0.3) sind bereits auf Render deployt ✓
- KEINE Render Persistent Disk vorhanden → 0.6 braucht Alternativlösung (siehe "Als Nächstes")
- Mongo = Gratis-Tier (512 MB) → 200-Tage-Snapshot-Retention aus 0.1 bleibt (≈140–160 MB + Notbremse)
- Eskalations-Design: GATE VOR SIZER ✓
- ML-Modell v1: KRYPTO-ONLY ✓

## RCA-Notiz ai_rewards (aktualisiert 14.08.)
- User hat bestätigt: Er hat am 13.08. NICHT "Belohnungsdaten löschen" geklickt.
- Ursache damit UNBESTÄTIGT (evtl. Kumpel mit Admin-Zugang oder Deploy-Nebeneffekt).
  Der Fix (backfill_missing + include_cleared) ist davon unabhängig und deckt beide Fälle ab.
- Empfehlung (Backlog): Lösch-Button künftig mit Bestätigungsdialog + Audit-Log (wer/wann),
  gilt auch für "Verlauf/Trades löschen" (siehe ⚠️ 167→96 auto_trades oben).

## Offene Entscheidungen des Users
- OFFEN (blockiert 0.7): Freigabe für Regime-Arbeiten in dieser Reihenfolge?
  (a) 0.7a Gate-Shadow-Domain-Fix (krypto-only, dringend — B6),
  (b) 0.7b Regime-Kalibrierung (Perzentil-Vol + ehrliches Breakout-Label),
  (c) 0.7c echte regime_engine-Brücke erst NACH 0.7b-Evaluierung.
  Details im Abschnitt "RCA: Regime-Qualität beider Welten".
- ERLEDIGT 15.06.: Fee-Wächter (Option 1) + Fee-Feedback-Paket (Lern-Feedback,
  Fees-vs-Risiko-Anzeige, Blockier-Statistik) umgesetzt.
- ERLEDIGT 14.08.: Self-Tuning-Guard (Variante a) + Phase-4-Zahlen freigegeben/umgesetzt
- ERLEDIGT 14.06.: Gate-v1-Datenbasis (Prod-Signale+Ghost+Decisions, nur lesend) freigegeben
- ERLEDIGT 14.06.: Trade-Schwund 167→96 + Reward-Löschung = Kumpel (User bestätigt, RCA zu)
- Offen: Render-Deploy durch User (aktiviert Phase 4 + 5 + 6 in Prod; danach Prod-Migration
  0.5 + Reward-Backfill ausführen, Reihenfolge siehe eigener Abschnitt). User wurde
  informiert: Deploy kann JETZT erfolgen.
- Offen: Phase-7-Richtung (Regime-Brücke =0.7 vs. Shadow-Beweis abwarten)
- Später: Gate-Aktivierung (nach Shadow-Beweis, 3 Kriterien über 4 Wochen)

## Bekannte Baustellen / Tech-Debt
- Signal-Evaluation ohne Kerzen-Backfill: Downtime-Lücken labeln falsch/spät — P1,
  sauber lösbar im ML-Dataset-Builder (candle_cache-basiertes Nach-Labeling)
- 3 Ergebnis-Wahrheiten — ERLEDIGT durch 0.5 (2026-08-13); Prod-Daten-Migration steht noch aus
- Swing-Signale bleiben unlabeled — ERLEDIGT durch 0.3 (2026-08-13)
- ML-Labor (`services/ai_ml_lab.py`): geshuffelte CV etc. — Neubau ERLEDIGT durch Gate v1
  (services/ml_gate.py, 14.06.); altes ml_lab läuft bewusst unverändert parallel (UI/
  Rückwärtskompatibilität). P2: nach bewährtem Gate-Betrieb altes ml_lab ablösen/ausbauen.
- Zwei Regime-Welten ohne Brücke (Observer classify_regime vs. regime_engine v2) — P1 (=0.7)
- Regime an Rewards zum Close- statt Entry-Zeitpunkt — ERLEDIGT mit ai_rewards-Fix
  (2026-08-13): _regime_for bevorzugt jetzt entry_market_snapshot
- ai_decisions ohne Index — ERLEDIGT 14.06. (decisions_ts + decisions_outcome_ts in
  core/indexes.py, mit Phase 5 mitgenommen)
- ai_engine.py 3171-Zeilen-Gott-Objekt — P2, nur bei Gelegenheit entflechten, kein Selbstzweck

## Handover-Notiz
Falls du der neue Agent bist: Lies zuerst diese Datei, dann /app/memory/PRD.md, dann /app/README.md.
Der letzte Schritt war das Fee-Feedback-Paket (15.06., Abschnitt oben: Fee-Anteil in
Rewards/Lern-Prompt, Fees-vs-Risiko an der Trade-Karte, Blockier-Statistik mit
fee_guard_blocks + /api/ai/fee-guard/stats), getestet iteration_24 100%. Davor am selben
Tag der Fee-Wächter (iteration_23 100%), davor OOM-Fix + Trade-Manager-Guards
(iteration_22 100%). Der nächste geplante Schritt ist offen (User entscheidet):
Regime-Brücke (=0.7, P1) oder Shadow-Beweis abwarten.
Der User muss außerdem noch Render deployen (aktiviert Fee-Wächter + Fee-Feedback +
Phase 4+5+6-Fixes in Prod) und danach die Prod-Migration 0.5 + Reward-Backfill
ausführen (Anleitung oben). Es gibt keine offenen ungetesteten Änderungen.
HINWEIS an den nächsten Agenten: NIE mehrere search_replace-Edits auf DIESELBE Datei
in einem Parallel-Batch ausführen — das hat diese Status-Datei am 15.06. einmal
korrumpiert (aus Git-Basis fb7cdcb rekonstruiert).
PFLICHT-REGEL (User-Wunsch): Nach jedem abgeschlossenen Schritt dem User einen Abschnitt
"🧪 So testest du es selbst" liefern: (1) Preview-URL + Klickpfad im UI oder konkreter API-Aufruf,
(2) was bei Erfolg zu sehen ist, (3) Hinweis, falls nur in Dev und noch nicht auf Render sichtbar.
Wichtig: Diese Umgebung läuft auf FRISCHER lokaler MongoDB; PROD_MONGO_URL/PROD_DB_NAME in
/app/backend/.env sind der NUR-LESEND-Zugang zur echten Render-DB (für Analysen/Migrationstests).
LLM-Keys sind aktiv (alle 5 Provider), KI Trader in Dev bewusst noch enabled=False.
Bitunix-/Telegram-/Supabase-Keys bewusst NICHT gesetzt (siehe Umgebung & echte Daten).
