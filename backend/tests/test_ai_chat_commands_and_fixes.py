"""Regressionstests für die KI-Trader-Verbesserungen (Iteration Chat-Kommandos):

  * clamp_level: SL/TP auf falscher Kursseite wird automatisch korrigiert
    (Bug: "ADJUST_SL ... SL 73.62 liegt auf der falschen Seite des Preises 73.64")
  * Chat-Kommandos: Vorerkennung + Lektions-/Trade-Matching (rein)
  * Strategie-Assistent: Validierung maschinenlesbarer Regel-Definitionen
  * Validierung: neue Schwelle min_lesson_confirmations
"""
from services.ai_chat_commands import looks_like_command, match_lesson, match_trades
from services.ai_strategy_lab import valid_rule_definition
from services.ai_trade_manager import clamp_level, resolve_price
from services.ai_validation import DEFAULT_SETTINGS


# ---------------- clamp_level (ADJUST_SL-Bug) ----------------
def test_clamp_sl_short_wrong_side_is_corrected():
    # Original-Bug: SHORT, Kurs 73.64, KI will SL 73.62 (unter dem Kurs = ungültig)
    price, clamped = clamp_level("adjust_sl", "SHORT", 73.62, 73.64)
    assert clamped is True
    assert price > 73.64


def test_clamp_sl_long_wrong_side_is_corrected():
    price, clamped = clamp_level("adjust_sl", "LONG", 100.5, 100.0)
    assert clamped is True
    assert price < 100.0


def test_clamp_sl_valid_values_unchanged():
    price, clamped = clamp_level("adjust_sl", "SHORT", 75.0, 73.64)
    assert clamped is False and price == 75.0
    price, clamped = clamp_level("adjust_sl", "LONG", 99.0, 100.0)
    assert clamped is False and price == 99.0


def test_clamp_tp_wrong_side_is_corrected():
    price, clamped = clamp_level("adjust_tp", "LONG", 99.0, 100.0)
    assert clamped is True and price > 100.0
    price, clamped = clamp_level("adjust_tp", "SHORT", 74.0, 73.64)
    assert clamped is True and price < 73.64


def test_clamp_handles_bad_input():
    price, clamped = clamp_level("adjust_sl", "LONG", None, 100.0)
    assert clamped is False
    price, clamped = clamp_level("adjust_sl", "LONG", 99.0, 0)
    assert clamped is False and price == 99.0


def test_resolve_price_unchanged():
    # Bestehendes Verhalten bleibt (Rückwärtskompatibilität)
    assert resolve_price("adjust_sl", 50.0, None, "LONG", 100.0) == 50.0
    assert resolve_price("adjust_sl", None, 1.0, "LONG", 100.0) == 99.0
    assert resolve_price("adjust_sl", None, 1.0, "SHORT", 100.0) == 101.0


# ---------------- Chat-Kommandos ----------------
def test_looks_like_command_detects_close_and_lessons():
    assert looks_like_command("Schließe bitte alle Paper-Positionen")
    assert looks_like_command("close all live positions")
    assert looks_like_command("Füge eine Lektion hinzu: keine Shorts nach 22 Uhr")
    assert looks_like_command("Setze den SL bei SOL auf 73.9")
    assert looks_like_command("ändere den Hebel auf 5x")


def test_looks_like_command_ignores_questions():
    assert not looks_like_command("Wie ist die Marktlage heute?")
    assert not looks_like_command("Was denkst du über BTC?")


def test_match_lesson_by_id_title_and_partial():
    lessons = [
        {"id": "les_a1", "title": "Keine Shorts nach 22 Uhr"},
        {"id": "les_b2", "title": "BTC nur mit Trend handeln"},
    ]
    assert match_lesson(lessons, "les_a1")["id"] == "les_a1"
    assert match_lesson(lessons, "btc nur mit trend handeln")["id"] == "les_b2"
    assert match_lesson(lessons, "shorts nach 22")["id"] == "les_a1"
    assert match_lesson(lessons, "gibt es nicht") is None
    assert match_lesson(lessons, "") is None


def test_match_trades_filters_mode_symbol_side():
    trades = [
        {"id": "t1", "mode": "paper", "symbol": "BTCUSDT", "side": "LONG"},
        {"id": "t2", "mode": "paper", "symbol": "SOLUSDT", "side": "SHORT"},
        {"id": "t3", "mode": "live", "symbol": "BTCUSDT", "side": "LONG"},
    ]
    assert {t["id"] for t in match_trades(trades, "paper", "ALL", "ALL")} == {"t1", "t2"}
    assert [t["id"] for t in match_trades(trades, "all", "BTCUSDT", "ALL")] == ["t1", "t3"]
    assert [t["id"] for t in match_trades(trades, "paper", "SOLUSDT", "SHORT")] == ["t2"]
    assert match_trades(trades, "live", "SOLUSDT", "ALL") == []


# ---------------- Strategie-Assistent ----------------
def test_valid_rule_definition():
    good = {"timeframe": "1m",
            "long_rules": [{"indicator": "rsi", "op": "<", "value": 30}]}
    assert valid_rule_definition(good)
    assert valid_rule_definition({"short_rules": [{"indicator": "ema_fast",
                                                   "op": "cross_below", "value": 0}]})
    assert not valid_rule_definition(None)
    assert not valid_rule_definition({})
    assert not valid_rule_definition({"long_rules": []})
    assert not valid_rule_definition({"long_rules": [{"op": "<"}]})  # indicator fehlt
    assert not valid_rule_definition("rsi < 30")


# ---------------- Validierung ----------------
def test_validation_has_lesson_confirmation_threshold():
    assert DEFAULT_SETTINGS["min_lesson_confirmations"] >= 1
    # bestehende Schwellen bleiben unverändert (Rückwärtskompatibilität)
    assert DEFAULT_SETTINGS["min_lesson_results"] == 5
    assert DEFAULT_SETTINGS["min_removal_results"] == 12
