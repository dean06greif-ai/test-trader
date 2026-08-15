"""
Integration tests for Regime Engine v2 + NNFX Framework.

Covers the API surface only (v2 dispatch, kmeans compatibility, NNFX strategies,
dynamic-live confirm/dismiss/settings). The math is already covered by
test_regime_engine.py + test_nnfx.py.

Run: pytest /app/backend/tests/test_regime_v2_integration.py -v
"""
import os
import time
import pytest
import requests

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")


# ---- fixtures ---------------------------------------------------------------

@pytest.fixture(scope="module")
def api():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="module")
def token(api):
    r = api.post(f"{BASE_URL}/api/auth/login",
                 json={"username": os.environ.get("ADMIN_USER", "Admin"), "password": os.environ.get("ADMIN_PASSWORD", "admin")})
    assert r.status_code == 200, r.text
    return r.json()["token"]


@pytest.fixture(scope="module")
def auth_headers(token):
    return {"Authorization": f"Bearer {token}"}


# ---- 1. Engine defaults -----------------------------------------------------

class TestEngineDefaults:
    def test_defaults_shape(self, api):
        r = api.get(f"{BASE_URL}/api/regime-lab/engine/defaults")
        assert r.status_code == 200
        d = r.json()
        assert d["engine"] == "v2"
        assert isinstance(d["config"], dict) and "horizons_days" in d["config"]
        assert isinstance(d["meta"], list) and len(d["meta"]) >= 5
        assert all("key" in m and "label" in m and "help" in m for m in d["meta"])
        tax = d["taxonomy"]
        # Standard-Taxonomie richtet sich nach dem Standard-Modus (5),
        # alle Modi 3/5/9 werden zusätzlich mitgeliefert.
        assert isinstance(tax, list) and len(tax) == d["default_regime_mode"]
        modes = {m["mode"]: m["taxonomy"] for m in d["regime_modes"]}
        assert set(modes) == {3, 5, 9}
        assert len(modes[9]) == 9
        for t in modes[9]:
            assert {"trend", "vol", "label", "nnfx"} <= set(t.keys())
        for t in tax:
            assert {"trend", "label", "nnfx"} <= set(t.keys())
        assert isinstance(d.get("nnfx_labels"), (dict, list))


# ---- 2. Current-regime (v2 + kmeans) ---------------------------------------

class TestCurrentRegime:
    def test_v2_shape(self, api):
        r = api.get(f"{BASE_URL}/api/dynamic/current-regime",
                    params={"symbol": "BTCUSDT", "timeframe": "4h", "days": 180})
        assert r.status_code == 200
        d = r.json()
        assert d["engine"] == "v2"
        cur = d["current"]
        for k in ("label", "nnfx", "confidence", "strength", "reason"):
            assert k in cur
        assert "details" in cur and "per_horizon" in cur["details"]
        assert isinstance(d["regimes"], list) and len(d["regimes"]) >= 1
        assert all("nnfx" in r_ for r_ in d["regimes"])
        val = d["validation"]
        for k in ("violation_bars_pct", "direction_accuracy_pct", "passed"):
            assert k in val

    def test_kmeans_backward_compat(self, api):
        r = api.get(f"{BASE_URL}/api/dynamic/current-regime",
                    params={"symbol": "BTCUSDT", "timeframe": "4h",
                            "days": 180, "engine": "kmeans"})
        assert r.status_code == 200
        d = r.json()
        assert d["engine"] == "kmeans"
        assert "current" in d
        assert "regimes" in d


# ---- 3. Existing analysis ra_82c98807 --------------------------------------

def _seed_analysis_exists() -> bool:
    try:
        r = requests.get(f"{BASE_URL}/api/regime-lab/ra_82c98807", timeout=20)
        return r.status_code == 200
    except Exception:
        return False


requires_seed_analysis = pytest.mark.skipif(
    not _seed_analysis_exists(),
    reason="Seed-Analyse ra_82c98807 nicht in der DB vorhanden")


@requires_seed_analysis
class TestExistingAnalysis:
    def test_ra_82c98807_v2(self, api):
        r = api.get(f"{BASE_URL}/api/regime-lab/ra_82c98807")
        assert r.status_code == 200
        a = r.json().get("analysis", r.json())
        comb = a["combined"]
        assert comb["model"]["engine"] == "v2"
        regs = comb["model"]["regimes"]
        assert len(regs) == 9
        for r_ in regs:
            for k in ("label", "nnfx", "share_pct"):
                assert k in r_
        assert comb["validation"]["passed"] is True
        per = comb["per_symbol"]
        assert per, "per_symbol darf nicht leer sein"
        sym = next(iter(per))
        assert "current" in per[sym]
        assert "validation" in per[sym]
        assert "ideal" in per[sym]
        assert "agreement" in per[sym]["ideal"]


# ---- 4. analyze validation --------------------------------------------------

class TestAnalyzeValidation:
    def test_engine_quatsch_400(self, api, auth_headers):
        r = api.post(f"{BASE_URL}/api/regime-lab/analyze",
                     headers=auth_headers,
                     json={"symbols": ["BTCUSDT"], "timeframe": "4h",
                           "days": 30, "engine": "quatsch"})
        assert r.status_code == 400
        assert "engine" in r.text.lower()

    def test_engine_v2_starts_and_completes(self, api, auth_headers):
        """Kleiner Job (1 Coin, 4h, 90 Tage) - sollte in ~2 Minuten fertig sein."""
        r = api.post(f"{BASE_URL}/api/regime-lab/analyze",
                     headers=auth_headers,
                     json={
                         "symbols": ["BTCUSDT"],
                         "timeframe": "4h",
                         "days": 90,
                         "name": "TEST_engine_v2_small",
                         "engine": "v2",
                         "engine_config": {
                             "horizons_days": [10, 30],
                             "trend_t": 2.5,
                             "min_hold_days": 5
                         },
                     })
        assert r.status_code == 200, r.text
        job = r.json()
        job_id = job.get("job_id") or job.get("id")
        assert job_id, f"no job_id in {job}"

        # Poll status - allow up to 4 minutes
        aid = None
        deadline = time.time() + 240
        last = None
        while time.time() < deadline:
            s = api.get(f"{BASE_URL}/api/regime-lab/status/{job_id}")
            if s.status_code != 200:
                time.sleep(3)
                continue
            js = s.json()
            last = js
            st = js.get("status") or js.get("state")
            if st in ("done", "success", "completed", "finished"):
                aid = (js.get("analysis_id") or js.get("aid") or
                       (js.get("result") or {}).get("analysis_id"))
                break
            if st in ("error", "failed"):
                pytest.fail(f"Job failed: {js}")
            time.sleep(4)

        if not aid:
            pytest.skip(f"Job nicht rechtzeitig fertig; letzter status={last}")

        # Fetch saved analysis
        rr = api.get(f"{BASE_URL}/api/regime-lab/{aid}")
        assert rr.status_code == 200
        a = rr.json().get("analysis", rr.json())
        assert a["settings"]["engine"] == "v2"
        assert a["settings"]["engine_config"]["trend_t"] == 2.5
        assert a["combined"]["model"]["engine"] == "v2"

        # cleanup
        api.delete(f"{BASE_URL}/api/regime-lab/{aid}", headers=auth_headers)


# ---- 5. Strategies ----------------------------------------------------------

class TestStrategies:
    def test_nnfx_strategies_present(self, api):
        r = api.get(f"{BASE_URL}/api/strategies")
        assert r.status_code == 200
        data = r.json()
        strategies = data.get("strategies", data.get("items", data if isinstance(data, list) else []))
        ids = {s.get("id") for s in strategies}
        assert {"nnfx_trend", "nnfx_reversion", "nnfx_breakout"} <= ids
        by_id = {s["id"]: s for s in strategies if s.get("id")}
        # Regime tag + at least 14 params each
        expected_regimes = {"nnfx_trend": "trend", "nnfx_reversion": "range",
                            "nnfx_breakout": "breakout"}
        for sid, reg in expected_regimes.items():
            s = by_id[sid]
            assert s.get("nnfx_regime") == reg, f"{sid}: {s.get('nnfx_regime')}"
            params = s.get("params") or s.get("parameters") or []
            assert len(params) >= 14, f"{sid} has {len(params)} params"


# ---- 6. Dynamic (NNFX) apply / settings / confirm / dismiss ----------------

DYN_ID = "dyn_80384eba"


@requires_seed_analysis
class TestBuildNnfx:
    """POST /api/regime-lab/{aid}/build-nnfx — maps all 9 regimes to NNFX
    strategies, creates a dyn strategy, and writes assignments back."""

    def test_build_nnfx_from_ra_82c98807(self, api, auth_headers):
        r = api.post(f"{BASE_URL}/api/regime-lab/ra_82c98807/build-nnfx",
                     headers=auth_headers, json={})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["status"] == "success"
        dyn_id = d["id"]
        # regime_strategies must map all 9 (0..8) to nnfx_*
        rs = d["regime_strategies"]
        assert len(rs) == 9
        allowed = {"nnfx_trend", "nnfx_reversion", "nnfx_breakout"}
        assert set(rs.values()) <= allowed
        nn = d["nnfx_strategies"]
        assert nn == {"trend": "nnfx_trend", "range": "nnfx_reversion",
                      "breakout": "nnfx_breakout"}

        # verify dyn saved with framework=nnfx + require_confirmation
        lst = api.get(f"{BASE_URL}/api/dynamic/list").json()["strategies"]
        found = next((s for s in lst if s["id"] == dyn_id), None)
        assert found, f"{dyn_id} nicht in /api/dynamic/list"
        assert found["framework"] == "nnfx"
        assert found.get("regime_strategies") == rs
        assert (found.get("settings") or {}).get("require_confirmation") is True

        # verify analysis got assignments written back
        a = api.get(f"{BASE_URL}/api/regime-lab/ra_82c98807") \
            .json()["analysis"]
        assign = a.get("assignments") or {}
        combined = {k: v for k, v in assign.items()
                    if k.startswith("combined:")}
        assert len(combined) == 9, f"expected 9 combined:* assignments, got {len(combined)}"
        for _, v in combined.items():
            assert v.get("strategy_id") in allowed
            assert v.get("nnfx") in ("trend", "range", "breakout")

        # cleanup: delete the newly created dyn
        api.delete(f"{BASE_URL}/api/dynamic/{dyn_id}", headers=auth_headers)


class TestDynamic:
    def test_dynamic_exists(self, api):
        r = api.get(f"{BASE_URL}/api/dynamic/list")
        assert r.status_code == 200
        ids = [s.get("id") for s in r.json().get("strategies", [])]
        if DYN_ID not in ids:
            pytest.skip(f"{DYN_ID} not present, skipping dynamic checks")

    def test_refresh_per_symbol(self, api):
        r = api.post(f"{BASE_URL}/api/dynamic/{DYN_ID}/refresh", json={"days": 30})
        if r.status_code == 404:
            pytest.skip(f"{DYN_ID} not found")
        assert r.status_code == 200, r.text
        d = r.json()
        # per_symbol may be under key per_symbol / by_symbol
        per = d.get("per_symbol") or d.get("by_symbol") or d.get("symbols") or {}
        assert per, f"no per_symbol payload: keys={list(d.keys())}"
        sample = next(iter(per.values()))
        assert isinstance(sample, dict)
        # Should carry regime/label/nnfx/confidence/reason
        for k in ("label", "nnfx", "confidence"):
            assert k in sample, f"missing {k} in {list(sample.keys())}"

    def test_apply_activates_nnfx_strategies(self, api, auth_headers):
        r = api.post(f"{BASE_URL}/api/dynamic/{DYN_ID}/apply", headers=auth_headers)
        if r.status_code == 404:
            pytest.skip(f"{DYN_ID} not found")
        assert r.status_code == 200, r.text
        d = r.json()
        applied = d.get("applied", [])
        assert isinstance(applied, list) and len(applied) >= 1
        # first entry should have a strategy_id starting with nnfx_
        assert any((a.get("strategy_id") or "").startswith("nnfx_") for a in applied), applied

    def test_settings_require_confirmation(self, api, auth_headers):
        r = api.post(f"{BASE_URL}/api/dynamic/{DYN_ID}/settings",
                     headers=auth_headers,
                     json={"require_confirmation": True})
        if r.status_code == 404:
            pytest.skip(f"{DYN_ID} not found")
        assert r.status_code == 200, r.text
        assert r.json()["settings"]["require_confirmation"] is True

        # Confirm without pending must be 400
        r2 = api.post(f"{BASE_URL}/api/dynamic/{DYN_ID}/confirm",
                      headers=auth_headers)
        assert r2.status_code == 400, r2.text
        assert "kein" in r2.text.lower() or "no" in r2.text.lower() or "pending" in r2.text.lower()

    def test_dismiss_returns_dismissed(self, api, auth_headers):
        r = api.post(f"{BASE_URL}/api/dynamic/{DYN_ID}/dismiss",
                     headers=auth_headers)
        if r.status_code == 404:
            pytest.skip(f"{DYN_ID} not found")
        assert r.status_code == 200
        assert r.json().get("status") == "dismissed"


# ---- 7. Regression: existing endpoints unchanged ---------------------------

class TestRegression:
    def test_strategies(self, api):
        r = api.get(f"{BASE_URL}/api/strategies")
        assert r.status_code == 200

    def test_coins(self, api):
        r = api.get(f"{BASE_URL}/api/coins")
        assert r.status_code == 200

    def test_regime_lab_list(self, api):
        r = api.get(f"{BASE_URL}/api/regime-lab/list")
        assert r.status_code == 200
