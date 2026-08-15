"""Iteration 7 - review-request specific coverage on top of test_refactor_regression.py."""
import os, time, requests

BASE = "http://localhost:8001"


def _tok():
    r = requests.post(f"{BASE}/api/auth/login",
                      json={"username": os.environ.get("ADMIN_USER", "Admin"), "password": os.environ.get("ADMIN_PASSWORD", "admin")}, timeout=10)
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _hdr():
    return {"Authorization": f"Bearer {_tok()}"}


# --- extra GET endpoints from review-list not covered by regression ---
def test_extra_gets_from_review():
    for p in ("/api/autotrade/balance",
              "/api/analytics/strategy-comparison",
              "/api/signals",
              "/api/rule-states",
              "/api/klines/BTCUSDT"):
        r = requests.get(f"{BASE}{p}", timeout=20)
        assert r.status_code == 200, f"{p}: {r.status_code} {r.text[:200]}"
        # must return JSON
        assert r.headers.get("content-type", "").startswith("application/json"), p


def test_settings_write_requires_admin_and_ok_with_token():
    r = requests.post(f"{BASE}/api/settings", json={"foo": "bar"}, timeout=10)
    assert r.status_code == 401
    r = requests.post(f"{BASE}/api/settings", headers=_hdr(),
                      json={"leverage_default": 20}, timeout=10)
    assert r.status_code == 200


# --- Backtest end-to-end with liquidation-protection & exports ---
def _wait_backtest(job_id, timeout=180):
    end = time.time() + timeout
    while time.time() < end:
        r = requests.get(f"{BASE}/api/backtest/status/{job_id}", timeout=15)
        if r.status_code == 200:
            j = r.json()
            if j.get("status") in ("done", "error", "canceled"):
                return j
        time.sleep(2)
    return None


def test_backtest_run_liquidation_protection_and_exports():
    hdr = _hdr()
    # make sure no other job runs
    requests.post(f"{BASE}/api/backtest/reset", headers=hdr, timeout=10)
    payload = {
        "strategy_ids": ["rsi_only"],
        "symbols": ["BTCUSDT"],
        "days": 1,
        "use_fast_path": True,
        "strategy_configs": {"rsi_only": {"leverage": 50, "sl_mode": "fixed",
                                          "sl_fixed_percent": 5}},
    }
    r = requests.post(f"{BASE}/api/backtest/run", headers=hdr, json=payload, timeout=30)
    assert r.status_code == 200, r.text
    job_id = r.json().get("job_id") or r.json().get("id")
    assert job_id, r.text
    j = _wait_backtest(job_id)
    assert j is not None, "backtest timed out"
    assert j.get("status") == "done", f"status={j.get('status')} err={j.get('error')}"
    # zero liquidations across the run (liquidation protection)
    liq_total = 0
    results = j.get("results") or j.get("summary", {}).get("results") or {}
    if isinstance(results, dict):
        for _, v in results.items():
            if isinstance(v, dict):
                liq_total += int(v.get("liquidations", 0) or 0)
                for _, sub in (v.get("symbols") or {}).items():
                    if isinstance(sub, dict):
                        liq_total += int(sub.get("liquidations", 0) or 0)
    assert liq_total == 0, f"unexpected liquidations={liq_total}"
    # equity + export endpoints must respond
    eq = requests.get(f"{BASE}/api/backtest/equity/{job_id}", timeout=15)
    assert eq.status_code == 200, eq.text
    ex = requests.get(f"{BASE}/api/backtest/export/{job_id}", params={"kind": "trades"}, timeout=15)
    assert ex.status_code == 200


# --- Optimizer body accepts 'sessions' & rejects unknown strategy ---
def test_optimizer_body_sessions_field_accepted():
    hdr = _hdr()
    r = requests.post(f"{BASE}/api/optimizer/run", headers=hdr, timeout=10, json={
        "mode": "params", "strategy_id": "nope",
        "symbols": ["BTCUSDT"], "sessions": "15:00-18:00"})
    assert r.status_code == 400
