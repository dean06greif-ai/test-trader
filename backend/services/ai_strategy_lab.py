"""KI-Strategie-Labor: eigene Strategie-Ideen der KI sicher zur Live-Reife bringen.

Hintergrund: Der KI Trader ist nur EINE von vielen Strategien der Plattform.
Er darf eigene Strategien entwickeln – aber nicht sofort mit echtem Geld.
Deshalb eine klare Pipeline pro Kandidat:

    ghost  ->  live_pending  ->  (Freigabe des Traders)  ->  paper | live
      |                                                          |
      +-- Ghost-Trades werden nur simuliert mitgeschrieben        +-- rejected

  * `ghost`        : Signale werden NICHT an den AutoTrader geschickt, sondern als
                     Ghost-Trade (reine Simulation ohne Kapital) mitgeschrieben und
                     gegen echte Kurse ausgewertet.
  * `live_pending` : Schwellen (Anzahl Ghost-Trades + Winrate) erreicht – wartet auf
                     die manuelle Freigabe des Traders.
  * `paper`        : darf handeln, wird aber im AutoTrader zu Paper gezwungen.
  * `live`         : vollständig freigegeben (Paper/Live entscheidet wie immer die
                     Coin-Schaltung des Traders).

Vom Trader selbst vorgegebene Strategien können direkt freigegeben werden
(`stage="live"`), ein Backtest ist keine Pflicht. Kandidaten mit maschinenlesbarer
Regel-Definition können zusätzlich als Custom-Strategie registriert werden, damit
Backtester/Optimizer sie rechnen können.
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from services.ai_memory import memory

logger = logging.getLogger(__name__)

COLL = "ai_strategy_candidates"
# Makro-/Struktur-Parameter, die pro eigener KI-Strategie individuell gelten dürfen
MACRO_PARAM_KEYS = ("sl_fixed_percent", "sl_atr_mult", "tp1_crv", "tpf_crv",
                    "tp1_close_percent", "leverage", "trail_atr_mult",
                    "breakeven_offset_percent", "max_capital")
# Nur diese Keys werden als Trade-Overrides an den AutoTrader gegeben
TRADE_OVERRIDE_KEYS = ("tp1_close_percent", "leverage", "trail_atr_mult",
                       "breakeven_offset_percent", "sl_atr_mult")
GHOST_COLL = "ai_ghost_trades"
STAGES = ("ghost", "live_pending", "paper", "live", "rejected")

DEFAULT_SETTINGS = {
    "enabled": True,
    "allow_ai_create": True,
    "min_ghost_trades": 20,
    "min_ghost_winrate": 55.0,
    "promote_to": "paper",          # Ziel-Stufe nach der Freigabe: paper | live
    "max_active_candidates": 5,
    "ghost_timeout_min": 240,        # Ghost-Trade ohne Treffer läuft aus (nicht gewertet)
}

TESTING_NOTE = (
    "Backtest/Optimizer/Strategie-Optimierer kannst du für deine eigenen Strategien nutzen, "
    "SOFERN sie sich in feste Regeln fassen lassen (Indikator-Bedingungen). Gib dafür in der "
    'Strategie-Idee optional "rule_definition" mit: {"timeframe": "1m", "indicators": '
    '{"rsi_period": 14, "ema_fast_period": 9, "ema_slow_period": 50}, "long_rules": '
    '[{"indicator": "rsi", "op": "<", "value": 30}], "short_rules": [...]}.\n'
    "ERLAUBTE SYNTAX (STRIKT – NUR DIESE):\n"
    "- Basis-Indikatoren: price, rsi, ema_fast, ema_slow, sma, ema_gap_pct, ha_color, macd, "
    "macd_signal, macd_hist, bb_upper, bb_middle, bb_lower, bb_width_pct, atr, atr_pct, vwap, "
    "stoch_k, stoch_d, volume, volume_sma, rel_volume, price_change_pct, recent_high, "
    "recent_low, adx, plus_di, minus_di, cci, keltner_upper/middle/lower, donchian_high/low.\n"
    "- Beliebige Perioden per Klammer-Syntax: ema(200), sma(50), rsi(7), atr(20), atr_pct(20), "
    "cci(30), adx(20), recent_high(20), recent_low(20), donchian_high(55), donchian_low(55), "
    "volume_sma(30), price_change_pct(12). NICHT 'ema_200' schreiben, sondern 'ema(200)'.\n"
    "- Zeit-Filter: Indikator 'hour' (Stunde 0-23, Europe/Berlin) mit op 'in_range'/'not_in_range' "
    'und value als Bereich, z.B. {"indicator": "hour", "op": "in_range", "value": [9, 17]}.\n'
    "- Operatoren: <, >, <=, >=, ==, !=, cross_above, cross_below, in_range, not_in_range.\n"
    '- Optional je Regel: "timeframe" (z.B. "5m", "15m", "1h", "4h", "1d") – Multi-Timeframe-'
    "Filter: Die Regel wird dann auf dem höheren Timeframe geprüft (letzte GESCHLOSSENE Kerze, "
    "kein Lookahead). Muss ein Vielfaches des Strategie-Timeframes sein; ohne Angabe gilt der "
    "Strategie-Timeframe. Sinnvoll für Kontext-/Filter-Regeln (Trend über ema(200), RSI-Zone, "
    "VWAP-Lage auf 15m/1h), NICHT für Cross-/Trigger-Regeln – Trigger auf dem Strategie-TF lassen.\n"
    "- Mathe-Ausdrücke als value oder indicator (nur +, -, *, /, Klammern, Zahlen, erlaubte "
    'Indikatoren), z.B. {"indicator": "atr_pct * price", "op": ">", "value": 50} oder '
    '{"indicator": "price", "op": ">", "value": "bb_middle + 2 * atr"}.\n'
    "Alles außerhalb dieser Syntax (eigene Namen wie 'volatility', 'ema_200', 'time', "
    "'not_in_range' mit Einzelwert) ist VERBOTEN und macht die Strategie untestbar.\n"
    "Solche Kandidaten erscheinen automatisch im Backtester/Optimizer (ohne live zu gehen).\n"
    "NICHT sinnvoll testbar sind: news-getriebene Trades, diskretionäre Entscheidungen und "
    "Trades, in denen du live nachjustierst (SL/TP verschieben, Teil-Close, Margin/Hebel "
    "ändern) – ein Backtest würde sie systematisch falsch bewerten. Nutze Backtests also als "
    "Zusatz-Evidenz für den regelbasierten Kern, nicht als Ersatz für deine dynamische, "
    "aktuelle Marktarbeit."
)

ASSIST_SYSTEM = (
    "Du bist der Strategie-Assistent des 'KI Traders' einer Krypto-Daytrading-Plattform und "
    "gleichzeitig der Forschungs-Analyst, der die Rechenergebnisse der Plattform auswertet. "
    "Der Trader beschreibt eine eigene Strategie-Idee. Deine Aufgaben:\n"
    "1. Ehrliches, konstruktives Feedback (Stärken, Schwächen, Risiken).\n"
    "2. Konkrete Verbesserungs-Vorschläge (Einstieg, Ausstieg, Risiko-Management, Filter).\n"
    "3. Eine geschärfte Formulierung der Idee (improved_thesis) und präzise Regeln in "
    "Prosa (improved_rules_text).\n"
    "4. WENN die Idee sich in feste Indikator-Regeln fassen lässt: eine maschinenlesbare "
    "rule_definition für den Backtester (Format siehe unten). Wenn NICHT (news-getrieben, "
    "diskretionär, Live-Nachjustierung nötig): rule_definition = null und erkläre in "
    "backtest_note ehrlich, warum bzw. welcher Teil-Aspekt trotzdem backtestbar wäre.\n"
    "5. Liegen BACKTEST- oder PARAMETER-OPTIMIERUNGS-DATEN zu dieser Strategie vor, nimm "
    "in `data_findings` KONKRET darauf Bezug (Zahlen nennen: PnL, Winrate, Trades, "
    "Walk-Forward/Holdout, Overfitting-Verdacht) und leite daraus die Verbesserungen ab. "
    "Liegen keine Daten vor, schreibe das offen und schlage vor, welcher Test fehlt.\n"
    "6. Berücksichtige deine EIGENEN früheren Einschätzungen zu dieser Strategie "
    "(Verlauf) – widersprich dir nicht, sondern baue darauf auf und sage, was sich "
    "seit dem letzten Mal durch neue Daten geändert hat.\n"
    "Antworte AUSSCHLIESSLICH mit validem JSON ohne Markdown:\n"
    '{"feedback": "3-6 Sätze auf Deutsch", '
    '"suggestions": ["konkreter Verbesserungs-Vorschlag 1", "..."], '
    '"data_findings": ["Erkenntnis aus Backtest-/Optimizer-Daten mit Zahlen"], '
    '"improved_thesis": "geschärfte Idee auf Deutsch", '
    '"improved_rules_text": "präzise Regeln in Prosa", '
    '"rule_definition": {"timeframe": "1m", "indicators": {...}, "long_rules": [...], '
    '"short_rules": [...]} oder null, '
    '"backtest_note": "1-3 Sätze: was der Backtest abbildet bzw. warum nicht backtestbar"}'
)


def valid_rule_definition(d) -> bool:
    """Minimal-Prüfung einer maschinenlesbaren Regel-Definition (rein, testbar)."""
    if not isinstance(d, dict):
        return False
    long_rules = d.get("long_rules")
    short_rules = d.get("short_rules")
    has_long = isinstance(long_rules, list) and len(long_rules) > 0
    has_short = isinstance(short_rules, list) and len(short_rules) > 0
    if not (has_long or has_short):
        return False
    for rules in (long_rules or []), (short_rules or []):
        for r in rules:
            if not (isinstance(r, dict) and r.get("indicator") and r.get("op")):
                return False
    return True


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ghost_stats(trades: List[Dict]) -> Dict:
    """Ghost-Statistik aus abgeschlossenen Ghost-Trades (rein, testbar)."""
    closed = [t for t in trades or [] if t.get("status") == "closed"]
    wins = sum(1 for t in closed if t.get("result") == "win")
    losses = sum(1 for t in closed if t.get("result") == "loss")
    decided = wins + losses
    pnl_pct = round(sum(float(t.get("pnl_pct") or 0) for t in closed), 3)
    return {"trades": len(closed), "wins": wins, "losses": losses,
            "win_rate": round(wins / decided * 100, 1) if decided else 0.0,
            "pnl_pct": pnl_pct,
            "open": sum(1 for t in trades or [] if t.get("status") == "open"),
            "expired": sum(1 for t in trades or [] if t.get("status") == "expired")}


def promotion_ready(stats: Dict, settings: Dict) -> bool:
    """Sind die Ghost-Schwellen erreicht? (rein, testbar)"""
    return (int(stats.get("trades") or 0) >= int(settings.get("min_ghost_trades", 20))
            and float(stats.get("win_rate") or 0) >= float(settings.get("min_ghost_winrate", 55.0)))


def ghost_outcome(side: str, price: float, sl: float, tp: float) -> Optional[str]:
    """Wurde ein Ghost-Trade durch den Kurs entschieden? (rein, testbar)"""
    if side == "LONG":
        if price >= tp:
            return "win"
        if price <= sl:
            return "loss"
    else:
        if price <= tp:
            return "win"
        if price >= sl:
            return "loss"
    return None


class StrategyLab:
    def __init__(self):
        self.engine = None
        self.settings: Dict = dict(DEFAULT_SETTINGS)
        self.last_error: Optional[str] = None
        self._cache: Dict[str, Dict] = {}

    def setup(self, engine):
        self.engine = engine

    @property
    def db(self):
        return self.engine.db if self.engine else None

    # ---------------- state ----------------
    async def load_state(self):
        try:
            doc = await self.db.settings.find_one({"_id": "ai_strategy_lab"})
            if doc:
                for k in DEFAULT_SETTINGS:
                    if k in doc:
                        self.settings[k] = doc[k]
            await self._refresh_cache()
            try:
                res = await self.dedupe_candidates()
                if res.get("count"):
                    logger.info(f"Strategie-Labor: {res['count']} Duplikat(e) "
                                f"beim Start entfernt")
            except Exception as e:
                logger.warning(f"Strategie-Dedupe fehlgeschlagen: {e}")
        except Exception as e:
            logger.warning(f"Strategie-Labor State laden fehlgeschlagen: {e}")

    async def dedupe_candidates(self) -> Dict:
        """Doppelte Kandidaten (gleicher Name) automatisch löschen.
        Behalten wird je Name der am weitesten fortgeschrittene Kandidat
        (Stufe, dann meiste Ghost-Trades, dann der älteste)."""
        rows = await self.db[COLL].find({"stage": {"$ne": "rejected"}}) \
            .sort("created_at", 1).to_list(500)
        by_name: Dict[str, list] = {}
        for r in rows:
            key = str(r.get("name") or "").strip().lower()
            if key:
                by_name.setdefault(key, []).append(r)
        removed = []
        stage_rank = {"live": 3, "paper": 2, "live_pending": 1, "ghost": 0}
        for group in by_name.values():
            if len(group) < 2:
                continue

            def _score(c):
                g = ((c.get("stats") or {}).get("ghost") or {})
                return (stage_rank.get(c.get("stage"), 0),
                        int(g.get("trades") or 0))

            keep = max(group, key=_score)
            for c in group:
                if c["id"] == keep["id"]:
                    continue
                await self.delete_candidate(c["id"])
                removed.append({"id": c["id"], "name": c.get("name")})
        if removed:
            logger.info(f"Strategie-Labor: {len(removed)} doppelte "
                        f"Kandidaten entfernt")
        return {"status": "ok", "removed": removed, "count": len(removed)}

    async def update_settings(self, updates: Dict) -> Dict:
        for key in ("enabled", "allow_ai_create"):
            if key in updates:
                self.settings[key] = bool(updates[key])
        for key, lo, hi in (("min_ghost_trades", 3, 200), ("max_active_candidates", 1, 20),
                            ("ghost_timeout_min", 15, 2880)):
            if key in updates:
                try:
                    self.settings[key] = max(lo, min(hi, int(updates[key])))
                except (TypeError, ValueError):
                    pass
        if "min_ghost_winrate" in updates:
            try:
                self.settings["min_ghost_winrate"] = max(30.0, min(95.0,
                                                                   float(updates["min_ghost_winrate"])))
            except (TypeError, ValueError):
                pass
        if updates.get("promote_to") in ("paper", "live"):
            self.settings["promote_to"] = updates["promote_to"]
        await self.db.settings.update_one({"_id": "ai_strategy_lab"},
                                          {"$set": dict(self.settings)}, upsert=True)
        return dict(self.settings)

    async def _refresh_cache(self):
        rows = await self.db[COLL].find({"stage": {"$ne": "rejected"}}).to_list(100)
        self._cache = {}
        for r in rows:
            r.pop("_id", None)
            self._cache[r["id"]] = r

    # ---------------- Kandidaten ----------------
    async def create_candidate(self, spec: Dict, source: str = "ki") -> Dict:
        if source == "ki" and not self.settings.get("allow_ai_create", True):
            return {"status": "blocked", "detail": "Neue KI-Strategien sind deaktiviert"}
        name = str(spec.get("name") or "").strip()[:80]
        if not name:
            return {"status": "error", "detail": "name fehlt"}
        # Dopplungs-Schutz: gleicher Name (Groß-/Kleinschreibung egal) existiert
        # bereits als nicht-abgelehnter Kandidat -> nicht nochmal anlegen.
        norm = name.strip().lower()
        existing = await self.db[COLL].find(
            {"stage": {"$ne": "rejected"}}, {"name": 1, "id": 1}).to_list(500)
        for r in existing:
            if str(r.get("name") or "").strip().lower() == norm:
                return {"status": "blocked",
                        "detail": f"Strategie „{name}“ existiert bereits – "
                                  f"Duplikat wird nicht angelegt",
                        "candidate_id": r.get("id")}
        active = await self.db[COLL].count_documents({"stage": {"$in": ["ghost", "live_pending"]}})
        if source == "ki" and active >= int(self.settings.get("max_active_candidates", 5)):
            return {"status": "blocked",
                    "detail": f"Zu viele Kandidaten in der Testphase ({active})"}
        stage = str(spec.get("stage") or "ghost")
        if stage not in STAGES or (source == "ki" and stage != "ghost"):
            stage = "ghost"
        cand = {
            "id": f"cand_{uuid.uuid4().hex[:8]}",
            "name": name,
            "thesis": str(spec.get("thesis") or "")[:1200],
            "rules_text": str(spec.get("rules_text") or "")[:1500],
            "symbols": [str(s).upper() for s in (spec.get("symbols") or [])][:10],
            "timeframe": str(spec.get("timeframe") or "1m"),
            "learned_from": str(spec.get("learned_from") or "")[:400],
            "rule_definition": spec.get("rule_definition") if isinstance(
                spec.get("rule_definition"), dict) else None,
            "stage": stage,
            "source": source,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "stats": {"ghost": ghost_stats([]), "live_note": None},
            "custom_strategy_id": None,
            "macro_params": {k: v for k, v in (spec.get("macro_params") or {}).items()
                             if k in MACRO_PARAM_KEYS},
            "trader_note": str(spec.get("trader_note") or "")[:400],
        }
        await self.db[COLL].insert_one(dict(cand))
        self._cache[cand["id"]] = cand
        await self.db.ai_chat.insert_one({
            "id": str(uuid.uuid4()), "role": "strategy",
            "text": (f"Neue Strategie-Idee „{name}“ angelegt "
                     f"({'vom Trader' if source != 'ki' else 'von mir'}) – Stufe {stage}. "
                     + (cand["thesis"][:300] if cand["thesis"] else "")),
            "candidate_id": cand["id"], "stage": stage, "ts": _now_iso()})
        await memory.remember("idea", f"Strategie-Kandidat {name}",
                              f"{cand['thesis']} | Regeln: {cand['rules_text']}",
                              meta={"candidate_id": cand["id"], "source": source},
                              tags=["strategy", "candidate"], weight=2,
                              source="strategy_lab")
        logger.info(f"Strategie-Kandidat angelegt: {name} ({cand['id']}, {stage})")
        # Regelbasierte Ideen sofort für Backtester/Optimizer verfügbar machen
        if cand.get("rule_definition"):
            try:
                reg = await self.register_for_testing(cand["id"])
                if reg.get("status") == "ok":
                    cand["custom_strategy_id"] = reg["strategy_id"]
            except Exception as e:
                logger.warning(f"Auto-Registrierung für Backtest fehlgeschlagen: {e}")
        return {"status": "ok", "candidate": cand}

    async def list_candidates(self, include_rejected: bool = True) -> List[Dict]:
        q = {} if include_rejected else {"stage": {"$ne": "rejected"}}
        rows = await self.db[COLL].find(q).sort("created_at", -1).limit(60).to_list(60)
        for r in rows:
            r.pop("_id", None)
        return rows

    async def get(self, cid: str) -> Optional[Dict]:
        c = await self.db[COLL].find_one({"id": cid})
        if c:
            c.pop("_id", None)
        return c

    async def decide(self, cid: str, action: str, note: str = "") -> Dict:
        """Freigabe/Ablehnung durch den Trader (nur er darf live schalten)."""
        cand = await self.get(cid)
        if not cand:
            return {"status": "error", "detail": "Kandidat nicht gefunden"}
        action = str(action).lower()
        if action == "approve":
            stage = self.settings.get("promote_to", "paper")
        elif action == "approve_live":
            stage = "live"
        elif action == "reject":
            stage = "rejected"
        elif action == "reset":
            stage = "ghost"
        else:
            return {"status": "error", "detail": "action muss approve|approve_live|reject|reset sein"}
        await self.db[COLL].update_one({"id": cid}, {"$set": {
            "stage": stage, "updated_at": _now_iso(),
            "decided_at": _now_iso(), "trader_note": str(note)[:400]}})
        await self._refresh_cache()
        # Ablehnung heißt wirklich AUS: Custom-Registrierung entfernen (Backtester/
        # Dashboard) und offene Trades des Kandidaten schließen. Vorher blieb die
        # Strategie registriert und offene Positionen liefen weiter.
        cleanup = {}
        if action == "reject":
            cleanup = await self._deactivate_candidate(cand)
        cleanup_txt = ""
        if cleanup.get("strategy_removed"):
            cleanup_txt += " Custom-Registrierung entfernt."
        if cleanup.get("closed_trades"):
            cleanup_txt += f" {cleanup['closed_trades']} offene Trades geschlossen."
        await self.db.ai_chat.insert_one({
            "id": str(uuid.uuid4()), "role": "strategy",
            "text": (f"Strategie „{cand['name']}“: Trader-Entscheidung „{action}“ → Stufe {stage}."
                     + (f" Hinweis: {note}" if note else "") + cleanup_txt),
            "candidate_id": cid, "stage": stage, "ts": _now_iso()})
        return {"status": "ok", "candidate": await self.get(cid), **cleanup}

    async def _deactivate_candidate(self, cand: Dict) -> Dict:
        """Kandidat vollständig deaktivieren: als Custom-Strategie deregistrieren
        (DB + Registry + enabled_strategies) und offene Trades schließen."""
        out = {"strategy_removed": False, "closed_trades": 0}
        sid = cand.get("custom_strategy_id")
        if sid:
            try:
                await self.db.custom_strategies.delete_one({"id": sid})
                from strategies.registry import registry as strategy_registry
                strategy_registry.remove_custom(sid)
                from core.state import scanner
                enabled = [s for s in scanner.settings.get("enabled_strategies", [])
                           if s != sid]
                scanner.update_settings({"enabled_strategies": enabled})
                await self.db.settings.update_one({"_id": "scanner_settings"},
                                                  {"$set": scanner.settings}, upsert=True)
                out["strategy_removed"] = True
            except Exception as e:
                logger.warning(f"Custom-Strategie {sid} nicht entfernt: {e}")
        try:
            from core.state import autotrader, scanner
            async for t in self.db.auto_trades.find(
                    {"ai_candidate_id": cand.get("id"), "status": "open"}):
                price = scanner.current_price(t["symbol"]) or t.get("entry")
                res = await autotrader.manual_close(t["id"], price)
                if res:
                    out["closed_trades"] += 1
        except Exception as e:
            logger.warning(f"Offene Trades von {cand.get('id')} nicht geschlossen: {e}")
        return out

    async def delete_candidate(self, cid: str) -> Dict:
        """Kandidat ENDGÜLTIG löschen: Deaktivierung (Registrierung + offene
        Trades) plus Ghost-Trades und Kandidaten-Dokument entfernen."""
        cand = await self.get(cid)
        if not cand:
            return {"status": "error", "detail": "Kandidat nicht gefunden"}
        cleanup = await self._deactivate_candidate(cand)
        await self.db[GHOST_COLL].delete_many({"candidate_id": cid})
        await self.db[COLL].delete_one({"id": cid})
        self._cache.pop(cid, None)
        await self.db.ai_chat.insert_one({
            "id": str(uuid.uuid4()), "role": "strategy",
            "text": (f"Strategie „{cand['name']}“ wurde endgültig gelöscht."
                     + (f" {cleanup['closed_trades']} offene Trades geschlossen."
                        if cleanup.get("closed_trades") else "")),
            "candidate_id": cid, "ts": _now_iso()})
        logger.info(f"Strategie-Kandidat {cid} gelöscht: {cleanup}")
        return {"status": "ok", "deleted": cid, **cleanup}

    def execution_stage(self, cid: Optional[str]) -> Optional[str]:
        """Welche Ausführung ist für diesen Kandidaten erlaubt? (in-memory, schnell)"""
        if not cid:
            return None
        cand = self._cache.get(cid)
        if not cand:
            return "unknown"
        return cand.get("stage")

    # ---------------- Makro-Parameter pro Strategie ----------------
    async def update_macro_params(self, cid: str, changes: Dict) -> Dict:
        """Struktur-Parameter (SL, CRV, Hebel ...) einer eigenen Strategie setzen.
        Wird von der Validierung (`ai_validation`) abgesichert aufgerufen."""
        cand = await self.get(cid)
        if not cand:
            return {"status": "error", "detail": "Kandidat nicht gefunden"}
        macro = dict(cand.get("macro_params") or {})
        applied = {}
        for key, value in (changes or {}).items():
            if key not in MACRO_PARAM_KEYS or value is None:
                continue
            try:
                macro[key] = float(value)
            except (TypeError, ValueError):
                continue
            applied[key] = macro[key]
        await self.db[COLL].update_one({"id": cid}, {"$set": {
            "macro_params": macro, "updated_at": _now_iso()}})
        await self._refresh_cache()
        logger.info(f"Makro-Parameter für {cid} gesetzt: {applied}")
        return {"status": "ok", "macro_params": macro, "applied": applied}

    def macro_params(self, cid: Optional[str]) -> Dict:
        """Aktive Makro-Parameter eines Kandidaten (in-memory, für Signal-Levels)."""
        if not cid:
            return {}
        cand = self._cache.get(cid) or {}
        if cand.get("stage") not in ("paper", "live"):
            return {}
        return dict(cand.get("macro_params") or {})

    def trade_overrides(self, cid: Optional[str]) -> Optional[Dict]:
        """Nur die Parameter, die der AutoTrader direkt übernehmen darf."""
        macro = self.macro_params(cid)
        out = {k: macro[k] for k in TRADE_OVERRIDE_KEYS if k in macro}
        return out or None

    # ---------------- Ghost-Trading ----------------
    async def record_ghost_trade(self, cid: str, symbol: str, side: str, entry: float,
                                 sl: float, tp: float, reason: str = "") -> Dict:
        gt = {
            "id": f"ghost_{uuid.uuid4().hex[:10]}", "candidate_id": cid,
            "symbol": str(symbol).upper(), "side": str(side).upper(),
            "entry": float(entry), "sl": float(sl), "tp": float(tp),
            "reason": str(reason)[:300], "status": "open",
            "opened_at": _now_iso(),
        }
        await self.db[GHOST_COLL].insert_one(dict(gt))
        logger.info(f"Ghost-Trade {gt['side']} {gt['symbol']} für Kandidat {cid}")
        return gt

    async def _evaluate_ghosts(self):
        open_rows = await self.db[GHOST_COLL].find({"status": "open"}).limit(200).to_list(200)
        if not open_rows:
            return
        touched = set()
        timeout_min = int(self.settings.get("ghost_timeout_min", 240))
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=timeout_min)
        for gt in open_rows:
            # Ghost-Trades ohne Entscheidung laufen aus – sonst blockieren sie die
            # Statistik dauerhaft und die Strategie käme nie zur Bewertung.
            try:
                opened = datetime.fromisoformat(str(gt.get("opened_at")))
                if opened.tzinfo is None:
                    opened = opened.replace(tzinfo=timezone.utc)
            except Exception:
                opened = None
            if opened and opened < cutoff:
                await self.db[GHOST_COLL].update_one({"id": gt["id"]}, {"$set": {
                    "status": "expired", "result": "expired",
                    "closed_at": _now_iso()}})
                touched.add(gt["candidate_id"])
                continue
            price = None
            try:
                price = self.engine.scanner.current_price(gt["symbol"])
            except Exception:
                price = None
            if not price:
                continue
            res = ghost_outcome(gt["side"], float(price), float(gt["sl"]), float(gt["tp"]))
            if not res:
                continue
            exit_price = float(gt["tp"]) if res == "win" else float(gt["sl"])
            move = (exit_price - gt["entry"]) if gt["side"] == "LONG" else (gt["entry"] - exit_price)
            pnl_pct = round(move / gt["entry"] * 100, 4) if gt["entry"] else 0.0
            await self.db[GHOST_COLL].update_one({"id": gt["id"]}, {"$set": {
                "status": "closed", "result": res, "exit_price": exit_price,
                "pnl_pct": pnl_pct, "closed_at": _now_iso()}})
            touched.add(gt["candidate_id"])
        for cid in touched:
            await self.refresh_candidate_stats(cid)

    async def real_stats(self, cid: str) -> Dict:
        """Ergebnisse der echten (Paper/Live-)Trades dieser Strategie."""
        try:
            rows = await self.db.auto_trades.find(
                {"ai_candidate_id": cid, "status": "closed"},
                {"result": 1, "realized_pnl": 1, "mode": 1}).limit(500).to_list(500)
        except Exception:
            return {"trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "pnl": 0.0}
        wins = sum(1 for r in rows if r.get("result") == "win")
        losses = sum(1 for r in rows if r.get("result") == "loss")
        decided = wins + losses
        return {"trades": len(rows), "wins": wins, "losses": losses,
                "win_rate": round(wins / decided * 100, 1) if decided else 0.0,
                "pnl": round(sum(float(r.get("realized_pnl") or 0) for r in rows), 4)}

    async def refresh_candidate_stats(self, cid: str) -> Dict:
        rows = await self.db[GHOST_COLL].find({"candidate_id": cid}).limit(500).to_list(500)
        stats = ghost_stats(rows)
        cand = await self.get(cid)
        if not cand:
            return stats
        updates = {"stats.ghost": stats, "stats.real": await self.real_stats(cid),
                   "updated_at": _now_iso()}
        if cand.get("stage") == "ghost" and promotion_ready(stats, self.settings):
            updates["stage"] = "live_pending"
            updates["promotion_ready_at"] = _now_iso()
            await self.db.ai_chat.insert_one({
                "id": str(uuid.uuid4()), "role": "strategy",
                "text": (f"Strategie „{cand['name']}“ hat die Ghost-Phase bestanden: "
                         f"{stats['trades']} Trades, Winrate {stats['win_rate']}%, "
                         f"Summe {stats['pnl_pct']}%. Sie wartet jetzt auf DEINE Freigabe, "
                         f"bevor sie {self.settings.get('promote_to', 'paper')} handeln darf."),
                "candidate_id": cid, "stage": "live_pending", "ts": _now_iso()})
            await memory.remember(
                "research_insight", f"Ghost-Phase bestanden: {cand['name']}",
                f"{stats['trades']} Ghost-Trades, Winrate {stats['win_rate']}%, "
                f"Summe {stats['pnl_pct']}%.", meta={"candidate_id": cid, "stats": stats},
                tags=["strategy", "promotion"], weight=3, source="strategy_lab")
        await self.db[COLL].update_one({"id": cid}, {"$set": updates})
        await self._refresh_cache()
        return stats

    async def ghost_trades(self, cid: Optional[str] = None, limit: int = 50) -> List[Dict]:
        q = {"candidate_id": cid} if cid else {}
        rows = await self.db[GHOST_COLL].find(q).sort("opened_at", -1) \
            .limit(max(1, min(300, limit))).to_list(300)
        for r in rows:
            r.pop("_id", None)
        return rows

    # ---------------- Backtest-Anbindung ----------------
    async def test_context(self, cid: str, max_chars: int = 2600) -> str:
        """Backtest- und Parameter-Optimierungs-Daten ZU DIESER Strategie.

        Nutzt dieselben Aufbereitungs-Funktionen wie der Forschungs-Analyst des
        KI-Teams, damit die Strategie-KI exakt dieselbe Datenbasis sieht."""
        from services.ai_research import digest_backtests, digest_optimizer
        cand = await self.get(cid)
        if not cand:
            return "(Kandidat nicht gefunden)"
        sid = cand.get("custom_strategy_id")
        if not sid:
            return ("(noch keine Backtest-/Optimizer-Läufe: die Strategie ist nicht als "
                    "testbare Strategie registriert)")
        try:
            backtests = await self.db.backtests.find({"$or": [
                {"params.strategies": sid},
                {"result.per_strategy.strategy_id": sid},
            ]}).sort("created_at", -1).limit(5).to_list(5)
            optimizer = await self.db.optimizer_runs.find({"$or": [
                {"params.strategy_id": sid},
                {"result.strategy_id": sid},
            ]}).sort("created_at", -1).limit(5).to_list(5)
        except Exception as e:
            logger.warning(f"Testdaten für {cid} nicht ladbar: {e}")
            return "(Testdaten konnten nicht geladen werden)"
        for rows in (backtests, optimizer):
            for r in rows:
                r.pop("_id", None)
        txt = ("=== BACKTESTS ZU DIESER STRATEGIE ===\n"
               f"{digest_backtests(backtests)}\n\n"
               "=== PARAMETER-OPTIMIERUNG ZU DIESER STRATEGIE ===\n"
               f"{digest_optimizer(optimizer)}")
        return txt[:max_chars]

    async def assist_history(self, cid: str, limit: int = 3) -> List[Dict]:
        """Frühere KI-Einschätzungen zu einer Strategie (Chat-Gedächtnis)."""
        cand = await self.get(cid)
        rows = (cand or {}).get("assist_history") or []
        return rows[-max(1, limit):]

    @staticmethod
    def _history_text(rows: List[Dict]) -> str:
        if not rows:
            return "(noch keine frühere Einschätzung)"
        out = []
        for r in rows:
            out.append(f"- [{str(r.get('ts', ''))[:16]}] {str(r.get('feedback', ''))[:400]}"
                       + ("\n  Vorschläge: " + "; ".join(
                           str(s)[:120] for s in (r.get("suggestions") or [])[:3])
                          if r.get("suggestions") else ""))
        return "\n".join(out)

    async def assist(self, spec: Dict, cid: Optional[str] = None,
                     apply_rules: bool = False) -> Dict:
        """KI-Hilfe für Trader-Strategien: Feedback, Verbesserungen und – wenn
        möglich – eine maschinenlesbare rule_definition für den Backtester.

        Für bestehende Kandidaten fließen die BACKTEST-/OPTIMIZER-Ergebnisse
        dieser Strategie und der eigene Gesprächsverlauf mit ein, damit die KI
        aus den Tests lernt und darauf Bezug nehmen kann."""
        cand = await self.get(cid) if cid else None
        if cid and not cand:
            return {"status": "error", "detail": "Kandidat nicht gefunden"}
        name = str(spec.get("name") or (cand or {}).get("name") or "Neue Strategie")[:80]
        thesis = str(spec.get("thesis") or (cand or {}).get("thesis") or "")[:1500]
        rules_text = str(spec.get("rules_text") or (cand or {}).get("rules_text") or "")[:1500]
        if not (thesis.strip() or rules_text.strip()):
            return {"status": "error", "detail": "Bitte zuerst die Strategie beschreiben (Idee/Regeln)"}
        if not self.engine or not self.engine.key:
            return {"status": "error", "detail": "Kein API-Key für den aktiven KI-Provider"}
        symbols = [str(s).upper() for s in (spec.get("symbols")
                                            or (cand or {}).get("symbols") or [])][:10]
        data_block, history_block = "", ""
        if cand:
            data_block = await self.test_context(cid)
            history_block = self._history_text(await self.assist_history(cid))
            g = (cand.get("stats") or {}).get("ghost") or {}
            real = (cand.get("stats") or {}).get("real") or {}
            data_block += (f"\n\n=== EIGENE ERGEBNISSE ===\nGhost: {g.get('trades', 0)} Trades, "
                           f"Winrate {g.get('win_rate', 0)}%, Summe {g.get('pnl_pct', 0)}% | "
                           f"Echt: {real.get('trades', 0)} Trades, Winrate "
                           f"{real.get('win_rate', 0)}%, PnL {real.get('pnl', 0)} USDT")
        prompt = (
            f"{TESTING_NOTE}\n\n"
            f"=== STRATEGIE DES TRADERS ===\n"
            f"Name: {name}\n"
            f"Coins: {', '.join(symbols) or 'alle beobachteten Assets'}\n"
            f"Idee/Beschreibung: {thesis or '(leer)'}\n"
            f"Regeln (Prosa): {rules_text or '(keine)'}\n\n"
            + (f"{data_block}\n\n" if data_block else "")
            + (f"=== DEINE FRÜHEREN EINSCHÄTZUNGEN ZU DIESER STRATEGIE ===\n{history_block}\n\n"
               if history_block else "")
            + "Gib jetzt Feedback, konkrete Verbesserungen (mit Bezug auf die Daten) und – "
              "falls machbar – die maschinenlesbare rule_definition."
        )
        try:
            # Bewusst die Rolle des Forschungs-Analysten: dieselbe KI, die auch
            # Backtests/Optimizer-Läufe des Teams auswertet.
            text, provider, model = await self.engine.generate_for_role(
                "research_analyst", prompt, ASSIST_SYSTEM, temperature=0.3)
            data = self.engine._parse_json(text)
        except Exception as e:
            logger.error(f"Strategie-Assistent fehlgeschlagen: {e}")
            return {"status": "error", "detail": f"KI-Antwort fehlgeschlagen: {str(e)[:150]}"}
        rd = data.get("rule_definition")
        if not valid_rule_definition(rd):
            rd = None
        out = {
            "status": "ok", "model": f"{provider}/{model}",
            "candidate_id": cid,
            "ts": _now_iso(),
            "feedback": str(data.get("feedback", ""))[:1500],
            "suggestions": [str(s)[:300] for s in (data.get("suggestions") or [])
                            if isinstance(s, str)][:6],
            "data_findings": [str(s)[:300] for s in (data.get("data_findings") or [])
                              if isinstance(s, str)][:6],
            "improved_thesis": str(data.get("improved_thesis", ""))[:1200],
            "improved_rules_text": str(data.get("improved_rules_text", ""))[:1500],
            "rule_definition": rd,
            "backtestable": bool(rd),
            "backtest_note": str(data.get("backtest_note", ""))[:500],
        }
        if cand:
            await self._remember_assist(cid, cand["name"], out)
        if cand and rd and apply_rules:
            await self.db[COLL].update_one({"id": cid}, {"$set": {
                "rule_definition": rd, "updated_at": _now_iso()}})
            await self._refresh_cache()
            out["registered"] = await self.register_for_testing(cid)
        return out

    async def _remember_assist(self, cid: str, name: str, out: Dict):
        """Einschätzung dauerhaft an der Strategie ablegen (Gesprächs-Gedächtnis)
        und im KI-Feed sichtbar machen. `last_assist` hält die vollständige
        Antwort, damit der Trader Vorschläge später übernehmen kann."""
        entry = {k: out.get(k) for k in ("ts", "model", "feedback", "suggestions",
                                         "data_findings", "backtest_note")}
        try:
            await self.db[COLL].update_one({"id": cid}, {
                "$push": {"assist_history": {"$each": [entry], "$slice": -5}},
                "$set": {"last_assist": {k: out.get(k) for k in (
                    "ts", "model", "feedback", "suggestions", "data_findings",
                    "improved_thesis", "improved_rules_text", "rule_definition",
                    "backtest_note")}}})
            await self._refresh_cache()
        except Exception as e:
            logger.warning(f"Assist-Verlauf für {cid} nicht gespeichert: {e}")
        try:
            await self.db.ai_chat.insert_one({
                "id": str(uuid.uuid4()), "role": "strategy",
                "text": (f"Einschätzung zu „{name}“: {out.get('feedback', '')[:600]}"),
                "candidate_id": cid,
                "suggestions": out.get("suggestions") or [],
                "data_findings": out.get("data_findings") or [],
                "model": out.get("model"), "ts": out.get("ts") or _now_iso()})
            await memory.remember(
                "research_insight", f"Strategie-Einschätzung: {name}",
                out.get("feedback", ""),
                meta={"candidate_id": cid, "data_findings": out.get("data_findings") or []},
                tags=["strategy", "assist"], weight=2, source="strategy_lab")
        except Exception as e:
            logger.debug(f"Assist-Feed für {cid}: {e}")

    async def apply_assist(self, cid: str, fields: Optional[List[str]] = None) -> Dict:
        """Verbesserungs-Vorschläge der KI in die Strategie übernehmen.

        `fields` wählt aus: "rule_definition" (Backtest-Regeln, danach direkt für
        Backtester/Optimizer registriert), "thesis" und "rules_text". Ohne Angabe
        wird alles übernommen, was die KI geliefert hat. Der Verlauf bleibt
        erhalten – nichts wird überschrieben, was die KI nicht vorgeschlagen hat."""
        cand = await self.get(cid)
        if not cand:
            return {"status": "error", "detail": "Kandidat nicht gefunden"}
        last = cand.get("last_assist") or {}
        if not last:
            return {"status": "error", "detail": "Noch keine KI-Einschätzung vorhanden"}
        wanted = set(fields or ["rule_definition", "thesis", "rules_text"])
        patch: Dict = {}
        if "thesis" in wanted and last.get("improved_thesis"):
            patch["thesis"] = str(last["improved_thesis"])[:1500]
        if "rules_text" in wanted and last.get("improved_rules_text"):
            patch["rules_text"] = str(last["improved_rules_text"])[:1500]
        rd = last.get("rule_definition")
        if "rule_definition" in wanted and valid_rule_definition(rd):
            patch["rule_definition"] = rd
        if not patch:
            return {"status": "error",
                    "detail": "Die KI hat für diese Auswahl nichts vorgeschlagen"}
        patch["updated_at"] = _now_iso()
        await self.db[COLL].update_one({"id": cid}, {"$set": patch})
        await self._refresh_cache()
        out = {"status": "ok", "applied": [k for k in patch if k != "updated_at"]}
        if "rule_definition" in patch:
            out["registered"] = await self.register_for_testing(cid)
        await self.db.ai_chat.insert_one({
            "id": str(uuid.uuid4()), "role": "strategy",
            "text": (f"Verbesserungs-Vorschläge für „{cand['name']}“ übernommen: "
                     f"{', '.join(out['applied'])}."),
            "candidate_id": cid, "ts": _now_iso()})
        out["candidate"] = await self.get(cid)
        return out

    async def register_for_testing(self, cid: str) -> Dict:
        """Kandidat mit Regel-Definition als Custom-Strategie registrieren, damit
        Backtester/Optimizer ihn rechnen können (ohne ihn live zu schalten)."""
        cand = await self.get(cid)
        if not cand:
            return {"status": "error", "detail": "Kandidat nicht gefunden"}
        if cand.get("stage") == "rejected":
            return {"status": "blocked",
                    "detail": "Kandidat wurde abgelehnt – zuerst über „Zurück in "
                              "Ghost“ (reset) reaktivieren"}
        definition = cand.get("rule_definition")
        if not isinstance(definition, dict) or not (definition.get("long_rules")
                                                    or definition.get("short_rules")):
            return {"status": "not_testable",
                    "detail": "Kandidat hat keine maschinenlesbaren Regeln "
                              "(news-/diskretionär getriebene Ideen sind nicht backtestbar)"}
        sid = cand.get("custom_strategy_id") or f"custom_{uuid.uuid4().hex[:8]}"
        definition = {**definition, "id": sid,
                      "name": f"KI-Kandidat: {cand['name']}",
                      "description": (cand.get("thesis") or "")[:300],
                      "timeframe": definition.get("timeframe") or cand.get("timeframe") or "1m"}
        # Strikte Prüfung + Alias-Auto-Fix: nicht auswertbare KI-Regeln werden
        # sofort abgewiesen (mit Problemen + Korrektur-Vorschlägen) statt eine
        # Strategie zu registrieren, die im Backtest still 0/0/0 liefert.
        from strategies import custom_params
        from strategies.custom_strategy import INDICATORS, OPERATORS
        normalized, problems = custom_params.normalize_definition(
            definition, INDICATORS, OPERATORS)
        if problems:
            return {"status": "not_testable",
                    "detail": "Regeln nicht auswertbar: " + "; ".join(problems[:5]),
                    "problems": problems,
                    "fixes": custom_params.fix_suggestions(
                        definition, INDICATORS, OPERATORS)}
        definition = normalized
        await self.db.custom_strategies.update_one({"id": sid}, {"$set": definition}, upsert=True)
        try:
            from strategies.registry import registry as strategy_registry
            strategy_registry.upsert_custom(definition)
        except Exception as e:
            logger.warning(f"Kandidat {cid} konnte nicht registriert werden: {e}")
        await self.db[COLL].update_one({"id": cid}, {"$set": {
            "custom_strategy_id": sid, "updated_at": _now_iso()}})
        await self._refresh_cache()
        return {"status": "ok", "strategy_id": sid, "definition": definition,
                "note": "Im Backtester/Optimizer wählbar. " + TESTING_NOTE}

    # ---------------- Prompt-Kontext ----------------
    async def context_text(self) -> str:
        try:
            rows = await self.list_candidates(include_rejected=False)
        except Exception:
            rows = []
        s = self.settings
        lines = [
            "=== DEIN STRATEGIE-LABOR (eigene Strategien sicher testen) ===",
            f"Pipeline: ghost → live_pending → Freigabe des Traders → "
            f"{s.get('promote_to', 'paper')}. Schwellen: mind. {s['min_ghost_trades']} "
            f"Ghost-Trades und {s['min_ghost_winrate']}% Winrate. Ohne Freigabe des Traders "
            "handelt KEINE neue Strategie mit echtem Geld.",
            "Willst du eine neue Strategie testen, gib sie im Analyse-JSON unter "
            '"new_strategies": [{"name": "...", "thesis": "...", "rules_text": "...", '
            '"symbols": ["BTCUSDT"], "learned_from": "welche bestehende Strategie/Parameter '
            'dich inspiriert hat", "rule_definition": {...optional, siehe unten...}}] '
            'zurück. Entscheidungen, die zu einem Kandidaten gehören, markiere mit '
            '"strategy_candidate_id".',
            "Eigene Makro-Parameter pro Strategie (SL/CRV/Hebel/TP1-Anteil) kannst du über "
            'config_changes mit "symbol": "<candidate_id>" anpassen – sie gelten nur für Trades '
            "dieser Strategie und werden mit deren eigener Trade-Stichprobe validiert "
            "(Schutz vor Overfitting).",
            TESTING_NOTE,
        ]
        if rows:
            lines.append("Aktuelle Kandidaten:")
            for c in rows[:8]:
                g = (c.get("stats") or {}).get("ghost") or {}
                macro = c.get("macro_params") or {}
                macro_txt = ", ".join(f"{k}={v}" for k, v in macro.items()) or "Standard"
                real = (c.get("stats") or {}).get("real") or {}
                real_txt = (f" | echte Trades {real.get('trades', 0)}, Winrate "
                            f"{real.get('win_rate', 0)}%, PnL {real.get('pnl', 0)} USDT"
                            if real.get("trades") else "")
                lines.append(
                    f"- {c['id']} „{c['name']}“ [{c['stage']}]{real_txt}: {g.get('trades', 0)} Ghost-Trades, "
                    f"Winrate {g.get('win_rate', 0)}%, Summe {g.get('pnl_pct', 0)}% | "
                    f"Coins {', '.join(c.get('symbols') or []) or 'alle'} | "
                    f"Makro-Parameter: {macro_txt} | "
                    f"Idee: {(c.get('thesis') or '')[:140]}")
        else:
            lines.append("Aktuelle Kandidaten: (keine)")
        return "\n".join(lines)

    async def tick(self):
        if self.db is None or not self.settings.get("enabled", True):
            return
        try:
            await self._evaluate_ghosts()
            for cid, cand in list(self._cache.items()):
                if cand.get("stage") in ("paper", "live"):
                    real = await self.real_stats(cid)
                    if real != (cand.get("stats") or {}).get("real"):
                        await self.db[COLL].update_one({"id": cid},
                                                       {"$set": {"stats.real": real}})
                        cand.setdefault("stats", {})["real"] = real
        except Exception as e:
            self.last_error = str(e)[:200]
            logger.error(f"Ghost-Auswertung fehlgeschlagen: {e}")

    def status(self) -> Dict:
        return {"settings": dict(self.settings), "stages": list(STAGES),
                "active": len([c for c in self._cache.values()
                               if c.get("stage") in ("ghost", "live_pending")]),
                "last_error": self.last_error}


strategy_lab = StrategyLab()
