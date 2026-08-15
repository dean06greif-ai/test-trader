"""
Strategy Scanner - evaluates ALL enabled strategies per coin.
Tracks live rule-state (for circle pre-fill) and emits full signals.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Dict, List, Optional
import logging
from services.technical_indicators import TechnicalIndicators
from services.timeframes import TIMEFRAMES, aggregate_candles
from strategies.registry import registry

logger = logging.getLogger(__name__)
BERLIN = ZoneInfo("Europe/Berlin")

DEFAULT_SETTINGS = {
    "active_strategy": "scalping_4_rules",          # kept for backwards-compat
    "enabled_strategies": ["scalping_4_rules", "pbd_model"],     # tabs shown in dashboard
    "strategy_signals_enabled": {},                 # {strategy_id: bool} missing => on
    "strategy_params": {},                          # {strategy_id: {param: value}}
    "coin_params": {},                              # {strategy_id: {symbol: {param: value}}}
    "strategy_timeframes": {},                      # {strategy_id: "5m" | "1h" | ...}
    "deleted_strategies": [],                       # permanently hidden/removed strategy ids
    "custom_sessions": [],                          # empty => 24/7 (global)
    "strategy_sessions": {},                        # {strategy_id: [sessions]} überschreibt global
    "pre_signal_enabled": True,
    "notifications": {},                            # {symbol: bool}
    # Sperrzeit (Minuten) für wiederholte Telegram-Meldungen desselben Setups
    "notify_cooldown_min": 15,
}


class StrategyScanner:
    def __init__(self):
        self.indicators = TechnicalIndicators()
        self.candle_buffer = {}
        self.forming = {}
        self.last_pre_signal = {}
        self.rule_states = {}   # {symbol: {strategy_id: {...}}}
        self.settings = dict(DEFAULT_SETTINGS)

    # ---------- setup ----------
    def strategy_timeframe(self, strategy_id: str) -> str:
        tf = self.settings.get("strategy_timeframes", {}).get(strategy_id)
        if not tf:
            strat = registry.get(strategy_id)
            tf = getattr(strat, "STRATEGY_TIMEFRAME", "1m") if strat else "1m"
        return tf if tf in TIMEFRAMES else "1m"

    def buffer_limit(self) -> int:
        mx = 1
        rule_mx = 0
        for sid in self.enabled_strategies():
            mx = max(mx, TIMEFRAMES.get(self.strategy_timeframe(sid), 1))
            rule_mx = max(rule_mx, self.rule_timeframe_minutes(sid))
        # 220 Kerzen je Timeframe (statt 140), Deckel 21 Tage 1m-Historie:
        # nötig, damit 2h-Strategien (Trend-Surfer, EMA200-Warmup ~200 Kerzen)
        # live genug aggregierte Kerzen haben.
        limit = min(220 * mx, 30240)
        if rule_mx:
            # Regel-Timeframe-Overrides: mind. 60 Kerzen des höchsten Regel-TF,
            # Deckel 30 Tage (RAM) – 1d-Filter haben damit live ~30 Kerzen.
            limit = min(max(limit, 60 * rule_mx), 43200)
        return limit

    def rule_timeframe_minutes(self, strategy_id: str) -> int:
        """Höchster Regel-TF-Override (Minuten) einer Custom-Strategie, sonst 0."""
        strat = registry.get(strategy_id)
        definition = getattr(strat, "definition", None) if strat else None
        if not isinstance(definition, dict):
            return 0
        mx = 0
        for side in ("long_rules", "short_rules"):
            for r in definition.get(side) or []:
                if isinstance(r, dict) and r.get("timeframe"):
                    mx = max(mx, TIMEFRAMES.get(r["timeframe"], 0))
        return mx

    def bootstrap(self, symbol: str, candles: list):
        if not candles:
            return
        self.candle_buffer[symbol] = candles[-self.buffer_limit():]
        self.forming[symbol] = dict(candles[-1])

    def update_settings(self, new_settings: Dict):
        for key, value in new_settings.items():
            if key in DEFAULT_SETTINGS:
                self.settings[key] = value
        # keep active_strategy inside enabled list
        enabled = self.settings.get("enabled_strategies") or ["scalping_4_rules"]
        if self.settings.get("active_strategy") not in enabled and enabled:
            self.settings["active_strategy"] = enabled[0]

    def is_notify_enabled(self, symbol: str) -> bool:
        return self.settings.get("notifications", {}).get(symbol, True)

    def enabled_strategies(self) -> List[str]:
        ids = self.settings.get("enabled_strategies") or ["scalping_4_rules"]
        deleted = set(self.settings.get("deleted_strategies", []))
        return [s for s in ids if registry.get(s) and s not in deleted]

    def is_signals_enabled(self, strategy_id: str) -> bool:
        return self.settings.get("strategy_signals_enabled", {}).get(strategy_id, True)

    # ---------- time / session ----------
    @staticmethod
    def berlin_now() -> datetime:
        return datetime.now(BERLIN)

    @staticmethod
    def berlin_date() -> str:
        return datetime.now(BERLIN).strftime("%Y-%m-%d")

    def _parse_time(self, s: str) -> tuple:
        try:
            p = s.split(":")
            return (int(p[0]), int(p[1]))
        except Exception:
            return (0, 0)

    def sessions_for(self, strategy_id: str = None) -> list:
        """Pro-Strategie Zeitfenster; fällt auf globale Sessions zurück."""
        if strategy_id:
            per = self.settings.get("strategy_sessions", {}).get(strategy_id)
            if per:
                return [s for s in per if s.get("enabled", True)]
        return [s for s in self.settings.get("custom_sessions", []) if s.get("enabled", True)]

    def is_trading_session(self, strategy_id: str = None) -> bool:
        sessions = self.sessions_for(strategy_id)
        if not sessions:
            return True
        now = self.berlin_now()
        cur = (now.hour, now.minute)
        for s in sessions:
            if self._parse_time(s.get("start", "00:00")) <= cur < self._parse_time(s.get("end", "23:59")):
                return True
        return False

    def get_current_session(self) -> str:
        sessions = [s for s in self.settings.get("custom_sessions", []) if s.get("enabled", True)]
        if not sessions:
            return "24/7 Mode"
        now = self.berlin_now()
        cur = (now.hour, now.minute)
        for s in sessions:
            if self._parse_time(s.get("start", "00:00")) <= cur < self._parse_time(s.get("end", "23:59")):
                return s.get("name", "Custom")
        return "Closed"

    # ---------- candles ----------
    def add_closed_candle(self, symbol: str, candle: Dict) -> bool:
        buf = self.candle_buffer.setdefault(symbol, [])
        if buf and candle["timestamp"] <= buf[-1]["timestamp"]:
            return False
        buf.append(candle)
        limit = self.buffer_limit()
        if len(buf) > limit:
            self.candle_buffer[symbol] = buf[-limit:]
        return True

    # ---------- analysis ----------
    def analyze_symbol(self, symbol: str) -> List[Dict]:
        """Run all enabled strategies. Update rule_states. Return NEW full signals."""
        candles = self.candle_buffer.get(symbol, [])
        signals = []
        states = {}
        for sid in self.enabled_strategies():
            strategy = registry.get(sid)
            params = strategy.get_params(self.settings, symbol)
            tf = self.strategy_timeframe(sid)
            c_use = candles if TIMEFRAMES.get(tf, 1) <= 1 \
                else aggregate_candles(candles, tf, drop_partial=True)
            try:
                res = strategy.analyze(c_use, symbol, params)
            except Exception as e:
                logger.error(f"analyze error {sid}/{symbol}: {e}")
                res = None
            if not res:
                continue
            states[sid] = {
                "strategy_id": sid,
                "strategy_name": strategy.STRATEGY_NAME,
                "timeframe": tf,
                "rules": res.get("rules", []),
                "bias": res.get("bias"),
                "long_count": res.get("long_count", 0),
                "short_count": res.get("short_count", 0),
                "rules_total": res.get("rules_total", len(res.get("rules", []))),
                "indicators": res.get("indicators", {}),
                "signal_active": bool(res.get("signal_type")) and not res.get("is_pre_signal"),
                "signal_type": res.get("signal_type"),
                "is_pre_signal": res.get("is_pre_signal", False),
            }
            sig = self._maybe_signal(symbol, strategy, res)
            if sig:
                signals.append(sig)
        if states:
            self.rule_states[symbol] = states
        return signals

    def _maybe_signal(self, symbol, strategy, res) -> Optional[Dict]:
        if not res.get("signal_type"):
            return None
        if not self.is_signals_enabled(strategy.STRATEGY_ID):
            return None
        if not self.is_trading_session(strategy.STRATEGY_ID):
            return None
        is_pre = res.get("is_pre_signal", False)
        if is_pre and not self.settings.get("pre_signal_enabled", True):
            return None
        key = (strategy.STRATEGY_ID, symbol)
        now_ts = datetime.now(timezone.utc).timestamp()
        if is_pre:
            last = self.last_pre_signal.get(key)
            if last and (now_ts - last) < 300:
                return None
            self.last_pre_signal[key] = now_ts

        levels = res.get("levels") or {}
        ind = res.get("indicators", {})
        rules_met = {r["id"]: (r["long"] if res["signal_type"] == "LONG" else r["short"])
                     for r in res.get("rules", [])}
        # Regel-Snapshot fürs Chart-Overlay: Label + erfüllt + optionaler Regel-TF
        rules_snapshot = [
            {"id": r["id"], "label": r.get("label"),
             "met": bool(r["long"] if res["signal_type"] == "LONG" else r["short"]),
             **({"timeframe": r["timeframe"]} if r.get("timeframe") else {})}
            for r in res.get("rules", [])]
        now = self.berlin_now()
        return {
            "symbol": symbol,
            "type": res["signal_type"],
            "signal_class": "PRE_SIGNAL" if is_pre else "SIGNAL",
            "entry_price": levels.get("entry"),
            "stop_loss": levels.get("stop_loss"),
            "take_profit_1": levels.get("take_profit_1"),
            "take_profit_full": levels.get("take_profit_full"),
            "crv": levels.get("crv", 0),
            "rsi": ind.get("rsi", 0),
            "ema_fast": ind.get("ema_fast", 0),
            "ema_slow": ind.get("ema_slow", 0),
            "rules_met": rules_met,
            "rules_met_count": sum(1 for v in rules_met.values() if v),
            "rules_total": len(rules_met),
            "rules_snapshot": rules_snapshot,
            "strategy_timeframe": self.strategy_timeframe(strategy.STRATEGY_ID),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "trade_date": self.berlin_date(),
            "hour": now.hour,
            "weekday": now.weekday(),
            "session": self.get_current_session(),
            "strategy_id": strategy.STRATEGY_ID,
            "strategy_name": strategy.STRATEGY_NAME,
            "status": "active",
        }

    def get_rule_states(self, symbols: List[str]) -> Dict:
        return {s: self.rule_states.get(s, {}) for s in symbols if s in self.rule_states}

    def current_price(self, symbol: str) -> Optional[float]:
        f = self.forming.get(symbol)
        if f:
            return f.get("close")
        buf = self.candle_buffer.get(symbol)
        return buf[-1]["close"] if buf else None

    def debug_snapshot(self, symbol: str) -> Dict:
        candles = self.candle_buffer.get(symbol, [])
        return {"symbol": symbol, "closed_candles": len(candles),
                "price": candles[-1]["close"] if candles else None,
                "strategies": list(self.rule_states.get(symbol, {}).keys())}
