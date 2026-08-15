"""Offline-Testbett für die Live-Erkennung: Kerzen einmal laden und picklen,
dann Varianten von regime_reactive.detect() schnell vergleichen.

Aufruf:
  python3 scripts/live_testbed.py fetch          # Kerzen laden (720d 1h BTC/ETH)
  python3 scripts/live_testbed.py eval           # aktuelle Logik bewerten
"""
import asyncio
import os
import pickle
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

import numpy as np  # noqa: E402

DATA = os.path.join(os.path.dirname(__file__), "_testbed_candles.pkl")


async def fetch():
    from services.regime_lab import fetch_histories
    hist = await fetch_histories(["BTCUSDT", "ETHUSDT"], 720, "1h")
    with open(DATA, "wb") as fh:
        pickle.dump(hist, fh)
    print({k: len(v) for k, v in hist.items()})


def evaluate():
    from services import regime_engine as eng
    from services import regime_reactive as rx
    with open(DATA, "rb") as fh:
        hist = pickle.load(fh)
    for sym, candles in hist.items():
        cfg = eng.resolve_config({"regime_mode": 3, "min_phase_days": 1.0},
                                 "1h", len(candles))
        f = eng.compute_matrix(candles, cfg)
        det = rx.detect(f, cfg)
        live, fin = det["live3"], det["final3"]
        warm = det["warm"]
        n = len(live)
        m = slice(warm, n)
        agree = float(np.mean(live[m] == fin[m])) * 100
        # Segment-Zahlen
        def nseg(a):
            return int(np.sum(a[1:] != a[:-1]) + 1)
        # Widersprüche: Live-Trend, aber Kurs lief in die Gegenrichtung (netto im Segment)
        close = np.asarray(f["close"])
        bad = tot = 0
        s = warm
        for i in range(warm + 1, n + 1):
            if i == n or live[i] != live[s]:
                lab = int(live[s])
                if lab in (0, 2) and i - s > 4:
                    net = close[i - 1] / close[s] - 1
                    tot += 1
                    if (lab == 2 and net < -0.01) or (lab == 0 and net > 0.01):
                        bad += 1
                s = i
        print(f"{sym}: live-vs-final {agree:.1f}% · live_segs={nseg(live[m])} "
              f"final_segs={nseg(fin[m])} · widerspruch {bad}/{tot} "
              f"· pivots={len(det['pivots'])}")


if __name__ == "__main__":
    if sys.argv[1] == "fetch":
        asyncio.run(fetch())
    else:
        evaluate()
