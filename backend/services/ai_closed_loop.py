"""Closed-Loop-Selbstoptimierung (abschaltbar).

Ist der Schalter aktiv, stößt der Forschungs-Analyst nach seiner Auswertung
selbstständig einen Optimizer-Lauf für den stärksten Kandidaten an
(gleiche Job-Infrastruktur wie die manuelle Optimierung, `services/optimizer.py`).
Das Ergebnis wandert zurück ins KI-Gedächtnis und in den KI-Chat, sodass der
nächste Forschungs-/Lernlauf darauf aufbaut – die Schleife
Analyse → Optimierung → neues Wissen → Analyse schließt sich.

Bewusste Grenzen (Stabilität vor Automatik):
  * Standardmäßig AUS (`settings/ai_closed_loop.enabled = false`).
  * Höchstens `max_runs_per_day` Läufe, nie parallel zu einer laufenden
    Optimierung, Mindestabstand `min_gap_hours`.
  * Es werden KEINE Parameter automatisch scharfgeschaltet – validierte
    Kandidaten werden als Vorschlag hinterlegt und im Optimizer-UI übernommen.
"""
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

from core import timeutil

from services.ai_memory import memory

logger = logging.getLogger(__name__)

DEFAULT_SETTINGS = {
    "enabled": False,
    "max_runs_per_day": 2,
    "min_gap_hours": 6,
    "days": 60,
    "iterations": 60,
    "objective": "pnl",
    "timeframe": None,          # None = Timeframe des Referenzlaufs
    "walk_forward": True,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def pick_candidate(optimizer_runs: List[Dict], backtests: List[Dict]) -> Optional[Dict]:
    """Kandidat für den nächsten Optimizer-Lauf bestimmen (rein, testbar).

    Bevorzugt den letzten Optimizer-Lauf (bekannte Strategie + Coins + TF),
    sonst die beste Strategie des letzten Backtests."""
    for run in optimizer_runs or []:
        res = run.get("result") or {}
        sid = res.get("strategy_id") or (res.get("definition") or {}).get("id")
        symbols = [s for s in (res.get("symbols") or []) if s]
        if sid and symbols:
            return {"strategy_id": sid, "symbols": symbols[:6],
                    "timeframe": res.get("timeframe"),
                    "source": f"Optimizer-Lauf {str(run.get('created_at', ''))[:16]}"}
    for bt in backtests or []:
        res = bt.get("result") or {}
        per = sorted((res.get("per_strategy") or []),
                     key=lambda s: -float(s.get("pnl") or 0))
        params = bt.get("params") or {}
        symbols = [s for s in (params.get("symbols") or []) if s]
        if per and symbols:
            best = per[0]
            return {"strategy_id": best.get("strategy_id"), "symbols": symbols[:6],
                    "timeframe": best.get("timeframe") or params.get("timeframe"),
                    "source": f"Backtest {str(bt.get('created_at', ''))[:16]} "
                              f"(beste Strategie {best.get('strategy_name')})"}
    return None


class ClosedLoop:
    def __init__(self):
        self.engine = None
        self.settings: Dict = dict(DEFAULT_SETTINGS)
        self.state: Dict = {"runs_today": 0, "day": None, "last_run": None,
                            "last_job_id": None, "last_result": None}
        self.last_error: Optional[str] = None

    def setup(self, engine):
        self.engine = engine

    @property
    def db(self):
        return self.engine.db if self.engine else None

    async def load_state(self):
        try:
            doc = await self.db.settings.find_one({"_id": "ai_closed_loop"})
            if doc:
                for k in DEFAULT_SETTINGS:
                    if k in doc:
                        self.settings[k] = doc[k]
                for k in self.state:
                    if k in doc:
                        self.state[k] = doc[k]
        except Exception as e:
            logger.warning(f"Closed-Loop State laden fehlgeschlagen: {e}")

    async def _persist(self):
        await self.db.settings.update_one(
            {"_id": "ai_closed_loop"},
            {"$set": {**self.settings, **self.state}}, upsert=True)

    async def update_settings(self, updates: Dict) -> Dict:
        if "enabled" in updates:
            self.settings["enabled"] = bool(updates["enabled"])
        if "walk_forward" in updates:
            self.settings["walk_forward"] = bool(updates["walk_forward"])
        for key, lo, hi in (("max_runs_per_day", 1, 12), ("min_gap_hours", 1, 72),
                            ("days", 7, 365), ("iterations", 20, 400)):
            if key in updates:
                try:
                    self.settings[key] = max(lo, min(hi, int(updates[key])))
                except (TypeError, ValueError):
                    pass
        if updates.get("objective") in ("pnl", "win_rate", "profit_factor", "sharpe"):
            self.settings["objective"] = updates["objective"]
        await self._persist()
        return dict(self.settings)

    def _quota_left(self) -> bool:
        # Tages-Quote nach deutscher Zeit (wie alle anderen Tages-Grenzen der App)
        today = timeutil.berlin_date()
        if self.state.get("day") != today:
            self.state["day"] = today
            self.state["runs_today"] = 0
        return int(self.state.get("runs_today", 0)) < int(self.settings.get("max_runs_per_day", 2))

    def _gap_ok(self) -> bool:
        last = self.state.get("last_run")
        if not last:
            return True
        try:
            ts = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
        except ValueError:
            return True
        return datetime.now(timezone.utc) - ts >= timedelta(
            hours=float(self.settings.get("min_gap_hours", 6)))

    async def maybe_run(self, trigger: str = "research") -> Dict:
        """Vom Forschungs-Analysten aufgerufen (oder manuell)."""
        if not self.settings.get("enabled"):
            return {"status": "disabled"}
        if not self._quota_left():
            return {"status": "quota", "detail": "Tageslimit erreicht"}
        if not self._gap_ok():
            return {"status": "cooldown",
                    "detail": f"Mindestabstand {self.settings['min_gap_hours']}h nicht erreicht"}
        return await self.run_now(trigger=trigger)

    async def run_now(self, trigger: str = "manual") -> Dict:
        from core import state as core_state
        from core.state import scanner
        from core.utils import _watch_job_task
        from services import optimizer as opt
        from services.bitunix_trade import DEFAULT_COIN_CFG
        from strategies.registry import registry as strategy_registry

        if any(j["status"] == "running" for j in opt.JOBS.values()):
            return {"status": "busy", "detail": "Es läuft bereits eine Optimierung"}
        runs = await self.db.optimizer_runs.find().sort("created_at", -1).limit(5).to_list(5)
        backtests = await self.db.backtests.find().sort("created_at", -1).limit(5).to_list(5)
        cand = pick_candidate(runs, backtests)
        if not cand or not strategy_registry.get(cand.get("strategy_id")):
            return {"status": "no_candidate",
                    "detail": "Kein geeigneter Kandidat (erst Backtest/Optimizer laufen lassen)"}
        body = {
            "mode": "params",
            "strategy_id": cand["strategy_id"],
            "symbols": cand["symbols"],
            "days": int(self.settings.get("days", 60)),
            "timeframe": self.settings.get("timeframe") or cand.get("timeframe"),
            "objective": self.settings.get("objective", "pnl"),
            "iterations": int(self.settings.get("iterations", 60)),
            "algorithm": "bayes",
            # Robustheits-Konfiguration im Format des Optimizers (services/robustness.py)
            "walk_forward": ({"enabled": True, "mode": "rolling"}
                             if self.settings.get("walk_forward", True) else {}),
            "execution": "cloud",
        }
        params = {k: body.get(k) for k in ("mode", "strategy_id", "symbols", "days",
                                          "timeframe", "objective", "iterations",
                                          "algorithm", "walk_forward")}
        params["execution"] = "cloud"
        params["trigger"] = f"KI Closed-Loop ({trigger})"
        job_id = opt.create_job(params)
        task = asyncio.create_task(opt.run_optimizer(job_id, body, strategy_registry,
                                                     scanner.settings, DEFAULT_COIN_CFG,
                                                     core_state.db))
        _watch_job_task(task, opt.JOBS, job_id)
        asyncio.create_task(self._watch_result(job_id, cand, body))
        self.state.update({"runs_today": int(self.state.get("runs_today", 0)) + 1,
                           "last_run": _now_iso(), "last_job_id": job_id})
        await self._persist()
        logger.info(f"Closed-Loop startet Optimierung {job_id} für "
                    f"{cand['strategy_id']} ({trigger})")
        await memory.remember(
            "idea", f"Closed-Loop Optimierung gestartet: {cand['strategy_id']}",
            f"Auslöser: {trigger}. Basis: {cand['source']}. Coins: "
            f"{', '.join(cand['symbols'])}, {body['days']} Tage, Ziel {body['objective']}.",
            meta={"job_id": job_id, "body": params}, tags=["closed_loop"], weight=2,
            source="closed_loop")
        return {"status": "started", "job_id": job_id, "candidate": cand, "body": params}

    async def _watch_result(self, job_id: str, cand: Dict, body: Dict):
        """Wartet auf das Job-Ende und legt das Ergebnis als Vorschlag ab."""
        from services import optimizer as opt
        for _ in range(720):                      # max. 60 Minuten
            await asyncio.sleep(5)
            job = opt.JOBS.get(job_id)
            if not job or job.get("status") in ("done", "error", "cancelled"):
                break
        job = opt.JOBS.get(job_id) or {}
        if job.get("status") != "done":
            self.state["last_result"] = {"job_id": job_id, "status": job.get("status"),
                                         "ts": _now_iso()}
            await self._persist()
            return
        res = job.get("result") or {}
        top = (res.get("top5") or [])
        best = top[0] if top else {}
        m = best.get("metrics") or {}
        tm = best.get("test_metrics") or {}
        proposal = {
            "job_id": job_id, "status": "done", "ts": _now_iso(),
            "strategy_id": cand["strategy_id"], "symbols": cand["symbols"],
            "timeframe": body.get("timeframe"), "objective": body.get("objective"),
            "params": best.get("params") or {}, "trade_params": best.get("trade_params") or {},
            "metrics": {"pnl": m.get("pnl"), "win_rate": m.get("win_rate"),
                        "trades": m.get("trades"), "max_drawdown": m.get("max_drawdown")},
            "test_metrics": {"pnl": tm.get("pnl"), "win_rate": tm.get("win_rate")},
            "passed": bool(best.get("passed")),
            "fail_reasons": best.get("fail_reasons") or [],
        }
        self.state["last_result"] = proposal
        await self._persist()
        text = (f"Closed-Loop-Optimierung für {cand['strategy_id']} beendet: "
                f"PnL {m.get('pnl')} / Winrate {m.get('win_rate')}% / "
                f"{m.get('trades')} Trades"
                + (f", Holdout PnL {tm.get('pnl')}" if tm else "")
                + f". Validierung: {'bestanden' if proposal['passed'] else 'nicht bestanden'}. "
                  f"Parameter: "
                + ", ".join(f"{k}={v}" for k, v in list(proposal["params"].items())[:8]))
        await memory.remember(
            "research_insight" if proposal["passed"] else "lesson",
            f"Closed-Loop Ergebnis {cand['strategy_id']}", text,
            meta=proposal, tags=["closed_loop", "optimizer"],
            weight=3 if proposal["passed"] else 2, source="closed_loop")
        try:
            import uuid
            await self.db.ai_chat.insert_one({
                "id": str(uuid.uuid4()), "role": "research", "text": text,
                "closed_loop": proposal, "ts": _now_iso()})
        except Exception:
            pass

    def status(self) -> Dict:
        return {"settings": dict(self.settings), "state": dict(self.state),
                "last_error": self.last_error}


closed_loop = ClosedLoop()
