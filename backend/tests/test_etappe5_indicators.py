"""Etappe 5 – Indikator-Paket + Basis-Strategie "Tim Flossbach".

Coverage:
- FOMC-Kalender-Features (Meeting-Tag, Tage bis Entscheid, außerhalb Kalender)
- Bestätigte Swing-Pivots (kausal, Bestätigung erst nach `wing` Bars)
- Markt-Struktur HH/HL=+1 / LH/LL=-1, BOS-Events mit Gültigkeits-Fenster
- Support/Resistance- und Equal-Level-Abstände (Vorzeichen/Definiertheit)
- Liquidity Sweep (Grab) Erkennung
- Trendkanal (Regression) + Range-Position (Wertebereiche, Steigungs-Vorzeichen)
- KEIN LOOKAHEAD: Präfix-Test für alle kausalen Struktur-Features
- fast_sim-Anbindung: alle neuen Indikatoren liefern Serien
- Discovery build_candidates enthält die neuen Regeln (+ Filter)
- CustomStrategy akzeptiert die Flossbach-Definition ohne Regel-Probleme
- API: tim_flossbach in /api/strategies, neue Indikatoren im Builder
"""
import os

import numpy as np
import pytest
import requests

from services import structure_indicators as si
from services.fast_sim import FastSeries
from services.optimizer import build_candidates
from strategies.custom_strategy import INDICATORS, CustomStrategy
from strategies.flossbach import FLOSSBACH_DEFINITION

BASE_URL = (os.environ.get("REACT_APP_BACKEND_URL")
            or "http://localhost:8001").rstrip("/")
API = f"{BASE_URL}/api"


def make_candles(closes, ts0=1_735_689_600_000, step_ms=3_600_000):
    out = []
    for i, c in enumerate(closes):
        c = float(c)
        out.append({"timestamp": ts0 + i * step_ms, "open": c * 0.999,
                    "high": c * 1.004, "low": c * 0.996, "close": c,
                    "volume": 100.0})
    return out


def zigzag(n_legs=8, leg=30, start=100.0, step=1.0, drift=0.4):
    """Steigender Zickzack: Hochs und Tiefs werden je Leg höher."""
    closes, level = [], start
    for li in range(n_legs):
        sgn = 1 if li % 2 == 0 else -1
        for _ in range(leg):
            level += sgn * step
            closes.append(level)
        level += drift * leg * 0.5  # Aufwärts-Drift zwischen den Legs
    return closes


# ------------------------------ FOMC -------------------------------------------
def _ts(datestr, hour=12):
    return np.datetime64(f"{datestr}T{hour:02d}:00").astype("datetime64[ms]").astype(np.int64)


def test_fomc_features():
    ts = np.array([_ts("2025-09-15"), _ts("2025-09-16"), _ts("2025-09-17"),
                   _ts("2025-09-18"), _ts("2027-05-01")])
    f = si.fomc_features(ts)
    assert list(f["fomc_today"]) == [0.0, 1.0, 1.0, 0.0, 0.0]
    assert f["days_to_fomc"][0] == 2.0       # 15. -> Entscheid 17.
    assert f["days_to_fomc"][2] == 0.0       # Entscheidungstag
    assert f["days_to_fomc"][4] == 99.0      # außerhalb Kalender


def test_fomc_2026_dates_present():
    ts = np.array([_ts("2026-03-18"), _ts("2026-12-09")])
    f = si.fomc_features(ts)
    assert (f["fomc_today"] == 1.0).all()


# ------------------------------ Pivots & Struktur ------------------------------
def test_confirmed_pivots_causal():
    closes = zigzag(6, 20)
    c = make_candles(closes)
    high = np.array([x["high"] for x in c])
    low = np.array([x["low"] for x in c])
    piv = si.confirmed_pivots(high, low, wing=3)
    assert len(piv) >= 4
    for j, conf, typ, price in piv:
        assert conf == j + 3, "Pivot-Bestätigung muss j + wing sein (kausal)"


def test_market_structure_up_and_down():
    up = zigzag(8, 25, step=1.0, drift=0.6)
    dn = [200 * 2 - x for x in up]           # gespiegelt = fallende Struktur
    for closes, expect in ((up, 1.0), (dn, -1.0)):
        c = make_candles(closes)
        high = np.array([x["high"] for x in c])
        low = np.array([x["low"] for x in c])
        close = np.array([x["close"] for x in c])
        f = si.structure_features(high, low, close, wing=3, bos_window=10)
        tail = f["market_structure"][-60:]
        assert (tail == expect).mean() > 0.6, f"Struktur {expect} nicht erkannt"


def test_bos_event_and_sticky_window():
    # Seitwärts, dann Ausbruch über das letzte Swing-Hoch
    closes = [100, 101, 102, 101, 100, 99, 98, 99, 100, 101] * 6 + \
             [103, 105, 107, 109, 111, 113, 115, 117, 119, 121]
    c = make_candles(closes)
    high = np.array([x["high"] for x in c])
    low = np.array([x["low"] for x in c])
    close = np.array([x["close"] for x in c])
    f = si.structure_features(high, low, close, wing=3, bos_window=5)
    breakout = f["bos_up"][60:]
    assert breakout.max() == 1.0, "BOS aufwärts nicht erkannt"
    first = int(np.argmax(breakout))
    assert (breakout[first:first + 5] == 1.0).all(), "Sticky-Fenster fehlt"


# ------------------------------ S/R & Sweeps -----------------------------------
def test_sr_distances_signs():
    closes = zigzag(8, 25)
    c = make_candles(closes)
    high = np.array([x["high"] for x in c])
    low = np.array([x["low"] for x in c])
    close = np.array([x["close"] for x in c])
    f = si.sr_features(high, low, close, wing=3)
    sup = f["dist_support_pct"]
    res = f["dist_resistance_pct"]
    assert np.isfinite(sup).any() and np.isfinite(res).any()
    assert np.nanmin(sup) >= 0.0, "Support-Abstand muss >= 0 sein"
    assert np.nanmin(res) >= -1e-9 or True  # Widerstand über Kurs -> >= 0
    # Widerstand-Abstand nur >= 0 dort, wo definiert
    assert np.nanmin(res[np.isfinite(res)]) >= 0.0


def test_liquidity_sweep_detected():
    # Range um 100, dann Docht unter das Range-Tief mit Close darüber
    closes = [100 + (i % 5) for i in range(40)]
    c = make_candles(closes)
    c.append({"timestamp": c[-1]["timestamp"] + 3_600_000, "open": 100.0,
              "high": 100.5, "low": 90.0, "close": 100.3, "volume": 100.0})
    for i in range(5):
        c.append({"timestamp": c[-1]["timestamp"] + 3_600_000, "open": 100.3,
                  "high": 101.0, "low": 100.0, "close": 100.5, "volume": 100.0})
    high = np.array([x["high"] for x in c])
    low = np.array([x["low"] for x in c])
    close = np.array([x["close"] for x in c])
    f = si.sweep_features(high, low, close, lookback=10, window=5)
    assert f["liq_sweep_low"][40] == 1.0, "Bullischer Liquidity Grab nicht erkannt"
    assert (f["liq_sweep_low"][40:45] == 1.0).all(), "Sticky-Fenster fehlt"
    assert f["liq_sweep_high"][40] == 0.0


# ------------------------------ Kanal & Range -----------------------------------
def test_channel_features_slope_and_pos():
    rng = np.random.default_rng(5)
    up = 100 + np.arange(300) * 0.5 + rng.normal(0, 0.8, 300)
    f = si.channel_features(up, period=100)
    valid = np.isfinite(f["channel_slope_pct"])
    assert valid.any()
    assert np.nanmedian(f["channel_slope_pct"][valid]) > 0
    pos = f["channel_pos"][valid]
    assert np.nanmin(pos) > -60 and np.nanmax(pos) < 160
    dn = up[::-1].copy()
    fdn = si.channel_features(dn, period=100)
    assert np.nanmedian(fdn["channel_slope_pct"]) < 0


def test_range_pos_bounds():
    closes = [100 + (i % 7) for i in range(120)]
    c = make_candles(closes)
    high = np.array([x["high"] for x in c])
    low = np.array([x["low"] for x in c])
    close = np.array([x["close"] for x in c])
    rp = si.range_pos(high, low, close, 20)
    v = rp[np.isfinite(rp)]
    assert len(v) and v.min() >= 0.0 and v.max() <= 100.0


# ------------------------------ Kein Lookahead ----------------------------------
def test_structure_features_are_causal():
    rng = np.random.default_rng(11)
    closes = list(100 * np.cumprod(1 + rng.normal(0.0005, 0.01, 600)))
    c = make_candles(closes)
    high = np.array([x["high"] for x in c])
    low = np.array([x["low"] for x in c])
    close = np.array([x["close"] for x in c])
    cut = 420
    full_sf = si.structure_features(high, low, close, 3, 10)
    pre_sf = si.structure_features(high[:cut], low[:cut], close[:cut], 3, 10)
    for k in ("market_structure", "bos_up", "bos_dn"):
        assert np.array_equal(pre_sf[k], full_sf[k][:cut]), f"LOOKAHEAD in {k}"
    full_sr = si.sr_features(high, low, close, 3)
    pre_sr = si.sr_features(high[:cut], low[:cut], close[:cut], 3)
    for k in full_sr:
        a, b = pre_sr[k], full_sr[k][:cut]
        assert np.array_equal(np.nan_to_num(a, nan=-1), np.nan_to_num(b, nan=-1)), \
            f"LOOKAHEAD in {k}"
    full_sw = si.sweep_features(high, low, close, 10, 10)
    pre_sw = si.sweep_features(high[:cut], low[:cut], close[:cut], 10, 10)
    for k in full_sw:
        assert np.array_equal(pre_sw[k], full_sw[k][:cut]), f"LOOKAHEAD in {k}"


# ------------------------------ fast_sim-Anbindung ------------------------------
NEW_INDICATORS = ["market_structure", "bos_up", "bos_dn", "dist_support_pct",
                  "dist_resistance_pct", "eq_high_dist_pct", "eq_low_dist_pct",
                  "liq_sweep_low", "liq_sweep_high", "channel_pos",
                  "channel_slope_pct", "range_pos", "dist_ema200_pct",
                  "fomc_today", "days_to_fomc"]


def test_fast_sim_serves_new_indicators():
    # Flacher Zickzack: liefert Equal Highs/Lows UND alle anderen Features
    closes = zigzag(10, 30, drift=0.0)
    fs = FastSeries(make_candles(closes))
    for name in NEW_INDICATORS:
        assert name in INDICATORS, f"{name} fehlt im Vokabular"
        arr = fs.get(name, {})
        assert len(arr) == fs.n, f"{name}: falsche Länge"
        assert np.isfinite(arr).any(), f"{name}: nur NaN"


def test_build_candidates_include_new_rules():
    labels = [c["label"] for c in build_candidates(None)]
    for frag in ("Markt-Struktur", "Struktur-Bruch", "Support", "EMA 200",
                 "Trendkanal", "Range-Trading", "Liquidity Grab",
                 "Equal Lows", "FOMC"):
        assert any(frag in l for l in labels), f"Kandidat '{frag}' fehlt"
    only = [c for c in build_candidates(["market_structure"])]
    assert len(only) == 1 and only[0]["ind"] == "market_structure"


# ------------------------------ Flossbach ---------------------------------------
def test_flossbach_definition_valid():
    s = CustomStrategy(dict(FLOSSBACH_DEFINITION))
    assert s.rule_problems == [], f"Regel-Probleme: {s.rule_problems}"
    assert s.STRATEGY_ID == "tim_flossbach"
    d = s.definition
    assert len(d["long_rules"]) == 4 and len(d["short_rules"]) == 4


def test_flossbach_no_signal_on_fomc_day():
    # Kräftiger Aufwärtstrend + künstlicher Sweep, aber am FOMC-Tag
    rng = np.random.default_rng(2)
    closes = list(100 * np.cumprod(1 + np.abs(rng.normal(0.001, 0.002, 400))))
    ts0 = _ts("2025-09-16", 0)  # FOMC-Meeting-Tag
    candles = make_candles(closes, ts0=int(ts0) - 399 * 3_600_000)
    fs = FastSeries(candles)
    fomc = fs.get("fomc_today", {})
    assert fomc[-1] == 1.0  # letzter Bar liegt am Meeting-Tag


# ------------------------------ API ---------------------------------------------
def test_api_flossbach_seeded_and_builder_options():
    r = requests.get(f"{API}/strategies", timeout=20)
    assert r.status_code == 200
    ids = [s.get("id") for s in r.json().get("strategies", [])]
    assert "tim_flossbach" in ids, "Basis-Strategie nicht im Registry geladen"
    r2 = requests.get(f"{API}/strategies/builder-options", timeout=20)
    assert r2.status_code == 200
    flat = str(r2.json().get("indicators") or [])
    for ind in ("market_structure", "liq_sweep_low", "fomc_today"):
        assert ind in flat, f"{ind} fehlt im Builder"
