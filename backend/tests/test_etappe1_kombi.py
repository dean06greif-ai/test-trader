"""Etappe 1 – Kombi-Detektor (Detektor 'kombi') Verification Tests.

Coverage:
- resolve_config akzeptiert detector='kombi' und sanitisiert die kombi_* Keys
- detect()-Dispatch liefert die vollständige Detektor-Struktur
- KEIN LOOKAHEAD: Live-Labels auf einem Präfix == Präfix der Live-Labels
  auf der vollen Serie (strenge Kausalitäts-Prüfung)
- Trend-Dominanz: kurze Seitwärts-Einschübe (< kombi_dominance_days) innerhalb
  eines Trends werden final absorbiert und live überbrückt
- _dominance_merge Unit-Verhalten (nur gleichgerichtete Trends, nur <= max_bars)
- Pivot-Beschleunigung: schaltet nach einem Crash nie SPÄTER als ohne
- Alte Detektoren (reactive/ema) unverändert nutzbar (Regression)
- API: engine/defaults enthält kombi-Gruppe; kombi-calibrate validiert Input
"""
import math
import os

import numpy as np
import pytest
import requests

from services import regime_engine as eng
from services import regime_reactive as rx
from services.regime_kombi import _dominance_merge, detect_kombi

BASE_URL = (os.environ.get("REACT_APP_BACKEND_URL")
            or "http://localhost:8001").rstrip("/")
API = f"{BASE_URL}/api"

ADMIN_USER = "Admin"
ADMIN_PASS = os.environ.get("ADMIN_PASSWORD", "admin")


# ------------------------------ Helpers ---------------------------------------
def make_candles(closes, ts0=1_700_000_000_000, step_ms=3_600_000, noise=0.0,
                 seed=7):
    """1h-Kerzen aus einer Close-Serie (High/Low leicht darüber/darunter)."""
    rng = np.random.default_rng(seed)
    out = []
    for i, c in enumerate(closes):
        c = float(c) * (1.0 + (rng.normal(0, noise) if noise else 0.0))
        out.append({"timestamp": ts0 + i * step_ms,
                    "open": c * 0.999, "high": c * 1.004,
                    "low": c * 0.996, "close": c, "volume": 100.0})
    return out


def synth_closes(segments, start=100.0, bars_per_day=24):
    """Serie aus (tage, drift_pct_pro_tag)-Segmenten."""
    closes = [start]
    for days, drift in segments:
        per_bar = (1.0 + drift / 100.0) ** (1.0 / bars_per_day)
        for _ in range(int(days * bars_per_day)):
            closes.append(closes[-1] * per_bar)
    return closes


def kombi_cfg(n_bars, **over):
    base = {"detector": "kombi", "auto_adapt": False, "adapt_profile": "off",
            "regime_mode": 3, "use_volume_confirm": False}
    base.update(over)
    return eng.resolve_config(base, "1h", n_bars)


def run_detect(candles, **over):
    cfg = kombi_cfg(len(candles), **over)
    f = eng.compute_matrix(candles, cfg)
    return detect_kombi(f, cfg), cfg, f


def segments_of(labels):
    segs, s = [], 0
    labels = list(labels)
    for i in range(1, len(labels) + 1):
        if i == len(labels) or labels[i] != labels[s]:
            segs.append((s, i, labels[s]))
            s = i
    return segs


# ------------------------------ Config ----------------------------------------
def test_resolve_config_kombi_defaults():
    cfg = kombi_cfg(5000)
    assert cfg["detector"] == "kombi"
    assert cfg["kombi_ema_days"] == 14.0
    assert cfg["kombi_thr"] == 0.18
    assert cfg["kombi_slope_days"] == 5.0
    assert cfg["kombi_persist_days"] == 1.0
    assert cfg["kombi_dominance_days"] == 3.0
    assert cfg["kombi_pivot_accel"] is True


def test_resolve_config_kombi_clamps():
    cfg = kombi_cfg(5000, kombi_thr=99.0, kombi_slope_days=0.01,
                    kombi_dominance_days=999, kombi_pivot_accel=0)
    assert cfg["kombi_thr"] == 1.0
    assert cfg["kombi_slope_days"] == 0.5
    assert cfg["kombi_dominance_days"] == 15.0
    assert cfg["kombi_pivot_accel"] is False


def test_unknown_detector_falls_back_to_reactive():
    cfg = eng.resolve_config({"detector": "quatsch"}, "1h", 5000)
    assert cfg["detector"] == "reactive"


# ------------------------------ Struktur --------------------------------------
def test_detect_dispatch_and_structure():
    closes = synth_closes([(30, 2.0), (10, 0.0), (30, -2.0)])
    candles = make_candles(closes, noise=0.001)
    cfg = kombi_cfg(len(candles))
    f = eng.compute_matrix(candles, cfg)
    det = rx.detect(f, cfg)          # Dispatch über regime_reactive.detect
    n = len(candles)
    for key in ("live_dir", "live3", "final3", "trendiness", "probs", "conf",
                "thr", "retrace", "since_ext", "rev_count", "pivots", "warm"):
        assert key in det, f"Key {key} fehlt"
    assert len(det["live3"]) == n and len(det["final3"]) == n
    assert set(np.unique(det["live3"])) <= {0, 1, 2}
    assert set(np.unique(det["final3"])) <= {0, 1, 2}
    assert det["probs"].shape == (n, 3)
    assert np.allclose(det["probs"].sum(axis=1), 1.0, atol=1e-6)
    # classify + final_ids über die Engine-Pfade laufen ohne Fehler
    ids, conf, det2 = rx.classify(f, cfg)
    assert len(ids) == n
    fin = rx.final_ids_from(det2, f, cfg)
    assert len(fin) == n


def test_up_down_direction_detected():
    closes = synth_closes([(40, 2.5), (40, -2.5)])
    candles = make_candles(closes, noise=0.001)
    det, cfg, _ = run_detect(candles)
    warm = det["warm"]
    n = len(candles)
    first = det["final3"][warm:warm + (n - warm) // 3]
    last = det["final3"][-(n - warm) // 3:]
    assert (first == 2).mean() > 0.5, "Aufwärtstrend nicht erkannt (final)"
    assert (last == 0).mean() > 0.5, "Abwärtstrend nicht erkannt (final)"


# ------------------------------ Kein Lookahead --------------------------------
def test_live_labels_are_causal_no_lookahead():
    closes = synth_closes([(25, 2.0), (8, 0.0), (25, -2.0), (10, 0.0),
                           (20, 1.5)])
    candles = make_candles(closes, noise=0.002)
    det_full, cfg, _ = run_detect(candles)
    cut = int(len(candles) * 0.7)
    f_pre = eng.compute_matrix(candles[:cut], cfg)
    det_pre = detect_kombi(f_pre, cfg)
    same = np.array_equal(det_pre["live3"], det_full["live3"][:cut])
    assert same, ("LOOKAHEAD in der Live-Sicht: Präfix-Labels weichen von den "
                  "Labels der vollen Serie ab")


def test_final_may_use_lookahead_live_not():
    """Die Final-Sicht DARF vom Präfix abweichen (zentrierte Steigung) –
    das ist ihr Sinn. Nur die Live-Sicht muss kausal identisch sein."""
    closes = synth_closes([(20, 2.0), (20, -2.0)])
    candles = make_candles(closes, noise=0.002)
    det_full, cfg, _ = run_detect(candles)
    assert det_full["final3"] is not det_full["live3"]


# ------------------------------ Trend-Dominanz --------------------------------
def test_dominance_merge_unit():
    dom = 24 * 3  # 3 Tage in 1h-Bars
    lab = np.array([2] * 200 + [1] * 48 + [2] * 200, dtype=np.int8)
    out = _dominance_merge(lab, dom)
    assert (out == 2).all(), "Seitwärts-Einschub (2d) zwischen Auf-Trends nicht absorbiert"
    # Zu langer Einschub bleibt
    lab2 = np.array([2] * 200 + [1] * 120 + [2] * 200, dtype=np.int8)
    out2 = _dominance_merge(lab2, dom)
    assert (out2[200:320] == 1).all(), "Zu langer Seitwärts-Einschub wurde absorbiert"
    # Gegengerichtete Trends: Einschub bleibt (echter Übergang)
    lab3 = np.array([2] * 200 + [1] * 48 + [0] * 200, dtype=np.int8)
    out3 = _dominance_merge(lab3, dom)
    assert (out3[200:248] == 1).all(), "Übergangs-Seitwärts zwischen Auf und Ab wurde absorbiert"


def test_trend_dominance_bridges_short_gap_in_final():
    # 25d auf, 2d flach, 25d auf – die Mini-Seitwärtsphase gehört zum Trend
    closes = synth_closes([(25, 2.5), (2, 0.0), (25, 2.5)])
    candles = make_candles(closes, noise=0.0)
    det, cfg, _ = run_detect(candles)
    warm = det["warm"]
    segs = [s for s in segments_of(det["final3"][warm:]) if s[2] == 1]
    mid = len(candles) // 2
    for s0, s1, _lab in segs:
        assert not (s0 + warm < mid < s1 + warm), \
            "Final-Sicht zeigt Seitwärts mitten im übergeordneten Aufwärtstrend"


def test_trend_dominance_live_bridges_short_gap():
    closes = synth_closes([(25, 2.5), (2, 0.0), (25, 2.5)])
    candles = make_candles(closes, noise=0.0)
    det, cfg, _ = run_detect(candles)
    bpd = 24
    gap0 = 25 * bpd
    gap1 = 27 * bpd
    # Während des 2-Tage-Einschubs (< 3d Dominanz) bleibt die Live-Sicht im Trend
    live_gap = det["live3"][gap0:gap1 + bpd]
    assert (live_gap == 1).mean() < 0.2, \
        "Live-Sicht fällt während des 2d-Einschubs auf Seitwärts (Dominanz greift nicht)"


# ------------------------------ Pivot-Beschleunigung --------------------------
def test_pivot_accel_never_later():
    # Auf-Trend, dann scharfer Crash: mit Beschleuniger darf der Wechsel auf
    # 'abwärts' nie SPÄTER kommen als ohne.
    closes = synth_closes([(30, 2.0), (20, -4.0)])
    candles = make_candles(closes, noise=0.001)
    det_on, _, _ = run_detect(candles, kombi_pivot_accel=True)
    det_off, _, _ = run_detect(candles, kombi_pivot_accel=False)
    crash_start = 30 * 24

    def first_down(det):
        idx = np.where(det["live3"][crash_start:] == 0)[0]
        return int(idx[0]) if len(idx) else 10 ** 9

    assert first_down(det_on) <= first_down(det_off), \
        "Pivot-Beschleunigung schaltet SPÄTER als ohne (darf nie passieren)"


def test_pivot_accel_only_when_ema_agrees():
    """Beschleuniger erzeugt keine Regime, die die EMA-Hysterese nicht sieht:
    jede Kerze mit Trend-Label muss von der EMA-Sicht (raw oder Dominanz-
    Überbrückung) gedeckt sein – geprüft indirekt: ohne Pivots ändern sich
    nur ÜBERGÄNGE (Timing), nie der Bestand an Phasen-Richtungen."""
    closes = synth_closes([(25, 2.0), (10, 0.0), (25, -2.0)])
    candles = make_candles(closes, noise=0.002)
    det_on, _, _ = run_detect(candles, kombi_pivot_accel=True)
    det_off, _, _ = run_detect(candles, kombi_pivot_accel=False)
    labs_on = {s[2] for s in segments_of(det_on["live3"])}
    labs_off = {s[2] for s in segments_of(det_off["live3"])}
    assert labs_on <= labs_off | {1}, \
        "Beschleuniger hat neue Trend-Richtungen erfunden"


# ------------------------------ Regression alte Detektoren --------------------
def test_reactive_and_ema_detectors_unchanged():
    closes = synth_closes([(30, 2.0), (30, -2.0)])
    candles = make_candles(closes, noise=0.002)
    for detector in ("reactive", "ema"):
        cfg = eng.resolve_config({"detector": detector, "auto_adapt": False,
                                  "adapt_profile": "off", "regime_mode": 3},
                                 "1h", len(candles))
        assert cfg["detector"] == detector
        f = eng.compute_matrix(candles, cfg)
        ids, conf, det = rx.classify(f, cfg)
        assert len(ids) == len(candles)
        assert set(np.unique(det["live3"])) <= {0, 1, 2}


# ------------------------------ API -------------------------------------------
@pytest.fixture(scope="module")
def token():
    r = requests.post(f"{API}/auth/login",
                      json={"username": ADMIN_USER, "password": ADMIN_PASS},
                      timeout=15)
    assert r.status_code == 200, f"Login fehlgeschlagen: {r.text}"
    return r.json()["token"]


def test_engine_defaults_contains_kombi(token):
    r = requests.get(f"{API}/regime-lab/engine/defaults", timeout=15)
    assert r.status_code == 200
    d = r.json()
    cfg = d["config"]
    for k, v in (("kombi_ema_days", 14.0), ("kombi_thr", 0.18),
                 ("kombi_slope_days", 5.0), ("kombi_dominance_days", 3.0),
                 ("kombi_pivot_accel", True)):
        assert cfg.get(k) == v, f"Default {k} != {v}"
    meta = {m["key"]: m for m in d["meta"]}
    assert "kombi_thr" in meta
    assert meta["kombi_thr"]["detectors"] == ["kombi"]
    assert "Kombi" in meta["kombi_thr"]["group"]


def test_kombi_calibrate_requires_admin():
    r = requests.post(f"{API}/regime-lab/kombi-calibrate",
                      json={"symbols": ["BTCUSDT"]}, timeout=15)
    assert r.status_code in (401, 403)


def test_kombi_calibrate_validates_symbols(token):
    r = requests.post(f"{API}/regime-lab/kombi-calibrate", json={},
                      headers={"Authorization": f"Bearer {token}"}, timeout=15)
    assert r.status_code == 400
