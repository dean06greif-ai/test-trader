"""Iteration 3 tests: live-detection fix (MTF anchor, causal hold, robust final rule).

Acceptance criteria (from review request):
- POST /api/regime-lab/analyze with symbols=[BTC,ETH] 1h/360d cloud engine v2 mode 3
  min_hold_days=1 engine_config.min_phase_days=1 train_pct=75 -> job finishes.
- combined.per_symbol.BTCUSDT.segments has between 5 and 60 entries (final).
- combined.per_symbol.BTCUSDT.live_segments has < 200 entries (Live-Flicker-Fix).
- combined.validation.passed == True.
- live_segments direction diversity: no single regime > 70% of the total time,
  AT LEAST 2 distinct regime-ids each > 10% of total time.
- current_regime returns label, confidence, details.probabilities, early_warning.

Regression: engine defaults still expose the reactive/iter2 keys.
"""
from __future__ import annotations

import os
import time
from collections import defaultdict

import pytest
import requests

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")


# ---------------------------------------------------------------- helpers
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
    return r.json()["token"]


@pytest.fixture(scope="session")
def auth(session: requests.Session, token: str) -> requests.Session:
    session.headers.update({"Authorization": f"Bearer {token}"})
    return session


def _wait_no_active(auth: requests.Session, timeout: int = 240) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = auth.get(f"{BASE_URL}/api/regime-lab/active", timeout=10)
        if r.status_code == 200 and not r.json().get("active"):
            return
        time.sleep(2.0)


def _wait_job(auth: requests.Session, job_id: str, timeout: int = 300) -> dict:
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


def _run_analysis(auth: requests.Session, name: str) -> dict:
    _wait_no_active(auth)
    payload = {
        "symbols": ["BTCUSDT", "ETHUSDT"],
        "timeframe": "1h",
        "days": 360,
        "scope": "combined",
        "engine": "v2",
        "regime_mode": 3,
        "execution": "cloud",
        "min_hold_days": 1,
        "engine_config": {"min_phase_days": 1},
        "train_pct": 75,
        "name": name,
    }
    r = auth.post(f"{BASE_URL}/api/regime-lab/analyze", json=payload, timeout=30)
    assert r.status_code == 200, r.text
    jid = r.json().get("job_id") or r.json().get("id")
    assert jid, r.text
    final = _wait_job(auth, jid, timeout=300)
    aid = final.get("analysis_id") or (final.get("result") or {}).get("analysis_id")
    if not aid:
        lst = auth.get(f"{BASE_URL}/api/regime-lab/list", timeout=10).json()
        aid = lst["analyses"][0]["id"]
    det = auth.get(f"{BASE_URL}/api/regime-lab/{aid}", timeout=20).json()
    return det.get("analysis") or {}


def _seg_key(seg: dict):
    """Extract regime id / label independent of exact field name."""
    for k in ("regime", "regime_id", "id", "label", "state"):
        if k in seg:
            return seg[k]
    return None


def _seg_bounds(seg: dict):
    """Return (start, end) numeric timestamps or indices for a segment."""
    for pair in (("start", "end"), ("t0", "t1"), ("from", "to"),
                 ("start_ts", "end_ts"), ("i0", "i1")):
        if pair[0] in seg and pair[1] in seg:
            try:
                return float(seg[pair[0]]), float(seg[pair[1]])
            except (TypeError, ValueError):
                pass
    return None, None


def _regime_time_share(live_segments):
    total = 0.0
    per = defaultdict(float)
    for s in live_segments:
        a, b = _seg_bounds(s)
        if a is None or b is None or b <= a:
            # fall back to unit weight
            per[_seg_key(s)] += 1.0
            total += 1.0
            continue
        dur = b - a
        per[_seg_key(s)] += dur
        total += dur
    if total <= 0:
        return {}, 0.0
    return {k: v / total for k, v in per.items()}, total


# ---------------------------------------------------------------- session-scoped analysis
@pytest.fixture(scope="session")
def analysis_iter3(auth):
    """Reuse the freshly created 'iter3-live-fix' analysis if present, else create one."""
    # try to reuse the analysis just created by the agent
    r = auth.get(f"{BASE_URL}/api/regime-lab/list", timeout=15)
    if r.status_code == 200:
        for item in r.json().get("analyses", []):
            if item.get("name") == "iter3-live-fix":
                aid = item.get("id")
                det = auth.get(f"{BASE_URL}/api/regime-lab/{aid}", timeout=20).json()
                a = det.get("analysis") or {}
                # verify it looks fresh (has combined)
                if a.get("combined"):
                    return a
    return _run_analysis(auth, "iter3-live-fix-2")


# ---------------------------------------------------------------- tests
class TestRegimeIter3LiveFix:

    def test_segments_final_count_5_to_60(self, analysis_iter3):
        ps = ((analysis_iter3.get("combined") or {})
              .get("per_symbol", {}).get("BTCUSDT", {}))
        segs = ps.get("segments") or []
        assert 5 <= len(segs) <= 60, (
            f"BTCUSDT segments={len(segs)} not in [5,60]"
        )

    def test_live_segments_under_200(self, analysis_iter3):
        ps = ((analysis_iter3.get("combined") or {})
              .get("per_symbol", {}).get("BTCUSDT", {}))
        live = ps.get("live_segments") or []
        assert live, "live_segments empty"
        assert len(live) < 200, (
            f"live_segments={len(live)} (>=200 means live-flicker not fixed)"
        )

    def test_validation_passed(self, analysis_iter3):
        v = (analysis_iter3.get("combined") or {}).get("validation") or {}
        assert v.get("passed") is True, v

    def test_live_segments_are_directional(self, analysis_iter3):
        """No single regime > 70% AND at least 2 regimes each > 10%."""
        ps = ((analysis_iter3.get("combined") or {})
              .get("per_symbol", {}).get("BTCUSDT", {}))
        live = ps.get("live_segments") or []
        assert live, "live_segments empty"
        share, total = _regime_time_share(live)
        assert total > 0
        top = max(share.values()) if share else 1.0
        assert top <= 0.70, (
            f"BTCUSDT live-share top={top:.3f} (>70% single regime) -> "
            f"non-directional: {share}"
        )
        big = [k for k, v in share.items() if v > 0.10]
        assert len(big) >= 2, (
            f"BTCUSDT only {len(big)} regime(s) with >10% share: {share}"
        )

    def test_live_segments_eth_are_directional(self, analysis_iter3):
        """Same check for ETH (Bug c: ETH abwärts als aufwärts erkannt)."""
        ps = ((analysis_iter3.get("combined") or {})
              .get("per_symbol", {}).get("ETHUSDT", {}))
        live = ps.get("live_segments") or []
        if not live:
            pytest.skip("ETHUSDT live_segments missing (analysis maybe BTC-only)")
        share, total = _regime_time_share(live)
        top = max(share.values()) if share else 1.0
        assert top <= 0.70, (
            f"ETHUSDT live-share top={top:.3f}: {share}"
        )
        big = [k for k, v in share.items() if v > 0.10]
        assert len(big) >= 2, (
            f"ETHUSDT only {len(big)} regime(s) with >10% share: {share}"
        )

    def test_current_regime_shape(self, analysis_iter3):
        ps = ((analysis_iter3.get("combined") or {})
              .get("per_symbol", {}).get("BTCUSDT", {}))
        cur = ps.get("current") or ps.get("current_regime") or {}
        assert cur, f"no current regime block: {list(ps.keys())}"
        assert "label" in cur, cur
        assert "confidence" in cur, cur
        assert isinstance(cur["confidence"], (int, float))
        details = cur.get("details") or {}
        probs = details.get("probabilities") or {}
        assert probs, f"missing details.probabilities: {details}"
        for k in ("down", "side", "up"):
            assert k in probs, f"probabilities missing {k}: {probs}"
        # early_warning may be under current or per_symbol
        ew = cur.get("early_warning") or ps.get("early_warning")
        assert ew is not None, f"early_warning missing: keys={list(cur.keys())}"

    def test_corrections_block(self, analysis_iter3):
        ps = ((analysis_iter3.get("combined") or {})
              .get("per_symbol", {}).get("BTCUSDT", {}))
        corr = ps.get("corrections") or {}
        assert corr.get("pivots", 0) > 0, corr
        assert corr.get("avg_delay_days") is not None, corr


# ---------------------------------------------------------------- regression
class TestEngineDefaultsRegression:
    def test_defaults_still_have_iter2_keys(self, auth: requests.Session):
        r = auth.get(f"{BASE_URL}/api/regime-lab/engine/defaults", timeout=10)
        assert r.status_code == 200
        cfg = r.json().get("config", {})
        for k in ("detector", "rev_atr_mult", "persist_candles",
                  "min_phase_days", "mtf_confirm", "mtf_mult",
                  "use_volume_confirm", "volume_boost"):
            assert k in cfg, f"missing cfg key {k}"
        assert cfg["detector"] == "reactive"
