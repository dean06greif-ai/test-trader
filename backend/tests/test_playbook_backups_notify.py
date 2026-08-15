"""Regressionstests: Strategie-Playbook, Diversifikations-Guards,
Mehrfach-Backup-Keys und zusammengefasste Modell-Ausfall-Meldungen.

Reine Unit-Tests (keine DB, kein Server) – passend zur bestehenden Konvention,
dass Prüf-Logik modul-global und direkt testbar ist.
"""
import services.ai_playbook as pb
import services.ai_providers as ai_providers
from services.notifications import _fail_lines, summarize_model_failures


# ---------------- Backup-Keys (mehrere pro Provider) ----------------
def test_multiple_backup_keys_order(monkeypatch):
    monkeypatch.setenv("CEREBRAS_API_KEY", "k-primary")
    monkeypatch.setenv("CEREBRAS_API_KEY_BACKUP", "k-b1")
    monkeypatch.setenv("CEREBRAS_API_KEY_BACKUP2", "k-b2")
    monkeypatch.setenv("CEREBRAS_API_KEY_BACKUP3", "k-b3")
    assert ai_providers.provider_keys("cerebras") == ["k-primary", "k-b1", "k-b2", "k-b3"]
    assert ai_providers.primary_key("cerebras") == "k-primary"
    assert ai_providers.backup_keys_info()["cerebras"] is True


def test_single_backup_still_works(monkeypatch):
    monkeypatch.setenv("MISTRAL_API_KEY", "m-primary")
    monkeypatch.setenv("MISTRAL_API_KEY_BACKUP", "m-b1")
    monkeypatch.delenv("MISTRAL_API_KEY_BACKUP2", raising=False)
    assert ai_providers.provider_keys("mistral") == ["m-primary", "m-b1"]


def test_backup_key_counts(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "o-primary")
    monkeypatch.setenv("OPENROUTER_API_KEY_BACKUP", "o-b1")
    monkeypatch.setenv("OPENROUTER_API_KEY_BACKUP2", "o-b2")
    assert ai_providers.backup_key_counts()["openrouter"] == 2


# ---------------- Playbook: Setup-Normalisierung & Urteile ----------------
def test_normalize_setup():
    assert pb.normalize_setup("breakout") == "breakout"
    assert pb.normalize_setup("Mean Reversion") == "mean_reversion"
    assert pb.normalize_setup("liquidity sweep (ICT)") == "liquidity_sweep"
    assert pb.normalize_setup("SWING_TREND") == "swing_trend"
    assert pb.normalize_setup("hedge gegen Swing-Long") == "hedge"
    assert pb.normalize_setup("") is None
    assert pb.normalize_setup("völlig unbekannt xyz") == "other"


def test_verdict_for():
    assert pb.verdict_for(3, 3, 10.0) == "test"          # zu wenig Daten
    assert pb.verdict_for(10, 7, 25.0) == "bewährt"
    assert pb.verdict_for(10, 2, -30.0) == "schwach"
    assert pb.verdict_for(6, 2, -5.0) == "test" or pb.verdict_for(6, 2, -5.0) == "neutral"
    assert pb.verdict_for(10, 5, 2.0) == "neutral"


def test_setup_enum_covers_all_setups():
    for sid in pb.SETUPS:
        assert sid in pb.SETUP_ENUM


# ---------------- Diversifikations-Guards ----------------
def _open(symbol, side, entry):
    return {"symbol": symbol, "side": side, "entry": entry}


def test_direction_guard_blocks_fourth_same_side():
    trades = [_open("XRPUSDT", "LONG", 2.0), _open("DOGEUSDT", "LONG", 0.1),
              _open("ADAUSDT", "LONG", 0.5)]
    ok, why = pb.diversification_check(trades, "LINKUSDT", "LONG", 15.0,
                                       max_same_direction=3)
    assert not ok and "Richtungs-Guard" in why
    # Gegenrichtung (Hedge) bleibt immer erlaubt
    ok, _ = pb.diversification_check(trades, "LINKUSDT", "SHORT", 15.0,
                                     max_same_direction=3)
    assert ok


def test_direction_guard_disabled_with_zero():
    trades = [_open("BTCUSDT", "LONG", 100)] * 5
    ok, _ = pb.diversification_check(trades, "XRPUSDT", "LONG", 2.0,
                                     max_same_direction=0)
    assert ok


def test_cluster_guard_blocks_same_zone_entry():
    trades = [_open("BTCUSDT", "LONG", 100.0)]
    ok, why = pb.diversification_check(trades, "BTCUSDT", "LONG", 100.2,
                                       max_same_direction=0, min_dist_pct=0.5)
    assert not ok and "Cluster-Guard" in why
    # Genug Abstand -> erlaubt
    ok, _ = pb.diversification_check(trades, "BTCUSDT", "LONG", 102.0,
                                     max_same_direction=0, min_dist_pct=0.5)
    assert ok
    # Anderes (nicht korreliertes) Symbol oder Gegenrichtung -> erlaubt
    ok, _ = pb.diversification_check(trades, "XRPUSDT", "LONG", 100.2,
                                     max_same_direction=0, min_dist_pct=0.5)
    assert ok
    ok, _ = pb.diversification_check(trades, "BTCUSDT", "SHORT", 100.2,
                                     max_same_direction=0, min_dist_pct=0.5)
    assert ok


# ---------------- Korrelations-Guard ----------------
def test_correlation_guard_blocks_second_correlated_same_side():
    trades = [_open("BTCUSDT", "LONG", 60000)]
    ok, why = pb.diversification_check(trades, "ETHUSDT", "LONG", 3000,
                                       max_same_direction=3, min_dist_pct=0.5)
    assert not ok and "Korrelations-Guard" in why
    # Gegenrichtung (Hedge) bleibt erlaubt
    ok, _ = pb.diversification_check(trades, "ETHUSDT", "SHORT", 3000,
                                     max_same_direction=3, min_dist_pct=0.5)
    assert ok
    # Nicht-korrelierter Coin bleibt erlaubt
    ok, _ = pb.diversification_check(trades, "XRPUSDT", "LONG", 2.0,
                                     max_same_direction=3, min_dist_pct=0.5)
    assert ok
    # Guard abschaltbar
    ok, _ = pb.diversification_check(trades, "ETHUSDT", "LONG", 3000,
                                     max_same_direction=3, min_dist_pct=0.5,
                                     correlation_guard=False)
    assert ok


def test_correlated_trades_count_as_one_direction_risk():
    # BTC+ETH+SOL LONG (Altbestand) = 1 Risiko-Einheit -> XRP LONG noch erlaubt
    trades = [_open("BTCUSDT", "LONG", 60000), _open("ETHUSDT", "LONG", 3000),
              _open("SOLUSDT", "LONG", 150)]
    ok, _ = pb.diversification_check(trades, "XRPUSDT", "LONG", 2.0,
                                     max_same_direction=2, min_dist_pct=0)
    assert ok
    # Ohne Korrelations-Guard zählen sie als 3 -> Limit 2 blockiert
    ok, why = pb.diversification_check(trades, "XRPUSDT", "LONG", 2.0,
                                       max_same_direction=2, min_dist_pct=0,
                                       correlation_guard=False)
    assert not ok and "Richtungs-Guard" in why


# ---------------- Zusammengefasste Ausfall-Meldungen ----------------
def test_fail_lines_groups_by_provider_and_reason():
    failures = [
        {"model": "openrouter/a", "reason": "rate_limited", "detail": "429 free-models-per-day"},
        {"model": "openrouter/b", "reason": "rate_limited", "detail": "429 free-models-per-day"},
        {"model": "openrouter/c", "reason": "rate_limited", "detail": "429 free-models-per-day"},
        {"model": "groq/x", "reason": "error", "detail": "timeout"},
    ]
    lines = _fail_lines(failures)
    assert len(lines) == 2  # statt 4 Zeilen nur 2 Gruppen
    joined = "\n".join(lines)
    for name in ("a", "b", "c", "x"):
        assert name in joined  # jedes Modell bleibt benannt
    assert "openrouter" in lines[0] and "Rate-Limit" in lines[0]


def test_summarize_single_failure_keeps_detail():
    title, msg, meta = summarize_model_failures([
        {"role": "Analyst", "provider": "groq", "model": "gpt-oss-120b",
         "reason": "error", "detail": "boom", "fallback": None}])
    assert title == "KI-Warnung: Analyst"
    assert "groq/gpt-oss-120b" in msg and "boom" in msg


def test_summarize_many_failures_compact_but_precise():
    items = [
        {"role": "Analyst", "provider": "groq", "model": "gpt-oss-120b",
         "reason": "rate_limited", "detail": "429"},
        {"role": "Analyst", "provider": "groq", "model": "llama-3.3-70b",
         "reason": "rate_limited", "detail": "429"},
        {"role": "Trade-Manager", "provider": "groq", "model": "gpt-oss-20b",
         "reason": "rate_limited", "detail": "429"},
        # Duplikat wird entfernt
        {"role": "Analyst", "provider": "groq", "model": "gpt-oss-120b",
         "reason": "rate_limited", "detail": "429"},
    ]
    title, msg, meta = summarize_model_failures(items)
    assert "3 Modell-Ausfälle" in title
    assert msg.count("\n") <= 2  # eine Gruppen-Zeile + Abschluss-Satz
    # Welches Modell bei welchem Assistenten: bleibt sichtbar
    assert "Analyst: gpt-oss-120b, llama-3.3-70b" in msg
    assert "Trade-Manager: gpt-oss-20b" in msg
    assert meta.get("aggregated") is True
