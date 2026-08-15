"""
Strategie-Backtester auf historischen 1-Minuten-Daten (Binance Public API).
Simuliert die gleiche Trade-Logik wie der Live/Paper-AutoTrader:
Struktur/Fixed-SL, TP1-Teilverkauf, Break-Even (konfigurierbar), ATR-Trailing,
Gewinnsicherung, Zeitfenster und Gebühren.
"""
import asyncio
import gc
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

import aiohttp

from services.candles import CandleArray
from services.technical_indicators import TechnicalIndicators
from services.timeframes import TIMEFRAMES, aggregate_candles

logger = logging.getLogger(__name__)

TRADE_CFG_KEYS = ("max_capital", "leverage", "fee_percent", "tp1_crv", "tp_full_crv",
                  "tp1_close_percent", "sl_mode", "sl_fixed_percent", "sl_lookback",
                  "sl_ticks", "breakeven_enabled", "trail_after_tp1", "trail_atr_mult",
                  "atr_sl_multiplier", "atr_period", "min_risk_percent",
                  "profit_secure_enabled", "profit_secure_trigger_pct", "profit_lock_pct",
                  "trade_pre_signals", "be_mode", "be_trigger_crv", "be_trigger_profit_pct",
                  "be_smart_lookback", "require_all_rules", "sessions",
                  "tp_mode", "tp1_percent", "tp_full_percent", "maintenance_margin_rate",
                  "auto_leverage_enabled", "auto_lev_mode", "auto_lev_value", "auto_lev_max")

JOBS: Dict[str, Dict] = {}

BINANCE_URL = "https://data-api.binance.vision/api/v3/klines"
WARMUP = 80
WINDOW = 260
BERLIN = ZoneInfo("Europe/Berlin")


class JobCancelled(Exception):
    pass


def _clip_history(history, start_ms, end_ms):
    """Zeitraum-Filter – bei CandleArray ein reiner Index-Slice ohne Kopie."""
    if isinstance(history, CandleArray):
        return history.slice_range(start_ms, end_ms)
    return [c for c in history
            if (not start_ms or c["timestamp"] >= start_ms)
            and (not end_ms or c["timestamp"] <= end_ms)]


async def fetch_history(session: aiohttp.ClientSession, symbol: str, days: int,
                        job: Dict = None) -> List[Dict]:
    """Lädt 1m-Kerzen (nutzt Hybrid-Cache; siehe services.candle_cache)."""
    from services import candle_cache
    return await candle_cache.get_candles(session, symbol, days, job=job)


# ---------------- Zeitfenster ----------------
def _parse_hhmm(s: str) -> Optional[int]:
    try:
        p = str(s).strip().split(":")
        return int(p[0]) * 60 + (int(p[1]) if len(p) > 1 else 0)
    except (ValueError, IndexError):
        return None


def parse_sessions(sessions) -> List[tuple]:
    """Akzeptiert Liste [{start,end,enabled}] ODER String '09:00-12:00,15:00-22:00'."""
    spans = []
    if not sessions:
        return spans
    if isinstance(sessions, str):
        items = [x for x in sessions.split(",") if "-" in x]
        sessions = [{"start": x.split("-")[0], "end": x.split("-")[1]} for x in items]
    for s in sessions:
        if isinstance(s, dict):
            if s.get("enabled") is False:
                continue
            a = _parse_hhmm(s.get("start", "00:00"))
            b = _parse_hhmm(s.get("end", "23:59"))
            if a is not None and b is not None and a != b:
                spans.append((a, b))
    return spans


def make_session_checker(sessions) -> Optional[Callable[[int], bool]]:
    """None => 24/7. Sonst Checker: Kerzen-Zeitstempel in Berlin-Zeit im Fenster?"""
    spans = parse_sessions(sessions)
    if not spans:
        return None
    offset_cache: Dict[int, int] = {}

    def in_session(ts_ms: int) -> bool:
        day = ts_ms // 86400000
        off = offset_cache.get(day)
        if off is None:
            off = int(datetime.fromtimestamp(ts_ms / 1000, tz=BERLIN)
                      .utcoffset().total_seconds() // 60)
            offset_cache[day] = off
        m = int((ts_ms // 60000 + off) % 1440)
        for a, b in spans:
            if a < b:
                if a <= m < b:
                    return True
            else:  # über Mitternacht
                if m >= a or m < b:
                    return True
        return False

    return in_session


def compute_levels(cfg: Dict, side: str, entry: float, candles):
    ti = TechnicalIndicators
    ca = candles if isinstance(candles, CandleArray) else None
    atr = 0.0
    atr_period = int(cfg.get("atr_period", 14))
    if len(candles) > atr_period + 1:
        if ca is not None:
            from services import vec
            atr = float(vec.atr(ca.hi, ca.lo, ca.cl, atr_period)[-1] or 0.0)
            if atr != atr:  # NaN
                atr = 0.0
        else:
            atr_arr = ti.calculate_atr(candles, atr_period)
            atr = atr_arr[-1] or 0.0
    buffer = atr * float(cfg.get("atr_sl_multiplier", 1.2))
    mode = cfg.get("sl_mode", "structure")
    if mode == "atr" and atr > 0:
        sl = entry - buffer if side == "LONG" else entry + buffer
    elif mode == "structure" and candles:
        lookback = int(cfg.get("sl_lookback", 10))
        struct_buffer = buffer if buffer > 0 else int(cfg.get("sl_ticks", 4)) * entry * 0.0001
        if side == "LONG":
            lo = float(ca.lo[-lookback:].min()) if ca is not None \
                else min(c["low"] for c in candles[-lookback:])
            sl = lo - struct_buffer
        else:
            hi = float(ca.hi[-lookback:].max()) if ca is not None \
                else max(c["high"] for c in candles[-lookback:])
            sl = hi + struct_buffer
    else:
        pct = float(cfg.get("sl_fixed_percent", 1.0)) / 100
        sl = entry * (1 - pct) if side == "LONG" else entry * (1 + pct)
    risk = abs(entry - sl)
    if risk <= 0:
        risk = entry * 0.003
        sl = entry - risk if side == "LONG" else entry + risk
    min_risk_abs = entry * float(cfg.get("min_risk_percent", 0.25)) / 100
    if risk < min_risk_abs:
        risk = min_risk_abs
        sl = entry - risk if side == "LONG" else entry + risk
    tp_mode = cfg.get("tp_mode", "crv")
    tp1 = tpf = None
    if tp_mode == "fixed_pct":
        p1 = float(cfg.get("tp1_percent", 0.5)) / 100
        pf = float(cfg.get("tp_full_percent", 1.0)) / 100
        if side == "LONG":
            tp1, tpf = entry * (1 + p1), entry * (1 + pf)
        else:
            tp1, tpf = entry * (1 - p1), entry * (1 - pf)
    elif tp_mode == "structure" and candles:
        lookback = int(cfg.get("sl_lookback", 10))
        if side == "LONG":
            target = float(ca.hi[-lookback:].max()) if ca is not None \
                else max(c["high"] for c in candles[-lookback:])
            if target > entry * 1.001:
                tpf = target
                tp1 = entry + (target - entry) * 0.5
        else:
            target = float(ca.lo[-lookback:].min()) if ca is not None \
                else min(c["low"] for c in candles[-lookback:])
            if target < entry * 0.999:
                tpf = target
                tp1 = entry - (entry - target) * 0.5
    if tp1 is None or tpf is None:  # crv (Standard) oder Struktur-Fallback
        if side == "LONG":
            tp1 = entry + risk * float(cfg.get("tp1_crv", 1.0))
            tpf = entry + risk * float(cfg.get("tp_full_crv", 2.0))
        else:
            tp1 = entry - risk * float(cfg.get("tp1_crv", 1.0))
            tpf = entry - risk * float(cfg.get("tp_full_crv", 2.0))
    return sl, tp1, tpf, risk, atr


def resolve_be_mode(cfg: Dict) -> str:
    mode = cfg.get("be_mode")
    if mode in ("off", "tp1", "crv", "profit_pct", "smart"):
        # Legacy-Schalter hat Vorrang, wenn explizit deaktiviert
        if mode == "tp1" and cfg.get("breakeven_enabled") is False:
            return "off"
        return mode
    return "tp1" if cfg.get("breakeven_enabled", True) else "off"


def effective_leverage(cfg: Dict, entry: float, sl: float) -> float:
    """Auto-Leverage: Hebel so wählen, dass der Liquidationspreis einen
    konfigurierbaren Abstand HINTER dem Stop-Loss liegt.
    auto_lev_mode: 'liq_pct'  -> Abstand = X % vom Preis hinter dem SL
                   'liq_ticks'-> Abstand = X Ticks (1 Tick = 0.01% vom Preis)"""
    base = float(cfg.get("leverage", 10) or 10)
    if not cfg.get("auto_leverage_enabled") or not entry or entry <= 0:
        return base
    mmr = float(cfg.get("maintenance_margin_rate", 0.5)) / 100
    sl_dist = abs(entry - sl) / entry
    if sl_dist <= 0:
        return base
    val = float(cfg.get("auto_lev_value", 0.5) or 0.5)
    if cfg.get("auto_lev_mode", "liq_pct") == "liq_ticks":
        extra = max(val, 0.0) * 0.0001
    else:
        extra = max(val, 0.0) / 100.0
    liq_dist = sl_dist + extra
    lev = 1.0 / max(liq_dist + mmr, 1e-6)
    max_lev = float(cfg.get("auto_lev_max", 50) or 50)
    return round(max(1.0, min(lev, max_lev)), 2)


def simulate_pair(strategy, candles: List[Dict], symbol: str, settings: Dict,
                  cfg: Dict, progress_cb=None, collect_trades: bool = False,
                  should_stop: Callable[[], bool] = None,
                  signal_provider: Callable[[int], Optional[Dict]] = None) -> Dict:
    """Sync CPU-bound simulation for one (strategy, symbol) pair."""
    fee_pct = float(cfg.get("fee_percent", 0.06)) / 100
    capital = float(cfg.get("max_capital", 100.0))
    leverage = float(cfg.get("leverage", 10))
    # Liquidation (Isolated Margin): Distanz bis Liquidationspreis in % vom Entry.
    # ~ 1/Hebel minus Maintenance-Margin-Rate (Default 0.5%).
    mmr = float(cfg.get("maintenance_margin_rate", 0.5)) / 100
    auto_lev = bool(cfg.get("auto_leverage_enabled"))
    tp1_close = float(cfg.get("tp1_close_percent", 50)) / 100
    be_mode = resolve_be_mode(cfg)
    be_trigger_crv = float(cfg.get("be_trigger_crv", 1.0) or 1.0)
    be_trigger_profit = float(cfg.get("be_trigger_profit_pct", 30.0) or 30.0)
    be_smart_lookback = int(cfg.get("be_smart_lookback", 10) or 10)
    trail_enabled = bool(cfg.get("trail_after_tp1", True))
    trail_mult = float(cfg.get("trail_atr_mult", 1.5))
    ps_enabled = bool(cfg.get("profit_secure_enabled", False))
    ps_trigger = float(cfg.get("profit_secure_trigger_pct", 30.0))
    ps_lock = max(0.0, min(float(cfg.get("profit_lock_pct", 50.0)), 95.0)) / 100
    require_all = bool(cfg.get("require_all_rules", False))
    session_check = make_session_checker(cfg.get("sessions"))
    # Richtungs-Bias (Regime-Strategie-Suche): nur diese Seiten handeln.
    # None/leer = beide Seiten erlaubt (Standard, unverändertes Verhalten).
    allowed_sides = cfg.get("allowed_sides") or None
    if allowed_sides:
        allowed_sides = {str(s).upper() for s in allowed_sides
                         if str(s).upper() in ("LONG", "SHORT")} or None

    trades: List[Dict] = []
    open_t: Optional[Dict] = None
    n = len(candles)

    def close_trade(t, price, result, ts):
        fee = t["qty_rem"] * price * fee_pct
        t["pnl"] += (price - t["entry"]) * t["qty_rem"] if t["side"] == "LONG" \
            else (t["entry"] - price) * t["qty_rem"]
        t["pnl"] -= fee
        t["fees"] += fee
        # Isolated Margin: Verlust kann die eingesetzte Marge nicht übersteigen
        if t["pnl"] < -capital:
            t["pnl"] = -capital
        t["exit"] = price
        t["result"] = result
        t["closed_ts"] = ts
        t["qty_rem"] = 0
        trades.append(t)

    def be_price(t):
        # Echtes Break-Even inkl. Entry+Exit-Gebühren (siehe bitunix_trade._be_price)
        return t["entry"] * (1 + fee_pct) / (1 - fee_pct) if t["side"] == "LONG" \
            else t["entry"] * (1 - fee_pct) / (1 + fee_pct)

    def move_be(t, new_sl):
        if (t["side"] == "LONG" and new_sl > t["sl"]) or \
           (t["side"] == "SHORT" and new_sl < t["sl"]):
            t["sl"] = new_sl
        t["be_moved"] = True

    warmup = WARMUP if n >= WARMUP * 2 else max(20, n // 3)
    ca = candles if isinstance(candles, CandleArray) else None
    if ca is not None:
        a_ts, a_op, a_hi, a_lo, a_cl, a_vol = ca.ts, ca.op, ca.hi, ca.lo, ca.cl, ca.vol
    for i in range(warmup, n):
        if ca is not None:
            c_ts = int(a_ts[i])
            c_open, c_high, c_low = float(a_op[i]), float(a_hi[i]), float(a_lo[i])
            c_close, c_vol = float(a_cl[i]), float(a_vol[i])
        else:
            c = candles[i]
            c_ts, c_open, c_high = c["timestamp"], c["open"], c["high"]
            c_low, c_close, c_vol = c["low"], c["close"], c.get("volume", 0)
        if i % 400 == 0:
            if progress_cb:
                progress_cb(i, n)
            if should_stop and should_stop():
                raise JobCancelled()

        # ---- manage open trade against this candle ----
        if open_t is not None:
            t = open_t
            side = t["side"]
            lo, hi, close_p = c_low, c_high, c_close

            def hit(level, direction):
                if direction == "up":
                    return hi >= level
                return lo <= level

            sl_hit = hit(t["sl"], "down" if side == "LONG" else "up")
            liq_hit = hit(t["liq"], "down" if side == "LONG" else "up")
            tpf_hit = hit(t["tpf"], "up" if side == "LONG" else "down")
            tp1_hit = (not t["tp1_done"]) and hit(t["tp1"], "up" if side == "LONG" else "down")

            # conservative: SL/Liquidation first when both touched in same candle
            if sl_hit or liq_hit:
                # Welches Level wird zuerst erreicht? (LONG: das höhere von SL/Liq)
                if side == "LONG":
                    level = max(t["sl"], t["liq"])
                    is_liq = liq_hit and t["liq"] >= t["sl"]
                else:
                    level = min(t["sl"], t["liq"])
                    is_liq = liq_hit and t["liq"] <= t["sl"]
                if is_liq:
                    t["liquidated"] = True
                    close_trade(t, t["liq"], "loss", c_ts)
                else:
                    is_be = t["be_moved"]
                    res = "breakeven" if is_be else ("win" if t["tp1_done"] else "loss")
                    close_trade(t, level, res, c_ts)
                open_t = None
            else:
                if tp1_hit and not tpf_hit:
                    cq = t["qty"] * tp1_close
                    fee = cq * t["tp1"] * fee_pct
                    t["pnl"] += ((t["tp1"] - t["entry"]) * cq if side == "LONG"
                                 else (t["entry"] - t["tp1"]) * cq) - fee
                    t["fees"] += fee
                    t["qty_rem"] = t["qty"] - cq
                    t["tp1_done"] = True
                    if be_mode == "tp1":
                        move_be(t, be_price(t))
                    elif be_mode == "smart":
                        s0 = max(0, i - be_smart_lookback)
                        if i > s0:
                            if ca is not None:
                                new_sl = float(a_lo[s0:i].min()) if side == "LONG" \
                                    else float(a_hi[s0:i].max())
                            else:
                                seg = candles[s0:i]
                                new_sl = min(x["low"] for x in seg) if side == "LONG" \
                                    else max(x["high"] for x in seg)
                            move_be(t, new_sl)
                if tpf_hit and open_t is not None:
                    close_trade(t, t["tpf"], "win", c_ts)
                    open_t = None
                elif open_t is not None:
                    # Break-Even bei frei wählbarem CRV / Gewinn-%
                    if not t["be_moved"] and be_mode in ("crv", "profit_pct"):
                        trigger = False
                        if be_mode == "crv":
                            target = t["entry"] + t["risk"] * be_trigger_crv if side == "LONG" \
                                else t["entry"] - t["risk"] * be_trigger_crv
                            trigger = hi >= target if side == "LONG" else lo <= target
                        else:
                            unreal = (close_p - t["entry"]) * t["qty_rem"] if side == "LONG" \
                                else (t["entry"] - close_p) * t["qty_rem"]
                            trigger = capital > 0 and unreal / capital * 100 >= be_trigger_profit > 0
                        if trigger:
                            move_be(t, be_price(t))
                    # Gewinnsicherung (auf Schlusskurs)
                    if ps_enabled and not t["secured"] and capital > 0:
                        unreal = (close_p - t["entry"]) * t["qty_rem"] if side == "LONG" \
                            else (t["entry"] - close_p) * t["qty_rem"]
                        if unreal / capital * 100 >= ps_trigger > 0:
                            new_sl = t["entry"] + (close_p - t["entry"]) * ps_lock if side == "LONG" \
                                else t["entry"] - (t["entry"] - close_p) * ps_lock
                            if (side == "LONG" and new_sl > t["sl"]) or \
                               (side == "SHORT" and new_sl < t["sl"]):
                                t["sl"] = new_sl
                            t["secured"] = True
                    # ATR-Trailing nach TP1
                    if trail_enabled and t["tp1_done"] and t["atr"] > 0:
                        new_sl = close_p - t["atr"] * trail_mult if side == "LONG" \
                            else close_p + t["atr"] * trail_mult
                        if (side == "LONG" and new_sl > t["sl"]) or \
                           (side == "SHORT" and new_sl < t["sl"]):
                            t["sl"] = new_sl

        # ---- check for new signal on this closed candle ----
        if open_t is None:
            if session_check and not session_check(c_ts):
                continue
            if signal_provider is not None:
                sig = signal_provider(i)
            else:
                window = candles[max(0, i - WINDOW):i + 1]
                try:
                    sig = strategy.check_signal(window, symbol, settings)
                except Exception:
                    sig = None
            if sig and sig.get("type") in ("LONG", "SHORT"):
                if allowed_sides and sig["type"] not in allowed_sides:
                    continue
                if sig.get("signal_class") == "PRE_SIGNAL" and not cfg.get("trade_pre_signals"):
                    continue
                if require_all and sig.get("rules_total") \
                        and (sig.get("rules_met_count") or 0) < sig["rules_total"]:
                    continue
                side = sig["type"]
                entry = float(sig.get("entry_price") or c_close)
                if entry <= 0:
                    continue
                window = candles[max(0, i - WINDOW):i + 1]
                sl, tp1, tpf, risk, atr = compute_levels(cfg, side, entry, window)
                lev_t = effective_leverage(cfg, entry, sl) if auto_lev else leverage
                liq_dist_t = max(1.0 / max(lev_t, 1.0) - mmr, 0.0005)
                qty = capital * lev_t / entry
                entry_fee = entry * qty * fee_pct
                liq = entry * (1 - liq_dist_t) if side == "LONG" else entry * (1 + liq_dist_t)
                # Liquidations-Schutz (wie im Live-Trading): Der Stop-Loss wird
                # immer VOR dem Liquidationspreis platziert. Liegt der berechnete
                # SL hinter der Liquidation, wird er knapp davor gesetzt –
                # dadurch kann ein Trade nie liquidiert werden, solange der SL greift.
                if side == "LONG" and sl <= liq:
                    sl = liq + (entry - liq) * 0.05
                    risk = abs(entry - sl)
                elif side == "SHORT" and sl >= liq:
                    sl = liq - (liq - entry) * 0.05
                    risk = abs(entry - sl)
                open_t = {
                    "side": side, "entry": entry, "sl": sl, "tp1": tp1, "tpf": tpf,
                    "liq": liq, "liquidated": False, "lev": round(lev_t, 2),
                    "qty": qty, "qty_rem": qty, "atr": atr,
                    "pnl": -entry_fee, "fees": entry_fee,
                    "tp1_done": False, "be_moved": False, "secured": False,
                    "opened_ts": c_ts,
                    "sl_init": sl, "risk": risk,
                    "sig_rsi": sig.get("rsi"), "sig_ema_fast": sig.get("ema_fast"),
                    "sig_ema_slow": sig.get("ema_slow"), "sig_crv": sig.get("crv"),
                    "sig_rules_met": sig.get("rules_met_count"),
                    "sig_rules_total": sig.get("rules_total"),
                    "c_open": c_open, "c_high": c_high, "c_low": c_low,
                    "c_close": c_close, "c_volume": c_vol,
                }

    # ---- metrics ----
    # Winrate basiert auf tatsächlichem PnL (nicht auf SL/TP-Label):
    # Ein Trade der TP1 realisiert und danach am BE-Stop schließt, ist trotzdem profitabel.
    eps = 1e-6
    wins = sum(1 for t in trades if t["pnl"] > eps)
    losses = sum(1 for t in trades if t["pnl"] < -eps)
    breakevens = sum(1 for t in trades if -eps <= t["pnl"] <= eps)
    decided = wins + losses
    pnl_total = sum(t["pnl"] for t in trades)
    fees_total = sum(t["fees"] for t in trades)
    gross_win = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gross_loss = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for t in trades:
        equity += t["pnl"]
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    durations = [(t["closed_ts"] - t["opened_ts"]) / 60000 for t in trades
                 if t.get("closed_ts") and t.get("opened_ts")]

    def _iso(ts):
        return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat() if ts else ""

    all_trades = []
    if collect_trades:
        for t in trades:
            all_trades.append({
                "opened": _iso(t.get("opened_ts")), "closed": _iso(t.get("closed_ts")),
                "side": t["side"], "entry": t["entry"], "exit": t.get("exit"),
                "sl_initial": t.get("sl_init"), "sl_final": t.get("sl"),
                "tp1": t["tp1"], "tp_full": t["tpf"], "risk": round(t.get("risk", 0), 8),
                "result": t.get("result"), "pnl": round(t["pnl"], 4),
                "fees": round(t["fees"], 4), "qty": round(t["qty"], 6),
                "tp1_done": t["tp1_done"], "breakeven_moved": t["be_moved"],
                "profit_secured": t["secured"], "liquidated": t.get("liquidated", False),
                "liq_price": round(t.get("liq", 0), 8),
                "leverage": t.get("lev"),
                "duration_min": round((t["closed_ts"] - t["opened_ts"]) / 60000, 1)
                if t.get("closed_ts") else None,
                "atr_entry": round(t.get("atr") or 0, 8),
                "rsi_entry": t.get("sig_rsi"), "ema_fast_entry": t.get("sig_ema_fast"),
                "ema_slow_entry": t.get("sig_ema_slow"), "crv_signal": t.get("sig_crv"),
                "rules_met": t.get("sig_rules_met"), "rules_total": t.get("sig_rules_total"),
                "entry_candle_open": t.get("c_open"), "entry_candle_high": t.get("c_high"),
                "entry_candle_low": t.get("c_low"), "entry_candle_close": t.get("c_close"),
                "entry_candle_volume": t.get("c_volume"),
            })

    return {
        "all_trades": all_trades,
        "trades": len(trades),
        "wins": wins, "losses": losses, "breakevens": breakevens,
        "secured": sum(1 for t in trades if t["secured"]),
        "be_moved": sum(1 for t in trades if t["be_moved"]),
        "liquidations": sum(1 for t in trades if t.get("liquidated")),
        "win_rate": round(wins / decided * 100, 1) if decided else 0.0,
        "pnl": round(pnl_total, 2),
        "pnl_pct": round(pnl_total / capital * 100, 2) if capital else 0.0,
        "max_drawdown_pct": round(max_dd / capital * 100, 2) if capital else 0.0,
        "avg_leverage": round(sum(t.get("lev", leverage) for t in trades) / len(trades), 1)
        if trades else round(leverage, 1),
        "fees": round(fees_total, 2),
        "avg_pnl": round(pnl_total / len(trades), 3) if trades else 0.0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else (round(gross_win, 2) if gross_win else 0.0),
        "max_drawdown": round(max_dd, 2),
        "avg_duration_min": round(sum(durations) / len(durations), 1) if durations else 0.0,
        "long_trades": sum(1 for t in trades if t["side"] == "LONG"),
        "short_trades": sum(1 for t in trades if t["side"] == "SHORT"),
        "last_trades": [
            {"side": t["side"], "entry": round(t["entry"], 6), "exit": round(t.get("exit", 0), 6),
             "result": t["result"], "pnl": round(t["pnl"], 3),
             "opened": datetime.fromtimestamp(t["opened_ts"] / 1000, tz=timezone.utc).isoformat(),
             } for t in trades[-15:]],
    }


def create_job(params: Dict) -> str:
    job_id = uuid.uuid4().hex[:12]
    # Kerzendaten nur für den aktuellsten Job im Speicher halten
    for j in JOBS.values():
        j.pop("export_candles", None)
    JOBS[job_id] = {"id": job_id, "status": "running", "progress": 0,
                    "phase": "Daten laden", "params": params, "cancel": False,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "result": None, "error": None}
    if len(JOBS) > 10:
        for k in list(JOBS.keys())[:-10]:
            JOBS.pop(k, None)
    return job_id


def resolve_timeframe(strat, sid: str, scfg: Dict, settings: Dict, default_tf: str = None) -> str:
    tf = (scfg.get("timeframe")
          or default_tf
          or settings.get("strategy_timeframes", {}).get(sid)
          or getattr(strat, "STRATEGY_TIMEFRAME", "1m"))
    return tf if tf in TIMEFRAMES else "1m"


MAX_EXPORT_CANDLES = 400000
MAX_EXPORT_TRADES = 100000


def _range_ms(s, end_of_day=False):
    if not s:
        return None
    try:
        dtv = datetime.fromisoformat(str(s))
        if dtv.tzinfo is None:
            dtv = dtv.replace(tzinfo=timezone.utc)
        ms = int(dtv.timestamp() * 1000)
        if end_of_day and len(str(s)) <= 10:
            ms += 86399999
        return ms
    except ValueError:
        return None


def _effective_strategy(strat, sid: str, scfg: Dict):
    """Pro-Backtest Definition-Override für Custom-/Discovery-Strategien."""
    if scfg.get("definition") and getattr(strat, "IS_CUSTOM", False):
        try:
            from strategies.custom_strategy import CustomStrategy
            return CustomStrategy({**strat.definition, **scfg["definition"], "id": sid})
        except Exception as e:
            logger.warning(f"definition override failed {sid}: {e}")
    return strat


def _pair_trade_cfg(cfg: Dict, scfg: Dict) -> Dict:
    """Pro-Strategie Trade-Config (TP/SL, Zeitfenster etc.) über Basis-Config."""
    pair_cfg = dict(cfg)
    for ck in TRADE_CFG_KEYS:
        if scfg.get(ck) is not None:
            pair_cfg[ck] = scfg[ck]
    return pair_cfg


def _pair_settings(settings: Dict, sid: str, scfg: Dict) -> Dict:
    """Pro-Strategie Indikator-Parameter überschreiben."""
    if scfg.get("params"):
        sp = dict(settings.get("strategy_params", {}))
        sp[sid] = {**sp.get(sid, {}), **scfg["params"]}
        return {**settings, "strategy_params": sp}
    return settings


async def _simulate_all_sequential(job, strategy_ids, symbols, days, cfg, registry,
                                   settings, strategy_configs, default_timeframe,
                                   start_ms, end_ms, cancelled):
    """Bisheriger sequenzieller Pfad (Logik unverändert, nur ausgelagert)."""
    from services import fast_sim

    total_units = max(len(symbols) * (1 + len(strategy_ids)), 1)
    done_units = 0
    per_pair = []
    export_trades: List[Dict] = []
    export_candles: Dict[str, List[Dict]] = {}
    strat_tf: Dict[str, str] = {}
    bench = {"data_seconds": 0.0, "sim_seconds": 0.0, "cpu_seconds": 0.0,
             "workers": 1, "pairs": 0, "sim_candles": 0, "raw_candles": 0}

    async with aiohttp.ClientSession() as session:
        for sym in symbols:
            if cancelled():
                raise JobCancelled()
            job["phase"] = f"Lade Daten: {sym}"
            _t = time.perf_counter()
            history = await fetch_history(session, sym, days, job=job)
            bench["data_seconds"] += time.perf_counter() - _t
            bench["raw_candles"] += len(history)
            if start_ms or end_ms:
                history = _clip_history(history, start_ms, end_ms)
            done_units += 1
            job["progress"] = round(done_units / total_units * 100)
            if len(history) <= 100:
                done_units += len(strategy_ids)
                job["progress"] = round(done_units / total_units * 100)
                continue

            tf_cache: Dict[str, List[Dict]] = {}
            fs_cache: Dict[str, "fast_sim.FastSeries"] = {}
            for sid in strategy_ids:
                if cancelled():
                    raise JobCancelled()
                strat = registry.get(sid)
                if not strat:
                    done_units += 1
                    continue
                scfg = strategy_configs.get(sid) or {}
                strat = _effective_strategy(strat, sid, scfg)
                tf = resolve_timeframe(strat, sid, scfg, settings, default_timeframe)
                strat_tf[sid] = tf
                if tf not in tf_cache:
                    tf_cache[tf] = aggregate_candles(history, tf)
                candles = tf_cache[tf]
                pair_cfg = _pair_trade_cfg(cfg, scfg)
                eff_settings = _pair_settings(settings, sid, scfg)
                # Schneller Pfad für Custom-Strategien
                provider = None
                use_fast = bool(cfg.get("use_fast_path", True))
                if not use_fast:
                    pass  # Legacy-Pfad erzwungen (RAM-Schonung / Verifikation)
                else:
                    # Fast-Path (Custom + opt-in Built-ins); Fehler -> Legacy-Fallback.
                    try:
                        if tf not in fs_cache:
                            fs_cache[tf] = fast_sim.FastSeries(candles)
                        provider = fast_sim.provider_for(strat, fs_cache[tf],
                                                         eff_settings, sym)
                    except Exception as e:
                        logger.warning(f"fast_sim fallback {sid}: {e}")
                        provider = None
                job["phase"] = f"Simuliere {getattr(strat, 'STRATEGY_NAME', sid)} auf {sym} ({tf})"
                base = round(done_units / total_units * 100)
                nxt = round((done_units + 1) / total_units * 100)

                def cb(i, m, _base=base, _next=nxt):
                    job["progress"] = _base + round((i / m) * (_next - _base))

                _t = time.perf_counter()
                res = await asyncio.to_thread(simulate_pair, strat, candles, sym,
                                              eff_settings, pair_cfg, cb, True,
                                              cancelled, provider)
                _dur = time.perf_counter() - _t
                bench["sim_seconds"] += _dur
                bench["cpu_seconds"] += _dur
                bench["pairs"] += 1
                bench["sim_candles"] += len(candles)
                all_trades = res.pop("all_trades", [])
                if len(export_trades) < MAX_EXPORT_TRADES:
                    for t in all_trades:
                        export_trades.append({"strategy_id": sid,
                                              "strategy_name": getattr(strat, "STRATEGY_NAME", sid),
                                              "symbol": sym, "timeframe": tf, **t})
                per_pair.append({"strategy_id": sid,
                                 "strategy_name": getattr(strat, "STRATEGY_NAME", sid),
                                 "symbol": sym, "timeframe": tf,
                                 "candles": len(candles), **res})
                done_units += 1
                job["progress"] = round(done_units / total_units * 100)

            # Kerzen-Export (mit Limit, damit große Läufe nicht am RAM sterben)
            stored = sum(len(v) for v in export_candles.values())
            for tf, cds in tf_cache.items():
                if stored + len(cds) <= MAX_EXPORT_CANDLES:
                    export_candles[f"{sym}|{tf}"] = cds
                    stored += len(cds)
            del history, tf_cache, fs_cache
            gc.collect()

    return per_pair, export_trades, export_candles, strat_tf, bench


async def _simulate_all_parallel(job, strategy_ids, symbols, days, cfg, registry,
                                 settings, strategy_configs, default_timeframe,
                                 start_ms, end_ms, cancelled, workers: int):
    """Multi-Core-Pfad (lokaler Worker, SIM_WORKERS>1): alle (Strategie, Coin)-
    Paare parallel im Prozess-Pool. Gleiche simulate_pair-/Fast-Path-Logik wie
    sequenziell -> identische Ergebnisse, gleiche Export-Reihenfolge."""
    from services import parallel_sim

    per_pair: List[Dict] = []
    export_trades: List[Dict] = []
    export_candles: Dict[str, List[Dict]] = {}
    strat_tf: Dict[str, str] = {}
    bench = {"data_seconds": 0.0, "sim_seconds": 0.0, "cpu_seconds": 0.0,
             "workers": workers, "pairs": 0, "sim_candles": 0, "raw_candles": 0}

    # Phase 1: Daten laden (netzwerk-gebunden, wie bisher sequenziell + Cache)
    all_hist: Dict[str, List[Dict]] = {}
    _t = time.perf_counter()
    async with aiohttp.ClientSession() as session:
        for i, sym in enumerate(symbols):
            if cancelled():
                raise JobCancelled()
            job["phase"] = f"Lade Daten: {sym}"
            history = await fetch_history(session, sym, days, job=job)
            bench["raw_candles"] += len(history)
            if start_ms or end_ms:
                history = _clip_history(history, start_ms, end_ms)
            if len(history) > 100:
                all_hist[sym] = history
            job["progress"] = round((i + 1) / max(len(symbols), 1) * 25)
    bench["data_seconds"] = time.perf_counter() - _t

    # Phase 2: Paare vorbereiten + Timeframe-Aggregationen (einmal pro sym|tf)
    agg: Dict[str, List[Dict]] = {}
    pairs = []  # (sid, strat, sym, tf, key, eff_settings, pair_cfg)
    for sym in symbols:
        if sym not in all_hist:
            continue
        for sid in strategy_ids:
            strat = registry.get(sid)
            if not strat:
                continue
            scfg = strategy_configs.get(sid) or {}
            strat = _effective_strategy(strat, sid, scfg)
            tf = resolve_timeframe(strat, sid, scfg, settings, default_timeframe)
            strat_tf[sid] = tf
            key = f"{sym}|{tf}"
            if key not in agg:
                agg[key] = aggregate_candles(all_hist[sym], tf)
            pairs.append((sid, strat, sym, tf, key,
                          _pair_settings(settings, sid, scfg),
                          _pair_trade_cfg(cfg, scfg)))
    del all_hist
    gc.collect()
    if not pairs:
        return per_pair, export_trades, export_candles, strat_tf, bench

    # Phase 3: Prozess-Pool – alle Paare parallel
    use_fast = bool(cfg.get("use_fast_path", True))
    pool = parallel_sim.make_pool(agg, workers)
    loop = asyncio.get_running_loop()
    _t = time.perf_counter()
    try:
        futs = [loop.run_in_executor(pool, parallel_sim.sim_pair_task_timed,
                                     parallel_sim.strategy_spec(strat), key, sym,
                                     eff_s, pcfg, True, use_fast)
                for sid, strat, sym, tf, key, eff_s, pcfg in pairs]
        total = len(futs)
        job["phase"] = f"Simuliere {total} Paare auf {workers} Kernen"
        pending = set(futs)
        while pending:
            if cancelled():
                raise JobCancelled()
            done, pending = await asyncio.wait(pending, timeout=0.5)
            done_n = total - len(pending)
            job["progress"] = 25 + round(done_n / total * 74)
            job["phase"] = f"Simuliere Paare: {done_n}/{total} fertig ({workers} Kerne)"
        results = [f.result() for f in futs]  # Submit-Reihenfolge = stabile Exporte
    finally:
        parallel_sim.close_pool(pool, kill=cancelled())
    bench["sim_seconds"] = time.perf_counter() - _t

    # Phase 4: Ergebnisse einsammeln (gleiche Struktur wie sequenziell)
    for (sid, strat, sym, tf, key, _s, _c), (res, dur) in zip(pairs, results):
        bench["cpu_seconds"] += dur
        if res.get("_error"):
            logger.warning(f"parallel sim {sid}/{sym} failed: {res['_error']}")
            continue
        bench["pairs"] += 1
        bench["sim_candles"] += len(agg[key])
        all_trades = res.pop("all_trades", [])
        if len(export_trades) < MAX_EXPORT_TRADES:
            for t in all_trades:
                export_trades.append({"strategy_id": sid,
                                      "strategy_name": getattr(strat, "STRATEGY_NAME", sid),
                                      "symbol": sym, "timeframe": tf, **t})
        per_pair.append({"strategy_id": sid,
                         "strategy_name": getattr(strat, "STRATEGY_NAME", sid),
                         "symbol": sym, "timeframe": tf,
                         "candles": len(agg[key]), **res})
    stored = 0
    for key, cds in agg.items():
        if stored + len(cds) <= MAX_EXPORT_CANDLES:
            export_candles[key] = cds
            stored += len(cds)
    return per_pair, export_trades, export_candles, strat_tf, bench


def _build_benchmark(bench: Dict, t_start: float, dl_before: Dict, dl_after: Dict,
                     execution: str) -> Dict:
    """Laufzeit-Statistik eines Laufs: Cache- vs. Download-Kerzen, Multi-Core-
    Speedup und geschätzte Zeitersparnis durch lokale/gecachte Daten."""
    downloaded = max(dl_after.get("candles", 0) - dl_before.get("candles", 0), 0)
    dl_seconds = max(dl_after.get("seconds", 0.0) - dl_before.get("seconds", 0.0), 0.0)
    raw = max(bench.get("raw_candles", 0), 0)
    cached = max(raw - downloaded, 0)
    # Download-Rate: gemessen wenn genug geladen wurde, sonst typischer Binance-Wert
    rate = downloaded / dl_seconds if (downloaded > 2000 and dl_seconds > 0.5) else 2500.0
    sim = bench.get("sim_seconds", 0.0)
    cpu = bench.get("cpu_seconds", 0.0)
    speedup = round(cpu / sim, 2) if (sim > 0.05 and cpu > 0) else 1.0
    return {
        "execution": execution,
        "workers": bench.get("workers", 1),
        "total_seconds": round(time.perf_counter() - t_start, 2),
        "data_seconds": round(bench.get("data_seconds", 0.0), 2),
        "sim_seconds": round(sim, 2),
        "cpu_seconds": round(cpu, 2),
        "parallel_speedup": max(speedup, 1.0),
        "pairs": bench.get("pairs", 0),
        "evaluations": bench.get("evaluations"),
        "dyn_segments": bench.get("dyn_segments", 0),
        "worker_name": bench.get("worker_name"),
        "sim_candles": bench.get("sim_candles", 0),
        "raw_candles": raw,
        "downloaded_candles": downloaded,
        "cached_candles": cached,
        "cache_ratio": round(cached / raw * 100, 1) if raw else 0.0,
        "est_cache_saved_seconds": round(cached / rate, 1) if cached else 0.0,
    }


async def run_backtest(job_id: str, strategy_ids: List[str], symbols: List[str],
                       days: int, cfg: Dict, registry, settings: Dict, db=None,
                       strategy_configs: Dict = None, default_timeframe: str = None,
                       date_from: str = None, date_to: str = None):
    job = JOBS[job_id]
    strategy_configs = strategy_configs or {}
    start_ms = _range_ms(date_from)
    end_ms = _range_ms(date_to, end_of_day=True)

    def cancelled():
        return bool(job.get("cancel"))

    try:
        # Multi-Core nur wenn SIM_WORKERS gesetzt (lokaler Worker); sonst
        # bisheriger sequenzieller Pfad (Server/Cloud unverändert).
        from services import candle_cache as _cc
        t_start = time.perf_counter()
        dl_before = _cc.download_stats()
        try:
            from services import parallel_sim
            workers = parallel_sim.workers_configured()
        except Exception:
            workers = 1
        if workers > 1:
            per_pair, export_trades, export_candles, strat_tf, bench = \
                await _simulate_all_parallel(job, strategy_ids, symbols, days, cfg,
                                             registry, settings, strategy_configs,
                                             default_timeframe, start_ms, end_ms,
                                             cancelled, workers)
        else:
            per_pair, export_trades, export_candles, strat_tf, bench = \
                await _simulate_all_sequential(job, strategy_ids, symbols, days, cfg,
                                               registry, settings, strategy_configs,
                                               default_timeframe, start_ms, end_ms,
                                               cancelled)
        benchmark = _build_benchmark(bench, t_start, dl_before,
                                     _cc.download_stats(),
                                     job.get("execution") or "cloud")

        # aggregate per strategy
        per_strategy: Dict[str, Dict] = {}
        for r in per_pair:
            agg = per_strategy.setdefault(r["strategy_id"], {
                "strategy_id": r["strategy_id"], "strategy_name": r["strategy_name"],
                "trades": 0, "wins": 0, "losses": 0, "breakevens": 0, "secured": 0,
                "liquidations": 0,
                "pnl": 0.0, "fees": 0.0, "max_drawdown": 0.0, "symbols": 0,
                "_dur": [],
            })
            agg["trades"] += r["trades"]
            agg["wins"] += r["wins"]
            agg["losses"] += r["losses"]
            agg["breakevens"] += r["breakevens"]
            agg["secured"] += r.get("secured", 0)
            agg["liquidations"] += r.get("liquidations", 0)
            agg["pnl"] = round(agg["pnl"] + r["pnl"], 2)
            agg["fees"] = round(agg["fees"] + r["fees"], 2)
            agg["max_drawdown"] = round(agg["max_drawdown"] + r["max_drawdown"], 2)
            agg["symbols"] += 1
            if r["trades"]:
                agg["_dur"].append(r["avg_duration_min"])
        cap_ref = float(cfg.get("max_capital", 100.0)) or 100.0
        for agg in per_strategy.values():
            d = agg["wins"] + agg["losses"]
            agg["win_rate"] = round(agg["wins"] / d * 100, 1) if d else 0.0
            agg["pnl_pct"] = round(agg["pnl"] / cap_ref * 100, 1)
            agg["max_drawdown_pct"] = round(agg["max_drawdown"] / cap_ref * 100, 1)
            agg["avg_pnl"] = round(agg["pnl"] / agg["trades"], 3) if agg["trades"] else 0.0
            agg["avg_duration_min"] = round(sum(agg["_dur"]) / len(agg["_dur"]), 1) if agg["_dur"] else 0.0
            agg["timeframe"] = strat_tf.get(agg["strategy_id"], "1m")
            agg.pop("_dur", None)

        best_per_symbol = {}
        for r in per_pair:
            cur = best_per_symbol.get(r["symbol"])
            if cur is None or r["pnl"] > cur["pnl"]:
                best_per_symbol[r["symbol"]] = {"strategy_id": r["strategy_id"],
                                                "strategy_name": r["strategy_name"],
                                                "pnl": r["pnl"], "win_rate": r["win_rate"]}

        result = {
            "days": days,
            "date_from": date_from, "date_to": date_to,
            "config": {k: cfg.get(k) for k in ("max_capital", "leverage", "fee_percent",
                                               "tp1_crv", "tp_full_crv", "sl_mode",
                                               "profit_secure_enabled", "be_mode",
                                               "require_all_rules", "auto_leverage_enabled",
                                               "auto_lev_mode", "auto_lev_value")},
            "strategy_timeframes": strat_tf,
            "per_pair": per_pair,
            "per_strategy": sorted(per_strategy.values(), key=lambda x: -x["pnl"]),
            "best_per_symbol": best_per_symbol,
            "benchmark": benchmark,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        # Nicht auswertbare Regeln (z.B. KI-Strategie mit unbekanntem Indikator)
        # sichtbar machen – sonst bleibt "überall 0" unerklärt.
        warnings = []
        fixes_map = {}
        for sid in per_strategy:
            s = registry.get(sid)
            for p in (getattr(s, "rule_problems", None) or []):
                warnings.append(f"{getattr(s, 'STRATEGY_NAME', sid)}: {p}")
            if getattr(s, "IS_CUSTOM", False) and getattr(s, "rule_problems", None):
                from strategies import custom_params
                from strategies.custom_strategy import INDICATORS, OPERATORS
                fx = custom_params.fix_suggestions(s.definition, INDICATORS, OPERATORS)
                if fx:
                    fixes_map[sid] = {"name": getattr(s, "STRATEGY_NAME", sid),
                                      "fixes": fx}
        if warnings:
            result["strategy_warnings"] = warnings[:12]
        if fixes_map:
            result["strategy_fixes"] = fixes_map
        job["result"] = result
        job["export_trades"] = export_trades
        job["export_candles"] = export_candles
        job["status"] = "done"
        job["progress"] = 100
        job["phase"] = "Fertig"
        try:
            from core import state as _st
            from services import notifications as _nf
            top = (result.get("per_strategy") or [{}])[0]
            await _nf.telegram_notify(
                _st.db, _st.telegram, "backtest_done",
                f"📊 *BACKTEST FERTIG* ({days} Tage, {len(per_pair)} Paare)\n"
                f"Beste Strategie: {top.get('strategy_name', '-')} · "
                f"PnL `{top.get('pnl', 0)} USDT` · WR {top.get('win_rate', 0)}%")
        except Exception as e:
            logger.warning(f"backtest_done notify failed: {e}")
        if db is not None:
            try:
                await db.backtests.insert_one({"id": job_id, "params": job["params"],
                                               "created_at": job["created_at"],
                                               "result": result})
                await db.backtest_trades.insert_one({"job_id": job_id,
                                                     "created_at": job["created_at"],
                                                     "rows": export_trades[:50000]})
            except Exception as e:
                logger.warning(f"backtest persist failed: {e}")
    except JobCancelled:
        job["status"] = "cancelled"
        job["phase"] = "Abgebrochen"
        job["error"] = None
        logger.info(f"backtest {job_id} cancelled")
    except Exception as e:
        logger.exception("backtest failed")
        job["status"] = "error"
        job["error"] = str(e)[:300]
