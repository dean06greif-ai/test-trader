"""Basis-Endpoints: Health, Coins, Klines, Signale, Settings, Session, System."""
import logging

from fastapi import APIRouter, Depends, HTTPException
from typing import Dict

from core import state
from core.auth import require_admin
from core.config import TOP_10_COINS, ALL_SYMBOLS, TRADABLE_SYMBOLS
from core import instruments
from core.state import scanner, feed, telegram
from core.utils import _clean

logger = logging.getLogger(__name__)

router = APIRouter(tags=["general"])


@router.get("/")
async def root():
    return {"app": "Crypto Scalping Scanner", "status": "running"}


@router.get("/api/health")
async def health_check():
    return {"status": "alive"}


@router.get("/api/debug/status")
async def debug_status():
    from services import candle_cache as _cc
    return {"data_feed": feed.status, "session_active": scanner.is_trading_session(),
            "enabled_strategies": scanner.enabled_strategies(),
            "candle_cache": _cc.stats(),
            "coins": [scanner.debug_snapshot(s) for s in ALL_SYMBOLS]}


@router.get("/api/coins")
async def get_coins():
    """Alle wählbaren Instrumente + Gruppierung (Krypto/Resources/Indices/Forex).

    `coins` bleibt die flache Symbol-Liste (Backtester/Optimizer nutzen sie),
    `groups` liefert die Sidebar-Struktur inkl. Handelbarkeit.
    """
    return {"coins": list(ALL_SYMBOLS),
            "crypto": list(TOP_10_COINS),
            "tradable": list(TRADABLE_SYMBOLS),
            "groups": instruments.groups()}


@router.get("/api/klines/{symbol}")
async def get_klines(symbol: str, limit: int = 200):
    """Historical candles for the chart (fixes empty/black chart)."""
    try:
        candles = await instruments.fetch_live_candles(symbol, limit)
        return {"symbol": symbol, "candles": candles[-limit:]}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/api/klines/{symbol}/history")
async def get_klines_history(symbol: str, days: int = 7, timeframe: str = None):
    """Längere Chart-Historie (1 Woche / 1 Monat / 1 Jahr), aggregiert auf ein
    zur Spanne passendes Timeframe – für die Lade-Buttons im Haupt-Chart."""
    import aiohttp
    from services import candle_cache
    from services import macro_context as mc
    from services.timeframes import aggregate_candles, TIMEFRAMES
    from core import instruments
    days = max(1, min(int(days), 365))
    inst = instruments.get(symbol.upper())
    is_crypto = inst is None or inst.live_source != "yahoo"
    if days > 45 and is_crypto and timeframe is None:
        # 1 Jahr: Tages-Kerzen direkt von der Börse (1m-Cache wäre zu schwer)
        async with aiohttp.ClientSession(headers=mc._HEADERS) as session:
            rows = []
            try:
                data = await mc._get_json(
                    session, "https://data-api.binance.vision/api/v3/klines",
                    {"symbol": symbol.upper(), "interval": "1d", "limit": min(days, 1000)})
                rows = [{"timestamp": int(k[0]), "open": float(k[1]), "high": float(k[2]),
                         "low": float(k[3]), "close": float(k[4]), "volume": float(k[5])}
                        for k in (data or [])]
            except Exception:
                rows = []
            if not rows:
                rows = await mc.fetch_klines(session, symbol.upper(), "1d", days)
        if not rows:
            raise HTTPException(status_code=502, detail=f"Keine Historie für {symbol}")
        return {"symbol": symbol, "days": days, "timeframe": "1d", "candles": rows}
    tf = timeframe if timeframe in TIMEFRAMES else (
        "5m" if days <= 2 else "15m" if days <= 10 else "1h" if days <= 45 else "4h")
    try:
        async with aiohttp.ClientSession() as session:
            candles = await candle_cache.get_candles(session, symbol, days)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)[:200])
    if not candles:
        raise HTTPException(status_code=502, detail=f"Keine Historie für {symbol}")
    if tf != "1m":
        candles = aggregate_candles(candles, tf)
    rows = candles.to_list() if hasattr(candles, "to_list") else list(candles)
    return {"symbol": symbol, "days": days, "timeframe": tf, "candles": rows}


@router.get("/api/signals")
async def get_signals(limit: int = 50, strategy_id: str = None):
    q = {"trade_date": scanner.berlin_date()}
    if strategy_id:
        q["strategy_id"] = strategy_id
    signals = await state.db.signals.find(q).sort("timestamp", -1).limit(limit).to_list(limit)
    return {"signals": [_clean(s) for s in signals]}


@router.get("/api/rule-states")
async def get_rule_states(symbol: str = None):
    if symbol:
        return {"symbol": symbol, "states": scanner.rule_states.get(symbol, {})}
    return {"states": scanner.rule_states}


@router.get("/api/settings")
async def get_settings():
    return scanner.settings


@router.post("/api/settings")
async def update_settings(settings: Dict, _: bool = Depends(require_admin)):
    scanner.update_settings(settings)
    await state.db.settings.update_one({"_id": "scanner_settings"},
                                       {"$set": scanner.settings}, upsert=True)
    return {"status": "success", "settings": scanner.settings}


@router.get("/api/session/status")
async def session_status():
    now = scanner.berlin_now()
    return {"is_active": scanner.is_trading_session(),
            "current_session": scanner.get_current_session(),
            "custom_sessions": scanner.settings.get("custom_sessions", []),
            "pre_signal_enabled": scanner.settings.get("pre_signal_enabled", True),
            "berlin_time": now.strftime("%H:%M:%S"), "berlin_date": scanner.berlin_date()}


# ---------------- System / RAM / Cache ----------------
@router.get("/api/system/ram")
async def system_ram():
    """RAM-Auslastung inkl. Bewertung, was viel Speicher braucht."""
    import psutil
    from services import candle_cache
    from services import backtester as bt
    proc = psutil.Process()
    rss_mb = proc.memory_info().rss / 1024 / 1024
    vm = psutil.virtual_memory()
    cstats = candle_cache.stats()
    # ~500 Bytes pro Kerze im dict-Format (Messung)
    cache_mb = cstats["total_candles"] * 500 / 1024 / 1024
    export_candles = 0
    export_trades = 0
    for j in bt.JOBS.values():
        export_candles += sum(len(v) for v in (j.get("export_candles") or {}).values())
        export_trades += len(j.get("export_trades") or [])
    export_mb = export_candles * 500 / 1024 / 1024 + export_trades * 900 / 1024 / 1024
    return {
        "process_rss_mb": round(rss_mb, 1),
        "system_total_mb": round(vm.total / 1024 / 1024),
        "system_available_mb": round(vm.available / 1024 / 1024),
        "system_used_percent": vm.percent,
        "candle_cache": {
            "symbols": cstats["symbols"],
            "total_candles": cstats["total_candles"],
            "estimated_mb": round(cache_mb, 1),
            "max_candles": cstats["max_candles"],
            "disk_enabled": cstats["disk_enabled"],
        },
        "backtest_exports": {
            "candles": export_candles, "trades": export_trades,
            "estimated_mb": round(export_mb, 1),
        },
        "breakdown_hint": {
            "kerzen_cache": f"~{round(cache_mb, 1)} MB (größter Posten bei langen Zeiträumen)",
            "backtest_export": f"~{round(export_mb, 1)} MB (Kerzen/Trades des letzten Laufs für CSV)",
            "fast_path": "FastSeries: ~8 Bytes/Kerze pro Indikator-Serie, wird nach jedem "
                         "Symbol wieder freigegeben (gering)",
            "basis": "Python + Bibliotheken: ~150-250 MB Grundlast",
        },
    }


@router.post("/api/system/cache/clear")
async def system_cache_clear(_: bool = Depends(require_admin)):
    """Kerzen-Cache leeren + Backtest-Export-Rohdaten freigeben (RAM-Reset)."""
    import gc
    from services import candle_cache
    from services import backtester as bt
    before = candle_cache.stats()["total_candles"]
    candle_cache.clear()
    for j in bt.JOBS.values():
        j.pop("export_candles", None)
        j.pop("export_trades", None)
    gc.collect()
    return {"status": "cleared", "candles_freed": before}


@router.get("/api/system/boot-backfill")
async def boot_backfill_status():
    """Fix 0.6: Fortschritt des Kerzen-Backfills nach Neustart (Rebuild-on-Boot)."""
    from services import boot_backfill
    return boot_backfill.status()


@router.post("/api/system/boot-backfill/run")
async def boot_backfill_run(_: bool = Depends(require_admin)):
    """Backfill manuell (erneut) starten – z.B. zum Testen. Läuft im Hintergrund."""
    import asyncio
    from services import boot_backfill
    if boot_backfill.STATUS.get("state") == "running":
        return {"status": "already_running", **boot_backfill.status()}
    asyncio.create_task(boot_backfill.run_boot_backfill())
    return {"status": "started"}


@router.get("/api/audit-log")
async def audit_log_list(limit: int = 50, _: bool = Depends(require_admin)):
    """Lösch-Protokoll: wer hat wann was gelöscht (nur Admin)."""
    docs = await state.db.audit_log.find({}, {"_id": 0}) \
        .sort("ts", -1).to_list(min(max(limit, 1), 200))
    return {"entries": docs}


@router.get("/api/system/indicator-cache")
async def indicator_cache_stats():
    """Statistiken der Indikator-Bibliothek (Deep-Test-Beschleuniger)."""
    from services import indicator_cache
    return indicator_cache.stats()


@router.post("/api/system/indicator-cache/clear")
async def indicator_cache_clear(_: bool = Depends(require_admin)):
    """Indikator-Bibliothek leeren (Disk + Memory)."""
    from services import indicator_cache
    removed = indicator_cache.clear()
    return {"status": "cleared", "files_removed": removed}


@router.post("/api/telegram/test")
async def test_telegram(_: bool = Depends(require_admin)):
    if not telegram.bot:
        raise HTTPException(status_code=400, detail="Telegram not configured")
    if await telegram.send_test_message():
        return {"status": "success"}
    raise HTTPException(status_code=500, detail="Failed")
