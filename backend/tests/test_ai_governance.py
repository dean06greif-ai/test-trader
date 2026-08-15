"""Regressionstests für die neuen Governance-Bausteine des KI Traders.

Abgedeckt:
  * MasterPrompt: harte Regeln für Trades, Einstellungs-Änderungen, Lektionen
  * Lektionen: Trader-Edits (`locked`) sind für die KI unantastbar
  * Daten-Validierung: KI-Änderungen brauchen eine Mindest-Stichprobe
  * Strategie-Labor: Ghost-Statistik, Promotion-Schwellen, Ghost-Auswertung
"""
from services import ai_lessons
from services.ai_master_prompt import (DEFAULT_RULES, check_change_rules, check_lesson_rules,
                                       check_trade_rules, normalize_rules)
from services.ai_strategy_lab import ghost_outcome, ghost_stats, promotion_ready
from services.ai_validation import DEFAULT_SETTINGS, evaluate_change, evaluate_lesson


# ---------------- MasterPrompt ----------------
def test_blocked_symbol_and_side_are_rejected():
    rules = normalize_rules({"blocked_symbols": ["dogeusdt"], "allowed_sides": ["LONG"]})
    ok, why = check_trade_rules(rules, "DOGEUSDT", "LONG")
    assert not ok and "gesperrt" in why
    ok, why = check_trade_rules(rules, "BTCUSDT", "SHORT")
    assert not ok and "SHORT" in why
    assert check_trade_rules(rules, "BTCUSDT", "LONG")[0]


def test_confidence_leverage_and_open_trade_limits():
    rules = normalize_rules({"min_confidence": 70, "max_leverage": 10, "max_open_trades": 2})
    assert not check_trade_rules(rules, "BTCUSDT", "LONG", confidence=60)[0]
    assert not check_trade_rules(rules, "BTCUSDT", "LONG", confidence=80, leverage=20)[0]
    assert not check_trade_rules(rules, "BTCUSDT", "LONG", confidence=80, open_trades=2)[0]
    assert check_trade_rules(rules, "BTCUSDT", "LONG", confidence=80, leverage=5,
                             open_trades=1)[0]


def test_config_change_against_master_prompt_is_blocked():
    rules = normalize_rules({"max_leverage": 10, "min_confidence": 60})
    assert not check_change_rules(rules, {"leverage": 25})[0]
    assert not check_change_rules(rules, {"auto_lev_max": 40})[0]
    assert not check_change_rules(rules, {"min_confidence": 50})[0]
    assert check_change_rules(rules, {"leverage": 8})[0]


def test_lesson_against_master_prompt_is_blocked():
    rules = normalize_rules({"max_leverage": 10, "blocked_symbols": ["DOGEUSDT"]})
    assert not check_lesson_rules(rules, "Mehr Hebel", "Bei Trends Hebel 30x nutzen")[0]
    assert not check_lesson_rules(rules, "DOGE", "DOGE lässt sich gut long traden")[0]
    assert check_lesson_rules(rules, "Enger SL", "SL bei 0.4% funktioniert besser")[0]


def test_defaults_are_permissive_enough_for_existing_workflows():
    ok, _ = check_trade_rules(DEFAULT_RULES, "BTCUSDT", "SHORT", confidence=55, leverage=10,
                              open_trades=3)
    assert ok


# ---------------- Lektionen ----------------
def _lesson(title, **kw):
    return {"title": title, "detail": f"detail {title}", **kw}


def test_locked_lessons_survive_ai_removal_and_overwrite():
    old = [_lesson("Trader-Regel", locked=True, origin="user", weight=4),
           _lesson("KI-Regel", weight=2)]
    merged = ai_lessons.merge_lessons(
        old, [_lesson("Trader-Regel", detail="von KI umformuliert")],
        removed=["Trader-Regel", "KI-Regel"], max_lessons=10)
    titles = [m["title"] for m in merged]
    assert titles.count("Trader-Regel") == 1
    assert "KI-Regel" not in titles           # KI darf ihre eigene Lektion verwerfen
    kept = [m for m in merged if m["title"] == "Trader-Regel"][0]
    assert kept["detail"] == "detail Trader-Regel"   # Trader-Text unverändert
    assert kept["locked"] is True


def test_locked_lessons_do_not_count_against_the_limit():
    old = [_lesson(f"T{i}", locked=True) for i in range(3)] + \
          [_lesson(f"A{i}") for i in range(10)]
    merged = ai_lessons.merge_lessons(old, [], [], max_lessons=5)
    assert len([m for m in merged if m.get("locked")]) == 3
    assert len(merged) == 8


def test_lessons_text_marks_trader_lessons():
    txt = ai_lessons.lessons_text([_lesson("Trader-Regel", locked=True, origin="user"),
                                   _lesson("KI-Regel")])
    assert "VOM TRADER" in txt and "Gewicht" in txt


def test_normalize_adds_ids_to_legacy_lessons():
    out = ai_lessons.normalize_all([{"title": "alt", "detail": "x"}])
    assert out[0]["id"].startswith("les_") and out[0]["origin"] == "ai"
    assert out[0]["locked"] is False


# ---------------- Daten-Validierung ----------------
def test_engine_change_needs_minimum_sample():
    stats = {"totals": {"closed_trades": 4}}
    res = evaluate_change(DEFAULT_SETTINGS, stats, "engine")
    assert not res["validated"] and res["required"] == DEFAULT_SETTINGS["min_closed_trades"]
    ok = evaluate_change(DEFAULT_SETTINGS, {"totals": {"closed_trades": 30}}, "engine")
    assert ok["validated"]


def test_coin_change_uses_symbol_sample():
    stats = {"totals": {"closed_trades": 100}, "by_symbol": {"BTCUSDT": {"trades": 2}}}
    assert not evaluate_change(DEFAULT_SETTINGS, stats, "coin", "BTCUSDT")["validated"]
    stats["by_symbol"]["BTCUSDT"]["trades"] = 20
    assert evaluate_change(DEFAULT_SETTINGS, stats, "coin", "BTCUSDT")["validated"]


def test_lesson_removal_needs_more_data_than_creation():
    stats = {"totals": {"closed_trades": 6}}
    assert evaluate_lesson(DEFAULT_SETTINGS, stats)["validated"]
    assert not evaluate_lesson(DEFAULT_SETTINGS, stats, removal=True)["validated"]


def test_validation_can_be_switched_off():
    off = {**DEFAULT_SETTINGS, "enabled": False}
    assert evaluate_change(off, {"totals": {"closed_trades": 0}}, "engine")["validated"]


# ---------------- Strategie-Labor ----------------
def test_ghost_stats_and_promotion_threshold():
    trades = [{"status": "closed", "result": "win", "pnl_pct": 1.0} for _ in range(12)] + \
             [{"status": "closed", "result": "loss", "pnl_pct": -0.5} for _ in range(8)] + \
             [{"status": "open"}]
    st = ghost_stats(trades)
    assert st["trades"] == 20 and st["wins"] == 12 and st["win_rate"] == 60.0
    assert st["open"] == 1
    assert promotion_ready(st, {"min_ghost_trades": 20, "min_ghost_winrate": 55.0})
    assert not promotion_ready(st, {"min_ghost_trades": 25, "min_ghost_winrate": 55.0})
    assert not promotion_ready(st, {"min_ghost_trades": 20, "min_ghost_winrate": 65.0})


def test_ghost_outcome_respects_direction():
    assert ghost_outcome("LONG", 105, 95, 104) == "win"
    assert ghost_outcome("LONG", 94, 95, 104) == "loss"
    assert ghost_outcome("SHORT", 96, 105, 97) == "win"
    assert ghost_outcome("SHORT", 106, 105, 97) == "loss"
    assert ghost_outcome("LONG", 100, 95, 104) is None
