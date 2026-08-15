"""Phase 5 ML Gate API tests - shadow mode, training, settings, dataset, report."""
import os
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    # fallback for local runs
    with open("/app/frontend/.env") as f:
        for line in f:
            if line.startswith("REACT_APP_BACKEND_URL="):
                BASE_URL = line.split("=", 1)[1].strip().rstrip("/")
                break

ADMIN_USER = "Admin"
ADMIN_PASS = "Dean06Greif!/Admin"  # from /app/backend/.env (test_credentials.md is stale)


@pytest.fixture(scope="module")
def admin_token():
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={"username": ADMIN_USER, "password": ADMIN_PASS}, timeout=30)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    tok = r.json().get("token")
    assert tok
    return tok


@pytest.fixture(scope="module")
def auth_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


# ---------- Regression health ----------
def test_health_alive():
    r = requests.get(f"{BASE_URL}/api/health", timeout=15)
    assert r.status_code == 200
    data = r.json()
    # accept either "status": "alive" or {"alive": true} style
    assert data.get("status") in ("alive", "ok") or data.get("alive") is True, data


def test_ai_status_phase4_keys():
    r = requests.get(f"{BASE_URL}/api/ai/status", timeout=20)
    assert r.status_code == 200, r.text
    d = r.json()
    # tune_conf_min=55 and collection_enabled=true from phase-4
    # Value may be nested; search flexibly
    txt = str(d)
    assert "tune_conf_min" in txt
    assert "collection_enabled" in txt


def test_ai_lab_status_ok():
    r = requests.get(f"{BASE_URL}/api/ai/lab/status", timeout=20)
    assert r.status_code == 200, r.text


# ---------- Gate status ----------
def test_gate_status_shadow_mode():
    r = requests.get(f"{BASE_URL}/api/ml/gate/status", timeout=20)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d.get("mode") == "shadow", d
    assert d.get("model_loaded") is True, d
    assert d.get("version") == 1, d
    settings = d.get("settings") or {}
    assert settings.get("threshold") == 0.45, settings
    assert settings.get("shadow_enabled") is True, settings


# ---------- Dataset ----------
def test_gate_dataset():
    r = requests.get(f"{BASE_URL}/api/ml/gate/dataset", timeout=60)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d.get("status") == "ok", d
    ds = d.get("dataset") or {}
    assert ds.get("source") == "prod_readonly", ds
    samples = ds.get("samples")
    assert isinstance(samples, int) and samples >= 900, f"samples={samples}"
    by_source = ds.get("by_source") or {}
    for k in ("decision", "signal", "ghost"):
        assert k in by_source, f"missing by_source.{k}: {by_source}"
    syms = ds.get("crypto_symbols") or []
    assert isinstance(syms, list) and len(syms) == 10, syms
    for s in syms:
        assert s.endswith("USDT"), s


# ---------- Train: auth ----------
def test_gate_train_requires_auth():
    r = requests.post(f"{BASE_URL}/api/ml/gate/train", timeout=30)
    assert r.status_code in (401, 403), r.status_code


def test_gate_train_success(auth_headers):
    r = requests.post(f"{BASE_URL}/api/ml/gate/train", headers=auth_headers, timeout=180)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d.get("status") == "ok", d
    assert d.get("version") == 2, d
    metrics = d.get("metrics") or {}
    assert metrics.get("folds_used", 0) >= 3, metrics
    for k in ("oos_brier_calibrated", "baseline_brier", "beats_baseline", "calibration_bins"):
        assert k in metrics, f"missing metric {k}: {metrics.keys()}"


def test_gate_models_lists_both_versions_no_booster():
    r = requests.get(f"{BASE_URL}/api/ml/gate/models", timeout=20)
    assert r.status_code == 200, r.text
    d = r.json()
    models = d.get("models") if isinstance(d, dict) else d
    assert isinstance(models, list) and len(models) >= 2, d
    versions = {m.get("version") for m in models}
    assert 1 in versions and 2 in versions, versions
    for m in models:
        assert "booster_b64" not in m, f"booster_b64 leaked: {list(m.keys())}"


# ---------- Report ----------
def test_gate_report_no_500():
    r = requests.get(f"{BASE_URL}/api/ml/gate/report", params={"days": 28}, timeout=30)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d.get("evaluated") == 0, d
    crit = d.get("criteria") or {}
    assert isinstance(crit, dict) and len(crit) == 5, crit
    assert d.get("model_version") is not None, d


# ---------- Settings ----------
def test_gate_settings_requires_auth():
    r = requests.post(f"{BASE_URL}/api/ml/gate/settings", json={"threshold": 0.5}, timeout=20)
    assert r.status_code in (401, 403), r.status_code


def test_gate_settings_update_and_persist(auth_headers):
    # Set 0.5
    r = requests.post(f"{BASE_URL}/api/ml/gate/settings",
                      json={"threshold": 0.5}, headers=auth_headers, timeout=20)
    assert r.status_code == 200, r.text
    d = r.json()
    settings = d.get("settings") or {}
    assert settings.get("threshold") == 0.5, settings

    # Verify via GET status
    r2 = requests.get(f"{BASE_URL}/api/ml/gate/status", timeout=15)
    assert r2.status_code == 200
    assert (r2.json().get("settings") or {}).get("threshold") == 0.5

    # Reset to 0.45
    r3 = requests.post(f"{BASE_URL}/api/ml/gate/settings",
                       json={"threshold": 0.45}, headers=auth_headers, timeout=20)
    assert r3.status_code == 200
    assert (r3.json().get("settings") or {}).get("threshold") == 0.45
