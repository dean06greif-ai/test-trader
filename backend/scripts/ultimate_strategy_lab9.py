"""Runde 9: Live-machbare Variante (Warmup <=135 Bars wegen Buffer-Limit 140).
EMA50/200 -> EMA25/100, ER(120) -> ER(100). Vergleich gegen Referenz.
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


def signals(candles, N=48, ema_f=25, ema_s=100, atr_mult=3.0, fee_mult=8.0,
            adx_min=20.0, er_min=0.4, er2_n=100, er2_min=0.15):
    h = np.array([c["high"] for c in candles], float)
    lo = np.array([c["low"] for c in candles], float)
    c = np.array([x["close"] for x in candles], float)
    ef, es = vec.ema(c, ema_f), vec.ema(c, ema_s)
    atr = vec.atr(h, lo, c, 14)
    hh = pd.Series(h).rolling(N).max().shift(1).to_numpy()
    ll = pd.Series(lo).rolling(N).min().shift(1).to_numpy()
    with np.errstate(invalid="ignore", divide="ignore"):
        af = (atr / c * 100 * atr_mult) >= fee_mult * 0.12
        adx, pdi, mdi = _adx(h, lo, c, 14)
        axf_l = (adx >= adx_min) & (pdi > mdi)
        axf_s = (adx >= adx_min) & (mdi > pdi)
        erf = efficiency_ratio(c, 20) >= er_min if er_min > 0 else np.ones(len(c), bool)
        gate = efficiency_ratio(c, er2_n) >= er2_min if er2_n > 0 else np.ones(len(c), bool)
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
    run("REF ema50/200 er120", data, dict(ema_f=50, ema_s=200, er2_n=120))
    run("LIVE ema25/100 er100", data, dict())
    run("LIVE ema30/120 er100", data, dict(ema_f=30, ema_s=120))
    run("LIVE ema25/100 er100 adx25", data, dict(adx_min=25))
    run("LIVE ema25/100 er100/0.18", data, dict(er2_min=0.18))
    run("LIVE don32", data, dict(N=32))


if __name__ == "__main__":
    asyncio.run(main())
