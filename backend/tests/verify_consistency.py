"""Verifikations-Läufe: Fast-Path vs. Legacy, Hebel/Liquidation, Break-Even, Timeframe.
Aufruf: python tests/verify_consistency.py  (aus /app/backend)
"""
import asyncio
import json
import sys
import time

sys.path.insert(0, ".")

import aiohttp

from services import candle_cache, fast_sim
from services.backtester import simulate_pair
from services.timeframes import aggregate_candles
from strategies.registry import registry
from strategies.custom_strategy import CustomStrategy

SYMBOL = "BTCUSDT"
DAYS = 14
CFG = {"max_capital": 100.0, "leverage": 10, "fee_percent": 0.06,
       "tp1_crv": 1.0, "tp_full_crv": 2.0, "tp1_close_percent": 50,
       "sl_mode": "structure", "sl_lookback": 10, "be_mode": "tp1"}

BUILTINS = ["rsi_only", "macd_rsi_momentum", "scalping_4_rules",
            "bollinger_reversion", "bollinger_squeeze", "ema_pullback_scalping",
            "stoch_reversal", "vwap_reversion", "ict_liquidity_sweep"]

CUSTOM_DEF = {
    "id": "verify_custom", "name": "Verify Custom", "timeframe": "5m",
    "indicators": {"rsi_period": 14, "ema_fast_period": 9, "ema_slow_period": 50},
    "long_rules": [{"indicator": "rsi", "op": "<", "value": 30}],
    "short_rules": [{"indicator": "rsi", "op": ">", "value": 70}],
    "sl_mode": "percent", "sl_percent": 1.0, "crv_target": 2.0,
}


async def fetch(symbol, days):
    async with aiohttp.ClientSession() as s:
        return await candle_cache.get_candles(s, symbol, days)


def run(strat, candles, cfg, settings=None, provider=None):
    t0 = time.time()
    r = simulate_pair(strat, candles, SYMBOL, settings or {}, cfg,
                      collect_trades=True, signal_provider=provider)
    r["_secs"] = round(time.time() - t0, 2)
    return r


def cmp_key(r):
    return {"trades": r["trades"], "pnl": r["pnl"], "wins": r["wins"],
            "losses": r["losses"], "fees": r["fees"], "max_dd": r["max_drawdown"],
            "liq": r.get("liquidations", 0), "be_moved": r.get("be_moved", 0),
            "secs": r["_secs"]}


def main():
    history = asyncio.run(fetch(SYMBOL, DAYS))
    print(f"Kerzen 1m: {len(history)}")
    report = {}

    # ---- 1) Fast vs Legacy Parität + Speed ----
    parity = {}
    for sid in BUILTINS:
        strat = registry.get(sid)
        if not strat:
            continue
        tf = getattr(strat, "STRATEGY_TIMEFRAME", "1m")
        candles = aggregate_candles(history, tf) if tf != "1m" else history
        fs = fast_sim.FastSeries(candles)
        provider = fast_sim.build_builtin_signal_provider(strat, fs, {}, SYMBOL)
        legacy = run(strat, candles, CFG)
        entry = {"timeframe": tf, "has_fast_path": provider is not None,
                 "legacy": cmp_key(legacy)}
        if provider is not None:
            fast = run(strat, candles, CFG, provider=provider)
            entry["fast"] = cmp_key(fast)
            entry["identical_trades"] = fast["trades"] == legacy["trades"]
            entry["pnl_diff"] = round(abs(fast["pnl"] - legacy["pnl"]), 2)
            entry["speedup"] = round(legacy["_secs"] / max(fast["_secs"], 0.001), 1)
        parity[sid] = entry
        print(sid, json.dumps(entry))
    report["fast_vs_legacy"] = parity

    # ---- 2) Custom (Discovery) Fast vs Legacy + Timeframe-Effekt ----
    cs = CustomStrategy(CUSTOM_DEF)
    tf_res = {}
    for tf in ("1m", "5m", "15m"):
        candles = aggregate_candles(history, tf) if tf != "1m" else history
        fs = fast_sim.FastSeries(candles)
        prov = fast_sim.build_signal_provider(CUSTOM_DEF, fs)
        legacy = run(cs, candles, CFG)
        fast = run(cs, candles, CFG, provider=prov)
        tf_res[tf] = {"legacy": cmp_key(legacy), "fast": cmp_key(fast),
                      "identical_trades": fast["trades"] == legacy["trades"],
                      "pnl_diff": round(abs(fast["pnl"] - legacy["pnl"]), 2)}
        print("custom", tf, json.dumps(tf_res[tf]))
    report["custom_timeframe"] = tf_res

    # ---- 3) Hebel 10x vs 100x (Liquidation) ----
    strat = registry.get("rsi_only")
    lev_res = {}
    for lev in (10, 100):
        cfg = dict(CFG, leverage=lev)
        fs = fast_sim.FastSeries(history)
        prov = fast_sim.build_builtin_signal_provider(strat, fs, {}, SYMBOL)
        r = run(strat, history, cfg, provider=prov)
        lev_res[f"{lev}x"] = cmp_key(r)
        print("leverage", lev, json.dumps(lev_res[f"{lev}x"]))
    report["leverage"] = lev_res

    # ---- 4) Break-Even Modi ----
    be_res = {}
    for mode in ("off", "tp1", "crv", "profit_pct", "smart"):
        cfg = dict(CFG, be_mode=mode, be_trigger_crv=0.7, be_trigger_profit_pct=20)
        fs = fast_sim.FastSeries(history)
        prov = fast_sim.build_builtin_signal_provider(strat, fs, {}, SYMBOL)
        r = run(strat, history, cfg, provider=prov)
        be_res[mode] = cmp_key(r)
        print("be", mode, json.dumps(be_res[mode]))
    report["breakeven_modes"] = be_res

    with open("/app/test_reports/verify_consistency.json", "w") as f:
        json.dump(report, f, indent=1)
    print("saved /app/test_reports/verify_consistency.json")


if __name__ == "__main__":
    main()
