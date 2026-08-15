"""Tests: Playbook-Statistik 'bester Regel-/Trade-Timeframe pro Setup'."""
from services.ai_playbook import best_tf_per_setup, tf_context_lines


ROWS = [
    {"setup": "breakout", "timeframe": "1m", "trades": 8, "wins": 3, "pnl": -4.0},
    {"setup": "breakout", "timeframe": "15m", "trades": 5, "wins": 4, "pnl": 12.5},
    {"setup": "breakout", "timeframe": "1h", "trades": 2, "wins": 2, "pnl": 30.0},
    {"setup": "trend_follow", "timeframe": "5m", "trades": 6, "wins": 4, "pnl": 7.0},
]


def test_best_tf_requires_min_trades():
    best = best_tf_per_setup(ROWS, min_trades=3)
    # 1h hat mehr PnL, aber nur 2 Trades -> 15m gewinnt
    assert best["breakout"]["timeframe"] == "15m"
    assert best["trend_follow"]["timeframe"] == "5m"


def test_best_tf_empty():
    assert best_tf_per_setup([]) == {}
    assert tf_context_lines([]) == []


def test_context_lines_sorted_by_pnl():
    lines = tf_context_lines(ROWS)
    assert lines[0].startswith("TIMEFRAME-PERFORMANCE")
    assert "breakout: bester TF 15m" in lines[1]
    assert "WR 80%" in lines[1]
    assert "trend_follow: bester TF 5m" in lines[2]
