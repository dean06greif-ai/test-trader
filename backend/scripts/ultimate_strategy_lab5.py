"""Runde 5: F4-Chop bekämpfen – Kaufman Efficiency Ratio, größere Donchian-N,
defensivere Exits. Gleiche Walk-Forward-Methodik wie Runde 4 (4 Folds, 8 Coins, 2h).
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

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
           "DOGEUSDT", "ADAUSDT", "LINKUSDT"]
DAYS = 730
FOLDS = 4

BASE_CFG = {"fee_percent": 0.06, "max_capital": 100.0, "leverage": 5,
            "sl_mode": "atr", "atr_period": 14, "atr_sl_multiplier": 3.0,
            "tp_mode": "crv", "tp1_crv": 1.5, "tp_full_crv": 5.0,
            "tp1_close_percent": 40, "be_mode": "tp1",
            "trail_after_tp1": True, "trail_atr_mult": 2.5,
            "min_risk_percent": 0.25}


def efficiency_ratio(c, n=20):
    delta = pd.Series(c).diff().abs().rolling(n).sum().to_numpy()
    move = np.abs(c - np.roll(c, n))
    move[:n] = 0
    with np.errstate(invalid="ignore", divide="ignore"):
        er = move / np.maximum(delta, 1e-12)
    return er


def signals(candles, N=48, atr_mult=3.0, fee_mult=8.0, adx_min=20.0,
            er_min=0.0, er_n=20):
    h = np.array([c["high"] for c in candles], float)
    lo = np.array([c["low"] for c in candles], float)
    c = np.array([x["close"] for x in candles], float)
    ef, es = vec.ema(c, 50), vec.ema(c, 200)
    atr = vec.atr(h, lo, c, 14)
    hh = pd.Series(h).rolling(N).max().shift(1).to_numpy()
    ll = pd.Series(lo).rolling(N).min().shift(1).to_numpy()
    with np.errstate(invalid="ignore", divide="ignore"):
        af = (atr / c * 100 * atr_mult) >= fee_mult * 0.12
        if adx_min > 0:
            adx, pdi, mdi = _adx(h, lo, c, 14)
            axf_l = (adx >= adx_min) & (pdi > mdi)
            axf_s = (adx >= adx_min) & (mdi > pdi)
        else:
            axf_l = axf_s = np.ones(len(c), bool)
        erf = efficiency_ratio(c, er_n) >= er_min if er_min > 0 \
            else np.ones(len(c), bool)
        long_ok = (c > hh) & (ef > es) & (c > es) & af & axf_l & erf
        short_ok = (c < ll) & (ef < es) & (c < es) & af & axf_s & erf
    return long_ok, short_ok


def run_folds(name, data, sig_kwargs, cfg_over=None):
    cfg = dict(BASE_CFG)
    if cfg_over:
        cfg.update(cfg_over)
    fold_pnl = [0.0] * FOLDS
    fold_n = [0] * FOLDS
    per_sym = {}
    for sym, cs in data.items():
        long_ok, short_ok = signals(cs, **sig_kwargs)

        def provider_for(offset):
            def provider(i):
                j = offset + i
                if long_ok[j]:
                    return {"type": "LONG", "signal_class": "SIGNAL",
                            "entry_price": cs[j]["close"]}
                if short_ok[j]:
                    return {"type": "SHORT", "signal_class": "SIGNAL",
                            "entry_price": cs[j]["close"]}
                return None
            return provider

        L = len(cs)
        tot = 0.0
        for f in range(FOLDS):
            a, b = int(L * f / FOLDS), int(L * (f + 1) / FOLDS)
            r = simulate_pair(None, cs[a:b], sym, {}, cfg,
                              signal_provider=provider_for(a))
            fold_pnl[f] += r["pnl"]
            fold_n[f] += r["trades"]
            tot += r["pnl"]
        per_sym[sym] = round(tot, 1)
    folds_s = " | ".join(f"F{f+1}: {fold_pnl[f]:+7.1f} (n={fold_n[f]})"
                         for f in range(FOLDS))
    print(f"[{name}]\n  {folds_s}\n  posFolds={sum(1 for p in fold_pnl if p > 0)}/{FOLDS} "
          f"posSyms={sum(1 for v in per_sym.values() if v > 0)}/{len(data)} "
          f"total={sum(fold_pnl):+8.1f} trades={sum(fold_n)}\n  perSym={per_sym}",
          flush=True)


async def main():
    data = {}
    async with aiohttp.ClientSession() as session:
        for sym in SYMBOLS:
            hist = await fetch_history(session, sym, DAYS)
            data[sym] = aggregate_candles(hist, "2h", drop_partial=True)
    print("data ready", flush=True)

    run_folds("don48 adx20 (ref)", data, dict(N=48))
    run_folds("don48 adx20 er0.3", data, dict(N=48, er_min=0.3))
    run_folds("don48 adx20 er0.4", data, dict(N=48, er_min=0.4))
    run_folds("don96 adx20", data, dict(N=96))
    run_folds("don96 adx20 er0.3", data, dict(N=96, er_min=0.3))
    run_folds("don48 adx20 tp1@1R/50%", data, dict(N=48),
              {"tp1_crv": 1.0, "tp1_close_percent": 50})
    run_folds("don48 adx20 be_crv0.7", data, dict(N=48),
              {"be_mode": "crv", "be_trigger_crv": 0.7})
    run_folds("don48 adx20 trail1.5", data, dict(N=48), {"trail_atr_mult": 1.5})
    run_folds("don48 adx20 er0.3 tp1@1R/50 becrv0.7", data,
              dict(N=48, er_min=0.3),
              {"tp1_crv": 1.0, "tp1_close_percent": 50,
               "be_mode": "crv", "be_trigger_crv": 0.7})
    run_folds("don48 adx20 atr2.5 tpf4", data, dict(N=48, atr_mult=2.5),
              {"atr_sl_multiplier": 2.5, "tp_full_crv": 4.0})


if __name__ == "__main__":
    asyncio.run(main())
