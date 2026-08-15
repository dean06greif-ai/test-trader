"""Markt-Beobachter ("market_observer"-Rolle des KI-Teams).

Sammelt in festen Intervallen den messbaren Marktzustand aller beobachteten
Coins (Trend, Volatilität, ATR, RSI, Volumen, Range-Position) und legt ihn als
Zeitreihe in `db.ai_market_snapshots` ab. Diese Snapshots sind

  1. Trainingsdaten für das ML-Labor (services/ai_ml_lab.py) – "welche
     Marktbedingungen liefern gute Ergebnisse",
  2. Kontext für den KI Trader (aktueller Marktzustand + Veränderung),
  3. Grundlage für die Regime-Zuordnung von Trades.

Reine Feature-Berechnung (`compute_features`) ist DB-/LLM-frei und wird in den
Regressionstests direkt geprüft. Der News-/Kalender-Teil bleibt beim
News-Wächter – dieser Beobachter arbeitet ausschließlich datengetrieben und
verbraucht standardmäßig KEIN LLM-Budget (optionale Kurz-Einschätzung via
Rollen-Feld `llm_summary`).
"""
import logging
import statistics
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from services.ai_memory import memory
from services.technical_indicators import TechnicalIndicators

logger = logging.getLogger(__name__)

OBSERVER_SYSTEM = (
    "Du bist der 'Markt-Beobachter' im KI-Team einer Krypto-Daytrading-Plattform. "
    "Du bewertest ausschließlich den gemessenen Marktzustand (Trend, Volatilität, "
    "Volumen, Range-Position) – keine Trades, keine News. Antworte AUSSCHLIESSLICH mit "
    "validem JSON ohne Markdown:\n"
    '{"regime": "trend_up|trend_down|range|volatil|ruhig", '
    '"summary": "2-4 Sätze auf Deutsch: Marktzustand + was das für Daytrading bedeutet", '
    '"watchlist": ["BTCUSDT"]}'
)

# Zeitbasierte Aufbewahrung: Snapshots sind ML-Trainingsdaten. Das alte
# Anzahl-Cap (20k) löschte bei 22 Symbolen im 15-min-Takt alles älter ~10 Tage
# und sabotierte damit still den ML-Lookback (120 Tage).
SNAPSHOT_RETENTION_DAYS = 200   # ~140-160 MB bei 22 Symbolen/15min (321 B/Doc)
MAX_SNAPSHOTS = 500_000         # Notbremse gegen Fehlkonfiguration, greift zuerst nie


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- Regime v2 (Fix 0.7b): Vol-Label per Symbol-Perzentil statt globaler
# Fix-Schwellen, "breakout" nur mit Bestätigung (sonst "drift"), Trend mit
# Hysterese gegen Label-Flattern, Mehr-Horizont-Trend (1d/3d) als Kontext.
REGIME_V = 2
TREND_ENTER_PCT = 0.08     # Trend-Eintritt (wie v1)
TREND_EXIT_PCT = 0.05      # Hysterese: bestehender Trend erst darunter verlassen
VOL_RANK_HIGH = 80.0       # aktuelle Vol >= P80 der eigenen Historie -> "volatil"
VOL_RANK_LOW = 30.0        # <= P30 -> "ruhig"
VOL_RANK_WINDOW = 60       # Fenster (Minuten) je Vol-Messung
VOL_RANK_STEP = 15
VOL_RANK_MIN_WINDOWS = 24  # mind. ~6h Historie, sonst Fallback auf Fix-Schwellen
VOL_HISTORY_MINUTES = 2880  # Perzentil-Basis: bis zu 48h eigene Historie
BREAKOUT_VOLUME_RATIO = 1.5


def _vol_percentile_rank(rets: List[float], current_vol: float) -> Optional[float]:
    """Perzentil-Rang der aktuellen 60m-Vol innerhalb der eigenen Symbol-Historie."""
    n = len(rets)
    if n < VOL_RANK_WINDOW + VOL_RANK_STEP * (VOL_RANK_MIN_WINDOWS - 1):
        return None
    vols = [statistics.pstdev(rets[i - VOL_RANK_WINDOW:i])
            for i in range(VOL_RANK_WINDOW, n + 1, VOL_RANK_STEP)]
    if len(vols) < VOL_RANK_MIN_WINDOWS:
        return None
    return round(sum(1 for v in vols if v <= current_vol) / len(vols) * 100, 1)


def compute_features(candles: List[Dict], prev_regime: Optional[str] = None) -> Optional[Dict]:
    """Marktzustands-Features aus 1m-Kerzen (rein, ohne Seiteneffekte)."""
    if not candles or len(candles) < 60:
        return None
    ti = TechnicalIndicators
    closes_full = [float(c["close"]) for c in candles]
    closes = closes_full[-240:]
    price = closes[-1]
    if price <= 0:
        return None
    rsi_arr = ti.calculate_rsi(closes, 14)
    rsi = float(rsi_arr[-1]) if rsi_arr and rsi_arr[-1] is not None else 50.0
    ema20 = ti.calculate_ema(closes, 20)[-1]
    ema50 = ti.calculate_ema(closes, 50)[-1] if len(closes) >= 50 else ema20
    trend_pct = (ema20 - ema50) / price * 100 if ema50 else 0.0
    try:
        atr = float(ti.calculate_atr(candles, 14)[-1] or 0.0)
    except Exception:
        atr = 0.0
    hist = closes_full[-(VOL_HISTORY_MINUTES + 1):]
    rets = [(hist[i] - hist[i - 1]) / hist[i - 1] * 100
            for i in range(1, len(hist)) if hist[i - 1]]
    vol_pct = statistics.pstdev(rets[-60:]) if len(rets) >= 10 else 0.0
    vol_rank = _vol_percentile_rank(rets, vol_pct)
    vols = [float(c.get("volume", 0) or 0) for c in candles]
    v_recent = sum(vols[-5:]) / 5 if len(vols) >= 5 else 0.0
    v_base = (sum(vols[-60:]) / 60) if len(vols) >= 60 else 0.0
    vol_ratio = (v_recent / v_base) if v_base else 1.0
    hi = max(float(c["high"]) for c in candles[-60:])
    lo = min(float(c["low"]) for c in candles[-60:])
    range_pos = (price - lo) / (hi - lo) * 100 if hi > lo else 50.0
    chg_60 = (price - closes[-60]) / closes[-60] * 100 if len(closes) >= 60 and closes[-60] else 0.0
    feats = {
        "price": round(price, 8),
        "rsi": round(rsi, 2),
        "trend_pct": round(trend_pct, 4),
        "atr_pct": round(atr / price * 100, 4),
        "volatility_pct": round(vol_pct, 4),
        "volume_ratio": round(vol_ratio, 3),
        "range_pos": round(range_pos, 2),
        "change_60m_pct": round(chg_60, 3),
        "regime": classify_regime_v2(trend_pct, vol_pct, range_pos,
                                     vol_rank=vol_rank, change_60m_pct=chg_60,
                                     volume_ratio=vol_ratio, prev_regime=prev_regime),
        "regime_v": REGIME_V,
        "vol_basis": "percentile" if vol_rank is not None else "fixed_fallback",
    }
    if vol_rank is not None:
        feats["vol_rank"] = vol_rank
    # Mehr-Horizont-Trend: stabiler Tages-Blick gegen das "Würfeln" im 60m-Bild
    if len(closes_full) >= 1441 and closes_full[-1441]:
        t1d = (price - closes_full[-1441]) / closes_full[-1441] * 100
        feats["trend_1d_pct"] = round(t1d, 3)
        dead = max(8.0 * vol_pct, 0.15)
        feats["daily_bias"] = "up" if t1d >= dead else ("down" if t1d <= -dead else "flat")
    if len(closes_full) >= 4321 and closes_full[-4321]:
        feats["trend_3d_pct"] = round((price - closes_full[-4321]) / closes_full[-4321] * 100, 3)
    return feats


def classify_regime(trend_pct: float, vol_pct: float, range_pos: float) -> str:
    """Regime v1 (fixe Schwellen) – bleibt für Alt-Daten/Regressionstests."""
    if vol_pct >= 0.35:
        base = "volatil"
    elif vol_pct <= 0.06:
        base = "ruhig"
    else:
        base = "normal"
    if trend_pct > 0.08:
        return f"trend_up_{base}"
    if trend_pct < -0.08:
        return f"trend_down_{base}"
    return f"range_{base}" if 20 <= range_pos <= 80 else f"breakout_{base}"


def classify_regime_v2(trend_pct: float, vol_pct: float, range_pos: float, *,
                       vol_rank: Optional[float] = None,
                       change_60m_pct: float = 0.0, volume_ratio: float = 1.0,
                       prev_regime: Optional[str] = None) -> str:
    """Regime v2 (Fix 0.7b, rein/testbar).

    - Vol-Label über Perzentil-Rang der eigenen Symbol-Historie (B1-Fix);
      ohne ausreichende Historie Fallback auf die v1-Fix-Schwellen.
    - Trend mit Hysterese: rein ab ±0.08, raus erst unter ±0.05 (weniger Flattern).
    - "breakout" nur mit Bestätigung: Preis am Range-Rand UND Bewegung in
      Randrichtung UND (60m-Move deutlich über Rauschen ODER Volumen-Spike);
      sonst ehrlich "drift" (B2-Fix).
    """
    if vol_rank is not None:
        base = ("volatil" if vol_rank >= VOL_RANK_HIGH
                else "ruhig" if vol_rank <= VOL_RANK_LOW else "normal")
    else:
        base = ("volatil" if vol_pct >= 0.35
                else "ruhig" if vol_pct <= 0.06 else "normal")
    prev = str(prev_regime or "")
    if trend_pct > TREND_ENTER_PCT or (prev.startswith("trend_up") and trend_pct > TREND_EXIT_PCT):
        return f"trend_up_{base}"
    if trend_pct < -TREND_ENTER_PCT or (prev.startswith("trend_down") and trend_pct < -TREND_EXIT_PCT):
        return f"trend_down_{base}"
    if 20 <= range_pos <= 80:
        return f"range_{base}"
    direction_ok = ((range_pos > 80 and change_60m_pct > 0)
                    or (range_pos < 20 and change_60m_pct < 0))
    move_ok = abs(change_60m_pct) >= max(6.0 * vol_pct, 0.05)
    if direction_ok and (move_ok or volume_ratio >= BREAKOUT_VOLUME_RATIO):
        return f"breakout_{base}"
    return f"drift_{base}"


def snapshot_to_text(snap: Dict) -> str:
    f = snap.get("features") or {}
    extra = ""
    if f.get("trend_1d_pct") is not None:
        extra = f" | 24h {f.get('trend_1d_pct'):+.2f}%"
        if f.get("trend_3d_pct") is not None:
            extra += f" / 3d {f.get('trend_3d_pct'):+.2f}%"
        if f.get("daily_bias"):
            extra += f" (Tages-Bias: {f.get('daily_bias')})"
    return (f"{snap.get('symbol')}: {f.get('regime')} | RSI {f.get('rsi')} | "
            f"Trend {f.get('trend_pct'):+.2f}% | Vola {f.get('volatility_pct')}% | "
            f"ATR {f.get('atr_pct')}% | Vol x{f.get('volume_ratio')} | "
            f"Range-Pos {f.get('range_pos')}%" + extra)


class MarketObserver:
    ROLE = "market_observer"

    def __init__(self):
        self.engine = None
        self.last_run: Optional[str] = None
        self.last_error: Optional[str] = None
        self.snapshots: Dict[str, Dict] = {}     # symbol -> letzter Snapshot
        self.last_summary: Optional[Dict] = None
        self._next_due = 0.0
        self._pruned_at = 0.0
        self._summary_prices: Dict[str, float] = {}
        self._summary_ts = 0.0

    def setup(self, engine):
        self.engine = engine

    @property
    def db(self):
        return self.engine.db if self.engine else None

    def _cfg(self) -> Dict:
        from services.ai_roles import role_manager
        return role_manager.role_cfg(self.ROLE)

    def _symbols(self) -> List[str]:
        return list(getattr(self.engine, "symbols", []) or [])

    # ---------------- collection ----------------
    async def collect(self, persist: bool = True) -> List[Dict]:
        out: List[Dict] = []
        scanner = getattr(self.engine, "scanner", None)
        if scanner is None:
            return out
        ts = _now_iso()
        for sym in self._symbols():
            prev = ((self.snapshots.get(sym) or {}).get("features") or {}).get("regime")
            feats = compute_features(scanner.candle_buffer.get(sym, []), prev_regime=prev)
            if not feats:
                continue
            snap = {"id": str(uuid.uuid4()), "symbol": sym, "ts": ts, "features": feats}
            self.snapshots[sym] = snap
            out.append(snap)
        if persist and out and self.db is not None:
            try:
                await self.db.ai_market_snapshots.insert_many([dict(s) for s in out])
            except Exception as e:
                logger.warning(f"Markt-Snapshots speichern fehlgeschlagen: {e}")
        return out

    def features_for(self, symbol: str) -> Dict:
        """Letzte Features eines Coins (für ML-Vorhersagen & Prompt-Blöcke)."""
        return dict((self.snapshots.get(symbol) or {}).get("features") or {})

    def entry_snapshot(self, symbol: str) -> Optional[Dict]:
        """Marktzustand im Entry-Moment (ML-Fix 0.2): frisch aus dem Kerzen-
        Puffer gerechnet, Fallback auf den letzten 15-min-Snapshot."""
        scanner = getattr(self.engine, "scanner", None) if self.engine else None
        feats = compute_features(
            scanner.candle_buffer.get(symbol, []) if scanner else [],
            prev_regime=((self.snapshots.get(symbol) or {}).get("features") or {}).get("regime"))
        if feats:
            return {"ts": _now_iso(), "source": "live", "features": feats}
        snap = self.snapshots.get(symbol)
        if snap and snap.get("features"):
            return {"ts": snap.get("ts"), "source": "last_snapshot",
                    "features": dict(snap["features"])}
        return None

    async def run_check(self, manual: bool = False) -> Dict:
        cfg = self._cfg()
        if not manual and not cfg.get("enabled", True):
            return {"status": "skipped", "detail": "Rolle deaktiviert"}
        try:
            snaps = await self.collect()
            self.last_run = _now_iso()
            self.last_error = None
            result = {"status": "ok", "snapshots": len(snaps), "ts": self.last_run}
            if cfg.get("llm_summary") and snaps and self.engine and self.engine.key:
                result["summary"] = await self._llm_summary(snaps)
            return result
        except Exception as e:
            self.last_error = str(e)[:300]
            logger.error(f"Markt-Beobachter fehlgeschlagen: {e}")
            return {"status": "error", "detail": self.last_error}

    async def _llm_summary(self, snaps: List[Dict]) -> Optional[str]:
        # Kosten sparen: hat sich der Markt seit der letzten Einschätzung kaum
        # bewegt (<0.3% je Coin) und ist sie <60 min alt, wird der alte Text
        # wiederverwendet statt das LLM erneut zu befragen.
        if self.last_summary and self._summary_prices \
                and (time.time() - self._summary_ts) < 3600:
            changed = False
            for s in snaps:
                sym = s.get("symbol")
                price = float((s.get("features") or {}).get("price") or 0)
                old = self._summary_prices.get(sym)
                if not old or not price or abs(price - old) / old * 100 > 0.3:
                    changed = True
                    break
            if not changed:
                logger.info("Markt-Beobachter: Markt unverändert – alte "
                            "Einschätzung wiederverwendet (LLM-Call gespart)")
                return self.last_summary.get("summary")
        try:
            body = "\n".join(snapshot_to_text(s) for s in snaps[:15])
            text, provider, model = await self.engine.generate_for_role(
                self.ROLE, f"=== GEMESSENER MARKTZUSTAND ===\n{body}\n\n"
                           "Bewerte den Gesamtmarkt als JSON.", OBSERVER_SYSTEM,
                temperature=0.3)
            data = self.engine._parse_json(text)
            summary = str(data.get("summary", ""))[:900]
            self.last_summary = {"regime": data.get("regime"), "summary": summary,
                                 "watchlist": [str(w)[:12] for w in (data.get("watchlist") or [])][:8],
                                 "model": f"{provider}/{model}", "ts": _now_iso()}
            self._summary_prices = {
                s.get("symbol"): float((s.get("features") or {}).get("price") or 0)
                for s in snaps}
            self._summary_ts = time.time()
            await memory.remember("market_observation",
                                  f"Marktzustand {self.last_summary['ts'][:16]}", summary,
                                  meta=self.last_summary, tags=["market"], weight=1,
                                  source=f"market_observer/{model}")
            return summary
        except Exception as e:
            logger.warning(f"Markt-Beobachter LLM-Einschätzung fehlgeschlagen: {str(e)[:140]}")
            return None

    async def context_text(self, limit: int = 12) -> str:
        if not self.snapshots:
            return ""
        lines = ["=== MARKT-BEOBACHTER (gemessener Marktzustand) ==="]
        for snap in list(self.snapshots.values())[:limit]:
            lines.append("- " + snapshot_to_text(snap))
        if self.last_summary:
            lines.append(f"Einschätzung ({self.last_summary.get('regime')}): "
                         f"{self.last_summary.get('summary')}")
        return "\n".join(lines)

    def status(self) -> Dict:
        cfg = self._cfg()
        return {
            "enabled": bool(cfg.get("enabled", True)),
            "interval_min": int(cfg.get("interval_min", 15) or 15),
            "llm_summary": bool(cfg.get("llm_summary", False)),
            "last_run": self.last_run,
            "last_error": self.last_error,
            "symbols_tracked": len(self.snapshots),
            "last_summary": self.last_summary,
        }

    # ---------------- loop ----------------
    async def tick(self):
        cfg = self._cfg()
        if not cfg.get("enabled", True) or self.db is None:
            return
        now = time.time()
        if now < self._next_due:
            return
        self._next_due = now + max(1, int(cfg.get("interval_min", 15) or 15)) * 60
        await self.run_check()
        if now - self._pruned_at > 3600:
            self._pruned_at = now
            await self._prune()

    async def _prune(self):
        try:
            cutoff = (datetime.now(timezone.utc)
                      - timedelta(days=SNAPSHOT_RETENTION_DAYS)).isoformat()
            res = await self.db.ai_market_snapshots.delete_many({"ts": {"$lt": cutoff}})
            if res.deleted_count:
                logger.info(f"Markt-Snapshots: {res.deleted_count} älter als "
                            f"{SNAPSHOT_RETENTION_DAYS} Tage entfernt")
            total = await self.db.ai_market_snapshots.count_documents({})
            if total <= MAX_SNAPSHOTS:
                return
            old = await self.db.ai_market_snapshots.find().sort("ts", 1) \
                .limit(total - MAX_SNAPSHOTS).to_list(total)
            ids = [o.get("id") for o in old if o.get("id")]
            if ids:
                await self.db.ai_market_snapshots.delete_many({"id": {"$in": ids}})
                logger.warning(f"Markt-Snapshots: Notbremse hat {len(ids)} Docs "
                               f"über dem Limit {MAX_SNAPSHOTS} entfernt")
        except Exception as e:
            logger.warning(f"Markt-Snapshot-Housekeeping fehlgeschlagen: {e}")


market_observer = MarketObserver()
