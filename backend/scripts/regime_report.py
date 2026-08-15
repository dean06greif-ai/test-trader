"""Übersicht der Regime-Erkennung über alle Szenarien (Diagnose-Skript).
Aufruf: python -m scripts.regime_report  (aus /app/backend)"""
import sys

sys.path.insert(0, ".")

from services import regime_engine as eng  # noqa: E402
from tests.regime_scenarios import scenarios  # noqa: E402


def share(labels):
    vals = [x for x in labels if x is not None]
    n = max(len(vals), 1)
    return {k: round(sum(1 for v in vals if eng.split_id(v)[0] == i) / n, 2)
            for i, k in enumerate(["down", "side", "up"])}


def main(config=None):
    print(f"{'Szenario':16} {'erwartet':8} {'down/side/up':22} "
          f"{'Segm.':6} {'ØTage':7} {'Viol%':6} {'ok'}")
    bad = 0
    for name, (candles, expect) in scenarios().items():
        model = eng.build_model({"X": candles}, "24h", config)
        labels = eng.classify_series(model, candles)
        rep = eng.validate_labels(candles, labels, model)
        sh = share(labels)
        ok = rep["passed"] and (expect is None or sh[expect] >= 0.55)
        bad += 0 if ok else 1
        print(f"{name:16} {str(expect):8} "
              f"{str([sh['down'], sh['side'], sh['up']]):22} "
              f"{rep['segments']:6} {rep['avg_segment_days']:7} "
              f"{rep['violation_bars_pct']:6} {'OK' if ok else 'FEHLER'}")
    print(f"-> {bad} Szenario(en) mit Problemen")
    return bad


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
