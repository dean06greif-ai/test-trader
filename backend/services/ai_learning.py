"""Selbst-Lernen des KI Traders.

Verknüpft die KI-Entscheidungen (ai_decisions) mit ihren echten Ergebnissen
(Signal-Win/Loss + geschlossene Paper-/Live-Trades), aggregiert daraus eine
Performance-Statistik und lässt das LLM daraus kompakte, umsetzbare
"Lektionen" ableiten. Die Lektionen + Statistik fließen in jede Analyse und
in den Chat ein – unabhängig davon, was der Nutzer schreibt.

Trigger: automatisch nach geschlossenen Trades (mit Mindestabstand),
täglich beim 00:00-Berlin-Reset und manuell per Endpoint.
"""
import logging
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

from services import ai_lessons
from services.ai_lessons import lesson_store
from services.ai_master_prompt import master_prompt
from services.ai_validation import validation_gate

logger = logging.getLogger(__name__)

LEARNING_SYSTEM = (
    "Du bist der 'KI Trader' einer Krypto-Daytrading-Plattform und wertest deine EIGENE "
    "Trading-Performance aus, um besser zu werden. Sei brutal ehrlich und rein datenbasiert. "
    "Der MASTERPROMPT des Traders ist das oberste Gebot: Lektionen, die ihm widersprechen, "
    "werden automatisch verworfen – formuliere sie nicht. Lektionen, die als 'VOM TRADER "
    "FESTGELEGT/ANGEPASST' markiert sind, darfst du NICHT ändern oder verwerfen; behandle sie "
    "als gesetzt und richte deine übrigen Lektionen widerspruchsfrei daran aus. "
    "Erkenne Muster: Welche Coins/Richtungen/Konfidenz-Level funktionieren, welche nicht? "
    "Passen SL/TP/Hebel zum beobachteten Verhalten (z.B. SL zu eng -> viele knappe Stop-Outs, "
    "TP zu weit -> Gewinne drehen ins Minus)? Behalte bewährte alte Lektionen bei, verwirf "
    "widerlegte, formuliere neue NUR bei ausreichender Datenbasis (siehe DATEN-VALIDIERUNG). "
    "Bei sehr wenigen Daten sei zurückhaltend und markiere Lektionen als vorläufig. "
    "AUSNAHME: Hat der Trader dir explizit angewiesen, eine bestimmte Lektion aufzunehmen "
    "oder zu aktivieren, setze bei dieser Lektion \"trader_directive\": true – sie wird "
    "dann SOFORT ohne Validierungs-Wartezeit aktiv. Hältst du sie für riskant oder "
    "schlecht, schreibe deine ehrliche Einschätzung in \"critique\" (sie gilt trotzdem). "
    "Antworte AUSSCHLIESSLICH mit validem JSON ohne Markdown, exakt in diesem Schema:\n"
    '{"assessment": "3-6 Sätze ehrliche Selbsteinschätzung auf Deutsch", '
    '"lessons": [{"title": "Kurztitel", "detail": "konkrete, umsetzbare Regel auf Deutsch", '
    '"trader_directive": false, "critique": ""}], '
    '"removed_lessons": ["Titel einer widerlegten Lektion"], '
    '"contradictory_lessons": ["Titel der schwächer validierten Lektion bei Widerspruch/Doppelung"], '
    '"config_changes": [{"symbol": "BTCUSDT", "changes": {}, "reason": "kurz"}]}\n'
    "In 'lessons' gehören NEUE und AKTUALISIERTE Lektionen. Bestehende Lektionen bleiben "
    "automatisch gespeichert – wiederhole sie nur, wenn du ihren Text schärfen willst. "
    "NEUE Lektionen werden erst nach mehrfacher Wiedererkennung aktiv: siehst du eine "
    "Erkenntnis aus der Kandidaten-Liste erneut in den Daten bestätigt, gib sie mit EXAKT "
    "demselben Titel zurück – nur so wird sie wiedererkannt und validiert. "
    "Nur ausdrücklich widerlegte Lektionen in 'removed_lessons' auflisten.\n"
    "Prüfe die BISHERIGEN LEKTIONEN außerdem aktiv auf Doppelungen und inhaltliche "
    "Widersprüche: gib in 'contradictory_lessons' die Titel der jeweils SCHWÄCHER "
    "validierten Lektion an (weniger Bestätigungen/geringeres Gewicht) – es bleibt "
    "nur die am besten validierte Aussage bestehen. Lektionen, die dem MasterPrompt "
    "widersprechen, werden ohnehin automatisch entfernt.\n"
    "config_changes nur angeben, wenn im Prompt ausdrücklich erlaubt – sonst leere Liste."
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def aggregate_performance(signals: List[Dict], trades: List[Dict]) -> Dict:
    """Reine Aggregation (testbar): Signal- und Trade-Listen -> Statistik-Dict."""
    sigs = [s for s in signals if s.get("signal_class") != "PRE_SIGNAL"]
    wins = sum(1 for s in sigs if s.get("result") == "win")
    losses = sum(1 for s in sigs if s.get("result") == "loss")
    decided = wins + losses

    by_symbol: Dict[str, Dict] = {}
    by_action = {"LONG": {"total": 0, "wins": 0, "losses": 0},
                 "SHORT": {"total": 0, "wins": 0, "losses": 0}}
    conf_buckets = {"<70": {"total": 0, "wins": 0, "losses": 0},
                    "70-79": {"total": 0, "wins": 0, "losses": 0},
                    ">=80": {"total": 0, "wins": 0, "losses": 0}}
    for s in sigs:
        d = by_symbol.setdefault(s.get("symbol"), {"signals": 0, "wins": 0, "losses": 0,
                                                   "trades": 0, "pnl": 0.0})
        d["signals"] += 1
        res = s.get("result")
        if res == "win":
            d["wins"] += 1
        elif res == "loss":
            d["losses"] += 1
        a = by_action.get(s.get("type"))
        if a is not None:
            a["total"] += 1
            if res == "win":
                a["wins"] += 1
            elif res == "loss":
                a["losses"] += 1
        conf = s.get("ai_confidence")
        if conf is not None:
            b = conf_buckets["<70" if conf < 70 else ("70-79" if conf < 80 else ">=80")]
            b["total"] += 1
            if res == "win":
                b["wins"] += 1
            elif res == "loss":
                b["losses"] += 1

    closed = [t for t in trades if t.get("status") == "closed"]
    modes: Dict[str, Dict] = {}
    for m in ("paper", "live"):
        mt = [t for t in closed if t.get("mode") == m]
        pnl = sum(float(t.get("realized_pnl", 0) or 0) for t in mt)
        w = sum(1 for t in mt if float(t.get("realized_pnl", 0) or 0) > 0)
        modes[m] = {"count": len(mt), "pnl": round(pnl, 4), "wins": w, "losses": len(mt) - w,
                    "win_rate": round(w / len(mt) * 100, 1) if mt else 0.0,
                    "avg_pnl": round(pnl / len(mt), 4) if mt else 0.0}
        for t in mt:
            d = by_symbol.setdefault(t.get("symbol"), {"signals": 0, "wins": 0, "losses": 0,
                                                       "trades": 0, "pnl": 0.0})
            d["trades"] += 1
            d["pnl"] = round(d["pnl"] + float(t.get("realized_pnl", 0) or 0), 4)

    traded = {s: v for s, v in by_symbol.items() if v["trades"] > 0}
    best = max(traded, key=lambda s: traded[s]["pnl"]) if traded else None
    worst = min(traded, key=lambda s: traded[s]["pnl"]) if traded else None

    return {
        "totals": {
            "signals": len(sigs), "signal_wins": wins, "signal_losses": losses,
            "signal_win_rate": round(wins / decided * 100, 1) if decided else 0.0,
            "closed_trades": len(closed),
            "open_trades": sum(1 for t in trades if t.get("status") == "open"),
            "total_pnl": round(modes["paper"]["pnl"] + modes["live"]["pnl"], 4),
        },
        "by_symbol": by_symbol,
        "by_action": by_action,
        "confidence_buckets": conf_buckets,
        "trades": modes,
        "best_symbol": best,
        "worst_symbol": worst,
    }


def performance_to_text(stats: Dict) -> str:
    t = stats.get("totals", {})
    tr = stats.get("trades", {})
    lines = [
        f"Signale (letzte {stats.get('lookback_days', '?')} Tage): {t.get('signals', 0)} gesamt, "
        f"{t.get('signal_wins', 0)} Win / {t.get('signal_losses', 0)} Loss "
        f"(Winrate {t.get('signal_win_rate', 0)}%)",
    ]
    for m, label in (("paper", "Paper"), ("live", "LIVE")):
        d = tr.get(m, {})
        if d.get("count"):
            lines.append(f"{label}-Trades: {d['count']} geschlossen, PnL {d['pnl']:+.2f} USDT, "
                         f"Winrate {d['win_rate']}%, Ø {d['avg_pnl']:+.2f} USDT/Trade")
    if not tr.get("paper", {}).get("count") and not tr.get("live", {}).get("count"):
        lines.append("Trades: noch keine geschlossenen KI-Trades")
    ba = stats.get("by_action", {})
    for a in ("LONG", "SHORT"):
        d = ba.get(a, {})
        dec = d.get("wins", 0) + d.get("losses", 0)
        if d.get("total"):
            wr = round(d["wins"] / dec * 100, 1) if dec else 0.0
            lines.append(f"{a}: {d['total']} Signale, Winrate {wr}%")
    cb = stats.get("confidence_buckets", {})
    cb_parts = []
    for k, d in cb.items():
        dec = d.get("wins", 0) + d.get("losses", 0)
        if dec:
            cb_parts.append(f"{k}%: {round(d['wins'] / dec * 100)}% Winrate ({dec} entschieden)")
    if cb_parts:
        lines.append("Nach Konfidenz: " + " | ".join(cb_parts))
    sym = stats.get("by_symbol", {})
    sym_parts = []
    for s, d in sorted(sym.items(), key=lambda kv: kv[1]["pnl"], reverse=True):
        if d["signals"] or d["trades"]:
            dec = d["wins"] + d["losses"]
            wr = f", Winrate {round(d['wins'] / dec * 100)}%" if dec else ""
            pnl = f", PnL {d['pnl']:+.2f}" if d["trades"] else ""
            sym_parts.append(f"{s}: {d['signals']} Sig{wr}{pnl}")
    if sym_parts:
        lines.append("Pro Coin: " + " | ".join(sym_parts[:13]))
    if stats.get("best_symbol"):
        lines.append(f"Bester Coin (PnL): {stats['best_symbol']} | Schwächster: {stats.get('worst_symbol')}")
    return "\n".join(lines)


def merge_lessons(old: List[Dict], new: List[Dict], removed: List[str],
                  max_lessons: int) -> List[Dict]:
    """Zusammenführen der Lektionen – Implementierung in `services/ai_lessons.py`.

    Bleibt als Re-Export erhalten (Rückwärtskompatibilität für bestehende Tests
    und Aufrufer). Vom Trader bearbeitete Lektionen (`locked`) sind dort
    geschützt: die KI kann sie weder überschreiben noch verwerfen.
    """
    return ai_lessons.merge_lessons(old, new, removed, max_lessons)


class AILearning:
    def __init__(self, engine):
        self.engine = engine
        self.last_learn: Optional[str] = None
        self.learning_now = False
        self._lessons_cache: Optional[List[Dict]] = None
        self._last_tick = 0.0
        self._last_learn_ts = 0.0
        self.min_learn_gap_sec = 900  # max. 1 Trade-Close-Lernlauf pro 15 min

    @property
    def db(self):
        return self.engine.db

    async def load_state(self):
        try:
            doc = await self.db.settings.find_one({"_id": "ai_lessons"})
            if doc:
                raw = doc.get("lessons", []) or []
                lessons = ai_lessons.normalize_all(raw)
                # Migration: Alt-Bestand hatte keine ids/origin – ohne id können
                # Lektionen im UI nicht bearbeitet oder gelöscht werden.
                if any(not (l or {}).get("id") for l in raw if isinstance(l, dict)):
                    await lesson_store.save_all(lessons)
                    logger.info(f"AI lessons migriert: {len(lessons)} Lektionen mit id versehen")
                self._lessons_cache = lessons
                self.last_learn = doc.get("updated_at")
        except Exception as e:
            logger.warning(f"AI lessons load failed: {e}")

    # ---------------- outcome sync ----------------
    async def sync_outcomes(self) -> List[Dict]:
        """Schreibt Signal-Ergebnisse & Trade-PnL zurück in ai_decisions.
        Gibt die NEU geschlossenen KI-Trades zurück (Lern-Trigger)."""
        try:
            sigs = await self.db.signals.find({
                "strategy_id": "ai_trader",
                "result": {"$in": ["win", "loss", "breakeven"]},
                "ai_learn_synced": {"$ne": True},
            }).limit(200).to_list(200)
            for s in sigs:
                if s.get("id"):
                    # Fix 0.5: outcome_source mitschreiben; ein bereits
                    # trade-gelabeltes Outcome (kanonisch) nie durch
                    # TP1-Touch zurückstufen.
                    src = s.get("result_source") or "tp1_touch"
                    flt = {"signal_id": s["id"]}
                    if src != "trade_pnl":
                        flt["outcome_source"] = {"$ne": "trade_pnl"}
                    await self.db.ai_decisions.update_many(
                        flt,
                        {"$set": {"outcome": s.get("result"),
                                  "outcome_source": src,
                                  "outcome_ts": _now_iso()}})
                    await self.db.signals.update_one(
                        {"id": s["id"]}, {"$set": {"ai_learn_synced": True}})
        except Exception as e:
            logger.warning(f"AI outcome sync (signals) failed: {e}")

        new_trades: List[Dict] = []
        try:
            trades = await self.db.auto_trades.find({
                "strategy_id": "ai_trader", "status": "closed",
                "ai_learn_synced": {"$ne": True},
            }).limit(100).to_list(100)
            for t in trades:
                await self.db.auto_trades.update_one(
                    {"id": t["id"]}, {"$set": {"ai_learn_synced": True}})
                if t.get("signal_id"):
                    # Fix 0.5: Trade-Ergebnis ist die kanonische Wahrheit ->
                    # setzt outcome IMMER (überschreibt TP1-Touch-Label).
                    upd = {"trade_pnl": t.get("realized_pnl"),
                           "trade_mode": t.get("mode"),
                           "trade_closed_at": t.get("closed_at")}
                    if t.get("result") in ("win", "loss", "breakeven"):
                        upd.update({"outcome": t["result"],
                                    "outcome_source": "trade_pnl",
                                    "outcome_ts": _now_iso()})
                    await self.db.ai_decisions.update_many(
                        {"signal_id": t["signal_id"]}, {"$set": upd})
                t.pop("_id", None)
                new_trades.append(t)
        except Exception as e:
            logger.warning(f"AI outcome sync (trades) failed: {e}")
        return new_trades

    # ---------------- stats ----------------
    async def gather_stats(self) -> Dict:
        days = int(self.engine.config.get("learning_lookback_days", 14))
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        signals = await self.db.signals.find(
            {"strategy_id": "ai_trader", "timestamp": {"$gte": cutoff}}).to_list(2000)
        trades = await self.db.auto_trades.find(
            {"strategy_id": "ai_trader", "opened_at": {"$gte": cutoff}}).to_list(2000)
        stats = aggregate_performance(signals, trades)
        stats["lookback_days"] = days
        return stats

    async def performance_text(self) -> str:
        try:
            return performance_to_text(await self.gather_stats())
        except Exception as e:
            return f"(Performance-Daten nicht verfügbar: {str(e)[:80]})"

    # ---------------- lessons ----------------
    def invalidate_lessons(self):
        """Cache verwerfen (nach Trader-Änderungen über die Endpunkte)."""
        self._lessons_cache = None

    async def get_lessons(self) -> List[Dict]:
        if not self._lessons_cache:
            try:
                self._lessons_cache = await lesson_store.all()
            except Exception as e:
                logger.warning(f"Lektionen laden fehlgeschlagen: {e}")
                self._lessons_cache = []
        return self._lessons_cache or []

    async def lessons_text(self) -> str:
        return ai_lessons.lessons_text(await self.get_lessons())

    # ---------------- Lektions-Kandidaten (Validierung durch Wiedererkennung) ----------------
    async def lesson_candidates(self, limit: int = 20) -> List[Dict]:
        """Noch nicht validierte Lektions-Vorschläge der KI (warten auf
        Wiedererkennung in weiteren Lernläufen + ausreichende Datenbasis)."""
        try:
            rows = await self.db.ai_lesson_candidates.find() \
                .sort("updated_at", -1).limit(max(1, min(50, limit))).to_list(50)
        except Exception:
            return []
        for r in rows:
            r.pop("_id", None)
        return rows

    async def _bump_lesson_candidate(self, key: str, title: str, detail: str,
                                     model: str, weight: int, stats: Dict) -> Dict:
        doc = await self.db.ai_lesson_candidates.find_one({"key": key}) or {}
        totals = (stats or {}).get("totals") or {}
        entry = {
            "key": key, "title": title, "detail": detail, "model": model,
            "weight": max(int(weight or 2), int(doc.get("weight", 0) or 0)),
            "confirmations": int(doc.get("confirmations", 0)) + 1,
            "sample": int(totals.get("closed_trades") or 0)
            + int(totals.get("signal_wins") or 0) + int(totals.get("signal_losses") or 0),
            "first_seen": doc.get("first_seen") or _now_iso(),
            "updated_at": _now_iso(),
        }
        await self.db.ai_lesson_candidates.update_one(
            {"key": key}, {"$set": entry}, upsert=True)
        return entry

    async def _candidates_prompt_block(self) -> str:
        cands = await self.lesson_candidates()
        if not cands:
            return ""
        lines = [f"- „{c['title']}“ ({c['confirmations']} Bestätigung(en)): {c['detail']}"
                 for c in cands[:12]]
        return ("=== NOCH NICHT VALIDIERTE LEKTIONS-KANDIDATEN ===\n"
                "Diese Erkenntnisse hast du früher vorgeschlagen. Bestätigen die aktuellen "
                "Daten eine davon, gib sie mit EXAKT demselben Titel erneut zurück – erst "
                "dann wird sie als Lektion aktiv. Widerlegen die Daten sie, ignoriere sie.\n"
                + "\n".join(lines))

    def summary(self) -> Dict:
        return {
            "enabled": bool(self.engine.config.get("learning_enabled", True)),
            "last_learn": self.last_learn,
            "lessons_count": len(self._lessons_cache or []),
            "learning_now": self.learning_now,
        }

    # ---------------- loop hook ----------------
    async def tick(self):
        """Wird vom Engine-Loop aufgerufen (alle ~5s, intern auf 30s gedrosselt)."""
        now = time.time()
        if now - self._last_tick < 30:
            return
        self._last_tick = now
        new_trades = await self.sync_outcomes()
        cfg = self.engine.config
        if (new_trades and cfg.get("learning_enabled", True)
                and cfg.get("learn_on_trade_close", True) and self.engine.key
                and (now - self._last_learn_ts) > self.min_learn_gap_sec):
            await self.run_learning(trigger="trade_close")

    # ---------------- learning run ----------------
    async def _recent_outcomes_text(self, limit: int = 25) -> str:
        trades = await self.db.auto_trades.find(
            {"strategy_id": "ai_trader", "status": "closed"}
        ).sort("closed_at", -1).limit(limit).to_list(limit)
        if not trades:
            return "(noch keine geschlossenen KI-Trades)"
        sig_ids = [t.get("signal_id") for t in trades if t.get("signal_id")]
        dec_by_sig: Dict[str, Dict] = {}
        if sig_ids:
            decs = await self.db.ai_decisions.find(
                {"signal_id": {"$in": sig_ids}}).to_list(len(sig_ids))
            dec_by_sig = {d.get("signal_id"): d for d in decs}
        lines = []
        for t in reversed(trades):
            d = dec_by_sig.get(t.get("signal_id"), {})
            pnl = float(t.get("realized_pnl", 0) or 0)
            dur = ""
            try:
                o = datetime.fromisoformat(str(t.get("opened_at")).replace("Z", "+00:00"))
                c = datetime.fromisoformat(str(t.get("closed_at")).replace("Z", "+00:00"))
                dur = f", Dauer {int((c - o).total_seconds() / 60)}min"
            except Exception:
                pass
            conf = d.get("confidence")
            reason = str(d.get("reasoning", ""))[:110]
            lines.append(
                f"- {t.get('symbol')} {t.get('side')} [{t.get('mode')}] Hebel {t.get('leverage')}x, "
                f"PnL {pnl:+.2f} USDT{dur}"
                + (f", Konfidenz {conf}%" if conf is not None else "")
                + (f" | Begründung damals: {reason}" if reason else ""))
        return "\n".join(lines)

    async def run_learning(self, trigger: str = "manual") -> Dict:
        if self.learning_now:
            return {"status": "busy", "detail": "Lernlauf läuft bereits"}
        if not self.engine.key:
            return {"status": "error", "detail": "Kein API-Key für den aktiven Provider"}
        # Kosten: geplante Lernläufe (daily/daily_summary) überspringen, wenn seit
        # dem letzten Lernlauf KEIN KI-Trade geschlossen wurde – ohne neue
        # Ergebnisse gibt es nichts Neues zu lernen (Lektionen bleiben erhalten).
        if trigger in ("daily", "daily_summary") and self._last_learn_ts > 0:
            try:
                since = datetime.fromtimestamp(self._last_learn_ts, tz=timezone.utc).isoformat()
                fresh = await self.db.auto_trades.count_documents(
                    {"strategy_id": "ai_trader", "status": "closed",
                     "closed_at": {"$gt": since}})
                if fresh == 0:
                    logger.info("Lernlauf (%s) übersprungen: keine neuen "
                                "Trade-Ergebnisse seit letztem Lernlauf", trigger)
                    return {"status": "skipped",
                            "detail": "Keine neuen Trade-Ergebnisse seit letztem "
                                      "Lernlauf – LLM-Call gespart"}
            except Exception as e:
                logger.warning(f"Lernlauf-Frische-Check fehlgeschlagen: {e}")
        self.learning_now = True
        try:
            stats = await self.gather_stats()
            stats_txt = performance_to_text(stats)
            lean = bool(self.engine.config.get("lean_prompt", True))
            outcomes_txt = await self._recent_outcomes_text(15 if lean else 25)
            reward_txt = ""
            try:
                from services import ai_rewards
                reward_txt = await ai_rewards.context_text(self.db)
            except Exception as re_:
                logger.warning(f"Reward-Kontext fehlgeschlagen: {re_}")
            # MasterPrompt-Audit VOR dem Lernlauf: Altbestand bereinigen, damit
            # keine verstoßende Lektion in den Prompt gelangt oder erhalten bleibt.
            try:
                audit = await lesson_store.audit_against_master()
                if audit.get("removed"):
                    self._lessons_cache = None
            except Exception as e:
                logger.warning(f"Lektionen-Audit vor Lernlauf fehlgeschlagen: {e}")
            old = await self.get_lessons()
            old_txt = ai_lessons.lessons_text(old)
            directives = await self.engine._user_directives(10)
            # Lean-Prompt: Forschungs-/ML-Blöcke fließen bereits direkt in die
            # Analyse-Prompts – für den Lernlauf (Lektionen aus EIGENEN
            # Ergebnissen) sind sie verzichtbar und sparen Tokens.
            research_txt = ""
            if not lean:
                try:
                    from services.ai_research import research_analyst
                    research_txt = await research_analyst.context_text()
                except Exception:
                    pass
            ml_txt = ""
            if not lean:
                try:
                    from services.ai_ml_lab import ml_lab
                    ml_txt = await ml_lab.context_text()
                except Exception:
                    pass
            max_lessons = int(self.engine.config.get("max_lessons", 10))
            autonomy = self.engine.config.get("autonomy", "suggest")
            candidates_block = await self._candidates_prompt_block()
            autonomy_block = ""
            if autonomy in ("suggest", "auto"):
                from services.ai_knowledge import tunable_spec_text
                autonomy_block = (
                    "\n\nDu DARFST zusätzlich datenbasierte Änderungen an deinen Trade-Einstellungen "
                    "zurückgeben (Feld config_changes, max. 4; symbol \"ENGINE\" für "
                    "min_confidence/cooldown_min). NIE max_capital oder mode.\n" + tunable_spec_text())
            forced_block = ""
            if trigger == "kill_switch":
                forced_block = (
                    "=== ZWANGS-LERNPHASE NACH KILL-SWITCH (HÖCHSTE PRIORITÄT) ===\n"
                    "Der Kill-Switch wurde soeben durch eine Verlust-Serie ausgelöst. "
                    "Auto-Trading bleibt gesperrt, bis dieser Lernlauf abgeschlossen ist. "
                    "Analysiere die jüngste Verlust-Serie besonders kritisch: Welches "
                    "gemeinsame Muster haben die Verlust-Trades (Symbol, Richtung, Regime, "
                    "Uhrzeit, Konfidenz, Haltedauer)? Formuliere mindestens eine konkrete "
                    "Lektion, die genau diese Verlust-Serie künftig verhindert.\n\n")
            prompt = (
                forced_block
                + f"{master_prompt.prompt_block()}\n\n"
                f"{master_prompt.lesson_policy_block()}\n\n"
                f"{validation_gate.prompt_block()}\n\n"
                f"=== PERFORMANCE-STATISTIK (letzte {stats.get('lookback_days')} Tage) ===\n{stats_txt}\n\n"
                + (f"{reward_txt}\n\n" if reward_txt else "")
                + f"=== LETZTE GESCHLOSSENE TRADES (chronologisch) ===\n{outcomes_txt}\n\n"
                f"=== BISHERIGE LEKTIONEN ===\n{old_txt}\n\n"
                + (f"{candidates_block}\n\n" if candidates_block else "")
                + (f"{research_txt}\n\n" if research_txt else "")
                + (f"{ml_txt}\n\n" if ml_txt else "")
                + f"=== AKTUELLE TRADER-DIREKTIVEN ===\n{directives}\n\n"
                f"Gespeichert sind aktuell {len(old)} von maximal {max_lessons} Lektionen. "
                "Gib NEUE oder geschärfte Lektionen zurück (bestehende bleiben automatisch "
                "erhalten) und liste in removed_lessons nur, was die Daten klar widerlegen."
                f"{autonomy_block}"
            )
            raw, model_used = await self.engine._generate_json(prompt, LEARNING_SYSTEM, role="learner")
            data = self.engine._parse_json(raw)
            from services import ai_providers
            run_weight = ai_providers.model_weight(model_used)
            old_by_title = {str(l.get("title", "")).strip().lower(): l for l in old}
            locked_titles = {str(l.get("title", "")).strip().lower()
                             for l in old if l.get("locked")}
            # Freigaben: neue Lektionen und das Verwerfen brauchen Datenbasis
            gate_new = validation_gate.lesson(stats)
            gate_removal = validation_gate.lesson(stats, removal=True)
            min_conf = int(validation_gate.settings.get("min_lesson_confirmations", 2))
            fresh, skipped, skipped_items = [], [], []

            def _skip(s_title, s_reason, s_detail="", approvable=True):
                skipped.append(f"{s_title}: {s_reason}")
                skipped_items.append({
                    "id": uuid.uuid4().hex[:8], "title": str(s_title)[:120],
                    "detail": str(s_detail or "")[:400],
                    "reason": str(s_reason)[:300],
                    "approvable": bool(approvable), "ts": _now_iso()})
            for l in (data.get("lessons") or [])[:max_lessons]:
                if not (isinstance(l, dict) and l.get("title")):
                    continue
                title = str(l["title"])[:120]
                detail = str(l.get("detail", ""))[:400]
                key = title.strip().lower()
                if key in locked_titles:
                    _skip(title, "vom Trader festgelegt (unveränderlich)", detail,
                          approvable=False)
                    continue
                ok_master, why = master_prompt.check_lesson(title, detail)
                if not ok_master:
                    _skip(title, why, detail)
                    continue
                prev = old_by_title.get(key)
                if l.get("trader_directive") and prev is None:
                    # ANWEISUNG DES TRADERS: sofort aktiv, KEIN Kandidaten-Gate
                    # (Hoheitsrecht des Traders). Die KI darf die Lektion
                    # trotzdem kritisch kommentieren (Feld "critique").
                    critique = str(l.get("critique") or "")[:300]
                    fresh.append({"title": title, "detail": detail,
                                  "model": model_used, "weight": max(run_weight, 3),
                                  "weight_label": ai_providers.WEIGHT_LABELS.get(
                                      max(run_weight, 3), "hoch"),
                                  "origin": "user", "locked": True,
                                  "updated_at": _now_iso(), "confirmations": 1})
                    _skip(title, "AUF ANWEISUNG DES TRADERS sofort aktiviert"
                          + (f" – Einschätzung der KI: {critique}" if critique else ""),
                          detail, approvable=False)
                    continue
                if prev is None:
                    # NEUE Lektion: wird erst aktiv, wenn dieselbe Erkenntnis
                    # (exakter Titel) mehrfach wiedererkannt wurde UND die
                    # Datenbasis reicht – bis dahin bleibt sie Kandidat.
                    cand = await self._bump_lesson_candidate(
                        key, title, detail, model_used, run_weight, stats)
                    if not gate_new.get("validated") or cand["confirmations"] < min_conf:
                        need = []
                        if cand["confirmations"] < min_conf:
                            need.append(f"Wiedererkennung {cand['confirmations']}/{min_conf}")
                        if not gate_new.get("validated"):
                            need.append(gate_new.get("reason", ""))
                        _skip(title, "Kandidat – wartet auf Validierung "
                              f"({'; '.join(n for n in need if n)})", detail)
                        continue
                    await self.db.ai_lesson_candidates.delete_one({"key": key})
                    weight = max(run_weight, int(cand.get("weight", 0) or 0))
                    confirmations = int(cand.get("confirmations", 1))
                else:
                    # Schärfung einer bestehenden Lektion: Gewichtung stärkerer
                    # Modelle bleibt erhalten, Bestätigungen zählen weiter.
                    weight = max(run_weight, int(prev.get("weight", 0)))
                    confirmations = int(prev.get("confirmations", 0)) + 1
                fresh.append({"title": title,
                              "detail": detail,
                              "model": model_used,
                              "weight": weight,
                              "weight_label": ai_providers.WEIGHT_LABELS.get(weight, "mittel"),
                              "origin": "ai", "locked": False,
                              "updated_at": _now_iso(),
                              "confirmations": confirmations})
            removed = [str(t) for t in (data.get("removed_lessons") or [])
                       if isinstance(t, str)]
            if removed and not gate_removal.get("validated"):
                _skip(f"Verwerfen von {len(removed)} Lektion(en)",
                      str(gate_removal.get("reason")), approvable=False)
                removed = []
            # Hygiene-Bereinigung (Doppelungen/Widersprüche): darf auch OHNE
            # Removal-Gate erfolgen – aber nie vom Trader gesperrte Lektionen.
            contra = [str(t) for t in (data.get("contradictory_lessons") or [])
                      if isinstance(t, str)
                      and str(t).strip().lower() not in locked_titles]
            if contra:
                removed = sorted({*removed, *contra})
                logger.info("AI learning: widersprüchliche/doppelte Lektionen "
                            f"entfernt: {contra[:6]}")
            if skipped:
                logger.info("AI learning: verworfene Lektions-Wünsche -> "
                            + " | ".join(skipped[:6]))
            lessons = merge_lessons(old, fresh, removed, max_lessons)
            assessment = str(data.get("assessment", ""))[:1200]
            now = _now_iso()
            await lesson_store.save_all(lessons)
            # Kandidaten aufräumen: aktiv gewordene Titel und >30 Tage alte Reste
            try:
                titles = [str(l.get("title", "")).strip().lower() for l in lessons]
                stale = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
                await self.db.ai_lesson_candidates.delete_many(
                    {"$or": [{"key": {"$in": titles}}, {"updated_at": {"$lt": stale}}]})
            except Exception:
                pass
            await self.db.settings.update_one(
                {"_id": "ai_lessons"},
                {"$set": {"assessment": assessment, "updated_at": now,
                          "trigger": trigger, "model": model_used,
                          "skipped": skipped[:10],
                          "skipped_items": skipped_items[:10],
                          "stats": stats.get("totals", {})}},
                upsert=True)
            self._lessons_cache = lessons
            self.last_learn = now
            self._last_learn_ts = time.time()

            cfg_results = []
            try:
                cfg_results = await self.engine._handle_config_changes(
                    data.get("config_changes") or [], source="learning")
            except Exception as ce:
                logger.error(f"Learning config changes failed: {ce}")
            # Nach neuen Ergebnissen kann die Datenlage geparkte Wünsche
            # bestätigen (Autonomie "auto").
            try:
                await self.engine.review_parked_proposals()
            except Exception as re_:
                logger.error(f"Autonomie-Review (Lernlauf) fehlgeschlagen: {re_}")

            await self.db.ai_chat.insert_one({
                "id": str(uuid.uuid4()), "role": "learning",
                "text": assessment, "lessons": lessons, "skipped": skipped[:10],
                "trigger": trigger, "model": model_used, "ts": now,
            })
            logger.info(f"AI learning done ({trigger}, {model_used}): "
                        f"{len(lessons)} Lektionen gespeichert "
                        f"({len(fresh)} neu/bestätigt, {len(removed)} verworfen, "
                        f"{len(skipped)} abgelehnt), "
                        f"{len(cfg_results)} Config-Änderungen")
            return {"status": "ok", "lessons": len(lessons), "new_lessons": len(fresh),
                    "removed_lessons": len(removed), "skipped": skipped[:10],
                    "assessment": assessment,
                    "config_changes": len(cfg_results), "model": model_used}
        except Exception as e:
            logger.error(f"AI learning failed: {e}")
            return {"status": "error", "detail": str(e)[:300]}
        finally:
            self.learning_now = False
