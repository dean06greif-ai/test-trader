"""Iteration 15: Tests for CandleArray refactor + new features.

Covers:
- GET /api/dynamic/current-regime (multi-symbol, edge cases)
- GET /api/localworker/status (required_version, outdated flag)
- Cloud backtest run + CSV exports (candles/trades)
- Optimizer dynamic mode with per_regime_strategies true/false
- POST /api/dynamic/save with sub_strategies + list
- Local backtest fast execution
"""
import os
import time
import csv
import io
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "http://localhost:8001").rstrip("/")


def _resolve_custom_strat():
    """Erste existierende Custom-Strategie nehmen (kein Hardcoding auf
    eine ggf. gelöschte ID)."""
    try:
        d = requests.get(f"{BASE_URL}/api/strategies", timeout=15).json()
        strats = d.get("strategies", d) if isinstance(d, dict) else d
        for s in strats:
            if str(s.get("id", "")).startswith("custom_"):
                return s["id"]
    except Exception:
        pass
    return "custom_3a7f5e25"  # Fallback (historischer Wert)


CUSTOM_STRAT = _resolve_custom_strat()


@pytest.fixture(scope="module")
def token():
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={"username": os.environ.get("ADMIN_USER", "Admin"), "password": os.environ.get("ADMIN_PASSWORD", "admin")}, timeout=15)
    assert r.status_code == 200, r.text
    return r.json()["token"]


@pytest.fixture(scope="module")
def auth(token):
    return {"Authorization": f"Bearer {token}"}


# ---------- Dynamic current-regime ----------
class TestCurrentRegime:
    def test_btc_60d(self):
        r = requests.get(f"{BASE_URL}/api/dynamic/current-regime",
                         params={"symbol": "BTCUSDT", "timeframe": "5m", "days": 60},
                         timeout=120)
        assert r.status_code == 200, r.text
        d = r.json()
        assert "current" in d
        cur = d["current"]
        assert "regime" in cur and "label" in cur and "confidence" in cur
        assert isinstance(d.get("regimes"), list) and len(d["regimes"]) >= 1
        for reg in d["regimes"]:
            assert "share_pct" in reg
        assert isinstance(d.get("timeline"), list) and len(d["timeline"]) > 0
        assert "silhouette" in d

    def test_eth_30d(self):
        r = requests.get(f"{BASE_URL}/api/dynamic/current-regime",
                         params={"symbol": "ETHUSDT", "timeframe": "5m", "days": 30},
                         timeout=120)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["current"]["label"]
        assert len(d["regimes"]) >= 1

    def test_invalid_small_days_no_crash(self):
        # days=1 gets clamped to 14 -> should succeed OR return 400 with helpful message
        r = requests.get(f"{BASE_URL}/api/dynamic/current-regime",
                         params={"symbol": "BTCUSDT", "timeframe": "5m", "days": 1},
                         timeout=120)
        assert r.status_code in (200, 400), r.text


# ---------- Local worker status ----------
class TestLocalWorkerStatus:
    def test_required_version_and_outdated(self):
        r = requests.get(f"{BASE_URL}/api/localworker/status", timeout=10)
        assert r.status_code == 200
        d = r.json()
        assert "required_version" in d
        from services.local_exec import REQUIRED_WORKER_VERSION_STR
        assert d.get("required_version") == REQUIRED_WORKER_VERSION_STR, d
        assert isinstance(d.get("workers"), list)
        if d["workers"]:
            for w in d["workers"]:
                assert "outdated" in w
                assert "version" in w


# ---------- Cloud backtest + CSV export ----------
class TestCloudBacktestExport:
    def test_run_and_export(self, auth):
        payload = {
            "strategy_ids": [CUSTOM_STRAT],
            "symbols": ["BTCUSDT"],
            "days": 90,
            "timeframe": "5m",
            "execution": "cloud",
        }
        r = requests.post(f"{BASE_URL}/api/backtest/run", json=payload,
                          headers=auth, timeout=30)
        assert r.status_code == 200, r.text
        job_id = r.json().get("job_id") or r.json().get("id")
        assert job_id
        # Poll status
        deadline = time.time() + 240
        status = None
        result = None
        while time.time() < deadline:
            time.sleep(3)
            rs = requests.get(f"{BASE_URL}/api/backtest/status/{job_id}",
                              headers=auth, timeout=10)
            if rs.status_code == 200:
                jd = rs.json()
                status = jd.get("status")
                if status in ("done", "error", "failed"):
                    result = jd
                    break
        assert status == "done", f"Backtest not done: status={status} result={result}"
        # Verify trades > 0
        per_pair = (result or {}).get("result", {}).get("per_pair") or (result or {}).get("per_pair") or []
        # try alternate paths
        if not per_pair:
            per_pair = ((result or {}).get("result") or {}).get("pairs") or []
        total_trades = 0
        if per_pair:
            for p in per_pair:
                total_trades += int(p.get("trades") or p.get("trade_count") or 0)
        # Store job_id for exports
        # Candles export
        rc = requests.get(f"{BASE_URL}/api/backtest/export/{job_id}",
                         params={"kind": "candles"}, headers=auth, timeout=60)
        assert rc.status_code == 200, rc.text
        candles_csv = rc.text
        reader = csv.reader(io.StringIO(candles_csv))
        rows = list(reader)
        assert len(rows) >= 2, "Candles CSV has no data rows"
        header = rows[0]
        assert len(header) >= 5
        # Verify not all empty
        first_data = rows[1]
        assert any(c.strip() for c in first_data), "First candle row is all empty"
        # Trades export
        rt = requests.get(f"{BASE_URL}/api/backtest/export/{job_id}",
                        params={"kind": "trades"}, headers=auth, timeout=60)
        assert rt.status_code == 200, rt.text
        trades_csv = rt.text
        trows = list(csv.reader(io.StringIO(trades_csv)))
        assert len(trows) >= 1, "Trades CSV empty (not even header)"
        # If total_trades > 0, at least 2 rows (header + trade)
        if total_trades > 0:
            assert len(trows) >= 2


# ---------- Dynamic save with sub_strategies ----------
class TestDynamicSaveSubStrategies:
    def test_save_and_list(self, auth):
        body = {
            "strategy_id": CUSTOM_STRAT,
            "name": "TEST_iter15_subs",
            "model": {"regimes": [{"id": 0, "label": "trend_up"},
                                  {"id": 1, "label": "range"}],
                      "silhouette": 0.5, "lookback_days": 3.0},
            "configs": {"0": {"rsi_long_threshold": 30}},
            "sub_strategies": {"0": {"rules": [{"indicator": "rsi",
                                                "op": "<",
                                                "value": 25,
                                                "side": "long"}]}},
            "symbols": ["BTCUSDT"],
            "timeframe": "5m",
        }
        r = requests.post(f"{BASE_URL}/api/dynamic/save", json=body,
                          headers=auth, timeout=15)
        assert r.status_code == 200, r.text
        did = r.json()["id"]
        # list
        rl = requests.get(f"{BASE_URL}/api/dynamic/list", timeout=10)
        assert rl.status_code == 200
        found = None
        for s in rl.json()["strategies"]:
            if s["id"] == did:
                found = s
                break
        assert found is not None
        assert "sub_strategies" in found
        assert "0" in (found["sub_strategies"] or {})
        # cleanup
        rd = requests.delete(f"{BASE_URL}/api/dynamic/{did}", headers=auth, timeout=10)
        assert rd.status_code == 200


# ---------- Local backtest (worker present, .npy cache) ----------
class TestLocalBacktest:
    def test_local_365d(self, auth):
        payload = {
            "strategy_ids": [CUSTOM_STRAT],
            "symbols": ["BTCUSDT"],
            "days": 365,
            "timeframe": "5m",
            "execution": "local",
        }
        r = requests.post(f"{BASE_URL}/api/backtest/run", json=payload,
                          headers=auth, timeout=30)
        # Requires worker; if no worker returns 503
        if r.status_code == 503:
            pytest.skip("No local worker connected")
        assert r.status_code == 200, r.text
        job_id = r.json().get("job_id") or r.json().get("id")
        # Poll
        deadline = time.time() + 300
        status = None
        while time.time() < deadline:
            time.sleep(3)
            rs = requests.get(f"{BASE_URL}/api/backtest/status/{job_id}",
                              headers=auth, timeout=10)
            if rs.status_code == 200:
                status = rs.json().get("status")
                if status in ("done", "error", "failed"):
                    break
        assert status == "done", f"Local backtest not done: {status}"


# ---------- Dynamic optimizer per_regime_strategies ----------
class TestDynamicOptimizer:
    def _run_and_wait(self, auth, per_regime):
        payload = {
            "strategy_id": CUSTOM_STRAT,
            "symbols": ["BTCUSDT"],
            "days": 180,
            "timeframe": "5m",
            "mode": "dynamic",
            "execution": "cloud",
            "min_trades": 5,
            "iterations": 6,
            "indicators": ["rsi", "ema"],
            "dynamic": {
                "per_regime_strategies": per_regime,
                "max_rules_per_regime": 2,
                "start_from_base": True,
                "max_regimes": 3,
                "lookback_days": 3.0,
            },
        }
        r = requests.post(f"{BASE_URL}/api/optimizer/run", json=payload,
                          headers=auth, timeout=30)
        assert r.status_code == 200, r.text
        job_id = r.json().get("job_id") or r.json().get("id")
        # Poll (dynamic run can take long)
        deadline = time.time() + 600
        result = None
        status = None
        while time.time() < deadline:
            time.sleep(5)
            rs = requests.get(f"{BASE_URL}/api/optimizer/status/{job_id}",
                              headers=auth, timeout=15)
            if rs.status_code == 200:
                jd = rs.json()
                status = jd.get("status")
                if status in ("done", "error", "failed"):
                    result = jd
                    break
        assert status == "done", f"Optimizer not done: status={status}"
        return result

    def test_per_regime_true(self, auth):
        result = self._run_and_wait(auth, True)
        dyn = (result.get("result") or {}).get("dynamic") or result.get("dynamic")
        assert dyn is not None, f"No result.dynamic in {list(result.keys())}"
        assert dyn.get("per_regime_strategies") is True
        assert dyn.get("regime_walk_forward") is True
        regs = dyn.get("regimes") or []
        assert len(regs) >= 1
        for reg in regs:
            assert "metrics" in reg or "own_strategy" in reg
            assert "validation_passed" in reg
        assert "sub_strategies" in dyn
        assert "comparison" in dyn
        assert "verdict" in dyn

    def test_per_regime_false(self, auth):
        result = self._run_and_wait(auth, False)
        dyn = (result.get("result") or {}).get("dynamic") or result.get("dynamic")
        assert dyn is not None
        regs = dyn.get("regimes") or []
        assert len(regs) >= 1
        # own_strategy should be absent or empty
        for reg in regs:
            own = reg.get("own_strategy") or {}
            rules = own.get("rules") if isinstance(own, dict) else None
            assert not rules, f"Unexpected own_strategy.rules present: {rules}"
