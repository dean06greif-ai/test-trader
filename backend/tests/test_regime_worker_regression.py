"""Focused Cloud/local Regime-Lab regression coverage for worker support."""
import re
import time
from pathlib import Path

import pytest
import requests
from dotenv import dotenv_values

BASE_URL = dotenv_values("/app/frontend/.env").get("REACT_APP_BACKEND_URL", "").rstrip("/")
CREDENTIALS = Path("/app/memory/test_credentials.md").read_text(encoding="utf-8")
PASSWORD_MATCH = re.search(r"Passwort\s*`([^`]+)`", CREDENTIALS)
PASSWORD = PASSWORD_MATCH.group(1) if PASSWORD_MATCH else None


@pytest.fixture(scope="module")
def admin_client():
    if not BASE_URL:
        pytest.fail("REACT_APP_BACKEND_URL is missing")
    if not PASSWORD:
        pytest.skip("Admin password missing in test_credentials.md")
    session = requests.Session()
    session.headers.update({"Content-Type": "application/json"})
    response = session.post(
        f"{BASE_URL}/api/auth/login", json={"password": PASSWORD}, timeout=30
    )
    if response.status_code != 200:
        pytest.fail(f"Admin authentication failed: {response.status_code} {response.text}")
    token = response.json().get("token")
    if not isinstance(token, str) or not token:
        pytest.fail("Admin authentication response has no valid token")
    session.headers.update({"Authorization": f"Bearer {token}"})
    return session


def poll_job(client, job_id, timeout=240):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        response = client.get(f"{BASE_URL}/api/regime-lab/status/{job_id}", timeout=30)
        assert response.status_code == 200, response.text
        last = response.json()
        assert last.get("id") == job_id
        if last.get("status") != "running":
            return last
        time.sleep(2)
    pytest.fail(f"Cloud job {job_id} did not finish in {timeout}s; last={last}")


def start_cloud_with_conflict_tolerance(client, payload):
    """Retry 409 briefly because a real user may run the singleton job concurrently."""
    last = None
    for _ in range(4):
        last = client.post(f"{BASE_URL}/api/regime-lab/analyze", json=payload, timeout=30)
        if last.status_code != 409:
            return last
        time.sleep(5)
    pytest.skip(f"blocked by user activity: singleton Regime-Lab job stayed occupied: {last.text}")


class TestRegimeLabWorkerRegression:
    """Cloud persistence/cleanup, local dispatch response, and smoke endpoint regressions."""

    def test_01_regression_endpoints_and_worker_version(self, admin_client):
        checks = [
            ("/api/localworker/status", "required_version", str),
            ("/api/optimizer/active", "active", (dict, type(None))),
            ("/api/strategies", "strategies", list),
            ("/api/dynamic/list", "strategies", list),
        ]
        for endpoint, key, expected_type in checks:
            response = admin_client.get(f"{BASE_URL}{endpoint}", timeout=30)
            assert response.status_code == 200, f"{endpoint}: {response.text}"
            data = response.json()
            assert key in data, f"{endpoint}: missing {key}"
            assert isinstance(data[key], expected_type), f"{endpoint}: invalid {key}"
            if endpoint == "/api/localworker/status":
                assert len(str(data[key]).split(".")) == 3, data
                assert isinstance(data.get("online"), bool)
                assert isinstance(data.get("workers"), list)

    def test_02_cloud_analyze_without_execution_persists_and_deletes(self, admin_client):
        payload = {
            "symbols": ["BTCUSDT"],
            "timeframe": "15m",
            "days": 30,
            "scope": "combined",
            "max_regimes": 3,
            "train_pct": 75,
            "name": "QA-Cloud-Regression",
        }
        analysis_id = None
        try:
            response = start_cloud_with_conflict_tolerance(admin_client, payload)
            assert response.status_code == 200, response.text
            started = response.json()
            assert started.get("status") == "started"
            assert isinstance(started.get("job_id"), str) and started["job_id"]
            assert "execution" not in started, started

            completed = poll_job(admin_client, started["job_id"])
            assert completed.get("status") == "done", completed
            assert completed.get("progress") == 100
            analysis_id = completed.get("result", {}).get("analysis_id")
            assert isinstance(analysis_id, str) and analysis_id

            listed = admin_client.get(f"{BASE_URL}/api/regime-lab/list", timeout=30)
            assert listed.status_code == 200, listed.text
            analyses = listed.json().get("analyses")
            assert isinstance(analyses, list)
            saved = next((row for row in analyses if row.get("id") == analysis_id), None)
            assert saved is not None
            assert saved.get("name") == payload["name"]
            assert saved.get("symbols") == payload["symbols"]
            assert saved.get("timeframe") == payload["timeframe"]
            assert saved.get("days") == payload["days"]
            assert saved.get("scope") == payload["scope"]
        finally:
            if analysis_id:
                deleted = admin_client.delete(
                    f"{BASE_URL}/api/regime-lab/{analysis_id}", timeout=30
                )
                assert deleted.status_code == 200, deleted.text
                assert deleted.json().get("status") == "deleted"
                after = admin_client.get(f"{BASE_URL}/api/regime-lab/list", timeout=30)
                assert all(
                    row.get("id") != analysis_id
                    for row in after.json().get("analyses", [])
                )

    def test_03_local_analyze_never_returns_500(self, admin_client):
        payload = {
            "symbols": ["BTCUSDT"],
            "timeframe": "15m",
            "days": 30,
            "scope": "combined",
            "max_regimes": 3,
            "train_pct": 75,
            "name": "QA-Local-Dispatch",
            "execution": "local",
        }
        response = admin_client.post(
            f"{BASE_URL}/api/regime-lab/analyze", json=payload, timeout=30
        )
        print(f"local analyze response: {response.status_code} {response.text}")
        assert response.status_code != 500, response.text
        assert response.status_code in (200, 409, 503), response.text
        data = response.json()
        if response.status_code == 200:
            assert data.get("status") == "started"
            assert data.get("execution") == "local"
            job_id = data.get("job_id")
            assert isinstance(job_id, str) and job_id
            cancelled = admin_client.post(
                f"{BASE_URL}/api/regime-lab/cancel/{job_id}", timeout=30
            )
            assert cancelled.status_code == 200, cancelled.text
            assert cancelled.json().get("status") == "cancelling"
        elif response.status_code == 503:
            assert "Worker" in data.get("detail", ""), data
        else:
            detail = data.get("detail", "")
            assert "Worker" in detail or "läuft bereits" in detail, data
