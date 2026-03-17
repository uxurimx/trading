"""
core/executor.py
─────────────────
Motor de ejecución de órdenes REST contra Bybit v5.

IMPORTANTE: todas las coroutines son async y deben ejecutarse
desde AsyncBridge.submit(), NUNCA directamente desde el main thread GTK.

Flujo para orden de mercado con SL/TP:
  1. place_market_bracket() → envía orden de mercado
  2. Esperar fill (WebSocket execution topic en AccountStream)
  3. set_sl_tp() → confirma SL y TP en la posición abierta
  (Bybit a veces ignora SL/TP en el create si el fill es instantáneo)
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from typing import Dict, Optional, Tuple

import aiohttp

from core.config import settings
from core.order_model import OrderRequest, OrderResult

log = logging.getLogger("qts.executor")

_BASE = "https://api-testnet.bybit.com" if settings.bybit_testnet else "https://api.bybit.com"


# ─── Instrumento cache ────────────────────────────────────────────────────────

class InstrumentInfo:
    """Cache de los filtros de tamaño de un instrumento."""
    __slots__ = ("min_qty", "qty_step", "min_notional", "max_leverage")

    def __init__(self, min_qty=1.0, qty_step=1.0, min_notional=5.0, max_leverage=100):
        self.min_qty      = float(min_qty)
        self.qty_step     = float(qty_step)
        self.min_notional = float(min_notional)
        self.max_leverage = int(max_leverage)


# ─── Executor ─────────────────────────────────────────────────────────────────

class BybitExecutor:
    """
    Envía y gestiona órdenes en Bybit v5.
    Cachea información de instrumentos para validaciones.
    """

    # Defaults conservadores si no se puede obtener info del instrumento
    _DEFAULT_INFO = InstrumentInfo(min_qty=1.0, qty_step=1.0, min_notional=5.0)

    def __init__(self) -> None:
        self._instruments: Dict[str, InstrumentInfo] = {}
        self._hedge_mode: bool = False   # se detecta en load_all_instruments

    # ── Autenticación ─────────────────────────────────────────────────────────

    def _signed_headers(self, body_str: str) -> dict:
        """Headers HMAC-SHA256 para peticiones POST con body JSON."""
        ts  = str(int(time.time() * 1000))
        rw  = "5000"
        pre = f"{ts}{settings.bybit_api_key}{rw}{body_str}"
        sig = hmac.new(
            settings.bybit_api_secret.encode(),
            pre.encode(),
            hashlib.sha256,
        ).hexdigest()
        return {
            "X-BAPI-API-KEY":      settings.bybit_api_key,
            "X-BAPI-TIMESTAMP":    ts,
            "X-BAPI-SIGN":         sig,
            "X-BAPI-RECV-WINDOW":  rw,
            "Content-Type":        "application/json",
        }

    def _get_headers(self, qs: str) -> dict:
        """Headers para GET autenticado."""
        ts  = str(int(time.time() * 1000))
        rw  = "5000"
        pre = f"{ts}{settings.bybit_api_key}{rw}{qs}"
        sig = hmac.new(
            settings.bybit_api_secret.encode(),
            pre.encode(),
            hashlib.sha256,
        ).hexdigest()
        return {
            "X-BAPI-API-KEY":      settings.bybit_api_key,
            "X-BAPI-TIMESTAMP":    ts,
            "X-BAPI-SIGN":         sig,
            "X-BAPI-RECV-WINDOW":  rw,
        }

    async def _post(self, path: str, body: dict) -> dict:
        body_str = json.dumps(body, separators=(",", ":"))
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{_BASE}{path}",
                headers=self._signed_headers(body_str),
                data=body_str,
            ) as resp:
                return await resp.json()

    async def _get(self, path: str, params: dict) -> dict:
        qs = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{_BASE}{path}?{qs}",
                headers=self._get_headers(qs),
            ) as resp:
                return await resp.json()

    # ── Info de instrumento ───────────────────────────────────────────────────

    async def load_instrument_info(self, symbol: str) -> InstrumentInfo:
        """
        GET /v5/market/instruments-info — cachea lotSizeFilter.
        """
        if symbol in self._instruments:
            return self._instruments[symbol]
        try:
            data = await self._get("/v5/market/instruments-info", {
                "category": "linear",
                "symbol":   symbol,
            })
            items = data.get("result", {}).get("list", [])
            if not items:
                return self._DEFAULT_INFO

            it       = items[0]
            lot      = it.get("lotSizeFilter", {})
            lev      = it.get("leverageFilter", {})
            info = InstrumentInfo(
                min_qty      = lot.get("minOrderQty",      1.0),
                qty_step     = lot.get("qtyStep",          1.0),
                min_notional = lot.get("minNotionalValue", 5.0),
                max_leverage = int(float(lev.get("maxLeverage", 100))),
            )
            self._instruments[symbol] = info
            log.debug("Instrument %s: min_qty=%s step=%s", symbol, info.min_qty, info.qty_step)
            return info
        except Exception as e:
            log.warning("load_instrument_info(%s) failed: %s", symbol, e)
            return self._DEFAULT_INFO

    async def load_all_instruments(self, symbols: list) -> None:
        await asyncio.gather(*[self.load_instrument_info(s) for s in symbols])
        await self.detect_position_mode()

    async def detect_position_mode(self) -> None:
        """
        Detecta si la cuenta usa Hedge Mode (positionIdx 1/2) o One-way (0).

        Método fiable: consultar un símbolo específico.
        · One-way mode  → Bybit devuelve 1 slot  (positionIdx = 0)
        · Hedge mode    → Bybit devuelve 2 slots  (positionIdx = 1 y 2)
        """
        probe = self._instruments and next(iter(self._instruments)) or "BTCUSDT"
        try:
            data = await self._get("/v5/position/list", {
                "category": "linear",
                "symbol":   probe,
            })
            slots = data.get("result", {}).get("list", [])
            if len(slots) >= 2:
                self._hedge_mode = True
            elif len(slots) == 1:
                idx = int(float(slots[0].get("positionIdx", 0)))
                self._hedge_mode = idx > 0
            else:
                self._hedge_mode = False
            log.info("Position mode detectado: %s (slots=%d para %s)",
                     "Hedge (both-side)" if self._hedge_mode else "One-way",
                     len(slots), probe)
        except Exception as e:
            log.warning("detect_position_mode falló: %s — asumiendo One-way", e)
            self._hedge_mode = False

    def _pos_idx(self, side: str) -> int:
        """positionIdx correcto según el modo de la cuenta y el lado de la orden."""
        if not self._hedge_mode:
            return 0          # One-way mode
        return 1 if side == "Buy" else 2  # Hedge mode: 1=long, 2=short

    def get_info(self, symbol: str) -> InstrumentInfo:
        return self._instruments.get(symbol, self._DEFAULT_INFO)

    # ── Helpers de redondeo ───────────────────────────────────────────────────

    def round_qty(self, symbol: str, qty: float) -> float:
        """Redondea qty al qtyStep del instrumento (hacia abajo para evitar rechazos)."""
        info = self.get_info(symbol)
        step = info.qty_step
        if step <= 0:
            return qty
        rounded = int(qty / step) * step
        # evitar flotante raro: redondear a los decimales del step
        dec = len(str(step).rstrip("0").split(".")[-1]) if "." in str(step) else 0
        return round(rounded, dec)

    def price_precision(self, symbol: str) -> int:
        """Decimales razonables para precios (heurístico desde el precio)."""
        info = self.get_info(symbol)
        # Para precios: XRPUSDT típicamente 5 dec, BTCUSDT 1 dec
        return 5  # seguro para la mayoría

    def format_price(self, price: float) -> str:
        """Formatea precio para enviarlo a Bybit (sin notación científica)."""
        if price >= 1000:
            return f"{price:.2f}"
        if price >= 10:
            return f"{price:.4f}"
        return f"{price:.5f}"

    # ── Validaciones ─────────────────────────────────────────────────────────

    def validate_order(
        self, symbol: str, qty: float, entry_price: float
    ) -> Tuple[bool, str]:
        """Retorna (ok, reason). Verifica min_qty y min_notional."""
        info = self.get_info(symbol)
        if qty < info.min_qty:
            return False, f"qty {qty} < min_qty {info.min_qty} para {symbol}"
        notional = qty * entry_price
        if notional < info.min_notional:
            return False, f"notional ${notional:.2f} < mínimo ${info.min_notional:.2f}"
        return True, ""

    # ── Órdenes ───────────────────────────────────────────────────────────────

    async def place_market_bracket(self, req: OrderRequest) -> OrderResult:
        """
        Orden de mercado con SL y TP.
        POST /v5/order/create
        """
        ok, reason = self.validate_order(req.symbol, req.qty, req.entry_price)
        if not ok:
            return OrderResult(success=False, error_msg=reason)

        body: dict = {
            "category":    "linear",
            "symbol":      req.symbol,
            "side":        req.side,
            "orderType":   "Market",
            "qty":         str(req.qty),
            "timeInForce": "IOC",
            "positionIdx": self._pos_idx(req.side),
        }

        if req.sl_price > 0:
            body["stopLoss"]    = self.format_price(req.sl_price)
            body["slTriggerBy"] = "MarkPrice"

        if req.tp_price > 0:
            body["takeProfit"]  = self.format_price(req.tp_price)
            body["tpTriggerBy"] = "MarkPrice"

        try:
            data = await self._post("/v5/order/create", body)
            if data.get("retCode") == 0:
                result = data.get("result", {})
                log.info("Order placed: %s %s x%s  id=%s",
                         req.side, req.symbol, req.qty, result.get("orderId"))
                return OrderResult(
                    success=True,
                    order_id=result.get("orderId", ""),
                )
            # Auto-corrección: si positionIdx es incorrecto, cambiar modo y reintentar
            if data.get("retCode") == 110025 or "position idx" in data.get("retMsg", "").lower():
                self._hedge_mode = not self._hedge_mode
                log.warning("positionIdx incorrecto — cambiando a %s y reintentando",
                            "Hedge" if self._hedge_mode else "One-way")
                body["positionIdx"] = self._pos_idx(req.side)
                data = await self._post("/v5/order/create", body)
                if data.get("retCode") == 0:
                    result = data.get("result", {})
                    log.info("Order placed (retry): %s %s x%s  id=%s",
                             req.side, req.symbol, req.qty, result.get("orderId"))
                    return OrderResult(success=True, order_id=result.get("orderId", ""))
            msg = data.get("retMsg", "unknown error")
            log.warning("Order failed: %s — %s", req.symbol, msg)
            return OrderResult(success=False, error_msg=msg)
        except Exception as e:
            log.error("place_market_bracket exception: %s", e)
            return OrderResult(success=False, error_msg=str(e))

    async def place_limit_bracket(self, req: OrderRequest) -> OrderResult:
        """
        Orden límite con SL y TP.
        """
        ok, reason = self.validate_order(req.symbol, req.qty, req.price or req.entry_price)
        if not ok:
            return OrderResult(success=False, error_msg=reason)

        body: dict = {
            "category":    "linear",
            "symbol":      req.symbol,
            "side":        req.side,
            "orderType":   "Limit",
            "qty":         str(req.qty),
            "price":       self.format_price(req.price or req.entry_price),
            "timeInForce": "GTC",
            "positionIdx": self._pos_idx(req.side),
        }

        if req.sl_price > 0:
            body["stopLoss"]    = self.format_price(req.sl_price)
            body["slTriggerBy"] = "MarkPrice"
        if req.tp_price > 0:
            body["takeProfit"]  = self.format_price(req.tp_price)
            body["tpTriggerBy"] = "MarkPrice"

        try:
            data = await self._post("/v5/order/create", body)
            if data.get("retCode") == 0:
                result = data.get("result", {})
                return OrderResult(success=True, order_id=result.get("orderId", ""))
            return OrderResult(success=False, error_msg=data.get("retMsg", "error"))
        except Exception as e:
            return OrderResult(success=False, error_msg=str(e))

    async def set_sl_tp(
        self,
        symbol: str,
        sl:     float = 0.0,
        tp:     float = 0.0,
        side:   str   = "Buy",
    ) -> bool:
        """
        Modifica SL y/o TP de una posición abierta.
        POST /v5/position/trading-stop
        Pasar 0 en sl o tp para no modificarlo.
        """
        body: dict = {
            "category":    "linear",
            "symbol":      symbol,
            "positionIdx": self._pos_idx(side),
        }
        if sl > 0:
            body["stopLoss"]    = self.format_price(sl)
            body["slTriggerBy"] = "MarkPrice"
        if tp > 0:
            body["takeProfit"]  = self.format_price(tp)
            body["tpTriggerBy"] = "MarkPrice"

        if len(body) <= 3:
            return True  # nada que modificar

        try:
            data = await self._post("/v5/position/trading-stop", body)
            ok = data.get("retCode") == 0
            if not ok:
                log.warning("set_sl_tp failed: %s — %s", symbol, data.get("retMsg"))
            return ok
        except Exception as e:
            log.error("set_sl_tp exception: %s", e)
            return False

    async def close_position(
        self,
        symbol:   str,
        qty:      float,
        side:     str,          # side actual de la posición ("Buy" o "Sell")
    ) -> OrderResult:
        """
        Cierra total o parcialmente la posición con orden de mercado reduce-only.
        side: la dirección de la posición abierta (opuesta al cierre).
        """
        close_side = "Sell" if side == "Buy" else "Buy"
        body = {
            "category":      "linear",
            "symbol":        symbol,
            "side":          close_side,
            "orderType":     "Market",
            "qty":           str(qty),
            "timeInForce":   "IOC",
            "reduceOnly":    True,
            "positionIdx":   self._pos_idx(side),   # idx de la posición que se cierra
        }
        try:
            data = await self._post("/v5/order/create", body)
            if data.get("retCode") == 0:
                result = data.get("result", {})
                log.info("Position closed: %s x%s  id=%s", symbol, qty, result.get("orderId"))
                return OrderResult(success=True, order_id=result.get("orderId", ""))
            return OrderResult(success=False, error_msg=data.get("retMsg", "error"))
        except Exception as e:
            log.error("close_position exception: %s", e)
            return OrderResult(success=False, error_msg=str(e))

    async def cancel_all_orders(self, symbol: str) -> bool:
        """Cancela todas las órdenes activas del símbolo."""
        body = {"category": "linear", "symbol": symbol}
        try:
            data = await self._post("/v5/order/cancel-all", body)
            return data.get("retCode") == 0
        except Exception as e:
            log.error("cancel_all_orders: %s", e)
            return False

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        """Configura el apalancamiento del símbolo."""
        body = {
            "category":     "linear",
            "symbol":       symbol,
            "buyLeverage":  str(leverage),
            "sellLeverage": str(leverage),
        }
        try:
            data = await self._post("/v5/position/set-leverage", body)
            return data.get("retCode") in (0, 110043)  # 110043 = ya tiene ese leverage
        except Exception as e:
            log.error("set_leverage: %s", e)
            return False
