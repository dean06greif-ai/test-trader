"""Gate v1 (Shadow-Modus) – Meta-Labeler des ML-Umbaus (Phase 5).

Hybrid-Architektur: Das LLM bleibt Entscheider. Das Gate predictet parallel die
Gewinnwahrscheinlichkeit jeder LONG/SHORT-Entscheidung und loggt sie NUR
(`gate_shadow` an der ai_decision) – es blockt nichts. Da nichts geblockt wird,
laufen auch "hätte geblockt"-Trades real zu Ende (Paper/Collection) →
kontrafaktische Auswertung über die echten Outcomes (shadow_report).

Anti-Overfitting (bewusst anders als das alte ml_lab):
  * Purged Walk-Forward statt geshuffelter StratifiedKFold (kein Look-Ahead)
  * Embargo zwischen Train/Test (Label-Leakage über offene Trades)
  * Kalibrierung (Platt) auf Out-of-Sample-Predictions
  * Brier-Score vs. Baseline (konstante Win-Rate) als Stopp-Metrik
  * Modelle versioniert in `ml_gate_models` – nie überschrieben

Datenbasis v1 (User-Freigabe 14.08.): Prod-Signale + Ghost-Trades + Decisions,
NUR LESEND via PROD_MONGO_URL (Dev); auf Render ist MONGO_URL selbst die
Prod-DB → dort wird ohne PROD_MONGO_URL die eigene DB gelesen. Krypto-only.
"""
import base64
import logging
import math
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

from core.instruments import TOP_10_COINS
from services.ai_ml_lab import nearest_snapshot, _ts

logger = logging.getLogger(__name__)

GATE_FEATURES = [
    "confidence", "side_long", "rsi", "trend_pct", "volatility_pct", "atr_pct",
    "volume_ratio", "range_pos", "change_60m_pct", "hour_utc", "weekday",
    "sl_pct", "tp1_pct", "crv", "regime_trend", "regime_vol", "regime_breakout",
    "has_market_state", "src_decision", "src_signal", "src_ghost",
]

MIN_SAMPLES = 120
MIN_PER_CLASS = 25
EMBARGO_HOURS = 24
WF_FOLDS = 5
MODELS_COLL = "ml_gate_models"
SETTINGS_ID = "ml_gate_settings"
DEFAULT_SETTINGS = {"threshold": 0.45, "shadow_enabled": True,
                    "auto_retrain": True, "retrain_hour_berlin": 4,
                    "retrain_min_new": 50}
XGB_PARAMS = dict(max_depth=3, n_estimators=250, learning_rate=0.05,
                  subsample=0.9, colsample_bytree=0.8, min_child_weight=5,
                  reg_lambda=2.0, gamma=0.5)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def encode_regime(regime: Optional[str]) -> Tuple[float, float, float]:
    """Regime-String (Observer-Heuristik) -> (trend, vol, breakout)."""
    r = str(regime or "")
    trend = 1.0 if r.startswith("trend_up") else (-1.0 if r.startswith("trend_down") else 0.0)
    vol = 2.0 if r.endswith("volatil") else (0.0 if r.endswith("ruhig") else 1.0)
    breakout = 1.0 if r.startswith("breakout") else 0.0
    return trend, vol, breakout


def gate_feature_row(side: str, confidence: float, sl_pct: float, tp1_pct: float,
                     ts: Optional[datetime], feats: Optional[Dict],
                     source: str) -> Dict:
    """Ein Feature-Vektor (rein, testbar). source: decision|signal|ghost."""
    f = feats or {}
    ts = ts or datetime.now(timezone.utc)
    sl = float(sl_pct or 0.0)
    tp1 = float(tp1_pct or 0.0)
    trend, vol, breakout = encode_regime(f.get("regime"))
    return {
        "confidence": float(confidence or 0.0),
        "side_long": 1.0 if str(side).upper() in ("LONG", "long") else 0.0,
        "rsi": float(f.get("rsi") if f.get("rsi") is not None else 50.0),
        "trend_pct": float(f.get("trend_pct") or 0.0),
        "volatility_pct": float(f.get("volatility_pct") or 0.0),
        "atr_pct": float(f.get("atr_pct") or 0.0),
        "volume_ratio": float(f.get("volume_ratio") or 1.0),
        "range_pos": float(f.get("range_pos") or 50.0),
        "change_60m_pct": float(f.get("change_60m_pct") or 0.0),
        "hour_utc": float(ts.hour),
        "weekday": float(ts.weekday()),
        "sl_pct": sl,
        "tp1_pct": tp1,
        "crv": round(tp1 / sl, 3) if sl else 0.0,
        "regime_trend": trend,
        "regime_vol": vol,
        "regime_breakout": breakout,
        "has_market_state": 1.0 if f else 0.0,
        "src_decision": 1.0 if source == "decision" else 0.0,
        "src_signal": 1.0 if source == "signal" else 0.0,
        "src_ghost": 1.0 if source == "ghost" else 0.0,
    }


def _pct_dist(entry, level) -> float:
    try:
        entry, level = float(entry), float(level)
        return abs(entry - level) / entry * 100 if entry else 0.0
    except (TypeError, ValueError):
        return 0.0


def row_from_decision(dec: Dict, snaps: Optional[List[Dict]] = None) -> Optional[Tuple[Dict, int, float, datetime]]:
    """ai_decision -> (row, label, weight, ts). None wenn unbrauchbar."""
    outcome = dec.get("outcome")
    if outcome not in ("win", "loss"):
        return None
    ts = _ts(dec.get("ts"))
    if not ts:
        return None
    ems = dec.get("entry_market_snapshot") or {}
    feats = ems.get("features")
    if not feats and snaps:
        snap = nearest_snapshot(snaps, ts)
        feats = (snap or {}).get("features")
    row = gate_feature_row(dec.get("action"), dec.get("confidence"),
                           dec.get("sl_pct"), dec.get("tp1_pct"), ts, feats, "decision")
    weight = 1.0 if dec.get("outcome_source") == "trade_pnl" else 0.8
    if dec.get("data_collection"):
        weight *= 0.85
    return row, (1 if outcome == "win" else 0), weight, ts


def row_from_signal(sig: Dict, snaps: Optional[List[Dict]] = None) -> Optional[Tuple[Dict, int, float, datetime]]:
    result = sig.get("result")
    if result not in ("win", "loss") or sig.get("result_ambiguous"):
        return None
    ts = _ts(sig.get("timestamp"))
    if not ts:
        return None
    snap = nearest_snapshot(snaps or [], ts)
    feats = (snap or {}).get("features")
    if not feats and sig.get("rsi") is not None:
        feats = {"rsi": sig.get("rsi")}
    entry = sig.get("entry_price")
    sl_pct = _pct_dist(entry, sig.get("stop_loss"))
    tp1_pct = _pct_dist(entry, sig.get("take_profit_1"))
    row = gate_feature_row(sig.get("type"), 0.0, sl_pct, tp1_pct, ts, feats, "signal")
    weight = 0.7 if sig.get("result_source") == "trade_pnl" else 0.6
    return row, (1 if result == "win" else 0), weight, ts


def row_from_ghost(g: Dict, snaps: Optional[List[Dict]] = None) -> Optional[Tuple[Dict, int, float, datetime]]:
    result = g.get("result")
    if result not in ("win", "loss"):
        return None
    ts = _ts(g.get("opened_at"))
    if not ts:
        return None
    snap = nearest_snapshot(snaps or [], ts)
    feats = (snap or {}).get("features")
    entry = g.get("entry")
    sl_pct = _pct_dist(entry, g.get("sl"))
    tp1_pct = _pct_dist(entry, g.get("tp"))
    row = gate_feature_row(g.get("side"), 0.0, sl_pct, tp1_pct, ts, feats, "ghost")
    return row, (1 if result == "win" else 0), 0.5, ts


def purged_walk_forward(timestamps: List[datetime], n_folds: int = WF_FOLDS,
                        embargo_hours: int = EMBARGO_HOURS,
                        min_train: int = 50, min_test: int = 10) -> List[Tuple[List[int], List[int]]]:
    """Zeitliche Blöcke, Train nur STRIKT vor Test-Start minus Embargo.

    Erwartet aufsteigend sortierte timestamps. Block 0 ist reines Anfangs-Training.
    """
    n = len(timestamps)
    if n < min_train + min_test:
        return []
    block = n // (n_folds + 1)
    splits = []
    embargo = timedelta(hours=embargo_hours)
    for k in range(1, n_folds + 1):
        start = k * block
        end = n if k == n_folds else (k + 1) * block
        test_idx = list(range(start, end))
        if len(test_idx) < min_test:
            continue
        cutoff = timestamps[start] - embargo
        train_idx = [i for i in range(start) if timestamps[i] < cutoff]
        if len(train_idx) < min_train:
            continue
        splits.append((train_idx, test_idx))
    return splits


def _to_matrix(rows: List[Dict]):
    import numpy as np
    return np.array([[float(r.get(f, 0.0) or 0.0) for f in GATE_FEATURES] for r in rows],
                    dtype="float32")


def _brier(p, y) -> float:
    import numpy as np
    return float(np.mean((np.asarray(p, dtype="float64") - np.asarray(y, dtype="float64")) ** 2))


def _fit_xgb(Xm, ym, wm):
    import xgboost as xgb
    pos = max(1, int((ym == 1).sum()))
    neg = max(1, int((ym == 0).sum()))
    model = xgb.XGBClassifier(objective="binary:logistic", eval_metric="logloss",
                              tree_method="hist", scale_pos_weight=neg / pos,
                              n_jobs=2, random_state=42, **XGB_PARAMS)
    model.fit(Xm, ym, sample_weight=wm)
    return model


def train_sync(rows: List[Dict], y: List[int], w: List[float],
               timestamps: List[datetime]) -> Dict:
    """Purged-WF-Training + Platt-Kalibrierung. Blockierend -> via to_thread."""
    import numpy as np
    order = sorted(range(len(rows)), key=lambda i: timestamps[i])
    rows = [rows[i] for i in order]
    y = [y[i] for i in order]
    w = [w[i] for i in order]
    timestamps = [timestamps[i] for i in order]
    Xm = _to_matrix(rows)
    ym = np.array(y, dtype="int32")
    wm = np.array(w, dtype="float32")

    splits = purged_walk_forward(timestamps)
    oos_pred: List[float] = []
    oos_true: List[int] = []
    base_pred: List[float] = []
    for train_idx, test_idx in splits:
        model = _fit_xgb(Xm[train_idx], ym[train_idx], wm[train_idx])
        p = model.predict_proba(Xm[test_idx])[:, 1]
        oos_pred += [float(x) for x in p]
        oos_true += [int(ym[i]) for i in test_idx]
        base_pred += [float(ym[train_idx].mean())] * len(test_idx)

    metrics: Dict = {"folds_used": len(splits), "oos_samples": len(oos_true),
                     "embargo_hours": EMBARGO_HOURS}
    calib = None
    if oos_true:
        try:
            from sklearn.metrics import roc_auc_score
            if len(set(oos_true)) > 1:
                metrics["oos_auc"] = round(float(roc_auc_score(oos_true, oos_pred)), 4)
        except Exception:
            pass
        metrics["oos_brier_raw"] = round(_brier(oos_pred, oos_true), 4)
        metrics["baseline_brier"] = round(_brier(base_pred, oos_true), 4)
        try:
            from sklearn.linear_model import LogisticRegression
            lr = LogisticRegression(C=1.0, max_iter=1000)
            lr.fit(np.asarray(oos_pred).reshape(-1, 1), np.asarray(oos_true))
            calib = {"coef": float(lr.coef_[0][0]), "intercept": float(lr.intercept_[0])}
            cal_p = [_apply_calib(p, calib) for p in oos_pred]
            metrics["oos_brier_calibrated"] = round(_brier(cal_p, oos_true), 4)
        except Exception as e:
            logger.warning(f"Gate-Kalibrierung fehlgeschlagen: {e}")
        best = min(metrics.get("oos_brier_calibrated", 9), metrics.get("oos_brier_raw", 9))
        metrics["beats_baseline"] = bool(best < metrics["baseline_brier"])
        # Kalibrierungs-Bins (Placebo-Detektor fürs spätere Dashboard)
        bins = []
        cal_all = [_apply_calib(p, calib) if calib else p for p in oos_pred]
        for b in range(10):
            lo, hi = b / 10, (b + 1) / 10
            sel = [i for i, p in enumerate(cal_all) if lo <= p < hi or (b == 9 and p == 1.0)]
            if sel:
                bins.append({"bin": f"{lo:.1f}-{hi:.1f}", "n": len(sel),
                             "predicted": round(sum(cal_all[i] for i in sel) / len(sel), 3),
                             "actual": round(sum(oos_true[i] for i in sel) / len(sel), 3)})
        metrics["calibration_bins"] = bins

    final = _fit_xgb(Xm, ym, wm)
    gains = final.get_booster().get_score(importance_type="gain")
    importances = sorted(
        [{"feature": name, "gain": round(float(gains.get(f"f{i}", 0.0)), 4)}
         for i, name in enumerate(GATE_FEATURES)], key=lambda x: -x["gain"])
    total = sum(x["gain"] for x in importances) or 1.0
    for x in importances:
        x["share_pct"] = round(x["gain"] / total * 100, 1)
    return {
        "booster_b64": base64.b64encode(bytes(final.get_booster().save_raw("json"))).decode("ascii"),
        "calibration": calib,
        "metrics": metrics,
        "importances": importances,
        "samples": int(len(ym)),
        "win_rate_data": round(float(ym.mean()) * 100, 1),
        "params": dict(XGB_PARAMS),
    }


def _apply_calib(p: float, calib: Optional[Dict]) -> float:
    if not calib:
        return float(p)
    z = calib["coef"] * float(p) + calib["intercept"]
    return 1.0 / (1.0 + math.exp(-z))


def evaluate_shadow(items: List[Dict], threshold: float) -> Dict:
    """Kontrafaktische Auswertung (rein). items: {p_win, label(1/0), r?}."""
    n = len(items)
    wins = [i for i in items if i["label"] == 1]
    losses = [i for i in items if i["label"] == 0]
    blocked = [i for i in items if i["p_win"] < threshold]
    passed = [i for i in items if i["p_win"] >= threshold]
    blocked_losers = sum(1 for i in blocked if i["label"] == 0)
    blocked_winners = sum(1 for i in blocked if i["label"] == 1)
    pct_losers_blocked = blocked_losers / len(losses) * 100 if losses else 0.0
    pct_winners_blocked = blocked_winners / len(wins) * 100 if wins else 0.0
    r_all = [i["r"] for i in items if i.get("r") is not None]
    r_passed = [i["r"] for i in passed if i.get("r") is not None]
    avg_r_all = sum(r_all) / len(r_all) if r_all else None
    avg_r_passed = sum(r_passed) / len(r_passed) if r_passed else None
    uplift_pct = None
    if avg_r_all is not None and avg_r_passed is not None and abs(avg_r_all) > 1e-9:
        uplift_pct = (avg_r_passed - avg_r_all) / abs(avg_r_all) * 100
    brier = _brier([i["p_win"] for i in items], [i["label"] for i in items]) if items else None
    base_rate = len(wins) / n if n else 0.0
    baseline = _brier([base_rate] * n, [i["label"] for i in items]) if items else None
    return {
        "threshold": threshold, "evaluated": n,
        "wins": len(wins), "losses": len(losses),
        "blocked": len(blocked), "passed": len(passed),
        "pct_losers_blocked": round(pct_losers_blocked, 1),
        "pct_winners_blocked": round(pct_winners_blocked, 1),
        "avg_r_all": round(avg_r_all, 3) if avg_r_all is not None else None,
        "avg_r_passed": round(avg_r_passed, 3) if avg_r_passed is not None else None,
        "economic_uplift_pct": round(uplift_pct, 1) if uplift_pct is not None else None,
        "brier": round(brier, 4) if brier is not None else None,
        "baseline_brier": round(baseline, 4) if baseline is not None else None,
        "criteria": {
            "min_samples_150": n >= 150,
            "losers_blocked_ge_35": pct_losers_blocked >= 35.0,
            "winners_blocked_le_15": pct_winners_blocked <= 15.0,
            "uplift_ge_20": uplift_pct is not None and uplift_pct >= 20.0,
            "brier_beats_baseline": (brier is not None and baseline is not None
                                     and brier < baseline),
        },
    }


class MLGate:
    """Versioniertes Gate-Modell + Shadow-Predictions. Blockt NIE."""

    def __init__(self):
        self.db = None
        self._prod_client = None
        self._booster = None
        self.model_meta: Optional[Dict] = None
        self.settings: Dict = dict(DEFAULT_SETTINGS)
        self.training_now = False
        self.last_error: Optional[str] = None
        self.shadow_count = 0
        self._last_tick = 0.0

    def setup(self, db):
        self.db = db

    # ---------------- Datenquelle (Dev: Prod nur lesend) ----------------
    def _source_db(self):
        url = os.environ.get("PROD_MONGO_URL")
        if url:
            if self._prod_client is None:
                from motor.motor_asyncio import AsyncIOMotorClient
                self._prod_client = AsyncIOMotorClient(url)
            return self._prod_client[os.environ.get("PROD_DB_NAME", "crypto_scanner")], "prod_readonly"
        return self.db, "local"

    async def load_state(self):
        if self.db is None:
            return
        try:
            doc = await self.db.settings.find_one({"_id": SETTINGS_ID})
            if doc:
                doc.pop("_id", None)
                self.settings.update({k: doc[k] for k in DEFAULT_SETTINGS if k in doc})
            latest = await self.db[MODELS_COLL].find_one(
                {}, sort=[("version", -1)], projection={"_id": 0})
            if latest:
                self._restore(latest)
                logger.info(f"Gate v1 geladen: v{latest.get('version')} "
                            f"(Brier {latest.get('metrics', {}).get('oos_brier_calibrated')})")
        except Exception as e:
            logger.warning(f"Gate load_state: {e}")

    def _restore(self, doc: Dict):
        import xgboost as xgb
        booster = xgb.Booster()
        booster.load_model(bytearray(base64.b64decode(doc["booster_b64"])))
        self._booster = booster
        meta = dict(doc)
        meta.pop("booster_b64", None)
        self.model_meta = meta

    async def update_settings(self, updates: Dict) -> Dict:
        if "threshold" in updates:
            self.settings["threshold"] = min(0.95, max(0.05, float(updates["threshold"])))
        if "shadow_enabled" in updates:
            self.settings["shadow_enabled"] = bool(updates["shadow_enabled"])
        if "auto_retrain" in updates:
            self.settings["auto_retrain"] = bool(updates["auto_retrain"])
        if "retrain_hour_berlin" in updates:
            self.settings["retrain_hour_berlin"] = min(23, max(0, int(updates["retrain_hour_berlin"])))
        if "retrain_min_new" in updates:
            self.settings["retrain_min_new"] = min(1000, max(10, int(updates["retrain_min_new"])))
        if self.db is not None:
            await self.db.settings.update_one(
                {"_id": SETTINGS_ID}, {"$set": dict(self.settings)}, upsert=True)
        return dict(self.settings)

    # ---------------- Dataset ----------------
    async def build_dataset(self) -> Tuple[List[Dict], List[int], List[float], List[datetime], Dict]:
        db, source = self._source_db()
        if db is None:
            raise RuntimeError("Keine Datenbank verfügbar")
        crypto = list(TOP_10_COINS)
        snaps_by_sym: Dict[str, List[Dict]] = {}
        cursor = db.ai_market_snapshots.find(
            {"symbol": {"$in": crypto}},
            projection={"_id": 0, "symbol": 1, "ts": 1, "features": 1})
        async for s in cursor:
            snaps_by_sym.setdefault(s.get("symbol"), []).append(s)

        rows, y, w, tss = [], [], [], []
        counts = {"decision": 0, "signal": 0, "ghost": 0}
        decs = await db.ai_decisions.find(
            {"action": {"$in": ["LONG", "SHORT"]}, "outcome": {"$in": ["win", "loss"]},
             "symbol": {"$in": crypto}},
            projection={"_id": 0, "action": 1, "confidence": 1, "sl_pct": 1, "tp1_pct": 1,
                        "ts": 1, "outcome": 1, "outcome_source": 1, "symbol": 1,
                        "entry_market_snapshot": 1, "data_collection": 1}).to_list(20000)
        for d in decs:
            item = row_from_decision(d, snaps_by_sym.get(d.get("symbol")))
            if item:
                rows.append(item[0]); y.append(item[1]); w.append(item[2]); tss.append(item[3])
                counts["decision"] += 1
        sigs = await db.signals.find(
            {"result": {"$in": ["win", "loss"]}, "symbol": {"$in": crypto},
             "strategy_id": {"$ne": "ai_trader"}},
            projection={"_id": 0, "type": 1, "timestamp": 1, "result": 1, "result_source": 1,
                        "result_ambiguous": 1, "entry_price": 1, "stop_loss": 1,
                        "take_profit_1": 1, "rsi": 1, "symbol": 1}).to_list(50000)
        for s in sigs:
            item = row_from_signal(s, snaps_by_sym.get(s.get("symbol")))
            if item:
                rows.append(item[0]); y.append(item[1]); w.append(item[2]); tss.append(item[3])
                counts["signal"] += 1
        ghosts = await db.ai_ghost_trades.find(
            {"result": {"$in": ["win", "loss"]}, "symbol": {"$in": crypto}},
            projection={"_id": 0, "side": 1, "opened_at": 1, "result": 1,
                        "entry": 1, "sl": 1, "tp": 1, "symbol": 1}).to_list(20000)
        for g in ghosts:
            item = row_from_ghost(g, snaps_by_sym.get(g.get("symbol")))
            if item:
                rows.append(item[0]); y.append(item[1]); w.append(item[2]); tss.append(item[3])
                counts["ghost"] += 1
        meta = {"source": source, "samples": len(y), "wins": sum(y),
                "losses": len(y) - sum(y), "by_source": counts,
                "with_market_state": sum(1 for r in rows if r["has_market_state"]),
                "crypto_symbols": crypto}
        return rows, y, w, tss, meta

    # ---------------- Training ----------------
    async def train(self, trigger: str = "manuell") -> Dict:
        if self.training_now:
            return {"status": "error", "detail": "Training läuft bereits"}
        self.training_now = True
        self.last_error = None
        try:
            rows, y, w, tss, meta = await self.build_dataset()
            wins, losses = sum(y), len(y) - sum(y)
            if len(y) < MIN_SAMPLES or min(wins, losses) < MIN_PER_CLASS:
                self.last_error = (f"Zu wenig Daten: {len(y)} Samples "
                                   f"({wins} win / {losses} loss), "
                                   f"Minimum {MIN_SAMPLES}/{MIN_PER_CLASS} je Klasse")
                return {"status": "error", "detail": self.last_error, "dataset": meta}
            import asyncio
            res = await asyncio.to_thread(train_sync, rows, y, w, tss)
            latest = await self.db[MODELS_COLL].find_one({}, sort=[("version", -1)],
                                                         projection={"version": 1})
            version = int((latest or {}).get("version", 0)) + 1
            doc = {"version": version, "trained_at": _now_iso(), "trigger": trigger,
                   "features": list(GATE_FEATURES), "dataset": meta, **res}
            await self.db[MODELS_COLL].insert_one(dict(doc))
            doc.pop("_id", None)
            self._restore(doc)
            logger.info(f"Gate v1 trainiert: v{version}, {res['samples']} Samples, "
                        f"Metriken {res['metrics']}")
            return {"status": "ok", "version": version, "dataset": meta,
                    "metrics": res["metrics"],
                    "importances": res["importances"][:8]}
        except Exception as e:
            self.last_error = str(e)[:300]
            logger.error(f"Gate-Training fehlgeschlagen: {e}")
            return {"status": "error", "detail": self.last_error}
        finally:
            self.training_now = False
            import gc
            gc.collect()

    # ---------------- Auto-Retrain (Engine-Loop ruft tick()) ----------------
    async def _count_labeled(self) -> int:
        db, _ = self._source_db()
        crypto = list(TOP_10_COINS)
        n = await db.ai_decisions.count_documents(
            {"action": {"$in": ["LONG", "SHORT"]}, "outcome": {"$in": ["win", "loss"]},
             "symbol": {"$in": crypto}})
        n += await db.signals.count_documents(
            {"result": {"$in": ["win", "loss"]}, "symbol": {"$in": crypto},
             "strategy_id": {"$ne": "ai_trader"}})
        n += await db.ai_ghost_trades.count_documents(
            {"result": {"$in": ["win", "loss"]}, "symbol": {"$in": crypto}})
        return int(n)

    def _retrain_due(self, labeled: int, now_berlin_hour: int) -> Optional[str]:
        """Rein/testbar: Grund fürs Auto-Retrain oder None."""
        if not self.settings.get("auto_retrain", True) or self.training_now:
            return None
        if self._booster is None:
            return "auto (Erst-Training)" if labeled >= MIN_SAMPLES else None
        last_samples = int(((self.model_meta or {}).get("dataset") or {}).get("samples") or 0)
        if labeled - last_samples >= int(self.settings.get("retrain_min_new", 50)):
            return "auto (neue Ergebnisse)"
        if now_berlin_hour == int(self.settings.get("retrain_hour_berlin", 4)):
            last = _ts((self.model_meta or {}).get("trained_at"))
            if last is None or (datetime.now(timezone.utc) - last).total_seconds() > 20 * 3600:
                return "auto (täglich)"
        return None

    async def tick(self):
        now = time.time()
        if now - self._last_tick < 1800 or self.db is None:
            return
        self._last_tick = now
        try:
            labeled = await self._count_labeled()
            from services.ai_roles import BERLIN_TZ
            reason = self._retrain_due(labeled, datetime.now(BERLIN_TZ).hour)
        except Exception as e:
            logger.warning(f"Gate tick: {e}")
            return
        if reason:
            logger.info(f"Gate Auto-Retrain ({reason}, {labeled} gelabelte Samples)")
            await self.train(trigger=reason)

    # ---------------- Shadow-Prediction ----------------
    def predict_row(self, row: Dict) -> Optional[Dict]:
        if self._booster is None:
            return None
        import xgboost as xgb
        raw = float(self._booster.inplace_predict(_to_matrix([row]))[0])
        calib = (self.model_meta or {}).get("calibration")
        p = _apply_calib(raw, calib)
        return {"p_win": round(p, 4), "raw": round(raw, 4)}

    def shadow_predict(self, dec: Dict) -> Optional[Dict]:
        """Nie werfen, nie blocken – nur loggen. None wenn kein Modell/aus."""
        try:
            # Fix 0.7a (B6): Gate ist krypto-only trainiert -> keine
            # Out-of-Domain-Predictions für OIL/GOLD/SPY/Forex etc.
            if dec.get("symbol") not in TOP_10_COINS:
                return None
            if self._booster is None or not self.settings.get("shadow_enabled", True):
                return None
            ems = dec.get("entry_market_snapshot") or {}
            row = gate_feature_row(dec.get("action"), dec.get("confidence"),
                                   dec.get("sl_pct"), dec.get("tp1_pct"),
                                   _ts(dec.get("ts")), ems.get("features"), "decision")
            pred = self.predict_row(row)
            if pred is None:
                return None
            thr = float(self.settings.get("threshold", 0.45))
            self.shadow_count += 1
            return {**pred, "model_version": (self.model_meta or {}).get("version"),
                    "threshold": thr, "would_block": pred["p_win"] < thr}
        except Exception as e:
            logger.warning(f"Gate shadow_predict: {e}")
            return None

    # ---------------- Kontrafaktischer Report ----------------
    async def shadow_report(self, days: int = 28, threshold: Optional[float] = None) -> Dict:
        if self.db is None:
            return {"status": "error", "detail": "Keine DB"}
        thr = float(threshold if threshold is not None else self.settings.get("threshold", 0.45))
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))).isoformat()
        # Fix 0.7a (B6): Report nur über die Trainings-Domain (krypto-only) –
        # bestehende Out-of-Domain-gate_shadow-Einträge werden ausgeschlossen.
        decs = await self.db.ai_decisions.find(
            {"gate_shadow.p_win": {"$exists": True}, "outcome": {"$in": ["win", "loss"]},
             "ts": {"$gte": cutoff}, "symbol": {"$in": list(TOP_10_COINS)}},
            projection={"_id": 0, "id": 1, "gate_shadow": 1, "outcome": 1}).to_list(20000)
        trades = await self.db.auto_trades.find(
            {"decision_id": {"$in": [d.get("id") for d in decs]}, "status": "closed"},
            projection={"_id": 0, "decision_id": 1, "realized_pnl": 1, "risk": 1}).to_list(20000)
        r_by_dec = {}
        for t in trades:
            try:
                risk = float(t.get("risk") or 0)
                pnl = float(t.get("realized_pnl") or 0)
                r_by_dec[t.get("decision_id")] = (pnl / risk) if risk > 0 else None
            except (TypeError, ValueError):
                pass
        items = [{"p_win": float(d["gate_shadow"]["p_win"]),
                  "label": 1 if d["outcome"] == "win" else 0,
                  "r": r_by_dec.get(d.get("id"))} for d in decs]
        report = evaluate_shadow(items, thr)
        report["threshold_sweep"] = [evaluate_shadow(items, t / 100)
                                     for t in range(30, 61, 5)] if items else []
        report["days"] = days
        report["model_version"] = (self.model_meta or {}).get("version")
        report["note"] = ("Gate-Aktivierung NUR mit expliziter User-Freigabe, wenn alle "
                          "criteria über rollierende 4 Wochen mit >=150 Entscheidungen gelten.")
        return report

    def status(self) -> Dict:
        m = self.model_meta or {}
        return {
            "mode": "shadow",
            "model_loaded": self._booster is not None,
            "version": m.get("version"),
            "trained_at": m.get("trained_at"),
            "trigger": m.get("trigger"),
            "metrics": m.get("metrics"),
            "dataset": m.get("dataset"),
            "settings": dict(self.settings),
            "training_now": self.training_now,
            "shadow_predictions_since_boot": self.shadow_count,
            "last_error": self.last_error,
        }


ml_gate = MLGate()
