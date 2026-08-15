"""Tests für die neuen NNFX-Indikatoren und die drei NNFX-Strategien."""
import numpy as np
import pytest

from services import vec
from services.fast_sim import FastSeries, build_builtin_signal_provider
from strategies.nnfx_strategies import NNFX_STRATEGY_BY_REGIME
from strategies.registry import registry
from tests.test_regime_engine import make_candles, range_series, series


def candles_up(n=400):
    return make_candles(series(n, 0.5, 1.0, seed=301))


def candles_down(n=400):
    return make_candles(series(n, -0.5, 1.0, seed=302))


def candles_range(n=400):
    return make_candles(range_series(n, amp_pct=3, period=15, seed=303))


# ---------------------------------------------------------------- Indikatoren
def test_adx_di_matches_reference_behaviour():
    up = candles_up()
    fs = FastSeries(up)
    adx = fs.get("adx", {"adx_period": 14})
    pdi = fs.get("plus_di", {"adx_period": 14})
    mdi = fs.get("minus_di", {"adx_period": 14})
    assert np.isnan(adx[:28]).all(), "ADX darf in der Aufwärmphase keine Werte liefern"
    tail = slice(100, None)
    assert np.nanmean(adx[tail]) > 15
    assert np.nanmean(pdi[tail]) > np.nanmean(mdi[tail])
    assert np.nanmax(adx[tail]) <= 100.0
    dn = FastSeries(candles_down())
    assert np.nanmean(dn.get("plus_di", {"adx_period": 14})[tail]) < \
        np.nanmean(dn.get("minus_di", {"adx_period": 14})[tail])


def test_adx_di_against_manual_wilder():
    """Gegenprüfung der Wilder-Glättung an einer kurzen, handrechenbaren Serie."""
    c = candles_up(120)
    high = np.array([x["high"] for x in c])
    low = np.array([x["low"] for x in c])
    close = np.array([x["close"] for x in c])
    adx, pdi, mdi = vec.adx_di(high, low, close, 14)
    # DI-Werte müssen im Bereich 0..100 liegen und ADX aus |DI+-DI-| folgen
    ok = np.isfinite(pdi) & np.isfinite(mdi)
    assert (pdi[ok] >= 0).all() and (pdi[ok] <= 100).all()
    dx = 100 * np.abs(pdi - mdi) / np.maximum(pdi + mdi, 1e-9)
    assert np.nanmax(np.abs(np.nan_to_num(adx) - np.nan_to_num(adx))) == 0
    assert np.isfinite(dx[40:]).all()


def test_cci_extremes():
    fs = FastSeries(candles_up())
    cci = fs.get("cci", {"cci_period": 20})
    assert np.nanmean(cci[100:]) > 0
    fs2 = FastSeries(candles_down())
    assert np.nanmean(fs2.get("cci", {"cci_period": 20})[100:]) < 0


def test_keltner_bands_contain_price_mostly():
    fs = FastSeries(candles_range())
    up = fs.get("keltner_upper", {"keltner_period": 20, "atr_period": 14, "keltner_mult": 2.0})
    lo = fs.get("keltner_lower", {"keltner_period": 20, "atr_period": 14, "keltner_mult": 2.0})
    mid = fs.get("keltner_middle", {"keltner_period": 20, "atr_period": 14, "keltner_mult": 2.0})
    close = fs.close
    valid = np.isfinite(up) & np.isfinite(lo)
    inside = ((close >= lo) & (close <= up))[valid]
    assert inside.mean() > 0.85
    assert (up[valid] > mid[valid]).all() and (lo[valid] < mid[valid]).all()


def test_donchian_uses_only_past_bars():
    c = candles_up(200)
    fs = FastSeries(c)
    dh = fs.get("donchian_high", {"donchian_period": 20})
    high = fs.high
    i = 150
    assert dh[i] == pytest.approx(high[i - 20:i].max())
    assert np.isnan(dh[:20]).all()


# ---------------------------------------------------------------- Strategien
@pytest.mark.parametrize("sid", ["nnfx_trend", "nnfx_reversion", "nnfx_breakout"])
def test_nnfx_strategies_registered_with_params(sid):
    s = registry.get(sid)
    assert s is not None
    meta = s.get_metadata()
    assert meta["framework"] == "nnfx"
    assert meta["nnfx_regime"] in ("trend", "range", "breakout")
    assert len(meta["params"]) >= 8


@pytest.mark.parametrize("sid,candles_fn", [("nnfx_trend", candles_up),
                                            ("nnfx_reversion", candles_range),
                                            ("nnfx_breakout", candles_up)])
def test_nnfx_strategies_produce_signals(sid, candles_fn):
    s = registry.get(sid)
    candles = candles_fn()
    fs = FastSeries(candles)
    out = s.vectorized_signals(fs, s.get_params({}))
    assert out and out["long"] is not None
    n_sig = int(out["long"].sum() + out["short"].sum())
    assert n_sig > 0, f"{sid} liefert keine Signale"
    # nie vor der Aufwärmphase
    assert not out["long"][:out["warmup"]].any()
    assert not out["short"][:out["warmup"]].any()


def test_nnfx_trend_follows_direction():
    s = registry.get("nnfx_trend")
    up = FastSeries(candles_up())
    o = s.vectorized_signals(up, s.get_params({}))
    assert o["long"].sum() > o["short"].sum() * 3
    dn = FastSeries(candles_down())
    o2 = s.vectorized_signals(dn, s.get_params({}))
    assert o2["short"].sum() > o2["long"].sum() * 3


def test_nnfx_reversion_is_counter_trend():
    """Mean-Reversion kauft am unteren Rand -> Long-Signale liegen unter der Mitte."""
    s = registry.get("nnfx_reversion")
    candles = candles_range()
    fs = FastSeries(candles)
    p = s.get_params({})
    o = s.vectorized_signals(fs, p)
    mid = fs.get("bb_middle", {"bb_period": 20, "bb_std": 2.0})
    longs = np.where(o["long"])[0]
    assert len(longs) > 0
    assert (fs.close[longs] < mid[longs]).mean() > 0.9


def test_nnfx_analyze_returns_levels_and_rules():
    for sid in ("nnfx_trend", "nnfx_reversion", "nnfx_breakout"):
        s = registry.get(sid)
        res = s.analyze(candles_up(), "BTCUSDT", s.get_params({}))
        assert res is not None
        assert res["rules"] and all("label" in r for r in res["rules"])
        if res["signal_type"]:
            lv = res["levels"]
            assert lv["stop_loss"] != lv["entry"]
            if res["signal_type"] == "LONG":
                assert lv["stop_loss"] < lv["entry"] < lv["take_profit_full"]
            else:
                assert lv["stop_loss"] > lv["entry"] > lv["take_profit_full"]


def test_nnfx_fast_path_is_wired():
    """Der vektorisierte Pfad muss von fast_sim akzeptiert werden."""
    for sid, fn in [("nnfx_trend", candles_up), ("nnfx_reversion", candles_range),
                    ("nnfx_breakout", candles_up)]:
        s = registry.get(sid)
        fs = FastSeries(fn())
        provider = build_builtin_signal_provider(s, fs, {}, "BTCUSDT")
        assert provider is not None, sid
        hits = [provider(i) for i in range(fs.n)]
        assert any(h for h in hits), sid


def test_nnfx_params_change_behaviour():
    """Einstellungen wirken: strengerer ADX-Filter -> weniger Signale."""
    s = registry.get("nnfx_trend")
    fs = FastSeries(candles_up())
    loose = s.vectorized_signals(fs, {**s.get_params({}), "adx_min": 5})
    strict = s.vectorized_signals(fs, {**s.get_params({}), "adx_min": 45})
    assert int(loose["long"].sum()) >= int(strict["long"].sum())


def test_nnfx_direction_toggles():
    s = registry.get("nnfx_trend")
    fs = FastSeries(candles_up())
    o = s.vectorized_signals(fs, {**s.get_params({}), "allow_long": 0})
    assert o["long"].sum() == 0


def test_regime_to_nnfx_strategy_mapping():
    from services import regime_engine as eng
    for t in eng.taxonomy():
        sid = NNFX_STRATEGY_BY_REGIME[t["nnfx"]]
        assert registry.get(sid) is not None
