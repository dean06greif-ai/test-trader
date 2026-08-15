"""Runde 2: weite ATR-Stops + höhere Timeframes (30m/1h) – Fees müssen klein
relativ zum Trade-Risiko werden. Zusätzlich Long-Only-Variante und
Fee-relativer Volatilitätsfilter.

Aufruf: python scripts/ultimate_strategy_lab2.py [days]
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

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 365


def to_np(candles):
    h = np.array([c["high"] for c in candles], float)
    lo = np.array([c["low"] for c in candles], float)
    c_ = np.array([c["close"] for c in candles], float)
    v = np.array([c.get("volume", 0) for c in candles], float)
    return h, lo, c_, v


def donchian(candles, N=24, trend=True, atr_floor_fee_mult=8.0,
             fee_rt=0.12, atr_mult=2.5, ema_fast=50, ema_slow=200,
             long_only=False):
    h, lo, c, v = to_np(candles)
    n = len(c)
    ef = vec.ema(c, ema_fast)
    es = vec.ema(c, ema_slow)
    atr = vec.atr(h, lo, c, 14)
    hh = pd.Series(h).rolling(N).max().shift(1).to_numpy()
    ll = pd.Series(lo).rolling(N).min().shift(1).to_numpy()
    with np.errstate(invalid="ignore"):
        brk_l = c > hh
        brk_s = c < ll
        t_l = (ef > es) & (c > es) if trend else np.ones(n, bool)
        t_s = (ef < es) & (c < es) if trend else np.ones(n, bool)
        # Fee-relativer Filter: Risiko (atr_mult*ATR) muss >= X * Roundtrip-Fee sein
        risk_pct = atr / c * 100 * atr_mult
        af = risk_pct >= atr_floor_fee_mult * fee_rt
        long_ok = brk_l & t_l & af
        short_ok = (brk_s & t_s & af) if not long_only else np.zeros(n, bool)
    return long_ok, short_ok


def run(name, candles, sym, sig_fn, cfg, split=0.7):
    long_ok, short_ok = sig_fn(candles)

    def provider_for(offset):
        def provider(i):
            j = offset + i
            if long_ok[j]:
                return {"type": "LONG", "signal_class": "SIGNAL",
                        "entry_price": candles[j]["close"]}
            if short_ok[j]:
                return {"type": "SHORT", "signal_class": "SIGNAL",
                        "entry_price": candles[j]["close"]}
            return None
        return provider

    cut = int(len(candles) * split)
    line = [f"[{name}]"]
    for label, sub, off in (("train", candles[:cut], 0), ("test", candles[cut:], cut)):
        r = simulate_pair(None, sub, sym, {}, cfg, signal_provider=provider_for(off))
        line.append(
            f"  {sym} {label:5s}: n={r['trades']:4d} win={r['win_rate']:5.1f}% "
            f"pnl={r['pnl']:+9.2f} fees={r['fees']:7.2f} pf={r['profit_factor']:5.2f} "
            f"dd={r['max_drawdown']:6.1f} dur={r['avg_duration_min']:.0f}m")
    print("\n".join(line), flush=True)


async def main():
    async with aiohttp.ClientSession() as session:
        raw = {}
        for sym in SYMBOLS:
            raw[sym] = await fetch_history(session, sym, DAYS)
            print(f"fetched {sym}: {len(raw[sym])}", flush=True)

    for tf in ("30m", "1h", "2h"):
        agg = {s: aggregate_candles(raw[s], tf, drop_partial=True) for s in SYMBOLS}
        print(f"\n========== TF {tf} ==========", flush=True)
        for sym in SYMBOLS:
            cs = agg[sym]
            bh = (cs[-1]["close"] / cs[0]["close"] - 1) * 100
            print(f"-- {sym} Buy&Hold {bh:+.1f}% / {DAYS}d --", flush=True)
            for atr_mult in (2.0, 3.0):
                for tpf in (3.0, 5.0):
                    for be in ("tp1", "off"):
                        cfg = {"fee_percent": 0.06, "max_capital": 100.0,
                               "leverage": 5, "sl_mode": "atr", "atr_period": 14,
                               "atr_sl_multiplier": atr_mult, "tp_mode": "crv",
                               "tp1_crv": 1.5, "tp_full_crv": tpf,
                               "tp1_close_percent": 40, "be_mode": be,
                               "trail_after_tp1": True, "trail_atr_mult": 2.5,
                               "min_risk_percent": 0.25}
                        run(f"don24 atr{atr_mult} tpf{tpf} be={be} {tf}", cs, sym,
                            lambda c, am=atr_mult: donchian(c, N=24, atr_mult=am),
                            cfg)
            # Long-only Variante (Krypto-Drift)
            cfg = {"fee_percent": 0.06, "max_capital": 100.0, "leverage": 5,
                   "sl_mode": "atr", "atr_period": 14, "atr_sl_multiplier": 3.0,
                   "tp_mode": "crv", "tp1_crv": 1.5, "tp_full_crv": 5.0,
                   "tp1_close_percent": 40, "be_mode": "off",
                   "trail_after_tp1": True, "trail_atr_mult": 2.5,
                   "min_risk_percent": 0.25}
            run(f"don24 LONG-ONLY atr3 tpf5 {tf}", cs, sym,
                lambda c: donchian(c, N=24, atr_mult=3.0, long_only=True), cfg)
            run(f"don48 atr3 tpf5 beoff {tf}", cs, sym,
                lambda c: donchian(c, N=48, atr_mult=3.0), cfg)


if __name__ == "__main__":
    asyncio.run(main())
