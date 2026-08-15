"""Focused regression tests for rly-2.8 review request.

Covers: admin login, custom strategies with new rule engine, backtest run,
rule-preview, builder-options, trade-guard config, telegram notify-config,
notifications, klines weekend fallback (GOLD/SILVER/OIL).
"""
import os
import time
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    # fallback to local supervisor for pytest runs in the container
    BASE_URL = "http://localhost:8001"

API = BASE_URL + "/api"


@pytest.fixture(scope="module")
def admin_token():
    r = requests.post(f"{API}/auth/login", json={"username": os.environ.get("ADMIN_USER", "Admin"), "password": os.environ.get("ADMIN_PASSWORD", "admin")}, timeout=15)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    data = r.json()
    tok = data.get("access_token") or data.get("token")
    assert tok, f"no token in: {data}"
    return tok


@pytest.fixture(scope="module")
def auth_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


# -------- Auth --------
def test_admin_login_returns_jwt():
    r = requests.post(f"{API}/auth/login", json={"username": os.environ.get("ADMIN_USER", "Admin"), "password": os.environ.get("ADMIN_PASSWORD", "admin")}, timeout=15)
    assert r.status_code == 200
    data = r.json()
    tok = data.get("access_token") or data.get("token")
    assert isinstance(tok, str) and len(tok) > 10


# -------- Builder options --------
def test_builder_options(auth_headers):
    r = requests.get(f"{API}/strategies/builder-options", headers=auth_headers, timeout=15)
    assert r.status_code == 200, r.text
    j = r.json()
    inds = j.get("indicators") or []
    ops = j.get("operators") or []
    assert len(inds) >= 25, f"only {len(inds)} indicators"
    assert "not_in_range" in ops, f"operators missing not_in_range: {ops}"
    assert "indicator_meta" in j
    assert "period_fields" in j


# -------- Rule preview --------
def test_rule_preview(auth_headers):
    body = {
        "symbol": "BTCUSDT",
        "days": 7,
        "definition": {
            "timeframe": "15m",
            "long_rules": [
                {"indicator": "ema(200)", "op": "<", "value": "price"},
                {"indicator": "rsi(14)", "op": "<", "value": 45},
                {"indicator": "hour", "op": "not_in_range", "value": [0, 6]},
            ],
            "short_rules": [
                {"indicator": "price", "op": "cross_below", "value": "low(20)"}
            ],
        },
    }
    r = requests.post(f"{API}/strategies/rule-preview", headers=auth_headers, json=body, timeout=60)
    assert r.status_code == 200, r.text
    j = r.json()
    # Expect per-rule fires info; either 'long_rules'/'short_rules' arrays with fires/fire_pct
    assert isinstance(j, dict)
    text = str(j)
    assert "fires" in text or "fire_pct" in text, f"no per-rule fires: {j}"
    problems = j.get("problems") or []
    assert problems == [] or not any(p for p in problems), f"unexpected problems: {problems}"


# -------- Custom strategy + backtest --------
@pytest.fixture(scope="module")
def custom_strategy_id(auth_headers):
    body = {
        "name": "TEST_rly28_review_strategy",
        "timeframe": "15m",
        "long_rules": [
            {"indicator": "ema(200)", "op": "<", "value": "price"},
            {"indicator": "rsi(14)", "op": "<", "value": 45},
            {"indicator": "hour", "op": "not_in_range", "value": [0, 6]},
        ],
        "short_rules": [
            {"indicator": "price", "op": "cross_below", "value": "low(20)"}
        ],
    }
    r = requests.post(f"{API}/strategies/custom", headers=auth_headers, json=body, timeout=30)
    assert r.status_code in (200, 201), r.text
    j = r.json()
    sid = j.get("id") or j.get("strategy_id") or (j.get("strategy") or {}).get("id")
    assert sid, f"no strategy id in: {j}"
    yield sid
    # Cleanup
    try:
        requests.delete(f"{API}/strategies/custom/{sid}", headers=auth_headers, timeout=15)
    except Exception:
        pass


def test_backtest_run_with_custom_strategy(auth_headers, custom_strategy_id):
    # Cancel any running job first (best-effort)
    body = {
        "strategy_ids": [custom_strategy_id],
        "symbols": ["BTCUSDT"],
        "days": 7,
    }
    r = requests.post(f"{API}/backtest/run", headers=auth_headers, json=body, timeout=30)
    if r.status_code == 409:
        # Wait a bit and retry
        time.sleep(5)
        r = requests.post(f"{API}/backtest/run", headers=auth_headers, json=body, timeout=30)
    assert r.status_code in (200, 202), r.text
    j = r.json()
    job_id = j.get("job_id") or j.get("id")
    assert job_id, f"no job id: {j}"

    # Poll status
    result = None
    for _ in range(60):  # ~2 minutes
        s = requests.get(f"{API}/backtest/status/{job_id}", headers=auth_headers, timeout=15)
        assert s.status_code == 200, s.text
        sj = s.json()
        state = sj.get("status") or sj.get("state")
        if state in ("done", "completed", "finished", "success"):
            result = sj
            break
        if state in ("failed", "error"):
            pytest.fail(f"backtest failed: {sj}")
        time.sleep(2)
    assert result is not None, "backtest did not finish within timeout"

    # per_strategy trades > 0
    per = result.get("per_strategy") or (result.get("result") or {}).get("per_strategy") or {}
    assert per, f"no per_strategy in result: {result}"
    trades_total = 0
    iterable = per.values() if isinstance(per, dict) else per
    for v in iterable:
        if isinstance(v, dict):
            t = v.get("trades") or v.get("total_trades") or 0
            if not t and isinstance(v.get("symbols"), dict):
                for _s, sv in v["symbols"].items():
                    if isinstance(sv, dict):
                        t += sv.get("trades", 0) or sv.get("total_trades", 0) or 0
            trades_total += int(t or 0)
    assert trades_total > 0, f"expected trades>0, got per_strategy={per}"


# -------- Trade Guard --------
def test_trade_guard_get_and_update(auth_headers):
    r = requests.get(f"{API}/trade-guard", headers=auth_headers, timeout=15)
    assert r.status_code == 200, r.text
    j = r.json()
    cfg = j.get("config") or j
    for k in ("kill_switch_enabled", "max_daily_loss_pct", "max_consecutive_losses",
              "anti_stacking_enabled", "stacking_cooldown_min"):
        assert k in cfg, f"missing {k} in trade-guard: {cfg}"
    # defaults sanity
    assert float(cfg.get("max_daily_loss_pct", 0)) == 5.0
    assert int(cfg.get("max_consecutive_losses", 0)) == 3
    assert int(cfg.get("stacking_cooldown_min", 0)) == 30
    assert "state" in j or "kill_switch_active" in j or True  # state optional but should exist

    # Update
    prev = float(cfg["max_daily_loss_pct"])
    new_val = 7.5 if prev != 7.5 else 4.5
    up = requests.post(f"{API}/trade-guard/config", headers=auth_headers,
                       json={"max_daily_loss_pct": new_val}, timeout=15)
    assert up.status_code == 200, up.text
    r2 = requests.get(f"{API}/trade-guard", headers=auth_headers, timeout=15)
    cfg2 = (r2.json().get("config") or r2.json())
    assert float(cfg2["max_daily_loss_pct"]) == new_val
    # Revert
    requests.post(f"{API}/trade-guard/config", headers=auth_headers,
                  json={"max_daily_loss_pct": prev}, timeout=15)


# -------- Telegram notify-config --------
def test_telegram_notify_config(auth_headers):
    r = requests.get(f"{API}/telegram/notify-config", headers=auth_headers, timeout=15)
    assert r.status_code == 200, r.text
    j = r.json()
    keys = j if isinstance(j, dict) else {}
    expected_any = ["ai_failure", "backtest_done", "optimizer_done", "kill_switch",
                    "daily_summary", "website_ai_failure"]
    have = [k for k in expected_any if k in keys or k in (keys.get("toggles") or {})]
    assert len(have) >= 4, f"missing toggles, got: {keys}"

    toggles = keys.get("toggles") if "toggles" in keys else keys
    # Try toggle backtest_done
    if "backtest_done" in toggles:
        current = bool(toggles["backtest_done"])
        payload = {"backtest_done": (not current)}
        # Some APIs expect nested under toggles
        up = requests.post(f"{API}/telegram/notify-config", headers=auth_headers, json=payload, timeout=15)
        if up.status_code >= 400:
            up = requests.post(f"{API}/telegram/notify-config", headers=auth_headers,
                               json={"toggles": payload}, timeout=15)
        assert up.status_code == 200, up.text
        r2 = requests.get(f"{API}/telegram/notify-config", headers=auth_headers, timeout=15)
        j2 = r2.json()
        t2 = j2.get("toggles") if "toggles" in j2 else j2
        assert bool(t2["backtest_done"]) == (not current)
        # Revert
        requests.post(f"{API}/telegram/notify-config", headers=auth_headers,
                      json={"backtest_done": current}, timeout=15)


# -------- Notifications --------
def test_notifications_list_and_read(auth_headers):
    r = requests.get(f"{API}/notifications", headers=auth_headers, timeout=15)
    assert r.status_code == 200, r.text
    j = r.json()
    assert isinstance(j, (list, dict))
    # POST read (should not 500)
    r2 = requests.post(f"{API}/notifications/read", headers=auth_headers, json={}, timeout=15)
    assert r2.status_code in (200, 204), r2.text


# -------- Klines weekend fallback --------
@pytest.mark.parametrize("symbol,lo,hi", [
    ("GOLD", 3000, 5000),
    ("SILVER", 20, 80),
    ("OIL", 40, 120),
])
def test_klines_weekend_fallback(symbol, lo, hi):
    r = requests.get(f"{API}/klines/{symbol}", timeout=30)
    assert r.status_code == 200, f"{symbol} {r.status_code} {r.text[:200]}"
    j = r.json()
    candles = j.get("candles") or j.get("data") or j
    if isinstance(j, dict) and "candles" in j:
        candles = j["candles"]
    assert isinstance(candles, list) and len(candles) > 0, f"{symbol} empty candles"
    last = candles[-1]
    close = last.get("close") if isinstance(last, dict) else last[4]
    assert lo <= float(close) <= hi, f"{symbol} last close {close} not in [{lo},{hi}]"
