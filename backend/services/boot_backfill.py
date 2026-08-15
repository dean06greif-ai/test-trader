"""Fix 0.6: Candle-Cache Render-Neustart-fest via Rebuild-on-Boot.

Render hat keine Persistent Disk -> CANDLE_CACHE_DIR (/tmp) ist nach jedem
Deploy/Neustart leer. Statt Kerzen in Mongo zu persistieren (512-MB-Gratis-
Tier), wird der Cache beim Boot IM HINTERGRUND per API-Backfill neu
aufgebaut: aktive Symbole zuerst, rate-limit-sicher (sequenziell + Pause,
Fetch-Pacing kommt aus services.history_sources), non-blocking (App startet
sofort).

Bonus (P1-Tech-Debt geschlossen): Signale, die WÄHREND einer Downtime TP1/SL
erreicht haben, werden anhand der nachgeladenen 1m-Kerzen korrekt gelabelt --
gleiche Semantik wie core.pipeline.evaluate_open_signals (result_source
tp1_touch, kanonische Trade-Wahrheit trade_pnl hat weiterhin Vorrang, bereits
gelabelte Signale werden NIE überschrieben).
"""
import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

import numpy as np

from core import state
from core.instruments import ALL_SYMBOLS, TOP_10_COINS

logger = logging.getLogger(__name__)

ENABLED = os.environ.get("BOOT_BACKFILL_ENABLED", "1") != "0"
# 14 Tage = SIGNAL_EVAL_MAX_DAYS: deckt das komplette Auswertungsfenster ab.
DAYS = int(os.environ.get("BOOT_BACKFILL_DAYS", "14"))
PAUSE = float(os.environ.get("BOOT_BACKFILL_PAUSE", "1.5"))

STATUS: Dict = {
    "state": "idle",  # idle | running | done | error | disabled
    "enabled": ENABLED,
    "days": DAYS,
    "started_at": None,
    "finished_at": None,
    "current": None,
    "symbols_total": 0,
    "symbols_done": 0,
    "candles_loaded": 0,
    "signals_labeled_win": 0,
    "signals_labeled_loss": 0,
    "signals_checked": 0,
    "errors": [],
}

_running = False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ts_to_ms(ts) -> Optional[int]:
    if ts is None:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def label_from_candles(ev: Dict, candles, after_ms: int) -> Tuple[Optional[str], bool]:
    """Ermittelt win/loss aus dem 1m-Kerzenpfad NACH dem Signal-Zeitpunkt.

    Gleiche Level-Semantik wie evaluate_open_signals (TP1 vor SL = win).
    Berühren TP1 UND SL dieselbe Kerze, ist die Reihenfolge unbekannt ->
    konservativ "loss" + ambiguous=True (ML kann diese Labels ausschließen).
    """
    try:
        tp1 = float(ev["tp1"])
        sl = float(ev["sl"])
    except (KeyError, TypeError, ValueError):
        return None, False
    is_long = ev.get("type") == "LONG"
    idx = int(np.searchsorted(candles.ts, after_ms, side="right"))
    for i in range(idx, len(candles)):
        hi = float(candles.hi[i])
        lo = float(candles.lo[i])
        if is_long:
            hit_tp, hit_sl = hi >= tp1, lo <= sl
        else:
            hit_tp, hit_sl = lo <= tp1, hi >= sl
        if hit_tp and hit_sl:
            return "loss", True
        if hit_tp:
            return "win", False
        if hit_sl:
            return "loss", False
    return None, False


async def _label_downtime_signals(symbol: str, candles) -> Dict:
    """Labelt offene Signal-Evals eines Symbols anhand nachgeladener Kerzen."""
    from core.pipeline import update_performance
    out = {"win": 0, "loss": 0, "checked": 0}
    if candles is None or not len(candles):
        return out
    evs = [ev for ev in state.open_signal_evals if ev.get("symbol") == symbol]
    for ev in evs:
        after_ms = _ts_to_ms(ev.get("ts"))
        if after_ms is None:
            continue
        out["checked"] += 1
        result, ambiguous = label_from_candles(ev, candles, after_ms)
        if not result:
            continue
        update = {"result": result, "status": "closed",
                  "result_source": "tp1_touch", "result_backfilled": True,
                  "result_ts": _now_iso()}
        if ambiguous:
            update["result_ambiguous"] = True
        # Nur unlabelte Signale; trade_pnl (kanonische Wahrheit) nie anfassen.
        res = await state.db.signals.update_one(
            {"id": ev["id"], "result_source": {"$ne": "trade_pnl"},
             "$or": [{"result": {"$exists": False}}, {"result": None}]},
            {"$set": update})
        if res.matched_count > 0:
            await update_performance(
                {"symbol": symbol, "strategy_id": ev.get("strategy_id", "unknown"),
                 "type": ev.get("type")}, result=result)
            out[result] += 1
        # Level wurde erreicht (oder Signal ist bereits anderweitig gelabelt)
        # -> Live-Tracking beenden.
        try:
            state.open_signal_evals.remove(ev)
        except ValueError:
            pass
    return out


def _priority_symbols() -> list:
    """Aktive Symbole zuerst: offene Signal-Evals -> Krypto -> Rest."""
    known = set(ALL_SYMBOLS)
    prio = []
    for ev in list(state.open_signal_evals):
        s = ev.get("symbol")
        if s in known and s not in prio:
            prio.append(s)
    for s in list(TOP_10_COINS) + list(ALL_SYMBOLS):
        if s in known and s not in prio:
            prio.append(s)
    return prio


async def run_boot_backfill() -> Dict:
    """Hintergrund-Task: Kerzen-Historie nachladen + Downtime-Signale labeln."""
    global _running
    if not ENABLED:
        STATUS["state"] = "disabled"
        return STATUS
    if _running:
        return STATUS
    _running = True
    STATUS.update(state="running", started_at=_now_iso(), finished_at=None,
                  symbols_done=0, candles_loaded=0, signals_labeled_win=0,
                  signals_labeled_loss=0, signals_checked=0, errors=[])
    try:
        import aiohttp
        from services import candle_cache

        # Symbole mit offenen Trades ebenfalls priorisieren
        prio = _priority_symbols()
        try:
            open_syms = await state.db.auto_trades.distinct("symbol", {"status": "open"})
            head = [s for s in prio if s in set(open_syms)
                    or any(ev.get("symbol") == s for ev in state.open_signal_evals)]
            prio = head + [s for s in prio if s not in head]
        except Exception:
            pass
        STATUS["symbols_total"] = len(prio)

        timeout = aiohttp.ClientTimeout(total=180)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for sym in prio:
                STATUS["current"] = sym
                try:
                    candles = await candle_cache.get_candles(session, sym, DAYS)
                    STATUS["candles_loaded"] += len(candles)
                    lab = await _label_downtime_signals(sym, candles)
                    STATUS["signals_checked"] += lab["checked"]
                    STATUS["signals_labeled_win"] += lab["win"]
                    STATUS["signals_labeled_loss"] += lab["loss"]
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    STATUS["errors"].append(f"{sym}: {str(e)[:120]}")
                    logger.warning(f"boot_backfill {sym} fehlgeschlagen: {e}")
                STATUS["symbols_done"] += 1
                await asyncio.sleep(PAUSE)
        STATUS.update(state="done", current=None, finished_at=_now_iso())
        logger.info(
            f"boot_backfill fertig: {STATUS['symbols_done']}/{STATUS['symbols_total']} "
            f"Symbole, {STATUS['candles_loaded']} Kerzen, Downtime-Labels: "
            f"{STATUS['signals_labeled_win']}W/{STATUS['signals_labeled_loss']}L "
            f"({STATUS['signals_checked']} geprüft), {len(STATUS['errors'])} Fehler")
    except asyncio.CancelledError:
        STATUS.update(state="idle", current=None)
        raise
    except Exception as e:
        STATUS.update(state="error", current=None, finished_at=_now_iso())
        STATUS["errors"].append(f"fatal: {str(e)[:200]}")
        logger.error(f"boot_backfill abgebrochen: {e}")
    finally:
        _running = False
    return STATUS


def status() -> Dict:
    from services import candle_cache
    c = candle_cache.stats()
    return {**STATUS,
            "cache": {"symbols": c["symbols"], "total_candles": c["total_candles"],
                      "ram_mb": c["ram_mb"], "cache_dir": c["cache_dir"]}}
