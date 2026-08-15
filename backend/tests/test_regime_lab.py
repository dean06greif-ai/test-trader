"""Regime-Lab API workflow and regression coverage."""
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
STATE = {"analysis_id": None, "dynamic_id": None, "base_strategy_id": None, "analysis_job": None, "opt_job": None, "wf_job": None}


def poll_job(client, job_id, timeout=330):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        response = client.get(f"{BASE_URL}/api/regime-lab/status/{job_id}", timeout=30)
        assert response.status_code == 200, response.text
        last = response.json()
        if last.get("status") != "running":
            return last
        time.sleep(2)
    pytest.fail(f"Job {job_id} timed out after {timeout}s; last={last}")


@pytest.fixture(scope="module")
def api_client():
    assert BASE_URL, "REACT_APP_BACKEND_URL missing"
    session = requests.Session()
    session.headers.update({"Content-Type": "application/json"})
    yield session
    if PASSWORD:
        login = session.post(f"{BASE_URL}/api/auth/login", json={"password": PASSWORD}, timeout=30)
        if login.ok:
            session.headers.update({"Authorization": f"Bearer {login.json()['token']}"})
            if STATE.get("dynamic_id"):
                session.delete(f"{BASE_URL}/api/dynamic/{STATE['dynamic_id']}", timeout=30)
            if STATE.get("base_strategy_id"):
                session.delete(f"{BASE_URL}/api/strategies/custom/{STATE['base_strategy_id']}", timeout=30)
            if STATE.get("analysis_id"):
                session.delete(f"{BASE_URL}/api/regime-lab/{STATE['analysis_id']}", timeout=30)


@pytest.fixture(scope="module")
def admin_client(api_client):
    if not PASSWORD:
        pytest.skip("Admin password missing in test_credentials.md")
    response = api_client.post(f"{BASE_URL}/api/auth/login", json={"password": PASSWORD}, timeout=30)
    assert response.status_code == 200, response.text
    data = response.json()
    assert isinstance(data.get("token"), str) and data["token"]
    api_client.headers.update({"Authorization": f"Bearer {data['token']}"})
    return api_client


class TestRegimeLabWorkflow:
    """Analyze → optimize → assign → walk-forward → build, plus regressions."""

    def test_01_analyze_requires_admin(self, api_client):
        response = api_client.post(f"{BASE_URL}/api/regime-lab/analyze", json={
            "symbols": ["BTCUSDT"], "timeframe": "15m", "days": 60,
        }, timeout=30)
        assert response.status_code in (401, 403), response.text
        data = response.json()
        assert data.get("detail")

    def test_02_regression_endpoints(self, api_client):
        checks = [
            ("/api/regime-lab/list", "analyses", list),
            ("/api/regime-lab/active", "active", (dict, type(None))),
            ("/api/strategies", "strategies", list),
            ("/api/dynamic/list", "strategies", list),
        ]
        for endpoint, key, expected_type in checks:
            response = api_client.get(f"{BASE_URL}{endpoint}", timeout=30)
            assert response.status_code == 200, f"{endpoint}: {response.text}"
            data = response.json()
            assert key in data, f"{endpoint}: missing {key}"
            assert isinstance(data[key], expected_type), f"{endpoint}: invalid {key} type"

    def test_03_seeded_analysis_schema_labels_and_persistence(self, api_client):
        response = api_client.get(f"{BASE_URL}/api/regime-lab/list", timeout=30)
        assert response.status_code == 200, response.text
        rows = response.json().get("analyses")
        assert isinstance(rows, list) and rows
        seeded = next((row for row in rows if row.get("id") == "ra_c8206904"), None)
        if not seeded:
            pytest.skip("Seed-Analyse ra_c8206904 nicht (mehr) in der DB vorhanden")

        first_response = api_client.get(f"{BASE_URL}/api/regime-lab/{seeded['id']}", timeout=30)
        assert first_response.status_code == 200, first_response.text
        first_analysis = first_response.json()["analysis"]
        self._validate_analysis(first_analysis)
        regime_zero = next(r for r in first_analysis["combined"]["model"]["regimes"] if r["id"] == 0)
        assert regime_zero["stats"]["trend_strength"] >= 1.0
        assert regime_zero["label"] == "Abwärtstrend · hohe Volatilität", regime_zero

        # A second API read verifies that the GET migration was persisted, not merely response-local.
        second_response = api_client.get(f"{BASE_URL}/api/regime-lab/{seeded['id']}", timeout=30)
        assert second_response.status_code == 200, second_response.text
        second_analysis = second_response.json()["analysis"]
        second_regime_zero = next(
            r for r in second_analysis["combined"]["model"]["regimes"] if r["id"] == 0
        )
        assert second_regime_zero["label"] == regime_zero["label"]
        self._validate_analysis(second_analysis)

    def test_04_keep_toggle_and_restore(self, admin_client):
        aid = "ra_c8206904"
        detail = admin_client.get(f"{BASE_URL}/api/regime-lab/{aid}", timeout=30)
        if detail.status_code == 404:
            pytest.skip("Seeded analysis ra_c8206904 is unavailable")
        for keep in (False, True):
            response = admin_client.post(f"{BASE_URL}/api/regime-lab/{aid}/keep", json={
                "scope": "combined", "regime_id": 0, "keep": keep,
            }, timeout=30)
            assert response.status_code == 200, response.text
            assert response.json()["kept"]["combined:0"] is keep
            saved = admin_client.get(f"{BASE_URL}/api/regime-lab/{aid}", timeout=30).json()["analysis"]
            assert saved["kept"]["combined:0"] is keep

    def test_04a_mutations_reject_missing_regime_id(self, admin_client):
        cases = [
            ("keep", {"scope": "combined", "keep": False}),
            ("assign", {"scope": "combined", "candidate": {}}),
            ("optimize", {"scope": "combined", "mode": "combo"}),
        ]
        for endpoint, payload in cases:
            response = admin_client.post(
                f"{BASE_URL}/api/regime-lab/ra_c8206904/{endpoint}",
                json=payload, timeout=30,
            )
            assert response.status_code == 400, f"{endpoint}: {response.text}"
            assert "regime_id" in response.json().get("detail", ""), response.text

    def test_04b_discarded_regime_is_excluded_from_build_and_optimize(self, admin_client):
        aid = "ra_c8206904"
        dynamic_id = None
        strategy_id = None
        try:
            discarded = admin_client.post(f"{BASE_URL}/api/regime-lab/{aid}/keep", json={
                "scope": "combined", "regime_id": 0, "keep": False,
            }, timeout=30)
            assert discarded.status_code == 200, discarded.text
            assert discarded.json()["kept"]["combined:0"] is False

            rejected_optimize = admin_client.post(
                f"{BASE_URL}/api/regime-lab/{aid}/optimize",
                json={"scope": "combined", "regime_id": 0, "mode": "combo"}, timeout=30,
            )
            assert rejected_optimize.status_code == 400, rejected_optimize.text
            assert "verworfen" in rejected_optimize.json().get("detail", "")

            built = admin_client.post(f"{BASE_URL}/api/regime-lab/{aid}/build", json={
                "scope": "combined", "name": "QA-Kept-Test",
            }, timeout=30)
            assert built.status_code == 200, built.text
            data = built.json()
            dynamic_id = data.get("id")
            strategy_id = data.get("strategy_id")
            assert data.get("regimes") == [1], data

            listed = admin_client.get(f"{BASE_URL}/api/dynamic/list", timeout=30)
            assert listed.status_code == 200, listed.text
            created = next((row for row in listed.json().get("strategies", [])
                            if row.get("id") == dynamic_id), None)
            assert created and created.get("name") == "QA-Kept-Test"
            assert set(created.get("configs", {}).keys()) == {"1"}, created.get("configs")
        finally:
            restored = admin_client.post(f"{BASE_URL}/api/regime-lab/{aid}/keep", json={
                "scope": "combined", "regime_id": 0, "keep": True,
            }, timeout=30)
            assert restored.status_code == 200, restored.text
            assert restored.json()["kept"]["combined:0"] is True
            if dynamic_id:
                deleted_dynamic = admin_client.delete(
                    f"{BASE_URL}/api/dynamic/{dynamic_id}", timeout=30
                )
                assert deleted_dynamic.status_code in (200, 204, 404), deleted_dynamic.text
            if strategy_id and strategy_id.startswith("custom_") and strategy_id != "custom_dd633f11":
                deleted_strategy = admin_client.delete(
                    f"{BASE_URL}/api/strategies/custom/{strategy_id}", timeout=30
                )
                assert deleted_strategy.status_code in (200, 204, 404), deleted_strategy.text


    def test_05_analyze_and_running_job_guard(self, admin_client):
        active = admin_client.get(f"{BASE_URL}/api/regime-lab/active", timeout=30).json().get("active")
        assert active is None, f"Pre-existing Regime-Lab job prevents test: {active}"
        payload = {
            "symbols": ["BTCUSDT", "ETHUSDT"], "timeframe": "15m", "days": 60,
            "scope": "both", "max_regimes": 4, "train_pct": 75,
            "name": "TEST_Regime_Lab_QA",
        }
        started = admin_client.post(f"{BASE_URL}/api/regime-lab/analyze", json=payload, timeout=30)
        assert started.status_code == 200, started.text
        data = started.json()
        assert data.get("status") == "started" and data.get("job_id")
        STATE["analysis_job"] = data["job_id"]

        conflict = admin_client.post(f"{BASE_URL}/api/regime-lab/analyze", json=payload, timeout=30)
        assert conflict.status_code == 409, conflict.text
        assert "läuft bereits" in conflict.json().get("detail", "")

        completed = poll_job(admin_client, data["job_id"], timeout=180)
        assert completed.get("status") == "done", completed
        assert completed.get("progress") == 100
        STATE["analysis_id"] = completed.get("result", {}).get("analysis_id")
        assert STATE["analysis_id"]

    def test_06_created_analysis_persistence_and_schema(self, admin_client):
        aid = STATE["analysis_id"]
        assert aid
        listed = admin_client.get(f"{BASE_URL}/api/regime-lab/list", timeout=30).json()["analyses"]
        row = next((r for r in listed if r.get("id") == aid), None)
        assert row and row["name"] == "TEST_Regime_Lab_QA"
        response = admin_client.get(f"{BASE_URL}/api/regime-lab/{aid}", timeout=30)
        assert response.status_code == 200, response.text
        analysis = response.json()["analysis"]
        self._validate_analysis(analysis)
        for symbol in ("BTCUSDT", "ETHUSDT"):
            assert analysis["bounds"][symbol]["train_end_ts"]
            assert analysis["bounds"][symbol]["train_end_ts"] < analysis["bounds"][symbol]["end_ts"]

    def test_07_optimize_and_running_job_guard(self, admin_client):
        aid = STATE["analysis_id"]
        payload = {
            "scope": "combined", "regime_id": 1, "mode": "combo",
            "indicators": ["ema_slow", "rel_volume", "macd"], "iterations": 5,
            "min_trades": 5, "max_rules": 2, "optimize": {"tpsl": True},
            "regime_walk_forward": True,
        }
        started = admin_client.post(f"{BASE_URL}/api/regime-lab/{aid}/optimize", json=payload, timeout=30)
        assert started.status_code == 200, started.text
        data = started.json()
        assert data.get("status") == "started" and data.get("job_id")
        STATE["opt_job"] = data["job_id"]

        conflict = admin_client.post(f"{BASE_URL}/api/regime-lab/{aid}/optimize", json=payload, timeout=30)
        assert conflict.status_code == 409, conflict.text
        completed = poll_job(admin_client, data["job_id"], timeout=330)
        assert completed.get("status") == "done", completed
        assert completed.get("result", {}).get("top5"), completed

    def test_08_run_result_and_assign_two_regimes(self, admin_client):
        response = admin_client.get(f"{BASE_URL}/api/regime-lab/run/{STATE['opt_job']}", timeout=30)
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        assert result["regime_id"] == 1
        assert result["mode"] == "combo"
        assert isinstance(result.get("discovery", {}).get("rules", []), list)
        top = result["top5"][0]
        assert isinstance(top.get("trade_params"), dict)
        assert isinstance(top.get("metrics"), dict)
        assert "trades" in top["metrics"] and "pnl" in top["metrics"]
        assert "validation" in top
        candidate = {
            "mode": result["mode"], "strategy_id": result.get("strategy_id"),
            "strategy_name": result.get("strategy_name"), "definition": result.get("definition"),
            "rules": result.get("discovery", {}).get("rules", []),
            "trade_params": top["trade_params"], "metrics": top["metrics"],
            "validation": top.get("validation"), "source_job_id": STATE["opt_job"],
        }
        for regime_id in (1, 0):
            assigned = admin_client.post(
                f"{BASE_URL}/api/regime-lab/{STATE['analysis_id']}/assign",
                json={"scope": "combined", "regime_id": regime_id, "candidate": candidate}, timeout=30,
            )
            assert assigned.status_code == 200, assigned.text
            assert f"combined:{regime_id}" in assigned.json()["assignments"]
        saved = admin_client.get(
            f"{BASE_URL}/api/regime-lab/{STATE['analysis_id']}", timeout=30
        ).json()["analysis"]
        assert all(f"combined:{rid}" in saved["assignments"] for rid in (0, 1))

    def test_09_walkforward_result_and_persistence(self, admin_client):
        started = admin_client.post(
            f"{BASE_URL}/api/regime-lab/{STATE['analysis_id']}/walkforward",
            json={"scope": "combined"}, timeout=30,
        )
        assert started.status_code == 200, started.text
        STATE["wf_job"] = started.json()["job_id"]
        completed = poll_job(admin_client, STATE["wf_job"], timeout=240)
        assert completed.get("status") == "done", completed
        result = completed["result"]
        assert isinstance(result.get("dynamic_test"), dict)
        assert "pnl" in result["dynamic_test"] and "trades" in result["dynamic_test"]
        assert isinstance(result.get("best_single"), dict)
        assert isinstance(result.get("verdict"), dict)
        assert isinstance(result.get("per_regime"), list)
        assert isinstance(result.get("points"), list)
        persisted = admin_client.get(
            f"{BASE_URL}/api/regime-lab/{STATE['analysis_id']}", timeout=30
        ).json()["analysis"]
        assert "combined" in persisted.get("walkforward", {})

    def test_10_build_dynamic_and_verify_list(self, admin_client):
        response = admin_client.post(
            f"{BASE_URL}/api/regime-lab/{STATE['analysis_id']}/build",
            json={"scope": "combined", "name": "TEST_Regime_Lab_Dynamic_QA"}, timeout=30,
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data.get("status") == "success"
        STATE["dynamic_id"] = data.get("id")
        STATE["base_strategy_id"] = data.get("strategy_id")
        assert STATE["dynamic_id"] and isinstance(data.get("regimes"), list)
        listed = admin_client.get(f"{BASE_URL}/api/dynamic/list", timeout=30)
        assert listed.status_code == 200, listed.text
        created = next((d for d in listed.json()["strategies"] if d.get("id") == STATE["dynamic_id"]), None)
        assert created and created["name"] == "TEST_Regime_Lab_Dynamic_QA"
        assert created["settings"]["source"] == "regime_lab"
        assert created["settings"]["analysis_id"] == STATE["analysis_id"]

    @staticmethod
    def _validate_analysis(analysis):
        combined_model = analysis.get("combined", {}).get("model", {})
        assert combined_model.get("regimes")
        models = [("combined", combined_model)]
        models.extend(
            (f"per_coin:{symbol}", payload.get("model", {}))
            for symbol, payload in analysis.get("per_coin", {}).items()
        )
        for model_name, model in models:
            assert model.get("regimes"), model_name
            for regime in model["regimes"]:
                label = regime.get("label", "")
                strength = regime.get("stats", {}).get("trend_strength")
                assert strength is not None, {"model": model_name, "regime": regime}
                if strength >= 1.0:
                    assert "Seitwärtsmarkt" not in label, {
                        "model": model_name, "regime": regime,
                    }
                if strength < 0.5:
                    assert "Seitwärtsmarkt" in label, {
                        "model": model_name, "regime": regime,
                    }
        for symbol in analysis.get("symbols", []):
            assert analysis["combined"]["per_symbol"][symbol]["segments"]
            assert analysis["per_coin"][symbol]["model"]["regimes"]
            assert analysis["per_coin"][symbol]["segments"]
            assert analysis["chart"][symbol]
        assert isinstance(analysis["combined"].get("coin_similarity"), list)
