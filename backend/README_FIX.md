# Bitunix Live-Trading Fix (Krypto_Alert / branch `new6`)

## Was war kaputt?
Live-Orders wurden von Bitunix mit `code: 300105 "System error"` abgelehnt,
aber die App hat den Trade trotzdem lokal als `status: "open"` gespeichert
und weiter-simuliert („Geister-Positionen").

**Root cause:** Die App hat die internen Kurznamen `GOLD` / `SILVER` / `OIL`
als Order-Symbol an Bitunix geschickt. Bitunix kennt diese Namen nicht —
die echten USDT-M-Futures-Kontrakte heißen:

| App-intern | Bitunix-Kontrakt |
|------------|------------------|
| GOLD       | XAUUSDT          |
| SILVER     | XAGUSDT          |
| OIL        | CLUSDT           |
| BTCUSDT / XRPUSDT / …  | (identisch) |

## Was wurde geändert?

### 1) `backend/services/bitunix_trade.py`
- **Symbol-Mapping**: Neue Funktion `BitunixTradeClient.to_bitunix_symbol()`
  übersetzt App-Symbol → Bitunix-Kontrakt. Krypto-Symbole (BTCUSDT etc.)
  bleiben unverändert.
- **Katalog-Validierung**: Neue Methode `load_trading_pairs()` lädt beim
  Start `GET /api/v1/futures/market/trading_pairs`, cached echte Symbolliste
  und Step/Tick-Size je Kontrakt. Fängt zukünftige Bitunix-Umbenennungen ab.
- Alle privaten Calls (`place_order`, `flash_close`, `set_leverage`,
  `get_positions`) nutzen jetzt das gemappte Symbol.
- **Payload sauber**: `qty` und `price` werden gegen die Step-/Tick-Size
  gerundet (DOWN) und als String gesendet. Body bleibt kompaktes JSON
  (`separators=(",",":")`) für konsistente Signatur.
- **Order-Logik korrigiert (`on_signal`)**: Im Live-Modus wird zuerst die
  Order geschickt. Nur wenn `code == 0` **und** eine `orderId` zurückkommt,
  wird der Trade in Mongo gespeichert. Bei Ablehnung wird der Trade
  **verworfen** (kein Ghost-Trade, kein Fake-PnL) und ein Telegram-Alert
  gesendet.

### 2) `backend/services/telegram_bot.py`
- Neue Methode `send_rejection(symbol, side, reason)` schickt eine
  Telegram-Nachricht „⛔ ORDER ABGEBROCHEN“ mit Bitunix-Fehlercode/-Text.

### 3) `backend/server.py`
- Nach dem Mongo-Connect: `autotrader.set_telegram(telegram)` +
  `await trade_client.load_trading_pairs()`.

## Sicherheit — WICHTIG
Der alte Bitunix-API-Key (`65bf5888…` / `cbee405…`) war öffentlich sichtbar
und gilt als **kompromittiert**. Vor dem Live-Test:

1. In Bitunix → alten Key löschen.
2. Neuen Key mit Trade-Permission erstellen.
3. Nur in Render als Env-Var setzen:
   - `BITUNIX_API_KEY` (oder `BITUNIX_KEY`)
   - `BITUNIX_API_SECRET` (oder `BITUNIX_SECRET_KEY`)

## Deploy-Schritte auf Render
1. Diese drei Dateien ins Repo `dean06greif-ai/Krypto_Alert` Branch `new6`
   überschreiben (siehe `bitunix_live_fix.patch` für den reinen Diff):
   - `backend/server.py`
   - `backend/services/bitunix_trade.py`
   - `backend/services/telegram_bot.py`
2. Commit + push → Render deployed automatisch.
3. In Render-Logs prüfen, dass beim Start steht:
   `Bitunix trading_pairs cached: <N> symbols`
   (falls Zahl 0 ist: Endpoint blockiert → Support prüfen.)

## Kontrollierter Live-Test (Phase 5)
1. In Bitunix genug USDT-Guthaben.
2. Neuen API-Key eintragen.
3. In der App **1 Mini-Order pro Kontrakt** manuell auslösen:
   - GOLD (→ XAUUSDT)
   - SILVER (→ XAGUSDT)
   - OIL (→ CLUSDT)
   - BTCUSDT
4. **Erfolgskriterien pro Trade:**
   - Response `code == 0`
   - Feld `bitunix_order_id != null` im `auto_trades`-Doc
   - Position ist in Bitunix unter „Futures Positions“ sichtbar
5. Positionen sauber schließen (App → „Close“ oder direkt in Bitunix).

## Was passiert jetzt bei Reject?
- Kein DB-Eintrag mehr.
- Kein Fake-PnL.
- Telegram-Nachricht: „⛔ ORDER ABGEBROCHEN — Bitunix hat die Order
  abgelehnt: code XXXXXX: <msg>“.
- Log-Eintrag mit `Live order REJECTED …`.
