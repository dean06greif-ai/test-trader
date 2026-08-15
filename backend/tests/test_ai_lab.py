"""Regressionstests für das neue KI-Ökosystem (Gedächtnis, Forschungs-Analyst,
ML-Labor, Markt-Beobachter, Rollen-Voreinstellungen).

Getestet werden die reinen Funktionen sowie die Rückwärtskompatibilität der
Rollen-Verwaltung – ohne DB, ohne LLM, ohne Netzwerk.
"""
import asyncio
from datetime import datetime, timezone, timedelta

import pytest

from services import ai_market_observer as obs
from services import ai_ml_lab as mlab
from services import ai_research as res
from services import ai_roles
from services.ai_memory import KnowledgeStore, supabase_config


# ---------------- Markt-Beobachter: Feature-Berechnung ----------------
def _candles(n=120, start=100.0, step=0.1, vol=10.0):
    out = []
    price = start
    for i in range(n):
        price += step
        out.append({"timestamp": 1700000000000 + i * 60000, "open": price - step,
                    "high": price + 0.05, "low": price - 0.05, "close": price,
                    "volume": vol})
    return out


def test_compute_features_needs_enough_candles():
    assert obs.compute_features([]) is None
    assert obs.compute_features(_candles(30)) is None


def test_compute_features_uptrend():
    f = obs.compute_features(_candles(140, step=0.2))
    assert f is not None
    assert f["trend_pct"] > 0
    assert 0 <= f["rsi"] <= 100
    assert f["regime"].startswith("trend_up")
    for k in ("price", "atr_pct", "volatility_pct", "volume_ratio", "range_pos"):
        assert k in f


def test_classify_regime_branches():
    assert obs.classify_regime(0.5, 0.5, 50).startswith("trend_up_volatil")
    assert obs.classify_regime(-0.5, 0.01, 50).startswith("trend_down_ruhig")
    assert obs.classify_regime(0.0, 0.1, 50).startswith("range_")
    assert obs.classify_regime(0.0, 0.1, 95).startswith("breakout_")


def test_snapshot_to_text():
    f = obs.compute_features(_candles(140))
    txt = obs.snapshot_to_text({"symbol": "BTCUSDT", "features": f})
    assert "BTCUSDT" in txt and "RSI" in txt


# ---------------- Forschungs-Analyst: Digests ----------------
def test_digest_backtests_empty_and_filled():
    assert "keine Backtests" in res.digest_backtests([])
    rows = [{
        "created_at": "2026-06-01T10:00:00+00:00",
        "params": {"symbols": ["BTCUSDT", "ETHUSDT"], "timeframe": "5m", "days": 30},
        "result": {"days": 30, "per_strategy": [
            {"strategy_id": "scalping", "strategy_name": "Scalping", "pnl": 12.5,
             "pnl_pct": 12.5, "trades": 40, "win_rate": 60.0,
             "max_drawdown_pct": 4.2, "timeframe": "5m"},
            {"strategy_id": "macd", "strategy_name": "MACD", "pnl": -3.0,
             "pnl_pct": -3.0, "trades": 12, "win_rate": 33.3,
             "max_drawdown_pct": 8.0, "timeframe": "15m"}],
            "best_per_symbol": {"BTCUSDT": {"strategy_name": "Scalping", "pnl": 9.0}}},
    }]
    txt = res.digest_backtests(rows)
    assert "Scalping" in txt and "MACD" in txt
    assert "Winrate 60.0%" in txt
    assert "beste Strategie pro Coin" in txt


def test_digest_optimizer_reports_holdout_and_params():
    rows = [{"created_at": "2026-06-02T08:00:00+00:00", "result": {
        "mode": "params", "objective": "pnl", "days": 60, "timeframe": "5m",
        "strategy_name": "EMA Pullback", "symbols": ["BTCUSDT"],
        "top5": [{"metrics": {"pnl": 30.0, "win_rate": 55.0, "trades": 80,
                              "max_drawdown": 6.0},
                  "test_metrics": {"pnl": 8.0, "win_rate": 51.0},
                  "wf": {"score": 0.7}, "constancy": {"deviation_pct": 12.0},
                  "params": {"ema_fast": 9, "ema_slow": 21},
                  "trade_params": {"tp1_crv": 1.4},
                  "passed": True, "rank_reason": "robust über alle Fenster"}]}}]
    txt = res.digest_optimizer(rows)
    assert "Holdout" in txt and "ema_fast=9" in txt
    assert "bestanden: ja" in txt
    assert "keine Optimizer" in res.digest_optimizer([])


def test_digest_regime_runs_and_analyses():
    runs = [{"result": {"kind": "regime_opt", "regime_id": 1, "regime_label": "Trend",
                        "scope": "combined", "mode": "params", "timeframe": "5m",
                        "segments_info": {"segments": 12, "days": 40.0},
                        "top5": [{"metrics": {"pnl": 5.0, "win_rate": 58.0, "trades": 25},
                                  "validation_passed": True,
                                  "params": {"rsi_period": 14}}]}},
            {"result": {"kind": "walkforward", "analysis_name": "A1", "scope": "combined",
                        "dynamic": {"pnl": 7.0}, "best_single": {"metrics": {"pnl": 4.0}},
                        "verdict": "Kombination schlägt Einzelstrategie"}}]
    txt = res.digest_regime_runs(runs)
    assert "Trend" in txt and "Walk-Forward" in txt
    analyses = [{"id": "a1", "name": "Analyse 1", "timeframe": "5m",
                 "created_at": "2026-05-01T00:00:00+00:00",
                 "combined": {"model": {"regimes": [{"id": 0, "label": "ruhig", "share_pct": 40}]}}}]
    atxt = res.digest_regime_analyses(analyses)
    assert "Analyse 1" in atxt and "ruhig" in atxt


def test_build_prompt_contains_all_sections():
    p = res.build_prompt({"backtests": "b", "optimizer": "o", "regime_runs": "r",
                          "regime_analyses": "ra", "live_performance": "lp"},
                         {"backtests": 2, "optimizer_runs": 1,
                          "regime_lab_runs": 0, "regime_analyses": 0},
                         previous={"insights": [{"title": "T", "detail": "D"}]})
    for token in ("BACKTESTS", "OPTIMIZER-LÄUFE", "REGIME-LAB", "REGIME-ANALYSEN",
                  "LIVE-PERFORMANCE", "FRÜHEREN ERKENNTNISSE", "2 Backtests"):
        assert token in p


# ---------------- ML-Labor: Datensatz ----------------
def test_label_of():
    assert mlab.label_of({"trade_pnl": 2.5}) == 1
    assert mlab.label_of({"trade_pnl": -1.0}) == 0
    assert mlab.label_of({"outcome": "win"}) == 1
    assert mlab.label_of({"result": "loss"}) == 0
    assert mlab.label_of({"outcome": "breakeven"}) is None
    assert mlab.label_of({}) is None


def test_nearest_snapshot_respects_gap():
    t = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    snaps = [{"ts": (t - timedelta(minutes=5)).isoformat(), "features": {"rsi": 55}},
             {"ts": (t - timedelta(minutes=120)).isoformat(), "features": {"rsi": 20}}]
    assert nearest_rsi(snaps, t) == 55
    far = [{"ts": (t - timedelta(minutes=300)).isoformat(), "features": {"rsi": 20}}]
    assert mlab.nearest_snapshot(far, t) is None


def nearest_rsi(snaps, t):
    s = mlab.nearest_snapshot(snaps, t)
    return (s or {}).get("features", {}).get("rsi")


def test_feature_row_uses_market_state_and_order():
    row = mlab.feature_row(
        {"confidence": 80, "action": "LONG", "ts": "2026-06-01T12:00:00+00:00",
         "sl_pct": 1.0, "tp1_pct": 2.0, "news_impact": "positive", "rsi": 40},
        {"features": {"rsi": 61, "trend_pct": 0.2, "volatility_pct": 0.1,
                      "atr_pct": 0.05, "volume_ratio": 1.5, "range_pos": 70,
                      "change_60m_pct": 1.2}})
    assert row["side_long"] == 1.0
    assert row["rsi"] == 61            # Marktzustand hat Vorrang
    assert row["crv"] == 2.0
    assert row["news_score"] == 1.0
    assert row["hour_utc"] == 12.0
    assert set(mlab.FEATURES) <= set(row)


def test_build_dataset_filters_and_counts():
    ts = "2026-06-01T12:00:00+00:00"
    decisions = [
        {"symbol": "BTCUSDT", "action": "LONG", "confidence": 70, "ts": ts, "outcome": "win"},
        {"symbol": "BTCUSDT", "action": "SHORT", "confidence": 60, "ts": ts, "outcome": "loss"},
        {"symbol": "BTCUSDT", "action": "HOLD", "confidence": 50, "ts": ts, "outcome": "win"},
        {"symbol": "ETHUSDT", "action": "LONG", "confidence": 65, "ts": ts},  # offen
    ]
    snaps = [{"symbol": "BTCUSDT", "ts": ts, "features": {"rsi": 55, "trend_pct": 0.1}}]
    X, y, meta = mlab.build_dataset(decisions, snaps)
    assert meta["samples"] == 2 and y == [1, 0]
    assert meta["with_market_state"] == 2
    assert len(X) == 2 and X[0]["rsi"] == 55


def test_to_matrix_shape():
    X, _y, _m = mlab.build_dataset(
        [{"symbol": "BTCUSDT", "action": "LONG", "ts": "2026-06-01T12:00:00+00:00",
          "outcome": "win"}], [])
    m = mlab.to_matrix(X)
    assert m.shape == (1, len(mlab.FEATURES))


def test_importances_text():
    assert "rsi 40.0%" in mlab.importances_text([{"feature": "rsi", "share_pct": 40.0}])
    assert mlab.importances_text([]) == "(keine)"


def test_train_sync_produces_model_when_libs_present():
    ok, _err = mlab.libs_available()
    if not ok:
        pytest.skip("optuna/xgboost nicht installiert")
    rows, labels = [], []
    for i in range(80):
        win = i % 2 == 0
        rows.append(mlab.feature_row(
            {"confidence": 80 if win else 40, "action": "LONG",
             "ts": "2026-06-01T12:00:00+00:00", "sl_pct": 1.0, "tp1_pct": 2.0},
            {"features": {"rsi": 60 if win else 30, "trend_pct": 0.3 if win else -0.3,
                          "volatility_pct": 0.1, "atr_pct": 0.05,
                          "volume_ratio": 1.2, "range_pos": 60,
                          "change_60m_pct": 0.5 if win else -0.5}}))
        labels.append(1 if win else 0)
    out = mlab.train_sync(rows, labels, n_trials=5, timeout_sec=60)
    assert out["samples"] == 80
    assert 0.0 <= out["cv_auc"] <= 1.0
    assert out["booster_b64"]
    assert len(out["importances"]) == len(mlab.FEATURES)


# ---------------- Rollen: Voreinstellungen & Rückwärtskompatibilität ----------------
class _FakeSettings:
    def __init__(self, doc=None):
        self.doc = doc
        self.saved = None

    async def find_one(self, _q):
        return dict(self.doc) if self.doc else None

    async def update_one(self, _q, update, upsert=False):
        self.saved = update.get("$set")
        return None


class _FakeDB:
    def __init__(self, doc=None):
        self.settings = _FakeSettings(doc)


def test_default_roles_have_presets_for_every_role():
    for role in ai_roles.ROLE_LABELS:
        assert role in ai_roles.DEFAULT_ROLES_CONFIG
        cfg = ai_roles.DEFAULT_ROLES_CONFIG[role]
        assert cfg["model"], f"{role} ohne Voreinstellung"
        assert cfg["user_configured"] is False


def test_new_roles_registered():
    for role in ("research_analyst", "market_observer"):
        assert role in ai_roles.ROLE_LABELS
        assert role in ai_roles.DEFAULT_ROLES_CONFIG


def test_load_keeps_preset_when_user_never_configured():
    mgr = ai_roles.AIRoleManager()
    stored = {"analyst": {"enabled": False, "provider": None, "model": None}}
    asyncio.run(mgr.load(_FakeDB(stored)))
    cfg = mgr.role_cfg("analyst")
    assert cfg["enabled"] is False                      # Nutzer-Einstellung übernommen
    assert cfg["model"] == ai_roles.ROLE_PRESETS["analyst"]["model"]  # Preset bleibt


def test_load_respects_user_choice():
    mgr = ai_roles.AIRoleManager()
    stored = {"analyst": {"provider": "mistral", "model": "mistral-small-latest",
                          "user_configured": True}}
    asyncio.run(mgr.load(_FakeDB(stored)))
    cfg = mgr.role_cfg("analyst")
    assert cfg["provider"] == "mistral" and cfg["model"] == "mistral-small-latest"


def test_update_marks_user_configured_and_reset_restores_preset():
    mgr = ai_roles.AIRoleManager()
    db = _FakeDB()
    asyncio.run(mgr.update(db, {"analyst": {"provider": "mistral",
                                            "model": "ministral-8b-latest"}}))
    assert mgr.role_cfg("analyst")["user_configured"] is True
    assert mgr.role_cfg("analyst")["model"] == "ministral-8b-latest"
    asyncio.run(mgr.reset_role(db, "analyst"))
    assert mgr.role_cfg("analyst")["model"] == ai_roles.ROLE_PRESETS["analyst"]["model"]
    assert mgr.role_cfg("analyst")["user_configured"] is False


def test_research_role_specific_fields_are_sanitized():
    mgr = ai_roles.AIRoleManager()
    asyncio.run(mgr.update(_FakeDB(), {"research_analyst": {
        "schedule_times": ["07:00", "bad", "19:30"], "interval_hours": 999,
        "auto_on_new_results": False, "trigger_after_results": 0}}))
    cfg = mgr.role_cfg("research_analyst")
    assert cfg["schedule_times"] == ["07:00", "19:30"]
    assert cfg["interval_hours"] == 168
    assert cfg["auto_on_new_results"] is False
    assert cfg["trigger_after_results"] == 1


def test_market_observer_fields():
    mgr = ai_roles.AIRoleManager()
    asyncio.run(mgr.update(_FakeDB(), {"market_observer": {"interval_min": 1,
                                                           "llm_summary": True}}))
    cfg = mgr.role_cfg("market_observer")
    assert cfg["interval_min"] == 5          # geklemmt
    assert cfg["llm_summary"] is True


def test_chain_appends_available_providers(monkeypatch):
    from services import ai_providers
    monkeypatch.setattr(ai_providers, "available_providers",
                        lambda: {"gemini": False, "mistral": True, "groq": False,
                                 "openrouter": False, "github": False, "cerebras": False})
    mgr = ai_roles.AIRoleManager()
    chain = mgr.chain("analyst", {"provider": "gemini", "model": "gemini-3.5-flash"})
    providers = [p for p, _m in chain]
    assert "mistral" in providers, "Provider mit Key muss als Rettung angehängt werden"
    assert providers[0] == "gemini", "Voreinstellung bleibt erste Wahl"


def test_in_active_hours_unchanged():
    hours = {"start": "22:00", "end": "06:00"}
    assert ai_roles.in_active_hours(hours, datetime(2026, 6, 1, 23, 0)) is True
    assert ai_roles.in_active_hours(hours, datetime(2026, 6, 1, 12, 0)) is False
    assert ai_roles.in_active_hours(None) is True


# ---------------- KI-Gedächtnis ----------------
class _FakeCollection:
    def __init__(self):
        self.docs = []

    async def insert_one(self, doc):
        self.docs.append(doc)

    def find(self, query=None):
        rows = [d for d in self.docs
                if not query or all(d.get(k) == v for k, v in query.items()
                                    if not isinstance(v, dict))]
        return _FakeCursor(rows)

    async def count_documents(self, query):
        if not query:
            return len(self.docs)
        return len([d for d in self.docs if d.get("kind") == query.get("kind")])

    async def delete_many(self, _q):
        class R:
            deleted_count = 0
        return R()


class _FakeCursor:
    def __init__(self, rows):
        self.rows = rows

    def sort(self, *_a, **_k):
        self.rows = list(reversed(self.rows))
        return self

    def limit(self, n):
        self.rows = self.rows[:n]
        return self

    async def to_list(self, _n):
        return [dict(r) for r in self.rows]


class _MemDB:
    def __init__(self):
        self.ai_knowledge = _FakeCollection()


def test_knowledge_store_remember_and_recall_without_supabase(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    monkeypatch.delenv("SUPABASE_SECRET_KEY", raising=False)
    monkeypatch.delenv("SUPABASE_KEY", raising=False)
    assert supabase_config() is None
    store = KnowledgeStore()
    store.setup(_MemDB())
    assert store.mirror is None
    entry = asyncio.run(store.remember("research_insight", "T1", "Inhalt",
                                       tags=["research"], weight=3))
    assert entry["kind"] == "research_insight" and entry["weight"] == 3
    rows = asyncio.run(store.recall(kind="research_insight", limit=5))
    assert rows and rows[0]["title"] == "T1"
    stats = asyncio.run(store.stats())
    assert stats["total"] == 1 and stats["mirror"] is None


def test_knowledge_store_remember_many_and_context_text(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    store = KnowledgeStore()
    store.setup(_MemDB())
    n = asyncio.run(store.remember_many("idea", [
        {"title": "Idee A", "detail": "Test A"},
        {"title": "Idee B", "detail": "Test B"},
        {"no_title": True},
    ]))
    assert n == 2
    txt = asyncio.run(store.context_text(kinds=["idea"], per_kind=5))
    assert "Idee A" in txt and "Idee B" in txt


def test_supabase_config_strips_rest_suffix(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://abc.supabase.co/rest/v1/")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "secret")
    cfg = supabase_config()
    assert cfg["base"] == "https://abc.supabase.co"
    assert cfg["table"]


# ---------------- Trade-Manager: Limits & Preis-Auflösung ----------------
from services import ai_trade_manager as tmgr          # noqa: E402
from services import ai_closed_loop as cloop           # noqa: E402
from services.bitunix_trade import AutoTradeManager   # noqa: E402


def _trade(**over):
    t = {"id": "BTCUSDT-1", "symbol": "BTCUSDT", "side": "LONG", "mode": "paper",
         "entry": 100.0, "qty": 1.0, "qty_remaining": 1.0, "leverage": 10.0,
         "sl": 99.0, "tp1": 102.0, "tpf": 104.0, "ai_actions": 0,
         "ai_last_action_ts": 0, "margin_used": 10.0}
    t.update(over)
    return t


def test_check_limits_allows_and_blocks():
    s = dict(tmgr.DEFAULT_SETTINGS)
    assert tmgr.check_limits(_trade(), "hold", None, s)[0] is True
    assert tmgr.check_limits(_trade(), "close", None, s)[0] is True
    assert tmgr.check_limits(_trade(), "unknown", None, s)[0] is False
    # Aktions-Limit
    assert tmgr.check_limits(_trade(ai_actions=99), "close", None, s)[0] is False
    # Cooldown
    import time as _t
    assert tmgr.check_limits(_trade(ai_last_action_ts=_t.time()), "close", None, s)[0] is False
    # Hebel-Grenze
    assert tmgr.check_limits(_trade(), "set_leverage", 999, s)[0] is False
    assert tmgr.check_limits(_trade(), "set_leverage", 20, s)[0] is True
    # Margin-Regeln
    assert tmgr.check_limits(_trade(), "add_margin", 5, s)[0] is True
    assert tmgr.check_limits(_trade(), "add_margin", 999, s)[0] is False
    assert tmgr.check_limits(_trade(), "remove_margin", 50, s)[0] is False
    # partial_close Bereich
    assert tmgr.check_limits(_trade(), "partial_close", 50, s)[0] is True
    assert tmgr.check_limits(_trade(), "partial_close", 150, s)[0] is False
    # Margin-Sperre
    s_off = {**s, "allow_margin": False}
    assert tmgr.check_limits(_trade(), "add_margin", 5, s_off)[0] is False


def test_resolve_price_absolute_and_pct():
    assert tmgr.resolve_price("adjust_sl", 98.5, None, "LONG", 100) == 98.5
    assert tmgr.resolve_price("adjust_sl", None, 1.0, "LONG", 100) == 99.0
    assert tmgr.resolve_price("adjust_sl", None, 1.0, "SHORT", 100) == 101.0
    assert tmgr.resolve_price("adjust_tp", None, 2.0, "LONG", 100) == 102.0
    assert tmgr.resolve_price("adjust_tp", None, 2.0, "SHORT", 100) == 98.0
    assert tmgr.resolve_price("adjust_tp", None, None, "LONG", 100) is None


def test_trades_text_contains_key_numbers():
    txt = tmgr.trades_text([_trade(events=["OPEN LONG @ 100"])], {"BTCUSDT": 101.0})
    assert "BTCUSDT LONG" in txt and "Hebel 10.0x" in txt and "uPnL" in txt
    assert "keine offenen Trades" in tmgr.trades_text([], {})


def test_liq_price_moves_with_leverage():
    high = AutoTradeManager.liq_price_for("LONG", 100.0, 20)
    low = AutoTradeManager.liq_price_for("LONG", 100.0, 5)
    assert low < high < 100          # weniger Hebel => Liquidation weiter weg
    assert AutoTradeManager.liq_price_for("SHORT", 100.0, 10) > 100


# ---------------- Closed Loop: Kandidatenwahl ----------------
def test_pick_candidate_prefers_optimizer_run():
    runs = [{"created_at": "2026-06-02T00:00:00+00:00",
             "result": {"strategy_id": "ema_pullback_scalping", "symbols": ["BTCUSDT"],
                        "timeframe": "5m"}}]
    bts = [{"created_at": "2026-06-01T00:00:00+00:00",
            "params": {"symbols": ["ETHUSDT"]},
            "result": {"per_strategy": [{"strategy_id": "rsi_only", "pnl": 5}]}}]
    cand = cloop.pick_candidate(runs, bts)
    assert cand["strategy_id"] == "ema_pullback_scalping"
    cand2 = cloop.pick_candidate([], bts)
    assert cand2["strategy_id"] == "rsi_only" and cand2["symbols"] == ["ETHUSDT"]
    assert cloop.pick_candidate([], []) is None


def test_closed_loop_defaults_off():
    assert cloop.DEFAULT_SETTINGS["enabled"] is False


def test_trade_manager_role_registered():
    assert "trade_manager" in ai_roles.ROLE_LABELS
    assert ai_roles.DEFAULT_ROLES_CONFIG["trade_manager"]["model"]
