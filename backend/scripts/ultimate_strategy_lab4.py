"""Runde 4: Walk-Forward (4 Folds à ~180d) über 8 Coins auf 2h.
Filter-Kandidaten gegen Chop: ADX-Minimum, EMA-Gap-Minimum, Long/Short-Asymmetrie.
Daten kommen aus dem candle_cache (bereits von Runde 3 gefüllt).

Aufruf: python scripts/ultimate_strategy_lab4.py [days]
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

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
           "DOGEUSDT", "ADAUSDT", "LINKUSDT"]
DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 730
FOLDS = 4

CFG = {"fee_percent": 0.06, "max_capital": 100.0, "leverage": 5,
       "sl_mode": "atr", "atr_period": 14, "atr_sl_multiplier": 3.0,
       "tp_mode": "crv", "tp1_crv": 1.5, "tp_full_crv": 5.0,
       "tp1_close_percent": 40, "be_mode": "tp1",
       "trail_after_tp1": True, "trail_atr_mult": 2.5,
       "min_risk_percent": 0.25}


def _adx(h, lo, c, period=14):
    up = np.diff(h, prepend=h[0])
    dn = -np.diff(lo, prepend=lo[0])
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = np.maximum(h - lo, np.maximum(abs(h - np.roll(c, 1)), abs(lo - np.roll(c, 1))))
    tr[0] = h[0] - lo[0]
    alpha = 1.0 / period
    atr = pd.Series(tr).ewm(alpha=alpha, adjust=False).mean().to_numpy()
    pdi = 100 * pd.Series(plus_dm).ewm(alpha=alpha, adjust=False).mean().to_numpy() / np.maximum(atr, 1e-12)
    mdi = 100 * pd.Series(minus_dm).ewm(alpha=alpha, adjust=False).mean().to_numpy() / np.maximum(atr, 1e-12)
    dx = 100 * abs(pdi - mdi) / np.maximum(pdi + mdi, 1e-12)
    adx = pd.Series(dx).ewm(alpha=alpha, adjust=False).mean().to_numpy()
    return adx, pdi, mdi


def signals(candles, N=24, atr_mult=3.0, fee_mult=8.0, adx_min=0.0,
            gap_min=0.0, allow_short=True):
    h = np.array([c["high"] for c in candles], float)
    lo = np.array([c["low"] for c in candles], float)
    c = np.array([x["close"] for x in candles], float)
    ef, es = vec.ema(c, 50), vec.ema(c, 200)
    atr = vec.atr(h, lo, c, 14)
    hh = pd.Series(h).rolling(N).max().shift(1).to_numpy()
    ll = pd.Series(lo).rolling(N).min().shift(1).to_numpy()
    with np.errstate(invalid="ignore", divide="ignore"):
        af = (atr / c * 100 * atr_mult) >= fee_mult * 0.12
        gap = abs(ef - es) / c * 100
        gf = gap >= gap_min if gap_min > 0 else np.ones(len(c), bool)
        if adx_min > 0:
            adx, pdi, mdi = _adx(h, lo, c, 14)
            axf_l = (adx >= adx_min) & (pdi > mdi)
            axf_s = (adx >= adx_min) & (mdi > pdi)
        else:
            axf_l = axf_s = np.ones(len(c), bool)
        long_ok = (c > hh) & (ef > es) & (c > es) & af & gf & axf_l
        short_ok = (c < ll) & (ef < es) & (c < es) & af & gf & axf_s
        if not allow_short:
            short_ok = np.zeros(len(c), bool)
    return long_ok, short_ok


def run_folds(name, data, sig_kwargs, cfg=CFG):
    fold_pnl = [0.0] * FOLDS
    fold_n = [0] * FOLDS
    per_sym_total = {}
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
        per_sym_total[sym] = round(tot, 1)
    pos_syms = sum(1 for v in per_sym_total.values() if v > 0)
    folds_s = " | ".join(f"F{f+1}: {fold_pnl[f]:+7.1f} (n={fold_n[f]})"
                         for f in range(FOLDS))
    print(f"[{name}]\n  {folds_s}\n  posFolds={sum(1 for p in fold_pnl if p > 0)}/{FOLDS} "
          f"posSyms={pos_syms}/{len(data)} total={sum(fold_pnl):+8.1f} "
          f"trades={sum(fold_n)}\n  perSym={per_sym_total}", flush=True)


async def main():
    data = {}
    async with aiohttp.ClientSession() as session:
        for sym in SYMBOLS:
            hist = await fetch_history(session, sym, DAYS)
            data[sym] = aggregate_candles(hist, "2h", drop_partial=True)
            print(f"{sym}: {len(data[sym])} 2h candles", flush=True)

    run_folds("don24 basis", data, dict(N=24))
    run_folds("don24 adx20", data, dict(N=24, adx_min=20))
    run_folds("don24 adx25", data, dict(N=24, adx_min=25))
    run_folds("don24 gap0.5", data, dict(N=24, gap_min=0.5))
    run_folds("don24 gap1.0", data, dict(N=24, gap_min=1.0))
    run_folds("don24 adx20+gap0.5", data, dict(N=24, adx_min=20, gap_min=0.5))
    run_folds("don24 long-only", data, dict(N=24, allow_short=False))
    run_folds("don24 adx20 long-only", data, dict(N=24, adx_min=20, allow_short=False))
    run_folds("don48 adx20", data, dict(N=48, adx_min=20))
    run_folds("don24 feemult12", data, dict(N=24, fee_mult=12))


if __name__ == "__main__":
    asyncio.run(main())
