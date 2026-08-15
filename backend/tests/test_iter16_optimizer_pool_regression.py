"""Iter 16 review: Regression tests after ProcessPool refactor in optimizer.

Focus:
- Optimizer modes params / discovery / combo still work + benchmark fields
- Dynamic optimizer with per_regime_strategies True/False still works and
  benchmark contains dyn_segments > 0
- Cloud backtest (multi-pair) still works + CSV exports (candles/trades)
- Dynamic current-regime endpoint
- Local worker status endpoint returns required_version=1.4.0
"""
import io
import csv
import os
import time
import pytest
import requests


def _load_backend_url():
    v = os.environ.get("REACT_APP_BACKEND_URL")
    if not v:
        try:
            for line in open("/app/frontend/.env"):
                if line.startswith("REACT_APP_BACKEND_URL="):
                    v = line.split("=", 1)[1].strip()
                    break
        except Exception:
            pass
    assert v, "REACT_APP_BACKEND_URL not set"
    return v.rstrip("/")


BASE_URL = _load_backend_url()


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
def auth():
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={"username": os.environ.get("ADMIN_USER", "Admin"), "password": os.environ.get("ADMIN_PASSWORD", "admin")}, timeout=15)
    assert r.status_code == 200, r.text
    tok = r.json()["token"]
    return {"Authorization": f"Bearer {tok}"}


def _poll_opt(job_id, headers, timeout=600, interval=4):
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        r = requests.get(f"{BASE_URL}/api/optimizer/status/{job_id}",
                         headers=headers, timeout=15)
        if r.status_code == 200:
            j = r.json()
            last = j
            if j.get("status") in ("done", "error", "canceled", "failed"):
                return j
        time.sleep(interval)
    raise AssertionError(f"Timeout for opt {job_id}. Last: {last}")


def _poll_bt(job_id, headers, timeout=600, interval=4):
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        r = requests.get(f"{BASE_URL}/api/backtest/status/{job_id}",
                         headers=headers, timeout=15)
        if r.status_code == 200:
            j = r.json()
            last = j
            if j.get("status") in ("done", "error", "canceled", "failed"):
                return j
        time.sleep(interval)
    raise AssertionError(f"Timeout for bt {job_id}. Last: {last}")


def _reset(headers):
    try:
        requests.post(f"{BASE_URL}/api/optimizer/reset",
                      headers=headers, timeout=10)
    except Exception:
        pass


# ------------------------------------------------------------------
# Local worker status regression
# ------------------------------------------------------------------
class TestLocalWorkerStatus:
    def test_required_version(self, auth):
        r = requests.get(f"{BASE_URL}/api/localworker/status",
                         headers=auth, timeout=10)
        assert r.status_code == 200, r.text
        d = r.json()
        from services.local_exec import REQUIRED_WORKER_VERSION_STR
        assert d.get("required_version") == REQUIRED_WORKER_VERSION_STR, d


# ------------------------------------------------------------------
# Dynamic current-regime endpoint
# ------------------------------------------------------------------
class TestCurrentRegime:
    def test_btc_60d(self, auth):
        r = requests.get(
            f"{BASE_URL}/api/dynamic/current-regime",
            params={"symbol": "BTCUSDT", "timeframe": "5m", "days": 60},
            headers=auth, timeout=120,
        )
        assert r.status_code == 200, r.text
        d = r.json()
        assert "current" in d
        assert isinstance(d.get("regimes"), list) and len(d["regimes"]) >= 1
        assert isinstance(d.get("timeline"), list) and len(d["timeline"]) >= 1


# ------------------------------------------------------------------
# Optimizer mode=params (cloud) regression
# ------------------------------------------------------------------
class TestOptimizerParams:
    def test_params_run(self, auth):
        _reset(auth)
        body = {
            "mode": "params",
            "execution": "cloud",
            "strategy_id": CUSTOM_STRAT,
            "symbols": ["BTCUSDT"],
            "days": 90,
            "timeframe": "5m",
            "iterations": 20,
            "min_trades": 3,
        }
        r = requests.post(f"{BASE_URL}/api/optimizer/run", json=body,
                          headers=auth, timeout=30)
        assert r.status_code == 200, r.text
        jid = r.json()["job_id"]
        job = _poll_opt(jid, auth, timeout=420)
        assert job["status"] == "done", f"status={job.get('status')} err={job.get('error')}"
        result = job.get("result") or {}
        bm = result.get("benchmark") or {}
        assert bm.get("execution") == "cloud", bm
        assert (bm.get("evaluations") or 0) > 0, bm


# ------------------------------------------------------------------
# Optimizer mode=discovery + mode=combo (cloud) regression
# ------------------------------------------------------------------
class TestOptimizerDiscoveryCombo:
    def _run(self, auth, mode):
        _reset(auth)
        body = {
            "mode": mode,
            "execution": "cloud",
            "symbols": ["BTCUSDT"],
            "days": 90,
            "timeframe": "5m",
            "iterations": 6,
            "min_trades": 3,
            "max_rules": 2,
            "indicators": ["rsi", "ema_fast"],
        }
        r = requests.post(f"{BASE_URL}/api/optimizer/run", json=body,
                          headers=auth, timeout=30)
        assert r.status_code == 200, r.text
        jid = r.json()["job_id"]
        job = _poll_opt(jid, auth, timeout=480)
        assert job["status"] == "done", f"mode={mode} status={job.get('status')} err={job.get('error')}"
        result = job.get("result") or {}
        # top5 or candidates
        top5 = result.get("top5") or []
        assert isinstance(top5, list), f"mode={mode} top5={top5}"
        bm = result.get("benchmark") or {}
        assert bm.get("execution") == "cloud"
        return result

    def test_discovery(self, auth):
        res = self._run(auth, "discovery")
        assert (res.get("benchmark") or {}).get("evaluations", 0) > 0

    def test_combo(self, auth):
        res = self._run(auth, "combo")
        assert (res.get("benchmark") or {}).get("evaluations", 0) > 0


# ------------------------------------------------------------------
# Dynamic optimizer per_regime_strategies True + False
# ------------------------------------------------------------------
class TestOptimizerDynamic:
    def _run(self, auth, per_regime):
        _reset(auth)
        body = {
            "mode": "dynamic",
            "execution": "cloud",
            "strategy_id": CUSTOM_STRAT,
            "symbols": ["BTCUSDT"],
            "days": 180,
            "timeframe": "5m",
            "iterations": 10,
            "min_trades": 20,
            "indicators": ["rsi", "ema", "atr"],
            "dynamic": {
                "per_regime_strategies": per_regime,
                "max_rules_per_regime": 2,
                "start_from_base": True,
                "max_regimes": 3,
                "lookback_days": 3.0,
            },
        }
        r = requests.post(f"{BASE_URL}/api/optimizer/run", json=body,
                          headers=auth, timeout=30)
        assert r.status_code == 200, r.text
        jid = r.json()["job_id"]
        job = _poll_opt(jid, auth, timeout=900)
        assert job["status"] == "done", f"per_regime={per_regime} status={job.get('status')} err={job.get('error')}"
        return job.get("result") or {}

    def test_per_regime_true(self, auth):
        result = self._run(auth, True)
        dyn = result.get("dynamic") or {}
        assert dyn.get("per_regime_strategies") is True
        assert dyn.get("regime_walk_forward") is True
        regs = dyn.get("regimes") or []
        assert len(regs) >= 1
        for reg in regs:
            assert ("metrics" in reg) or ("own_strategy" in reg)
            assert "validation_passed" in reg
        assert "sub_strategies" in dyn
        bm = result.get("benchmark") or {}
        assert (bm.get("evaluations") or 0) > 0, bm
        assert (bm.get("dyn_segments") or 0) > 0, bm

    def test_per_regime_false(self, auth):
        result = self._run(auth, False)
        dyn = result.get("dynamic") or {}
        regs = dyn.get("regimes") or []
        assert len(regs) >= 1
        bm = result.get("benchmark") or {}
        assert (bm.get("dyn_segments") or 0) > 0, bm


# ------------------------------------------------------------------
# Cloud backtest with 2 pairs + CSV exports
# ------------------------------------------------------------------
class TestBacktestCloudExports:
    job_id = None

    def test_run(self, auth):
        body = {
            "strategy_ids": [CUSTOM_STRAT],
            "symbols": ["BTCUSDT", "ETHUSDT"],
            "days": 90,
            "timeframe": "5m",
            "execution": "cloud",
        }
        r = requests.post(f"{BASE_URL}/api/backtest/run", json=body,
                          headers=auth, timeout=30)
        assert r.status_code == 200, r.text
        TestBacktestCloudExports.job_id = r.json()["job_id"]
        job = _poll_bt(TestBacktestCloudExports.job_id, auth, timeout=600)
        assert job["status"] == "done", f"status={job.get('status')} err={job.get('error')}"
        result = job.get("result") or {}
        per_pair = result.get("per_pair") or []
        # per_pair may be dict or list depending on API version
        if isinstance(per_pair, dict):
            entries = list(per_pair.values())
        else:
            entries = list(per_pair)
        trades_total = 0
        for v in entries:
            v = v or {}
            m = v.get("metrics") if isinstance(v, dict) else None
            trades_total += int((m or v).get("trades", 0) if isinstance(v, dict) else 0)
        assert trades_total > 0, f"per_pair trades all 0: {per_pair}"

    def test_export_candles(self, auth):
        jid = TestBacktestCloudExports.job_id
        assert jid
        r = requests.get(f"{BASE_URL}/api/backtest/export/{jid}",
                         params={"kind": "candles"}, headers=auth, timeout=60)
        assert r.status_code == 200, r.text
        assert len(r.content) > 100
        # Parse CSV headers
        text = r.content.decode("utf-8", errors="replace")
        rows = list(csv.reader(io.StringIO(text)))
        assert len(rows) >= 2, "candles csv should have header + rows"

    def test_export_trades(self, auth):
        jid = TestBacktestCloudExports.job_id
        assert jid
        r = requests.get(f"{BASE_URL}/api/backtest/export/{jid}",
                         params={"kind": "trades"}, headers=auth, timeout=60)
        assert r.status_code == 200, r.text
        text = r.content.decode("utf-8", errors="replace")
        rows = list(csv.reader(io.StringIO(text)))
        assert len(rows) >= 2, "trades csv should have header + rows"
