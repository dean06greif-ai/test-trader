"""Runde 3: Robustheits-Check über 2 Jahre und 8 Coins (2h/4h).
Kandidaten: Donchian-Trend-Breakout (weite ATR-Stops) und
Trend-Pullback-Continuation (mehr Frequenz).

Aufruf: python scripts/ultimate_strategy_lab3.py [days]
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

CFG = {"fee_percent": 0.06, "max_capital": 100.0, "leverage": 5,
       "sl_mode": "atr", "atr_period": 14, "atr_sl_multiplier": 3.0,
       "tp_mode": "crv", "tp1_crv": 1.5, "tp_full_crv": 5.0,
       "tp1_close_percent": 40, "be_mode": "tp1",
       "trail_after_tp1": True, "trail_atr_mult": 2.5,
       "min_risk_percent": 0.25}


def to_np(candles):
    h = np.array([c["high"] for c in candles], float)
    lo = np.array([c["low"] for c in candles], float)
    c_ = np.array([c["close"] for c in candles], float)
    v = np.array([c.get("volume", 0) for c in candles], float)
    return h, lo, c_, v


def donchian(candles, N=24, atr_mult=3.0, fee_mult=8.0):
    h, lo, c, v = to_np(candles)
    ef, es = vec.ema(c, 50), vec.ema(c, 200)
    atr = vec.atr(h, lo, c, 14)
    hh = pd.Series(h).rolling(N).max().shift(1).to_numpy()
    ll = pd.Series(lo).rolling(N).min().shift(1).to_numpy()
    with np.errstate(invalid="ignore"):
        af = (atr / c * 100 * atr_mult) >= fee_mult * 0.12
        long_ok = (c > hh) & (ef > es) & (c > es) & af
        short_ok = (c < ll) & (ef < es) & (c < es) & af
    return long_ok, short_ok


def pullback(candles, atr_mult=3.0, fee_mult=8.0):
    """Trend-Pullback: EMA50>EMA200-Trend, Rücksetzer an/unter EMA20,
    Wiedereroberung (close > EMA20 nach Berührung) mit grüner Kerze."""
    h, lo, c, v = to_np(candles)
    e20, ef, es = vec.ema(c, 20), vec.ema(c, 50), vec.ema(c, 200)
    atr = vec.atr(h, lo, c, 14)
    n = len(c)
    touched_l = pd.Series(lo <= e20).rolling(3).max().to_numpy() > 0
    touched_s = pd.Series(h >= e20).rolling(3).max().to_numpy() > 0
    prev_c = np.roll(c, 1)
    with np.errstate(invalid="ignore"):
        af = (atr / c * 100 * atr_mult) >= fee_mult * 0.12
        up = (ef > es) & (c > es)
        dn = (ef < es) & (c < es)
        long_ok = up & touched_l & (c > e20) & (c > prev_c) & af
        short_ok = dn & touched_s & (c < e20) & (c < prev_c) & af
    long_ok[0] = short_ok[0] = False
    return long_ok, short_ok


def run(name, candles, sym, sig_fn, cfg=CFG, split=0.7):
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
    out = [f"[{name}]"]
    for label, sub, off in (("train", candles[:cut], 0), ("test", candles[cut:], cut)):
        r = simulate_pair(None, sub, sym, {}, cfg, signal_provider=provider_for(off))
        out.append(
            f"  {sym:9s} {label:5s}: n={r['trades']:4d} win={r['win_rate']:5.1f}% "
            f"pnl={r['pnl']:+9.2f} fees={r['fees']:7.2f} pf={r['profit_factor']:5.2f} "
            f"dd={r['max_drawdown']:6.1f} dur={r['avg_duration_min']:.0f}m")
    print("\n".join(out), flush=True)


async def main():
    raw = {}
    async with aiohttp.ClientSession() as session:
        for sym in SYMBOLS:
            try:
                raw[sym] = await fetch_history(session, sym, DAYS)
                print(f"fetched {sym}: {len(raw[sym])}", flush=True)
            except Exception as e:
                print(f"fetch {sym} failed: {e}", flush=True)

    for tf in ("2h", "4h"):
        print(f"\n========== TF {tf} ==========", flush=True)
        for sym, hist in raw.items():
            cs = aggregate_candles(hist, tf, drop_partial=True)
            bh = (cs[-1]["close"] / cs[0]["close"] - 1) * 100
            print(f"-- {sym} Buy&Hold {bh:+.1f}% --", flush=True)
            run(f"don24 {tf}", cs, sym, lambda c: donchian(c, N=24))
            run(f"don48 {tf}", cs, sym, lambda c: donchian(c, N=48))
            run(f"pullback {tf}", cs, sym, pullback)


if __name__ == "__main__":
    asyncio.run(main())
