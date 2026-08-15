"""Etappe 5 public API and Tim Flossbach backtest end-to-end tests."""
import re
import time
from pathlib import Path

import pytest
import requests
from dotenv import dotenv_values

frontend_env = dotenv_values("/app/frontend/.env")
base_url = frontend_env.get("REACT_APP_BACKEND_URL")
if not base_url:
    raise RuntimeError("REACT_APP_BACKEND_URL is missing from /app/frontend/.env")
BASE_URL = base_url.rstrip("/")
API = f"{BASE_URL}/api"


@pytest.fixture(scope="module")
def api_client():
    session = requests.Session()
    session.headers.update({"Content-Type": "application/json"})
    yield session
    session.close()


@pytest.fixture(scope="module")
def test_credentials():
    path = Path("/app/memory/test_credentials.md")
    if not path.exists():
        pytest.skip("Missing /app/memory/test_credentials.md")
    content = path.read_text(encoding="utf-8")
    user_match = re.search(r"(?im)^\s*[-*]\s*Username:\s*`?([^`\s]+)", content)
    password_match = re.search(r"(?im)^\s*[-*]\s*Passwort:\s*`?([^`\s]+)", content)
    if not user_match or not password_match:
        pytest.skip("No admin username/password found in test_credentials.md")
    return {"username": user_match.group(1), "password": password_match.group(1)}


@pytest.fixture(scope="module")
def admin_client(test_credentials):
    login = requests.post(f"{API}/auth/login", json=test_credentials, timeout=20)
    if login.status_code != 200:
        pytest.fail(f"Admin authentication failed: {login.status_code} {login.text[:300]}")
    data = login.json()
    token = data.get("token")
    if not isinstance(token, str) or not token:
        pytest.fail(f"Admin login returned invalid token: {data}")
    session = requests.Session()
    session.headers.update({
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    })
    yield session
    session.close()


# Strategy seed contract and builder metadata for the Etappe 5 indicator package.
def test_flossbach_strategy_and_builder_options(api_client):
    strategies_response = api_client.get(f"{API}/strategies", timeout=30)
    assert strategies_response.status_code == 200, strategies_response.text
    strategy_payload = strategies_response.json()
    assert isinstance(strategy_payload.get("strategies"), list)
    strategy = next(
        (row for row in strategy_payload["strategies"] if row.get("id") == "tim_flossbach"),
        None,
    )
    assert strategy is not None
    assert strategy["name"] == "Tim Flossbach (Basis)"
    assert strategy["is_custom"] is True
    definition = strategy["definition"]
    assert definition["timeframe"] == "1h"
    assert len(definition["long_rules"]) == 4
    assert len(definition["short_rules"]) == 4
    assert definition["sl_mode"] == "structure"
    assert definition["crv_target"] == 2.0

    options_response = api_client.get(f"{API}/strategies/builder-options", timeout=30)
    assert options_response.status_code == 200, options_response.text
    options = options_response.json()
    required_indicators = {
        "market_structure", "bos_up", "liq_sweep_low", "channel_pos",
        "range_pos", "dist_ema200_pct", "fomc_today",
    }
    assert required_indicators <= set(options["indicators"])
    period_keys = {field["key"] for field in options["period_fields"]}
    assert {"struct_pivot_wing", "bos_window", "channel_period", "ema200_period"} <= period_keys
    assert options["indicator_meta"]["market_structure"]["label"] == \
        "Markt-Struktur (1=HH/HL, -1=LH/LL, 0=neutral)"
    assert options["indicator_meta"]["liq_sweep_low"]["label"] == \
        "Liquidity Grab unter Tief (1=bullisch)"
    assert options["indicator_meta"]["fomc_today"]["label"] == \
        "FOMC-Meeting-Tag (1=heute)"


# Full cloud backtest, single-job exclusion, polling, and result data assertions.
def test_flossbach_180_day_backtest_and_busy_exclusion(api_client, admin_client):
    active = api_client.get(f"{API}/backtest/active", timeout=30)
    assert active.status_code == 200, active.text
    assert active.json().get("active") is None, "A pre-existing backtest is still running"

    payload = {
        "strategy_ids": ["tim_flossbach"],
        "symbols": ["BTCUSDT"],
        "days": 180,
        "timeframe": "1h",
        "execution": "cloud",
    }
    start = admin_client.post(f"{API}/backtest/run", json=payload, timeout=30)
    assert start.status_code == 200, start.text
    started = start.json()
    assert started["status"] == "started"
    assert isinstance(started["job_id"], str) and started["job_id"]

    second = admin_client.post(f"{API}/backtest/run", json=payload, timeout=30)
    assert second.status_code == 409, second.text
    assert "Backtest" in second.json().get("detail", "")

    deadline = time.time() + 300
    last = None
    while time.time() < deadline:
        status = api_client.get(
            f"{API}/backtest/status/{started['job_id']}", timeout=30
        )
        assert status.status_code == 200, status.text
        last = status.json()
        if last.get("status") != "running":
            break
        time.sleep(3)
    assert last is not None
    assert last.get("status") == "done", last
    assert last.get("progress") == 100
    result = last["result"]
    assert result["days"] == 180
    assert result["strategy_timeframes"]["tim_flossbach"] == "1h"
    rows = result.get("per_strategy")
    assert isinstance(rows, list)
    row = next((item for item in rows if item.get("strategy_id") == "tim_flossbach"), None)
    assert row is not None
    assert row["strategy_name"] == "Tim Flossbach (Basis)"
    assert row["timeframe"] == "1h"
    assert isinstance(row["trades"], int) and row["trades"] > 0
    assert isinstance(row["win_rate"], (int, float))
    assert isinstance(row["pnl"], (int, float))
