"""Runde 6 (final): ER-Feintuning + Out-of-Universe-Validierung.
Die finalen Parameter werden auf 4 NIE zuvor getesteten Coins geprüft
(AVAX, DOT, LTC, NEAR) – der ehrlichste Robustheits-Check gegen Overfitting.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiohttp

from services.backtester import fetch_history
from services.timeframes import aggregate_candles
from scripts.ultimate_strategy_lab5 import run_folds, BASE_CFG

TRAIN_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
                 "DOGEUSDT", "ADAUSDT", "LINKUSDT"]
OOU_SYMBOLS = ["AVAXUSDT", "DOTUSDT", "LTCUSDT", "NEARUSDT"]
DAYS = 730


async def load(symbols):
    data = {}
    async with aiohttp.ClientSession() as session:
        for sym in symbols:
            try:
                hist = await fetch_history(session, sym, DAYS)
                data[sym] = aggregate_candles(hist, "2h", drop_partial=True)
                print(f"{sym}: {len(data[sym])}", flush=True)
            except Exception as e:
                print(f"{sym} failed: {e}", flush=True)
    return data


async def main():
    data = await load(TRAIN_SYMBOLS)
    run_folds("FINAL don48 adx20 er0.4 (ref)", data, dict(N=48, er_min=0.4))
    run_folds("don48 adx20 er0.5", data, dict(N=48, er_min=0.5))
    run_folds("don48 adx25 er0.4", data, dict(N=48, adx_min=25, er_min=0.4))
    run_folds("don48 adx20 er0.4 trail1.5", data, dict(N=48, er_min=0.4),
              {"trail_atr_mult": 1.5})

    print("\n===== OUT-OF-UNIVERSE (nie getestete Coins) =====", flush=True)
    oou = await load(OOU_SYMBOLS)
    run_folds("OOU don48 adx20 er0.4", oou, dict(N=48, er_min=0.4))
    run_folds("OOU don48 adx20 er0.4 trail1.5", oou, dict(N=48, er_min=0.4),
              {"trail_atr_mult": 1.5})


if __name__ == "__main__":
    asyncio.run(main())
