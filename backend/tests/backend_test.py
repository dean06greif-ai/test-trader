"""Etappe 1 Kombi detector public-API and end-to-end regression tests."""
import os
import time

import pytest
import requests
from dotenv import dotenv_values

frontend_env = dotenv_values("/app/frontend/.env")
base_url = os.environ.get("REACT_APP_BACKEND_URL") or frontend_env.get("REACT_APP_BACKEND_URL")
if not base_url:
    raise RuntimeError("REACT_APP_BACKEND_URL is missing")
BASE_URL = base_url.rstrip("/")
API = f"{BASE_URL}/api"


@pytest.fixture(scope="module")
def api_client():
    session = requests.Session()
    session.headers.update({"Content-Type": "application/json"})
    yield session
    session.close()


@pytest.fixture(scope="module")
def admin_client(api_client):
    # Credentials were explicitly supplied in the review request.
    response = api_client.post(
        f"{API}/auth/login",
        json={"username": os.environ.get("ADMIN_USER", "Admin"), "password": os.environ.get("ADMIN_PASSWORD", "admin")},
        timeout=20,
    )
    if response.status_code != 200:
        pytest.fail(f"Admin authentication failed: {response.status_code} {response.text[:300]}")
    data = response.json()
    token = data.get("token")
    if not isinstance(token, str) or not token:
        pytest.fail("Admin login response did not include a non-empty token")
    session = requests.Session()
    session.headers.update({
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    })
    yield session
    session.close()


def wait_for_free_slot(api_client, timeout=240):
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = api_client.get(f"{API}/regime-lab/active", timeout=20)
        assert response.status_code == 200, response.text
        active = response.json().get("active")
        if not active:
            return
        time.sleep(2)
    pytest.fail("Regime-Lab job slot remained occupied")


def poll_job(api_client, job_id, timeout=240):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        response = api_client.get(f"{API}/regime-lab/status/{job_id}", timeout=20)
        assert response.status_code == 200, response.text
        last = response.json()
        if last.get("status") != "running":
            assert last.get("status") == "done", last
            return last
        time.sleep(2)
    pytest.fail(f"Job {job_id} timed out; last status={last}")


# Engine defaults and metadata contract.
def test_kombi_defaults_and_meta(api_client):
    response = api_client.get(f"{API}/regime-lab/engine/defaults", timeout=20)
    assert response.status_code == 200
    data = response.json()
    assert data["engine"] == "v2"
    expected = {
        "kombi_ema_days": 14.0,
        "kombi_thr": 0.18,
        "kombi_slope_days": 5.0,
        "kombi_persist_days": 1.0,
        "kombi_dominance_days": 3.0,
        "kombi_pivot_accel": True,
    }
    for key, value in expected.items():
        assert data["config"][key] == value
    meta = {item["key"]: item for item in data["meta"]}
    for key in expected:
        assert meta[key]["group"] == "Kombi-Detektor (EMA + Umkehrpunkte)"
        assert meta[key]["detectors"] == ["kombi"]


# Authentication and request validation on calibration endpoint.
def test_kombi_calibrate_auth_and_symbols_validation(api_client, admin_client):
    unauth = api_client.post(
        f"{API}/regime-lab/kombi-calibrate",
        json={"symbols": ["BTCUSDT"]},
        timeout=20,
    )
    assert unauth.status_code in (401, 403)
    assert isinstance(unauth.json().get("detail"), str)

    invalid = admin_client.post(f"{API}/regime-lab/kombi-calibrate", json={}, timeout=20)
    assert invalid.status_code == 400
    assert "Coin" in invalid.json().get("detail", "")


# Auto-calibration grid search, result schema, and chosen Kombi configuration.
def test_kombi_calibrate_e2e(api_client, admin_client):
    wait_for_free_slot(api_client)
    payload = {
        "symbols": ["BTCUSDT"],
        "timeframe": "1h",
        "days": 180,
        "train_pct": 75,
        "thr_grid": [0.14, 0.18],
        "slope_grid": [3, 5],
    }
    start = admin_client.post(f"{API}/regime-lab/kombi-calibrate", json=payload, timeout=30)
    assert start.status_code == 200, start.text
    started = start.json()
    assert started["status"] == "started"
    assert isinstance(started["job_id"], str) and started["job_id"]

    job = poll_job(api_client, started["job_id"], timeout=240)
    result = job["result"]
    assert result["kind"] == "kombi_calibrate"
    assert result["combos"] == 4
    assert result["symbols"] == ["BTCUSDT"]
    assert result["timeframe"] == "1h"
    assert result["days"] == 180
    assert result["train_pct"] == 75.0
    assert len(result["rows"]) == 4
    required = {
        "thr", "slope_days", "avg_final_segment_days",
        "holdout_direction_pct", "in_target", "score",
    }
    for row in result["rows"]:
        assert required <= row.keys()
        assert row["thr"] in (0.14, 0.18)
        assert row["slope_days"] in (3.0, 5.0)
        assert isinstance(row["in_target"], bool)
        assert isinstance(row["score"], (int, float))
    assert result["best"] in result["rows"]
    assert result["best_config"]["detector"] == "kombi"
    assert result["best_config"]["kombi_thr"] == result["best"]["thr"]
    assert result["best_config"]["kombi_slope_days"] == result["best"]["slope_days"]


# Kombi, reactive, and EMA cloud analyses must finish and persist usable segments.
@pytest.mark.parametrize("detector", ["kombi", "reactive", "ema"])
def test_analyze_detector_regression_e2e(api_client, admin_client, detector):
    wait_for_free_slot(api_client)
    payload = {
        "name": f"TEST_Etappe1_{detector}",
        "symbols": ["BTCUSDT"],
        "timeframe": "1h",
        "days": 120,
        "scope": "combined",
        "engine": "v2",
        "train_pct": 75,
        "engine_config": {"detector": detector},
    }
    start = admin_client.post(f"{API}/regime-lab/analyze", json=payload, timeout=30)
    assert start.status_code == 200, start.text
    started = start.json()
    assert started["status"] == "started"
    job = poll_job(api_client, started["job_id"], timeout=240)
    result = job["result"]
    assert result["kind"] == "analysis"
    analysis_id = result["analysis_id"]
    assert isinstance(analysis_id, str) and analysis_id.startswith("ra_")

    listing = api_client.get(f"{API}/regime-lab/list", timeout=30)
    assert listing.status_code == 200
    listed = next((row for row in listing.json()["analyses"] if row["id"] == analysis_id), None)
    assert listed is not None
    assert listed["settings"]["engine_config"]["detector"] == detector

    detail_response = api_client.get(f"{API}/regime-lab/{analysis_id}", timeout=30)
    assert detail_response.status_code == 200
    analysis = detail_response.json()["analysis"]
    model = analysis["combined"]["model"]
    symbol_data = analysis["combined"]["per_symbol"]["BTCUSDT"]
    assert model["config"]["detector"] == detector
    assert len(symbol_data["segments"]) > 0
    assert all({"regime", "from_ts", "to_ts", "bars"} <= segment.keys()
               for segment in symbol_data["segments"])
    assert len(symbol_data["live_segments"]) > 0
    assert symbol_data["live_agreement"]["direction_pct"] is not None

    cleanup = admin_client.delete(f"{API}/regime-lab/{analysis_id}", timeout=30)
    assert cleanup.status_code == 200
    assert cleanup.json()["status"] == "deleted"
