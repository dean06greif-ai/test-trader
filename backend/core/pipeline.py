"""Signal-Pipeline: Broadcast, Signal-Verarbeitung, Performance-Tracking.
(1:1 aus server.py verschoben – app.mongodb -> state.db)"""
import logging
import uuid
from typing import Dict, List

from core import state
from core.state import scanner, telegram, autotrader, open_signal_evals, \
    control_state, toggle_enabled, websocket_clients
from core.utils import _clean
from services import notify_guard

logger = logging.getLogger(__name__)

# ML-Fix 0.3: Signale werden bis zu 14 Tage ausgewertet (vorher: nur am
# Eröffnungstag, Mitternachts-Reset). Swing-Signale bekamen dadurch NIE ein
# result und fielen komplett aus den Lern-/Trainingsdaten.
SIGNAL_EVAL_MAX_DAYS = 14


async def broadcast(message: Dict):
    dead = []
    for c in websocket_clients:
        try:
            await c.send_json(message)
        except Exception:
            dead.append(c)
    for c in dead:
        if c in websocket_clients:
            websocket_clients.remove(c)


async def update_performance(signal: Dict, opened=False, result=None):
    symbol = signal["symbol"]
    perf = await state.db.performance.find_one({"symbol": symbol}) or {
        "symbol": symbol, "total_signals": 0, "long_signals": 0, "short_signals": 0,
        "wins": 0, "losses": 0, "breakevens": 0, "avg_crv": 0.0, "win_rate": 0.0,
        "by_strategy": {},
    }
    sid = signal.get("strategy_id", "unknown")
    bs = perf.get("by_strategy", {})
    st = bs.get(sid, {"total": 0, "wins": 0, "losses": 0, "breakevens": 0})
    if opened:
        perf["total_signals"] += 1
        if signal["type"] == "LONG":
            perf["long_signals"] += 1
        else:
            perf["short_signals"] += 1
        perf["last_signal"] = signal["timestamp"]
        n = perf["total_signals"]
        perf["avg_crv"] = round((perf.get("avg_crv", 0) * (n - 1) + signal.get("crv", 0)) / n, 3)
        st["total"] += 1
    if result:
        perf[{"win": "wins", "loss": "losses", "breakeven": "breakevens"}[result]] += 1
        st[{"win": "wins", "loss": "losses", "breakeven": "breakevens"}[result]] += 1
    decided = perf["wins"] + perf["losses"]
    perf["win_rate"] = round(perf["wins"] / decided * 100, 1) if decided else 0.0
    bs[sid] = st
    perf["by_strategy"] = bs
    perf.pop("_id", None)
    await state.db.performance.update_one({"symbol": symbol}, {"$set": perf}, upsert=True)


async def process_signal(signal: Dict, candles: List[Dict]):
    # Global admin kill-switch for signals -> completely suppress emission
    if control_state.get("signals_paused"):
        return
    # Per-(coin, strategy) toggle: if this combination is disabled, skip
    # BOTH signal emission and auto-trade for the pair. Other coins/strategies
    # remain unaffected.
    if not toggle_enabled(signal.get("strategy_id"), signal.get("symbol")):
        return

    strategy_id = signal.get("strategy_id")
    symbol = signal["symbol"]

    # Per-Coin-pro-Strategie Config (NEU) — VOR insert prüfen
    _coin_strat_key = f"{strategy_id}_{symbol}"
    coin_strat_cfg = autotrader.config.get("strategy_coin_configs", {}).get(_coin_strat_key)
    if coin_strat_cfg is None:
        _doc = await state.db.strategy_coin_configs.find_one({"_id": _coin_strat_key})
        coin_strat_cfg = _doc.get("config", {}) if _doc else {}
        # Keep the in-memory cache in sync so on_signal()/effective_mode()
        # see the SAME paper/live mode on the next call. Prevents live orders
        # slipping through because the DB config wasn't cached yet.
        if coin_strat_cfg:
            autotrader.config.setdefault("strategy_coin_configs", {})[_coin_strat_key] = coin_strat_cfg

    # Wenn AUS → komplett überspringen, nichts speichern.
    # Ausnahme Datensammel-Modus (Phase 4): Sammel-Signale sind immer Paper
    # und dürfen auch auf AUS-Coins simuliert werden (mehr ML-Daten).
    if coin_strat_cfg.get("mode", "off") == "off" and not signal.get("data_collection"):
        return

    signals_enabled_for_strategy = coin_strat_cfg.get("signals_enabled", True)

    signal["id"] = str(uuid.uuid4())
    notify = scanner.is_notify_enabled(symbol)
    signal["notify"] = notify
    # Manuelle Website-Trades laufen zwar durch die Pipeline (Guards, Kapital,
    # Ausführung), erzeugen aber KEIN sichtbares Signal (Bug-Report).
    suppress = bool(signal.get("suppress_signal"))
    if not suppress:
        await state.db.signals.insert_one(dict(signal))

    # BUGFIX (win-rate): track this signal in-memory so evaluate_open_signals()
    # can later mark it as win/loss based on price hitting TP1 or SL.
    # Scanner-Signale nutzen die Keys take_profit_1/stop_loss (nicht tp1/sl) –
    # ohne den Fallback wurde NIE ein Signal ausgewertet -> Tages-Winrate blieb 0.
    _tp1 = signal.get("tp1") or signal.get("take_profit_1")
    _sl = signal.get("sl") or signal.get("stop_loss")
    if not suppress and signal.get("signal_class") != "PRE_SIGNAL" and _tp1 and _sl:
        open_signal_evals.append({
            "id": signal["id"],
            "symbol": symbol,
            "type": signal["type"],
            "tp1": _tp1,
            "sl": _sl,
            "strategy_id": signal.get("strategy_id", "unknown"),
            "ts": signal.get("timestamp"),
        })

    # FIX 1: Telegram-Benachrichtigung senden (wenn aktiviert)
    # Spam-Bremse: identische Setups (gleicher Coin/Strategie/Richtung und
    # nahezu gleicher Entry) werden innerhalb der Sperrzeit nicht erneut
    # gemeldet – vorher kamen bei einem 5 Minuten laufenden Signal bis zu
    # 10 gleiche Nachrichten.
    if notify and signals_enabled_for_strategy and not signal.get("data_collection"):
        cooldown = scanner.settings.get("notify_cooldown_min", notify_guard.DEFAULT_COOLDOWN_MIN)
        allowed, reason = notify_guard.check(signal, cooldown)
        signal["notify_suppressed"] = not allowed
        if allowed:
            try:
                # tp1_close_percent für die Telegram-Nachricht hinzufügen
                coin_cfg = autotrader.coin_cfg(symbol)
                signal["tp1_close_percent"] = coin_cfg.get("tp1_close_percent", 50)
                sent = await telegram.send_signal(signal)
                if sent:
                    logger.info(f"Telegram notification sent for {symbol} {signal['type']}")
                else:
                    # Vorher wurde hier fälschlich "sent" geloggt, obwohl der
                    # Versand gescheitert war (Bug-Report: irreführende Logs).
                    logger.warning(f"Telegram notification NOT sent for {symbol} "
                                   f"{signal['type']} (siehe Fehler davor)")
            except Exception as e:
                logger.error(f"Failed to send Telegram notification: {e}")
        else:
            logger.debug(f"Telegram für {symbol} {signal['type']} unterdrückt: {reason}")

    # FIX 2: Auto-Trade ausführen (wenn Auto-Trading aktiviert ist)
    try:
        trade = await autotrader.on_signal(signal, candles)
        # Für Aufrufer (z.B. KI-Trade-Manager) nachvollziehbar machen, ob
        # wirklich ein Trade eröffnet wurde – inkl. Ablehnungsgrund der Guards.
        signal["_trade_opened"] = bool(trade)
        if trade:
            if not signal.get("manual_trade"):
                await update_performance(signal, opened=True)
            logger.info(f"Auto-trade opened for {symbol}: {trade['id']}")
    except Exception as e:
        logger.error(f"Auto-trade execution failed for {symbol}: {e}")


async def evaluate_open_signals(symbol: str, price: float):
    """Mark open signals win/loss based on which level price reaches first.
    Läuft bis SIGNAL_EVAL_MAX_DAYS über Tagesgrenzen hinweg (Swing-Fix)."""
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=SIGNAL_EVAL_MAX_DAYS)).isoformat()
    remaining = []
    for ev in open_signal_evals:
        ts = ev.get("ts")
        if ts and str(ts) < cutoff:
            continue  # abgelaufen: bleibt result=None, wird nicht mehr getrackt
        if ev["symbol"] != symbol:
            remaining.append(ev)
            continue
        result = None
        if ev["type"] == "LONG":
            if price >= ev["tp1"]:
                result = "win"
            elif price <= ev["sl"]:
                result = "loss"
        else:
            if price <= ev["tp1"]:
                result = "win"
            elif price >= ev["sl"]:
                result = "loss"
        if result:
            # Fix 0.5: kanonische Trade-Wahrheit (result_source=trade_pnl,
            # gesetzt beim Trade-Close in bitunix_trade._after_close) hat
            # Vorrang – TP1-Touch überschreibt sie NICHT. matched==0 =>
            # Signal ist bereits trade-gelabelt -> Tracking beenden, ohne
            # die TP1-Touch-Performance doppelt/widersprüchlich zu zählen.
            res = await state.db.signals.update_one(
                {"id": ev["id"], "result_source": {"$ne": "trade_pnl"}},
                {"$set": {"result": result, "status": "closed",
                          "result_source": "tp1_touch"}})
            if res.matched_count > 0:
                await update_performance({"symbol": symbol, "strategy_id": ev["strategy_id"], "type": ev["type"]}, result=result)
        else:
            remaining.append(ev)
    open_signal_evals[:] = remaining


async def emit_ai_signal(signal: Dict) -> bool:
    """Route an AI decision through the normal signal/auto-trade pipeline."""
    symbol = signal["symbol"]
    candles = scanner.candle_buffer.get(symbol, [])
    await process_signal(signal, candles)
    if signal.get("id"):
        if not signal.get("suppress_signal"):
            await broadcast({"type": "signal", "data": _clean(signal)})
        return True
    return False
