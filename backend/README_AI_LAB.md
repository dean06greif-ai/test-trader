# KI-Ökosystem (KI-Labor) – Erweiterung des KI Traders

Diese Erweiterung baut das bestehende KI-Team modular aus. Bestehende Endpunkte,
Datenstrukturen und Nutzer-Workflows bleiben unverändert; alles Neue liegt in
eigenen Modulen und ist über eigene Endpunkte/Panels erreichbar.

## Neue Bausteine

| Modul | Aufgabe |
|---|---|
| `services/ai_memory.py` | KI-Gedächtnis: austauschbare Speicherschicht. MongoDB (`ai_knowledge`) als Primärspeicher, Supabase als Langzeit-Spiegel (Dual-Write, degradiert sauber ohne Keys). |
| `services/ai_research.py` | Rolle `research_analyst`: wertet Backtests, Optimizer-Läufe (inkl. Walk-Forward/Robustheit), Regime-Lab und Regime-Analysen aus und übergibt die Erkenntnisse an KI Trader, Tiefen-Analyst und Lern-Modul. |
| `services/ai_ml_lab.py` | ML-Labor: Optuna (TPE) sucht Hyperparameter, XGBoost lernt aus echten Ergebnissen, welche Marktbedingungen Gewinne liefern. Die Haupt-KI erklärt das Ergebnis und leitet Regeln ab. |
| `services/ai_market_observer.py` | Rolle `market_observer`: misst laufend Trend, Volatilität, ATR, RSI, Volumen, Range-Position pro Coin (`ai_market_snapshots`) – Trainingsdaten für das ML-Labor. |
| `routers/ai_lab.py` | Endpunkte des KI-Labors. |
| `frontend/src/components/AILabPanel.js` | UI-Panel „KI-Labor“ im KI-Trader-Panel (Forschung / ML-Modell / Gedächtnis / Markt). |

## Datenfluss

```
Backtester / Optimizer / Regime-Lab ─┐
Markt-Beobachter (Snapshots) ────────┤→ Forschungs-Analyst ─┐
Signale / Trades (echte Ergebnisse) ─┴→ ML-Labor (Optuna+XGBoost) ─┤→ KI-Gedächtnis
                                                                   └→ KI Trader (Analyse-Prompt),
                                                                      Lern-Modul, Tiefen-Analyst
```

## Rollen-Voreinstellungen

`services/ai_roles.ROLE_PRESETS` belegt jede Rolle mit einem passenden, günstigen
Modell aus dem bestehenden Katalog. Sobald im UI eine eigene Wahl getroffen wird,
gilt diese dauerhaft (`user_configured`); „Voreinstellung wiederherstellen“ per
`POST /api/ai/roles/{role}/reset`. Ist für die Voreinstellung kein API-Key
gesetzt, hängt die Modell-Kette automatisch alle Provider mit Key als letzte
Fallback-Stufe an – eine Rolle fällt dadurch nie komplett aus.

## Endpunkte

```
GET  /api/ai/lab/status              Gesamtstatus (Forschung, ML, Beobachter, Gedächtnis)
GET  /api/ai/research/report         letzter Forschungsbericht
GET  /api/ai/research/data           aufbereitete Rohdaten-Digests
POST /api/ai/research/run            Forschungs-Auswertung starten            (Admin)
GET  /api/ai/ml/status               Modell-Status
GET  /api/ai/ml/dataset              Datenlage fürs Training
GET  /api/ai/ml/predict?symbol=      Gewinnwahrscheinlichkeit LONG/SHORT
POST /api/ai/ml/train                Optuna+XGBoost trainieren                (Admin)
POST /api/ai/ml/settings             ML-Einstellungen                         (Admin)
GET  /api/ai/observer/status         Markt-Beobachter
GET  /api/ai/observer/snapshots      Markt-Snapshots
POST /api/ai/observer/run            Markt jetzt scannen                      (Admin)
GET  /api/ai/memory/stats?health=1   Gedächtnis-Status (inkl. Supabase-Ping)
GET  /api/ai/memory/entries?kind=    Wissenseinträge
POST /api/ai/roles/{role}/reset      Rolle auf Voreinstellung zurücksetzen    (Admin)
```

## Automatik

* **Markt-Beobachter**: alle `interval_min` Minuten (Standard 15).
* **Forschungs-Analyst**: zu `schedule_times`, spätestens nach `interval_hours`,
  zusätzlich automatisch bei neuen Backtest-/Optimizer-/Regime-Ergebnissen.
* **ML-Labor**: täglich zur konfigurierten Stunde und nach `min_new_results`
  neuen abgeschlossenen Ergebnissen (mindestens 40 Datensätze, je 8 pro Klasse).
* Alle Läufe hängen im bestehenden `ai_engine.run_loop()` und sind einzeln
  gekapselt – ein Fehler kann den Trading-Loop nicht stoppen.

## Konfiguration (ENV)

```
SUPABASE_URL=https://<projekt>.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<secret key>
AI_MEMORY_TABLE=ai_knowledge          # optional
```

Supabase-Tabelle einmalig anlegen: `backend/scripts/supabase_schema.sql` im
SQL-Editor ausführen. Ohne Tabelle/Keys arbeitet das Gedächtnis unverändert mit
MongoDB weiter (der Fehler wird im KI-Labor-Panel angezeigt).

## Tests

```
cd backend && python -m pytest tests/test_ai_lab.py -q     # 27 Tests, offline
python scripts/seed_ai_lab_demo.py                         # Demo-Daten (Dev)
python scripts/seed_ai_lab_demo.py --clean                 # Demo-Daten entfernen
```

---

# Erweiterung 2: KI-Trade-Steuerung & Closed Loop

## KI-Trade-Steuerung (`services/ai_trade_manager.py`, Rolle `trade_manager`)

Die KI darf Trades eigenständig eröffnen und im laufenden Trade steuern. Alle
Aktionen laufen über EINE Quelle (`services/bitunix_trade.AutoTradeManager`) und
gelten identisch für Paper und Live:

| Aktion | Wirkung |
|---|---|
| `close` | Trade vorzeitig komplett schließen |
| `partial_close` | Teilmenge schließen (1–99 % der Restmenge) |
| `adjust_sl` | SL verschieben (absoluter Preis oder `pct` = Abstand zum Kurs) |
| `adjust_tp` | TP1 oder Final-TP verschieben (`target: tp1｜tpf`) |
| `add_margin` | Margin hinzufügen → Hebel sinkt, Liquidation rückt weg |
| `remove_margin` | Margin entnehmen → Hebel steigt |
| `set_leverage` | Hebel ändern, Positionsgröße bleibt erhalten |
| `hold` | bewusst nichts tun |

Live-Umsetzung: `POST /api/v1/futures/account/adjust_position_margin` (Margin,
positiv = hinzufügen) bzw. `change_leverage`; Teil-/Vollschließung über den
bestehenden Flash-Close. Paper-Trades werden identisch nachgerechnet
(Margin, effektiver Hebel, Liquidationspreis, Gebühren, realisierter PnL).

**Schutzregeln** (`check_limits`, alle im UI einstellbar): max. Aktionen pro
Trade, Cooldown zwischen Aktionen, Hebel-Obergrenze, Margin-Aufschlag in % der
Start-Margin, Zusatz-Margin nur aus dem freien Kapital-Kontingent. Kapitalrahmen
und Live/Paper-Modus bleiben für die KI tabu. Jede Aktion landet in
`ai_trade_actions`, im Trade-`events`-Verlauf, im KI-Chat und im Gedächtnis.

Eigene Trades: die KI gibt `symbol, side, sl_pct, tp1_pct, tpf_pct, leverage,
capital_pct` vor; Hebel (1–125x) und Kapitalanteil (5–100 % des konfigurierten
`max_capital`) werden in `on_signal` geklemmt.

```
GET  /api/ai/trade/status            Einstellungen + letzte Aktionen
POST /api/ai/trade/settings          Limits/Schalter                     (Admin)
POST /api/ai/trade/review            KI prüft jetzt alle offenen Trades   (Admin)
POST /api/ai/trade/action            Einzelaktion (KI oder manuell)       (Admin)
POST /api/ai/trade/open              Custom-Trade eröffnen                (Admin)
```

## Closed Loop (`services/ai_closed_loop.py`) – standardmäßig AUS

Ist der Schalter aktiv, startet der Forschungs-Analyst nach seiner Auswertung
selbst einen Optimizer-Lauf (Bayes/TPE) für den stärksten Kandidaten
(letzter Optimizer-Lauf, sonst beste Backtest-Strategie). Das Ergebnis wird als
**Vorschlag** hinterlegt (Gedächtnis + KI-Chat + `settings/ai_closed_loop`) –
die Übernahme bleibt bewusst manuell im Optimizer-Panel.
Grenzen: `max_runs_per_day`, `min_gap_hours`, nie parallel zu einer laufenden
Optimierung.

```
GET  /api/ai/closed_loop/status
POST /api/ai/closed_loop/settings    enabled, max_runs_per_day, min_gap_hours, days, iterations
POST /api/ai/closed_loop/run         sofort einen Lauf starten            (Admin)
```

UI: KI-Labor → Tab **„Trade-Steuerung"** (Schalter, Limits, manuelle Aktions-Buttons
pro offenem Trade, Aktions-Protokoll, Closed-Loop-Schalter).
