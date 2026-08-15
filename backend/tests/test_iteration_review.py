"""
Backend integration tests for current iteration review:
- Liquidity endpoints (levels/heatmap/context/live)
- Analytics clear/preview & scope=strategy
- Custom AI-generated strategies (alias normalization, DEFAULT_PARAMS)
- Optimizer/Backtester with strategy_warnings
- Regressions (health, coins, strategies, analytics, ai)
"""
import os
import time
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
ADMIN_USER = os.environ.get("ADMIN_USER", "Admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin")
TIMEOUT = 30


@pytest.fixture(scope="module")
def token():
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"username": ADMIN_USER, "password": ADMIN_PASSWORD},
        timeout=TIMEOUT,
    )
    assert r.status_code == 200, r.text
    return r.json()["token"]


@pytest.fixture(scope="module")
def auth_headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ---------------- Regression ----------------

def test_health():
    r = requests.get(f"{BASE_URL}/api/health", timeout=TIMEOUT)
    assert r.status_code == 200


def test_coins():
    r = requests.get(f"{BASE_URL}/api/coins", timeout=TIMEOUT)
    assert r.status_code == 200


def test_strategies_list():
    r = requests.get(f"{BASE_URL}/api/strategies", timeout=TIMEOUT)
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, (list, dict))


def test_ai_status():
    r = requests.get(f"{BASE_URL}/api/ai/status", timeout=TIMEOUT)
    assert r.status_code == 200


def test_ai_chat_history_fast():
    start = time.time()
    r = requests.get(f"{BASE_URL}/api/ai/chat/history?limit=100", timeout=TIMEOUT)
    elapsed = time.time() - start
    assert r.status_code == 200
    assert elapsed < 5.0, f"chat history too slow: {elapsed:.2f}s"


# ---------------- Liquidity ----------------

@pytest.mark.parametrize("symbol,interval", [("BTCUSDT", "15m"), ("ETHUSDT", "1h")])
def test_liquidity_levels(symbol, interval):
    r = requests.get(
        f"{BASE_URL}/api/liquidity/levels/{symbol}?interval={interval}", timeout=60
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert "price" in data
    assert "levels" in data and isinstance(data["levels"], list)
    if data["levels"]:
        lvl = data["levels"][0]
        for k in ("type", "side", "dist_pct", "strength"):
            assert k in lvl, f"missing {k} in level: {lvl}"
        assert 0 <= lvl["strength"] <= 100
    assert "volume_profile" in data
    vp = data["volume_profile"] or {}
    # Volume profile may be empty in edge cases, but keys should be present if populated
    if vp:
        for k in ("poc", "vah", "val"):
            assert k in vp


def test_liquidity_heatmap():
    r = requests.get(
        f"{BASE_URL}/api/liquidity/heatmap/BTCUSDT?bins=32", timeout=60
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert "bins" in data and isinstance(data["bins"], list)
    assert len(data["bins"]) > 0
    heats = [b.get("heat", 0) for b in data["bins"]]
    assert max(heats) == pytest.approx(1.0, abs=0.01), f"max heat={max(heats)}"
    for b in data["bins"]:
        assert "price" in b and "heat" in b
        assert 0 <= b["heat"] <= 1.0 + 1e-6
    assert "clusters" in data
    clusters = data["clusters"]
    assert "below_price" in clusters and "above_price" in clusters
    assert "orderbook_walls" in data
    # oi is optional but should exist as keys
    assert "oi_usd" in data or "oi_trend" in data


def test_liquidity_context():
    r = requests.get(f"{BASE_URL}/api/liquidity/context", timeout=60)
    assert r.status_code == 200, r.text
    data = r.json()
    assert isinstance(data, dict)


def test_liquidity_live():
    r = requests.get(f"{BASE_URL}/api/liquidity/live/BTCUSDT", timeout=30)
    assert r.status_code == 200, r.text


# ---------------- Analytics clear preview ----------------

def test_analytics_clear_preview_strategy():
    r = requests.post(
        f"{BASE_URL}/api/analytics/clear/preview",
        json={"range": "all", "scope": "strategy", "strategy_id": "scalping"},
        timeout=TIMEOUT,
    )
    assert r.status_code == 200, r.text
    data = r.json()
    for k in ("signals", "trades", "total", "symbols"):
        assert k in data, f"missing {k}: {data}"


def test_analytics_clear_preview_missing_strategy_id():
    r = requests.post(
        f"{BASE_URL}/api/analytics/clear/preview",
        json={"range": "all", "scope": "strategy"},
        timeout=TIMEOUT,
    )
    assert r.status_code == 400, r.text


def test_analytics_clear_preview_invalid_scope():
    r = requests.post(
        f"{BASE_URL}/api/analytics/clear/preview",
        json={"range": "all", "scope": "bogus"},
        timeout=TIMEOUT,
    )
    assert r.status_code == 400


def test_analytics_clear_preview_coin_missing_symbol():
    r = requests.post(
        f"{BASE_URL}/api/analytics/clear/preview",
        json={"range": "all", "scope": "coin"},
        timeout=TIMEOUT,
    )
    assert r.status_code == 400


def test_analytics_clear_requires_admin():
    r = requests.post(
        f"{BASE_URL}/api/analytics/clear",
        json={"range": "all", "scope": "strategy", "strategy_id": "scalping"},
        timeout=TIMEOUT,
    )
    assert r.status_code in (401, 403), r.text


def test_analytics_clear_scope_strategy(auth_headers):
    # Preview first
    p = requests.post(
        f"{BASE_URL}/api/analytics/clear/preview",
        json={"range": "all", "scope": "strategy", "strategy_id": "scalping"},
        timeout=TIMEOUT,
    )
    assert p.status_code == 200
    # Delete
    d = requests.post(
        f"{BASE_URL}/api/analytics/clear",
        headers=auth_headers,
        json={"range": "all", "scope": "strategy", "strategy_id": "scalping"},
        timeout=TIMEOUT,
    )
    assert d.status_code == 200, d.text
    # Verify preview -> 0
    p2 = requests.post(
        f"{BASE_URL}/api/analytics/clear/preview",
        json={"range": "all", "scope": "strategy", "strategy_id": "scalping"},
        timeout=TIMEOUT,
    )
    assert p2.status_code == 200
    assert p2.json().get("total", 0) == 0


# ---------------- Custom strategy + optimizer + backtest ----------------

QA_STRAT = {
    "id": "qa_ai_strat",
    "name": "QA KI Kandidat",
    "timeframe": "5m",
    "indicators": {"rsi_period": 14, "adx_period": 14},
    "long_rules": [
        {"indicator": "close", "op": "above", "value": "ema9"},
        {"indicator": "adx14", "op": ">", "value": 18},
    ],
    "short_rules": [
        {"indicator": "rsi", "op": "greater_than", "value": 65},
    ],
    "sl_percent": 1.5,
    "crv_target": 2.0,
}

QA_BAD = {
    "id": "qa_bad",
    "name": "QA Bad Indicator",
    "timeframe": "5m",
    "indicators": {},
    "long_rules": [{"indicator": "supertrend", "op": ">", "value": 1}],
    "short_rules": [],
    "sl_percent": 1.5,
    "crv_target": 2.0,
}


@pytest.fixture(scope="module")
def qa_strategies(auth_headers):
    # cleanup first (idempotent)
    for sid in ("qa_ai_strat", "qa_bad"):
        requests.delete(f"{BASE_URL}/api/strategies/custom/{sid}", headers=auth_headers, timeout=TIMEOUT)
    r1 = requests.post(f"{BASE_URL}/api/strategies/custom", headers=auth_headers, json=QA_STRAT, timeout=TIMEOUT)
    assert r1.status_code in (200, 201), r1.text
    r2 = requests.post(f"{BASE_URL}/api/strategies/custom", headers=auth_headers, json=QA_BAD, timeout=TIMEOUT)
    assert r2.status_code in (200, 201), r2.text
    yield
    for sid in ("qa_ai_strat", "qa_bad"):
        requests.delete(f"{BASE_URL}/api/strategies/custom/{sid}", headers=auth_headers, timeout=TIMEOUT)


def _find_strategy(strategies, sid):
    if isinstance(strategies, dict):
        if sid in strategies:
            return strategies[sid]
        strategies = strategies.get("strategies") or list(strategies.values())
    if isinstance(strategies, list):
        for s in strategies:
            if isinstance(s, dict) and s.get("id") == sid:
                return s
    return None


def test_custom_strategy_default_params_populated(qa_strategies):
    r = requests.get(f"{BASE_URL}/api/strategies", timeout=TIMEOUT)
    assert r.status_code == 200
    strat = _find_strategy(r.json(), "qa_ai_strat")
    assert strat is not None, "qa_ai_strat not found in /api/strategies"
    params = strat.get("params") or strat.get("DEFAULT_PARAMS") or {}
    assert params, f"qa_ai_strat params must not be empty: {strat}"


def _poll_job(job_url: str, max_wait=180):
    start = time.time()
    last = None
    while time.time() - start < max_wait:
        r = requests.get(job_url, timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        j = r.json()
        last = j
        status = j.get("status") or j.get("state")
        if status in ("done", "finished", "completed", "success", "error", "failed"):
            return j
        time.sleep(2)
    raise AssertionError(f"job did not finish in {max_wait}s. last={last}")


def test_optimizer_run_custom_strategy(qa_strategies, auth_headers):
    payload = {
        "mode": "params",
        "strategy_id": "qa_ai_strat",
        "symbols": ["BTCUSDT"],
        "days": 3,
        "iterations": 8,
        "objective": "pnl",
        "min_trades": 1,
        "timeframe": "5m",
    }
    r = requests.post(f"{BASE_URL}/api/optimizer/run", headers=auth_headers, json=payload, timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    job = r.json()
    job_id = job.get("id") or job.get("job_id")
    assert job_id, f"no job id in {job}"
    result = _poll_job(f"{BASE_URL}/api/optimizer/status/{job_id}", max_wait=240)
    status = result.get("status") or result.get("state")
    assert status not in ("error", "failed"), f"job failed: {result}"
    res = result.get("result") or {}
    baseline = res.get("baseline") or {}
    best = res.get("best") or {}
    assert baseline.get("params"), f"baseline.params empty: {baseline}"
    assert best.get("params"), f"best.params empty: {best}"
    keys = set(best["params"].keys())
    expected_any = {"rsi_period", "adx_period", "ema_fast_period", "long1_value", "short1_value"}
    assert keys & expected_any, f"best.params missing expected keys: {keys}"


def test_backtest_run_custom(qa_strategies, auth_headers):
    payload = {
        "strategy_ids": ["qa_ai_strat"],
        "symbols": ["BTCUSDT"],
        "days": 2,
        "timeframe": "5m",
    }
    r = requests.post(f"{BASE_URL}/api/backtest/run", headers=auth_headers, json=payload, timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    job = r.json()
    job_id = job.get("id") or job.get("job_id")
    assert job_id
    result = _poll_job(f"{BASE_URL}/api/backtest/status/{job_id}", max_wait=240)
    res = result.get("result") or {}
    assert "per_strategy" in res, f"missing per_strategy: {res}"


def test_backtest_strategy_warnings_unknown_indicator(qa_strategies, auth_headers):
    payload = {
        "strategy_ids": ["qa_bad"],
        "symbols": ["BTCUSDT"],
        "days": 2,
        "timeframe": "5m",
    }
    r = requests.post(f"{BASE_URL}/api/backtest/run", headers=auth_headers, json=payload, timeout=TIMEOUT)
    assert r.status_code == 200
    job_id = r.json().get("id") or r.json().get("job_id")
    result = _poll_job(f"{BASE_URL}/api/backtest/status/{job_id}", max_wait=240)
    res = result.get("result") or {}
    warnings = res.get("strategy_warnings") or {}
    # accept dict {sid: [warns]} or list of strings
    text = str(warnings).lower()
    assert "supertrend" in text, f"strategy_warnings missing 'supertrend': {warnings}"
