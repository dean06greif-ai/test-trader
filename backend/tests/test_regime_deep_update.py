"""
Backend tests for the current update:
- Worker package completeness (manifest + zip)
- Local worker status (online, not outdated, v1.6.0)
- Local backtest via worker
- Regime lab engine defaults (modes 3/5/9, default 5, adapt_profiles)
- Regime analysis with modes 3, 5, 9 (labels, adapt.report.candidates)
- Deep test in optimizer (combo mode)
- Deep test in regime-lab optimize
"""
import io
import os
import time
import zipfile
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://daytrader-ml.preview.emergentagent.com").rstrip("/")


@pytest.fixture(scope="session")
def token():
    r = requests.post(f"{BASE_URL}/api/auth/login", json={"username": os.environ.get("ADMIN_USER", "Admin"), "password": os.environ.get("ADMIN_PASSWORD", "admin")}, timeout=15)
    assert r.status_code == 200, r.text
    return r.json()["token"]


@pytest.fixture(scope="session")
def auth_headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# -------- Worker package --------
class TestWorkerPackage:
    def test_manifest_complete(self, auth_headers):
        r = requests.get(f"{BASE_URL}/api/localworker/package/manifest", headers=auth_headers, timeout=15)
        assert r.status_code == 200, r.text
        m = r.json()
        assert m.get("complete") is True, f"manifest not complete: {m}"
        assert m.get("missing") in ([], None), f"missing files reported: {m.get('missing')}"
        wf = m.get("worker_files") or m.get("files") or []
        wf_s = " ".join(str(f) for f in wf)
        for needed in ["worker.py", "requirements.txt", "README.md", "start_worker.bat"]:
            assert needed in wf_s, f"{needed} missing from manifest worker_files: {wf}"
        mods = m.get("modules") or {}
        for folder in ["core", "services", "strategies", "models"]:
            assert folder in mods, f"module folder {folder} missing: {mods}"

    def test_package_zip_contents(self, auth_headers):
        r = requests.get(f"{BASE_URL}/api/localworker/package", headers=auth_headers, timeout=30)
        assert r.status_code == 200, r.text
        z = zipfile.ZipFile(io.BytesIO(r.content))
        names = z.namelist()
        for needed in ["worker.py", "requirements.txt", "README.md", "start_worker.bat"]:
            assert any(n.endswith(needed) for n in names), f"{needed} missing from zip"
        for folder in ["core/", "services/", "strategies/", "models/"]:
            assert any(folder in n for n in names), f"folder {folder} missing from zip"


# -------- Worker status --------
class TestWorkerStatus:
    def test_worker_online_and_current(self, auth_headers):
        r = requests.get(f"{BASE_URL}/api/localworker/status", headers=auth_headers, timeout=15)
        assert r.status_code == 200
        d = r.json()
        assert d.get("online") is True, f"worker not online: {d}"
        # Erwartete Versionen dynamisch ermitteln (kein Hardcoding)
        import re
        from services.local_exec import REQUIRED_WORKER_VERSION_STR
        pkg_ver = re.search(r'VERSION = "([\d.]+)"',
                            open("/app/local_worker/worker.py").read()).group(1)
        assert d.get("required_version") == REQUIRED_WORKER_VERSION_STR
        assert d.get("workers"), "no workers reported"
        w = d["workers"][0]
        assert w["version"] == pkg_ver, f"worker={w['version']} paket={pkg_ver}"
        assert w.get("outdated") is not True


# -------- Regime engine defaults --------
class TestRegimeEngineDefaults:
    def test_defaults(self, auth_headers):
        r = requests.get(f"{BASE_URL}/api/regime-lab/engine/defaults", headers=auth_headers, timeout=15)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d.get("default_regime_mode") == 5
        modes = d.get("regime_modes") or []
        # regime_modes is a list of {mode, count, taxonomy}
        assert isinstance(modes, list)
        by_mode = {m.get("mode"): m for m in modes}
        assert set(by_mode.keys()) >= {3, 5, 9}, f"modes present: {list(by_mode.keys())}"
        for k in (3, 5, 9):
            entry = by_mode[k]
            tax = entry.get("taxonomy") or []
            assert len(tax) == k, f"mode {k} taxonomy len={len(tax)}"
        adapt = d.get("adapt_profiles") or []
        akeys = {(p.get("key") if isinstance(p, dict) else p) for p in adapt}
        assert {"fein", "standard", "grob"}.issubset(akeys), f"adapt_profiles keys={akeys}"


# -------- Local backtest --------
class TestLocalBacktest:
    def test_local_backtest_runs(self, auth_headers):
        body = {"strategy_ids": ["rsi_only"], "symbols": ["BTCUSDT"], "days": 5,
                "timeframe": "5m", "execution": "local"}
        r = requests.post(f"{BASE_URL}/api/backtest/run", headers=auth_headers, json=body, timeout=30)
        assert r.status_code == 200, r.text
        job_id = r.json().get("job_id") or r.json().get("id")
        assert job_id, r.text
        deadline = time.time() + 180
        last = None
        while time.time() < deadline:
            s = requests.get(f"{BASE_URL}/api/backtest/status/{job_id}", headers=auth_headers, timeout=15)
            if s.status_code == 200:
                last = s.json()
                if last.get("status") in ("done", "completed", "finished", "error", "failed"):
                    break
            time.sleep(3)
        assert last, "no status response"
        assert last.get("status") in ("done", "completed", "finished"), f"backtest job status: {last.get('status')} error={last.get('error')}"
        assert not last.get("error"), f"backtest error: {last.get('error')}"


# -------- Regime analysis helper --------
def _run_regime_analysis(auth_headers, body, poll_timeout=240):
    r = requests.post(f"{BASE_URL}/api/regime-lab/analyze", headers=auth_headers, json=body, timeout=30)
    assert r.status_code == 200, f"analyze failed: {r.status_code} {r.text}"
    job_id = r.json().get("job_id") or r.json().get("id")
    assert job_id, r.text
    deadline = time.time() + poll_timeout
    last = None
    while time.time() < deadline:
        s = requests.get(f"{BASE_URL}/api/regime-lab/status/{job_id}", headers=auth_headers, timeout=15)
        if s.status_code == 200:
            last = s.json()
            st = last.get("status")
            if st in ("completed", "done", "finished", "error", "failed"):
                break
        time.sleep(3)
    assert last and last.get("status") in ("completed", "done", "finished"), f"regime job did not complete: {last}"
    result_obj = last.get("result") or {}
    analysis_id = result_obj.get("analysis_id") or last.get("analysis_id") or last.get("id") or last.get("result_id")
    if not analysis_id:
        # fetch latest
        lst = requests.get(f"{BASE_URL}/api/regime-lab", headers=auth_headers, timeout=15).json()
        items = lst.get("items") or lst if isinstance(lst, list) else lst.get("items", [])
        if items:
            analysis_id = items[0].get("id")
    assert analysis_id, f"no analysis_id in status: {last}"
    a = requests.get(f"{BASE_URL}/api/regime-lab/{analysis_id}", headers=auth_headers, timeout=30)
    assert a.status_code == 200, a.text
    aj = a.json()
    # response is wrapped as { "analysis": {...} }
    if isinstance(aj, dict) and "analysis" in aj and "combined" not in aj:
        aj = aj["analysis"]
    return analysis_id, aj


class TestRegimeModes:
    def test_mode_3(self, auth_headers):
        body = {"symbols": ["BTCUSDT"], "timeframe": "4h", "days": 720, "scope": "combined",
                "engine": "v2", "regime_mode": 3, "name": "QA 3 Regime"}
        aid, res = _run_regime_analysis(auth_headers, body)
        combined = res.get("combined") or {}
        model = combined.get("model") or {}
        assert model.get("regime_mode") == 3, f"regime_mode in model: {model.get('regime_mode')}"
        tax = model.get("taxonomy") or model.get("regimes") or []
        labels = [t.get("label") if isinstance(t, dict) else t for t in tax]
        assert len(tax) == 3, f"expected 3 regimes, got {len(tax)}: {labels}"
        expected = {"Aufwärtstrend", "Seitwärtsmarkt", "Abwärtstrend"}
        assert expected.issubset(set(labels)), f"labels={labels}"
        val = combined.get("validation") or {}
        vp = val.get("violation_bars_pct")
        assert vp is None or vp <= 8, f"violation_bars_pct={vp}"
        adapt = model.get("adapt") or {}
        report = adapt.get("report") or {}
        cands = report.get("candidates") or []
        assert len(cands) >= 3, f"expected >=3 candidates, got {len(cands)}"
        for c in cands[:3]:
            assert "quality" in c, f"candidate missing quality: {c}"

    def test_mode_5_default_adaptive(self, auth_headers):
        body = {"symbols": ["BTCUSDT"], "timeframe": "4h", "days": 720, "scope": "combined",
                "engine": "v2", "regime_mode": 5, "name": "QA 5 Regime"}
        aid, res = _run_regime_analysis(auth_headers, body)
        combined = res.get("combined") or {}
        model = combined.get("model") or {}
        assert model.get("regime_mode") == 5
        tax = model.get("taxonomy") or model.get("regimes") or []
        labels = [t.get("label") if isinstance(t, dict) else t for t in tax]
        assert len(tax) == 5, f"labels={labels}"
        expected = {"Starker Abwärtstrend", "Leichter Abwärtstrend", "Seitwärtsmarkt",
                    "Leichter Aufwärtstrend", "Starker Aufwärtstrend"}
        assert expected.issubset(set(labels)), f"labels={labels}"
        cfg = model.get("config") or {}
        # adaptive: should NOT be exactly defaults 2 / 0.5 / [5,10,20,50,100]
        mh = cfg.get("min_hold_days")
        cd = cfg.get("confirm_days")
        hz = cfg.get("horizons_days")
        default_hz = [5, 10, 20, 50, 100]
        not_all_default = not (mh == 2 and cd == 0.5 and hz == default_hz)
        assert not_all_default, f"adaptive config not applied: {cfg}"

    def test_mode_9_backcompat(self, auth_headers):
        body = {"symbols": ["BTCUSDT"], "timeframe": "4h", "days": 365, "scope": "combined",
                "engine": "v2", "regime_mode": 9, "name": "QA 9 Regime"}
        aid, res = _run_regime_analysis(auth_headers, body)
        combined = res.get("combined") or {}
        model = combined.get("model") or {}
        tax = model.get("taxonomy") or model.get("regimes") or []
        assert len(tax) == 9, f"expected 9 taxonomy entries, got {len(tax)}"
        # each entry should have vol field
        for t in tax:
            if isinstance(t, dict):
                assert t.get("vol") in ("low", "mid", "high"), f"entry vol invalid: {t}"


# -------- Deep Test in Optimizer --------
class TestOptimizerDeepTest:
    def _run_optimizer(self, auth_headers, body, poll_timeout=240):
        r = requests.post(f"{BASE_URL}/api/optimizer/run", headers=auth_headers, json=body, timeout=30)
        assert r.status_code == 200, f"{r.status_code} {r.text}"
        job_id = r.json().get("job_id") or r.json().get("id")
        assert job_id
        deadline = time.time() + poll_timeout
        last = None
        while time.time() < deadline:
            s = requests.get(f"{BASE_URL}/api/optimizer/status/{job_id}", headers=auth_headers, timeout=15)
            if s.status_code == 200:
                last = s.json()
                if last.get("status") in ("completed", "done", "finished", "error", "failed"):
                    break
            time.sleep(3)
        assert last and last.get("status") in ("completed", "done", "finished"), f"opt job status: {last}"
        return last

    def test_optimizer_with_deep_test(self, auth_headers):
        body = {"mode": "combo", "symbols": ["BTCUSDT"], "days": 30, "timeframe": "1h",
                "objective": "combo", "iterations": 10, "max_rules": 3, "min_trades": 3,
                "deep_test": True,
                "indicators": ["rsi", "ema_slow", "macd_hist", "bb_lower", "stoch_k", "vwap"],
                "execution": "cloud"}
        res = self._run_optimizer(auth_headers, body)
        result = res.get("result") or {}
        assert result.get("deep_test") is True, f"deep_test flag missing: {result.get('deep_test')}"
        dr = result.get("deep_report") or {}
        for key in ["singles", "pairs", "contribution", "best_synergies", "indicator_frequency", "conclusion"]:
            assert key in dr, f"deep_report missing key {key}: keys={list(dr.keys())}"
        assert result.get("rules"), "result.rules empty"

    def test_optimizer_without_deep_test(self, auth_headers):
        body = {"mode": "combo", "symbols": ["BTCUSDT"], "days": 30, "timeframe": "1h",
                "objective": "combo", "iterations": 10, "max_rules": 3, "min_trades": 3,
                "indicators": ["rsi", "ema_slow", "macd_hist", "bb_lower", "stoch_k", "vwap"],
                "execution": "cloud"}
        res = self._run_optimizer(auth_headers, body, poll_timeout=180)
        result = res.get("result") or {}
        # deep_report should not be present or None
        dr = result.get("deep_report")
        assert not dr, f"deep_report unexpectedly present: {list(dr.keys()) if isinstance(dr, dict) else dr}"


# -------- Deep Test in Regime-Lab Optimize --------
class TestRegimeLabOptimizeDeep:
    def test_regime_lab_optimize_deep(self, auth_headers):
        # produce a fresh smaller analysis with 5 regimes
        body = {"symbols": ["BTCUSDT"], "timeframe": "4h", "days": 365, "scope": "combined",
                "engine": "v2", "regime_mode": 5, "name": "QA opt-deep"}
        aid, res = _run_regime_analysis(auth_headers, body)
        # pick a regime id
        combined = res.get("combined") or {}
        model = combined.get("model") or {}
        tax = model.get("taxonomy") or []
        rid = None
        for t in tax:
            if isinstance(t, dict) and t.get("id"):
                rid = t["id"]; break
        assert rid, f"no regime id found in taxonomy: {tax}"
        opt_body = {"scope": "combined", "regime_id": rid, "mode": "combo",
                    "indicators": ["rsi", "ema_slow", "macd_hist", "stoch_k"],
                    "iterations": 8, "objective": "combo", "min_trades": 3, "max_rules": 3,
                    "optimize": {"tpsl": True}, "timeframe": "4h",
                    "regime_walk_forward": False, "deep_test": True}
        r = requests.post(f"{BASE_URL}/api/regime-lab/{aid}/optimize", headers=auth_headers, json=opt_body, timeout=30)
        assert r.status_code == 200, r.text
        job_id = r.json().get("job_id") or r.json().get("id")
        assert job_id, r.text
        deadline = time.time() + 300
        last = None
        while time.time() < deadline:
            s = requests.get(f"{BASE_URL}/api/regime-lab/status/{job_id}", headers=auth_headers, timeout=15)
            if s.status_code == 200:
                last = s.json()
                if last.get("status") in ("completed", "done", "finished", "error", "failed"):
                    break
            time.sleep(3)
        assert last and last.get("status") in ("completed", "done", "finished"), f"opt job: {last}"
        result = last.get("result") or {}
        disc = result.get("discovery") or {}
        assert disc.get("deep_test") is True, f"discovery.deep_test={disc.get('deep_test')}"
        dr = disc.get("deep_report") or {}
        for key in ["contribution", "best_synergies"]:
            assert key in dr, f"deep_report missing {key}: {list(dr.keys())}"
        assert result.get("top5"), "top5 empty"
