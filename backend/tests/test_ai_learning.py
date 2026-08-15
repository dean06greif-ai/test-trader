"""Regressionstests: KI-Lernen, Einstellungs-Autonomie, Whitelist-Validierung."""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.ai_knowledge import (validate_changes, tunable_spec_text,
                                   AI_TUNABLE_COIN_KEYS, AI_TUNABLE_ENGINE_KEYS)
from services.ai_learning import aggregate_performance, performance_to_text
from services.ai_engine import AIEngine, DEFAULT_AI_CONFIG


# ---------------- Whitelist / Validation ----------------

def test_max_capital_is_always_forbidden():
    valid, rejected = validate_changes({"max_capital": 500, "leverage": 5})
    assert "max_capital" not in valid
    assert "max_capital" in rejected
    assert valid == {"leverage": 5}


def test_mode_and_enabled_forbidden():
    valid, rejected = validate_changes({"mode": "live", "enabled": True, "signals_enabled": False})
    assert valid == {}
    assert set(rejected) == {"mode", "enabled", "signals_enabled"}


def test_numbers_are_clamped_to_bounds():
    valid, _ = validate_changes({"leverage": 500, "sl_fixed_percent": 0.01, "tp1_crv": "2.5"})
    assert valid["leverage"] == AI_TUNABLE_COIN_KEYS["leverage"]["max"]
    assert valid["sl_fixed_percent"] == AI_TUNABLE_COIN_KEYS["sl_fixed_percent"]["min"]
    assert valid["tp1_crv"] == 2.5


def test_enum_and_bool_and_unknown():
    valid, rejected = validate_changes({
        "sl_mode": "atr", "be_mode": "banana", "breakeven_enabled": "true", "foo": 1})
    assert valid["sl_mode"] == "atr"
    assert valid["breakeven_enabled"] is True
    assert "be_mode" in rejected and "foo" in rejected


def test_engine_scope_keys():
    valid, rejected = validate_changes({"min_confidence": 20, "cooldown_min": 999,
                                        "leverage": 5}, scope="engine")
    assert valid["min_confidence"] == AI_TUNABLE_ENGINE_KEYS["min_confidence"]["min"]
    assert valid["cooldown_min"] == AI_TUNABLE_ENGINE_KEYS["cooldown_min"]["max"]
    assert "leverage" in rejected


def test_spec_text_mentions_keys():
    txt = tunable_spec_text()
    assert "leverage" in txt and "min_confidence" in txt and "sl_mode" in txt


# ---------------- Config-Defaults ----------------

def test_default_config_has_new_keys():
    for k in ("autonomy", "learning_enabled", "learn_on_trade_close",
              "learning_lookback_days", "max_lessons", "use_ai_levels"):
        assert k in DEFAULT_AI_CONFIG
    assert DEFAULT_AI_CONFIG["autonomy"] == "suggest"


# ---------------- Aggregation ----------------

def _mk_signal(sym="BTCUSDT", typ="LONG", result=None, conf=70):
    return {"symbol": sym, "type": typ, "result": result, "ai_confidence": conf,
            "signal_class": "SIGNAL"}


def _mk_trade(sym="BTCUSDT", mode="paper", pnl=1.0, status="closed"):
    return {"symbol": sym, "mode": mode, "realized_pnl": pnl, "status": status}


def test_aggregate_performance_basic():
    signals = [_mk_signal(result="win", conf=85), _mk_signal(result="loss", conf=60),
               _mk_signal(typ="SHORT", result="win", conf=72), _mk_signal(result=None)]
    trades = [_mk_trade(pnl=5.0), _mk_trade(pnl=-2.0, mode="live"),
              _mk_trade(sym="ETHUSDT", pnl=3.0), _mk_trade(status="open", pnl=0)]
    stats = aggregate_performance(signals, trades)
    t = stats["totals"]
    assert t["signals"] == 4 and t["signal_wins"] == 2 and t["signal_losses"] == 1
    assert t["signal_win_rate"] == round(2 / 3 * 100, 1)
    assert t["closed_trades"] == 3 and t["open_trades"] == 1
    assert stats["trades"]["paper"]["pnl"] == 8.0
    assert stats["trades"]["live"]["pnl"] == -2.0
    assert stats["confidence_buckets"][">=80"]["wins"] == 1
    assert stats["confidence_buckets"]["<70"]["losses"] == 1
    assert stats["by_action"]["SHORT"]["wins"] == 1
    assert stats["best_symbol"] == "BTCUSDT" or stats["best_symbol"] == "ETHUSDT"


def test_aggregate_ignores_pre_signals():
    stats = aggregate_performance([{"signal_class": "PRE_SIGNAL", "symbol": "X",
                                    "type": "LONG", "result": "win"}], [])
    assert stats["totals"]["signals"] == 0


def test_performance_text_renders():
    stats = aggregate_performance([_mk_signal(result="win")], [_mk_trade(pnl=2.0)])
    stats["lookback_days"] = 14
    txt = performance_to_text(stats)
    assert "Winrate" in txt and "Paper" in txt


# ---------------- _handle_config_changes (FakeDB) ----------------

class FakeCollection:
    def __init__(self):
        self.docs = []

    def _match(self, doc, q):
        return all(doc.get(k) == v for k, v in q.items())

    async def insert_one(self, d):
        self.docs.append(dict(d))

    async def find_one(self, q, **kw):
        for d in self.docs:
            if self._match(d, q):
                return dict(d)
        return None

    async def update_one(self, q, u, upsert=False):
        for d in self.docs:
            if self._match(d, q):
                d.update(u.get("$set", {}))
                return
        if upsert:
            nd = dict(q)
            nd.update(u.get("$set", {}))
            self.docs.append(nd)

    async def count_documents(self, q):
        def ok(doc):
            for k, v in q.items():
                if isinstance(v, dict):
                    if "$exists" in v:
                        parts = k.split(".")
                        cur = doc
                        for part in parts:
                            cur = (cur or {}).get(part) if isinstance(cur, dict) else None
                        if bool(cur is not None) != bool(v["$exists"]):
                            return False
                        continue
                    if "$gte" in v and str(doc.get(k, "")) < str(v["$gte"]):
                        return False
                    continue
                if doc.get(k) != v:
                    return False
            return True
        return len([d for d in self.docs if ok(d)])

    async def update_many(self, q, u):
        for d in self.docs:
            if self._match(d, q):
                d.update(u.get("$set", {}))

    async def replace_one(self, q, d, upsert=False):
        for i, old in enumerate(self.docs):
            if self._match(old, q):
                self.docs[i] = dict(d)
                return
        if upsert:
            self.docs.append(dict(d))


class FakeDB:
    def __init__(self):
        self.settings = FakeCollection()
        self.ai_proposals = FakeCollection()
        self.ai_chat = FakeCollection()
        self.strategy_coin_configs = FakeCollection()


class FakeLearning:
    """Liefert die Datenbasis, die die neue Validierung (ai_validation.py) prüft."""

    def __init__(self, closed=50, per_symbol=25):
        self.closed = closed
        self.per_symbol = per_symbol

    async def gather_stats(self):
        return {"totals": {"closed_trades": self.closed},
                "by_symbol": {s: {"trades": self.per_symbol}
                              for s in ("BTCUSDT", "ETHUSDT")}}


def _engine(autonomy, learning=True, closed=50, per_symbol=25):
    e = AIEngine()
    e.db = FakeDB()
    e.symbols = ["BTCUSDT", "ETHUSDT"]
    e.config["autonomy"] = autonomy
    # Ausreichende Datenbasis: sonst parkt die Validierung die Änderung
    e.learning = FakeLearning(closed, per_symbol) if learning else None
    return e


def _confirm(e, scope, symbol, key, direction="down", times=3):
    """Frühere Vorschläge derselben Richtung simulieren (Bestätigungen)."""
    for i in range(times):
        e.db.ai_proposals.docs.append({
            "id": f"seed{i}", "scope": scope, "symbol": symbol,
            "changes": {key: 1}, "macro_direction": direction,
            "ts": "2999-01-01T00:00:00+00:00"})


def test_change_without_enough_data_is_parked():
    e = _engine("auto", closed=1, per_symbol=1)
    res = asyncio.run(e._handle_config_changes(
        [{"symbol": "BTCUSDT", "changes": {"tp1_close_percent": 40},
          "reason": "Bauchgefühl"}]))
    assert res[0]["status"] in ("needs_data", "needs_confirmation")
    assert e.db.strategy_coin_configs.docs == []   # nichts angewendet


def test_macro_change_needs_multiple_confirmations():
    e = _engine("auto")
    res = asyncio.run(e._handle_config_changes(
        [{"symbol": "BTCUSDT", "changes": {"sl_fixed_percent": 0.6},
          "reason": "ein Trade lief ins Minus"}]))
    assert res[0]["status"] == "needs_confirmation"
    assert e.db.strategy_coin_configs.docs == []
    # Nach genügend Bestätigungen greift die Änderung – aber nur in kleinen Schritten
    _confirm(e, "coin", "BTCUSDT", "sl_fixed_percent", direction="up", times=3)
    res2 = asyncio.run(e._handle_config_changes(
        [{"symbol": "BTCUSDT", "changes": {"sl_fixed_percent": 2.4},
          "reason": "mehrfach bestätigt"}]))
    assert res2[0]["status"] == "auto_applied"
    doc = asyncio.run(e.db.strategy_coin_configs.find_one({"_id": "ai_trader_BTCUSDT"}))
    assert doc["config"]["sl_fixed_percent"] <= 1.2   # Schrittweite begrenzt (Default 1.0)


def test_user_change_bypasses_validation():
    e = _engine("auto", closed=0, per_symbol=0)
    res = asyncio.run(e._handle_config_changes(
        [{"symbol": "BTCUSDT", "changes": {"leverage": 8}, "reason": "Trader will das"}],
        source="user"))
    assert res[0]["changes"]["leverage"] == 8
    assert res[0]["status"] == "auto_applied"


def test_change_against_master_prompt_is_blocked():
    from services.ai_master_prompt import master_prompt
    prev = dict(master_prompt.rules)
    master_prompt.rules = {**prev, "max_leverage": 5}
    try:
        e = _engine("auto")
        res = asyncio.run(e._handle_config_changes(
            [{"symbol": "BTCUSDT", "changes": {"leverage": 20}, "reason": "mehr Hebel"}]))
        assert res[0]["status"] == "blocked_master"
        assert e.db.strategy_coin_configs.docs == []
    finally:
        master_prompt.rules = prev


def test_suggest_mode_creates_pending_proposal():
    e = _engine("suggest")
    _confirm(e, "coin", "BTCUSDT", "leverage", direction="down")
    res = asyncio.run(e._handle_config_changes(
        [{"symbol": "BTCUSDT", "changes": {"leverage": 8, "max_capital": 999},
          "reason": "test"}], source="analysis"))
    assert len(res) == 1
    p = res[0]
    assert p["status"] == "pending"
    assert p["changes"] == {"leverage": 8}
    assert "max_capital" not in p["changes"]
    # Nichts wurde angewendet
    assert e.db.strategy_coin_configs.docs == []
    # Proposal + Chat-Eintrag persistiert
    assert len([d for d in e.db.ai_proposals.docs
                if not str(d["id"]).startswith("seed")]) == 1
    assert e.db.ai_chat.docs[0]["role"] == "config"


def test_auto_mode_applies_immediately():
    e = _engine("auto")
    _confirm(e, "coin", "BTCUSDT", "leverage", direction="down")
    res = asyncio.run(e._handle_config_changes(
        [{"symbol": "BTCUSDT", "changes": {"leverage": 8}, "reason": "test"}]))
    assert res[0]["status"] == "auto_applied"
    doc = asyncio.run(e.db.strategy_coin_configs.find_one({"_id": "ai_trader_BTCUSDT"}))
    assert doc["config"]["leverage"] == 8


def test_autonomy_off_ignores_changes():
    e = _engine("off")
    res = asyncio.run(e._handle_config_changes(
        [{"symbol": "BTCUSDT", "changes": {"leverage": 8}}]))
    assert res == []
    assert e.db.ai_proposals.docs == []


def test_unknown_symbol_skipped():
    e = _engine("auto")
    res = asyncio.run(e._handle_config_changes(
        [{"symbol": "PEPEUSDT", "changes": {"leverage": 8}}]))
    assert res == []


def test_noop_changes_skipped():
    e = _engine("suggest")
    # leverage 10 ist bereits Default -> kein Vorschlag
    res = asyncio.run(e._handle_config_changes(
        [{"symbol": "BTCUSDT", "changes": {"leverage": 10}}]))
    assert res == []


def test_engine_scope_change():
    e = _engine("auto")
    _confirm(e, "engine", "ENGINE", "min_confidence", direction="up")
    res = asyncio.run(e._handle_config_changes(
        [{"symbol": "ENGINE", "changes": {"min_confidence": 75}, "reason": "zu viele Fehlsignale"}]))
    assert res[0]["status"] == "auto_applied"
    # Struktur-Parameter wandern nur in kleinen Schritten (65 -> max. 70)
    assert 65 < e.config["min_confidence"] <= 75


def test_approve_pending_proposal():
    e = _engine("suggest")
    _confirm(e, "coin", "ETHUSDT", "sl_fixed_percent", direction="up")
    res = asyncio.run(e._handle_config_changes(
        [{"symbol": "ETHUSDT", "changes": {"sl_fixed_percent": 1.5}}]))
    pid = res[0]["id"]
    prop = asyncio.run(e.decide_proposal(pid, True))
    assert prop["status"] == "applied"
    doc = asyncio.run(e.db.strategy_coin_configs.find_one({"_id": "ai_trader_ETHUSDT"}))
    assert 1.0 < doc["config"]["sl_fixed_percent"] <= 1.5   # Schritt begrenzt
    # Doppelt entscheiden geht nicht
    assert asyncio.run(e.decide_proposal(pid, True)) is None


def test_reject_pending_proposal():
    e = _engine("suggest")
    _confirm(e, "coin", "ETHUSDT", "tp1_crv", direction="up")
    res = asyncio.run(e._handle_config_changes(
        [{"symbol": "ETHUSDT", "changes": {"tp1_crv": 2.0}}]))
    pid = res[0]["id"]
    prop = asyncio.run(e.decide_proposal(pid, False))
    assert prop["status"] == "rejected"
    doc = asyncio.run(e.db.strategy_coin_configs.find_one({"_id": "ai_trader_ETHUSDT"}))
    assert doc is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
