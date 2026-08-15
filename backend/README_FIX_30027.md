# Bitunix Live-Trading Fix – Code 30027 "TP price must be greater than mark price"

## Root Cause (3 verzahnte Bugs)

### 1. Precision-Parsing-Bug (Hauptursache) 🔥
Der Bitunix `trading_pairs` Endpoint liefert `basePrecision` / `quotePrecision`
als **Anzahl der Dezimalstellen** (integer), z. B. für AVAXUSDT:

```json
{ "symbol": "AVAXUSDT", "basePrecision": 0, "quotePrecision": 3, ... }
```

Der alte Code in `bitunix_trade.py::load_trading_pairs` hat diese Zahl aber als
tick-**Schrittweite** interpretiert:

```python
# ALTER, BUGGY Code
"price_tick": float(row.get("quotePrecision") or 0)   # -> 3.0 statt 0.001
```

Beim Runden lief dann folgendes ab:

```
_round_step(6.716963, 3.0)   → floor(6.716963 / 3) * 3   → 2 * 3 = 6.0
_round_step(77.806977, 2.0)  → floor(77.806977 / 2) * 2  → 38 * 2 = 76.0
```

Bitunix bekam also für den TP-Preis **6.0 statt 6.717** bzw. **76.0 statt 77.81** –
beides unter dem Mark Price → Bitunix lehnt mit `code 30027` ab.

### 2. Stale Entry für MARKET-Orders
`on_signal` hat SL/TP aus `signal.entry_price` berechnet. Zwischen Signal und
Order-Submit können Sekunden vergehen; für MARKET-Orders wird die Position aber
zum aktuellen Mark gefüllt. Ergebnis: TP/SL können auf der falschen Seite des
neuen Mark landen.

### 3. Kein Mindestabstand zum Mark
Bitunix verlangt einen minimalen Puffer zwischen TP/SL und Mark. Bei sehr engen
Levels (0.10 %) kann Slippage den TP direkt über/unter den Mark schieben.

## Fix

### Änderungen in `backend/services/bitunix_trade.py`

1. **`_precision_to_step()`** neu: konvertiert Dezimalstellen-Zähler (3) korrekt
   in tick step (0.001). Behandelt auch echte Step-Werte (0.001) idempotent.
2. **`_round_step()`** behält die Nachkommastellen des Ticks bei (verhindert
   `"6"` statt `"6.717"`).
3. **`_round_step_up()`** neu: aufrunden für LONG-TP / SHORT-SL, damit die
   Tick-Rundung den Preis nicht auf die falsche Seite des Marks schiebt.
4. **`_fmt_price(symbol, price, direction)`** akzeptiert jetzt eine Richtung
   (`"up"` / `"down"`) und rundet richtungssicher.
5. **`get_mark_price(symbol)`** neu: holt den aktuellen Mark Price über den
   öffentlichen `market/tickers` Endpoint.
6. **`place_order()`** akzeptiert `position_side` und rundet TP/SL immer in die
   sichere Richtung.
7. **`on_signal()`** (Live-Mode) holt jetzt den Mark Price direkt vor dem
   Order-Submit und:
   - re-anchort SL/TP an den aktuellen Mark bei MARKET-Orders,
   - erzwingt einen Mindestabstand von `max(2 * tick, 0.05 % Mark)`,
   - loggt die adjustierten Werte für Transparenz.
8. **`qty_step` / `min_qty` Zusammenspiel:** falls `minTradeVolume` (z. B. 1
   für AVAX) größer als der derivierte Step ist, wird `minTradeVolume` genommen
   → keine Fractional-Order-Probleme mehr.

### Neue Regression-Tests in `backend/tests/test_bitunix_precision_fix.py`

8 Tests – decken das exakte Failing-Szenario aus deinen Screenshots ab (AVAX
LONG TP 6.716963 gegen mark 6.702 und SOL LONG TP 77.806977 gegen mark 77.59).

## Deployment / Anwendung

### Option A – Patch direkt in dein Repo einspielen
```bash
cd /pfad/zu/Krypto_Alert
git checkout new6
git apply /pfad/zu/bitunix_30027_fix.patch
git add -A
git commit -m "Fix: Bitunix code 30027 – precision parsing + live mark price gating"
git push origin new6
```

### Option B – Datei manuell ersetzen
Ersetze in deinem Repo:
- `backend/services/bitunix_trade.py` → `bitunix_trade.py` aus diesem Verzeichnis
- `backend/tests/test_bitunix_precision_fix.py` → neuer Test

Dann pushen wie oben.

## Verifikation nach dem Deploy

Führ die neuen Tests direkt in deinem Container aus:
```bash
cd backend && python -m pytest tests/test_bitunix_precision_fix.py -v
```

Alle 8 Tests müssen `PASSED` sein. Der letzte Test schlägt Bitunix live an –
falls das Netzwerk raus geht, ist das kein Fehler des Fixes.

## Was du beim Live-Trading nun in den Logs sehen wirst

```
Live SL/TP adjusted for AVAXUSDT side=LONG mark=6.702
  -> entry=6.702 sl=6.6955 tp1=6.7085 tpf=6.715
AutoTrade OPEN LONG AVAXUSDT qty=149 entry=6.702 mode=live
```

Statt „Bitunix hat die Order abgelehnt" bekommst du jetzt eine gefüllte Order
mit korrekten SL/TP-Levels, die direkt in Bitunix' Anforderungen passen.
