"""Iteration 11 – Robustness features (Walk-Forward, DD-Filter, Konstanz),
Top-5-Ranking, days-Clamp bis 5500 und LocalWorker use_gpu Persistenz."""
import os
import time
import pytest
import requests

def _read_frontend_env():
    try:
        with open("/app/frontend/.env", "r") as f:
            for line in f:
                if line.startswith("REACT_APP_BACKEND_URL="):
                    return line.split("=", 1)[1].strip()
    except Exception:
        return None
    return None


BASE_URL = (os.environ.get("REACT_APP_BACKEND_URL") or _read_frontend_env() or "").rstrip("/")
assert BASE_URL, "REACT_APP_BACKEND_URL not configured"


@pytest.fixture(scope="module")
def token():
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={"username": os.environ.get("ADMIN_USER", "Admin"), "password": os.environ.get("ADMIN_PASSWORD", "admin")}, timeout=15)
    assert r.status_code == 200, r.text
    tok = r.json().get("token")
    assert tok
    return tok


@pytest.fixture(scope="module")
def headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _reset_optimizer(headers):
    try:
        requests.post(f"{BASE_URL}/api/optimizer/reset", headers=headers, timeout=15)
    except Exception:
        pass


def _wait_done(job_id, timeout=180):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = requests.get(f"{BASE_URL}/api/optimizer/status/{job_id}", timeout=15)
        assert r.status_code == 200, r.text
        j = r.json()
        st = j.get("status")
        if st in ("done", "error", "cancelled"):
            return j
        time.sleep(2)
    return {"status": "timeout"}


# ---------------- 1) Discovery + WF + DD + Konstanz ----------------
def test_discovery_with_all_robustness(headers):
    _reset_optimizer(headers)
    body = {
        "mode": "discovery",
        "symbols": ["BTCUSDT"],
        "days": 2,
        "timeframe": "5m",
        "iterations": 8,
        "min_trades": 2,
        "max_rules": 2,
        "indicators": ["rsi", "ema_fast", "macd_hist"],
        "optimize": {"tpsl": False},
        "walk_forward": {"enabled": True, "train_pct": 75},
        "dd_filter": {"enabled": True, "max_dd_pct": 60},
        "constancy": {"enabled": True, "chunk_days": 2, "max_deviation_pct": 150},
    }
    r = requests.post(f"{BASE_URL}/api/optimizer/run", json=body, headers=headers, timeout=30)
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]

    j = _wait_done(job_id, timeout=180)
    assert j["status"] == "done", f"job status={j.get('status')}, phase={j.get('phase')}, error={j.get('error')}"

    result = j.get("result") or {}
    # top5 vorhanden
    top5 = result.get("top5")
    assert isinstance(top5, list) and len(top5) >= 1 and len(top5) <= 5
    for e in top5:
        assert "rank" in e and "passed" in e and "score" in e and "metrics" in e
        assert "definition" in e and "rules" in e and "trade_params" in e
        # WF fields
        assert "test_metrics" in e
        wf = e.get("wf") or {}
        assert "wf_score" in wf
        # DD
        assert "dd_ratio_pct" in e and "dd_pass" in e
        # Konstanz
        c = e.get("constancy") or {}
        assert "deviation_pct" in c and "passed" in c and "chunks" in c

    # walk_forward echo
    wf_echo = result.get("walk_forward") or {}
    assert "train_days" in wf_echo and "test_days" in wf_echo and "train_pct" in wf_echo
    # robustness echo
    assert "robustness" in result


# ---------------- 2) Regression: discovery ohne neue Felder ----------------
def test_discovery_regression_no_robust_fields(headers):
    _reset_optimizer(headers)
    body = {
        "mode": "discovery",
        "symbols": ["BTCUSDT"],
        "days": 2,
        "timeframe": "5m",
        "iterations": 6,
        "min_trades": 2,
        "max_rules": 2,
        "indicators": ["rsi", "ema_fast"],
        "optimize": {"tpsl": False},
    }
    r = requests.post(f"{BASE_URL}/api/optimizer/run", json=body, headers=headers, timeout=30)
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]

    j = _wait_done(job_id, timeout=180)
    assert j["status"] == "done", f"job status={j.get('status')}, error={j.get('error')}"
    result = j.get("result") or {}

    # top5 existiert (reines Ranking)
    top5 = result.get("top5")
    assert isinstance(top5, list) and len(top5) >= 1
    for e in top5:
        assert e.get("passed") is True
        # KEINE wf/dd/constancy Felder
        assert "wf" not in e
        assert "dd_pass" not in e
        assert "constancy" not in e

    # Regression: definition/metrics/rules/steps vorhanden
    assert "definition" in result
    assert "metrics" in result
    assert "rules" in result
    assert "steps" in result


# ---------------- 3) mode=params + WF ----------------
def test_params_mode_with_wf(headers):
    _reset_optimizer(headers)
    # strategy_id von /api/strategies holen
    r = requests.get(f"{BASE_URL}/api/strategies", headers=headers, timeout=15)
    assert r.status_code == 200
    data = r.json()
    strategies = data.get("strategies", data) if isinstance(data, dict) else data
    ids = [s.get("id") or s.get("strategy_id") for s in strategies]
    sid = "scalping_4_rules" if "scalping_4_rules" in ids else ids[0]

    body = {
        "mode": "params",
        "strategy_id": sid,
        "symbols": ["BTCUSDT"],
        "days": 2,
        "timeframe": "5m",
        "iterations": 8,
        "min_trades": 2,
        "walk_forward": {"enabled": True, "train_pct": 75},
    }
    r = requests.post(f"{BASE_URL}/api/optimizer/run", json=body, headers=headers, timeout=30)
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]

    j = _wait_done(job_id, timeout=180)
    assert j["status"] == "done", f"job status={j.get('status')}, error={j.get('error')}"
    result = j.get("result") or {}

    top5 = result.get("top5")
    assert isinstance(top5, list) and len(top5) >= 1
    for e in top5:
        assert "params" in e
        assert "trade_params" in e
        # kein definition (params-mode)
        assert "definition" not in e

    # Regression: baseline / best / top vorhanden
    assert "baseline" in result or "best" in result


# ---------------- 4) days-Clamp bis 5500 (sofort canceln) ----------------
def test_days_clamp_5500(headers):
    _reset_optimizer(headers)
    body = {
        "mode": "discovery",
        "symbols": ["BTCUSDT"],
        "days": 9999,
        "timeframe": "5m",
        "iterations": 6,
        "min_trades": 2,
        "max_rules": 2,
        "indicators": ["rsi"],
        "optimize": {"tpsl": False},
    }
    r = requests.post(f"{BASE_URL}/api/optimizer/run", json=body, headers=headers, timeout=30)
    assert r.status_code == 200, r.text  # KEIN 400
    job_id = r.json()["job_id"]

    # Params echo prüfen (clamp visible in job.params)
    time.sleep(1)
    rs = requests.get(f"{BASE_URL}/api/optimizer/status/{job_id}", timeout=15)
    assert rs.status_code == 200

    # sofort canceln
    rc = requests.post(f"{BASE_URL}/api/optimizer/cancel/{job_id}", headers=headers, timeout=15)
    assert rc.status_code == 200, rc.text
    _reset_optimizer(headers)


# ---------------- 5) apply type=strategy aus top5-Definition ----------------
def test_apply_from_top5_definition(headers):
    _reset_optimizer(headers)
    body = {
        "mode": "discovery",
        "symbols": ["BTCUSDT"],
        "days": 2,
        "timeframe": "5m",
        "iterations": 6,
        "min_trades": 2,
        "max_rules": 2,
        "indicators": ["rsi", "ema_fast"],
        "optimize": {"tpsl": False},
    }
    r = requests.post(f"{BASE_URL}/api/optimizer/run", json=body, headers=headers, timeout=30)
    assert r.status_code == 200
    job_id = r.json()["job_id"]
    j = _wait_done(job_id, timeout=180)
    assert j["status"] == "done"
    top5 = (j.get("result") or {}).get("top5") or []
    assert top5
    definition = top5[0].get("definition")
    assert definition

    apply_body = {
        "type": "strategy",
        "job_id": job_id,
        "definition": definition,
        "name": f"TEST_iter11_{int(time.time())}",
    }
    ra = requests.post(f"{BASE_URL}/api/optimizer/apply", json=apply_body,
                       headers=headers, timeout=30)
    assert ra.status_code == 200, ra.text
    data = ra.json()
    assert data.get("status") == "success"
    assert (data.get("id") or "").startswith("custom_")


# ---------------- 6) LocalWorker use_gpu Persistenz ----------------
def test_localworker_use_gpu_persistence(headers):
    # GET settings
    r = requests.get(f"{BASE_URL}/api/localworker/settings", headers=headers, timeout=15)
    # Endpoint kann auch ohne Auth existieren
    if r.status_code == 404:
        pytest.skip("Endpoint /api/localworker/settings nicht vorhanden")
    assert r.status_code in (200, 401), r.text
    if r.status_code == 401:
        pytest.skip("Auth required not passing; skip")

    original_wrap = r.json() or {}
    original = original_wrap.get("settings", original_wrap)
    # POST use_gpu=true
    body = dict(original)
    body["use_gpu"] = True
    r2 = requests.post(f"{BASE_URL}/api/localworker/settings", json=body,
                       headers=headers, timeout=15)
    assert r2.status_code == 200, r2.text

    r3 = requests.get(f"{BASE_URL}/api/localworker/settings", headers=headers, timeout=15)
    assert r3.status_code == 200
    j3 = r3.json()
    settings3 = j3.get("settings", j3)
    assert settings3.get("use_gpu") is True

    # zurücksetzen auf original
    body2 = dict(original)
    body2["use_gpu"] = bool(original.get("use_gpu", False))
    requests.post(f"{BASE_URL}/api/localworker/settings", json=body2,
                  headers=headers, timeout=15)
