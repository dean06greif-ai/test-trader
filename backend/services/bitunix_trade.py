"""
Bitunix futures trading: request signer, live REST client, paper broker,
and an AutoTradeManager that opens/manages auto-trades with dynamic SL/TP,
partial TP1 and break-even logic.

Fix summary (Bitunix Live-Trading):
- Root cause: The app used the internal short names GOLD / SILVER / OIL as
  order symbols. Bitunix does not know these symbols and rejected the order
  with code 300105 "System error".
  The real Bitunix USDT-M futures contracts are:
      GOLD   -> XAUUSDT
      SILVER -> XAGUSDT
      OIL    -> CLUSDT
  Crypto symbols like BTCUSDT / XRPUSDT already match.
- Every private call to Bitunix (place_order, flash_close, set_leverage,
  get_positions) now translates the internal symbol via `to_bitunix_symbol()`.
- The mapping is validated at startup against
  GET /api/v1/futures/market/trading_pairs so future contract-name changes
  on Bitunix don't break us silently.
- Second fix: `on_signal` no longer stores the trade locally when the live
  order was rejected. Instead a Telegram alert is emitted and the trade is
  dropped. That prevents "ghost positions" that never existed on Bitunix.
- qty/price are sent as strings, rounded to the contract's step/tick size
  when the metadata is available.
"""
import asyncio
import os
import time
import json
import hashlib
import logging
import aiohttp
import uuid
from decimal import Decimal, ROUND_DOWN, ROUND_UP, ROUND_HALF_UP
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional
from core import instruments as _instruments
from services.technical_indicators import TechnicalIndicators
from services.backtester import effective_leverage

logger = logging.getLogger(__name__)


def fee_guard_min_sl_pct(fee_percent: float, mult: float) -> float:
    """Mindest-SL-Distanz in % = mult × Roundtrip-Fees (2 × fee_percent)."""
    return max(0.0, float(mult)) * 2.0 * max(0.0, float(fee_percent))


def fee_guard_check(ai_cfg: Dict, cfg: Dict, entry: float, sl: float):
    """Fee-Wächter (KI-Trader): blockt Trades, deren SL-Distanz unter dem
    Vielfachen der Roundtrip-Fees liegt – mathematisch garantierte
    Fee-Verlierer. Liefert (ok, grund)."""
    ai_cfg = ai_cfg or {}
    if not ai_cfg.get("fee_guard_enabled", True):
        return True, ""
    try:
        mult = float(ai_cfg.get("fee_guard_mult", 4.0) or 0)
    except (TypeError, ValueError):
        mult = 4.0
    if mult <= 0 or not entry or not sl:
        return True, ""
    fee = float((cfg or {}).get("fee_percent", 0.06) or 0.06)
    min_pct = fee_guard_min_sl_pct(fee, mult)
    sl_dist_pct = abs(float(entry) - float(sl)) / float(entry) * 100.0
    if sl_dist_pct + 1e-9 < min_pct:
        return False, (
            f"Fee-Wächter: SL-Distanz {sl_dist_pct:.3f}% < Minimum {min_pct:.2f}% "
            f"({mult:g}× Roundtrip-Fees {2 * fee:.2f}%) – Gebühren würden das "
            f"geplante Risiko auffressen (abschaltbar im KI-Setup)")
    return True, ""


# ---------------------------------------------------------------------------
# Symbol mapping: internal display name -> real Bitunix contract symbol.
# Quelle: core.instruments (dort werden neue Assets gepflegt). Symbole, die
# bereits dem Bitunix-Kontrakt entsprechen (BTCUSDT, QQQUSDT, ...), gehen
# unverändert durch; Instrumente ohne Kontrakt (Forex) sind nicht live handelbar.
# ---------------------------------------------------------------------------
SYMBOL_MAP: Dict[str, str] = dict(_instruments.SYMBOL_MAP)


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _nonce() -> str:
    return os.urandom(16).hex()


def _millis() -> str:
    return str(int(time.time() * 1000))


def sign_request(api_key: str, secret: str, query: Optional[Dict], body_str: str,
                 nonce: str, ts: str) -> str:
    qp = ""
    if query:
        qp = "".join(f"{k}{query[k]}" for k in sorted(query.keys()))
    digest = _sha256(nonce + ts + api_key + qp + body_str)
    return _sha256(digest + secret)


def _round_step(value: float, step: float, rounding=ROUND_DOWN) -> str:
    """Round `value` to a multiple of `step` and return a plain string
    (no scientific notation, no trailing zeros beyond the step precision).
    Default rounding is DOWN (used for quantities). For prices we sometimes
    need HALF_EVEN / to-tick alignment; pass a different `rounding` in that
    case."""
    if step <= 0:
        return f"{value}"
    d_val = Decimal(str(value))
    d_step = Decimal(str(step))
    quant = (d_val / d_step).to_integral_value(rounding=rounding) * d_step
    # Preserve step precision explicitly – .normalize() drops trailing zeros
    # which Bitunix sometimes rejects (e.g. "1.11" instead of "1.1100").
    step_exp = d_step.normalize().as_tuple().exponent
    if step_exp < 0:
        quant = quant.quantize(Decimal(10) ** step_exp)
    s = format(quant, "f")
    return s if s else "0"


def _precision_to_step(prec) -> float:
    """Bitunix returns basePrecision / quotePrecision as *decimal places*
    (e.g. 3 -> 0.001). If the value already looks like a step (e.g. 0.001)
    we pass it through. Handles ints, floats and strings safely."""
    if prec is None or prec == "":
        return 0.0
    try:
        f = float(prec)
    except (TypeError, ValueError):
        return 0.0
    if f <= 0:
        return 0.0
    # Heuristic: an integer >= 1 is a decimal-place count, anything < 1 is
    # already a step size.
    if f >= 1 and float(int(f)) == f:
        return float(Decimal(10) ** Decimal(-int(f)))
    return f


class BitunixTradeClient:
    """Live Bitunix USDT-M futures client (signed private endpoints).

    Owns the symbol translation layer + a cache of contract metadata
    (step size, tick size, min qty) loaded from the public
    /api/v1/futures/market/trading_pairs endpoint.
    """

    def __init__(self):
        # Support both naming conventions (Render uses BITUNIX_SECRET_KEY)
        self.api_key = os.getenv("BITUNIX_API_KEY") or os.getenv("BITUNIX_KEY", "")
        self.secret = (os.getenv("BITUNIX_API_SECRET")
                       or os.getenv("BITUNIX_SECRET_KEY")
                       or os.getenv("BITUNIX_SECRET", ""))
        self.base = os.getenv("BITUNIX_BASE_URL", "https://fapi.bitunix.com").rstrip("/")

        # contract metadata: bitunix_symbol -> {"qty_step", "price_tick", "min_qty"}
        self._pairs_meta: Dict[str, Dict[str, float]] = {}
        self._valid_bitunix_symbols: set = set()

    def configured(self) -> bool:
        return bool(self.api_key and self.secret)

    # --------------------- symbol translation ----------------------------
    def to_bitunix_symbol(self, internal: str) -> str:
        """Translate the app-internal symbol (e.g. GOLD) to the real Bitunix
        contract symbol (e.g. XAUUSDT). Crypto symbols pass through unchanged."""
        if not internal:
            return internal
        s = internal.upper()
        mapped = SYMBOL_MAP.get(s, s)
        # If we already know the pair catalogue and the mapped symbol isn't in
        # it, log a warning so the mismatch shows up in the logs instead of
        # silently ending up as code 300105 "System error".
        if self._valid_bitunix_symbols and mapped not in self._valid_bitunix_symbols:
            logger.warning(
                f"Bitunix symbol '{mapped}' (from internal '{internal}') is not "
                "listed in trading_pairs; order will likely be rejected."
            )
        return mapped

    def contract_meta(self, bitunix_symbol: str) -> Dict[str, float]:
        return self._pairs_meta.get(bitunix_symbol, {})

    async def load_trading_pairs(self) -> None:
        """Load the public trading-pair catalogue and cache step/tick/min-qty.
        Called once at startup; safe to re-call. Never raises to the caller."""
        url = f"{self.base}/api/v1/futures/market/trading_pairs"
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    payload = await r.json()
        except Exception as e:
            logger.error(f"load_trading_pairs failed: {e}")
            return

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            logger.warning(f"trading_pairs unexpected payload: {str(payload)[:200]}")
            return

        meta: Dict[str, Dict[str, float]] = {}
        valid: set = set()
        for row in data:
            sym = row.get("symbol")
            if not sym:
                continue
            valid.add(sym)
            try:
                meta[sym] = {
                    # basePrecision / quotePrecision are DECIMAL PLACES on
                    # Bitunix, not step sizes. Convert them properly.
                    "qty_step": _precision_to_step(row.get("basePrecision")),
                    "price_tick": _precision_to_step(
                        row.get("quotePrecision") or row.get("pricePrecision")
                    ),
                    "min_qty": float(row.get("minTradeVolume") or 0) or 0.0,
                }
            except (TypeError, ValueError):
                meta[sym] = {}
        self._pairs_meta = meta
        self._valid_bitunix_symbols = valid
        logger.info(f"Bitunix trading_pairs cached: {len(valid)} symbols")

        # Sanity check the internal -> bitunix mapping now that we have data.
        for internal, mapped in SYMBOL_MAP.items():
            if mapped not in valid:
                logger.error(
                    f"Symbol mapping mismatch: internal '{internal}' -> '{mapped}' "
                    "is NOT a valid Bitunix contract. Live orders will fail."
                )

    # --------------------- signed transport ------------------------------
    async def _post(self, path: str, body: Dict) -> Dict:
        body_str = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        nonce, ts = _nonce(), _millis()
        sign = sign_request(self.api_key, self.secret, None, body_str, nonce, ts)
        headers = {"api-key": self.api_key, "nonce": nonce, "timestamp": ts,
                   "sign": sign, "language": "en-US", "Content-Type": "application/json"}
        async with aiohttp.ClientSession() as s:
            async with s.post(self.base + path, data=body_str, headers=headers,
                              timeout=aiohttp.ClientTimeout(total=15)) as r:
                txt = await r.text()
                try:
                    return json.loads(txt)
                except Exception:
                    return {"code": r.status, "msg": txt[:200]}

    async def _get(self, path: str, query: Dict = None) -> Dict:
        query = query or {}
        nonce, ts = _nonce(), _millis()
        sign = sign_request(self.api_key, self.secret, query, "", nonce, ts)
        headers = {"api-key": self.api_key, "nonce": nonce, "timestamp": ts,
                   "sign": sign, "language": "en-US"}
        async with aiohttp.ClientSession() as s:
            async with s.get(self.base + path, params=query, headers=headers,
                             timeout=aiohttp.ClientTimeout(total=15)) as r:
                txt = await r.text()
                try:
                    return json.loads(txt)
                except Exception:
                    return {"code": r.status, "msg": txt[:200]}

    # --------------------- public API ------------------------------------
    def _fmt_qty(self, bitunix_symbol: str, qty: float) -> str:
        m = self._pairs_meta.get(bitunix_symbol) or {}
        step = m.get("qty_step") or 0.0
        min_qty = m.get("min_qty") or 0.0
        # If the raw qty is already below the exchange minimum, bump it up to
        # the minimum so we don't get code 30016 for a rounded-to-zero amount.
        if min_qty > 0 and qty < min_qty:
            qty = min_qty
        if step > 0:
            rounded = _round_step(qty, step, ROUND_DOWN)
            # After rounding down we might dip below min_qty again; round up
            # to the nearest step in that case.
            try:
                if min_qty > 0 and float(rounded) < min_qty:
                    rounded = _round_step(min_qty, step, ROUND_UP)
            except ValueError:
                pass
            return rounded
        return f"{qty}"

    def _fmt_price(self, bitunix_symbol: str, price: float,
                   direction: str = "nearest") -> str:
        """direction:
            "nearest" -> half-up (default, LIMIT entry)
            "up"      -> ROUND_UP  (LONG TP / SHORT SL – keep away from mark)
            "down"    -> ROUND_DOWN (LONG SL / SHORT TP – keep away from mark)
        """
        m = self._pairs_meta.get(bitunix_symbol) or {}
        tick = m.get("price_tick") or 0.0
        if tick <= 0:
            return f"{price}"
        mode = {"up": ROUND_UP, "down": ROUND_DOWN}.get(direction, ROUND_HALF_UP)
        return _round_step(price, tick, mode)

    async def place_order(self, symbol, side, qty, order_type="MARKET", price=None,
                          tp_price=None, sl_price=None, reduce_only=False):
        b_symbol = self.to_bitunix_symbol(symbol)
        # Direction-aware rounding for TP/SL. For LONG (BUY):
        #   * TP must be ABOVE mark, so round UP to the next tick.
        #   * SL must be BELOW mark, so round DOWN.
        # For SHORT (SELL) it's the opposite. This prevents Bitunix code
        # 30027 ("TP price must be greater than mark price") caused by
        # rounding a marginal TP down onto/below the mark.
        is_long = str(side).upper() in ("BUY", "LONG")
        tp_dir = "up" if is_long else "down"
        sl_dir = "down" if is_long else "up"
        body = {"symbol": b_symbol, "qty": self._fmt_qty(b_symbol, qty), "side": side,
                "tradeSide": "OPEN", "orderType": order_type}
        if order_type == "LIMIT" and price:
            body["price"] = self._fmt_price(b_symbol, price)
        if tp_price:
            body.update({"tpPrice": self._fmt_price(b_symbol, tp_price, tp_dir),
                         "tpStopType": "MARK_PRICE", "tpOrderType": "MARKET"})
        if sl_price:
            body.update({"slPrice": self._fmt_price(b_symbol, sl_price, sl_dir),
                         "slStopType": "MARK_PRICE", "slOrderType": "MARKET"})
        if reduce_only:
            body["reduceOnly"] = True
        return await self._post("/api/v1/futures/trade/place_order", body)

    async def flash_close(self, symbol, position_id, side, qty):
        b_symbol = self.to_bitunix_symbol(symbol)
        order_side = "SELL" if side == "LONG" else "BUY"
        body = {"symbol": b_symbol, "qty": self._fmt_qty(b_symbol, qty),
                "side": order_side, "tradeSide": "CLOSE", "orderType": "MARKET",
                "positionId": position_id, "reduceOnly": True}
        return await self._post("/api/v1/futures/trade/place_order", body)

    async def place_position_tp_sl(self, symbol, position_id, side,
                                    tp_price=None, tp_qty=None,
                                    sl_price=None, sl_qty=None):
        """Attach a (partial) TP and/or SL to an existing position via
        Bitunix `/api/v1/futures/tpsl/place_order`. Used for:
          * placing TP1 as a real reduce-only partial order right after entry
          * moving SL to break-even when TP1 fills
        `side` is the POSITION side ("LONG"/"SHORT") – rounding direction is
        derived from it so ticks never push TP under or SL above the mark.
        Returns the raw exchange response."""
        b_symbol = self.to_bitunix_symbol(symbol)
        is_long = str(side).upper() == "LONG"
        body: Dict = {"symbol": b_symbol, "positionId": position_id}
        if tp_price:
            body["tpPrice"] = self._fmt_price(b_symbol, tp_price,
                                              "up" if is_long else "down")
            body["tpStopType"] = "MARK_PRICE"
            body["tpOrderType"] = "MARKET"
            if tp_qty is not None:
                body["tpQty"] = self._fmt_qty(b_symbol, tp_qty)
        if sl_price:
            body["slPrice"] = self._fmt_price(b_symbol, sl_price,
                                              "down" if is_long else "up")
            body["slStopType"] = "MARK_PRICE"
            body["slOrderType"] = "MARKET"
            if sl_qty is not None:
                body["slQty"] = self._fmt_qty(b_symbol, sl_qty)
        return await self._post("/api/v1/futures/tpsl/place_order", body)

    async def modify_position_tp_sl(self, symbol, position_id,
                                     tp_price=None, sl_price=None, side="LONG"):
        """Position-TP/SL ändern (z.B. SL -> Break-Even).

        BUGFIX (Bug-Report 'Please set at least one of TP/Stop Loss'):
        Vorher wurde ein falscher Endpoint mit 'orderId' aufgerufen. Laut
        Bitunix-Doku ist es POST /api/v1/futures/tpsl/position/modify_order
        mit Pflichtfeld 'positionId' – deshalb wurden ALLE SL/TP-Anpassungen
        an der Börse abgelehnt."""
        b_symbol = self.to_bitunix_symbol(symbol)
        is_long = str(side).upper() == "LONG"
        body: Dict = {"symbol": b_symbol, "positionId": str(position_id)}
        if tp_price:
            body["tpPrice"] = self._fmt_price(b_symbol, tp_price,
                                              "up" if is_long else "down")
            body["tpStopType"] = "MARK_PRICE"
        if sl_price:
            body["slPrice"] = self._fmt_price(b_symbol, sl_price,
                                              "down" if is_long else "up")
            body["slStopType"] = "MARK_PRICE"
        return await self._post("/api/v1/futures/tpsl/position/modify_order", body)

    async def get_pending_tpsl(self, symbol, position_id=None):
        """Offene TP/SL-Orders (optional je Position) abfragen."""
        q: Dict = {"symbol": self.to_bitunix_symbol(symbol)}
        if position_id:
            q["positionId"] = str(position_id)
        return await self._get("/api/v1/futures/tpsl/get_pending_orders", q)

    async def cancel_tpsl_order(self, symbol, order_id):
        """Eine offene TP/SL-Order stornieren."""
        return await self._post("/api/v1/futures/tpsl/cancel_order",
                                {"symbol": self.to_bitunix_symbol(symbol),
                                 "orderId": str(order_id)})

    async def adjust_position_margin(self, symbol, amount: float,
                                     position_id: Optional[str] = None,
                                     side: Optional[str] = None):
        """Margin einer ISOLIERTEN Position anpassen.
        amount > 0 = Margin hinzufügen, amount < 0 = Margin entnehmen.
        Bitunix: POST /api/v1/futures/account/adjust_position_margin"""
        b_symbol = self.to_bitunix_symbol(symbol)
        body: Dict = {"symbol": b_symbol, "marginCoin": "USDT",
                      "amount": f"{float(amount):.6f}".rstrip("0").rstrip(".")}
        if position_id:
            body["positionId"] = position_id
        elif side:
            body["side"] = str(side).upper()
        return await self._post("/api/v1/futures/account/adjust_position_margin", body)

    async def set_leverage(self, symbol, leverage, margin_mode="ISOLATION"):
        b_symbol = self.to_bitunix_symbol(symbol)
        return await self._post("/api/v1/futures/account/change_leverage",
                                {"symbol": b_symbol, "leverage": int(leverage),
                                 "marginCoin": "USDT"})

    async def get_positions(self, symbol=None):
        q = {"symbol": self.to_bitunix_symbol(symbol)} if symbol else {}
        return await self._get("/api/v1/futures/position/get_pending_positions", q)

    async def get_history_positions(self, symbol=None, position_id=None, limit=20):
        """Geschlossene Positionen (echter closePrice/realizedPNL der Börse)."""
        q = {"limit": int(limit)}
        if symbol:
            q["symbol"] = self.to_bitunix_symbol(symbol)
        if position_id:
            q["positionId"] = str(position_id)
        return await self._get("/api/v1/futures/position/get_history_positions", q)

    async def resolve_position_id(self, symbol: str, side: str) -> Optional[str]:
        """Poll get_positions to find the positionId matching an open position.
        Bitunix's place_order response only returns orderId, not positionId,
        so we fetch it separately to attach TP1 / modify SL later.
        Returns None if the position cannot be found."""
        try:
            res = await self.get_positions(symbol)
        except Exception as e:
            logger.warning(f"resolve_position_id({symbol}) failed: {e}")
            return None
        data = res.get("data") if isinstance(res, dict) else None
        rows = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
        want = str(side).upper()
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_side = str(row.get("side") or row.get("positionSide") or "").upper()
            # Bitunix returns "BUY"/"SELL" for side and/or LONG/SHORT for positionSide
            if row_side in ("BUY", "LONG") and want != "LONG":
                continue
            if row_side in ("SELL", "SHORT") and want != "SHORT":
                continue
            pid = row.get("positionId") or row.get("id")
            if pid:
                return str(pid)
        return None

    async def get_balance(self):
        return await self._get("/api/v1/futures/account", {"marginCoin": "USDT"})

    async def get_mark_price(self, symbol: str) -> Optional[float]:
        """Public endpoint: latest mark price for a Bitunix futures symbol.
        Returns None on any failure – caller should degrade gracefully."""
        b_symbol = self.to_bitunix_symbol(symbol)
        url = f"{self.base}/api/v1/futures/market/tickers"
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, params={"symbols": b_symbol},
                                 timeout=aiohttp.ClientTimeout(total=8)) as r:
                    payload = await r.json()
        except Exception as e:
            logger.warning(f"get_mark_price failed for {b_symbol}: {e}")
            return None
        data = payload.get("data") if isinstance(payload, dict) else None
        row = None
        if isinstance(data, list) and data:
            row = data[0]
        elif isinstance(data, dict):
            row = data
        if not isinstance(row, dict):
            return None
        for key in ("markPrice", "mark_price", "lastPrice", "last", "close"):
            v = row.get(key)
            if v is None:
                continue
            try:
                f = float(v)
                if f > 0:
                    return f
            except (TypeError, ValueError):
                continue
        return None


DEFAULT_CAPITAL_ALLOCATION = {
    "live": {"mode": "full", "value": 0.0},
    "paper": {"mode": "full", "value": 0.0, "base_balance": 1000.0},
}


DEFAULT_COIN_CFG = {
    "enabled": False,
    "max_capital": 100.0,
    "leverage": 10,
    "margin_mode": "ISOLATION",
    "order_type": "MARKET",
    "sl_mode": "structure",       # structure | fixed | atr
    "sl_fixed_percent": 1.0,
    "sl_ticks": 4,
    "sl_lookback": 10,
    "atr_period": 14,
    "atr_sl_multiplier": 1.2,     # ATR buffer beyond structure (anti stop-hunt)
    "tp1_crv": 1.0,
    "tp1_close_percent": 50,
    "tp_full_crv": 2.0,
    "breakeven_enabled": True,
    # Break-Even Modus: "tp1" | "crv" | "profit_pct" | "smart" | "off"
    "be_mode": "tp1",
    "be_trigger_crv": 1.0,           # bei be_mode=crv: BE ab X R Gewinn
    "be_trigger_profit_pct": 30.0,   # bei be_mode=profit_pct: BE ab X% Gewinn auf Marge
    "be_smart_lookback": 10,         # bei be_mode=smart: Swing-Lookback
    "require_all_rules": False,      # nur traden wenn ALLE Regeln erfüllt sind
    "trail_after_tp1": True,      # ATR trailing stop after TP1 -> let winners run
    "trail_atr_mult": 1.5,
    "fee_percent": 0.06,
    "trade_pre_signals": False,
    # --- Auto-Leverage: Hebel automatisch aus SL-Abstand berechnen ---
    "auto_leverage_enabled": False,
    "auto_lev_mode": "liq_pct",      # liq_pct | liq_ticks
    "auto_lev_value": 0.5,           # % bzw. Ticks hinter dem Stop
    "auto_lev_max": 50,              # maximaler Hebel
    # --- Gewinnsicherung: SL in den Gewinn ziehen + Marge freisetzen ---
    "profit_secure_enabled": False,
    "profit_secure_trigger_pct": 30.0,   # ab X% Gewinn auf die Marge
    "profit_lock_pct": 50.0,             # X% des aktuellen Gewinns absichern
    # --- Bitunix live-order safety (fix for codes 30016 / 30027) ---
    # Minimum absolute distance (percent of mark price) that TP/SL must keep
    # away from the current mark price when the order hits the exchange.
    "min_tp_distance_percent": 0.15,
    # Floor for the risk-per-trade so TP/SL never end up microscopic
    # (percent of entry price). Prevents the classic "0.07%" reject case.
    "min_risk_percent": 0.25,
    # --- Take-Profit Modus: "crv" (dynamisch, R-Vielfache) | "fixed_pct" | "structure" ---
    "tp_mode": "crv",
    "tp1_percent": 0.5,       # bei tp_mode=fixed_pct: TP1-Abstand % vom Entry
    "tp_full_percent": 1.0,   # bei tp_mode=fixed_pct: Full-TP-Abstand % vom Entry
    # --- Liquidation (Isolated Margin) ---
    "maintenance_margin_rate": 0.5,  # % - bestimmt Liquidationspreis (~1/Hebel - MMR)
}


def parse_closed_position(res, position_id) -> Optional[Dict]:
    """Echten Abschluss einer Position aus get_history_positions ziehen (rein, testbar).

    Netto-PnL laut Doku = realizedPNL (ohne Fees/Funding) − fee + funding.
    Bug-Report (ETH 15.08): Die Bitunix-API liefert realizedPNL in der Praxis
    teils BEREITS inkl. Fees – die Fee würde dann doppelt abgezogen (−8,59 $
    wurde als −12,58 $ verbucht). Erkennung über den reinen Preis-PnL
    (entryPrice/closePrice/qty): Liegt realizedPNL näher an (Preis-PnL − fee)
    als am Preis-PnL selbst, sind die Fees schon enthalten."""
    if not isinstance(res, dict) or res.get("code") != 0:
        return None
    data = res.get("data") or {}
    items = data.get("positionList") if isinstance(data, dict) else data
    for p in items or []:
        if not isinstance(p, dict) or str(p.get("positionId")) != str(position_id):
            continue
        try:
            gross = float(p["realizedPNL"])
            fee = abs(float(p.get("fee") or 0))
            funding = float(p.get("funding") or 0)
            close = float(p.get("closePrice") or 0)
            max_qty = float(p.get("maxQty") or 0)
        except (KeyError, TypeError, ValueError):
            return None
        net = gross - fee + funding
        fee_included = False
        try:
            entry = float(p.get("entryPrice") or 0)
            side = str(p.get("side") or "").upper()
        except (TypeError, ValueError):
            entry, side = 0.0, ""
        if fee > 0 and entry > 0 and close > 0 and max_qty > 0 \
                and side in ("BUY", "SELL", "LONG", "SHORT"):
            sign = 1.0 if side in ("BUY", "LONG") else -1.0
            price_pnl = (close - entry) * max_qty * sign
            if abs(gross - (price_pnl - fee)) < abs(gross - price_pnl):
                net = gross + funding
                fee_included = True
        return {"exit_price": close if close > 0 else None,
                "net_pnl": round(net, 6),
                "gross_pnl": round(gross, 6), "fee": round(fee, 6),
                "fee_included_in_pnl": fee_included,
                "funding": round(funding, 6), "max_qty": max_qty}
    return None


def _extract_order_id(res) -> Optional[str]:
    """orderId aus einer Bitunix-Antwort ziehen (Feldname variiert)."""
    if not isinstance(res, dict):
        return None
    data = res.get("data")
    for src in (data if isinstance(data, dict) else {}, res):
        for key in ("orderId", "order_id", "id"):
            if src.get(key):
                return str(src[key])
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return _extract_order_id({"data": data[0]})
    return None


class AutoTradeManager:
    """
    Opens & manages auto-trades. In paper mode everything is simulated in Mongo.
    In live mode it calls Bitunix. Dynamic SL/TP, partial TP1 + break-even.
    """

    def __init__(self, client: BitunixTradeClient):
        self.client = client
        self.db = None
        self.telegram = None  # optional TelegramNotifier for reject alerts
        self.config = {"mode": "paper", "coins": {}}
        self._last_pos_sync = 0.0  # Throttle für den Bitunix-Positions-Abgleich

    def set_db(self, db):
        self.db = db

    def set_telegram(self, telegram):
        self.telegram = telegram

    def set_config(self, config: Dict):
        self.config = {
            "mode": config.get("mode", "paper"),
            "coins": config.get("coins", {}),
            "strategy_overrides": config.get("strategy_overrides", {}),
            # Preserve per-strategy-per-coin configs across set_config calls.
            # If the incoming config omits them, keep whatever we already had
            # so a partial update never wipes the paper/live safety settings.
            "strategy_coin_configs": config.get(
                "strategy_coin_configs",
                self.config.get("strategy_coin_configs", {}) if hasattr(self, "config") and self.config else {},
            ),
            "capital_allocation": config.get(
                "capital_allocation",
                self.config.get("capital_allocation", {}) if hasattr(self, "config") and self.config else {},
            ),
        }

    def capital_allocation(self, mode: str) -> Dict:
        """Saved capital allocation for 'live' or 'paper' (merged with defaults)."""
        base = dict(DEFAULT_CAPITAL_ALLOCATION.get(mode, {}))
        base.update((self.config.get("capital_allocation", {}) or {}).get(mode, {}))
        return base

    async def _live_total_balance(self) -> Optional[float]:
        if not self.client or not self.client.configured():
            return None
        try:
            bal = await self.client.get_balance()
            data = bal.get("data") if isinstance(bal, dict) else None
            if isinstance(data, list) and data:
                data = data[0]
            if isinstance(data, dict):
                def _num(v):
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        return 0.0
                return (_num(data.get("available") or data.get("availableBalance"))
                        + _num(data.get("frozen")) + _num(data.get("margin")))
        except Exception as e:
            logger.warning(f"_live_total_balance failed: {e}")
        return None

    async def allocated_capital(self, mode: str, total: Optional[float] = None) -> Optional[float]:
        """Effective capital cap (USDT) for the bot in the given mode.
        None = no enforceable cap (e.g. live balance unknown)."""
        a = self.capital_allocation(mode)
        am, val = a.get("mode", "full"), float(a.get("value") or 0)
        if mode == "paper":
            base = float(a.get("base_balance") or 1000.0)
            if am == "fixed":
                return val if val > 0 else base
            if am == "percent":
                return base * min(max(val, 0), 100) / 100
            return base
        if total is None:
            total = await self._live_total_balance()
        if am == "fixed":
            return min(val, total) if total is not None else val
        if am == "percent":
            return total * min(max(val, 0), 100) / 100 if total is not None else None
        return total

    async def used_margin(self, mode: str) -> float:
        """Sum of margin (max_capital) bound in open trades of the given mode."""
        if self.db is None:
            return 0.0
        used = 0.0
        async for t in self.db.auto_trades.find({"status": "open", "mode": mode}):
            used += float(t.get("max_capital") or 0)
        return round(used, 6)

    def coin_cfg(self, symbol: str) -> Dict:
        c = dict(DEFAULT_COIN_CFG)
        c.update(self.config.get("coins", {}).get(symbol, {}))
        return c

    def strategy_override(self, strategy_id: Optional[str]) -> Dict:
        if not strategy_id:
            return {}
        return dict(self.config.get("strategy_overrides", {}).get(strategy_id, {}))

    def effective_cfg(self, symbol: str, strategy_id: Optional[str]) -> Dict:
        """
        Merge coin defaults with any strategy-level override. Strategy override
        values (max_capital, leverage, sl_*, tp_*, breakeven, fee, pre_signals)
        take precedence when set. Reserved keys ('mode', 'enabled',
        'signals_enabled') are handled separately by the caller.
        """
        cfg = self.coin_cfg(symbol)
        so = self.strategy_override(strategy_id)
        RESERVED = {"mode", "enabled", "signals_enabled"}
        for k, v in so.items():
            if k in RESERVED or v is None:
                continue
            cfg[k] = v
        # Highest priority: per-strategy-per-coin trade parameters
        # (e.g. individual stop-loss / max_capital for Scalping+BTC).
        if strategy_id and symbol:
            key = f"{strategy_id}_{symbol}"
            scc = self.config.get("strategy_coin_configs", {}).get(key, {})
            for k, v in scc.items():
                if k in RESERVED or v is None:
                    continue
                cfg[k] = v
        return cfg

    def effective_mode(self, strategy_id: Optional[str], symbol: Optional[str] = None) -> str:
        """Return effective trading mode.
        Priority: strategy_coin_config > strategy_override > global mode.
        'off' means the strategy is disabled and no trade should be opened."""
        # 1) Highest priority: per-strategy-per-coin config
        if strategy_id and symbol:
            key = f"{strategy_id}_{symbol}"
            scc = self.config.get("strategy_coin_configs", {}).get(key, {})
            scm = scc.get("mode")
            if scm in ("live", "paper", "off"):
                return scm
        # 2) Strategy-level override
        so = self.strategy_override(strategy_id)
        sm = so.get("mode")
        if sm in ("live", "paper", "off"):
            return sm
        # 3) Fallback: global mode
        return self.config.get("mode", "paper")

    def is_enabled(self, symbol: str) -> bool:
        return self.coin_cfg(symbol).get("enabled", False)

    def _levels(self, cfg, side, entry, candles, indicators):
        # Volatility (ATR) drives a dynamic, noise-aware stop.
        atr = 0.0
        if candles and len(candles) > int(cfg.get("atr_period", 14)) + 1:
            atr_arr = TechnicalIndicators.calculate_atr(candles, int(cfg.get("atr_period", 14)))
            atr = atr_arr[-1] or 0.0
        atr_mult = float(cfg.get("atr_sl_multiplier", 1.2))
        buffer = atr * atr_mult

        mode = cfg.get("sl_mode", "structure")
        if mode == "atr" and atr > 0:
            sl = entry - buffer if side == "LONG" else entry + buffer
        elif mode == "structure" and candles:
            lookback = int(cfg["sl_lookback"])
            tick = entry * 0.0001
            ticks = int(cfg["sl_ticks"])
            struct_buffer = (buffer if buffer > 0 else ticks * tick)
            if side == "LONG":
                low = min(c["low"] for c in candles[-lookback:])
                sl = low - struct_buffer
            else:
                high = max(c["high"] for c in candles[-lookback:])
                sl = high + struct_buffer
        else:
            pct = float(cfg["sl_fixed_percent"]) / 100
            sl = entry * (1 - pct) if side == "LONG" else entry * (1 + pct)
        risk = abs(entry - sl)
        if risk <= 0:
            risk = entry * 0.003
            sl = entry - risk if side == "LONG" else entry + risk
        # ------------------------------------------------------------------
        # Enforce a MINIMUM TP/SL distance from entry. If risk is too small
        # (e.g. 0.07%), the market moves past TP between signal generation
        # and order placement and Bitunix rejects with code 30027
        # ("TP price must be greater than mark price"). The floor is the
        # bigger of `min_risk_percent` (default 0.25%) and 3x the ATR-driven
        # buffer if ATR is available.
        # ------------------------------------------------------------------
        min_risk_pct = float(cfg.get("min_risk_percent", 0.25)) / 100
        min_risk_abs = entry * min_risk_pct
        if risk < min_risk_abs:
            risk = min_risk_abs
            sl = entry - risk if side == "LONG" else entry + risk
        tp_mode = cfg.get("tp_mode", "crv")
        tp1 = tpf = None
        if tp_mode == "fixed_pct":
            p1 = float(cfg.get("tp1_percent", 0.5)) / 100
            pf = float(cfg.get("tp_full_percent", 1.0)) / 100
            if side == "LONG":
                tp1, tpf = entry * (1 + p1), entry * (1 + pf)
            else:
                tp1, tpf = entry * (1 - p1), entry * (1 - pf)
        elif tp_mode == "structure" and candles:
            lb = int(cfg.get("sl_lookback", 10))
            if side == "LONG":
                target = max(c["high"] for c in candles[-lb:])
                if target > entry * 1.001:
                    tpf = target
                    tp1 = entry + (target - entry) * 0.5
            else:
                target = min(c["low"] for c in candles[-lb:])
                if target < entry * 0.999:
                    tpf = target
                    tp1 = entry - (entry - target) * 0.5
        if tp1 is None or tpf is None:  # crv (Standard) oder Struktur-Fallback
            if side == "LONG":
                tp1 = entry + risk * cfg["tp1_crv"]
                tpf = entry + risk * cfg["tp_full_crv"]
            else:
                tp1 = entry - risk * cfg["tp1_crv"]
                tpf = entry - risk * cfg["tp_full_crv"]
        return round(sl, 6), round(tp1, 6), round(tpf, 6), risk, round(atr, 6)

    async def _notify_reject(self, symbol: str, side: str, reason: str) -> None:
        if not self.telegram:
            return
        # Anti-Spam: identische Ablehnung (Symbol+Seite+Meldungskern) höchstens
        # alle 30 Minuten melden. Bug-Report: eine manuell eröffnete QQQ-Position
        # (nicht per OpenAPI handelbar) erzeugte bei jedem Zyklus eine
        # "ORDER ABGEBROCHEN"-Telegram-Nachricht.
        try:
            sent = getattr(self, "_reject_sent", None)
            if sent is None:
                sent = self._reject_sent = {}
            key = f"{symbol}:{side}:{str(reason)[:80]}"
            now = time.time()
            if now - sent.get(key, 0) < 1800:
                logger.info(f"Reject-Notify unterdrückt (30min-Cooldown): {key}")
                return
            sent[key] = now
            if len(sent) > 200:
                for k in sorted(sent, key=sent.get)[:100]:
                    sent.pop(k, None)
        except Exception:
            pass
        try:
            await self.telegram.send_rejection(symbol, side, reason)
        except Exception as e:
            logger.error(f"telegram reject notify failed: {e}")

    async def _current_mark(self, symbol: str) -> Optional[float]:
        """Try to get the freshest mark price. Falls back to None."""
        if self.client and self.client.configured():
            try:
                return await self.client.get_mark_price(symbol)
            except Exception as e:
                logger.warning(f"_current_mark failed: {e}")
        return None

    async def on_signal(self, signal: Dict, candles: List[Dict]) -> Optional[Dict]:
        symbol = signal["symbol"]
        strategy_id = signal.get("strategy_id")
        # Master-Kill-Switch ("Stop All Trades"): blockiert JEDE neue Trade-
        # Eröffnung zentral – Scanner-Strategien, KI-Trader und KI-Custom-Trades.
        # Vorher wurden beim Einschalten nur offene Trades geschlossen, neue
        # liefen ungebremst weiter (Import lazy wegen core.state-Zirkularität).
        from core.state import control_state
        if control_state.get("trades_paused"):
            logger.info(f"AutoTrade blockiert {symbol} ({strategy_id}): "
                        "Master-Schalter 'Stop All Trades' aktiv")
            signal["_reject_reason"] = "Master-Schalter 'Stop All Trades' aktiv"
            return None
        # Effective mode: strategy_coin_config > strategy override > global.
        # 'off' means this strategy is disabled -> no trade.
        # Datensammel-Modus (Phase 4): Sammel-Signale sind IMMER Paper und
        # laufen auch auf Coins, die für die Strategie auf AUS stehen.
        collection = bool(signal.get("data_collection"))
        eff_mode = "paper" if collection else self.effective_mode(strategy_id, symbol)
        if eff_mode == "off":
            signal["_reject_reason"] = f"{strategy_id or 'Strategie'} steht für {symbol} auf AUS"
            return None
        cfg = self.effective_cfg(symbol, strategy_id)
        # Höchste Priorität für KI-Strategie-Kandidaten: deren individuelle
        # Makro-Parameter (services/ai_strategy_lab.py) überschreiben die Config
        # nur für diesen Trade.
        overrides = signal.get("cfg_overrides") or {}
        if isinstance(overrides, dict):
            for k, v in overrides.items():
                if k in cfg and v is not None:
                    cfg[k] = v
        # Enable-Logik: Wenn eine per-(Strategie,Coin)- oder Strategie-Config
        # explizit auf live/paper steht, gilt DEREN enabled-Flag (Default True).
        # Nur ohne solche Config bleibt der Coin-Schalter der Master-Switch.
        # Fix: vorher blockierte der Coin-Level-Schalter Trades, obwohl die
        # Strategie-Coin-Config auf live/paper gespeichert war.
        scc = self.config.get("strategy_coin_configs", {}).get(
            f"{strategy_id}_{symbol}", {}) if strategy_id else {}
        so = self.strategy_override(strategy_id)
        if not collection:
            if scc.get("mode") in ("live", "paper"):
                if scc.get("enabled") is False:
                    return None
            elif so.get("mode") in ("live", "paper"):
                if so.get("enabled") is False:
                    return None
            elif not cfg["enabled"]:
                return None
        if signal.get("signal_class") == "PRE_SIGNAL" and not cfg["trade_pre_signals"]:
            return None
        # Nur traden, wenn ALLE Regeln erfüllt sind (Fix: 3/5-Regeln-Trades)
        if cfg.get("require_all_rules") and signal.get("rules_total") \
                and (signal.get("rules_met_count") or 0) < signal["rules_total"]:
            return None
        # Offene-Trades-Limit pro Coin.
        # KI-Trader ("ai_trader"): bis zu max_trades_per_coin (1–5, per Panel-
        # Dropdown einstellbar) gleichzeitig offene Trades pro Coin.
        # Alle anderen Strategien: strikt EIN offener Trade pro Coin.
        if strategy_id == "ai_trader":
            ai_cfg = await self.db.settings.find_one({"_id": "ai_trader_config"}) or {}
            if collection:
                # Sammel-Trades haben eigene Slots und verbrauchen keine Live-Slots
                max_per_coin = max(1, min(5, int(ai_cfg.get("collection_max_per_coin", 2) or 2)))
                open_count = await self.db.auto_trades.count_documents(
                    {"symbol": symbol, "status": "open", "strategy_id": "ai_trader",
                     "data_collection": True})
            else:
                max_per_coin = max(1, min(5, int(ai_cfg.get("max_trades_per_coin", 1) or 1)))
                open_count = await self.db.auto_trades.count_documents(
                    {"symbol": symbol, "status": "open", "strategy_id": "ai_trader",
                     "data_collection": {"$ne": True}})
            if open_count >= max_per_coin:
                signal["_reject_reason"] = (f"Trade-Limit erreicht: {open_count} offene "
                                            f"KI-Trades auf {symbol} (max. {max_per_coin})")
                return None
        else:
            existing = await self.db.auto_trades.find_one({"symbol": symbol, "status": "open"})
            if existing:
                signal["_reject_reason"] = f"Bereits ein offener Trade auf {symbol}"
                return None

        side = signal["type"]
        entry = float(signal.get("entry_price") or 0)
        if entry <= 0:
            return None
        # ---- Risiko-Schutzschicht: Kill-Switch + Anti-Stacking ----
        from services import trade_guard
        tf = str(signal.get("timeframe") or signal.get("strategy_timeframe") or "")
        if not tf:
            try:
                from strategies.registry import registry as _reg
                st = _reg.get(strategy_id) if strategy_id else None
                tf = getattr(st, "STRATEGY_TIMEFRAME", "1m") if st else "1m"
            except Exception:
                tf = "1m"
        guard_ok, guard_reason = await trade_guard.check_open_allowed(self.db, signal, tf)
        if not guard_ok:
            logger.info(f"AutoTrade blockiert {symbol} {side}: {guard_reason}")
            signal["_reject_reason"] = guard_reason
            return None
        sl, tp1, tpf, risk, atr = self._levels(cfg, side, entry, candles, signal)

        # KI Trader: optional die von der KI berechneten Levels direkt nutzen
        # (use_ai_levels in der KI-Config). Live-Mark-Price-Guards unten greifen weiterhin.
        if signal.get("use_ai_levels"):
            try:
                _sl = float(signal.get("stop_loss") or 0)
                _tp1 = float(signal.get("take_profit_1") or 0)
                _tpf = float(signal.get("take_profit_full") or 0)
                if _sl > 0 and _tp1 > 0 and _tpf > 0:
                    sl, tp1, tpf = _sl, _tp1, _tpf
                    risk = abs(entry - sl) or risk
            except (TypeError, ValueError):
                pass

        # ---- Fee-Wächter (nur KI-Trader): Physik-Grenze statt Stil-Vorgabe.
        # Der finale SL steht erst HIER fest (Coin-Config oder use_ai_levels) –
        # deshalb sitzt der Wächter an dieser Stelle und deckt alle Pfade ab
        # (Live-Signale, Sammel-Trades, KI-Panel-Trades). Abschaltbar im Setup.
        if strategy_id == "ai_trader":
            fg_ok, fg_reason = fee_guard_check(ai_cfg, cfg, entry, sl)
            if not fg_ok:
                logger.info(f"AutoTrade blockiert {symbol} {side}: {fg_reason}")
                signal["_reject_reason"] = fg_reason
                # Blockier-Statistik: jeden Block mit geschätzten Roundtrip-Fees
                # protokollieren (Notional = Kapital × Hebel). 60-Tage-Retention.
                try:
                    lev_est = effective_leverage(cfg, entry, sl) \
                        if cfg.get("auto_leverage_enabled") else float(cfg.get("leverage") or 1)
                    fee_pct = float(cfg.get("fee_percent", 0.06) or 0.06)
                    est_fees = round(float(cfg.get("max_capital") or 0) * lev_est
                                     * 2 * fee_pct / 100, 4)
                    now_iso = datetime.now(timezone.utc).isoformat()
                    await self.db.fee_guard_blocks.insert_one({
                        "id": str(uuid.uuid4()), "ts": now_iso,
                        "symbol": symbol, "side": side, "collection": collection,
                        "sl_dist_pct": round(abs(entry - sl) / entry * 100, 4),
                        "est_fees_usdt": est_fees, "reason": fg_reason})
                    await self.db.fee_guard_blocks.delete_many(
                        {"ts": {"$lt": (datetime.now(timezone.utc)
                                        - timedelta(days=60)).isoformat()}})
                except Exception:
                    pass
                if not collection:
                    try:
                        await self.db.ai_chat.insert_one({
                            "id": str(uuid.uuid4()), "role": "governance",
                            "text": f"Trade {side} {symbol} blockiert – {fg_reason}",
                            "ts": datetime.now(timezone.utc).isoformat()})
                    except Exception:
                        pass
                return None

        # Auto-Leverage: Hebel so setzen, dass die Liquidation den konfigurierten
        # Abstand hinter dem Stop-Loss hat (sonst fester Hebel aus der Config)
        lev_used = effective_leverage(cfg, entry, sl) if cfg.get("auto_leverage_enabled") \
            else float(cfg["leverage"])

        # ---- KI-Custom-Trade: Hebel/Kapitalanteil dürfen pro Trade vorgegeben
        # werden (services/ai_trade_manager.py). Immer innerhalb der Limits der
        # Coin-Config – max_capital und der Live/Paper-Modus bleiben tabu.
        try:
            ai_lev = float(signal.get("ai_leverage") or 0)
            if ai_lev > 0:
                lev_used = max(1.0, min(200.0, ai_lev))
        except (TypeError, ValueError):
            pass
        mode = eff_mode
        # Manueller Modus-Override (nur Nicht-KI-Quellen setzen force_mode)
        fm = str(signal.get("force_mode") or "").lower()
        if fm in ("live", "paper"):
            mode = fm
        # Instrumente ohne Bitunix-Kontrakt (z.B. Forex) können nicht live
        # geordert werden -> automatisch als Paper-Trade simulieren.
        if mode == "live" and not _instruments.is_tradable(symbol):
            logger.warning(f"{symbol}: kein Bitunix-Kontrakt – Live-Trade wird "
                           "als Paper-Trade simuliert")
            mode = "paper"
        # KI-Strategie-Kandidaten (services/ai_strategy_lab.py) dürfen erst nach
        # bestandener Ghost-Phase und Freigabe des Traders live handeln.
        if mode == "live" and signal.get("force_paper"):
            logger.info(f"{symbol}: Signal erzwingt Paper "
                        f"({signal.get('force_paper_reason') or 'noch nicht freigegeben'})")
            mode = "paper"
        # ---- Kapital-Zuweisung: Gesamt-Exposure des Bots begrenzen ----
        capital = float(cfg["max_capital"])
        # KI-Trader mit eigenem "Max. Kapital pro Trade": überschreibt die
        # Coin-Config als Basis; die KI wählt darunter per ai_capital_pct.
        try:
            ai_max_cap = float(signal.get("ai_max_capital") or 0)
            if ai_max_cap > 0:
                capital = ai_max_cap
        except (TypeError, ValueError):
            pass
        try:
            ai_cap_pct = float(signal.get("ai_capital_pct") or 0)
            if 5.0 <= ai_cap_pct <= 100.0:
                capital = round(capital * ai_cap_pct / 100, 6)
        except (TypeError, ValueError):
            pass
        alloc_note = None
        try:
            alloc_cap = await self.allocated_capital(mode)
        except Exception as e:
            logger.warning(f"allocated_capital({mode}) failed: {e}")
            alloc_cap = None
        if alloc_cap is not None:
            used = await self.used_margin(mode)
            free_alloc = round(alloc_cap - used, 6)
            if free_alloc < 5.0:
                logger.info(f"{symbol}: Kapital-Limit erreicht "
                            f"({used:.2f}/{alloc_cap:.2f} USDT belegt) -> kein Trade")
                signal["_reject_reason"] = (f"Kapital-Limit erreicht: "
                                            f"{used:.2f}/{alloc_cap:.2f} USDT belegt")
                await self._notify_reject(
                    symbol, side,
                    f"Kapital-Limit erreicht: {used:.2f}/{alloc_cap:.2f} USDT belegt")
                return None
            if capital > free_alloc:
                alloc_note = (f"Kapital auf {free_alloc:.2f} USDT begrenzt "
                              f"(Limit {alloc_cap:.2f}, belegt {used:.2f})")
                capital = free_alloc
        qty = round((capital * lev_used) / entry, 6)

        # ---- LIVE MODE: hit the exchange FIRST; only persist on success ----
        if mode == "live" and self.client.configured():
            # Guard: if the calculated qty is below the exchange minimum and
            # we don't have enough capital to bump it up, skip the trade and
            # notify instead of letting Bitunix reject with code 30016.
            b_sym = self.client.to_bitunix_symbol(symbol)
            meta = self.client.contract_meta(b_sym) or {}
            min_qty = float(meta.get("min_qty") or 0)
            if min_qty > 0 and qty < min_qty:
                needed_capital = (min_qty * entry) / lev_used
                logger.warning(
                    f"{symbol}: qty {qty} < min {min_qty}. Needs "
                    f"~{needed_capital:.2f} USDT capital @ {lev_used}x."
                )
                await self._notify_reject(
                    symbol, side,
                    f"Menge {qty} unter Bitunix-Minimum {min_qty}. "
                    f"Erhoehe max_capital auf mind. {needed_capital:.2f} USDT."
                )
                return None

            # Re-align TP/SL to the CURRENT mark price so they can't be on
            # the wrong side by the time the order arrives (code 30027).
            try:
                mark = await self._current_mark(symbol)
            except Exception:
                mark = None
            if mark and mark > 0:
                # Minimum absolute distance TP/SL must keep from the mark
                # price. Configurable via `min_tp_distance_percent` (default
                # 0.15%). This eats a bit of edge but eliminates 30027.
                min_dist_pct = float(cfg.get("min_tp_distance_percent", 0.15)) / 100
                min_dist = mark * min_dist_pct
                if side == "LONG":
                    tpf = max(tpf, mark + min_dist)
                    tp1 = max(tp1, mark + min_dist / 2)
                    sl = min(sl, mark - min_dist)
                else:
                    tpf = min(tpf, mark - min_dist)
                    tp1 = min(tp1, mark - min_dist / 2)
                    sl = max(sl, mark + min_dist)
                sl, tp1, tpf = round(sl, 6), round(tp1, 6), round(tpf, 6)

            try:
                await self.client.set_leverage(symbol, max(int(round(lev_used)), 1),
                                               cfg["margin_mode"])
                side_order = "BUY" if side == "LONG" else "SELL"
                res = await self.client.place_order(symbol, side_order, qty,
                                                    order_type=cfg["order_type"],
                                                    tp_price=tpf, sl_price=sl)
            except Exception as e:
                # BUGFIX (ADA/DOT ohne SL): Ein Timeout/Netzfehler kann auftreten,
                # NACHDEM Bitunix die Order bereits angenommen hat. Vorher wurde
                # der Trade dann lokal verworfen -> die Position lief unsichtbar
                # (nicht auf der Website) und ohne lokales Monitoring an der
                # Börse weiter. Jetzt: nachprüfen, ob an der Börse eine nicht
                # erfasste Menge aufgetaucht ist, und die Position übernehmen.
                reason = f"exception: {str(e)[:160]}"
                logger.error(f"Live order EXCEPTION {symbol}: {e}")
                await asyncio.sleep(1.5)
                untracked = None
                try:
                    untracked = await self._untracked_qty(symbol, side)
                except Exception as e2:
                    logger.warning(f"_untracked_qty({symbol}) failed: {e2}")
                if untracked is not None and untracked >= qty * 0.5:
                    logger.error(f"{symbol}: Order-Antwort verloren, aber Position "
                                 f"an der Börse gefunden (Menge {untracked}) – "
                                 "Trade wird übernommen statt verworfen")
                    res = {"code": 0, "data": {}, "recovered_after_exception": True}
                else:
                    await self._notify_reject(symbol, side, reason)
                    return None

            ok = isinstance(res, dict) and res.get("code") == 0
            order_id = _extract_order_id(res)
            if ok and not order_id:
                # code 0 ohne auffindbare orderId: Order wurde angenommen ->
                # NICHT als Ablehnung behandeln (vorher entstand hier eine
                # Ghost-Position ohne lokalen Trade und ggf. ohne Stop-Loss).
                logger.warning(f"{symbol}: Order angenommen (code 0), aber keine "
                               f"orderId in der Antwort: {str(res)[:160]}")
            if not ok:
                reason = (isinstance(res, dict) and (res.get("msg") or str(res))) or "unknown error"
                code = isinstance(res, dict) and res.get("code")
                logger.error(f"Live order REJECTED {symbol} side={side} qty={qty} "
                             f"code={code} msg={reason}")
                await self._notify_reject(symbol, side, f"code {code}: {reason}")
                # No local persistence -> no ghost position.
                return None

            # ----------------------------------------------------------------
            # Entry filled. Now put TP1 (partial, reduce-only) directly on the
            # exchange – previously TP1 was only enforced by our local monitor
            # via flash_close, so if the backend was lagging or offline the
            # partial TP never fired.
            # ----------------------------------------------------------------
            position_id: Optional[str] = None
            tp1_placed = False
            tpsl_order_id: Optional[str] = None
            try:
                # small delay so the position is picked up by the position API
                position_id = await self.client.resolve_position_id(symbol, side)
                if position_id:
                    tp1_close_qty = round(qty * float(cfg["tp1_close_percent"]) / 100, 6)
                    tp1_res = await self.client.place_position_tp_sl(
                        symbol, position_id, side,
                        tp_price=tp1, tp_qty=tp1_close_qty)
                    tp1_ok = isinstance(tp1_res, dict) and tp1_res.get("code") == 0
                    if tp1_ok:
                        tp1_placed = True
                        tpsl_order_id = _extract_order_id(tp1_res)
                        logger.info(f"TP1 partial placed on Bitunix {symbol} "
                                    f"@ {tp1} qty={tp1_close_qty}")
                    else:
                        logger.warning(f"TP1 partial place failed {symbol}: {tp1_res}")
                else:
                    logger.warning(f"Could not resolve positionId for {symbol}; "
                                   "TP1 partial NOT placed (local monitor will "
                                   "handle it as fallback).")
            except Exception as e:
                logger.error(f"TP1 partial exception {symbol}: {e}")

            # ---- SL-VERIFIKATION (Bug-Report: Position ohne Stop-Loss) ----
            # place_order enthält den SL zwar, aber die Börse kann ihn still
            # verwerfen. Deshalb wird er hier verifiziert und notfalls
            # nachgesetzt; scheitert auch das endgültig, wird die Position
            # sofort wieder geschlossen (Nutzer-Vorgabe: Retry -> Close).
            sl_missing = False
            if position_id:
                sl_ok = await self._ensure_live_sl(symbol, side, position_id, sl)
                if sl_ok is False:
                    try:
                        close_res = await self.client.flash_close(
                            symbol, position_id, side, qty)
                    except Exception as e:
                        close_res = {"code": -1, "msg": str(e)[:140]}
                    closed_ok = isinstance(close_res, dict) and close_res.get("code") == 0
                    await self._notify_reject(
                        symbol, side,
                        "Stop-Loss konnte an der Börse NICHT gesetzt werden – "
                        + ("Position wurde zur Sicherheit sofort geschlossen."
                           if closed_ok else
                           "NOTFALL-CLOSE FEHLGESCHLAGEN – bitte SOFORT manuell "
                           "in Bitunix prüfen!"))
                    if closed_ok:
                        logger.error(f"{symbol}: Notfall-Close wegen fehlendem SL")
                        return None
                    # Close fehlgeschlagen: Trade trotzdem lokal führen, damit
                    # Monitor + Watchdog die Position weiter absichern.
                    sl_missing = True

            trade_extra = {"bitunix_order_id": order_id, "bitunix_response": res,
                           "bitunix_position_id": position_id,
                           "bitunix_tpsl_order_id": tpsl_order_id,
                           "tp1_exchange_placed": tp1_placed,
                           "sl_exchange_missing": sl_missing}
        else:
            trade_extra = {"bitunix_order_id": None,
                           "bitunix_position_id": None,
                           "bitunix_tpsl_order_id": None,
                           "tp1_exchange_placed": False}

        # Gebühren: Entry-Fee sofort verbuchen (Taker-Fee auf das Volumen),
        # damit Paper-Trades die REALE Kostenbasis von Live-Trades abbilden.
        entry_fee = round(entry * qty * float(cfg.get("fee_percent", 0.06)) / 100, 6)

        # Liquidationspreis (Isolated Margin): ~ Entry * (1 ± (1/Hebel - MMR))
        lev = max(lev_used, 1.0)
        mmr = float(cfg.get("maintenance_margin_rate", 0.5)) / 100
        liq_dist = max(1.0 / lev - mmr, 0.0005)
        liq_price = round(entry * (1 - liq_dist) if side == "LONG"
                          else entry * (1 + liq_dist), 6)

        # ML-Fix 0.2: Marktzustand im Entry-Moment dauerhaft am Trade speichern
        # (vorher wurde er nachträglich per nearest-Snapshot rekonstruiert).
        try:
            from services.ai_market_observer import market_observer, compute_features
            prev_regime = (market_observer.features_for(symbol) or {}).get("regime")
            feats = compute_features(candles, prev_regime=prev_regime)
            entry_snap = ({"ts": datetime.now(timezone.utc).isoformat(),
                           "source": "signal_candles", "features": feats}
                          if feats else market_observer.entry_snapshot(symbol))
        except Exception:
            entry_snap = None

        trade = {
            "id": f"{symbol}-{int(time.time()*1000)}",
            "symbol": symbol, "side": side, "mode": mode,
            "entry": entry, "sl": sl, "tp1": tp1, "tpf": tpf, "initial_sl": sl,
            "liq_price": liq_price, "liquidated": False,
            "atr": atr,
            "qty": qty, "qty_remaining": qty, "risk": round(risk, 6),
            "tp1_crv": cfg["tp1_crv"], "tp_full_crv": cfg["tp_full_crv"],
            "tp1_close_percent": cfg["tp1_close_percent"],
            "breakeven_enabled": cfg["breakeven_enabled"], "fee_percent": cfg["fee_percent"],
            "be_mode": (cfg.get("be_mode") or ("tp1" if cfg.get("breakeven_enabled", True) else "off")),
            "be_trigger_crv": float(cfg.get("be_trigger_crv", 1.0) or 1.0),
            "be_trigger_profit_pct": float(cfg.get("be_trigger_profit_pct", 30.0) or 30.0),
            "leverage": round(lev_used, 2), "max_capital": round(capital, 6),
            "auto_leverage": bool(cfg.get("auto_leverage_enabled")),
            "status": "open", "tp1_hit": False, "breakeven_moved": False,
            "realized_pnl": round(-entry_fee, 6), "fees_paid": entry_fee,
            "profit_secure_enabled": bool(cfg.get("profit_secure_enabled", False)),
            "profit_secure_trigger_pct": float(cfg.get("profit_secure_trigger_pct", 30.0)),
            "profit_lock_pct": float(cfg.get("profit_lock_pct", 50.0)),
            "profit_secured": False,
            "strategy_id": ("external" if signal.get("manual_trade")
                            else signal.get("strategy_id")),
            "strategy_name": ("Manuell (Website)" if signal.get("manual_trade")
                              else signal.get("strategy_name")),
            "manual_trade": bool(signal.get("manual_trade")),
            "timeframe": tf,
            "horizon": signal.get("ai_horizon") or "scalp",
            "runner": bool(signal.get("ai_runner")),
            "setup": signal.get("ai_setup"),
            "ai_reasoning": signal.get("ai_reasoning"),
            "ai_news_impact": signal.get("ai_news_impact"),
            "ai_confidence": signal.get("ai_confidence"),
            "ai_size_reason": signal.get("ai_size_reason"),
            "ai_levels_reason": signal.get("ai_levels_reason"),
            "ai_candidate_id": signal.get("ai_candidate_id"),
            "signal_id": signal.get("id"),
            "decision_id": signal.get("decision_id"),
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "trade_date": signal.get("trade_date"),
            "entry_market_snapshot": entry_snap,
            "events": ([f"OPEN {side} @ {entry} (Entry-Fee {entry_fee} USDT)"]
                       + ([alloc_note] if alloc_note else [])),
            **trade_extra,
        }

        if collection:
            trade["data_collection"] = True
            if signal.get("collection_reason"):
                trade["collection_reason"] = signal["collection_reason"]
        await self.db.auto_trades.insert_one(dict(trade))
        logger.info(f"AutoTrade OPEN {side} {symbol} qty={qty} entry={entry} mode={mode}"
                    + (" [Datensammlung]" if collection else ""))
        trade.pop("_id", None)
        if collection:
            # Kein Telegram-Spam durch Sammel-Trades (bis zu Dutzende pro Tag)
            return trade
        try:
            from services import notifications
            emoji = "🟢" if side == "LONG" else "🔴"
            await notifications.telegram_notify(
                self.db, self.telegram, "trade_opened",
                f"{emoji} *TRADE ERÖFFNET* ({mode.upper()})\n"
                f"💰 {symbol} · {side} · {trade.get('strategy_name') or strategy_id}\n"
                f"Entry `{entry}` · SL `{sl}` · TP `{tpf}` · Hebel {trade['leverage']}x")
        except Exception as e:
            logger.warning(f"trade_opened notify failed: {e}")
        return trade

    async def monitor(self, prices: Dict[str, float]):
        """Called periodically. Manage open trades against live prices."""
        if self.db is None:
            return
        cursor = self.db.auto_trades.find({"status": "open"})
        async for t in cursor:
            if t.get("external_adopted"):
                # Vom Watchdog übernommene Börsen-Position: SL/TP liegen an der
                # Börse, der Bitunix-Sync verbucht den externen Close.
                continue
            symbol = t["symbol"]
            price = prices.get(symbol)
            if not price:
                continue
            await self._manage_trade(t, price)
        # Bitunix-Abgleich: extern geschlossene Live-Positionen auch lokal
        # schließen (max. 1x pro Minute, um die API nicht zu belasten).
        now = time.time()
        if now - self._last_pos_sync >= 60:
            self._last_pos_sync = now
            try:
                await self.sync_live_positions()
            except Exception as e:
                logger.warning(f"Bitunix-Positions-Sync fehlgeschlagen: {e}")

    # ------------------------------------------------------------------
    # Bitunix-Abgleich: extern geschlossene Live-Positionen erkennen.
    # ------------------------------------------------------------------
    _POSITION_GONE_HINTS = ("position not exist", "position does not exist",
                            "position not exists", "no position",
                            "insufficient amount", "insufficient position")

    def _looks_like_position_gone(self, detail: str) -> bool:
        d = str(detail or "").lower()
        return any(k in d for k in self._POSITION_GONE_HINTS)

    async def _position_still_open(self, symbol: str, side: str) -> Optional[bool]:
        """Existiert die Position an der Börse noch? None = API unsicher
        (dann NICHT handeln, kein falscher Sync)."""
        try:
            res = await self.client.get_positions(symbol)
        except Exception as e:
            logger.warning(f"get_positions({symbol}) fehlgeschlagen: {e}")
            return None
        if not isinstance(res, dict) or res.get("code") not in (0, "0", None):
            return None
        data = res.get("data")
        rows = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
        want = str(side).upper()
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_side = str(row.get("side") or row.get("positionSide") or "").upper()
            match = (row_side in ("BUY", "LONG") and want == "LONG") or \
                    (row_side in ("SELL", "SHORT") and want == "SHORT")
            if not match:
                continue
            qty_raw = row.get("qty", row.get("total", row.get("amount")))
            try:
                if qty_raw is None or float(qty_raw) > 0:
                    return True
            except (TypeError, ValueError):
                return True
        return False

    async def _untracked_qty(self, symbol: str, side: str) -> Optional[float]:
        """Börsen-Menge (symbol+side) minus lokal erfasster offener Menge.
        None = Börsen-API unsicher (dann keine Entscheidung treffen)."""
        live = await self._live_position_qty({"symbol": symbol, "side": side})
        if live is None:
            return None
        local = 0.0
        async for t in self.db.auto_trades.find(
                {"status": "open", "mode": "live", "symbol": symbol,
                 "side": str(side).upper()}):
            local += float(t.get("qty_remaining", t.get("qty", 0)) or 0)
        return live - local

    async def _position_has_sl(self, symbol: str, position_id: str) -> Optional[bool]:
        """Hat die Position an der Börse einen aktiven Stop-Loss?
        True/False = sichere Antwort, None = API unsicher (NICHT eskalieren)."""
        try:
            res = await self.client.get_pending_tpsl(symbol, position_id=position_id)
        except Exception as e:
            logger.warning(f"_position_has_sl({symbol}) failed: {e}")
            return None
        if not isinstance(res, dict) or res.get("code") not in (0, "0"):
            return None
        data = res.get("data")
        if isinstance(data, dict):
            data = data.get("orderList") or data.get("list") or data.get("rows") or [data]
        rows = data if isinstance(data, list) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            for key in ("slPrice", "slStopPrice", "stopLossPrice"):
                try:
                    if float(row.get(key) or 0) > 0:
                        return True
                except (TypeError, ValueError):
                    continue
        return False

    async def _ensure_live_sl(self, symbol: str, side: str, position_id: str,
                              sl_price: float,
                              max_attempts: int = 3) -> Optional[bool]:
        """Sicherstellen, dass die Live-Position einen Börsen-SL hat.
        True = SL bestätigt, False = konnte nach max_attempts NICHT gesetzt
        werden, None = TP/SL-API unsicher (kein falscher Alarm)."""
        last = None
        for attempt in range(1, max_attempts + 1):
            has = await self._position_has_sl(symbol, position_id)
            if has is None:
                logger.warning(f"{symbol}: SL-Verifikation unsicher (TP/SL-API "
                               "nicht lesbar) – kein Eingriff")
                return None
            if has:
                return True
            logger.warning(f"{symbol}: KEIN Stop-Loss an der Börse – setze nach "
                           f"(Versuch {attempt}/{max_attempts})")
            try:
                last = await self.client.place_position_tp_sl(
                    symbol, position_id, side, sl_price=sl_price)
            except Exception as e:
                last = {"code": -1, "msg": str(e)[:140]}
            await asyncio.sleep(1.0)
        logger.error(f"{symbol}: Stop-Loss konnte NICHT gesetzt werden: {last}")
        return False

    async def _exchange_close_truth(self, t: Dict) -> Optional[Dict]:
        """ECHTEN Bitunix-Abschluss (closePrice/realizedPNL) für einen Live-Trade
        holen. None bei Paper-Trades, fehlender Position-ID oder wenn die Position
        manuell aufgestockt war (dann wäre der Positions-PnL mehr als der Trade)."""
        pid = t.get("bitunix_position_id")
        if t.get("mode") != "live" or not pid or not self.client.configured():
            return None
        try:
            res = await self.client.get_history_positions(position_id=pid)
        except Exception as e:
            logger.debug(f"Positions-Historie nicht abrufbar ({t.get('symbol')}): {e}")
            return None
        exact = parse_closed_position(res, pid)
        if not exact:
            return None
        qty = float(t.get("qty") or 0)
        if not t.get("external_adopted") and exact.get("max_qty") and qty > 0 \
                and abs(exact["max_qty"] - qty) / exact["max_qty"] > 0.05:
            return None
        return exact

    async def _book_external_close(self, t: Dict) -> Optional[Dict]:
        """Trade lokal als extern (an der Börse) geschlossen verbuchen.
        Für Live-Trades (inkl. manueller Bitunix-Trades) wird der ECHTE
        Börsen-Abschluss übernommen (Positions-Historie), statt ihn per
        Mark-Preis zu schätzen – Schätzung nur noch als Fallback."""
        exact = await self._exchange_close_truth(t)
        if exact:
            price = exact["exit_price"] or float(
                await self._current_mark(t["symbol"]) or t.get("entry") or 0)
            realized = exact["net_pnl"]
            fees_total = exact["fee"]
            event = (f"EXTERN GESCHLOSSEN (Bitunix-Sync) @ {price} – echter "
                     f"Börsen-PnL {realized:+} (inkl. Fees/Funding)")
        else:
            price = float(await self._current_mark(t["symbol"]) or t.get("entry") or 0)
            if price <= 0:
                return None
            qty_rem = float(t.get("qty_remaining", t["qty"]) or 0)
            fee_pct = float(t.get("fee_percent", 0.06)) / 100
            fee = qty_rem * price * fee_pct
            pnl = ((price - t["entry"]) if t["side"] == "LONG"
                   else (t["entry"] - price)) * qty_rem
            realized = round(float(t.get("realized_pnl", 0.0)) + pnl - fee, 6)
            fees_total = round(float(t.get("fees_paid", 0.0)) + fee, 6)
            event = f"EXTERN GESCHLOSSEN (Bitunix-Sync) @ {price}"
        result = "win" if realized > 0 else ("breakeven" if realized == 0 else "loss")
        closed_at = datetime.now(timezone.utc).isoformat()
        await self.db.auto_trades.update_one({"id": t["id"]}, {"$set": {
            "status": "closed", "exit_price": price, "result": result,
            "realized_pnl": realized, "qty_remaining": 0,
            "fees_paid": fees_total,
            "pnl_exchange_exact": bool(exact),
            "closed_by": "bitunix_sync", "live_close_failed": False,
            "closed_at": closed_at,
            "events": (t.get("events", []) + [event])[-20:]}})
        await self._after_close({**t, "status": "closed", "result": result,
                                 "realized_pnl": realized, "exit_price": price,
                                 "closed_at": closed_at})
        logger.info(f"Bitunix-Sync: {t['symbol']} {t['side']} extern geschlossen -> "
                    f"lokal verbucht (PnL {realized})")
        return {"result": result, "realized_pnl": realized, "exit_price": price}

    async def _reconcile_close_error(self, t: Dict, detail: str) -> Optional[Dict]:
        """Live-Aktion scheiterte mit einem 'Position weg'-Fehler (z.B.
        'Position not exist' / 'Insufficient amount'): an der Börse nachprüfen
        und den Trade ggf. SOFORT als extern geschlossen verbuchen, statt ihn
        fälschlich offen zu lassen (Bug-Report: CLOSE/ADJUST-Fehlerschleifen)."""
        if not self._looks_like_position_gone(detail):
            return None
        if await self._position_still_open(t["symbol"], t["side"]) is not False:
            return None
        return await self._book_external_close(t)

    async def sync_live_positions(self) -> int:
        """Offene Live-Trades gegen die echten Bitunix-Positionen abgleichen.

        Wurde eine Position extern geschlossen (z.B. direkt in der Bitunix-App
        oder durch Börsen-TP/SL, den wir nicht mitbekommen haben), wird der
        Trade lokal zum aktuellen Kurs geschlossen und normal verbucht.
        Gibt die Anzahl synchronisierter Trades zurück."""
        if self.db is None or not self.client.configured():
            return 0
        open_live = await self.db.auto_trades.find(
            {"status": "open", "mode": "live"}).to_list(100)
        if not open_live:
            await self._record_sync(0, 0)
            return 0
        synced = 0
        cache: Dict[str, Optional[bool]] = {}
        for t in open_live:
            key = f"{t['symbol']}|{str(t['side']).upper()}"
            if key not in cache:
                cache[key] = await self._position_still_open(t["symbol"], t["side"])
            if cache[key] is not False:
                continue
            if await self._book_external_close(t):
                synced += 1
        await self._record_sync(synced, len(open_live))
        return synced

    async def _record_sync(self, synced: int, open_live: int):
        """Zeitpunkt/Ergebnis des letzten Bitunix-Abgleichs fürs Master-Panel merken."""
        try:
            await self.db.settings.update_one(
                {"_id": "bitunix_sync_status"},
                {"$set": {"last_sync_at": datetime.now(timezone.utc).isoformat(),
                          "last_synced": synced, "open_live": open_live}},
                upsert=True)
        except Exception as e:
            logger.debug(f"Sync-Status nicht gespeichert: {e}")

    async def _manage_trade(self, t: Dict, price: float):
        side = t["side"]
        updates = {}
        events = list(t.get("events", []))
        realized = t.get("realized_pnl", 0.0)
        qty_rem = t.get("qty_remaining", t["qty"])
        closed = False
        exit_price = None
        result = None
        fee_pct = float(t.get("fee_percent", 0.06)) / 100
        fees_paid = float(t.get("fees_paid", 0.0))

        def pnl(qty, exit_p):
            return (exit_p - t["entry"]) * qty if side == "LONG" else (t["entry"] - exit_p) * qty

        def exit_fee(qty, exit_p):
            return qty * exit_p * fee_pct

        hit_tp1 = (price >= t["tp1"]) if side == "LONG" else (price <= t["tp1"])
        hit_tpf = (price >= t["tpf"]) if side == "LONG" else (price <= t["tpf"])
        hit_sl = (price <= t["sl"]) if side == "LONG" else (price >= t["sl"])

        # ---- Liquidations-Check (Isolated Margin): hat Vorrang vor allem ----
        liq = t.get("liq_price")
        if liq and qty_rem > 0:
            hit_liq = (price <= liq) if side == "LONG" else (price >= liq)
            if hit_liq:
                fee = exit_fee(qty_rem, liq)
                realized += pnl(qty_rem, liq) - fee
                fees_paid += fee
                margin = float(t.get("max_capital") or 0)
                if margin > 0 and realized < -margin:
                    realized = -margin  # Verlust maximal = eingesetzte Marge
                events.append(f"LIQUIDATION @ {liq} (Marge verloren)")
                updates.update({
                    "fees_paid": round(fees_paid, 6),
                    "realized_pnl": round(realized, 6),
                    "qty_remaining": 0, "events": events[-20:],
                    "status": "closed", "exit_price": liq, "result": "loss",
                    "liquidated": True,
                    "closed_at": datetime.now(timezone.utc).isoformat(),
                })
                if t["mode"] == "live" and self.client.configured():
                    await self._live_flash_close(t, 0)
                logger.info(f"AutoTrade LIQUIDATION {t['symbol']} pnl={updates['realized_pnl']}")
                await self.db.auto_trades.update_one({"id": t["id"]}, {"$set": updates})
                await self._after_close({**t, **updates})
                return

        # Break-Even Modus auflösen (Legacy: breakeven_enabled)
        be_mode = t.get("be_mode")
        if be_mode not in ("off", "tp1", "crv", "profit_pct", "smart"):
            be_mode = "tp1" if t.get("breakeven_enabled") else "off"
        if be_mode == "tp1" and t.get("breakeven_enabled") is False:
            be_mode = "off"

        def _be_price():
            # ECHTES Break-Even inkl. Gebühren: Entry-Fee (auf Entry-Notional)
            # UND Exit-Fee (auf BE-Notional) müssen gedeckt sein.
            # LONG:  be*(1-fee) = entry*(1+fee)  ->  be = entry*(1+fee)/(1-fee)
            # SHORT: entry*(1-fee) = be*(1+fee)  ->  be = entry*(1-fee)/(1+fee)
            fee = t.get("fee_percent", 0.06) / 100
            be = t["entry"] * (1 + fee) / (1 - fee) if side == "LONG" \
                else t["entry"] * (1 - fee) / (1 + fee)
            return round(be, 6)

        # TP1 partial + break-even
        if not t.get("tp1_hit") and hit_tp1 and not hit_tpf:
            close_qty = round(t["qty"] * t["tp1_close_percent"] / 100, 6)
            fee = exit_fee(close_qty, t["tp1"])
            realized += pnl(close_qty, t["tp1"]) - fee
            fees_paid += fee
            qty_rem = round(qty_rem - close_qty, 6)
            events.append(f"TP1 hit @ {t['tp1']} closed {t['tp1_close_percent']}% (Fee {round(fee, 6)})")
            updates["tp1_hit"] = True
            be_price = None
            if be_mode in ("tp1", "smart") and not t.get("breakeven_moved"):
                # smart nutzt live als Fallback ebenfalls Entry+Gebühren
                # (Swing-Struktur wird im Backtester exakt simuliert)
                be_price = _be_price()
                updates["sl"] = be_price
                updates["breakeven_moved"] = True
                events.append(f"SL -> Break-Even @ {be_price} ({be_mode})")
            # Live sync: only flash-close if the exchange TP1 was NOT placed
            # (otherwise Bitunix already closed 50%, no need to close again).
            # Then push the break-even SL to the exchange so it survives even
            # if our backend restarts.
            if t.get("mode") == "live" and self.client.configured():
                if not t.get("tp1_exchange_placed"):
                    await self._live_partial_close(t, close_qty)
                if be_price is not None and t.get("bitunix_position_id"):
                    ok_be = await self._live_move_sl(t, be_price, qty_rem)
                    if ok_be:
                        events.append("Exchange SL -> BE synced")
                    else:
                        events.append("Exchange SL move FAILED (local only)")

        # ATR trailing stop after TP1 -> lock profit while letting the runner breathe
        if (t.get("tp1_hit") or updates.get("tp1_hit")):
            cfg2 = self.coin_cfg(t["symbol"])
            atr = t.get("atr") or 0
            if cfg2.get("trail_after_tp1", True) and atr > 0:
                cur_sl = updates.get("sl", t["sl"])
                mult = float(cfg2.get("trail_atr_mult", 1.5))
                trailed = None
                if side == "LONG":
                    new_sl = round(price - atr * mult, 6)
                    if new_sl > cur_sl:
                        updates["sl"] = new_sl
                        trailed = new_sl
                        events.append(f"TRAIL SL -> {new_sl}")
                else:
                    new_sl = round(price + atr * mult, 6)
                    if new_sl < cur_sl:
                        updates["sl"] = new_sl
                        trailed = new_sl
                        events.append(f"TRAIL SL -> {new_sl}")
                # Sync trailed SL to the exchange too so a backend restart
                # can't leave us protected only by the original SL.
                if trailed is not None and t.get("mode") == "live":
                    await self._live_move_sl(t, trailed, qty_rem)

        # Break-Even bei frei wählbarem CRV oder Gewinn-% (vor TP1 möglich)
        if not closed and qty_rem > 0 and be_mode in ("crv", "profit_pct") \
                and not (t.get("breakeven_moved") or updates.get("breakeven_moved")):
            risk = float(t.get("risk") or abs(t["entry"] - t.get("initial_sl", t["sl"])) or 0)
            trigger = False
            if be_mode == "crv" and risk > 0:
                crv = float(t.get("be_trigger_crv", 1.0) or 1.0)
                target = t["entry"] + risk * crv if side == "LONG" else t["entry"] - risk * crv
                trigger = price >= target if side == "LONG" else price <= target
            elif be_mode == "profit_pct":
                margin = float(t.get("max_capital") or 0)
                thr = float(t.get("be_trigger_profit_pct", 30.0) or 30.0)
                trigger = margin > 0 and thr > 0 and pnl(qty_rem, price) / margin * 100 >= thr
            if trigger:
                be_p = _be_price()
                cur = updates.get("sl", t["sl"])
                improved = (side == "LONG" and be_p > cur) or (side == "SHORT" and be_p < cur)
                if improved:
                    updates["sl"] = be_p
                updates["breakeven_moved"] = True
                events.append(f"SL -> Break-Even @ {be_p} ({be_mode})")
                # Exchange-SL nur syncen, wenn BE den SL wirklich verbessert –
                # sonst würde ein bereits besserer (getrailter) SL an der Börse
                # wieder verschlechtert werden.
                if improved and t.get("mode") == "live":
                    await self._live_move_sl(t, be_p, qty_rem)

        # Gewinnsicherung: SL in den Gewinn ziehen sobald Trigger erreicht
        if not closed and qty_rem > 0 and t.get("profit_secure_enabled") \
                and not t.get("profit_secured"):
            margin = float(t.get("max_capital") or 0)
            trig = float(t.get("profit_secure_trigger_pct", 30.0) or 30.0)
            lock = max(0.0, min(float(t.get("profit_lock_pct", 50.0) or 50.0), 95.0)) / 100
            unreal = pnl(qty_rem, price)
            if margin > 0 and trig > 0 and unreal / margin * 100 >= trig:
                new_sl = round(t["entry"] + (price - t["entry"]) * lock, 6) if side == "LONG" \
                    else round(t["entry"] - (t["entry"] - price) * lock, 6)
                cur = updates.get("sl", t["sl"])
                if (side == "LONG" and new_sl > cur) or (side == "SHORT" and new_sl < cur):
                    updates["sl"] = new_sl
                    events.append(f"GEWINNSICHERUNG: SL -> {new_sl} "
                                  f"(+{round(unreal / margin * 100, 1)}% auf Marge, "
                                  f"{int(lock * 100)}% gesichert)")
                    if t.get("mode") == "live":
                        await self._live_move_sl(t, new_sl, qty_rem)
                updates["profit_secured"] = True

        # Snapshot vor dem Exit: wird gebraucht, falls der Live-Close an der
        # Börse scheitert und der Trade offen bleiben muss (kein Doppelbuchen).
        pre_exit = {"realized": realized, "fees": fees_paid, "qty_rem": qty_rem}

        # Full TP
        if hit_tpf and qty_rem > 0:
            fee = exit_fee(qty_rem, t["tpf"])
            realized += pnl(qty_rem, t["tpf"]) - fee
            fees_paid += fee
            events.append(f"TP FULL hit @ {t['tpf']} (Fee {round(fee, 6)})")
            closed, exit_price, qty_rem = True, t["tpf"], 0

        # Stop loss (re-read possibly moved SL)
        cur_sl = updates.get("sl", t["sl"])
        hit_sl = (price <= cur_sl) if side == "LONG" else (price >= cur_sl)
        if not closed and hit_sl and qty_rem > 0:
            fee = exit_fee(qty_rem, cur_sl)
            realized += pnl(qty_rem, cur_sl) - fee
            fees_paid += fee
            is_be = t.get("breakeven_moved") or updates.get("breakeven_moved")
            events.append(f"{'BREAK-EVEN' if is_be else 'STOP'} hit @ {cur_sl} (Fee {round(fee, 6)})")
            closed, exit_price, qty_rem = True, cur_sl, 0

        # Klassifizierung anhand netto realisiertem PnL (Gebühren bereits abgezogen):
        # Alles > 0 = Win, alles < 0 = Loss, ~0 = Break-Even.
        if closed:
            eps = 1e-6
            if realized > eps:
                result = "win"
            elif realized < -eps:
                result = "loss"
            else:
                result = "breakeven"

        updates["fees_paid"] = round(fees_paid, 6)
        updates["realized_pnl"] = round(realized, 6)
        updates["qty_remaining"] = qty_rem
        updates["events"] = events[-20:]
        if closed:
            live_close_ok = True
            if t["mode"] == "live" and self.client.configured():
                res = await self._live_flash_close(t, qty_rem)
                if not res.get("ok"):
                    # Position evtl. extern bereits geschlossen (TP/SL/Bitunix-
                    # App) -> sofort als extern geschlossen verbuchen statt
                    # in einer CLOSE-Fehlerschleife zu hängen.
                    booked = await self._reconcile_close_error(t, res.get("detail"))
                    if booked:
                        return
                    # Position an der Börse ist NICHT zu -> Trade lokal offen
                    # lassen und beim nächsten Monitor-Tick erneut versuchen.
                    attempts = int(t.get("live_close_attempts", 0) or 0) + 1
                    events.append(f"CLOSE-VERSUCH {attempts} fehlgeschlagen (Börse): "
                                  f"{res.get('detail')}")
                    updates.update({"live_close_attempts": attempts,
                                    "live_close_failed": True,
                                    "live_close_error": res.get("detail"),
                                    "events": events[-20:]})
                    if attempts == 1:
                        await self._notify_reject(
                            t["symbol"], t["side"],
                            f"Exit konnte an der Börse nicht ausgeführt werden: "
                            f"{res.get('detail')} – Position läuft weiter!")
                    # Restmenge bleibt offen, damit der nächste Tick erneut schließt.
                    updates["qty_remaining"] = pre_exit["qty_rem"]
                    updates["realized_pnl"] = round(pre_exit["realized"], 6)
                    updates["fees_paid"] = round(pre_exit["fees"], 6)
                    live_close_ok = attempts >= 5
                    if live_close_ok:
                        logger.error(f"AutoTrade {t['id']}: Live-Close nach {attempts} "
                                     f"Versuchen aufgegeben – Trade wird lokal geschlossen")
                        updates["qty_remaining"] = qty_rem
                        updates["realized_pnl"] = round(realized, 6)
                        updates["fees_paid"] = round(fees_paid, 6)
                else:
                    updates["live_close_failed"] = False
                    events.append(str(res.get("detail")))
                    updates["events"] = events[-20:]
            if live_close_ok:
                updates["status"] = "closed"
                updates["exit_price"] = exit_price
                updates["result"] = result
                updates["closed_at"] = datetime.now(timezone.utc).isoformat()
                logger.info(f"AutoTrade CLOSE {t['symbol']} {result} "
                            f"pnl={updates['realized_pnl']}")

        await self.db.auto_trades.update_one({"id": t["id"]}, {"$set": updates})
        if updates.get("status") == "closed":
            await self._after_close({**t, **updates})

    async def _after_close(self, t: Dict):
        """Nach jedem Close: Kill-Switch-Zähler + Telegram-Meldung (Toggle)."""
        # Fix 0.5: Ergebnis-Wahrheit vereinheitlichen. Der Trade kennt das
        # KANONISCHE Ergebnis (Vorzeichen realized_pnl inkl. Fees) -> ans
        # verknüpfte Signal zurückschreiben. TP1-Touch-Labels (pipeline.
        # evaluate_open_signals) dürfen das nie mehr überschreiben.
        try:
            if t.get("signal_id") and t.get("result"):
                await self.db.signals.update_one(
                    {"id": t["signal_id"]},
                    {"$set": {"result": t["result"], "status": "closed",
                              "result_source": "trade_pnl",
                              "trade_id": t.get("id"),
                              "result_ts": t.get("closed_at")
                              or datetime.now(timezone.utc).isoformat()}})
        except Exception as e:
            logger.warning(f"signal result sync (trade_pnl) failed: {e}")
        try:
            from services import notifications, trade_guard
            pnl = float(t.get("realized_pnl") or 0)
            cap = float(t.get("max_capital") or 0)
            pct = f" ({round(pnl / cap * 100, 2):+}%)" if cap else ""
            emoji = "✅" if t.get("result") == "win" else ("⚪" if t.get("result") == "breakeven" else "❌")
            await notifications.telegram_notify(
                self.db, self.telegram, "trade_closed",
                f"{emoji} *TRADE GESCHLOSSEN* ({str(t.get('mode', '')).upper()})\n"
                f"💰 {t['symbol']} · {t['side']} · {t.get('strategy_name') or t.get('strategy_id')}\n"
                f"Ergebnis: {t.get('result')} · PnL `{round(pnl, 4)} USDT`{pct}")
            try:
                from services import ai_rewards
                await ai_rewards.on_trade_closed(self.db, t)
            except Exception as re_:
                logger.warning(f"reward hook failed: {re_}")
            await trade_guard.on_trade_closed(self.db, self.telegram, t)
        except Exception as e:
            logger.warning(f"after_close hook failed: {e}")

    # ------------------------------------------------------------------
    # Live-Ausführung: eine gemeinsame, VERIFIZIERTE Quelle für alle
    # Schließ-/Anpassungs-Wege (Monitor, manuelles UI, KI-Trade-Manager).
    # Vorher wurden flash_close-Antworten ignoriert -> die Website zeigte den
    # Trade als geschlossen, an der Börse lief die Position weiter.
    # ------------------------------------------------------------------
    async def _resolve_position(self, t: Dict, refresh: bool = False) -> Optional[str]:
        """positionId der Live-Position (bei Bedarf nachladen und persistieren)."""
        pid = t.get("bitunix_position_id")
        if pid and not refresh:
            return str(pid)
        try:
            pid = await self.client.resolve_position_id(t["symbol"], t["side"])
        except Exception as e:
            logger.error(f"_resolve_position({t.get('symbol')}) failed: {e}")
            return t.get("bitunix_position_id")
        if pid:
            t["bitunix_position_id"] = str(pid)
            try:
                await self.db.auto_trades.update_one(
                    {"id": t["id"]}, {"$set": {"bitunix_position_id": str(pid)}})
            except Exception:
                pass
            return str(pid)
        return t.get("bitunix_position_id")

    async def _live_position_qty(self, t: Dict) -> Optional[float]:
        """Offene Menge der Position an der Börse. None = nicht ermittelbar."""
        try:
            res = await self.client.get_positions(t["symbol"])
        except Exception as e:
            logger.warning(f"_live_position_qty({t.get('symbol')}) failed: {e}")
            return None
        data = res.get("data") if isinstance(res, dict) else None
        rows = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
        want = str(t.get("side", "")).upper()
        total = 0.0
        matched = 0
        found = False
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_side = str(row.get("side") or row.get("positionSide") or "").upper()
            if row_side in ("BUY", "LONG") and want != "LONG":
                continue
            if row_side in ("SELL", "SHORT") and want != "SHORT":
                continue
            matched += 1
            for key in ("qty", "positionAmt", "amount", "size", "total", "available"):
                if row.get(key) not in (None, ""):
                    try:
                        total += abs(float(row[key]))
                        found = True
                        break
                    except (TypeError, ValueError):
                        continue
        if isinstance(data, list) and matched == 0:
            # Keine Position in unserer Richtung mehr offen
            return 0.0
        return total if found else None

    async def close_live_position(self, t: Dict, qty: float, full: bool = True) -> Dict:
        """Position an der Börse wirklich schließen – mit positionId-Auflösung,
        Retry und Verifikation. Rückgabe: {"ok": bool, "detail": str}."""
        if t.get("mode") != "live" or not self.client.configured():
            return {"ok": True, "detail": "kein Live-Trade"}
        last = "unbekannter Fehler"
        for attempt in (1, 2):
            pid = await self._resolve_position(t, refresh=(attempt == 2))
            if not pid:
                remaining = await self._live_position_qty(t)
                if remaining is not None and remaining <= 1e-12:
                    return {"ok": True, "detail": "Position existiert an der Börse nicht mehr"}
                last = "positionId an der Börse nicht gefunden"
                await asyncio.sleep(1.0)
                continue
            # BUGFIX ('Insufficient amount'): lokale qty_remaining kann größer
            # sein als die echte Börsen-Menge (Rundung, externe Teil-Closes,
            # TP1-Fills). Vor dem Close die reale Menge abgleichen.
            live_qty = await self._live_position_qty(t)
            if live_qty is not None:
                if live_qty <= 1e-12:
                    return {"ok": True,
                            "detail": "Position existiert an der Börse nicht mehr"}
                # BUGFIX ('Insufficient amount'-Endlosschleife, Bug-Report DOT):
                # An der Börse liegt nur noch Dust unter dem handelbaren
                # Minimum (z.B. 0.08 USDT Rest) – jeder Close wird abgelehnt.
                # Solche Reste gelten als geschlossen statt 5x zu scheitern.
                min_qty = 0.0
                try:
                    b_sym = self.client.to_bitunix_symbol(t["symbol"])
                    min_qty = float((self.client.contract_meta(b_sym) or {})
                                    .get("min_qty") or 0)
                except Exception:
                    min_qty = 0.0
                if min_qty > 0 and live_qty < min_qty:
                    logger.info(f"{t.get('symbol')}: Börsen-Rest {live_qty} unter "
                                f"Minimum {min_qty} (Dust) – gilt als geschlossen")
                    return {"ok": True,
                            "detail": f"Restmenge {live_qty} unter Börsen-Minimum "
                                      f"{min_qty} (Dust) – Position gilt als geschlossen"}
                if qty > live_qty:
                    # Nie mehr schließen als real offen ist; bei mehreren lokalen
                    # Trades auf einer Börsen-Position max. die eigene Menge.
                    logger.info(f"{t.get('symbol')}: Close-Menge {qty} auf reale "
                                f"Börsen-Menge {live_qty} begrenzt")
                    qty = live_qty
            try:
                res = await self.client.flash_close(t["symbol"], pid, t["side"], qty)
            except Exception as e:
                last = f"Exception: {str(e)[:140]}"
                await asyncio.sleep(1.0)
                continue
            ok = isinstance(res, dict) and res.get("code") == 0
            if ok:
                if not full:
                    return {"ok": True, "detail": "Börse: Teil-Close ausgeführt", "response": res}
                # Erwartete Restmenge: 0, außer weitere lokale Trades teilen sich
                # dieselbe Börsen-Position (live_qty > eigene Menge).
                expected_left = max((live_qty or qty) - qty, 0.0)
                verified = await self._verify_flat(t, expected_left=expected_left)
                if verified["ok"]:
                    return {"ok": True, "detail": verified["detail"], "response": res}
                last = verified["detail"]
            else:
                last = (isinstance(res, dict) and (res.get("msg") or str(res)[:140])) \
                    or "unbekannte Börsen-Antwort"
                logger.error(f"flash_close {t.get('symbol')} rejected: {res}")
            await asyncio.sleep(1.0)
        logger.error(f"Live-Close FEHLGESCHLAGEN {t.get('symbol')} ({t.get('id')}): {last}")
        return {"ok": False, "detail": last}

    async def _verify_flat(self, t: Dict, expected_left: float = 0.0) -> Dict:
        """Prüft an der Börse nach, ob die (eigene) Menge wirklich weg ist."""
        remaining = None
        tol = max(float(expected_left), 0.0) + 1e-12
        for _ in range(3):
            await asyncio.sleep(0.8)
            remaining = await self._live_position_qty(t)
            if remaining is None:
                return {"ok": True, "detail": "Börse: Order akzeptiert "
                                              "(Positions-API liefert keine Menge)"}
            if remaining <= tol:
                return {"ok": True, "detail": "Börse: Position geschlossen (verifiziert)"}
        return {"ok": False,
                "detail": f"Börse meldet weiterhin eine offene Menge ({remaining})"}

    async def _cancel_position_tpsl(self, t: Dict, pid: str) -> int:
        """Alle offenen TP/SL-Orders der Position stornieren (gegen Duplikate).

        BUGFIX: Vorher wurde bei jedem SL/TP-Update eine NEUE TP/SL-Order
        platziert, wenn das Modify scheiterte oder keine Order-ID bekannt war –
        an der Börse stapelten sich identische SL-Orders, deren Mengen in Summe
        die Position überstiegen ('TP/SL amount must be less than the size of
        the position')."""
        cancelled = 0
        try:
            res = await self.client.get_pending_tpsl(t["symbol"], position_id=pid)
        except Exception as e:
            logger.warning(f"_cancel_position_tpsl({t.get('symbol')}) list failed: {e}")
            return 0
        data = res.get("data") if isinstance(res, dict) else None
        if isinstance(data, dict):
            data = data.get("orderList") or data.get("list") or data.get("rows") or [data]
        rows = data if isinstance(data, list) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            oid = row.get("id") or row.get("orderId")
            if not oid:
                continue
            try:
                c = await self.client.cancel_tpsl_order(t["symbol"], oid)
                if isinstance(c, dict) and c.get("code") == 0:
                    cancelled += 1
                else:
                    logger.info(f"cancel_tpsl_order {t.get('symbol')}/{oid}: {c}")
            except Exception as e:
                logger.warning(f"cancel_tpsl_order {t.get('symbol')}/{oid} failed: {e}")
        if cancelled:
            logger.info(f"{t.get('symbol')}: {cancelled} alte TP/SL-Order(s) storniert")
        return cancelled

    async def sync_live_levels(self, t: Dict, sl: Optional[float] = None,
                               tp: Optional[float] = None) -> Dict:
        """SL und/oder TP der Live-Position an der Börse aktualisieren.

        Bevorzugt wird die bestehende TP/SL-Order modifiziert (kein Duplikat).
        Scheitert das, werden zuerst ALLE alten TP/SL-Orders der Position
        storniert und dann EINE neue gesetzt – so können sich keine doppelten
        SL-Orders mehr ansammeln. Die Menge wird auf die reale Börsen-Menge
        begrenzt (Fix für 'TP/SL amount must be less than the size of the
        position')."""
        if t.get("mode") != "live" or not self.client.configured():
            return {"ok": True, "detail": "kein Live-Trade"}
        pid = await self._resolve_position(t)
        if not pid:
            return {"ok": False, "detail": "positionId an der Börse nicht gefunden"}
        qty_rem = float(t.get("qty_remaining", t.get("qty", 0)) or 0)
        # Reale Börsen-Menge hat Vorrang vor der lokalen Buchhaltung.
        live_qty = await self._live_position_qty(t)
        if live_qty is not None:
            if live_qty <= 1e-12:
                return {"ok": False, "detail": "Position existiert an der Börse nicht mehr"}
            if qty_rem <= 0 or qty_rem > live_qty:
                logger.info(f"{t.get('symbol')}: TP/SL-Menge {qty_rem} auf reale "
                            f"Börsen-Menge {live_qty} korrigiert")
                qty_rem = live_qty
        existing = t.get("bitunix_tpsl_order_id")
        last = "abgelehnt"
        if existing or pid:
            try:
                # Modify läuft über die positionId (Bitunix-Doku), nicht über
                # die TP/SL-Order-ID – vorher wurde deshalb jede Anpassung mit
                # 'Please set at least one of TP/Stop Loss' abgelehnt.
                res = await self.client.modify_position_tp_sl(
                    t["symbol"], pid, tp_price=tp, sl_price=sl, side=t["side"])
                if isinstance(res, dict) and res.get("code") == 0:
                    return {"ok": True, "detail": "Börse: SL/TP geändert", "response": res}
                last = (isinstance(res, dict) and (res.get("msg") or str(res)[:120])) or last
                logger.info(f"modify_position_tp_sl {t.get('symbol')} fehlgeschlagen "
                            f"({last}) – setze neue TP/SL-Order")
            except Exception as e:
                last = f"Exception: {str(e)[:120]}"
        # Alte Orders wegräumen, damit sich keine SL-Duplikate stapeln und die
        # Mengen-Summe die Position nicht mehr übersteigt.
        await self._cancel_position_tpsl(t, pid)
        # Nach dem Aufräumen BEIDE Seiten neu setzen: die nicht angefragte Seite
        # kommt aus dem Trade, damit die Position nie ohne SL bzw. TP dasteht.
        eff_sl = sl if sl is not None else (float(t.get("sl") or 0) or None)
        eff_tp = tp if tp is not None else (float(t.get("tpf") or 0) or None)
        res = None
        for use_qty in (True, False):
            try:
                res = await self.client.place_position_tp_sl(
                    t["symbol"], pid, t["side"],
                    tp_price=eff_tp, tp_qty=(qty_rem if (eff_tp and use_qty) else None),
                    sl_price=eff_sl, sl_qty=(qty_rem if (eff_sl and use_qty) else None))
            except Exception as e:
                return {"ok": False, "detail": f"Exception: {str(e)[:140]}"}
            if isinstance(res, dict) and res.get("code") == 0:
                break
            msg = (isinstance(res, dict) and (res.get("msg") or "")) or ""
            # Letzter Ausweg: ohne Mengen-Angabe (gilt für die ganze Position)
            if use_qty and "size of the position" in str(msg).lower():
                logger.info(f"{t.get('symbol')}: TP/SL mit Menge abgelehnt ({msg}) "
                            "– Retry ohne Mengen-Angabe (ganze Position)")
                continue
            break
        if isinstance(res, dict) and res.get("code") == 0:
            new_id = _extract_order_id(res)
            if new_id:
                try:
                    await self.db.auto_trades.update_one(
                        {"id": t["id"]}, {"$set": {"bitunix_tpsl_order_id": new_id}})
                    t["bitunix_tpsl_order_id"] = new_id
                except Exception:
                    pass
            return {"ok": True, "detail": "Börse: SL/TP aktualisiert", "response": res}
        detail = (isinstance(res, dict) and (res.get("msg") or str(res)[:140])) or last
        logger.warning(f"sync_live_levels {t.get('symbol')} rejected: {res}")
        return {"ok": False, "detail": detail}

    async def _live_partial_close(self, t, qty):
        res = await self.close_live_position(t, qty, full=False)
        return bool(res.get("ok"))

    async def _live_move_sl(self, t, new_sl_price: float, qty_rem: float) -> bool:
        """Neuen SL für die Restposition an Bitunix schicken (Break-Even, Trailing).
        Delegiert an `sync_live_levels` (löst die positionId bei Bedarf nach)."""
        res = await self.sync_live_levels(t, sl=new_sl_price)
        if not res.get("ok"):
            logger.warning(f"_live_move_sl {t.get('symbol')} -> {new_sl_price}: "
                           f"{res.get('detail')}")
        return bool(res.get("ok"))

    async def _live_flash_close(self, t, qty):
        """Voll-Close an der Börse (verifiziert). Rückgabe: Ergebnis-Dict."""
        return await self.close_live_position(
            t, qty or t.get("qty_remaining") or t.get("qty"), full=True)

    async def manual_close(self, trade_id: str, price: float):
        """Trade schließen (manuell oder durch die KI).

        WICHTIG: Bei Live-Trades wird die Position an der Börse ZUERST wirklich
        geschlossen und verifiziert. Schlägt das fehl, bleibt der Trade offen und
        es wird ein Fehler zurückgegeben – vorher wurde er lokal als geschlossen
        markiert, während die Bitunix-Position weiterlief."""
        t = await self.db.auto_trades.find_one({"id": trade_id, "status": "open"})
        if not t:
            return None
        side = t["side"]
        qty_rem = t.get("qty_remaining", t["qty"])
        live_note = ""
        if t["mode"] == "live" and self.client.configured():
            res = await self._live_flash_close(t, qty_rem)
            if not res.get("ok"):
                # Position existiert an der Börse evtl. gar nicht mehr
                # (extern/TP/SL geschlossen) -> sofort abgleichen statt den
                # Trade fälschlich offen zu lassen.
                booked = await self._reconcile_close_error(t, res.get("detail"))
                if booked:
                    return {**booked, "live_verified": True, "external": True,
                            "note": "Position war an der Börse bereits geschlossen – "
                                    "Trade wurde als extern geschlossen verbucht."}
                await self.db.auto_trades.update_one({"id": trade_id}, {"$set": {
                    "live_close_failed": True,
                    "live_close_error": res.get("detail"),
                    "events": (t.get("events", []) +
                               [f"CLOSE FEHLGESCHLAGEN (Börse): {res.get('detail')}"])[-20:]}})
                await self._notify_reject(
                    t["symbol"], side,
                    f"Live-Position konnte NICHT geschlossen werden: {res.get('detail')}")
                return {"error": "Live-Position konnte an der Börse nicht geschlossen werden: "
                                 f"{res.get('detail')}. Der Trade bleibt offen – bitte erneut "
                                 "versuchen oder direkt bei Bitunix prüfen."}
            live_note = f" [{res.get('detail')}]"
        fee_pct = float(t.get("fee_percent", 0.06)) / 100
        fee = qty_rem * price * fee_pct
        pnl = (price - t["entry"]) * qty_rem if side == "LONG" else (t["entry"] - price) * qty_rem
        realized = round(t.get("realized_pnl", 0.0) + pnl - fee, 6)
        result = "win" if realized > 0 else ("breakeven" if realized == 0 else "loss")
        await self.db.auto_trades.update_one({"id": trade_id}, {"$set": {
            "status": "closed", "exit_price": price, "result": result,
            "realized_pnl": realized, "qty_remaining": 0,
            "fees_paid": round(float(t.get("fees_paid", 0.0)) + fee, 6),
            "live_close_failed": False,
            "closed_at": datetime.now(timezone.utc).isoformat(),
            "events": (t.get("events", []) +
                       [f"MANUAL CLOSE @ {price} (Fee {round(fee, 6)}){live_note}"])[-20:]}})
        await self._after_close({**t, "status": "closed", "result": result,
                                 "realized_pnl": realized, "exit_price": price,
                                 "closed_at": datetime.now(timezone.utc).isoformat()})
        return {"result": result, "realized_pnl": realized,
                "live_verified": bool(live_note) or t["mode"] != "live"}

    # ------------------------------------------------------------------
    # Trade-Steuerung im laufenden Trade (Teil-Close, SL/TP, Margin, Hebel).
    # Wird von der manuellen Bedienung UND vom KI-Trade-Manager
    # (services/ai_trade_manager.py) genutzt – eine Quelle für die Logik.
    # ------------------------------------------------------------------
    @staticmethod
    def liq_price_for(side: str, entry: float, leverage: float,
                      mmr_percent: float = 0.5) -> float:
        """Liquidationspreis für Entry/Hebel (gleiche Formel wie beim Öffnen)."""
        mmr = float(mmr_percent) / 100
        liq_dist = max(1.0 / max(float(leverage), 0.01) - mmr, 0.0005)
        return round(entry * (1 - liq_dist) if side == "LONG"
                     else entry * (1 + liq_dist), 6)

    async def _open_trade(self, trade_id: str) -> Optional[Dict]:
        return await self.db.auto_trades.find_one({"id": trade_id, "status": "open"})

    async def partial_close(self, trade_id: str, percent: float,
                            price: Optional[float] = None) -> Optional[Dict]:
        """Teilweise schließen (percent = 1..99 der RESTMENGE)."""
        t = await self._open_trade(trade_id)
        if not t:
            return None
        pct = max(1.0, min(99.0, float(percent)))
        qty_rem = float(t.get("qty_remaining", t["qty"]))
        price = float(price or await self._current_mark(t["symbol"]) or t["entry"])
        qty = round(qty_rem * pct / 100, 8)
        if qty <= 0:
            return {"error": "Menge zu klein"}
        left = round(qty_rem - qty, 8)
        dust_note = None
        # Dust-Schutz (Live): würde eine Restmenge unter dem Börsen-Minimum übrig
        # bleiben, wird stattdessen komplett geschlossen – sonst bleiben verwaiste
        # Cent-Positionen zurück, die niemand mehr schließen kann.
        if left > 0 and t["mode"] == "live" and self.client.configured():
            try:
                b_sym = self.client.to_bitunix_symbol(t["symbol"])
                min_qty = float((self.client.contract_meta(b_sym) or {})
                                .get("min_qty") or 0)
            except Exception:
                min_qty = 0.0
            if min_qty > 0 and left < min_qty:
                dust_note = (f"Restmenge {left} wäre unter Börsen-Minimum {min_qty} "
                             f"(Dust) – Trade wird komplett geschlossen")
                qty = qty_rem
                left = 0.0
        fee = qty * price * (float(t.get("fee_percent", 0.06)) / 100)
        pnl = ((price - t["entry"]) if t["side"] == "LONG" else (t["entry"] - price)) * qty
        realized = round(float(t.get("realized_pnl", 0.0)) + pnl - fee, 6)
        if t["mode"] == "live" and self.client.configured():
            res = await self.close_live_position(t, qty, full=(left <= 0))
            if not res.get("ok"):
                booked = await self._reconcile_close_error(t, res.get("detail"))
                if booked:
                    return {"closed_qty": 0, "qty_remaining": 0, "external": True,
                            **booked,
                            "note": "Position war an der Börse bereits geschlossen – "
                                    "Trade wurde komplett als extern geschlossen verbucht."}
                await self.db.auto_trades.update_one({"id": trade_id}, {"$set": {
                    "events": (t.get("events", []) +
                               [f"PARTIAL CLOSE FEHLGESCHLAGEN (Börse): "
                                f"{res.get('detail')}"])[-20:]}})
                return {"error": "Teil-Close an der Börse fehlgeschlagen: "
                                 f"{res.get('detail')} – Trade unverändert."}
        upd = {
            "qty_remaining": left, "realized_pnl": realized,
            "fees_paid": round(float(t.get("fees_paid", 0.0)) + fee, 6),
            "events": (t.get("events", []) +
                       [f"PARTIAL CLOSE {pct:.0f}% ({qty}) @ {price} "
                        f"(PnL {round(pnl - fee, 4)})"]
                       + ([dust_note] if dust_note else []))[-20:]}
        if left <= 0:
            upd.update({"status": "closed", "exit_price": price,
                        "result": "win" if realized > 0 else
                                  ("loss" if realized < 0 else "breakeven"),
                        "closed_at": datetime.now(timezone.utc).isoformat()})
        await self.db.auto_trades.update_one({"id": trade_id}, {"$set": upd})
        if left <= 0:
            await self._after_close({**t, **upd})
        return {"closed_qty": qty, "qty_remaining": left, "price": price,
                "realized_pnl": realized,
                **({"note": dust_note} if dust_note else {})}

    async def adjust_levels(self, trade_id: str, sl: Optional[float] = None,
                            tp1: Optional[float] = None,
                            tpf: Optional[float] = None) -> Optional[Dict]:
        """SL / TP1 / Final-TP im laufenden Trade verschieben."""
        t = await self._open_trade(trade_id)
        if not t:
            return None
        price = float(await self._current_mark(t["symbol"]) or t["entry"])
        long_side = t["side"] == "LONG"
        updates: Dict = {}
        events: List[str] = []
        if sl is not None:
            sl = float(sl)
            if (long_side and sl >= price) or (not long_side and sl <= price):
                return {"error": f"SL {sl} liegt auf der falschen Seite des Preises {price}"}
            updates["sl"] = round(sl, 8)
            events.append(f"SL {t.get('sl')} -> {round(sl, 8)}")
        for key, val in (("tp1", tp1), ("tpf", tpf)):
            if val is None:
                continue
            val = float(val)
            if (long_side and val <= price) or (not long_side and val >= price):
                return {"error": f"{key.upper()} {val} liegt auf der falschen "
                                 f"Seite des Preises {price}"}
            updates[key] = round(val, 8)
            events.append(f"{key.upper()} {t.get(key)} -> {round(val, 8)}")
        if not updates:
            return {"error": "Keine Level angegeben"}
        # Live: neue Level ZUERST an die Börse schicken. Ohne diesen Schritt
        # stimmte nur die Website-Anzeige, die echte Order behielt ihre alten
        # SL/TP-Werte.
        if t["mode"] == "live" and self.client.configured():
            exch_tp = updates.get("tpf") or updates.get("tp1")
            res = await self.sync_live_levels(t, sl=updates.get("sl"), tp=exch_tp)
            if not res.get("ok"):
                booked = await self._reconcile_close_error(t, res.get("detail"))
                if booked:
                    return {"external": True, **booked,
                            "note": "Position existiert an der Börse nicht mehr – "
                                    "Trade wurde als extern geschlossen verbucht, "
                                    "Level-Anpassung entfällt."}
                await self.db.auto_trades.update_one({"id": trade_id}, {"$set": {
                    "events": (t.get("events", []) +
                               [f"ADJUST FEHLGESCHLAGEN (Börse): {res.get('detail')}"])[-20:]}})
                return {"error": "Börse hat die neuen Level abgelehnt: "
                                 f"{res.get('detail')} – Level unverändert."}
            events.append("Börse bestätigt")
        updates["events"] = (t.get("events", []) + ["ADJUST " + ", ".join(events)])[-20:]
        await self.db.auto_trades.update_one({"id": trade_id}, {"$set": updates})
        return {"sl": updates.get("sl", t.get("sl")), "tp1": updates.get("tp1", t.get("tp1")),
                "tpf": updates.get("tpf", t.get("tpf")), "price": price}

    async def _free_capital_ok(self, trade: Dict, extra_margin: float) -> bool:
        """Zusätzliche Margin nur aus dem freien Kapital-Kontingent (Paper & Live)."""
        try:
            alloc = await self.allocated_capital(trade.get("mode", "paper"))
            if alloc is None:
                return True
            used = await self.used_margin(trade.get("mode", "paper"))
            return (alloc - used) >= float(extra_margin)
        except Exception as e:
            logger.warning(f"_free_capital_ok failed: {e}")
            return True

    async def adjust_margin(self, trade_id: str, amount: float) -> Optional[Dict]:
        """Margin hinzufügen (amount > 0) oder entnehmen (amount < 0).
        Positionsgröße bleibt gleich -> effektiver Hebel und Liquidationspreis
        verschieben sich entsprechend."""
        t = await self._open_trade(trade_id)
        if not t:
            return None
        amount = float(amount)
        if amount == 0:
            return {"error": "Betrag 0"}
        qty_rem = float(t.get("qty_remaining", t["qty"]))
        notional = qty_rem * float(t["entry"])
        lev_now = float(t.get("leverage", 1) or 1)
        margin_now = notional / max(lev_now, 0.01)
        new_margin = margin_now + amount
        if new_margin < notional / 200:
            return {"error": "Margin zu klein (max. Hebel 200x erreicht)"}
        if new_margin > notional:
            new_margin = notional          # Hebel 1x ist das Minimum
        new_lev = round(notional / new_margin, 2)
        if amount > 0 and not await self._free_capital_ok(t, amount):
            return {"error": "Zu wenig freies Kapital für zusätzliche Margin"}
        live_note = ""
        if t["mode"] == "live" and self.client.configured():
            try:
                res = await self.client.adjust_position_margin(
                    t["symbol"], amount, position_id=t.get("bitunix_position_id"),
                    side=t["side"])
                if not (isinstance(res, dict) and res.get("code") == 0):
                    return {"error": f"Börse hat Margin-Anpassung abgelehnt: {res}"}
            except Exception as e:
                return {"error": f"Margin-Anpassung fehlgeschlagen: {str(e)[:160]}"}
            live_note = " (Börse bestätigt)"
        liq = self.liq_price_for(t["side"], float(t["entry"]), new_lev)
        await self.db.auto_trades.update_one({"id": trade_id}, {"$set": {
            "leverage": new_lev, "margin_used": round(new_margin, 4),
            "liq_price": liq,
            "events": (t.get("events", []) +
                       [f"MARGIN {'+' if amount > 0 else ''}{round(amount, 4)} USDT{live_note}: "
                        f"Hebel {lev_now}x -> {new_lev}x, Liq {liq}"])[-20:]}})
        return {"margin": round(new_margin, 4), "leverage": new_lev, "liq_price": liq}

    async def adjust_leverage(self, trade_id: str, leverage: float) -> Optional[Dict]:
        """Hebel im laufenden Trade ändern – Positionsgröße bleibt erhalten,
        die gebundene Margin ändert sich."""
        t = await self._open_trade(trade_id)
        if not t:
            return None
        new_lev = max(1.0, min(200.0, float(leverage)))
        qty_rem = float(t.get("qty_remaining", t["qty"]))
        notional = qty_rem * float(t["entry"])
        lev_now = float(t.get("leverage", 1) or 1)
        if abs(new_lev - lev_now) < 0.01:
            return {"error": "Hebel unverändert"}
        extra = notional / new_lev - notional / max(lev_now, 0.01)
        if extra > 0 and not await self._free_capital_ok(t, extra):
            return {"error": "Zu wenig freies Kapital für den niedrigeren Hebel "
                             f"(zusätzlich {round(extra, 2)} USDT Margin nötig)"}
        if t["mode"] == "live" and self.client.configured():
            try:
                res = await self.client.set_leverage(t["symbol"], int(round(new_lev)))
                if not (isinstance(res, dict) and res.get("code") == 0):
                    return {"error": f"Börse hat Hebeländerung abgelehnt: {res}"}
            except Exception as e:
                return {"error": f"Hebeländerung fehlgeschlagen: {str(e)[:160]}"}
        liq = self.liq_price_for(t["side"], float(t["entry"]), new_lev)
        await self.db.auto_trades.update_one({"id": trade_id}, {"$set": {
            "leverage": round(new_lev, 2), "margin_used": round(notional / new_lev, 4),
            "liq_price": liq,
            "events": (t.get("events", []) +
                       [f"HEBEL {lev_now}x -> {round(new_lev, 2)}x "
                        f"(Margin {round(notional / new_lev, 2)} USDT, Liq {liq})"])[-20:]}})
        return {"leverage": round(new_lev, 2), "margin": round(notional / new_lev, 4),
                "liq_price": liq}
