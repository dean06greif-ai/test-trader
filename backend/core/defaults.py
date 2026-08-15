"""Default-Konfigurationen für Strategie-Overrides (aus server.py verschoben)."""

# ---- NEW: Strategy-level auto-trade override ----
# Mirrors the coin-level DEFAULT_COIN_CFG so a strategy can fully override
# the trade parameters. `mode` here is per-strategy ('live'|'paper'|'off')
# and takes precedence over the global config mode. 'off' disables the
# strategy entirely (no trades, no signal notifications override happens
# through signals_enabled).
DEFAULT_STRATEGY_OVERRIDE = {
    "enabled": False,
    "mode": "off",  # "live" | "paper" | "off"
    "signals_enabled": True,  # Bell toggle for signal notifications
    # Trade sizing
    "max_capital": 100.0,
    "leverage": 10,
    # SL config
    "sl_mode": "structure",       # structure | fixed
    "sl_fixed_percent": 1.0,
    "sl_ticks": 4,
    "sl_lookback": 10,
    # TP config
    "tp1_crv": 1.0,
    "tp1_close_percent": 50,
    "tp_full_crv": 2.0,
    "breakeven_enabled": True,
    "be_mode": "tp1",
    "be_trigger_crv": 1.0,
    "be_trigger_profit_pct": 30.0,
    "be_smart_lookback": 10,
    "require_all_rules": False,
    "fee_percent": 0.06,
    "trade_pre_signals": False,
    "profit_secure_enabled": False,
    "profit_secure_trigger_pct": 30.0,
    "profit_lock_pct": 50.0,
    "auto_leverage_enabled": False,
    "auto_lev_mode": "liq_pct",
    "auto_lev_value": 0.5,
    "auto_lev_max": 50,
}

# ── PER-COIN-PER-STRATEGY CONFIG ─────────────────────────────────────────────
DEFAULT_STRATEGY_COIN_CFG: dict = {
    "enabled": False,
    "mode": "off",
    "signals_enabled": True,
    "max_capital": 100.0,
    "leverage": 10,
    "order_type": "MARKET",
    "sl_mode": "structure",
    "sl_fixed_percent": 1.0,
    "sl_ticks": 4,
    "sl_lookback": 10,
    "tp1_crv": 1.0,
    "tp1_close_percent": 50,
    "tp_full_crv": 2.0,
    "breakeven_enabled": True,
    "be_mode": "tp1",
    "be_trigger_crv": 1.0,
    "be_trigger_profit_pct": 30.0,
    "be_smart_lookback": 10,
    "require_all_rules": False,
    "fee_percent": 0.06,
    "trade_pre_signals": False,
    "profit_secure_enabled": False,
    "profit_secure_trigger_pct": 30.0,
    "profit_lock_pct": 50.0,
    "auto_leverage_enabled": False,
    "auto_lev_mode": "liq_pct",
    "auto_lev_value": 0.5,
    "auto_lev_max": 50,
}

# Trade-Parameter, die der Optimizer in Live/Paper/Backtest-Configs schreiben darf
OPT_TRADE_KEYS = ("tp1_crv", "tp_full_crv", "tp1_close_percent", "sl_lookback",
                  "sl_mode", "sl_fixed_percent", "atr_sl_multiplier",
                  "be_mode", "be_trigger_crv", "be_trigger_profit_pct",
                  "profit_secure_enabled", "profit_secure_trigger_pct", "profit_lock_pct",
                  "leverage", "auto_leverage_enabled", "auto_lev_mode",
                  "auto_lev_value", "auto_lev_max", "sessions")
