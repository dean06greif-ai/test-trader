"""Forschungs-Analyst ("research_analyst"-Rolle des KI-Teams).

Wertet alles aus, was auf der Website gerechnet wird – Backtests, Optimizer-Läufe
(inkl. Walk-Forward/Robustheit), Regime-Lab-Läufe und Regime-Analysen – und
destilliert daraus Erkenntnisse: welche Strategien, Parameter und Marktregime
liefern verlässlich Ergebnisse und warum. Die Erkenntnisse landen im
KI-Gedächtnis (services/ai_memory.py) und fließen als Kontext-Block in die
Analysen des KI Traders, in die Tiefenanalyse und in die Lernläufe.

Der Analyst läuft
  - zu konfigurierbaren Uhrzeiten (Rollen-Feld `schedule_times`),
  - spätestens alle `interval_hours` Stunden,
  - und automatisch, sobald neue Backtest-/Optimizer-/Regime-Ergebnisse vorliegen
    (`auto_on_new_results`).

Reine Aufbereitungs-Funktionen (digest_*) sind bewusst frei von DB/LLM, damit
sie in den Regressionstests direkt geprüft werden können.
"""
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

from services.ai_memory import memory

logger = logging.getLogger(__name__)

RESEARCH_SYSTEM = (
    "Du bist der 'Forschungs-Analyst' im KI-Team einer Krypto-Daytrading-Plattform. "
    "Du analysierst die Rechenergebnisse der Plattform (Backtests, Optimizer-Läufe mit "
    "Walk-Forward/Robustheits-Kennzahlen, Regime-Lab, Regime-Analysen) und leitest daraus "
    "belastbares Handelswissen ab: Welche Strategien/Parameter funktionieren in welchen "
    "Marktbedingungen, wo ist Overfitting im Spiel, welche Kombinationen sind robust? "
    "Sei streng datenbasiert: unterscheide klar zwischen 'im Training gut' und "
    "'im Walk-Forward/Holdout bestätigt'. Wenige Trades => niedrige Konfidenz. "
    "Deine Erkenntnisse werden dem KI Trader als Wissen übergeben – formuliere sie so, "
    "dass er sie direkt in Entscheidungen umsetzen kann.\n"
    "Antworte AUSSCHLIESSLICH mit validem JSON ohne Markdown, exakt in diesem Schema:\n"
    '{"summary": "4-8 Sätze Gesamtbild auf Deutsch", '
    '"insights": [{"title": "Kurztitel", "detail": "umsetzbare Erkenntnis auf Deutsch", '
    '"confidence": 0-100, "tags": ["strategie|regime|parameter|risiko"]}], '
    '"strategy_ranking": [{"strategy": "Name/ID", "verdict": "stark|brauchbar|schwach", '
    '"reason": "kurz"}], '
    '"regime_notes": ["Erkenntnis zu einem Marktregime"], '
    '"ideas": [{"title": "neue Idee", "detail": "konkreter Test-/Umsetzungsvorschlag"}], '
    '"recommendations": ["konkrete Empfehlung für den KI Trader"]}'
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ---------------- reine Aufbereitung (testbar) ----------------
def digest_backtests(rows: List[Dict], max_rows: int = 6) -> str:
    """Backtest-Läufe -> kompakter Text (pro Lauf die besten Strategien)."""
    if not rows:
        return "(keine Backtests vorhanden)"
    lines: List[str] = []
    for r in rows[:max_rows]:
        res = r.get("result") or {}
        params = r.get("params") or {}
        per_strategy = sorted((res.get("per_strategy") or []),
                              key=lambda s: -_f(s.get("pnl")))[:4]
        head = (f"- Lauf {str(r.get('created_at', ''))[:16]} · {res.get('days', params.get('days'))} Tage · "
                f"{len(params.get('symbols') or [])} Coins · TF {params.get('timeframe') or 'auto'}")
        lines.append(head)
        for s in per_strategy:
            lines.append(
                f"    · {s.get('strategy_name') or s.get('strategy_id')}: "
                f"PnL {_f(s.get('pnl')):+.2f} ({_f(s.get('pnl_pct')):+.1f}%), "
                f"{int(_f(s.get('trades')))} Trades, Winrate {_f(s.get('win_rate')):.1f}%, "
                f"MaxDD {_f(s.get('max_drawdown_pct')):.1f}%, TF {s.get('timeframe', '?')}")
        best = res.get("best_per_symbol") or {}
        if best:
            top = sorted(best.items(), key=lambda kv: -_f(kv[1].get("pnl")))[:5]
            lines.append("    · beste Strategie pro Coin: " + ", ".join(
                f"{sym}={v.get('strategy_name') or v.get('strategy_id')}({_f(v.get('pnl')):+.1f})"
                for sym, v in top))
    return "\n".join(lines)


def digest_optimizer(rows: List[Dict], max_rows: int = 6) -> str:
    """Optimizer-Läufe -> Text inkl. Walk-Forward/Robustheits-Kennzahlen."""
    if not rows:
        return "(keine Optimizer-Läufe vorhanden)"
    lines: List[str] = []
    for r in rows[:max_rows]:
        res = r.get("result") or {}
        top5 = res.get("top5") or []
        best = top5[0] if top5 else {}
        m = best.get("metrics") or res.get("metrics") or {}
        tm = best.get("test_metrics") or {}
        wf = best.get("wf") or {}
        cons = best.get("constancy") or {}
        lines.append(
            f"- {str(r.get('created_at', ''))[:16]} · Modus {res.get('mode')} · "
            f"Ziel {res.get('objective')} · {res.get('days')} Tage · TF {res.get('timeframe')} · "
            f"Strategie {res.get('strategy_name') or (res.get('definition') or {}).get('name') or '?'} · "
            f"Coins {', '.join((res.get('symbols') or [])[:6])}")
        lines.append(
            f"    · Train: PnL {_f(m.get('pnl')):+.2f}, Winrate {_f(m.get('win_rate')):.1f}%, "
            f"{int(_f(m.get('trades')))} Trades, MaxDD {_f(m.get('max_drawdown')):.2f}"
            + (f" | Test/Holdout: PnL {_f(tm.get('pnl')):+.2f}, Winrate {_f(tm.get('win_rate')):.1f}%"
               if tm else " | kein Holdout")
            + (f" | WF-Score {wf.get('score')}" if wf else "")
            + (f" | Konstanz-Abw. {cons.get('deviation_pct')}%" if cons else "")
            + f" | bestanden: {'ja' if best.get('passed') else 'nein'}")
        if best.get("params"):
            ps = ", ".join(f"{k}={v}" for k, v in list((best.get("params") or {}).items())[:10])
            lines.append(f"    · beste Parameter: {ps}")
        if best.get("trade_params"):
            ts = ", ".join(f"{k}={v}" for k, v in list((best.get("trade_params") or {}).items())[:8])
            lines.append(f"    · Trade-Parameter: {ts}")
        if best.get("fail_reasons"):
            lines.append("    · Fail-Gründe: " + "; ".join(str(x)[:80] for x in best["fail_reasons"][:4]))
        if best.get("rank_reason"):
            lines.append(f"    · Ranking-Begründung: {str(best['rank_reason'])[:180]}")
    return "\n".join(lines)


def digest_regime_runs(rows: List[Dict], max_rows: int = 5) -> str:
    """Regime-Lab-Läufe (regime_opt / walkforward) -> Text."""
    if not rows:
        return "(keine Regime-Lab-Läufe vorhanden)"
    lines: List[str] = []
    for r in rows[:max_rows]:
        res = r.get("result") or {}
        kind = res.get("kind")
        if kind == "walkforward":
            dyn = res.get("dynamic") or res.get("dyn_metrics") or {}
            bs = (res.get("best_single") or {}).get("metrics") or {}
            lines.append(
                f"- Walk-Forward '{res.get('analysis_name')}' ({res.get('scope')}"
                f"{'/' + str(res.get('symbol')) if res.get('symbol') else ''}): "
                f"Kombination PnL {_f(dyn.get('pnl')):+.2f} vs. beste Einzel-Strategie "
                f"PnL {_f(bs.get('pnl')):+.2f} · Urteil: {str(res.get('verdict'))[:120]}")
            continue
        top5 = res.get("top5") or []
        best = top5[0] if top5 else {}
        m = best.get("metrics") or {}
        seg = res.get("segments_info") or {}
        lines.append(
            f"- Regime {res.get('regime_id')} '{res.get('regime_label')}' "
            f"({res.get('scope')}{'/' + str(res.get('symbol')) if res.get('symbol') else ''}) · "
            f"Modus {res.get('mode')} · TF {res.get('timeframe')} · "
            f"{seg.get('segments', '?')} Segmente / {seg.get('days', '?')} Tage")
        lines.append(
            f"    · Beste Variante: PnL {_f(m.get('pnl')):+.2f}, Winrate {_f(m.get('win_rate')):.1f}%, "
            f"{int(_f(m.get('trades')))} Trades, Validierung "
            f"{'bestanden' if best.get('validation_passed') else 'offen/nicht bestanden'}"
            + (f", Strategie {res.get('strategy_name')}" if res.get("strategy_name") else ""))
        if best.get("params"):
            lines.append("    · Parameter: " + ", ".join(
                f"{k}={v}" for k, v in list(best["params"].items())[:8]))
    return "\n".join(lines)


def digest_regime_analyses(rows: List[Dict], max_rows: int = 4) -> str:
    """Regime-Analysen (Cluster-Modelle) -> Text mit Regime-Charakteristik."""
    if not rows:
        return "(keine Regime-Analysen vorhanden)"
    lines: List[str] = []
    for doc in rows[:max_rows]:
        combined = (doc.get("combined") or {})
        model = combined.get("model") or {}
        regimes = model.get("regimes") or combined.get("regimes") or []
        lines.append(f"- Analyse '{doc.get('name') or doc.get('id')}' · TF {doc.get('timeframe')} · "
                     f"{len(regimes)} Regime · {str(doc.get('created_at', ''))[:16]}")
        for reg in regimes[:6]:
            if not isinstance(reg, dict):
                continue
            lines.append(f"    · Regime {reg.get('id', reg.get('regime_id'))}: "
                         f"{reg.get('label', '?')} "
                         + (f"(Anteil {reg.get('share_pct')}%)" if reg.get("share_pct") is not None else ""))
    return "\n".join(lines)


def build_prompt(digests: Dict[str, str], counts: Dict[str, int],
                 previous: Optional[Dict] = None, extra_blocks: str = "") -> str:
    prev_txt = "(noch keine früheren Erkenntnisse)"
    if previous and previous.get("insights"):
        prev_txt = "\n".join(
            f"- {i.get('title')}: {str(i.get('detail'))[:200]}"
            for i in (previous.get("insights") or [])[:12])
    return (
        f"Zeitpunkt (UTC): {_now_iso()}\n"
        f"Datenbestand: {counts.get('backtests', 0)} Backtests, "
        f"{counts.get('optimizer_runs', 0)} Optimizer-Läufe, "
        f"{counts.get('regime_lab_runs', 0)} Regime-Lab-Läufe, "
        f"{counts.get('regime_analyses', 0)} Regime-Analysen.\n\n"
        f"=== BACKTESTS (neueste zuerst) ===\n{digests.get('backtests', '')}\n\n"
        f"=== OPTIMIZER-LÄUFE (mit Walk-Forward/Robustheit) ===\n{digests.get('optimizer', '')}\n\n"
        f"=== REGIME-LAB ===\n{digests.get('regime_runs', '')}\n\n"
        f"=== REGIME-ANALYSEN ===\n{digests.get('regime_analyses', '')}\n\n"
        f"=== LIVE-PERFORMANCE DER STRATEGIEN (echtes Geld/Paper) ===\n"
        f"{digests.get('live_performance', '(keine Daten)')}\n\n"
        + (f"{extra_blocks}\n\n" if extra_blocks else "")
        + f"=== DEINE FRÜHEREN ERKENNTNISSE ===\n{prev_txt}\n\n"
        "Erstelle jetzt die Forschungs-Auswertung als JSON. Vergleiche Backtest-/Optimizer-"
        "Ergebnisse mit der echten Live-Performance und benenne Abweichungen (Overfitting, "
        "Slippage, Regime-Wechsel) klar."
    )


class ResearchAnalyst:
    """Orchestriert Datensammlung, LLM-Auswertung und Wissensspeicherung."""

    ROLE = "research_analyst"
    MIN_GAP_SEC = 900

    def __init__(self):
        self.engine = None
        self.last_run: Optional[str] = None
        self.last_error: Optional[str] = None
        self.running_now = False
        self.report: Optional[Dict] = None
        self._last_run_ts = 0.0
        self._last_counts: Dict[str, int] = {}
        self._ran_slots: Dict[str, str] = {}
        self._last_tick = 0.0

    def setup(self, engine):
        self.engine = engine

    @property
    def db(self):
        return self.engine.db if self.engine else None

    def _cfg(self) -> Dict:
        from services.ai_roles import role_manager
        return role_manager.role_cfg(self.ROLE)

    async def load_state(self):
        try:
            doc = await self.db.settings.find_one({"_id": "ai_research_report"})
            if doc:
                doc.pop("_id", None)
                self.report = doc
                self.last_run = doc.get("ts")
            st = await self.db.settings.find_one({"_id": "ai_research_state"})
            if st:
                self._last_counts = st.get("counts") or {}
        except Exception as e:
            logger.warning(f"Research-Analyst State laden fehlgeschlagen: {e}")

    async def reset(self) -> Dict:
        """Forschungs-Daten zurücksetzen: Report + Zähler-Zustand löschen."""
        try:
            await self.db.settings.delete_one({"_id": "ai_research_report"})
            await self.db.settings.delete_one({"_id": "ai_research_state"})
        except Exception as e:
            logger.warning(f"Forschungs-Reset fehlgeschlagen: {e}")
        self.report = None
        self.last_run = None
        self.last_error = None
        self._last_counts = {}
        logger.info("Forschungs-Analyst zurückgesetzt (Report & Zustand gelöscht)")
        return {"status": "ok"}

    # ---------------- data ----------------
    async def _counts(self) -> Dict[str, int]:
        out = {}
        for coll in ("backtests", "optimizer_runs", "regime_lab_runs", "regime_analyses"):
            try:
                out[coll] = await self.db[coll].count_documents({})
            except Exception:
                out[coll] = 0
        return out

    async def collect(self) -> Dict:
        """Alle Rechenergebnisse der Website einsammeln und aufbereiten."""
        async def _latest(coll: str, limit: int) -> List[Dict]:
            try:
                rows = await self.db[coll].find().sort("created_at", -1).limit(limit).to_list(limit)
                for r in rows:
                    r.pop("_id", None)
                return rows
            except Exception as e:
                logger.warning(f"Research: {coll} laden fehlgeschlagen: {e}")
                return []

        backtests = await _latest("backtests", 6)
        optimizer = await _latest("optimizer_runs", 6)
        regime_runs = await _latest("regime_lab_runs", 5)
        regime_analyses = await _latest("regime_analyses", 4)
        live_perf = "(keine Daten)"
        try:
            live_perf = await self.engine._strategy_performance_text()
        except Exception:
            pass
        return {
            "counts": await self._counts(),
            "digests": {
                "backtests": digest_backtests(backtests),
                "optimizer": digest_optimizer(optimizer),
                "regime_runs": digest_regime_runs(regime_runs),
                "regime_analyses": digest_regime_analyses(regime_analyses),
                "live_performance": live_perf,
            },
        }

    # ---------------- run ----------------
    async def run(self, manual: bool = False, trigger: str = "manual") -> Dict:
        if self.running_now:
            return {"status": "busy", "detail": "Forschungs-Auswertung läuft bereits"}
        cfg = self._cfg()
        if not manual and not cfg.get("enabled", True):
            return {"status": "skipped", "detail": "Rolle deaktiviert"}
        self.running_now = True
        try:
            data = await self.collect()
            counts = data["counts"]
            if not any(counts.values()):
                self.last_error = None
                return {"status": "no_data",
                        "detail": "Noch keine Backtest-/Optimizer-/Regime-Ergebnisse vorhanden"}
            extra = ""
            try:
                from services.ai_ml_lab import ml_lab
                extra = await ml_lab.context_text()
            except Exception:
                pass
            prompt = build_prompt(data["digests"], counts, self.report, extra_blocks=extra)
            text, provider, model = await self.engine.generate_for_role(
                self.ROLE, prompt, RESEARCH_SYSTEM, temperature=0.35)
            parsed = self.engine._parse_json(text)

            insights = [i for i in (parsed.get("insights") or []) if isinstance(i, dict)][:12]
            ideas = [i for i in (parsed.get("ideas") or []) if isinstance(i, dict)][:6]
            doc = {
                "summary": str(parsed.get("summary", ""))[:2500],
                "insights": insights,
                "strategy_ranking": [r for r in (parsed.get("strategy_ranking") or [])
                                     if isinstance(r, dict)][:12],
                "regime_notes": [str(r)[:250] for r in (parsed.get("regime_notes") or [])][:8],
                "ideas": ideas,
                "recommendations": [str(r)[:250] for r in (parsed.get("recommendations") or [])][:8],
                "counts": counts,
                "model": f"{provider}/{model}",
                "trigger": trigger,
                "ts": _now_iso(),
            }
            from services import ai_providers
            weight = ai_providers.model_weight(model)
            doc["weight"] = weight
            doc["weight_label"] = ai_providers.weight_label(model)

            await self.db.settings.update_one({"_id": "ai_research_report"},
                                              {"$set": dict(doc)}, upsert=True)
            await self.db.settings.update_one(
                {"_id": "ai_research_state"},
                {"$set": {"counts": counts, "ts": doc["ts"]}}, upsert=True)
            self._last_counts = counts
            self.report = doc
            self.last_run = doc["ts"]
            self._last_run_ts = time.time()
            self.last_error = None

            stored = await memory.remember_many(
                "research_insight", insights, source=f"research_analyst/{model}",
                weight=weight, tags=["research"])
            stored += await memory.remember_many(
                "idea", ideas, source=f"research_analyst/{model}", weight=weight,
                tags=["research", "idea"])
            if doc["summary"]:
                await memory.remember(
                    "research_insight", f"Gesamtbild {doc['ts'][:10]}", doc["summary"],
                    meta={"counts": counts, "recommendations": doc["recommendations"]},
                    tags=["research", "summary"], weight=weight,
                    source=f"research_analyst/{model}")

            await self.db.ai_chat.insert_one({
                "id": str(uuid.uuid4()), "role": "research",
                "text": doc["summary"], "insights": insights,
                "recommendations": doc["recommendations"],
                "strategy_ranking": doc["strategy_ranking"],
                "ideas": ideas, "model": doc["model"],
                "weight_label": doc["weight_label"],
                "manual": manual, "trigger": trigger, "ts": doc["ts"],
            })
            logger.info(f"Forschungs-Analyst fertig ({doc['model']}, {trigger}): "
                        f"{len(insights)} Erkenntnisse, {len(ideas)} Ideen, {stored} im Gedächtnis")
            # Closed Loop (abschaltbar): validierte Kandidaten selbst nachoptimieren
            loop_res = None
            try:
                from services.ai_closed_loop import closed_loop
                loop_res = await closed_loop.maybe_run(trigger=f"Forschung ({trigger})")
            except Exception as e:
                logger.warning(f"Closed-Loop-Start fehlgeschlagen: {str(e)[:150]}")
            return {"status": "ok", "insights": len(insights), "ideas": len(ideas),
                    "summary": doc["summary"], "model": doc["model"], "ts": doc["ts"],
                    "closed_loop": loop_res}
        except Exception as e:
            self.last_error = str(e)[:300]
            logger.error(f"Forschungs-Analyst fehlgeschlagen: {e}")
            return {"status": "error", "detail": self.last_error}
        finally:
            self.running_now = False

    # ---------------- context for other roles ----------------
    async def context_text(self, max_chars: int = 2600) -> str:
        doc = self.report
        if not doc:
            try:
                doc = await self.db.settings.find_one({"_id": "ai_research_report"})
            except Exception:
                doc = None
        if not doc or not doc.get("summary"):
            return ""
        lines = [f"=== FORSCHUNGS-ANALYST ({str(doc.get('ts', ''))[:16]} · "
                 f"{doc.get('model', '?')}, Gewicht {doc.get('weight_label', '?')}) ==="]
        lines.append(str(doc["summary"])[:1200])
        ins = doc.get("insights") or []
        if ins:
            lines.append("ERKENNTNISSE AUS BACKTESTS/OPTIMIZER/REGIME-LAB:")
            for i in ins[:8]:
                conf = i.get("confidence")
                lines.append(f"- {i.get('title')}{f' (Konfidenz {conf}%)' if conf is not None else ''}: "
                             f"{str(i.get('detail'))[:220]}")
        rank = doc.get("strategy_ranking") or []
        if rank:
            lines.append("STRATEGIE-BEWERTUNG: " + " | ".join(
                f"{r.get('strategy')}={r.get('verdict')}" for r in rank[:8]))
        notes = doc.get("regime_notes") or []
        if notes:
            lines.append("REGIME-HINWEISE: " + " | ".join(str(n)[:120] for n in notes[:4]))
        recs = doc.get("recommendations") or []
        if recs:
            lines.append("EMPFEHLUNGEN (stark gewichten):")
            lines.extend(f"- {str(r)[:200]}" for r in recs[:6])
        return "\n".join(lines)[:max_chars]

    def status(self) -> Dict:
        cfg = self._cfg()
        return {
            "enabled": bool(cfg.get("enabled", True)),
            "schedule_times": cfg.get("schedule_times") or [],
            "interval_hours": cfg.get("interval_hours"),
            "auto_on_new_results": cfg.get("auto_on_new_results", True),
            "last_run": self.last_run,
            "last_error": self.last_error,
            "running_now": self.running_now,
            "insights": len((self.report or {}).get("insights") or []),
            "counts": (self.report or {}).get("counts") or self._last_counts,
            "model": (self.report or {}).get("model"),
        }

    # ---------------- scheduling ----------------
    def _due_slot(self, now_berlin: datetime, times: List[str]) -> Optional[str]:
        today = now_berlin.strftime("%Y-%m-%d")
        cur = now_berlin.strftime("%H:%M")
        for slot in times:
            if cur < slot:
                continue
            if slot not in self._ran_slots:
                self._ran_slots[slot] = today  # Boot: vergangenen Slot überspringen
                continue
            if self._ran_slots[slot] != today:
                self._ran_slots[slot] = today
                return slot
        return None

    async def tick(self):
        """Wird vom Engine-Loop aufgerufen (intern auf 60s gedrosselt)."""
        now = time.time()
        if now - self._last_tick < 60 or not self.engine or self.db is None:
            return
        self._last_tick = now
        cfg = self._cfg()
        if not cfg.get("enabled", True) or not self.engine.key:
            return
        if now - self._last_run_ts < self.MIN_GAP_SEC:
            return

        from services.ai_roles import BERLIN_TZ
        slot = self._due_slot(datetime.now(BERLIN_TZ), cfg.get("schedule_times") or [])
        if slot:
            await self.run(trigger=f"schedule {slot}")
            return

        if cfg.get("auto_on_new_results", True):
            counts = await self._counts()
            prev = self._last_counts or {}
            new = sum(max(0, counts.get(k, 0) - prev.get(k, 0)) for k in counts)
            if prev and new >= int(cfg.get("trigger_after_results", 1) or 1):
                self._last_counts = counts
                await self.run(trigger=f"{new} neue Ergebnisse")
                return
            if not prev:
                self._last_counts = counts

        hours = cfg.get("interval_hours")
        if hours and self.last_run:
            try:
                last = datetime.fromisoformat(str(self.last_run).replace("Z", "+00:00"))
                age_h = (datetime.now(timezone.utc) - last).total_seconds() / 3600
                if age_h >= float(hours):
                    await self.run(trigger=f"Intervall {hours}h")
            except Exception:
                pass


research_analyst = ResearchAnalyst()
