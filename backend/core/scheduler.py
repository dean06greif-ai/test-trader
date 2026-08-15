"""Hintergrund-Loops: Scanner-Polling & täglicher Reset.
(1:1 aus server.py verschoben – app.mongodb -> state.db)"""
import asyncio
import logging
import time
from datetime import datetime, timezone

from core import state
from core.config import ALL_SYMBOLS, POLL_INTERVAL
from core.instruments import fetch_live_candles, get as get_instrument
from core.state import scanner, autotrader, scanner_running, open_signal_evals
from core.pipeline import process_signal, evaluate_open_signals, broadcast

logger = logging.getLogger(__name__)

_last_reset_date = None

# Frische-Wächter: ist die neueste (formende) Kerze älter als das, gilt der
# Preis als veraltet und wird NICHT für die SL/TP-Überwachung verwendet.
STALE_AFTER_MS = 4 * 60 * 1000
_stale_warned: dict = {}   # symbol -> letzter Warn-Zeitstempel (Log-Drossel)


def _needs_backfill(buf: list, klines: list) -> bool:
    """True, wenn zwischen Puffer-Ende und den neu geholten Kerzen 1m-Kerzen
    fehlen (z.B. nach Loop-Stau oder Quellen-Ausfall) – dann Historie nachladen,
    damit Indikatoren (EMA-Ketten, Aggregation) keine Lücken sehen."""
    if not buf or not klines:
        return False
    return (klines[0]["timestamp"] - buf[-1]["timestamp"]) > 60000


def _is_stale(symbol: str, forming_ts: int, now_ms: int) -> bool:
    """Preis-Frische prüfen. Yahoo-Instrumente (Gold/Öl/Forex) haben
    Handelspausen – dort ist eine alte Kerze normal, kein Staleness-Fall."""
    inst = get_instrument(symbol)
    if inst is not None and inst.live_source == "yahoo":
        return False
    return (now_ms - int(forming_ts)) > STALE_AFTER_MS


async def start_scanner():
    logger.info(f"Scanner started for {len(ALL_SYMBOLS)} instruments (every {POLL_INTERVAL}s)")
    scanner_running.set()
    while scanner_running.is_set():
        prices = {}
        for symbol in ALL_SYMBOLS:
            if not scanner_running.is_set():
                break
            try:
                klines = await fetch_live_candles(symbol, 5)
                if len(klines) < 2:
                    continue
                # Lücken-Backfill: fehlende Minuten nachladen statt still verlieren
                if _needs_backfill(scanner.candle_buffer.get(symbol), klines):
                    filler = await fetch_live_candles(symbol, 200)
                    if len(filler) >= len(klines):
                        klines = filler
                        logger.info(f"{symbol}: Kerzen-Lücke erkannt – "
                                    f"{len(filler)} Kerzen nachgeladen")
                closed_candles = klines[:-1]
                forming = klines[-1]
                new_candle = False
                for candle in closed_candles:
                    if scanner.add_closed_candle(symbol, candle):
                        new_candle = True
                scanner.forming[symbol] = forming
                price = forming["close"]
                fresh = not _is_stale(symbol, forming["timestamp"],
                                      int(datetime.now(timezone.utc).timestamp() * 1000))
                if fresh:
                    prices[symbol] = price
                else:
                    now = time.time()
                    if now - _stale_warned.get(symbol, 0) > 600:
                        _stale_warned[symbol] = now
                        age_min = (datetime.now(timezone.utc).timestamp() * 1000
                                   - forming["timestamp"]) / 60000
                        logger.warning(
                            f"{symbol}: Marktdaten veraltet ({age_min:.0f} min) – "
                            f"Preis wird nicht für SL/TP-Überwachung verwendet")

                signals = scanner.analyze_symbol(symbol)
                if new_candle:
                    for sig in signals:
                        await process_signal(sig, scanner.candle_buffer.get(symbol, []))

                if fresh:
                    await evaluate_open_signals(symbol, price)
                await broadcast({"type": "candle", "symbol": symbol, "data": forming})
                states = scanner.rule_states.get(symbol)
                if states:
                    await broadcast({"type": "rule_states", "symbol": symbol, "data": states})
            except Exception as e:
                logger.error(f"Scan error for {symbol}: {e}")
            await asyncio.sleep(0.1)
        try:
            await autotrader.monitor(prices)
        except Exception as e:
            logger.error(f"autotrade monitor error: {e}")
        await asyncio.sleep(POLL_INTERVAL)


async def daily_reset_loop():
    """At Berlin midnight: aggregate the day into compact analytics.
    (Raw signals & closed trades are NOT deleted anymore – auf Nutzerwunsch.)"""
    global _last_reset_date
    _last_reset_date = scanner.berlin_date()
    while True:
        await asyncio.sleep(60)
        today = scanner.berlin_date()
        if today != _last_reset_date:
            await perform_daily_reset(_last_reset_date)
            _last_reset_date = today


async def perform_daily_reset(prev_date: str):
    logger.info(f"Daily reset for {prev_date}")
    try:
        pipeline = [
            {"$match": {"trade_date": prev_date}},
            {"$group": {"_id": {"strategy": "$strategy_id", "type": "$type"},
                        "total": {"$sum": 1},
                        "wins": {"$sum": {"$cond": [{"$eq": ["$result", "win"]}, 1, 0]}},
                        "losses": {"$sum": {"$cond": [{"$eq": ["$result", "loss"]}, 1, 0]}},
                        "avg_crv": {"$avg": "$crv"}}},
        ]
        rows = await state.db.signals.aggregate(pipeline).to_list(500)
        summary = {"date": prev_date, "generated_at": datetime.now(timezone.utc).isoformat(),
                   "by_strategy_type": [{"strategy": r["_id"]["strategy"], "type": r["_id"]["type"],
                                         "total": r["total"], "wins": r["wins"], "losses": r["losses"],
                                         "avg_crv": round(r.get("avg_crv") or 0, 2)} for r in rows]}
        total = sum(r["total"] for r in rows)
        summary["total_signals"] = total
        await state.db.analytics_daily.update_one({"date": prev_date}, {"$set": summary}, upsert=True)
        # trade stats aggregate
        tstats = await state.db.auto_trades.aggregate([
            {"$match": {"trade_date": prev_date, "status": "closed"}},
            {"$group": {"_id": None, "trades": {"$sum": 1},
                        "pnl": {"$sum": "$realized_pnl"},
                        "wins": {"$sum": {"$cond": [{"$eq": ["$result", "win"]}, 1, 0]}}}}],
        ).to_list(1)
        if tstats:
            ts = tstats[0]
            await state.db.trade_stats.update_one({"date": prev_date}, {"$set": {
                "date": prev_date, "trades": ts["trades"], "pnl": round(ts.get("pnl") or 0, 4),
                "wins": ts["wins"]}}, upsert=True)
        # Tägliche Telegram-Zusammenfassung (Toggle: daily_summary)
        try:
            from services import notifications
            ts = tstats[0] if tstats else {"trades": 0, "pnl": 0, "wins": 0}
            n_tr = ts.get("trades", 0)
            wr = round(ts.get("wins", 0) / n_tr * 100, 1) if n_tr else 0
            await notifications.telegram_notify(
                state.db, state.telegram, "daily_summary",
                f"🌙 *TAGES-ZUSAMMENFASSUNG {prev_date}*\n"
                f"Signale: {total} · Trades: {n_tr} · "
                f"Winrate: {wr}% · PnL `{round(ts.get('pnl') or 0, 4)} USDT`")
        except Exception as e:
            logger.warning(f"daily_summary notify failed: {e}")
        # NOTE: Auto-Löschung deaktiviert – Signale und geschlossene Trades
        # bleiben dauerhaft in der DB erhalten (auf Nutzerwunsch).
        # ML-Fix 0.3: open_signal_evals wird NICHT mehr um Mitternacht geleert –
        # Swing-Signale (>1 Tag Haltedauer) werden sonst nie win/loss gelabelt.
        # Expiry (14 Tage) übernimmt evaluate_open_signals() selbst.
        await broadcast({"type": "daily_reset", "date": prev_date})
    except Exception as e:
        logger.error(f"daily reset error: {e}")
