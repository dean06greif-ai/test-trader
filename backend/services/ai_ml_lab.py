"""ML-Labor des KI-Ökosystems (Optuna + XGBoost).

Ziel: aus den ECHTEN Ergebnissen der Plattform lernen, welche Marktbedingungen
gute Trades liefern.

Pipeline:
  1. Datensatz bauen: KI-Entscheidungen (`ai_decisions`) bzw. Signale
     (`signals`) + ihr tatsächlicher Ausgang, angereichert mit dem
     Marktzustand zum Entscheidungszeitpunkt (Snapshots des Markt-Beobachters).
  2. Optuna sucht die besten Hyperparameter (TPE) für ein XGBoost-Modell.
  3. XGBoost lernt daraus die Abbildung Marktbedingung -> Gewinnwahrscheinlichkeit.
  4. Ergebnis (Kennzahlen, Feature-Wichtigkeiten, beste Parameter) landet im
     KI-Gedächtnis; der Forschungs-Analyst/KI Trader erklären und nutzen es.

Robustheit: optuna/xgboost/sklearn werden lazy importiert. Fehlen sie oder gibt
es zu wenige Daten, meldet der Status das sauber – die Plattform läuft
unverändert weiter. Das Training läuft in einem Thread (asyncio.to_thread),
damit der Event-Loop nie blockiert.
"""
import base64
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

from services.ai_memory import memory

logger = logging.getLogger(__name__)

# Feste Feature-Reihenfolge – Modell und Vorhersage müssen identisch sein.
FEATURES = [
    "confidence",       # Konfidenz der KI-Entscheidung
    "side_long",        # 1 = LONG, 0 = SHORT
    "rsi",
    "trend_pct",
    "volatility_pct",
    "atr_pct",
    "volume_ratio",
    "range_pos",
    "change_60m_pct",
    "hour_utc",
    "weekday",
    "news_score",       # +1 positiv / -1 negativ / 0 neutral
    "sl_pct",
    "tp1_pct",
    "crv",
]

MIN_SAMPLES = 40
MIN_PER_CLASS = 8
DEFAULT_TRIALS = 25


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ts(value) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def label_of(row: Dict) -> Optional[int]:
    """1 = Gewinn, 0 = Verlust, None = noch offen/unbrauchbar."""
    pnl = row.get("trade_pnl")
    if pnl is not None:
        try:
            pnl = float(pnl)
        except (TypeError, ValueError):
            pnl = None
        if pnl is not None and pnl != 0:
            return 1 if pnl > 0 else 0
    outcome = row.get("outcome") or row.get("result")
    if outcome == "win":
        return 1
    if outcome == "loss":
        return 0
    return None


def nearest_snapshot(snaps: List[Dict], target: datetime,
                     max_gap_min: int = 45) -> Optional[Dict]:
    """Zeitlich nächster Snapshot eines Coins (max. `max_gap_min` Abstand)."""
    best, best_gap = None, None
    for s in snaps:
        ts = _ts(s.get("ts"))
        if not ts:
            continue
        gap = abs((ts - target).total_seconds())
        if best_gap is None or gap < best_gap:
            best, best_gap = s, gap
    if best is None or best_gap is None or best_gap > max_gap_min * 60:
        return None
    return best


def feature_row(decision: Dict, snapshot: Optional[Dict]) -> Dict:
    """Ein Feature-Vektor (Dict) aus Entscheidung + Marktzustand – rein."""
    feats = (snapshot or {}).get("features") or {}
    ts = _ts(decision.get("ts") or decision.get("timestamp")) or datetime.now(timezone.utc)
    action = str(decision.get("action") or decision.get("type") or "").upper()
    news = str(decision.get("news_impact") or "neutral").lower()
    sl = float(decision.get("sl_pct") or 0) or 0.0
    tp1 = float(decision.get("tp1_pct") or 0) or 0.0
    return {
        "confidence": float(decision.get("confidence") or decision.get("ai_confidence") or 0),
        "side_long": 1.0 if action == "LONG" else 0.0,
        "rsi": float(feats.get("rsi") if feats.get("rsi") is not None
                     else (decision.get("rsi") or 50)),
        "trend_pct": float(feats.get("trend_pct") or 0.0),
        "volatility_pct": float(feats.get("volatility_pct") or 0.0),
        "atr_pct": float(feats.get("atr_pct") or 0.0),
        "volume_ratio": float(feats.get("volume_ratio") or 1.0),
        "range_pos": float(feats.get("range_pos") or 50.0),
        "change_60m_pct": float(feats.get("change_60m_pct") or 0.0),
        "hour_utc": float(ts.hour),
        "weekday": float(ts.weekday()),
        "news_score": 1.0 if news == "positive" else (-1.0 if news == "negative" else 0.0),
        "sl_pct": sl,
        "tp1_pct": tp1,
        "crv": round(tp1 / sl, 3) if sl else 0.0,
    }


def build_dataset(decisions: List[Dict], snapshots: List[Dict]) -> Tuple[List[Dict], List[int], Dict]:
    """Entscheidungen + Snapshots -> (X, y, meta). Rein und damit direkt testbar."""
    by_symbol: Dict[str, List[Dict]] = {}
    for s in snapshots or []:
        by_symbol.setdefault(s.get("symbol"), []).append(s)
    X: List[Dict] = []
    y: List[int] = []
    matched = 0
    for d in decisions or []:
        lbl = label_of(d)
        if lbl is None:
            continue
        action = str(d.get("action") or d.get("type") or "").upper()
        if action not in ("LONG", "SHORT"):
            continue
        ts = _ts(d.get("ts") or d.get("timestamp"))
        snap = nearest_snapshot(by_symbol.get(d.get("symbol")) or [], ts) if ts else None
        if snap:
            matched += 1
        X.append(feature_row(d, snap))
        y.append(lbl)
    meta = {"samples": len(y), "wins": sum(y), "losses": len(y) - sum(y),
            "with_market_state": matched}
    return X, y, meta


def to_matrix(rows: List[Dict]):
    import numpy as np
    return np.array([[float(r.get(f, 0.0) or 0.0) for f in FEATURES] for r in rows],
                    dtype="float32")


def libs_available() -> Tuple[bool, str]:
    try:
        import optuna  # noqa: F401
        import xgboost  # noqa: F401
        import sklearn  # noqa: F401
        return True, ""
    except Exception as e:  # pragma: no cover - Umgebungsabhängig
        return False, str(e)[:150]


def train_sync(X: List[Dict], y: List[int], n_trials: int = DEFAULT_TRIALS,
               timeout_sec: int = 120) -> Dict:
    """Optuna-Suche + finales XGBoost-Modell. Blockierend -> via to_thread nutzen."""
    import numpy as np
    import optuna
    import xgboost as xgb
    from sklearn.model_selection import StratifiedKFold, cross_val_score

    optuna.logging.set_verbosity(optuna.logging.WARNING)
    Xm, ym = to_matrix(X), np.array(y, dtype="int32")
    folds = 3 if len(ym) < 150 else 5
    folds = max(2, min(folds, int(min(np.bincount(ym)))))
    cv = StratifiedKFold(n_splits=folds, shuffle=True, random_state=42)
    pos_weight = float(max(1, (ym == 0).sum()) / max(1, (ym == 1).sum()))

    def objective(trial):
        params = {
            "max_depth": trial.suggest_int("max_depth", 2, 6),
            "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.3, log=True),
            "n_estimators": trial.suggest_int("n_estimators", 60, 400),
            "subsample": trial.suggest_float("subsample", 0.6, 1.0),
            "colsample_bytree": trial.suggest_float("colsample_bytree", 0.5, 1.0),
            "min_child_weight": trial.suggest_int("min_child_weight", 1, 10),
            "reg_lambda": trial.suggest_float("reg_lambda", 1e-3, 10.0, log=True),
            "gamma": trial.suggest_float("gamma", 0.0, 5.0),
        }
        model = xgb.XGBClassifier(
            objective="binary:logistic", eval_metric="logloss", tree_method="hist",
            scale_pos_weight=pos_weight, n_jobs=2, random_state=42, **params)
        scores = cross_val_score(model, Xm, ym, cv=cv, scoring="roc_auc", n_jobs=1)
        return float(np.mean(scores))

    study = optuna.create_study(direction="maximize",
                                sampler=optuna.samplers.TPESampler(seed=42))
    study.optimize(objective, n_trials=max(5, int(n_trials)), timeout=timeout_sec,
                   show_progress_bar=False, gc_after_trial=True)
    best = dict(study.best_params)
    final = xgb.XGBClassifier(
        objective="binary:logistic", eval_metric="logloss", tree_method="hist",
        scale_pos_weight=pos_weight, n_jobs=2, random_state=42, **best)
    final.fit(Xm, ym)
    acc_scores = cross_val_score(final, Xm, ym, cv=cv, scoring="accuracy", n_jobs=1)
    gains = final.get_booster().get_score(importance_type="gain")
    importances = []
    for i, name in enumerate(FEATURES):
        importances.append({"feature": name,
                            "gain": round(float(gains.get(f"f{i}", 0.0)), 4)})
    total_gain = sum(x["gain"] for x in importances) or 1.0
    for x in importances:
        x["share_pct"] = round(x["gain"] / total_gain * 100, 1)
    importances.sort(key=lambda x: -x["gain"])
    raw = bytes(final.get_booster().save_raw("json"))
    return {
        "best_params": best,
        "cv_auc": round(float(study.best_value), 4),
        "cv_accuracy": round(float(np.mean(acc_scores)), 4),
        "folds": folds,
        "trials": len(study.trials),
        "samples": int(len(ym)),
        "win_rate_data": round(float(ym.mean()) * 100, 1),
        "importances": importances,
        "booster_b64": base64.b64encode(raw).decode("ascii"),
    }


def importances_text(importances: List[Dict], top: int = 6) -> str:
    return ", ".join(f"{i['feature']} {i.get('share_pct', 0)}%"
                     for i in (importances or [])[:top]) or "(keine)"


class MLLab:
    """Verwaltet Training, Persistenz und Vorhersagen des ML-Modells."""

    def __init__(self):
        self.engine = None
        self.model_meta: Optional[Dict] = None
        self._booster = None
        self.training_now = False
        self.last_error: Optional[str] = None
        self.last_train: Optional[str] = None
        self._last_tick = 0.0
        self._last_train_ts = 0.0
        self._last_labeled = 0
        # ML-Einstellungen (UI-editierbar, persistiert in settings/ai_ml_settings)
        self.settings: Dict = {
            "auto_train": True,
            "train_hour_berlin": 3,      # täglicher Trainingslauf (Berlin)
            "min_new_results": 10,       # oder nach X neuen Ergebnissen
            "n_trials": DEFAULT_TRIALS,
            "lookback_days": 120,
            "explain_with_llm": True,    # Haupt-KI erklärt die Ergebnisse
        }

    def setup(self, engine):
        self.engine = engine

    @property
    def db(self):
        return self.engine.db if self.engine else None

    async def load_state(self):
        try:
            doc = await self.db.settings.find_one({"_id": "ai_ml_settings"})
            if doc:
                doc.pop("_id", None)
                for k in list(self.settings):
                    if k in doc:
                        self.settings[k] = doc[k]
            model = await self.db.settings.find_one({"_id": "ai_ml_model"})
            if model:
                model.pop("_id", None)
                self._restore_model(model)
        except Exception as e:
            logger.warning(f"ML-Labor State laden fehlgeschlagen: {e}")

    def _restore_model(self, doc: Dict):
        self.model_meta = {k: v for k, v in doc.items() if k != "booster_b64"}
        self.last_train = doc.get("trained_at")
        b64 = doc.get("booster_b64")
        if not b64:
            return
        try:
            import xgboost as xgb
            booster = xgb.Booster()
            booster.load_model(bytearray(base64.b64decode(b64)))
            self._booster = booster
        except Exception as e:
            logger.warning(f"ML-Modell laden fehlgeschlagen: {str(e)[:150]}")

    async def reset(self) -> Dict:
        """ML-Trainingsdaten zurücksetzen: gespeichertes Modell + Status löschen."""
        try:
            await self.db.settings.delete_one({"_id": "ai_ml_model"})
        except Exception as e:
            logger.warning(f"ML-Reset: Modell löschen fehlgeschlagen: {e}")
        self._booster = None
        self.model_meta = None
        self.last_train = None
        self.last_error = None
        self._last_train_ts = 0.0
        self._last_labeled = 0
        logger.info("ML-Labor zurückgesetzt (Modell & Status gelöscht)")
        return {"status": "ok"}

    async def update_settings(self, updates: Dict) -> Dict:
        if "auto_train" in updates:
            self.settings["auto_train"] = bool(updates["auto_train"])
        if "explain_with_llm" in updates:
            self.settings["explain_with_llm"] = bool(updates["explain_with_llm"])
        if "train_hour_berlin" in updates:
            self.settings["train_hour_berlin"] = max(0, min(23, int(updates["train_hour_berlin"])))
        if "min_new_results" in updates:
            self.settings["min_new_results"] = max(1, min(500, int(updates["min_new_results"])))
        if "n_trials" in updates:
            self.settings["n_trials"] = max(5, min(200, int(updates["n_trials"])))
        if "lookback_days" in updates:
            self.settings["lookback_days"] = max(7, min(720, int(updates["lookback_days"])))
        await self.db.settings.update_one({"_id": "ai_ml_settings"},
                                          {"$set": dict(self.settings)}, upsert=True)
        return dict(self.settings)

    # ---------------- data ----------------
    # Nur die Felder laden, die label_of()/feature_row() wirklich lesen –
    # volle Dokumente (entry_market_snapshot, reasoning, rules_snapshot, …)
    # erzeugten auf Render 512 MB transiente RAM-Spitzen bei jedem Auto-Training.
    _ROW_PROJECTION = {"_id": 0, "ts": 1, "timestamp": 1, "action": 1, "type": 1,
                       "outcome": 1, "result": 1, "trade_pnl": 1, "confidence": 1,
                       "ai_confidence": 1, "news_impact": 1, "sl_pct": 1,
                       "tp1_pct": 1, "rsi": 1, "symbol": 1}

    async def load_training_data(self) -> Tuple[List[Dict], List[int], Dict]:
        days = int(self.settings.get("lookback_days", 120))
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        decisions = await self.db.ai_decisions.find(
            {"ts": {"$gte": cutoff}},
            projection=self._ROW_PROJECTION).sort("ts", -1).limit(6000).to_list(6000)
        # Signale anderer Strategien als zusätzliche Lernmasse (gleiche Features).
        signals = await self.db.signals.find(
            {"timestamp": {"$gte": cutoff}, "result": {"$in": ["win", "loss"]},
             "signal_class": {"$ne": "PRE_SIGNAL"}},
            projection=self._ROW_PROJECTION).sort("timestamp", -1) \
            .limit(6000).to_list(6000)
        for s in signals:
            s.setdefault("ts", s.get("timestamp"))
            s.setdefault("confidence", s.get("ai_confidence") or 50)
        snapshots = await self.db.ai_market_snapshots.find(
            {"ts": {"$gte": cutoff}},
            projection={"_id": 0, "symbol": 1, "ts": 1, "features": 1}) \
            .sort("ts", -1).limit(30000).to_list(30000)
        X, y, meta = build_dataset(list(decisions) + list(signals), snapshots)
        meta["source"] = {"ai_decisions": len(decisions), "signals": len(signals),
                          "snapshots": len(snapshots)}
        return X, y, meta

    # ---------------- training ----------------
    async def train(self, manual: bool = False, n_trials: Optional[int] = None,
                    trigger: str = "manual") -> Dict:
        if self.training_now:
            return {"status": "busy", "detail": "Training läuft bereits"}
        ok, err = libs_available()
        if not ok:
            return {"status": "unavailable",
                    "detail": f"ML-Bibliotheken fehlen (optuna/xgboost/scikit-learn): {err}"}
        self.training_now = True
        try:
            X, y, meta = await self.load_training_data()
            wins, losses = meta.get("wins", 0), meta.get("losses", 0)
            if meta["samples"] < MIN_SAMPLES or min(wins, losses) < MIN_PER_CLASS:
                self.last_error = None
                return {"status": "insufficient_data", "detail":
                        f"Zu wenige abgeschlossene Ergebnisse: {meta['samples']} "
                        f"({wins} Gewinne / {losses} Verluste). Nötig: "
                        f"{MIN_SAMPLES} Datensätze und je {MIN_PER_CLASS} pro Klasse.",
                        "dataset": meta}
            import asyncio
            trials = int(n_trials or self.settings.get("n_trials", DEFAULT_TRIALS))
            res = await asyncio.to_thread(train_sync, X, y, trials)
            doc = {
                "trained_at": _now_iso(),
                "trigger": trigger,
                "features": FEATURES,
                "dataset": meta,
                **{k: v for k, v in res.items()},
            }
            await self.db.settings.update_one({"_id": "ai_ml_model"},
                                              {"$set": dict(doc)}, upsert=True)
            self._restore_model(doc)
            self._last_train_ts = time.time()
            self._last_labeled = meta["samples"]
            self.last_error = None

            finding = (
                f"XGBoost-Modell (Optuna, {res['trials']} Trials): CV-AUC {res['cv_auc']}, "
                f"Genauigkeit {res['cv_accuracy']}, {res['samples']} Datensätze "
                f"(Gewinnquote der Daten {res['win_rate_data']}%). "
                f"Wichtigste Marktbedingungen: {importances_text(res['importances'])}. "
                f"Beste Hyperparameter: "
                + ", ".join(f"{k}={v}" for k, v in list(res["best_params"].items())[:6])
            )
            await memory.remember("ml_finding",
                                  f"ML-Modell {doc['trained_at'][:16]}", finding,
                                  meta={"cv_auc": res["cv_auc"],
                                        "cv_accuracy": res["cv_accuracy"],
                                        "importances": res["importances"][:8],
                                        "best_params": res["best_params"],
                                        "dataset": meta},
                                  tags=["ml", "xgboost", "optuna"], weight=3,
                                  source="ml_lab")

            explanation = None
            if self.settings.get("explain_with_llm") and self.engine and self.engine.key:
                explanation = await self._explain(res, meta)
            logger.info(f"ML-Labor Training fertig ({trigger}): AUC {res['cv_auc']}, "
                        f"{res['samples']} Datensätze")
            return {"status": "ok", "cv_auc": res["cv_auc"], "cv_accuracy": res["cv_accuracy"],
                    "samples": res["samples"], "trials": res["trials"],
                    "importances": res["importances"][:8], "best_params": res["best_params"],
                    "dataset": meta, "explanation": explanation}
        except Exception as e:
            self.last_error = str(e)[:300]
            logger.error(f"ML-Labor Training fehlgeschlagen: {e}")
            return {"status": "error", "detail": self.last_error}
        finally:
            self.training_now = False
            import gc
            gc.collect()

    async def _explain(self, res: Dict, meta: Dict) -> Optional[str]:
        """Haupt-KI erklärt das Modell in Klartext und leitet Handlungsideen ab."""
        system = (
            "Du bist der 'KI Trader'. Ein XGBoost-Modell wurde mit Optuna auf den echten "
            "Trade-Ergebnissen dieser Plattform trainiert. Erkläre die Ergebnisse nüchtern "
            "auf Deutsch und leite daraus konkrete Handelsregeln ab. Warne bei schwacher "
            "Datenbasis (AUC nahe 0.5 = kein echter Vorhersagewert). Antworte "
            "AUSSCHLIESSLICH mit validem JSON ohne Markdown:\n"
            '{"explanation": "4-8 Sätze", "rules": [{"title": "kurz", "detail": "umsetzbare Regel"}]}'
        )
        try:
            prompt = (
                f"CV-AUC {res['cv_auc']} | Genauigkeit {res['cv_accuracy']} | "
                f"{res['samples']} Datensätze | Gewinnquote der Daten {res['win_rate_data']}% | "
                f"Folds {res['folds']} | Optuna-Trials {res['trials']}\n"
                f"Datenquellen: {meta.get('source')}\n"
                f"Datensätze mit gemessenem Marktzustand: {meta.get('with_market_state')}\n"
                "Feature-Wichtigkeiten (Gain-Anteil):\n"
                + "\n".join(f"- {i['feature']}: {i.get('share_pct')}%"
                            for i in res["importances"][:10])
                + "\nBeste Hyperparameter: "
                + ", ".join(f"{k}={v}" for k, v in res["best_params"].items())
                + "\n\nErkläre das Modell und gib Regeln als JSON zurück."
            )
            text, provider, model = await self.engine.generate_for_role(
                "learner", prompt, system, temperature=0.3)
            data = self.engine._parse_json(text)
            expl = str(data.get("explanation", ""))[:1500]
            rules = [r for r in (data.get("rules") or []) if isinstance(r, dict)][:6]
            await self.db.settings.update_one(
                {"_id": "ai_ml_model"},
                {"$set": {"explanation": expl, "rules": rules,
                          "explained_by": f"{provider}/{model}"}}, upsert=True)
            if self.model_meta is not None:
                self.model_meta["explanation"] = expl
                self.model_meta["rules"] = rules
            await memory.remember_many("ml_finding", rules, source=f"ml_lab/{model}",
                                       weight=3, tags=["ml", "regel"])
            if expl:
                await memory.remember("ml_finding", f"ML-Erklärung {_now_iso()[:16]}", expl,
                                      tags=["ml", "erklärung"], weight=3,
                                      source=f"ml_lab/{model}")
            return expl
        except Exception as e:
            logger.warning(f"ML-Erklärung fehlgeschlagen: {str(e)[:150]}")
            return None

    # ---------------- inference ----------------
    def predict_proba(self, features: Dict) -> Optional[float]:
        """Gewinnwahrscheinlichkeit für einen Feature-Vektor (0..1)."""
        if self._booster is None:
            return None
        try:
            import xgboost as xgb
            dm = xgb.DMatrix(to_matrix([features]))
            return float(self._booster.predict(dm)[0])
        except Exception as e:
            logger.warning(f"ML-Vorhersage fehlgeschlagen: {str(e)[:120]}")
            return None

    def predict_sides(self, market_features: Dict, confidence: float = 70.0) -> Dict:
        """Gewinnwahrscheinlichkeit für LONG und SHORT unter aktuellem Marktzustand."""
        out: Dict[str, Optional[float]] = {}
        for side, flag in (("LONG", 1.0), ("SHORT", 0.0)):
            row = feature_row(
                {"confidence": confidence, "action": side, "ts": _now_iso(),
                 "sl_pct": 0.8, "tp1_pct": 1.2, "news_impact": "neutral"},
                {"features": market_features})
            row["side_long"] = flag
            p = self.predict_proba(row)
            out[side] = round(p * 100, 1) if p is not None else None
        return out

    async def context_text(self) -> str:
        """Prompt-Block für KI Trader / Forschungs-Analyst."""
        meta = self.model_meta
        if not meta:
            try:
                doc = await self.db.settings.find_one({"_id": "ai_ml_model"})
            except Exception:
                doc = None
            if doc:
                doc.pop("_id", None)
                self._restore_model(doc)
                meta = self.model_meta
        if not meta:
            return ""
        lines = [f"=== ML-LABOR (XGBoost + Optuna, trainiert {str(meta.get('trained_at', ''))[:16]}) ===",
                 f"Vorhersagegüte: CV-AUC {meta.get('cv_auc')} · Genauigkeit {meta.get('cv_accuracy')} · "
                 f"{(meta.get('dataset') or {}).get('samples', 0)} echte Ergebnisse",
                 f"Wichtigste Marktbedingungen: {importances_text(meta.get('importances') or [])}"]
        if meta.get("explanation"):
            lines.append(f"Erklärung: {str(meta['explanation'])[:600]}")
        for r in (meta.get("rules") or [])[:5]:
            lines.append(f"- ML-Regel: {r.get('title')}: {str(r.get('detail'))[:180]}")
        try:
            from services.ai_market_observer import market_observer
            preds = []
            for sym, snap in list(market_observer.snapshots.items())[:8]:
                sides = self.predict_sides(snap.get("features") or {})
                if sides.get("LONG") is None:
                    continue
                preds.append(f"{sym}: LONG {sides['LONG']}% / SHORT {sides['SHORT']}%")
            if preds:
                lines.append("Modell-Gewinnwahrscheinlichkeit im AKTUELLEN Marktzustand "
                             "(Orientierung, kein Befehl): " + " | ".join(preds))
        except Exception:
            pass
        if float(meta.get("cv_auc") or 0) < 0.55:
            lines.append("ACHTUNG: AUC < 0.55 – das Modell hat noch kaum Vorhersagekraft. "
                         "Nutze es nur als schwaches Zusatzsignal.")
        return "\n".join(lines)

    def status(self) -> Dict:
        ok, err = libs_available()
        meta = self.model_meta or {}
        return {
            "available": ok,
            "unavailable_reason": None if ok else err,
            "settings": dict(self.settings),
            "training_now": self.training_now,
            "last_train": self.last_train,
            "last_error": self.last_error,
            "model": {
                "trained_at": meta.get("trained_at"),
                "cv_auc": meta.get("cv_auc"),
                "cv_accuracy": meta.get("cv_accuracy"),
                "samples": (meta.get("dataset") or {}).get("samples"),
                "with_market_state": (meta.get("dataset") or {}).get("with_market_state"),
                "trials": meta.get("trials"),
                "best_params": meta.get("best_params"),
                "importances": (meta.get("importances") or [])[:10],
                "explanation": meta.get("explanation"),
                "rules": meta.get("rules") or [],
                "trigger": meta.get("trigger"),
            } if meta else None,
        }

    # ---------------- loop ----------------
    async def tick(self):
        now = time.time()
        if now - self._last_tick < 120 or self.db is None:
            return
        self._last_tick = now
        if not self.settings.get("auto_train") or self.training_now:
            return
        if now - self._last_train_ts < 1800:
            return
        try:
            labeled = await self.db.ai_decisions.count_documents(
                {"outcome": {"$in": ["win", "loss"]}})
            labeled += await self.db.signals.count_documents(
                {"result": {"$in": ["win", "loss"]}})
        except Exception:
            return
        if not self._last_labeled:
            self._last_labeled = labeled
        due_new = labeled - self._last_labeled >= int(self.settings.get("min_new_results", 10))
        due_daily = False
        try:
            from services.ai_roles import BERLIN_TZ
            now_b = datetime.now(BERLIN_TZ)
            if now_b.hour == int(self.settings.get("train_hour_berlin", 3)):
                last = _ts(self.last_train)
                due_daily = (last is None
                             or (datetime.now(timezone.utc) - last).total_seconds() > 20 * 3600)
        except Exception:
            pass
        if due_new or due_daily:
            self._last_labeled = labeled
            await self.train(trigger="auto (neue Ergebnisse)" if due_new else "auto (täglich)")


ml_lab = MLLab()
