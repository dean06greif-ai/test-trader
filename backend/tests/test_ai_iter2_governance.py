"""Regressionstests für die zweite Ausbaustufe des KI Traders.

Abgedeckt:
  * Analyse-Zeitplan (Intervall je Zeitfenster, auch über Mitternacht)
  * Telegram-Spam-Bremse (Sperrzeit + Preis-Toleranz)
  * Makro-Parameter: Schrittbegrenzung + Mehrfach-Bestätigung
  * MasterPrompt: Lektions-Grundregeln, verbotene Begriffe, Tages-Risikolimits
  * Provider-Health (Limit-/Fallback-Anzeige)
  * Strategie-Labor: Ghost-Timeout wird in der Statistik getrennt gezählt
"""
from services import ai_providers, notify_guard
from services.ai_master_prompt import (check_day_rules, check_lesson_rules, normalize_rules)
from services.ai_schedule import (effective_interval, normalize_schedule, schedule_text,
                                  window_matches)
from services.ai_strategy_lab import ghost_stats
from services.ai_validation import DEFAULT_SETTINGS, clamp_step, evaluate_macro, is_macro_key


# ---------------- Zeitplan ----------------
def test_schedule_windows_and_default():
    sched = [{"from": "22:00", "to": "06:00", "interval_min": 30, "label": "Nacht"},
             {"from": "15:00", "to": "18:00", "interval_min": 5, "label": "US"}]
    assert effective_interval(sched, 10, 23 * 60) == (30, "Nacht")     # 23:00
    assert effective_interval(sched, 10, 2 * 60) == (30, "Nacht")      # 02:00 (über Mitternacht)
    assert effective_interval(sched, 10, 16 * 60) == (5, "US")         # 16:00
    assert effective_interval(sched, 10, 12 * 60) == (10, "Standard")  # 12:00


def test_disabled_window_is_ignored():
    sched = [{"from": "15:00", "to": "18:00", "interval_min": 5, "enabled": False}]
    assert effective_interval(sched, 12, 16 * 60) == (12, "Standard")


def test_invalid_windows_are_dropped():
    sched = normalize_schedule([{"from": "25:00", "to": "06:00", "interval_min": 30},
                                {"from": "08:00", "to": "08:00", "interval_min": 5},
                                {"from": "09:00", "to": "10:00", "interval_min": 7}])
    assert len(sched) == 1 and sched[0]["interval_min"] == 7


def test_window_matches_edges():
    w = {"from": "15:00", "to": "18:00"}
    assert window_matches(w, 15 * 60)
    assert not window_matches(w, 18 * 60)


def test_schedule_text_mentions_default():
    txt = schedule_text([{"from": "22:00", "to": "06:00", "interval_min": 30}], 10)
    assert "22:00-06:00 alle 30 min" in txt and "sonst alle 10 min" in txt


# ---------------- Telegram-Bremse ----------------
def test_repeated_signal_is_suppressed_within_cooldown():
    last = {"ts": 1000.0, "price": 100.0}
    ok, reason = notify_guard.should_notify(last, 1060.0, 900, 100.02)
    assert not ok and "gleiches Setup" in reason


def test_new_price_level_is_notified_again():
    last = {"ts": 1000.0, "price": 100.0}
    ok, _ = notify_guard.should_notify(last, 1060.0, 900, 101.5)
    assert ok


def test_notification_after_cooldown():
    last = {"ts": 1000.0, "price": 100.0}
    assert notify_guard.should_notify(last, 2000.0, 900, 100.0)[0]


def test_guard_state_flow():
    notify_guard.reset()
    sig = {"symbol": "BTCUSDT", "strategy_id": "scalping_4_rules", "type": "LONG",
           "entry": 50000.0}
    assert notify_guard.check(sig, 15)[0] is True
    assert notify_guard.check(dict(sig), 15)[0] is False       # gleiches Setup
    other = {**sig, "type": "SHORT"}
    assert notify_guard.check(other, 15)[0] is True            # andere Richtung
    notify_guard.reset()


# ---------------- Makro-Parameter ----------------
def test_macro_keys_recognised():
    assert is_macro_key("sl_fixed_percent") and is_macro_key("tp1_crv")
    assert not is_macro_key("max_capital")


def test_single_trade_cannot_move_stop_loss_far():
    value, clamped = clamp_step("sl_fixed_percent", 0.4, 2.4, 20)
    assert clamped and value <= 0.5


def test_small_changes_pass_unclamped():
    value, clamped = clamp_step("tp1_crv", 1.5, 1.6, 20)
    assert not clamped and value == 1.6


def test_clamp_respects_hard_bounds():
    value, _ = clamp_step("leverage", 48, 90, 100)
    assert value <= 50


def test_macro_needs_sample_and_confirmations():
    assert not evaluate_macro(DEFAULT_SETTINGS, 10, 5)["validated"]     # zu wenig Trades
    assert not evaluate_macro(DEFAULT_SETTINGS, 40, 1)["validated"]     # zu wenig Bestätigungen
    assert evaluate_macro(DEFAULT_SETTINGS, 40, 3)["validated"]


# ---------------- MasterPrompt: Lektionen + Tageslimits ----------------
def test_forbidden_terms_block_lessons():
    rules = normalize_rules({"forbidden_terms": ["all-in"]})
    ok, why = check_lesson_rules(rules, "Mut", "Bei starkem Trend all-in gehen")
    assert not ok and "all-in" in why


def test_daily_risk_limits():
    rules = {"max_daily_loss_usdt": 20, "max_trades_per_day": 5}
    assert not check_day_rules(rules, -21, 1)[0]
    assert not check_day_rules(rules, 5, 5)[0]
    assert check_day_rules(rules, -5, 2)[0]
    assert check_day_rules({}, -1000, 500)[0]     # ohne Limits keine Sperre


# ---------------- Provider-Health ----------------
def test_provider_health_reports_fallback_and_limits():
    ai_providers._health.clear()
    ai_providers._last_call.clear()
    ai_providers.record_result("gemini", "gemini-3.1-pro-preview", "rate_limited", "429 quota")
    ai_providers.record_result("gemini", "gemini-3.5-flash", "ok", key_index=0,
                               requested="gemini-3.1-pro-preview")
    h = ai_providers.health_status()
    assert h["fallback_active"] is True
    assert h["last_call"]["model"] == "gemini-3.5-flash"
    assert any(m["model"] == "gemini-3.1-pro-preview" for m in h["rate_limited"])
    assert h["rate_limited"][0]["cooldown_left_s"] > 0
    ai_providers._health.clear()
    ai_providers._last_call.clear()


# ---------------- Ghost-Statistik ----------------
def test_expired_ghost_trades_are_not_counted_as_results():
    trades = [{"status": "closed", "result": "win", "pnl_pct": 1.0},
              {"status": "expired", "result": "expired"},
              {"status": "open"}]
    st = ghost_stats(trades)
    assert st["trades"] == 1 and st["expired"] == 1 and st["open"] == 1
    assert st["win_rate"] == 100.0
