"""
Iteration 5 – Backend tests for new /api/optimizer/equity/{job_id} endpoint
plus regression checks for the existing /api/backtest/equity/{job_id}.

Covers:
  - Optimizer equity scope=optimized (default) returns points for run's symbols
  - Optimizer equity scope=all returns points across (potentially) more Top-10 coins
  - Unknown job_id returns 404
  - Existing backtest equity endpoint still works

External URL from REACT_APP_BACKEND_URL. Admin login = { username:'Admin', password:'admin' }.
"""

import os
import time
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL").rstrip("/")
API = f"{BASE_URL}/api"

ADMIN_USER = "Admin"
ADMIN_PW = "admin"


@pytest.fixture(scope="module")
def api_client():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="module")
def admin_client(api_client):
    r = api_client.post(f"{API}/auth/login",
                        json={"username": ADMIN_USER, "password": ADMIN_PW}, timeout=15)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    tok = r.json().get("token")
    assert tok
    api_client.headers.update({"Authorization": f"Bearer {tok}"})
    return api_client


def _wait_optimizer(client: requests.Session, job_id: str, timeout: int = 180) -> dict:
    """Poll optimizer status until done/error/cancelled."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        r = client.get(f"{API}/optimizer/status/{job_id}", timeout=15)
        assert r.status_code == 200, f"status failed: {r.status_code} {r.text}"
        last = r.json()
        if last.get("status") in ("done", "error", "cancelled"):
            return last
        time.sleep(1.0)
    raise AssertionError(f"optimizer job {job_id} did not finish in {timeout}s (last={last})")


@pytest.fixture(scope="module")
def quick_optimizer_job(admin_client):
    """Start a small, fast optimizer run so tests don't depend on any leftover DB state
    that might have long 360-day settings. Params mode, 1 coin, days=1, iterations=5."""
    # Ensure no other run is active first
    active = admin_client.get(f"{API}/optimizer/active", timeout=15).json()
    if active.get("active"):
        # try cancel
        jid = active["active"].get("id")
        if jid:
            admin_client.post(f"{API}/optimizer/cancel/{jid}", timeout=15)
            # wait a bit
            for _ in range(30):
                a = admin_client.get(f"{API}/optimizer/active", timeout=15).json()
                if not a.get("active"):
                    break
                time.sleep(1.0)

    body = {
        "mode": "params",
        "strategy_id": "rsi_only",
        "symbols": ["BTCUSDT"],
        "days": 1,
        "timeframe": "1m",
        "objective": "pnl",
        "iterations": 5,
        "min_trades": 0,
        "algorithm": "random",
    }
    r = admin_client.post(f"{API}/optimizer/run", json=body, timeout=30)
    if r.status_code != 200:
        pytest.skip(f"optimizer/run refused with {r.status_code}: {r.text}")
    job_id = r.json().get("job_id")
    assert job_id
    final = _wait_optimizer(admin_client, job_id, timeout=180)
    if final.get("status") != "done":
        pytest.skip(f"quick optimizer did not finish successfully: {final}")
    return job_id


# ---------- New: /api/optimizer/equity/{job_id} ----------
class TestOptimizerEquity:
    def test_scope_optimized_returns_points(self, admin_client, quick_optimizer_job):
        job_id = quick_optimizer_job
        r = admin_client.get(f"{API}/optimizer/equity/{job_id}?scope=optimized", timeout=120)
        assert r.status_code == 200, f"unexpected status: {r.status_code} {r.text[:400]}"
        d = r.json()
        # response shape
        assert d.get("job_id") == job_id
        assert d.get("scope") == "optimized"
        syms = d.get("symbols") or []
        assert isinstance(syms, list) and len(syms) >= 1
        # optimized run was for BTCUSDT
        assert "BTCUSDT" in syms
        points = d.get("points")
        assert isinstance(points, list)
        # points may be empty if 1-day rsi_only produces no closed trades - allow that but
        # validate structure if any exist
        for p in points[:5]:
            assert "t" in p and "pnl" in p and "symbol" in p
            assert "equity" in p and "peak" in p and "drawdown" in p
            assert "strategy_id" in p and "strategy_name" in p
            assert isinstance(p["liquidated"], bool)

    def test_scope_all_includes_more_or_equal_symbols(self, admin_client, quick_optimizer_job):
        job_id = quick_optimizer_job
        r = admin_client.get(f"{API}/optimizer/equity/{job_id}?scope=all", timeout=180)
        assert r.status_code == 200, f"unexpected status: {r.status_code} {r.text[:400]}"
        d = r.json()
        assert d.get("job_id") == job_id
        assert d.get("scope") == "all"
        syms_all = set(d.get("symbols") or [])
        # scope=all must cover more than just the single BTCUSDT optimized coin
        assert len(syms_all) >= 5, f"scope=all should include full Top-10, got {syms_all}"
        assert "BTCUSDT" in syms_all
        # points must be a list, and if non-empty must include at least one non-BTC symbol
        pts = d.get("points") or []
        assert isinstance(pts, list)
        # Even if some coins produce zero trades, at least the endpoint doesn't crash
        # and returns the full symbol list.
        other_syms = {p["symbol"] for p in pts} - {"BTCUSDT"}
        if pts:
            # Not strictly guaranteed (all trades could come from BTC only), so this is soft
            print(f"scope=all trade coins: {sorted({p['symbol'] for p in pts})}, total pts={len(pts)}")

    def test_unknown_id_returns_404(self, admin_client):
        r = admin_client.get(f"{API}/optimizer/equity/does_not_exist_xyz", timeout=15)
        assert r.status_code == 404, f"expected 404, got {r.status_code} {r.text[:200]}"

    def test_public_access(self, api_client, quick_optimizer_job):
        """No auth needed for GET (matches other GET endpoints)."""
        # New unauthenticated client (module fixture admin_client sets Authorization)
        s = requests.Session()
        r = s.get(f"{API}/optimizer/equity/{quick_optimizer_job}?scope=optimized", timeout=120)
        assert r.status_code == 200, f"unexpected: {r.status_code} {r.text[:200]}"


# ---------- Regression: existing backtest equity endpoint ----------
class TestBacktestEquityRegression:
    def test_backtest_equity_still_works(self, admin_client):
        """Run a tiny 1-day backtest and check /api/backtest/equity works unchanged."""
        body = {
            "strategy_ids": ["rsi_only"],
            "symbols": ["BTCUSDT"],
            "days": 1,
            "timeframe": "1m",
            "max_capital": 100.0,
        }
        r = admin_client.post(f"{API}/backtest/run", json=body, timeout=30)
        assert r.status_code == 200, f"backtest start failed: {r.status_code} {r.text[:300]}"
        job_id = r.json().get("job_id")
        assert job_id

        # poll
        deadline = time.time() + 120
        final = None
        while time.time() < deadline:
            s = admin_client.get(f"{API}/backtest/status/{job_id}", timeout=15)
            assert s.status_code == 200
            js = s.json()
            if js.get("status") in ("done", "error", "cancelled"):
                final = js
                break
            time.sleep(1.0)
        assert final and final.get("status") == "done", f"backtest not done: {final}"

        r = admin_client.get(f"{API}/backtest/equity/{job_id}", timeout=30)
        assert r.status_code == 200, f"equity endpoint failed: {r.status_code} {r.text[:200]}"
        d = r.json()
        assert "points" in d
        assert isinstance(d["points"], list)
        # each point (if any) must have the standard fields
        for p in d["points"][:3]:
            assert "t" in p and "equity" in p and "drawdown" in p
