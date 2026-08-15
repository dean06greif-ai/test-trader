"""Runde 8: Asset-Regime-Gate – zusätzliches Langfrist-Filter (ER über ~10-20
Tage bzw. EMA200-Distanz in ATR), damit Chop-/Bleed-Coins automatisch pausieren.
Ziel: 12/12-Universum netto klar positiv, ohne Coin-Cherry-Picking.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiohttp
import numpy as np
import pandas as pd

from services.backtester import fetch_history, simulate_pair
from services.timeframes import aggregate_candles
from services import vec
from scripts.ultimate_strategy_lab4 import _adx
from scripts.ultimate_strategy_lab5 import efficiency_ratio, BASE_CFG

ALL = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT",
       "ADAUSDT", "LINKUSDT", "AVAXUSDT", "DOTUSDT", "LTCUSDT", "NEARUSDT"]
DAYS = 730
FOLDS = 4


def signals(candles, N=48, atr_mult=3.0, fee_mult=8.0, adx_min=20.0,
            er_min=0.4, er2_n=0, er2_min=0.0, ema_dist_atr=0.0):
    h = np.array([c["high"] for c in candles], float)
    lo = np.array([c["low"] for c in candles], float)
    c = np.array([x["close"] for x in candles], float)
    ef, es = vec.ema(c, 50), vec.ema(c, 200)
    atr = vec.atr(h, lo, c, 14)
    hh = pd.Series(h).rolling(N).max().shift(1).to_numpy()
    ll = pd.Series(lo).rolling(N).min().shift(1).to_numpy()
    with np.errstate(invalid="ignore", divide="ignore"):
        af = (atr / c * 100 * atr_mult) >= fee_mult * 0.12
        adx, pdi, mdi = _adx(h, lo, c, 14)
        axf_l = (adx >= adx_min) & (pdi > mdi)
        axf_s = (adx >= adx_min) & (mdi > pdi)
        erf = efficiency_ratio(c, 20) >= er_min if er_min > 0 else np.ones(len(c), bool)
        gate = np.ones(len(c), bool)
        if er2_n > 0:
            gate &= efficiency_ratio(c, er2_n) >= er2_min
        if ema_dist_atr > 0:
            gate &= np.abs(c - es) / np.maximum(atr, 1e-12) >= ema_dist_atr
        long_ok = (c > hh) & (ef > es) & (c > es) & af & axf_l & erf & gate
        short_ok = (c < ll) & (ef < es) & (c < es) & af & axf_s & erf & gate
    return long_ok, short_ok


def run(name, data, sig_kwargs):
    cfg = dict(BASE_CFG)
    cfg["trail_atr_mult"] = 1.5
    fold_pnl = [0.0] * FOLDS
    total, n_tot, per = 0.0, 0, {}
    for sym, cs in data.items():
        long_ok, short_ok = signals(cs, **sig_kwargs)

        def provider_for(offset, cs=cs, lo=long_ok, so=short_ok):
            def p(i):
                j = offset + i
                if lo[j]:
                    return {"type": "LONG", "signal_class": "SIGNAL",
                            "entry_price": cs[j]["close"]}
                if so[j]:
                    return {"type": "SHORT", "signal_class": "SIGNAL",
                            "entry_price": cs[j]["close"]}
                return None
            return p

        L = len(cs)
        sym_tot = 0.0
        for f in range(FOLDS):
            a, b = int(L * f / FOLDS), int(L * (f + 1) / FOLDS)
            r = simulate_pair(None, cs[a:b], sym, {}, cfg,
                              signal_provider=provider_for(a))
            fold_pnl[f] += r["pnl"]
            sym_tot += r["pnl"]
            n_tot += r["trades"]
        per[sym] = round(sym_tot, 1)
        total += sym_tot
    pos = sum(1 for v in per.values() if v > 0)
    folds_s = " | ".join(f"F{f+1}: {fold_pnl[f]:+7.1f}" for f in range(FOLDS))
    print(f"[{name}] total={total:+9.1f} trades={n_tot} posSyms={pos}/{len(per)} "
          f"posFolds={sum(1 for p in fold_pnl if p > 0)}/{FOLDS}\n  {folds_s}\n  perSym={per}",
          flush=True)


async def main():
    data = {}
    async with aiohttp.ClientSession() as session:
        for sym in ALL:
            hist = await fetch_history(session, sym, DAYS)
            data[sym] = aggregate_candles(hist, "2h", drop_partial=True)
    print("data ready", flush=True)
    run("ref (kein Gate)", data, dict())
    run("gate ER(120)>=0.15", data, dict(er2_n=120, er2_min=0.15))
    run("gate ER(120)>=0.20", data, dict(er2_n=120, er2_min=0.20))
    run("gate ER(240)>=0.12", data, dict(er2_n=240, er2_min=0.12))
    run("gate |c-ema200|>=2ATR", data, dict(ema_dist_atr=2.0))
    run("gate |c-ema200|>=3ATR", data, dict(ema_dist_atr=3.0))
    run("gate ER120>=0.15 + 2ATR", data, dict(er2_n=120, er2_min=0.15,
                                              ema_dist_atr=2.0))


if __name__ == "__main__":
    asyncio.run(main())
