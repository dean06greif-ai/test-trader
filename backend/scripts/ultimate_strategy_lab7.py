"""Runde 7: Diagnose Long vs. Short über alle 12 Coins (Daten im Cache).
Frage: Verlieren die Shorts das Geld (Alt-Squeezes) oder die Longs (Bleed)?
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiohttp
import numpy as np

from services.backtester import fetch_history, simulate_pair
from services.timeframes import aggregate_candles
from scripts.ultimate_strategy_lab5 import signals, BASE_CFG

ALL = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT",
       "ADAUSDT", "LINKUSDT", "AVAXUSDT", "DOTUSDT", "LTCUSDT", "NEARUSDT"]
DAYS = 730


def run(name, data, side_filter=None, adx_min=20, er_min=0.4, cfg_over=None):
    cfg = dict(BASE_CFG)
    cfg["trail_atr_mult"] = 1.5
    if cfg_over:
        cfg.update(cfg_over)
    if side_filter:
        cfg["allowed_sides"] = [side_filter]
    total, n_tot = 0.0, 0
    per = {}
    for sym, cs in data.items():
        long_ok, short_ok = signals(cs, N=48, adx_min=adx_min, er_min=er_min)

        def provider(i, cs=cs, lo=long_ok, so=short_ok):
            if lo[i]:
                return {"type": "LONG", "signal_class": "SIGNAL",
                        "entry_price": cs[i]["close"]}
            if so[i]:
                return {"type": "SHORT", "signal_class": "SIGNAL",
                        "entry_price": cs[i]["close"]}
            return None

        r = simulate_pair(None, cs, sym, {}, cfg, signal_provider=provider)
        per[sym] = round(r["pnl"], 1)
        total += r["pnl"]
        n_tot += r["trades"]
    pos = sum(1 for v in per.values() if v > 0)
    print(f"[{name}] total={total:+9.1f} trades={n_tot} posSyms={pos}/{len(per)}\n"
          f"  perSym={per}", flush=True)


async def main():
    data = {}
    async with aiohttp.ClientSession() as session:
        for sym in ALL:
            hist = await fetch_history(session, sym, DAYS)
            data[sym] = aggregate_candles(hist, "2h", drop_partial=True)
    print("data ready", flush=True)
    run("beide Seiten", data)
    run("nur LONG", data, "LONG")
    run("nur SHORT", data, "SHORT")
    run("LONG + SHORT nur bei ADX>=30", data, adx_min=20)  # ref nochmal
    # Short-Signale strenger: eigene Signalrechnung mit adx 30 nur für Short
    total, n_tot, per = 0.0, 0, {}
    cfg = dict(BASE_CFG)
    cfg["trail_atr_mult"] = 1.5
    for sym, cs in data.items():
        lo1, _ = signals(cs, N=48, adx_min=20, er_min=0.4)
        _, so2 = signals(cs, N=48, adx_min=30, er_min=0.5)

        def provider(i, cs=cs, lo=lo1, so=so2):
            if lo[i]:
                return {"type": "LONG", "signal_class": "SIGNAL",
                        "entry_price": cs[i]["close"]}
            if so[i]:
                return {"type": "SHORT", "signal_class": "SIGNAL",
                        "entry_price": cs[i]["close"]}
            return None

        r = simulate_pair(None, cs, sym, {}, cfg, signal_provider=provider)
        per[sym] = round(r["pnl"], 1)
        total += r["pnl"]
        n_tot += r["trades"]
    pos = sum(1 for v in per.values() if v > 0)
    print(f"[LONG adx20/er0.4 + SHORT adx30/er0.5] total={total:+9.1f} "
          f"trades={n_tot} posSyms={pos}/{len(per)}\n  perSym={per}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
