"""Offline-Labor: Kandidaten für die neue Regel-Strategie auf ECHTEN
historischen Daten (Binance 1m) mit dem Original-Backtester der Plattform
(inkl. Fees 0.06%/Seite, TP1/BE/Trailing) durchtesten.

Aufruf: python scripts/ultimate_strategy_lab.py [days] > /tmp/lab_out.txt
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiohttp
import numpy as np

from services.backtester import fetch_history, simulate_pair
from services.timeframes import aggregate_candles
from services import vec

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 365

BASE_CFG = {
    "fee_percent": 0.06, "max_capital": 100.0, "leverage": 5,
    "sl_mode": "atr", "atr_period": 14, "atr_sl_multiplier": 1.2,
    "tp_mode": "crv", "tp1_crv": 1.0, "tp_full_crv": 2.0,
    "tp1_close_percent": 50, "be_mode": "tp1",
    "trail_after_tp1": True, "trail_atr_mult": 1.5,
    "min_risk_percent": 0.25,
}


def to_np(candles):
    o = np.array([c["open"] for c in candles], float)
    h = np.array([c["high"] for c in candles], float)
    lo = np.array([c["low"] for c in candles], float)
    c_ = np.array([c["close"] for c in candles], float)
    v = np.array([c.get("volume", 0) for c in candles], float)
    return o, h, lo, c_, v


def sma(a, n):
    out = np.full(len(a), np.nan)
    if len(a) >= n:
        cs = np.cumsum(np.insert(a, 0, 0.0))
        out[n - 1:] = (cs[n:] - cs[:-n]) / n
    return out


def donchian_signals(candles, N=48, vol_mult=1.3, atr_floor=0.10,
                     trend=True, ema_fast=50, ema_slow=200):
    """Trend-gefilterter Donchian-Breakout mit Volumen- & Volatilitäts-Filter."""
    o, h, lo, c, v = to_np(candles)
    n = len(c)
    ef = vec.ema(c, ema_fast)
    es = vec.ema(c, ema_slow)
    atr = vec.atr(h, lo, c, 14)
    vol_sma = sma(v, 20)
    import pandas as pd
    hh = pd.Series(h).rolling(N).max().shift(1).to_numpy()
    ll = pd.Series(lo).rolling(N).min().shift(1).to_numpy()
    with np.errstate(invalid="ignore"):
        brk_l = c > hh
        brk_s = c < ll
        if trend:
            t_l = (ef > es) & (c > es)
            t_s = (ef < es) & (c < es)
        else:
            t_l = t_s = np.ones(n, bool)
        volf = (v > vol_mult * vol_sma) if vol_mult > 0 else np.ones(n, bool)
        atrp = atr / c * 100
        af = atrp >= atr_floor
        long_ok = brk_l & t_l & volf & af
        short_ok = brk_s & t_s & volf & af
    return long_ok, short_ok


def rsi2_meanrev_signals(candles, lo_th=10, hi_th=90):
    """Baseline: RSI(2) Mean-Reversion mit EMA200-Trendfilter."""
    o, h, lo, c, v = to_np(candles)
    es = vec.ema(c, 200)
    r = vec.rsi(c, 2)
    with np.errstate(invalid="ignore"):
        long_ok = (r < lo_th) & (c > es)
        short_ok = (r > hi_th) & (c < es)
    return long_ok, short_ok


def run_variant(name, candles, sym, sig_fn, cfg_over=None, split=0.7):
    cfg = dict(BASE_CFG)
    if cfg_over:
        cfg.update(cfg_over)
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
    rows = []
    for label, sub, off in (("train", candles[:cut], 0),
                            ("test", candles[cut:], cut)):
        res = simulate_pair(None, sub, sym, {}, cfg,
                            signal_provider=provider_for(off))
        rows.append((label, res))
    out = []
    for label, r in rows:
        out.append(f"  {sym} {label:5s}: trades={r['trades']:4d} win={r['win_rate']:5.1f}% "
                   f"pnl={r['pnl']:+9.2f} fees={r['fees']:7.2f} "
                   f"pf={r['profit_factor']:5.2f} dd={r['max_drawdown']:.1f}")
    print(f"[{name}]")
    print("\n".join(out), flush=True)
    return rows


async def main():
    async with aiohttp.ClientSession() as session:
        raw = {}
        for sym in SYMBOLS:
            raw[sym] = await fetch_history(session, sym, DAYS)
            print(f"fetched {sym}: {len(raw[sym])} 1m candles", flush=True)

    for tf in ("15m", "5m"):
        agg = {s: aggregate_candles(raw[s], tf, drop_partial=True) for s in SYMBOLS}
        print(f"\n================ TIMEFRAME {tf} "
              f"({ {s: len(a) for s, a in agg.items()} }) ================",
              flush=True)
        for sym in SYMBOLS:
            cs = agg[sym]
            # Buy & Hold Referenz
            bh = (cs[-1]["close"] / cs[0]["close"] - 1) * 100
            print(f"-- {sym}: Buy&Hold {bh:+.1f}% über {DAYS}d --", flush=True)
            for N in (24, 48, 96):
                for vol_mult in (0, 1.3):
                    for crv in (2.0, 3.0):
                        name = f"donchian N={N} vol={vol_mult} tpf_crv={crv} {tf}"
                        run_variant(name, cs, sym,
                                    lambda c, N=N, vm=vol_mult: donchian_signals(
                                        c, N=N, vol_mult=vm),
                                    cfg_over={"tp_full_crv": crv})
            run_variant(f"donchian N=48 NO-TREND {tf}", cs, sym,
                        lambda c: donchian_signals(c, N=48, trend=False))
            run_variant(f"rsi2 meanrev {tf}", cs, sym, rsi2_meanrev_signals)


if __name__ == "__main__":
    asyncio.run(main())
