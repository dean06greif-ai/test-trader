"""Etappe 2 – Regime-Übergangs-Matrix Verification Tests.

Coverage:
- transition_matrix() Service-Logik auf synthetischem Analyse-Dokument:
  Zähler, Wahrscheinlichkeiten, Kontext (Ø Dauer), Richtungs-Ebene mit
  Merge gleichgerichteter Nachbar-Abschnitte, keine Verkettung über Coins
- view='live' nutzt live_segments, Fallback auf segments
- API: GET /api/regime-lab/{aid}/transitions validiert scope/view/symbol,
  404 bei unbekannter Analyse
"""
import os

import pytest
import requests

from services.regime_lab import transition_matrix

BASE_URL = (os.environ.get("REACT_APP_BACKEND_URL")
            or "http://localhost:8001").rstrip("/")
API = f"{BASE_URL}/api"


def seg(rid, bars, t0=0):
    return {"regime": rid, "from_ts": t0, "to_ts": t0 + bars, "bars": bars}


def make_doc(per_symbol=None, per_coin=None, mode=5, timeframe="1h"):
    return {"id": "ra_test", "timeframe": timeframe,
            "settings": {"regime_mode": mode},
            "combined": ({"per_symbol": per_symbol} if per_symbol else None),
            "per_coin": per_coin or {}}


# Modus 5: 0=stark ab · 1=leicht ab · 2=seitwärts · 3=leicht auf · 4=stark auf
def test_matrix_counts_and_probs():
    # BTC: 3 -> 2 -> 1 -> 2 -> 3   (24 bars/Tag)
    segs = [seg(3, 240), seg(2, 120), seg(1, 240), seg(2, 48), seg(3, 120)]
    doc = make_doc(per_symbol={"BTCUSDT": {"segments": segs}})
    r = transition_matrix(doc, "combined", None, "final")
    assert r["total_transitions"] == 4
    m = {(x["from"], x["to"]): x for x in r["matrix"]}
    assert m[(3, 2)]["count"] == 1
    assert m[(2, 1)]["prob_pct"] == 50.0    # von Seitwärts: 1x ->1, 1x ->3
    assert m[(2, 3)]["prob_pct"] == 50.0
    assert m[(3, 2)]["avg_from_days"] == 10.0   # 240 bars / 24
    pf = {p["from"]: p for p in r["per_from"]}
    assert pf[2]["total"] == 2
    assert pf[2]["avg_days"] == 3.5             # (120+48)/2/24


def test_direction_level_merges_same_direction():
    # leicht auf -> stark auf = EIN Aufwärts-Lauf; dann seitwärts, dann ab
    segs = [seg(3, 100), seg(4, 100), seg(2, 50), seg(1, 100)]
    doc = make_doc(per_symbol={"BTCUSDT": {"segments": segs}})
    r = transition_matrix(doc, "combined", None, "final")
    dm = {(x["from"], x["to"]): x for x in r["direction_matrix"]}
    assert (2, 2) not in dm, "gleichgerichtete Abschnitte nicht gemergt"
    assert dm[(2, 1)]["count"] == 1     # Auf -> Seitwärts
    assert dm[(1, 0)]["count"] == 1     # Seitwärts -> Ab
    assert dm[(2, 1)]["avg_from_days"] == pytest.approx(200 / 24, abs=0.06)


def test_no_chaining_across_coins():
    doc = make_doc(per_symbol={
        "BTCUSDT": {"segments": [seg(3, 100), seg(2, 100)]},
        "ETHUSDT": {"segments": [seg(1, 100), seg(2, 100)]},
    })
    r = transition_matrix(doc, "combined", None, "final")
    m = {(x["from"], x["to"]) for x in r["matrix"]}
    assert m == {(3, 2), (1, 2)}, "Übergänge wurden über Coin-Grenzen verkettet"
    assert r["total_transitions"] == 2
    assert r["last"] is None, "'last' darf es bei mehreren Coins nicht geben"


def test_live_view_and_fallback():
    entry = {"segments": [seg(3, 100), seg(2, 100)],
             "live_segments": [seg(3, 60), seg(2, 60), seg(1, 60)]}
    doc = make_doc(per_symbol={"BTCUSDT": entry})
    r_live = transition_matrix(doc, "combined", None, "live")
    assert r_live["total_transitions"] == 2
    # Fallback: kein live_segments vorhanden -> segments
    doc2 = make_doc(per_symbol={"BTCUSDT": {"segments": [seg(3, 100), seg(2, 100)]}})
    r2 = transition_matrix(doc2, "combined", None, "live")
    assert r2["total_transitions"] == 1


def test_per_coin_scope_and_last():
    doc = make_doc(per_coin={"ETHUSDT": {"segments": [seg(1, 100), seg(2, 48)]}})
    r = transition_matrix(doc, "per_coin", "ETHUSDT", "final")
    assert r["total_transitions"] == 1
    assert r["last"]["regime"] == 2
    assert r["last"]["days"] == 2.0
    assert r["last"]["direction"] == 1
    assert "label" in r["last"]


def test_empty_doc_is_safe():
    r = transition_matrix(make_doc(), "combined", None, "final")
    assert r["total_transitions"] == 0
    assert r["matrix"] == []


# ------------------------------ API -------------------------------------------
def test_api_validation_errors():
    r = requests.get(f"{API}/regime-lab/ra_gibtsnicht/transitions",
                     params={"scope": "quatsch"}, timeout=15)
    assert r.status_code == 400
    r = requests.get(f"{API}/regime-lab/ra_gibtsnicht/transitions",
                     params={"view": "quatsch"}, timeout=15)
    assert r.status_code == 400
    r = requests.get(f"{API}/regime-lab/ra_gibtsnicht/transitions",
                     params={"scope": "per_coin"}, timeout=15)
    assert r.status_code == 400
    r = requests.get(f"{API}/regime-lab/ra_gibtsnicht/transitions", timeout=15)
    assert r.status_code == 404


def test_api_real_analysis_if_present():
    lst = requests.get(f"{API}/regime-lab/list", timeout=15).json()
    analyses = lst.get("analyses") or []
    if not analyses:
        pytest.skip("Keine gespeicherte Analyse vorhanden")
    aid = analyses[0]["id"]
    r = requests.get(f"{API}/regime-lab/{aid}/transitions",
                     params={"scope": "combined", "view": "final"}, timeout=30)
    assert r.status_code == 200
    d = r.json()
    for k in ("matrix", "direction_matrix", "per_from", "regimes",
              "total_transitions", "mode"):
        assert k in d
    for cell in d["matrix"]:
        assert 0 <= cell["prob_pct"] <= 100
