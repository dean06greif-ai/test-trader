"""Iteration 2 tests: mini-phase filter, MTF confirm, volume confirm,
live overlay data. Adds regressions for engine defaults with new keys.
"""
from __future__ import annotations

import os
import time

import pytest
import requests

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")


@pytest.fixture(scope="session")
def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="session")
def token(session: requests.Session) -> str:
    r = session.post(
        f"{BASE_URL}/api/auth/login",
        json={"username": os.environ.get("ADMIN_USER", "Admin"), "password": os.environ.get("ADMIN_PASSWORD", "admin")},
        timeout=15,
    )
    assert r.status_code == 200, r.text
    d = r.json()
    return d["token"]


@pytest.fixture(scope="session")
def auth(session: requests.Session, token: str) -> requests.Session:
    session.headers.update({"Authorization": f"Bearer {token}"})
    return session


def _wait_no_active(auth: requests.Session, timeout: int = 180) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = auth.get(f"{BASE_URL}/api/regime-lab/active", timeout=10)
        if r.status_code == 200 and not r.json().get("active"):
            return
        time.sleep(2.0)


def _wait_job(auth: requests.Session, job_id: str, timeout: int = 240) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        r = auth.get(f"{BASE_URL}/api/regime-lab/status/{job_id}", timeout=15)
        assert r.status_code == 200, r.text
        last = r.json()
        state = (last.get("status") or last.get("state") or "").lower()
        if state in {"done", "finished", "success", "completed"}:
            return last
        if state in {"error", "failed"}:
            pytest.fail(f"Job failed: {last}")
        time.sleep(3.0)
    pytest.fail(f"Job {job_id} did not complete within {timeout}s; last={last}")


def _analyze_and_get(auth, engine_config: dict, name: str) -> dict:
    _wait_no_active(auth)
    payload = {
        "symbols": ["BTCUSDT"],
        "timeframe": "15m",
        "days": 180,
        "scope": "combined",
        "engine": "v2",
        "regime_mode": 3,
        "execution": "cloud",
        "min_hold_days": 0,
        "engine_config": engine_config,
        "name": name,
    }
    r = auth.post(f"{BASE_URL}/api/regime-lab/analyze", json=payload, timeout=30)
    assert r.status_code == 200, r.text
    job = r.json()
    jid = job.get("job_id") or job.get("id")
    assert jid, job
    final = _wait_job(auth, jid, timeout=300)
    aid = final.get("analysis_id") or (final.get("result") or {}).get("analysis_id")
    if not aid:
        lst = auth.get(f"{BASE_URL}/api/regime-lab/list", timeout=10).json()
        aid = lst["analyses"][0]["id"]
    det = auth.get(f"{BASE_URL}/api/regime-lab/{aid}", timeout=20).json()
    return det.get("analysis") or {}


class TestEngineDefaultsIter2:
    def test_defaults_expose_new_keys(self, auth: requests.Session):
        r = auth.get(f"{BASE_URL}/api/regime-lab/engine/defaults", timeout=10)
        assert r.status_code == 200
        d = r.json()
        cfg = d.get("config", {})
        for k in ("min_phase_days", "mtf_confirm", "mtf_mult",
                  "use_volume_confirm", "volume_boost",
                  "detector", "rev_atr_mult", "persist_candles"):
            assert k in cfg, f"missing cfg key {k}"
        meta_keys = {m.get("key") for m in d.get("meta", [])}
        for k in ("min_phase_days", "mtf_confirm", "mtf_mult",
                  "use_volume_confirm", "volume_boost"):
            assert k in meta_keys, f"missing meta key {k}"
        # sensible defaults
        assert cfg["mtf_confirm"] is True
        assert cfg["use_volume_confirm"] is True
        assert float(cfg["mtf_mult"]) >= 1.0
        assert float(cfg["volume_boost"]) >= 1.0


class TestAnalyzeMiniPhaseAuto:
    """Full 15m/180d analysis with min_phase_days=0 (auto)."""

    @pytest.fixture(scope="class")
    def analysis_auto(self, auth):
        return _analyze_and_get(auth, {"min_phase_days": 0}, "iter2-auto")

    def test_segments_reasonable(self, analysis_auto):
        ps = (analysis_auto.get("combined") or {}) \
            .get("per_symbol", {}).get("BTCUSDT", {})
        segs = ps.get("segments") or []
        assert segs, "segments empty"
        assert len(segs) < 60, f"too many segments: {len(segs)}"

    def test_corrections_min_phase_days_positive(self, analysis_auto):
        ps = (analysis_auto.get("combined") or {}) \
            .get("per_symbol", {}).get("BTCUSDT", {})
        corr = ps.get("corrections") or {}
        assert "min_phase_days" in corr, corr
        assert float(corr["min_phase_days"]) > 0, corr

    def test_live_segments_present(self, analysis_auto):
        ps = (analysis_auto.get("combined") or {}) \
            .get("per_symbol", {}).get("BTCUSDT", {})
        live = ps.get("live_segments") or []
        assert live, "live_segments missing/empty"
        # basic shape check
        first = live[0]
        assert "regime" in first or "label" in first or "state" in first, first


class TestAnalyzeMiniPhaseFixed:
    """Same analysis but with min_phase_days=5 -> fewer/equal segments."""

    @pytest.fixture(scope="class")
    def analyses(self, auth):
        a_auto = _analyze_and_get(auth, {"min_phase_days": 0}, "iter2-auto2")
        a_five = _analyze_and_get(auth, {"min_phase_days": 5}, "iter2-mp5")
        return a_auto, a_five

    def test_more_min_phase_days_reduces_segments(self, analyses):
        a_auto, a_five = analyses
        ps_a = (a_auto.get("combined") or {}) \
            .get("per_symbol", {}).get("BTCUSDT", {})
        ps_5 = (a_five.get("combined") or {}) \
            .get("per_symbol", {}).get("BTCUSDT", {})
        segs_a = ps_a.get("segments") or []
        segs_5 = ps_5.get("segments") or []
        assert segs_a and segs_5
        assert len(segs_5) <= len(segs_a), (
            f"min_phase_days=5 produced MORE segments "
            f"({len(segs_5)}) than auto ({len(segs_a)})"
        )
        corr_5 = ps_5.get("corrections") or {}
        # 5 requested. Actual value should be >= 5 (bar-rounding may bump).
        assert float(corr_5.get("min_phase_days", 0)) >= 4.9
