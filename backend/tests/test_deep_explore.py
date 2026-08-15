"""Tests: Endlos-Suche (Optimizer mode='explore') + Indikator-Pool-Sync.

Läuft gegen den laufenden Backend-Server (localhost:8001).
"""
import os
import sys
import time

import requests

BASE = "http://localhost:8001"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _hdr():
    r = requests.post(f"{BASE}/api/auth/login",
                      json={"username": os.environ.get("ADMIN_USER", "Admin"),
                            "password": os.environ.get("ADMIN_PASSWORD", "admin")},
                      timeout=10)
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _wait_done(job_id, timeout=240):
    st = {}
    for _ in range(timeout):
        st = requests.get(f"{BASE}/api/optimizer/status/{job_id}", timeout=10).json()
        if st.get("status") != "running":
            return st
        time.sleep(2)
    return st


class TestCandidatePool:
    def test_ha_color_removed_from_candidates(self):
        from services.optimizer import build_candidates
        inds = {c["ind"] for c in build_candidates(None)}
        assert "ha_color" not in inds, "Heikin-Ashi sollte kein Discovery-Kandidat mehr sein"

    def test_stage5_indicators_available(self):
        from services.optimizer import build_candidates
        inds = {c["ind"] for c in build_candidates(None)}
        for ind in ("market_structure", "bos_up", "dist_ema200_pct", "channel_pos",
                    "range_pos", "liq_sweep_low", "eq_low_dist_pct", "days_to_fomc",
                    "dist_support_pct", "channel_slope_pct"):
            assert ind in inds, f"{ind} fehlt im Kandidaten-Pool"

    def test_frontend_pool_matches_backend(self):
        """Jede Frontend-Indikator-ID muss ein Backend-Kandidat sein (und umgekehrt)."""
        import json
        import re
        from services.optimizer import build_candidates
        js = open(os.path.join(os.path.dirname(__file__),
                               "../../frontend/src/lib/indicatorPool.js")).read()
        fe_ids = set(re.findall(r"id:\s*'([a-z0-9_]+)'", js))
        be_ids = {c["ind"] for c in build_candidates(None)}
        assert fe_ids == be_ids, (f"Pool-Drift! Nur Frontend: {fe_ids - be_ids} · "
                                  f"Nur Backend: {be_ids - fe_ids}")
        del json


class TestExploreMode:
    def test_invalid_mode_rejected(self):
        r = requests.post(f"{BASE}/api/optimizer/run", headers=_hdr(),
                          json={"mode": "quatsch", "symbols": ["BTCUSDT"]}, timeout=10)
        assert r.status_code == 400
        assert "explore" in r.json()["detail"]

    def test_explore_run_time_limited(self):
        """Kurzer Endlos-Lauf mit Zeitlimit: muss sauber durchlaufen und den
        Explore-Report liefern (nicht zweimal dasselbe Ergebnis erzwingbar
        prüfbar, aber Seed-basiert)."""
        hdr = _hdr()
        body = {"mode": "explore", "symbols": ["BTCUSDT"], "days": 2,
                "timeframe": "5m", "iterations": 5, "min_trades": 3,
                "max_rules": 3,
                "indicators": ["rsi", "ema_fast", "ema_slow", "macd_hist",
                               "price_change_pct", "range_pos"],
                "explore": {"target_champions": 1, "max_minutes": 1},
                "walk_forward": {"enabled": True, "train_pct": 75, "mode": "single"}}
        r = requests.post(f"{BASE}/api/optimizer/run", headers=hdr, json=body,
                          timeout=15)
        if r.status_code == 409:
            return  # anderer Lauf aktiv
        assert r.status_code == 200, r.text
        job_id = r.json()["job_id"]
        st = _wait_done(job_id)
        assert st.get("status") == "done", st.get("error")
        rep = (st.get("result") or {}).get("explore_report")
        assert rep, "explore_report fehlt im Ergebnis"
        assert rep["tested"] > 0
        assert rep["stop_reason"] in ("target_reached", "time_limit",
                                      "space_exhausted")
        assert "top5" in st["result"]
        # Walk-Forward war Pflicht
        assert (st["result"].get("walk_forward") or {}).get("mode") == "single"

    def test_explore_stop_endpoint_keeps_best(self):
        hdr = _hdr()
        body = {"mode": "explore", "symbols": ["BTCUSDT"], "days": 2,
                "timeframe": "5m", "iterations": 5, "min_trades": 3,
                "indicators": ["rsi", "ema_fast", "ema_slow", "macd_hist",
                               "price_change_pct", "range_pos", "atr_pct"],
                "explore": {"target_champions": 10, "max_minutes": 0}}
        r = requests.post(f"{BASE}/api/optimizer/run", headers=hdr, json=body,
                          timeout=15)
        if r.status_code == 409:
            return
        assert r.status_code == 200, r.text
        job_id = r.json()["job_id"]
        time.sleep(8)  # etwas suchen lassen
        r = requests.post(f"{BASE}/api/optimizer/explore/stop/{job_id}",
                          headers=hdr, timeout=10)
        assert r.status_code in (200, 409), r.text  # 409 = schon fertig
        st = _wait_done(job_id, timeout=90)
        assert st.get("status") == "done", st.get("error")
        assert (st.get("result") or {}).get("explore_report") is not None

    def test_explore_best_endpoint(self):
        r = requests.get(f"{BASE}/api/optimizer/explore/best", timeout=10)
        assert r.status_code == 200
        assert "items" in r.json()

    def test_stop_requires_admin(self):
        r = requests.post(f"{BASE}/api/optimizer/explore/stop/xyz", timeout=10)
        assert r.status_code == 401
