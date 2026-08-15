"""Integrationstests für Iteration-3-Erweiterungen (Farben-Daten, Frühwarnung,
Strategie-Parameter je Regime, build-nnfx-Idempotenz, refresh/apply).

Regressionen der Kern-Mathematik werden bereits durch die (grünen) Unit-Suiten
`test_regime_engine.py`, `test_nnfx.py` und `test_regime_extras.py` abgedeckt.
Hier geht es um das HTTP-/DB-Verhalten der neuen Endpoints und Body-Keys.
"""
import os
import time

import pytest
import requests

BASE = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
ANALYSIS_ID = "ra_82c98807"           # historische BTC/ETH 12h Engine v2 Analyse
UP_LOW_VOL_REGIME = 6                 # id 6 = trend=up, vol=low (Aufwärts)


def _analysis_exists() -> bool:
    try:
        r = requests.get(f"{BASE}/api/regime-lab/{ANALYSIS_ID}", timeout=20)
        return r.status_code == 200
    except Exception:
        return False


# Tests gegen die historische Seed-Analyse überspringen, wenn sie in der DB
# nicht (mehr) existiert – sie prüfen Migrations-/Endpoint-Verhalten an einem
# konkreten Alt-Dokument.
requires_seed_analysis = pytest.mark.skipif(
    not _analysis_exists(),
    reason=f"Seed-Analyse {ANALYSIS_ID} nicht in der DB vorhanden")


# --------------------------------------------------------- Fixtures
@pytest.fixture(scope="module")
def api():
    s = requests.Session()
    s.headers["Content-Type"] = "application/json"
    return s


@pytest.fixture(scope="module")
def token(api):
    r = api.post(f"{BASE}/api/auth/login",
                 json={"username": os.environ.get("ADMIN_USER", "Admin"), "password": os.environ.get("ADMIN_PASSWORD", "admin")}, timeout=30)
    assert r.status_code == 200, r.text
    tk = r.json().get("token") or r.json().get("access_token")
    assert tk, r.text
    return tk


@pytest.fixture(scope="module")
def auth(api, token):
    api.headers["Authorization"] = f"Bearer {token}"
    return api


# --------------------------------------------------------- Regressions-Endpoints
class TestRegression:
    def test_engine_defaults(self, api):
        r = api.get(f"{BASE}/api/regime-lab/engine/defaults", timeout=30)
        assert r.status_code == 200
        d = r.json()
        assert d.get("engine") == "v2"
        # Standard ist der 5er-Modus; 3/5/9 müssen alle angeboten werden
        assert d.get("default_regime_mode") == 5
        modes = {m["mode"]: m["taxonomy"] for m in (d.get("regime_modes") or [])}
        assert set(modes.keys()) == {3, 5, 9}
        for mode, tx in modes.items():
            assert len(tx) == mode
            for t in tx:
                assert t["trend"] in ("down", "side", "up")
                if mode == 9:
                    assert t["vol"] in ("low", "mid", "high")
        tx = d.get("taxonomy") or []
        assert len(tx) == d["default_regime_mode"]
        assert set((d.get("nnfx_labels") or {}).keys()) == {"trend", "range", "breakout"}

    def test_strategies_and_coins(self, api):
        rs = api.get(f"{BASE}/api/strategies", timeout=30).json()
        sids = {s["id"] for s in rs.get("strategies", [])}
        assert {"nnfx_trend", "nnfx_reversion", "nnfx_breakout"}.issubset(sids)
        rc = api.get(f"{BASE}/api/coins", timeout=30).json()
        assert len(rc.get("coins") or []) > 0

    @requires_seed_analysis
    def test_regime_lab_list_and_analysis(self, api):
        rl = api.get(f"{BASE}/api/regime-lab/list", timeout=30).json()
        assert any(a["id"] == ANALYSIS_ID for a in rl.get("analyses", []))
        a = api.get(f"{BASE}/api/regime-lab/{ANALYSIS_ID}", timeout=30).json()["analysis"]
        model = ((a.get("combined") or {}).get("model")) or {}
        # Regimes tragen trend/vol/nnfx – Voraussetzung für Farb-/NNFX-Zuordnung
        for r in model.get("regimes") or []:
            assert r["trend"] in ("down", "side", "up")
            assert r["vol"] in ("low", "mid", "high")
            assert r["nnfx"] in ("trend", "range", "breakout")


# --------------------------------------------------------- Frühwarnung
class TestEarlyWarning:
    def test_current_regime_has_early_warning_fields(self, api):
        r = api.get(f"{BASE}/api/dynamic/current-regime",
                    params={"symbol": "BTCUSDT", "timeframe": "4h", "days": 90},
                    timeout=120)
        assert r.status_code == 200, r.text
        d = r.json()
        cur = d.get("current") or {}
        ew = cur.get("early_warning")
        assert ew is not None, "early_warning muss im current-Objekt vorhanden sein"
        # Pflichtfelder gemäß Review
        for f in ("active", "next_regime", "next_label", "next_nnfx",
                  "probability_pct", "eta_days", "pending", "pending_days",
                  "confirm_days", "min_hold_days", "hold_remaining_days", "reason"):
            assert f in ew, f"early_warning fehlt Feld {f}: {ew}"
        assert 0 <= ew["probability_pct"] <= 99
        assert ew["eta_days"] is None or ew["eta_days"] > 0
        assert ew["next_nnfx"] in ("trend", "range", "breakout")
        assert isinstance(ew["pending"], bool)
        assert isinstance(ew["active"], bool)


# --------------------------------------------------------- Optimize-Job Helper
def _wait_for_job(api, job_id, timeout_s=360):
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        r = api.get(f"{BASE}/api/regime-lab/status/{job_id}", timeout=30)
        if r.status_code != 200:
            time.sleep(2)
            continue
        j = r.json()
        last = j
        st = j.get("status")
        if st in ("done", "error", "cancelled"):
            return j
        time.sleep(4)
    raise AssertionError(f"Job {job_id} timeout (last={last})")


def _start_optimize(auth, body):
    r = auth.post(f"{BASE}/api/regime-lab/{ANALYSIS_ID}/optimize",
                  json=body, timeout=60)
    assert r.status_code == 200, r.text
    return r.json()["job_id"]


# --------------------------------------------------------- Strategie-Parameter je Regime
# Alle abhängigen Tests in EINER Klasse -> xdist loadscope pinnt sie auf denselben
# Worker, sodass die geteilten pytest-Attribute erhalten bleiben.
@requires_seed_analysis
class TestOptimizeAssignBuildNnfxRefresh:

    def test_01_optimize_with_flag_yields_distinct_strategy_params(self, auth):
        """optimize_strategy_params=true -> top5[*].strategy_params variiert und
        führt zu unterschiedlichen metrics (Regression Provider-Cache-Bugfix).
        Zusätzlich: Flag-Params allow_long/allow_short NICHT enthalten."""
        job_id = _start_optimize(auth, {
            "scope": "combined", "regime_id": UP_LOW_VOL_REGIME,
            "mode": "params", "strategy_id": "nnfx_trend",
            "iterations": 10, "objective": "pnl", "min_trades": 3,
            "optimize_strategy_params": True,
            "regime_walk_forward": False,   # Zeit sparen; WF nicht Gegenstand hier
        })
        j = _wait_for_job(auth, job_id, timeout_s=420)
        assert j["status"] == "done", f"Job Status={j.get('status')} err={j.get('error')}"
        res = j.get("result") or {}
        top5 = res.get("top5") or []
        assert len(top5) >= 2, f"Zu wenige Kandidaten: {top5}"
        # strategy_params muss vorhanden und in mindestens einem Kandidaten nicht leer sein
        with_params = [c for c in top5 if (c.get("strategy_params") or {})]
        assert with_params, f"Keine Kandidaten mit strategy_params: {top5}"
        # Werte unterscheiden sich zwischen Kandidaten (Cache-Bugfix-Regression)
        as_tuples = {tuple(sorted((c.get("strategy_params") or {}).items()))
                     for c in top5}
        assert len(as_tuples) >= 2, \
            f"Alle Kandidaten haben identische strategy_params: {as_tuples}"
        # Flag-Params dürfen NICHT gesampelt sein
        for c in top5:
            keys = set((c.get("strategy_params") or {}).keys())
            assert "allow_long" not in keys and "allow_short" not in keys, \
                f"allow_long/allow_short unerwartet in strategy_params: {keys}"
        # Verschiedene Signals -> unterschiedliche trades/pnl_pct in mind. 2 Kandidaten
        metrics_pairs = {(c.get("metrics", {}).get("trades"),
                          c.get("metrics", {}).get("pnl_pct"))
                         for c in top5}
        assert len(metrics_pairs) >= 2, f"Alle Kandidaten haben identische metrics: {metrics_pairs}"
        # Für nachfolgende Tests einen Kandidaten mit strategy_params merken
        chosen = with_params[0]
        pytest.optimize_result = {"job_id": job_id, "candidate": chosen, "result": res}

    def test_02_optimize_without_flag_has_empty_strategy_params(self, auth):
        job_id = _start_optimize(auth, {
            "scope": "combined", "regime_id": UP_LOW_VOL_REGIME,
            "mode": "params", "strategy_id": "nnfx_trend",
            "iterations": 6, "objective": "pnl", "min_trades": 3,
            "regime_walk_forward": False,
            # KEIN optimize_strategy_params
        })
        j = _wait_for_job(auth, job_id, timeout_s=360)
        assert j["status"] == "done", j
        top5 = (j.get("result") or {}).get("top5") or []
        assert len(top5) >= 1
        for c in top5:
            assert (c.get("strategy_params") or {}) == {}, \
                f"Ohne Flag darf strategy_params nicht befüllt sein: {c.get('strategy_params')}"
        # trade_params variieren weiterhin (mindestens ein Kandidat hat welche)
        assert any((c.get("trade_params") or {}) for c in top5), \
            "Ohne strategy_params-Flag sollten trade_params variieren"

    def test_03_optimize_include_flag_params_allows_binary(self, auth):
        """Mit include_flag_params=true DARF allow_long/allow_short vorkommen."""
        job_id = _start_optimize(auth, {
            "scope": "combined", "regime_id": UP_LOW_VOL_REGIME,
            "mode": "params", "strategy_id": "nnfx_trend",
            "iterations": 10, "objective": "pnl", "min_trades": 1,
            "optimize_strategy_params": True,
            "include_flag_params": True,
            "strategy_param_keys": ["allow_long", "allow_short"],
            "regime_walk_forward": False,
        })
        j = _wait_for_job(auth, job_id, timeout_s=360)
        assert j["status"] == "done", j
        top5 = (j.get("result") or {}).get("top5") or []
        keys_seen = set()
        for c in top5:
            keys_seen |= set((c.get("strategy_params") or {}).keys())
        # min. eines der Flag-Keys muss vorkommen können
        assert keys_seen & {"allow_long", "allow_short"}, \
            f"include_flag_params sollte Flag-Params zulassen, keys={keys_seen}"


# --------------------------------------------------------- Assign + build-nnfx Idempotenz (Fortsetzung obiger Klasse)
    def test_04_assign_persists_strategy_params(self, auth):
        cand = getattr(pytest, "optimize_result", {}).get("candidate")
        job_id = getattr(pytest, "optimize_result", {}).get("job_id")
        if not cand:
            pytest.skip("optimize job did not run – depends on TestStrategyParamsOptimize")
        # Snapshot vorheriger Assignment
        before = auth.get(f"{BASE}/api/regime-lab/{ANALYSIS_ID}", timeout=30).json()["analysis"]
        prev_a = ((before.get("assignments") or {}).get(f"combined:{UP_LOW_VOL_REGIME}") or {})
        prev_sp = prev_a.get("strategy_params") or {}
        # Assign mit strategy_params
        body = {"scope": "combined", "regime_id": UP_LOW_VOL_REGIME,
                "candidate": {
                    "mode": "params",
                    "strategy_id": "nnfx_trend",
                    "strategy_name": "NNFX Trend",
                    "definition": None,
                    "rules": [],
                    "trade_params": cand.get("trade_params") or {},
                    "strategy_params": cand.get("strategy_params") or {},
                    "metrics": cand.get("metrics"),
                    "validation": cand.get("validation"),
                    "source_job_id": job_id,
                }}
        r = auth.post(f"{BASE}/api/regime-lab/{ANALYSIS_ID}/assign", json=body, timeout=30)
        assert r.status_code == 200, r.text
        # Reload und prüfen
        after = auth.get(f"{BASE}/api/regime-lab/{ANALYSIS_ID}", timeout=30).json()["analysis"]
        a = (after.get("assignments") or {}).get(f"combined:{UP_LOW_VOL_REGIME}")
        assert a is not None
        assert a.get("strategy_params") == cand.get("strategy_params"), \
            f"strategy_params nicht gespeichert: got={a.get('strategy_params')} exp={cand.get('strategy_params')}"
        assert a.get("strategy_id") == "nnfx_trend"
        # Für spätere Tests merken
        pytest.assigned_params = a.get("strategy_params")
        pytest.prev_assignment_sp = prev_sp

    def test_05_build_nnfx_idempotent_and_preserves_strategy_params(self, auth):
        # Vorher: alle dyn-IDs mit framework=nnfx für diese Analyse aufsammeln
        dl_before = auth.get(f"{BASE}/api/dynamic/list", timeout=30).json()
        before_ids = {s["id"] for s in dl_before.get("strategies", [])
                      if s.get("framework") == "nnfx"}
        # Erster Aufruf
        r1 = auth.post(f"{BASE}/api/regime-lab/{ANALYSIS_ID}/build-nnfx",
                       json={"scope": "combined"}, timeout=60)
        assert r1.status_code == 200, r1.text
        d1 = r1.json()
        did1 = d1["id"]
        # Zweiter Aufruf sollte SELBE ID liefern (Idempotenz)
        r2 = auth.post(f"{BASE}/api/regime-lab/{ANALYSIS_ID}/build-nnfx",
                       json={"scope": "combined"}, timeout=60)
        assert r2.status_code == 200, r2.text
        did2 = r2.json()["id"]
        assert did1 == did2, f"build-nnfx nicht idempotent: {did1} != {did2}"
        # Assignments dürfen strategy_params NICHT verloren haben
        after = auth.get(f"{BASE}/api/regime-lab/{ANALYSIS_ID}", timeout=30).json()["analysis"]
        a = (after.get("assignments") or {}).get(f"combined:{UP_LOW_VOL_REGIME}")
        exp = getattr(pytest, "assigned_params", None)
        assert a is not None and a.get("strategy_params") == exp, \
            f"strategy_params nach build-nnfx verloren: got={a.get('strategy_params')} exp={exp}"
        # regime_strategies + regime_params im dyn-Doc
        dl_after = auth.get(f"{BASE}/api/dynamic/list", timeout=30).json()
        row = next((s for s in dl_after.get("strategies", []) if s["id"] == did1), None)
        assert row is not None, f"dyn {did1} nicht in /api/dynamic/list"
        assert row.get("framework") == "nnfx"
        assert row.get("regime_strategies"), "regime_strategies fehlt im dyn-Doc"
        # regime_params sollte den soeben zugeordneten Regime enthalten
        rp = row.get("regime_params") or {}
        if exp:
            assert rp.get(str(UP_LOW_VOL_REGIME)) == exp, \
                f"regime_params.{UP_LOW_VOL_REGIME} nicht übernommen: {rp}"
        pytest.dyn_id = did1


# --------------------------------------------------------- Refresh + Apply (Fortsetzung obiger Klasse)
    def test_06_refresh_no_500_and_apply_ok(self, auth):
        did = getattr(pytest, "dyn_id", None)
        if not did:
            pytest.skip("dyn_id missing – build-nnfx test must run first")
        r = auth.post(f"{BASE}/api/dynamic/{did}/refresh",
                      json={"days": 90}, timeout=180)
        assert r.status_code == 200, r.text
        d = r.json()
        # per_symbol muss BTC/ETH enthalten (keine Fehler-Only-Antwort)
        per = d.get("per_symbol") or {}
        assert per, f"Refresh liefert leere per_symbol: {d}"
        ok = [sym for sym, v in per.items() if not v.get("error")]
        assert ok, f"Refresh: alle Symbole liefern Fehler: {per}"
        # apply
        ra = auth.post(f"{BASE}/api/dynamic/{did}/apply", timeout=60)
        assert ra.status_code == 200, ra.text
        applied = ra.json().get("applied") or []
        assert isinstance(applied, list)
        # jeder applied-Eintrag enthält strategy_id (Multi-Strategy-Modus)
        for a in applied:
            assert "strategy_id" in a, f"applied ohne strategy_id: {a}"
            # strategy_params optional aber falls vorhanden sollte Dict sein
            if a.get("strategy_params") is not None:
                assert isinstance(a["strategy_params"], dict)
