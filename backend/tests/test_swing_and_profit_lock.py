"""Regressionstests: Swing-Trades, Profit-Lock (secure_profit), Exposure-Hinweis,
PnL-%-auf-Margin und Dust-Schutz."""
import pytest

from services.ai_trade_manager import (DEFAULT_SETTINGS, check_limits,
                                       exposure_text, trades_text)
from core.utils import _enrich_trade


def _trade(**kw):
    t = {"id": "t1", "symbol": "BTCUSDT", "side": "LONG", "mode": "paper",
         "entry": 100.0, "qty": 10.0, "qty_remaining": 10.0, "leverage": 10.0,
         "status": "open", "ai_actions": 0, "ai_last_action_ts": 0}
    t.update(kw)
    return t


class TestProfitLockLimits:
    def test_secure_profit_requires_profit(self):
        ok, why = check_limits(_trade(), "secure_profit", 50, dict(DEFAULT_SETTINGS),
                               in_profit=False)
        assert not ok and "Gewinn" in why

    def test_secure_profit_disabled(self):
        s = dict(DEFAULT_SETTINGS, profit_lock_enabled=False)
        ok, why = check_limits(_trade(), "secure_profit", 50, s, in_profit=True)
        assert not ok and "deaktiviert" in why

    def test_secure_profit_ok_within_bounds(self):
        # Margin = 10*100/10 = 100 USDT; 50% entnehmen -> 50 bleiben, Hebel 20x
        ok, why = check_limits(_trade(), "secure_profit", 50, dict(DEFAULT_SETTINGS),
                               in_profit=True)
        assert ok, why

    def test_secure_profit_pct_bounds(self):
        for bad in (0, 90, 100):
            ok, _ = check_limits(_trade(), "secure_profit", bad,
                                 dict(DEFAULT_SETTINGS), in_profit=True)
            assert not ok

    def test_secure_profit_min_margin_left(self):
        s = dict(DEFAULT_SETTINGS, profit_lock_min_margin_pct=30)
        ok, why = check_limits(_trade(), "secure_profit", 80, s, in_profit=True)
        assert not ok and "Margin" in why

    def test_remove_margin_leverage_cap_no_profit(self):
        # Margin 100, 60 entnehmen -> Hebel 25x (< max 50) -> ok
        ok, why = check_limits(_trade(), "remove_margin", 60,
                               dict(DEFAULT_SETTINGS), in_profit=False)
        assert ok, why
        # 85% Rest-Margin-Regel: 90 entnehmen -> 10 bleiben (10% < 15%) -> blockiert
        ok, why = check_limits(_trade(), "remove_margin", 90,
                               dict(DEFAULT_SETTINGS), in_profit=False)
        assert not ok

    def test_remove_margin_higher_cap_in_profit(self):
        # Hebel-Deckel steigt bei Gewinn auf profit_lock_max_leverage
        s = dict(DEFAULT_SETTINGS, max_leverage=20, profit_lock_max_leverage=100,
                 profit_lock_min_margin_pct=5)
        # 80 entnehmen -> Rest 20 -> Hebel 50x: ohne Profit blockiert, mit Profit ok
        ok, _ = check_limits(_trade(), "remove_margin", 80, s, in_profit=False)
        assert not ok
        ok, why = check_limits(_trade(), "remove_margin", 80, s, in_profit=True)
        assert ok, why

    def test_set_leverage_profit_cap(self):
        s = dict(DEFAULT_SETTINGS, max_leverage=50, profit_lock_max_leverage=100)
        ok, _ = check_limits(_trade(), "set_leverage", 80, s, in_profit=False)
        assert not ok
        ok, why = check_limits(_trade(), "set_leverage", 80, s, in_profit=True)
        assert ok, why

    def test_allow_margin_off_blocks_secure_profit(self):
        s = dict(DEFAULT_SETTINGS, allow_margin=False)
        ok, why = check_limits(_trade(), "secure_profit", 50, s, in_profit=True)
        assert not ok and "deaktiviert" in why


class TestExposureText:
    def test_empty(self):
        assert "keine offenen" in exposure_text([])

    def test_one_sided_warning(self):
        trades = [_trade(symbol=s) for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT")]
        txt = exposure_text(trades)
        assert "3 LONG / 0 SHORT" in txt and "einseitige LONG" in txt

    def test_cluster_detection(self):
        trades = [_trade(), _trade(), _trade(side="SHORT", symbol="ETHUSDT")]
        txt = exposure_text(trades)
        assert "BTCUSDT LONG x2" in txt and "einseitige" not in txt


class TestTradesTextHorizon:
    def test_swing_label(self):
        t = _trade(horizon="swing", runner=True, opened_at="2026-06-01T10:00:00")
        txt = trades_text([t], {"BTCUSDT": 101.0})
        assert "[SWING · RUNNER]" in txt

    def test_scalp_default(self):
        t = _trade(opened_at="2026-06-01T10:00:00")
        txt = trades_text([t], {"BTCUSDT": 101.0})
        assert "[SCALP]" in txt


class TestPnlPctMargin:
    def test_margin_basis_matches_bitunix(self):
        # Entry 100, qty 10, Hebel 10 -> Margin 100; Kurs 101 -> uPnL 10 = +10% auf Margin
        t = _trade(realized_pnl=0.0, opened_at="2026-06-01T10:00:00Z")
        e = _enrich_trade(t, current_price=101.0)
        c = e["computed"]
        assert c["margin_used"] == 100.0
        assert c["pnl_pct_margin"] == pytest.approx(10.0, abs=0.01)
        assert c["upnl_pct_margin"] == pytest.approx(10.0, abs=0.01)
        # Positionsgrößen-% bleibt davon unabhängig (1%)
        assert c["pnl_pct"] == pytest.approx(1.0, abs=0.01)

    def test_upnl_margin_matches_bitunix_after_partial(self):
        # Nach Teilschließung: Bitunix zeigt nur den unrealisierten PnL der
        # Restposition auf die Rest-Margin – realisierte Anteile bleiben außen vor.
        t = _trade(qty=10.0, qty_remaining=2.0, realized_pnl=-3.0,
                   opened_at="2026-06-01T10:00:00Z")
        e = _enrich_trade(t, current_price=101.0)
        c = e["computed"]
        # Rest-Margin = 2*100/10 = 20; uPnL = 2*1 = 2 -> +10%
        assert c["upnl_pct_margin"] == pytest.approx(10.0, abs=0.01)
        # Gesamt-PnL% (inkl. realisiert): (2-3)/20 = -5%
        assert c["pnl_pct_margin"] == pytest.approx(-5.0, abs=0.01)

    def test_margin_used_field_preferred(self):
        # Nach Profit-Lock: margin_used gepflegt (20 statt 100) -> % auf Rest-Margin
        t = _trade(realized_pnl=0.0, margin_used=20.0,
                   opened_at="2026-06-01T10:00:00Z")
        e = _enrich_trade(t, current_price=101.0)
        assert e["computed"]["pnl_pct_margin"] == pytest.approx(50.0, abs=0.01)

    def test_closed_trade_has_margin_pct(self):
        t = _trade(status="closed", realized_pnl=25.0, exit_price=102.5,
                   opened_at="2026-06-01T10:00:00Z", closed_at="2026-06-01T11:00:00Z")
        e = _enrich_trade(t)
        assert e["computed"]["pnl_pct_margin"] == pytest.approx(25.0, abs=0.01)


class TestSwingSignalClamps:
    """Swing-Grenzen in AIEngine._emit_signal (indirekt über die Konstanten-Logik)."""

    def test_swing_config_defaults(self):
        from services.ai_engine import DEFAULT_AI_CONFIG
        assert DEFAULT_AI_CONFIG["swing_enabled"] is True
        assert 1 <= DEFAULT_AI_CONFIG["swing_max_leverage"] <= 20

    def test_actions_contains_secure_profit(self):
        from services.ai_trade_manager import ACTIONS
        assert "secure_profit" in ACTIONS


class TestAnalysisGroups:
    def _engine(self, enabled=True):
        from services.ai_engine import AIEngine
        e = AIEngine.__new__(AIEngine)
        e.config = {"group_analysis": enabled}
        return e

    def test_grouping(self):
        groups = dict(self._engine()._analysis_groups(
            ["BTCUSDT", "ETHUSDT", "EURUSD", "USDJPY", "SPYUSDT", "GOLDUSDT"]))
        assert groups["Krypto"] == ["BTCUSDT", "ETHUSDT"]
        assert groups["Forex"] == ["EURUSD", "USDJPY"]
        assert groups["Indizes & Rohstoffe"] == ["SPYUSDT", "GOLDUSDT"]

    def test_disabled_returns_single_batch(self):
        res = self._engine(enabled=False)._analysis_groups(["BTCUSDT", "EURUSD"])
        assert len(res) == 1 and res[0][1] == ["BTCUSDT", "EURUSD"]

    def test_empty_groups_omitted(self):
        res = self._engine()._analysis_groups(["BTCUSDT"])
        assert [g for g, _ in res] == ["Krypto"]

    def test_group_analysis_config_default(self):
        from services.ai_engine import DEFAULT_AI_CONFIG
        assert DEFAULT_AI_CONFIG["group_analysis"] is True
