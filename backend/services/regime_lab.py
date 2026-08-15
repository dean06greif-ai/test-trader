"""Regime-Lab: Regime-Analysen erstellen, speichern und für die
regime-gezielte Strategie-Suche wiederverwenden.

Kernidee (siehe Anforderungen):
- Für eine Konfiguration (Coins + Timeframe + Zeitraum + Regime-Einstellungen)
  werden Marktphasen gesucht und gespeichert – kombiniert über alle Coins UND
  je Coin einzeln, damit man vergleichen kann, ob Coins ähnliche Phasen haben.
- Die Analyse speichert je Coin einen komprimierten Kursverlauf + die
  Regime-Abschnitte, damit das Frontend die Phasen direkt am Chart anzeigen kann.
- Optionaler Holdout (train_pct < 100): Das Regime-Modell wird NUR auf dem
  Trainingsteil geclustert; der hintere Teil bleibt unangetastet für den
  finalen Walk-Forward-Test der zusammengestellten dynamischen Strategie.
- Klassifikation ist rein rückblickend (services.regime) -> kein Lookahead.
"""
import asyncio
import bisect
import logging
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

import aiohttp

from services import regime as rg
from services import regime_engine as eng
from services.backtester import JobCancelled

logger = logging.getLogger(__name__)

JOBS: Dict[str, Dict] = {}

CHART_MAX_POINTS = 1200
MAX_ANALYSES = 40


def create_job(kind: str, params: Dict) -> str:
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"id": job_id, "kind": kind, "status": "running", "progress": 0,
                    "phase": "Startet", "params": params, "cancel": False,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "result": None, "error": None}
    if len(JOBS) > 10:
        for k in list(JOBS.keys())[:-10]:
            JOBS.pop(k, None)
    return job_id


def running_job() -> Optional[Dict]:
    for j in JOBS.values():
        if j.get("status") == "running":
            return j
    return None


async def fetch_histories(symbols: List[str], days: int, timeframe: str,
                          job: Dict = None, end_ts: Dict[str, int] = None,
                          progress_span=(0, 10)) -> Dict[str, List[Dict]]:
    """Kerzen laden + auf den Timeframe aggregieren. Mit end_ts (aus einer
    gespeicherten Analyse) werden die Daten exakt auf den Analyse-Zeitraum
    geschnitten, damit spätere Läufe reproduzierbar bleiben."""
    from services.backtester import fetch_history
    from services.timeframes import aggregate_candles
    histories: Dict[str, List[Dict]] = {}
    p0, p1 = progress_span
    async with aiohttp.ClientSession() as session:
        for i, sym in enumerate(symbols):
            if job and job.get("cancel"):
                raise JobCancelled()
            if job:
                job["phase"] = f"Lade Daten: {sym}"
                job["progress"] = p0 + round(i / max(len(symbols), 1) * (p1 - p0))
            raw = await fetch_history(session, sym, days, job=job)
            candles = aggregate_candles(raw, timeframe)
            del raw
            if end_ts and end_ts.get(sym):
                candles = [c for c in candles if c["timestamp"] <= end_ts[sym]]
            if len(candles) > 100:
                histories[sym] = candles
    return histories


def _downsample(candles: List[Dict], max_pts: int = CHART_MAX_POINTS) -> List[List]:
    step = max(len(candles) // max_pts, 1)
    pts = [[int(c["timestamp"]), float(c["close"])] for c in candles[::step]]
    last = candles[-1]
    if pts and pts[-1][0] != int(last["timestamp"]):
        pts.append([int(last["timestamp"]), float(last["close"])])
    return pts


def _ema_payload(candles: List[Dict], timeframe: str, ema_days,
                 max_pts: int = CHART_MAX_POINTS) -> Dict[str, List[List]]:
    """EMA-Linien (Tages-Perioden) fürs Chart – identische Abtastung wie
    _downsample, damit die Punkte zeitlich zu den Kurs-Punkten passen."""
    import numpy as np
    from services import regime_features as rf
    bpd = rf.bars_per_day(timeframe)
    close = np.array([float(c["close"]) for c in candles], dtype=float)
    step = max(len(candles) // max_pts, 1)
    out: Dict[str, List[List]] = {}
    for d in (ema_days or []):
        try:
            dd = float(d)
        except (TypeError, ValueError):
            continue
        span = max(int(round(dd * bpd)), 2)
        if span >= len(close):
            continue
        e = rf.ema(close, span)
        pts = [[int(candles[i]["timestamp"]), float(round(float(e[i]), 8))]
               for i in range(0, len(candles), step)]
        if pts and pts[-1][0] != int(candles[-1]["timestamp"]):
            pts.append([int(candles[-1]["timestamp"]),
                        float(round(float(e[-1]), 8))])
        key = str(int(dd)) if dd.is_integer() else str(dd)
        out[key] = pts
    return out


def _segments_payload(candles: List[Dict], labels: List) -> List[Dict]:
    out = []
    for (s, e, rid) in rg.segments_from_labels(labels):
        out.append({"regime": int(rid),
                    "from_ts": int(candles[s]["timestamp"]),
                    "to_ts": int(candles[min(e, len(candles) - 1)]["timestamp"]),
                    "bars": int(e - s)})
    return out


def _validation_payload(candles: List[Dict], labels: List, model: Dict) -> Optional[Dict]:
    """Logische Prüfung der erkannten Regime (nur Engine v2): passt jedes Label
    zum tatsächlichen Kursverlauf? Ergebnis wird mit der Analyse gespeichert."""
    if not rg.is_v2(model):
        return None
    try:
        return eng.validate_labels(candles, labels, model)
    except Exception as e:  # noqa: BLE001 – Prüfung darf die Analyse nie killen
        logger.warning(f"regime validation failed: {e}")
        return None


def _ideal_payload(candles: List[Dict], labels: List, model: Dict) -> Optional[Dict]:
    """Rückblick-Vergleich ("so lagen die Phasen wirklich") – NUR zur Anzeige,
    nutzt Zukunftssicht und wird nie für Backtests/Live verwendet."""
    if not rg.is_v2(model):
        return None
    try:
        mode = eng.norm_mode((model.get("config") or {}).get("regime_mode", 9))
        ideal = eng.ideal_labels(model, candles)
        return {"segments": _segments_payload(candles, ideal),
                "agreement": eng.agreement_with_ideal(labels, ideal, mode),
                "lookahead": True}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"ideal labels failed: {e}")
        return None


def _current_payload(candles: List[Dict], model: Dict, timeframe: str,
                     conf_min: float, min_hold_days: float) -> Optional[Dict]:
    """Aktuelles Regime am Ende der Analysedaten (für die Anzeige im Regime-Lab)."""
    try:
        cur = rg.current_regime(model, candles, timeframe, conf_min, min_hold_days)
        return {k: v for k, v in cur.items() if k != "similarities"}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"current regime failed: {e}")
        return None


def _validation_summary(per_symbol: Dict[str, Dict]) -> Dict:
    reps = [v.get("validation") for v in per_symbol.values() if v.get("validation")]
    if not reps:
        return {}
    bars = [r["violation_bars_pct"] for r in reps]
    accs = [r["direction_accuracy_pct"] for r in reps
            if r.get("direction_accuracy_pct") is not None]
    return {"symbols": len(reps),
            "violation_bars_pct": round(sum(bars) / len(bars), 2),
            "worst_violation_bars_pct": round(max(bars), 2),
            "direction_accuracy_pct": (round(sum(accs) / len(accs), 1) if accs else None),
            "avg_segment_days": round(sum(r["avg_segment_days"] for r in reps)
                                      / len(reps), 2),
            "passed": all(r["passed"] for r in reps)}


def _regime_usage(segments_by_sym: Dict[str, List[Dict]], timeframe: str) -> Dict:
    """Wie viele Bars/Tage entfallen je Regime auf die Analyse? (Plausibilitäts-Check)"""
    bpd = rg.bars_per_day(timeframe)
    usage: Dict[int, Dict] = {}
    for segs in segments_by_sym.values():
        for s in segs:
            u = usage.setdefault(s["regime"], {"bars": 0, "segments": 0})
            u["bars"] += s["bars"]
            u["segments"] += 1
    return {str(k): {"bars": v["bars"], "segments": v["segments"],
                     "days": round(v["bars"] / max(bpd, 1e-9), 1)}
            for k, v in usage.items()}


def _coin_similarity(histories: Dict[str, List[Dict]],
                     labels_map: Dict[str, List]) -> List[Dict]:
    """Anteil der Zeit, in der zwei Coins (unter dem kombinierten Modell) im
    selben Regime sind – hilft beim Finden von Coins mit ähnlichen Phasen."""
    ts_maps = {}
    for sym, candles in histories.items():
        labels = labels_map.get(sym) or []
        ts_maps[sym] = {int(candles[i]["timestamp"]): labels[i]
                        for i in range(len(labels)) if labels[i] is not None}
    syms = sorted(ts_maps.keys())
    out = []
    for i in range(len(syms)):
        for j in range(i + 1, len(syms)):
            a, b = ts_maps[syms[i]], ts_maps[syms[j]]
            common = a.keys() & b.keys()
            if not common:
                continue
            same = sum(1 for t in common if a[t] == b[t])
            out.append({"a": syms[i], "b": syms[j],
                        "agreement_pct": round(same / len(common) * 100, 1),
                        "bars": len(common)})
    out.sort(key=lambda x: -x["agreement_pct"])
    return out


def _live_agreement(candles, live_labels, final_labels, model,
                    train_end_ts=None):
    """Wie gut trifft die LIVE-Sicht (kausal, ohne Zukunftswissen) die
    pivot-korrigierten FINAL-Phasen? Richtung je Kerze – gesamt, nur im
    Holdout (Walk-Forward-Testzeitraum) und nur auf Trend-Kerzen. Das ist
    die entscheidende Kennzahl dafür, ob man die Regime-Umschaltung im
    Paper-/Live-Trading nutzen kann."""
    mode = eng.norm_mode((model.get("config") or {}).get(
        "regime_mode", model.get("regime_mode", eng.DEFAULT_REGIME_MODE)))
    tot = same = hot = hsame = ttot = tsame = 0
    for c, lv, fn in zip(candles, live_labels, final_labels):
        if lv is None or fn is None:
            continue
        a = eng.split_id(int(lv), mode)[0]
        b = eng.split_id(int(fn), mode)[0]
        tot += 1
        eq = int(a == b)
        same += eq
        if b != 1:
            ttot += 1
            tsame += eq
        if train_end_ts and int(c["timestamp"]) > int(train_end_ts):
            hot += 1
            hsame += eq
    pct = lambda x, y: round(x / y * 100.0, 1) if y else None  # noqa: E731
    return {"direction_pct": pct(same, tot),
            "holdout_direction_pct": pct(hsame, hot),
            "trend_hit_pct": pct(tsame, ttot),
            "bars": tot, "holdout_bars": hot}


def _symbol_payload(model: Dict, candles, timeframe: str, conf_min: float,
                    min_hold_days: float, with_ideal: bool,
                    train_end_ts=None):
    """CPU-lastige Auswertung EINES Symbols (läuft in einem Thread, damit der
    Event-Loop – und damit Worker-Heartbeat/API – nie blockiert)."""
    reactive = (rg.is_v2(model) and str((model.get("config") or {})
                .get("detector") or "reactive") != "regression")
    if reactive:
        # Reaktive Erkennung: Analyse-Sicht = pivot-korrigierte Phasen (jede
        # Phase beginnt am bestätigten Hoch-/Tiefpunkt). Live-Sicht und die
        # Selbstkorrekturen ("wann wurde die Umkehr erkannt") werden zusätzlich
        # gespeichert. Live/Backtests nutzen weiterhin nur die Live-Labels.
        pay = eng.reactive_payload(model, candles)
        labels = pay["final_labels"]
        entry = {"segments": _segments_payload(candles, labels),
                 "live_segments": _segments_payload(candles, pay["live_labels"]),
                 "corrections": pay["report"],
                 "live_agreement": _live_agreement(candles, pay["live_labels"],
                                                   labels, model, train_end_ts),
                 "validation": _validation_payload(candles, labels, model),
                 "current": _current_payload(candles, model, timeframe,
                                             conf_min, min_hold_days),
                 "ideal": (_ideal_payload(candles, labels, model)
                           if with_ideal else None)}
        return labels, entry
    labels = rg.classify_series(model, candles, timeframe, conf_min, min_hold_days)
    entry = {"segments": _segments_payload(candles, labels),
             "validation": _validation_payload(candles, labels, model),
             "current": _current_payload(candles, model, timeframe,
                                         conf_min, min_hold_days),
             "ideal": (_ideal_payload(candles, labels, model)
                       if with_ideal else None)}
    return labels, entry


async def run_analysis(job_id: str, body: Dict, db):
    """Regime-Analyse-Job: Modelle clustern (kombiniert + je Coin), Abschnitte
    berechnen und alles als wiederverwendbare Analyse speichern."""
    job = JOBS[job_id]
    try:
        symbols = body.get("symbols") or []
        timeframe = body.get("timeframe") or "5m"
        days = int(min(max(int(body.get("days") or 180), 7), 5500))
        scope = body.get("scope") or "both"
        max_regimes = int(min(max(int(body.get("max_regimes") or 5), 2), 10))
        lookback_days = float(min(max(float(body.get("lookback_days") or 3), 0.5), 60))
        min_share = float(min(max(float(body.get("min_share_pct") or 5), 1), 30))
        conf_min = float(min(max(float(body.get("confidence_min") or 70), 50), 95)) / 100.0
        min_hold_days = float(min(max(float(body.get("min_hold_days") or 2), 0.25), 60))
        train_pct = float(min(max(float(body.get("train_pct") or 100), 50), 100))
        engine = (body.get("engine") or rg.DEFAULT_ENGINE).lower()
        engine_config = body.get("engine_config") or {}
        if engine == "v2":
            # Wechsel-Einstellungen der Oberfläche gelten auch für die Engine v2.
            # Wichtig: die Mindesthaltedauer wird bei aktiver automatischer
            # Anpassung NICHT aus der Oberfläche übernommen – sonst würde der
            # Standardwert (2 Tage) die zeitraum-abhängige Glättung aushebeln.
            auto_on = bool(engine_config.get("auto_adapt",
                                             eng.DEFAULT_CONFIG["auto_adapt"]))
            prof = str(engine_config.get("adapt_profile",
                                        eng.DEFAULT_CONFIG["adapt_profile"])).lower()
            engine_config = {**engine_config,
                             "confidence_min": engine_config.get("confidence_min", conf_min)}
            if not (auto_on and prof != "off"):
                engine_config["min_hold_days"] = engine_config.get("min_hold_days",
                                                                   min_hold_days)
            # Granularität (3/5/9) darf auch direkt im Body stehen
            if body.get("regime_mode") is not None:
                engine_config["regime_mode"] = eng.norm_mode(body["regime_mode"])
            if body.get("adapt_profile"):
                engine_config["adapt_profile"] = str(body["adapt_profile"]).lower()
            if body.get("auto_adapt") is not None:
                engine_config["auto_adapt"] = bool(body["auto_adapt"])
        with_ideal = bool(body.get("with_ideal", True))

        histories = await fetch_histories(symbols, days, timeframe, job)
        if not histories:
            raise RuntimeError("Zu wenig Daten für diesen Timeframe/Zeitraum")

        def stop():
            return bool(job.get("cancel"))

        bounds = {}
        train_hist = {}
        for sym, candles in histories.items():
            cut = int(len(candles) * train_pct / 100.0)
            cut = min(max(cut, 100), len(candles))
            train_hist[sym] = candles[:cut]
            bounds[sym] = {"start_ts": int(candles[0]["timestamp"]),
                           "end_ts": int(candles[-1]["timestamp"]),
                           "train_end_ts": (int(candles[cut - 1]["timestamp"])
                                            if cut < len(candles) else None),
                           "bars": len(candles)}

        combined = None
        if scope in ("both", "combined"):
            if stop():
                raise JobCancelled()
            job["phase"] = "Kombiniertes Regime-Modell clustern (alle Coins)"
            job["progress"] = 20
            model = await asyncio.to_thread(
                rg.detect_regimes, train_hist, timeframe, max_regimes,
                lookback_days, min_share, engine=engine,
                engine_config=engine_config)
            if model:
                labels_map, per_symbol = {}, {}
                for sym, candles in histories.items():
                    if stop():
                        raise JobCancelled()
                    job["phase"] = f"Regime klassifizieren: {sym}"
                    labels, entry = await asyncio.to_thread(
                        _symbol_payload, model, candles, timeframe,
                        conf_min, min_hold_days, with_ideal,
                        (bounds.get(sym) or {}).get("train_end_ts"))
                    labels_map[sym] = labels
                    per_symbol[sym] = entry
                segs_by_sym = {s: v["segments"] for s, v in per_symbol.items()}
                combined = {"model": model, "per_symbol": per_symbol,
                            "usage": _regime_usage(segs_by_sym, timeframe),
                            "validation": _validation_summary(per_symbol),
                            "coin_similarity": _coin_similarity(histories, labels_map)}
        per_coin = {}
        if scope in ("both", "per_coin"):
            for i, (sym, candles) in enumerate(histories.items()):
                if stop():
                    raise JobCancelled()
                job["phase"] = f"Regime-Modell je Coin: {sym}"
                job["progress"] = 40 + round(i / max(len(histories), 1) * 50)
                model_s = await asyncio.to_thread(
                    rg.detect_regimes, {sym: train_hist[sym]}, timeframe,
                    max_regimes, lookback_days, min_share,
                    engine=engine, engine_config=engine_config)
                if not model_s:
                    per_coin[sym] = {"error": "Zu wenig Daten für dieses Coin-Modell"}
                    continue
                labels, entry = await asyncio.to_thread(
                    _symbol_payload, model_s, candles, timeframe,
                    conf_min, min_hold_days, with_ideal,
                    (bounds.get(sym) or {}).get("train_end_ts"))
                segs = entry["segments"]
                per_coin[sym] = {"model": model_s, **entry,
                                 "usage": _regime_usage({sym: segs}, timeframe)}

        if not combined and not per_coin:
            raise RuntimeError("Regime konnten nicht bestimmt werden – Zeitraum erhöhen")

        job["phase"] = "Analyse speichern"
        job["progress"] = 95
        chart_ema_days = engine_config.get("chart_ema_days") \
            if isinstance(engine_config.get("chart_ema_days"), (list, tuple)) else None
        chart_ema_days = list(chart_ema_days or [9, 21, 50, 200])
        aid = f"ra_{uuid.uuid4().hex[:8]}"
        doc = {"id": aid,
               "job_id": job_id,
               "name": body.get("name") or f"Regime-Analyse {timeframe} · {days}d",
               "symbols": list(histories.keys()), "timeframe": timeframe,
               "days": days, "scope": scope,
               "settings": {"max_regimes": max_regimes, "lookback_days": lookback_days,
                            "min_share_pct": min_share,
                            "confidence_min": round(conf_min * 100, 0),
                            "min_hold_days": min_hold_days, "train_pct": train_pct,
                            "engine": engine, "engine_config": engine_config,
                            "regime_mode": (eng.norm_mode(
                                engine_config.get("regime_mode",
                                                  eng.DEFAULT_REGIME_MODE))
                                if engine == "v2" else None)},
               "bounds": bounds,
               "chart": {sym: _downsample(c) for sym, c in histories.items()},
               "chart_emas": {sym: _ema_payload(c, timeframe, chart_ema_days)
                              for sym, c in histories.items()},
               "combined": combined, "per_coin": per_coin,
               "kept": {}, "assignments": {}, "walkforward": {},
               "created_at": datetime.now(timezone.utc).isoformat()}
        result = {"kind": "analysis", "analysis_id": aid}
        if db is not None:
            await persist_analysis(db, doc)
        else:
            # Lokaler Worker: kein DB-Zugriff – Dokument mit dem Ergebnis
            # zurückschicken, der Server persistiert es (persist_worker_result).
            result["analysis_doc"] = doc
        job["result"] = result
        job["status"] = "done"
        job["progress"] = 100
        job["phase"] = "Fertig"
    except JobCancelled:
        job["status"] = "cancelled"
        job["phase"] = "Abgebrochen"
    except Exception as e:  # noqa: BLE001 – Job-Fehler sauber melden
        logger.exception(f"regime analysis {job_id} failed")
        job["status"] = "error"
        job["error"] = str(e)[:300]
        job["phase"] = "Fehler"


# ---------------- EMA-Perioden-Vergleich ----------------
async def run_ema_compare(job_id: str, body: Dict, db):
    """Vergleicht mehrere EMA-Perioden (z.B. 5/9/14) für den Detektor 'ema'
    auf denselben Daten: Final-/Live-Segmente, Live=Final-Trefferquoten
    (gesamt/Holdout/Trend), Phasendauern und Validierung – als Tabelle."""
    job = JOBS[job_id]
    try:
        symbols = body.get("symbols") or []
        timeframe = body.get("timeframe") or "15m"
        days = int(min(max(int(body.get("days") or 360), 30), 5500))
        train_pct = float(min(max(float(body.get("train_pct") or 75), 50), 100))
        periods = [float(p) for p in (body.get("periods") or [5, 9, 14])
                   if 2 <= float(p) <= 100][:8]
        if not periods:
            raise RuntimeError("Mindestens 1 EMA-Periode (2-100 Tage) angeben")
        engine_config = dict(body.get("engine_config") or {})
        conf_min = float(engine_config.get("confidence_min") or 0.55)
        min_hold = float(engine_config.get("min_hold_days") or 0)

        histories = await fetch_histories(symbols, days, timeframe, job)
        if not histories:
            raise RuntimeError("Zu wenig Daten für diesen Timeframe/Zeitraum")
        bpd = rg.bars_per_day(timeframe)
        bounds, train_hist = {}, {}
        for sym, candles in histories.items():
            cut = min(max(int(len(candles) * train_pct / 100.0), 100), len(candles))
            train_hist[sym] = candles[:cut]
            bounds[sym] = (int(candles[cut - 1]["timestamp"])
                           if cut < len(candles) else None)

        rows = []
        for pi, period in enumerate(periods):
            if job.get("cancel"):
                raise JobCancelled()
            job["phase"] = f"EMA {period:g} Tage rechnen"
            job["progress"] = 10 + round(pi / len(periods) * 85)
            ec = {**engine_config, "detector": "ema", "ema_regime_days": period}
            model = await asyncio.to_thread(
                rg.detect_regimes, train_hist, timeframe, 5, 3.0, 5.0,
                engine="v2", engine_config=ec)
            if not model:
                rows.append({"period": period, "error": "Modell fehlgeschlagen"})
                continue
            agg = {"direction_pct": [], "holdout_direction_pct": [],
                   "trend_hit_pct": [], "avg_final_days": [], "avg_live_days": [],
                   "switches_final": 0, "switches_live": 0,
                   "violation_pct": [], "passed": True}
            for sym, candles in histories.items():
                if job.get("cancel"):
                    raise JobCancelled()
                _labels, entry = await asyncio.to_thread(
                    _symbol_payload, model, candles, timeframe,
                    conf_min, min_hold, False, bounds.get(sym))
                la = entry.get("live_agreement") or {}
                for k in ("direction_pct", "holdout_direction_pct", "trend_hit_pct"):
                    if la.get(k) is not None:
                        agg[k].append(la[k])
                segs = entry.get("segments") or []
                lsegs = entry.get("live_segments") or []
                if segs:
                    agg["avg_final_days"].append(
                        sum(s["bars"] for s in segs) / len(segs) / max(bpd, 1e-9))
                if lsegs:
                    agg["avg_live_days"].append(
                        sum(s["bars"] for s in lsegs) / len(lsegs) / max(bpd, 1e-9))
                agg["switches_final"] += max(len(segs) - 1, 0)
                agg["switches_live"] += max(len(lsegs) - 1, 0)
                va = entry.get("validation") or {}
                if va.get("violation_bars_pct") is not None:
                    agg["violation_pct"].append(va["violation_bars_pct"])
                    agg["passed"] = agg["passed"] and bool(va.get("passed"))
            mean = lambda xs: round(sum(xs) / len(xs), 1) if xs else None  # noqa: E731
            rows.append({"period": period,
                         "direction_pct": mean(agg["direction_pct"]),
                         "holdout_direction_pct": mean(agg["holdout_direction_pct"]),
                         "trend_hit_pct": mean(agg["trend_hit_pct"]),
                         "avg_final_segment_days": mean(agg["avg_final_days"]),
                         "avg_live_segment_days": mean(agg["avg_live_days"]),
                         "switches_final": agg["switches_final"],
                         "switches_live": agg["switches_live"],
                         "violation_pct": mean(agg["violation_pct"]),
                         "passed": agg["passed"]})
        best = max((r for r in rows if r.get("holdout_direction_pct") is not None),
                   key=lambda r: r["holdout_direction_pct"], default=None)
        result = {"kind": "ema_compare", "rows": rows,
                  "best_period": best["period"] if best else None,
                  "symbols": list(histories.keys()), "timeframe": timeframe,
                  "days": days, "train_pct": train_pct,
                  "created_at": datetime.now(timezone.utc).isoformat()}
        if db is not None:
            try:
                await db.regime_lab_runs.replace_one(
                    {"id": job_id}, {"id": job_id, "result": result,
                                     "created_at": result["created_at"]}, upsert=True)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"ema_compare persist failed: {e}")
        job["result"] = result
        job["status"] = "done"
        job["progress"] = 100
        job["phase"] = "Fertig"
    except JobCancelled:
        job["status"] = "cancelled"
        job["phase"] = "Abgebrochen"
    except Exception as e:  # noqa: BLE001
        logger.exception(f"ema compare {job_id} failed")
        job["status"] = "error"
        job["error"] = str(e)[:300]
        job["phase"] = "Fehler"


# ---------------- Kombi-Detektor: Auto-Kalibrierung ----------------
async def run_kombi_calibrate(job_id: str, body: Dict, db):
    """Auto-Kalibrierung für den Detektor 'kombi': Grid-Suche über die
    Trend-Schwelle (kombi_thr) und das Steigungs-Fenster (kombi_slope_days).
    Ziel: mittlere FINAL-Phasendauer im Zielband (Default 5-15 Tage) bei
    maximaler Holdout-Trefferquote (Live=Final, kein Lookahead)."""
    job = JOBS[job_id]
    try:
        symbols = body.get("symbols") or []
        timeframe = body.get("timeframe") or "15m"
        days = int(min(max(int(body.get("days") or 360), 30), 5500))
        train_pct = float(min(max(float(body.get("train_pct") or 75), 50), 95))
        t_lo = float(body.get("target_min_days") or 5.0)
        t_hi = max(float(body.get("target_max_days") or 15.0), t_lo + 1.0)
        thr_grid = sorted({round(float(x), 3) for x in
                           (body.get("thr_grid")
                            or [0.10, 0.14, 0.18, 0.22, 0.26, 0.30])
                           if 0.05 <= float(x) <= 1.0})[:10]
        slope_grid = sorted({round(float(x), 2) for x in
                             (body.get("slope_grid") or [2, 3, 5, 7])
                             if 0.5 <= float(x) <= 20})[:8]
        if not thr_grid or not slope_grid:
            raise RuntimeError("Mindestens je 1 Wert für Schwelle und Fenster angeben")
        engine_config = dict(body.get("engine_config") or {})
        engine_config["detector"] = "kombi"
        conf_min = float(engine_config.get("confidence_min") or 0.55)
        min_hold = float(engine_config.get("min_hold_days") or 0)

        histories = await fetch_histories(symbols, days, timeframe, job)
        if not histories:
            raise RuntimeError("Zu wenig Daten für diesen Timeframe/Zeitraum")
        bpd = rg.bars_per_day(timeframe)
        bounds, train_hist = {}, {}
        for sym, candles in histories.items():
            cut = min(max(int(len(candles) * train_pct / 100.0), 100), len(candles))
            train_hist[sym] = candles[:cut]
            bounds[sym] = (int(candles[cut - 1]["timestamp"])
                           if cut < len(candles) else None)

        combos = [(t, s) for t in thr_grid for s in slope_grid]
        rows = []
        for ci, (thr, slope) in enumerate(combos):
            if job.get("cancel"):
                raise JobCancelled()
            job["phase"] = f"Schwelle {thr:g} · Fenster {slope:g}d ({ci + 1}/{len(combos)})"
            job["progress"] = 10 + round(ci / len(combos) * 85)
            ec = {**engine_config, "kombi_thr": thr, "kombi_slope_days": slope}
            model = await asyncio.to_thread(
                rg.detect_regimes, train_hist, timeframe, 5, 3.0, 5.0,
                engine="v2", engine_config=ec)
            if not model:
                rows.append({"thr": thr, "slope_days": slope,
                             "error": "Modell fehlgeschlagen"})
                continue
            agg = {"direction_pct": [], "holdout_direction_pct": [],
                   "trend_hit_pct": [], "avg_final_days": [],
                   "avg_live_days": [], "switches_final": 0,
                   "switches_live": 0}
            for sym, candles in histories.items():
                if job.get("cancel"):
                    raise JobCancelled()
                _labels, entry = await asyncio.to_thread(
                    _symbol_payload, model, candles, timeframe,
                    conf_min, min_hold, False, bounds.get(sym))
                la = entry.get("live_agreement") or {}
                for k in ("direction_pct", "holdout_direction_pct", "trend_hit_pct"):
                    if la.get(k) is not None:
                        agg[k].append(la[k])
                segs = entry.get("segments") or []
                lsegs = entry.get("live_segments") or []
                if segs:
                    agg["avg_final_days"].append(
                        sum(s["bars"] for s in segs) / len(segs) / max(bpd, 1e-9))
                if lsegs:
                    agg["avg_live_days"].append(
                        sum(s["bars"] for s in lsegs) / len(lsegs) / max(bpd, 1e-9))
                agg["switches_final"] += max(len(segs) - 1, 0)
                agg["switches_live"] += max(len(lsegs) - 1, 0)
            mean = lambda xs: (sum(xs) / len(xs)) if xs else None  # noqa: E731
            dur = mean(agg["avg_final_days"])
            hold = mean(agg["holdout_direction_pct"])
            # Score: Holdout-Trefferquote minus Strafe je Tag außerhalb des
            # 5-15-Tage-Zielbands (Phasendauer hat Priorität, dann Treffer).
            if dur is None:
                out_band, score = None, None
            else:
                out_band = max(0.0, t_lo - dur) + max(0.0, dur - t_hi)
                score = (hold or 0.0) - 4.0 * out_band
            rnd = lambda v, d=1: round(v, d) if v is not None else None  # noqa: E731
            rows.append({"thr": thr, "slope_days": slope,
                         "direction_pct": rnd(mean(agg["direction_pct"])),
                         "holdout_direction_pct": rnd(hold),
                         "trend_hit_pct": rnd(mean(agg["trend_hit_pct"])),
                         "avg_final_segment_days": rnd(dur),
                         "avg_live_segment_days": rnd(mean(agg["avg_live_days"])),
                         "switches_final": agg["switches_final"],
                         "switches_live": agg["switches_live"],
                         "in_target": (dur is not None and t_lo <= dur <= t_hi),
                         "score": rnd(score, 2)})
        scored = [r for r in rows if r.get("score") is not None]
        scored.sort(key=lambda r: r["score"], reverse=True)
        best = scored[0] if scored else None
        best_config = None
        if best:
            best_config = {**dict(body.get("engine_config") or {}),
                           "detector": "kombi", "kombi_thr": best["thr"],
                           "kombi_slope_days": best["slope_days"]}
        result = {"kind": "kombi_calibrate",
                  "rows": scored[:40] + [r for r in rows if r.get("score") is None],
                  "best": best, "best_config": best_config,
                  "target_min_days": t_lo, "target_max_days": t_hi,
                  "symbols": list(histories.keys()), "timeframe": timeframe,
                  "days": days, "train_pct": train_pct,
                  "combos": len(combos),
                  "created_at": datetime.now(timezone.utc).isoformat()}
        if db is not None:
            try:
                await db.regime_lab_runs.replace_one(
                    {"id": job_id}, {"id": job_id, "result": result,
                                     "created_at": result["created_at"]}, upsert=True)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"kombi_calibrate persist failed: {e}")
        job["result"] = result
        job["status"] = "done"
        job["progress"] = 100
        job["phase"] = "Fertig"
    except JobCancelled:
        job["status"] = "cancelled"
        job["phase"] = "Abgebrochen"
    except Exception as e:  # noqa: BLE001
        logger.exception(f"kombi calibrate {job_id} failed")
        job["status"] = "error"
        job["error"] = str(e)[:300]
        job["phase"] = "Fehler"


# ---------------- Wissenschaftliche Kalibrierung ----------------
async def run_calibration(job_id: str, body: Dict, db):
    """Kalibrierungs-Job: Referenz-Regime (zentriert/HMM) berechnen und die
    Engine-Parameter daran messen/optimieren (services.regime_truth)."""
    job = JOBS[job_id]
    try:
        from services import regime_truth as rt
        symbols = body.get("symbols") or []
        timeframe = body.get("timeframe") or "15m"
        days = int(min(max(int(body.get("days") or 360), 30), 5500))
        source = (body.get("truth_source") or "centered").lower()
        engine_config = dict(body.get("engine_config") or {})
        if body.get("regime_mode") is not None:
            engine_config["regime_mode"] = eng.norm_mode(body["regime_mode"])

        histories = await fetch_histories(symbols, days, timeframe, job)
        if not histories:
            raise RuntimeError("Zu wenig Daten für diesen Timeframe/Zeitraum")

        def stop():
            return bool(job.get("cancel"))

        def prog(pct, phase):
            job["progress"] = int(min(max(pct, 0), 99))
            job["phase"] = str(phase)[:200]

        report = await asyncio.to_thread(rt.calibrate, histories, timeframe,
                                         engine_config, source, stop, prog)
        if report is None:
            raise JobCancelled()
        job["result"] = {"kind": "calibration", "report": report}
        job["status"] = "done"
        job["progress"] = 100
        job["phase"] = "Fertig"
        if db is not None:
            try:
                await db.regime_calibrations.insert_one(
                    {"id": job_id, "created_at": datetime.now(timezone.utc).isoformat(),
                     "report": report})
            except Exception as e:  # noqa: BLE001
                logger.warning(f"calibration persist failed: {e}")
    except JobCancelled:
        job["status"] = "cancelled"
        job["phase"] = "Abgebrochen"
    except Exception as e:  # noqa: BLE001
        logger.exception(f"regime calibration {job_id} failed")
        job["status"] = "error"
        job["error"] = str(e)[:300]
        job["phase"] = "Fehler"


# ---------------- Regime-Übergangs-Matrix (Etappe 2) ----------------
def transition_matrix(doc: Dict, scope: str, symbol: Optional[str],
                      view: str) -> Dict:
    """Historische Übergangs-Wahrscheinlichkeiten zwischen Regimen aus den
    gespeicherten Abschnitten einer Analyse. view='final' nutzt die
    pivot-korrigierten Final-Abschnitte, view='live' die kausalen
    Live-Abschnitte (ohne Lookahead). Übergänge werden je Coin gezählt
    (nie über Coin-Grenzen verkettet)."""
    bpd = max(rg.bars_per_day(doc.get("timeframe") or "1h"), 1e-9)
    mode = eng.norm_mode(((doc.get("settings") or {}).get("regime_mode"))
                         or eng.DEFAULT_REGIME_MODE)
    key = "live_segments" if view == "live" else "segments"
    seg_lists: List[List[Dict]] = []
    if scope == "per_coin":
        entry = (doc.get("per_coin") or {}).get(symbol) or {}
        segs = entry.get(key) or entry.get("segments") or []
        if segs:
            seg_lists.append(segs)
    else:
        per_symbol = ((doc.get("combined") or {}).get("per_symbol")) or {}
        syms = [symbol] if symbol else sorted(per_symbol.keys())
        for s in syms:
            entry = per_symbol.get(s) or {}
            segs = entry.get(key) or entry.get("segments") or []
            if segs:
                seg_lists.append(segs)

    def _count(lists: List[List[Dict]]) -> Dict:
        cells: Dict = {}
        from_tot: Dict = {}
        for segs in lists:
            for a, b in zip(segs, segs[1:]):
                fk, tk = int(a["regime"]), int(b["regime"])
                c = cells.setdefault((fk, tk),
                                     {"count": 0, "from_days": [], "to_days": []})
                c["count"] += 1
                c["from_days"].append(a["bars"] / bpd)
                c["to_days"].append(b["bars"] / bpd)
                ft = from_tot.setdefault(fk, {"total": 0, "days": []})
                ft["total"] += 1
                ft["days"].append(a["bars"] / bpd)
        mean = lambda xs: round(sum(xs) / len(xs), 1) if xs else None  # noqa: E731
        matrix = [{"from": fk, "to": tk, "count": c["count"],
                   "prob_pct": round(c["count"] / from_tot[fk]["total"] * 100, 1),
                   "avg_from_days": mean(c["from_days"]),
                   "avg_to_days": mean(c["to_days"])}
                  for (fk, tk), c in sorted(cells.items())]
        per_from = [{"from": fk, "total": v["total"],
                     "avg_days": mean(v["days"]),
                     "top_next": max((m for m in matrix if m["from"] == fk),
                                     key=lambda m: m["count"])["to"]}
                    for fk, v in sorted(from_tot.items())]
        return {"matrix": matrix, "per_from": per_from,
                "total_transitions": sum(v["total"] for v in from_tot.values())}

    full = _count(seg_lists)

    # Richtungs-Ebene (Auf/Seitwärts/Ab): benachbarte Abschnitte gleicher
    # Richtung zusammenfassen, dann Übergänge zählen – beantwortet direkt
    # "was folgt historisch auf Seitwärts?".
    dir_lists: List[List[Dict]] = []
    for segs in seg_lists:
        merged: List[Dict] = []
        for s in segs:
            d = eng.split_id(int(s["regime"]), mode)[0]
            if merged and merged[-1]["regime"] == d:
                merged[-1]["bars"] += int(s["bars"])
            else:
                merged.append({"regime": d, "bars": int(s["bars"])})
        dir_lists.append(merged)
    direction = _count(dir_lists)

    # Kontext: aktuelles (= letztes) Regime, wenn genau EIN Coin betrachtet wird
    last = None
    if len(seg_lists) == 1 and seg_lists[0]:
        seg = seg_lists[0][-1]
        rid = int(seg["regime"])
        last = {"regime": rid, "label": eng.regime_label(rid, mode),
                "direction": eng.split_id(rid, mode)[0],
                "days": round(seg["bars"] / bpd, 1)}

    ids = sorted({m["from"] for m in full["matrix"]}
                 | {m["to"] for m in full["matrix"]})
    dir_labels = {0: "Abwärts", 1: "Seitwärts", 2: "Aufwärts"}
    return {"scope": scope, "symbol": symbol, "view": view, "mode": mode,
            "regimes": [{"id": r, "label": eng.regime_label(r, mode)}
                        for r in ids],
            "direction_labels": dir_labels,
            **full,
            "direction_matrix": direction["matrix"],
            "direction_per_from": direction["per_from"],
            "last": last,
            "note": ("Übergänge aus den gespeicherten Abschnitten dieser "
                     "Analyse – je Coin gezählt, nie über Coin-Grenzen. "
                     "view=live nutzt die kausalen Live-Abschnitte "
                     "(ohne Lookahead), view=final die rückwirkend "
                     "korrigierten Phasen.")}


# ---------------- Persistierung (auch für lokal berechnete Jobs) ----------------
async def persist_analysis(db, doc: Dict):
    await db.regime_analyses.replace_one({"id": doc["id"]}, doc, upsert=True)
    n = await db.regime_analyses.count_documents({})
    if n > MAX_ANALYSES:
        old = await db.regime_analyses.find().sort("created_at", 1) \
            .limit(n - MAX_ANALYSES).to_list(n)
        for o in old:
            await db.regime_analyses.delete_one({"id": o["id"]})


async def persist_worker_result(db, job_id: str, job: Dict):
    """Ergebnis eines auf dem lokalen Worker berechneten Regime-Lab-Jobs
    serverseitig speichern (der Worker hat keinen Datenbank-Zugriff)."""
    res = job.get("result") or {}
    kind = res.get("kind")
    if kind == "analysis" and res.get("analysis_doc"):
        await persist_analysis(db, res.pop("analysis_doc"))
    elif kind == "calibration":
        await db.regime_calibrations.insert_one(
            {"id": job_id, "created_at": datetime.now(timezone.utc).isoformat(),
             "report": res.get("report")})
    elif kind == "regime_opt":
        await db.regime_lab_runs.replace_one(
            {"id": job_id}, {"id": job_id, "result": res,
                             "created_at": res.get("created_at")}, upsert=True)
    elif kind == "walkforward":
        key = scope_key(res.get("scope") or "combined", res.get("symbol"))
        await db.regime_analyses.update_one(
            {"id": res.get("analysis_id")},
            {"$set": {f"walkforward.{key}":
                      {k: v for k, v in res.items() if k != "points"}}})


# ---------------- Wiederverwendung gespeicherter Analysen ----------------
def model_for(doc: Dict, scope: str, symbol: str = None) -> Optional[Dict]:
    if scope == "per_coin":
        return ((doc.get("per_coin") or {}).get(symbol) or {}).get("model")
    return ((doc.get("combined") or {}).get("model"))


def scope_key(scope: str, symbol: str = None) -> str:
    return f"per_coin:{symbol}" if scope == "per_coin" else "combined"


def regime_ranges(doc: Dict, scope: str, symbol: str, sym: str,
                  regime_id: int, only_train: bool = True) -> List[Dict]:
    """Gespeicherte Zeitbereiche eines Regimes für ein Symbol; optional auf den
    Trainingsteil geschnitten (der Holdout bleibt für den Walk-Forward unberührt)."""
    if scope == "per_coin":
        segs = ((doc.get("per_coin") or {}).get(symbol) or {}).get("segments") or []
    else:
        segs = (((doc.get("combined") or {}).get("per_symbol") or {})
                .get(sym) or {}).get("segments") or []
    train_end = (doc.get("bounds") or {}).get(sym, {}).get("train_end_ts")
    out = []
    for s in segs:
        if s["regime"] != regime_id:
            continue
        from_ts, to_ts = s["from_ts"], s["to_ts"]
        if only_train and train_end:
            if from_ts > train_end:
                continue
            to_ts = min(to_ts, train_end)
        out.append({"from_ts": from_ts, "to_ts": to_ts})
    return out


def segments_from_ranges(candles: List[Dict], ranges: List[Dict], regime_id: int,
                         warmup_bars: int) -> List[Dict]:
    """Zeitbereiche auf (beliebige, ggf. andere Timeframe-) Kerzen abbilden –
    inkl. Warmup-Vorlauf, damit Indikatoren korrekt anlaufen."""
    ts = [c["timestamp"] for c in candles]
    segs = []
    for r in ranges:
        s = bisect.bisect_left(ts, r["from_ts"])
        e = bisect.bisect_right(ts, r["to_ts"])
        if e - s < 10:
            continue
        w0 = max(s - warmup_bars, 0)
        segs.append({"regime": regime_id, "start_ts": candles[s]["timestamp"],
                     "candles": candles[w0:e], "n_bars": e - s})
    return segs
