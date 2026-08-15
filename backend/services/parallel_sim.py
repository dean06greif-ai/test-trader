"""Multi-Core-Simulation über einen Prozess-Pool.

Aktiv nur, wenn SIM_WORKERS gesetzt ist (macht der lokale Worker; "0" = alle
Kerne, "N" = N Kerne). Auf dem Server bleibt SIM_WORKERS ungesetzt -> 1 ->
der bisherige sequenzielle Pfad läuft unverändert.

Kind-Prozesse erhalten die Kerzen-Daten EINMALIG (Linux/Mac: fork/Copy-on-
Write ohne Pickling, Windows: spawn + Initializer) und bauen Strategie +
Fast-Path-Provider selbst auf. Dadurch müssen keine unpicklebaren Closures
übertragen werden und die eigentliche Simulation ist exakt derselbe Code
(simulate_pair / optimizer._evaluate) wie im sequenziellen Pfad.
"""
import logging
import multiprocessing as mp
import os
from concurrent.futures import ProcessPoolExecutor
from typing import Dict, List

logger = logging.getLogger(__name__)

_DATA: Dict[str, List[Dict]] = {}  # key -> Kerzen (im Kind-Prozess verfügbar)
_FS: Dict[str, object] = {}        # key -> FastSeries-Cache (pro Kind-Prozess)


def workers_configured() -> int:
    """SIM_WORKERS: nicht gesetzt -> 1 (sequenziell, Server-Default),
    0 -> alle CPU-Kerne, N -> min(N, Kerne)."""
    raw = os.environ.get("SIM_WORKERS")
    if raw is None:
        return 1
    try:
        n = int(raw)
    except ValueError:
        return 1
    cores = os.cpu_count() or 1
    return cores if n <= 0 else min(n, cores)


def _init_spawn(data: Dict[str, List[Dict]]):
    global _DATA, _FS
    _DATA = data
    _FS = {}


def make_pool(data: Dict[str, List[Dict]], workers: int) -> ProcessPoolExecutor:
    """Pool erstellen. `data` sind alle Kerzen-Serien, die Tasks brauchen."""
    global _DATA, _FS
    _DATA = data  # fork-Kinder erben das per Copy-on-Write (kein Pickling)
    _FS = {}
    if "fork" in mp.get_all_start_methods():
        ctx = mp.get_context("fork")
        pool = ProcessPoolExecutor(max_workers=workers, mp_context=ctx)
    else:  # Windows: Daten einmalig pro Kind-Prozess übertragen
        ctx = mp.get_context("spawn")
        pool = ProcessPoolExecutor(max_workers=workers, mp_context=ctx,
                                   initializer=_init_spawn, initargs=(data,))
    logger.info(f"parallel_sim: Pool mit {workers} Prozessen gestartet "
                f"({sum(len(v) for v in data.values())} Kerzen, "
                f"{ctx.get_start_method()})")
    return pool


def close_pool(pool: ProcessPoolExecutor, kill: bool = False):
    """Pool schließen. kill=True (Abbruch): laufende Prozesse sofort beenden."""
    global _DATA, _FS
    try:
        pool.shutdown(wait=not kill, cancel_futures=True)
        if kill:
            for p in list(getattr(pool, "_processes", {}).values()):
                try:
                    p.terminate()
                except Exception:  # noqa: BLE001
                    pass
    except Exception as e:  # noqa: BLE001
        logger.warning(f"parallel_sim close: {e}")
    _DATA = {}
    _FS = {}


def strategy_spec(strategy) -> Dict:
    """Picklebare Beschreibung einer Strategie für den Kind-Prozess."""
    if getattr(strategy, "IS_CUSTOM", False):
        return {"definition": dict(strategy.definition)}
    return {"strategy_id": strategy.STRATEGY_ID}


def _strategy_from_spec(spec: Dict):
    if spec.get("definition") is not None:
        from strategies.custom_strategy import CustomStrategy
        return CustomStrategy(spec["definition"])
    from strategies.registry import registry
    strat = registry.get(spec.get("strategy_id"))
    if strat is None:
        raise RuntimeError(f"Strategie {spec.get('strategy_id')} nicht gefunden")
    return strat


def _fast(key: str):
    from services import fast_sim
    if key not in _FS:
        _FS[key] = fast_sim.FastSeries(_DATA[key])
    return _FS[key]


# ---------------- Tasks (laufen im Kind-Prozess) ----------------
def sim_pair_task(spec: Dict, key: str, symbol: str, settings: Dict, cfg: Dict,
                  collect_trades: bool, use_fast: bool) -> Dict:
    """Ein (Strategie, Symbol)-Paar simulieren (Backtester). Fehler werden als
    {"_error": ...} zurückgegeben, damit ein Paar-Fehler nicht den Job killt
    (wie im sequenziellen Pfad, wo check_signal-Fehler geschluckt werden)."""
    from services import fast_sim
    from services.backtester import simulate_pair
    try:
        strat = _strategy_from_spec(spec)
        candles = _DATA[key]
        provider = None
        if use_fast:
            try:
                provider = fast_sim.provider_for(strat, _fast(key), settings, symbol)
            except Exception as e:  # noqa: BLE001 – Fallback wie sequenziell
                logger.warning(f"fast_sim fallback (parallel) {spec}: {e}")
                provider = None
        return simulate_pair(strat, candles, symbol, settings, cfg, None,
                             collect_trades, None, provider)
    except Exception as e:  # noqa: BLE001
        return {"_error": str(e)[:300]}


def evaluate_task(spec: Dict, symbols: List[str], settings: Dict, cfg: Dict) -> Dict:
    """Eine Parameter-/Regel-Kombination über alle Symbole bewerten (Optimizer).
    Nutzt exakt optimizer._evaluate – identische Aggregation wie sequenziell."""
    from services.optimizer import _evaluate
    strat = _strategy_from_spec(spec)
    hist = {s: _DATA[s] for s in symbols}
    fs = {s: _fast(s) for s in symbols}
    return _evaluate(strat, hist, settings, cfg, fs, None)


def static_metrics_task(spec: Dict, keys: List[str], symbols: List[str],
                        settings: Dict, cfg: Dict, capital: float) -> Dict:
    """Statische Benchmark-Konfiguration über die gesamten Trainingsdaten
    bewerten (dynamischer Modus). Gibt nur Metriken zurück – die Trade-Listen
    bleiben im Kind-Prozess, damit nichts Großes übertragen wird."""
    from services import fast_sim
    from services.backtester import simulate_pair
    from services.dynamic_strategy import metrics_from_rows
    strat = _strategy_from_spec(spec)
    rows = []
    for key, sym in zip(keys, symbols):
        provider = None
        try:
            provider = fast_sim.provider_for(strat, _fast(key), settings, sym)
        except Exception:  # noqa: BLE001
            provider = None
        res = simulate_pair(strat, _DATA[key], sym, settings, cfg, None, True,
                            None, provider)
        rows.extend(res.get("all_trades") or [])
    return metrics_from_rows(rows, capital)


def sim_segment_task(spec: Dict, key: str, symbol: str, settings: Dict, cfg: Dict,
                     start_iso: str, use_fast: bool = True) -> List[Dict]:
    """Einen Regime-Abschnitt simulieren (dynamischer Modus). Es werden nur Trades
    zurückgegeben, die IM Abschnitt geöffnet wurden (Warmup verworfen) – identisch
    zu dynamic_strategy.simulate_segment, nur im Kind-Prozess."""
    from services import fast_sim
    from services.backtester import simulate_pair
    try:
        strat = _strategy_from_spec(spec)
        candles = _DATA[key]
        provider = None
        if use_fast:
            try:
                provider = fast_sim.provider_for(strat, _fast(key), settings, symbol)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"fast_sim fallback (segment) {key}: {e}")
                provider = None
        res = simulate_pair(strat, candles, symbol, settings, cfg, None, True,
                            None, provider)
        return [t for t in (res.get("all_trades") or [])
                if (t.get("opened") or "") >= start_iso]
    except Exception as e:  # noqa: BLE001
        logger.warning(f"sim_segment_task {key}: {e}")
        return []


# ---- Timed-Varianten für die Benchmark-Statistik (gleiche Logik + Dauer) ----
def sim_segment_task_timed(*args):
    import time
    t0 = time.perf_counter()
    rows = sim_segment_task(*args)
    return rows, time.perf_counter() - t0


def sim_pair_task_timed(*args):
    import time
    t0 = time.perf_counter()
    res = sim_pair_task(*args)
    return res, time.perf_counter() - t0


def evaluate_task_timed(*args):
    import time
    t0 = time.perf_counter()
    m = evaluate_task(*args)
    return m, time.perf_counter() - t0
