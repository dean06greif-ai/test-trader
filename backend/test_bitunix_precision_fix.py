"""
Regression tests for the code-30027 fix:
  - `basePrecision` / `quotePrecision` are DECIMAL PLACES, not tick steps.
  - MARKET orders re-anchor SL/TP to the live mark price with a min-distance
    buffer so Bitunix cannot reject with "TP price must be greater than mark".
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.bitunix_trade import (  # noqa: E402
    BitunixTradeClient,
    _precision_to_step,
    _round_step,
    _round_step_up,
)


# ------------------------------------------------------------------ helpers
def _install_avax_meta(client: BitunixTradeClient) -> None:
    """Simulate what load_trading_pairs would produce for AVAXUSDT."""
    client._pairs_meta = {
        "AVAXUSDT": {
            "qty_step": _precision_to_step(0) if _precision_to_step(0) else 1.0,
            "price_tick": _precision_to_step(3),
            "min_qty": 1.0,
        }
    }
    # Bitunix returns basePrecision=0 (whole units) + minTradeVolume=1 for AVAX
    # -> qty_step should end up as 1.0.
    client._pairs_meta["AVAXUSDT"]["qty_step"] = 1.0
    client._valid_bitunix_symbols = {"AVAXUSDT"}


# ------------------------------------------------------------------ tests
def test_precision_to_step_decimals():
    assert _precision_to_step(3) == 0.001
    assert _precision_to_step(2) == 0.01
    assert _precision_to_step(1) == 0.1
    assert _precision_to_step(0) == 1.0
    assert _precision_to_step(None) == 0.0


def test_precision_to_step_passthrough_for_real_step():
    # Already a tick step -> return unchanged.
    assert _precision_to_step(0.001) == 0.001
    assert _precision_to_step(0.5) == 0.5


def test_round_step_preserves_precision():
    # The old code normalized 6.717 to "6" (dropping trailing zeros). Ensure
    # the string keeps the fractional digits the tick asks for.
    assert _round_step(6.716963, 0.001) == "6.716"
    assert _round_step(77.806977, 0.01) == "77.80"
    assert _round_step_up(6.716963, 0.001) == "6.717"


def test_fmt_price_directional_rounding_long_tp():
    """LONG TP must be rounded UP so the tick doesn't push it below the mark."""
    client = BitunixTradeClient()
    _install_avax_meta(client)
    # LONG TP for AVAX at 6.7009 should end up >= 6.701 (mark 6.702 requires >mark;
    # this test only checks rounding direction, not the min-gap logic).
    assert client._fmt_price("AVAXUSDT", 6.7009, "up") == "6.701"
    assert client._fmt_price("AVAXUSDT", 6.7009, "down") == "6.700"


def test_avax_tp_no_longer_collapses_to_six():
    """The exact failing scenario from the Telegram screenshot."""
    client = BitunixTradeClient()
    _install_avax_meta(client)
    tp_str = client._fmt_price("AVAXUSDT", 6.716963, "up")
    # OLD buggy code produced "6" because it treated quotePrecision=3 as step=3.0.
    assert float(tp_str) > 6.702, f"TP {tp_str} would be rejected by Bitunix"
    assert tp_str.startswith("6.71") or tp_str.startswith("6.72")


def test_sol_tp_no_longer_collapses_to_seventysix():
    client = BitunixTradeClient()
    client._pairs_meta = {"SOLUSDT": {"qty_step": 0.01, "price_tick": 0.01, "min_qty": 0.1}}
    client._valid_bitunix_symbols = {"SOLUSDT"}
    tp_str = client._fmt_price("SOLUSDT", 77.806977, "up")
    assert float(tp_str) > 77.59, f"TP {tp_str} would be rejected"
    assert tp_str in ("77.81", "77.82")


def test_qty_uses_min_trade_volume_for_avax():
    client = BitunixTradeClient()
    _install_avax_meta(client)
    # 149.16 AVAX should floor to 149 (min_qty=1).
    assert client._fmt_qty("AVAXUSDT", 149.16) == "149"


@pytest.mark.asyncio
async def test_get_mark_price_live():
    """Integration smoke test against Bitunix public API."""
    client = BitunixTradeClient()
    price = await client.get_mark_price("AVAXUSDT")
    assert price is None or price > 0


if __name__ == "__main__":
    # Allow direct invocation: `python test_bitunix_precision_fix.py`
    test_precision_to_step_decimals()
    test_precision_to_step_passthrough_for_real_step()
    test_round_step_preserves_precision()
    test_fmt_price_directional_rounding_long_tp()
    test_avax_tp_no_longer_collapses_to_six()
    test_sol_tp_no_longer_collapses_to_seventysix()
    test_qty_uses_min_trade_volume_for_avax()
    asyncio.run(test_get_mark_price_live())
    print("All precision-fix regression tests passed.")
