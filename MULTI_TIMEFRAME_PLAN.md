# Multi-Timeframe pro Regel – Plan & Umsetzungsstand

> Diese Datei ist der "genaue Plan", damit jede KI/Entwickler:in hier weiterarbeiten kann.
> Stand: 14.06.2026 – **Etappen 1–6 sind UMGESETZT** (inkl. Tests), Details unten.

## Ziel / Entscheidung (mit dem User abgestimmt)
**Hybrid-Ansatz:** Basis-Timeframe pro Strategie bleibt (dort laufen alle Trigger),
aber **pro Regel ein optionales Timeframe-Override** – gedacht für Filter-/Kontext-Regeln
(z.B. Trend über `ema(200)` auf 1h, RSI-Zone auf 15m, Entry-Trigger auf 1m).
- **Default = Strategie-TF** → alle bestehenden Strategien verhalten sich exakt wie bisher (rückwärtskompatibel).
- Multi-Timeframe wird **nie erzwungen** – auch der Optimizer behält den Strategie-TF immer als Option.
- Wählbare Regel-TF-Stufen: `1m, 3m, 5m, 15m, 30m, 1h, 2h, 4h, 8h, 12h, 1d` (bis 1 Tag, User-Wunsch).
- Regel: Regel-TF muss **≥ Strategie-TF und ein Vielfaches davon** sein (sonst Ablehnung beim Speichern).

## Semantik (wichtig für Live/Backtest-Parität)
- Eine Regel mit `"timeframe": "15m"` wird auf den zu 15m aggregierten Kerzen ausgewertet
  (`drop_partial=True` → nur **geschlossene** 15m-Kerzen, **kein Lookahead/Repaint**).
- Jede Basis-Kerze i nutzt die letzte 15m-Kerze, deren Schlusszeit ≤ Schlusszeit von i ist
  (`np.searchsorted`-Mapping in `FastSeries.htf()`).
- Cross-Regeln auf höherem TF gelten für alle Basis-Kerzen bis zur nächsten HTF-Kerze –
  deshalb Empfehlung (auch im KI-Prompt): Cross-/Trigger-Regeln auf dem Strategie-TF lassen.
- Live-Scanner und Backtester/Optimizer nutzen **denselben** Auswertungspunkt
  (`fast_sim._rule_cond`) → garantiert identische Ergebnisse.

## Datenmodell
Regel-Format (Custom-/KI-Strategien, MongoDB `custom_strategies`):
```json
{"indicator": "ema(200)", "op": "<", "value": "price", "timeframe": "1h", "label": "..."}
```
`timeframe` ist optional; fehlt es, gilt der Strategie-TF (`definition.timeframe`).

## Umgesetzte Etappen (alle fertig)

### Etappe 1 – Kern (Auswertung)
- `services/timeframes.py`: `"1d"` (=1440) ergänzt; `RULE_TIMEFRAMES`, `tf_minutes()`,
  `normalize_rule_tf()` (Aliase wie `24h`→`1d`), `valid_rule_tf()`, `rule_tf_options()`.
  `aggregate_candles(..., base_ms=...)`: drop_partial funktioniert jetzt auch, wenn die
  Quelle nicht 1m ist (z.B. 5m→15m) – vorher steckte dort ein hartes 60000ms.
- `services/fast_sim.py`: `FastSeries.base_tf_ms()` (aus Timestamps), `FastSeries.htf(tf)`
  (aggregierte Serie + Mapping, gecacht) und `_rule_cond_htf()`; `_rule_cond()` prüft
  `rule["timeframe"]` und fällt bei ungültigem TF defensiv auf den Basis-TF zurück.

### Etappe 2 – Validierung / Persistenz
- `strategies/custom_params.py::normalize_definition`: normalisiert `timeframe` je Regel,
  meldet ungültige TF als `problems` (Speichern wird wie gewohnt mit 422 abgewiesen),
  entfernt Overrides, die dem Basis-TF entsprechen.
- `rule_text()` zeigt `@tf` an; `CustomStrategy._auto_label` ebenso.

### Etappe 3 – Live-Scanner
- `CustomStrategy.analyze` nutzt `_rule_cond` → Overrides wirken automatisch live.
- `services/strategy_scanner.py::buffer_limit`: berücksichtigt Regel-TF-Overrides
  (mind. 60 Kerzen des höchsten Regel-TF, Deckel 30 Tage 1m-Historie = 43200 Kerzen wegen
  512-MB-Render-RAM; 1d-Filter haben live damit ~30 Kerzen – reicht für z.B. `ema(20)`-Filter,
  nicht für `ema(200)@1d`).

### Etappe 4 – Backtester
- Kein Sondercode nötig: Backtester baut `FastSeries` aus den auf Strategie-TF aggregierten
  Kerzen (echte Timestamps) → `_rule_cond` wertet Regel-TFs identisch aus.
- `rule-preview` (StrategyBuilder, 7-Tage-Minibacktest) zeigt Feuer-Raten pro Regel inkl. TF.

### Etappe 5 – Optimizer / Discovery / Endlos-Suche
- Neuer Request-Parameter `rule_timeframes: {enabled, min, max}` (z.B. 1m–4h) in
  `POST /api/optimizer/run` → `services/optimizer.py::run_optimizer` berechnet gültige
  TF-Optionen relativ zum Lauf-TF.
- **Parameter-Modus** (nur Custom-Strategien): `custom_params.rule_timeframe_space()` erzeugt
  Suchraum-Keys `long1_tf`, `short2_tf`, … (erste Option = Strategie-TF = kein Override).
  `apply_params()` schreibt sie in die Definition; `CustomStrategy.get_params()` reicht
  `*_tf`-Keys durch (BaseStrategy filtert sonst auf DEFAULT_PARAMS). Ergebnis-Anwendung
  ("Übernehmen") funktioniert damit unverändert über `strategy_params`.
- **Discovery/Combo/Deep/Explore**: `build_candidates(allowed, tf_options)` erzeugt
  zusätzlich TF-Varianten jedes Kandidaten (`"RSI < 30 @15m"`); Greedy/Explore wählt sie
  nur, wenn der Score besser ist. `deep_search.run`/`deep_explore.run` haben `tf_options=None`.

### Etappe 6 – UI + KI-Trader
- `frontend/src/constants/timeframes.js`: `RULE_TIMEFRAMES` + `TF_MINUTES`.
- `StrategyBuilder.js`: TF-Dropdown pro Regel ("TF: Strategie (1m)" = Default; ungültige
  Stufen deaktiviert), serialisiert/deserialisiert `timeframe`, Label bekommt `@tf`.
- `Optimizer.js`: Chip "Regel-Timeframes optimieren (Multi-Timeframe)" + von/bis-Auswahl
  (Default 1m–4h); sichtbar bei Parameter-Modus (Custom-Strategie) und Discovery/Combo/Explore.
- KI-Strategie-Labor (`services/ai_strategy_lab.py`): Prompt erklärt das optionale
  `"timeframe"`-Feld je Regel (Filter ja, Trigger nein) → die KI kann Multi-Timeframe
  selbstständig nutzen; Validierung/Auto-Fix läuft über denselben `normalize_definition`-Pfad.

## Tests
- `backend/tests/test_rule_timeframes.py` (neu): TF-Helper, Aggregation mit base_ms,
  HTF-Mapping ohne Lookahead, `_rule_cond` mit Override, Rückwärtskompatibilität
  (Regel ohne TF = exakt altes Verhalten), Live/Fast-Path-Parität, normalize/apply_params,
  Optimizer-Suchraum, Candidate-TF-Varianten, buffer_limit.
- Bestehende Suites (`test_rule_engine`, `test_backtest_optimizer`, `test_strategies`, …) grün.

## Offene / mögliche Folge-Aufgaben (Backlog)
- P2: TF-Override auch für Coin-spezifische Regel-Parameter-UI sichtbar machen (Diff-Ansicht zeigt `@tf` bereits).
- P2: KI-Trader-Playbook: Statistik, welcher Regel-TF pro Setup am besten performt.
- P3: Buffer-Deckel (43200) env-übersteuerbar machen, falls Render-RAM erhöht wird.
