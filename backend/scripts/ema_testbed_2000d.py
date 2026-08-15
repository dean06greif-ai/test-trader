"""2000-Tage-Validierung: skaliert das EMA-Set mit dem Zeitraum besser?"""
import asyncio
import os
import pickle
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
import numpy as np  # noqa: E402

DATA2 = os.path.join(os.path.dirname(__file__), "_testbed_2000d.pkl")


async def fetch():
    """Direkt 1d-Klines vom Binance-Mirror (schnell, nur für diesen Test)."""
    import aiohttp
    url = "https://data-api.binance.vision/api/v3/klines"
    hist = {}
    async with aiohttp.ClientSession() as s:
        for sym in ("BTCUSDT", "ETHUSDT"):
            rows = []
            end = None
            while len(rows) < 2000:
                params = {"symbol": sym, "interval": "1d", "limit": 1000}
                if end:
                    params["endTime"] = end
                async with s.get(url, params=params) as r:
                    data = await r.json(content_type=None)
                if not isinstance(data, list) or not data:
                    break
                rows = data + rows
                end = int(data[0][0]) - 1
                if len(data) < 1000:
                    break
            hist[sym] = [{"timestamp": int(k[0]), "open": float(k[1]),
                          "high": float(k[2]), "low": float(k[3]),
                          "close": float(k[4]), "volume": float(k[5])}
                         for k in rows[-2000:]]
    with open(DATA2, "wb") as fh:
        pickle.dump(hist, fh)
    print({k: len(v) for k, v in hist.items()})


def evaluate():
    from services import regime_engine as eng
    from services import regime_reactive as rx
    from ema_testbed import ema_arrays, ema_dir_slope, apply_variant, metrics
    with open(DATA2, "rb") as fh:
        hist = pickle.load(fh)
    rows = {}
    for sym, candles in hist.items():
        cfg = eng.resolve_config({"regime_mode": 3}, "24h", len(candles))
        bpd = cfg["bars_per_day"]
        f = eng.compute_matrix(candles, cfg)
        close = np.asarray(f["close"])
        dvol = np.nan_to_num(np.asarray(f["daily_vol_pct"]), nan=2.0)
        det = rx.detect(f, cfg)
        live, fin, warm = det["live3"], det["final3"], det["warm"]
        rows.setdefault("BASE", []).append(
            (sym, metrics(live, fin, close, warm, bpd)))
        span = len(candles) / bpd
        scale = min(max(span / 720.0, 0.7), 4.0)
        for name, ds in (("fix(9,21,50)", (9, 21, 50)),
                         ("fix(9,50,200)", (9, 50, 200)),
                         ("skal", tuple(round(x * scale, 1) for x in (9, 21, 50)))):
            E = ema_arrays(close, bpd, ds, None)
            for thr in (0.15, 0.2, 0.3):
                ed = ema_dir_slope(close, E, dvol, thr=thr)
                # 1 Tag Persistenz
                k = max(int(round(bpd)), 1)
                run = 0
                eds = ed.copy()
                for i in range(1, len(ed)):
                    run = run + 1 if ed[i] == ed[i - 1] and ed[i] != 0 else 0
                    if ed[i] != 0 and run < k:
                        eds[i] = 0
                lv = apply_variant(live, eds, "side2trend")
                rows.setdefault(f"{name} thr={thr}", []).append(
                    (sym, metrics(lv, fin, close, warm, bpd)))
    print(f"{'Variante':28s} {'agree':>6s} {'hold':>6s} {'hit':>6s} {'lag':>5s} "
          f"{'miss':>4s} {'segs':>5s}")
    for key, res in rows.items():
        ag = np.mean([r["agree"] for _, r in res])
        ho = np.mean([r["hold"] for _, r in res])
        hi = np.mean([r["hit"] for _, r in res])
        lg = np.mean([r["lag"] for _, r in res if r["lag"] is not None])
        ms = sum(r["miss"] for _, r in res)
        sg = sum(r["segs"] for _, r in res)
        print(f"{key:28s} {ag:6.1f} {ho:6.1f} {hi:6.1f} {lg:5.1f} {ms:4d} {sg:5d}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "fetch":
        asyncio.run(fetch())
    else:
        evaluate()
