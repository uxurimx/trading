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
import re
import time
from typing import Dict, Optional, Tuple

import aiohttp

from core.config import settings
from core.logger import executor_logger
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

    # ── Carga dinámica de símbolos (sincrónica, solo en startup) ──────────────

    _MIN_VOL_24H_USDT = 10_000_000.0  # $10M mínimo de volumen diario

    @staticmethod
    def fetch_top_usdt_symbols_sync(
        limit: int = 100, testnet: bool = False
    ) -> "list[tuple[str, float]]":
        """
        Obtiene los top-N pares USDT perpetuos de Bybit con volumen >= $10M/día.
        Usa urllib (stdlib) — sin dependencias externas.
        Solo llamar en startup (bloquea el hilo).
        Retorna List[Tuple[symbol, vol_24h_usd]]; lista vacía si falla la conexión.
        """
        import urllib.request as _urllib
        _MIN_VOL = BybitExecutor._MIN_VOL_24H_USDT
        base = "https://api-testnet.bybit.com" if testnet else "https://api.bybit.com"
        url  = f"{base}/v5/market/tickers?category=linear"
        try:
            req = _urllib.Request(url, headers={"User-Agent": "QTS/1.0"})
            with _urllib.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            if data.get("retCode") != 0:
                log.warning("fetch_top_usdt_symbols: retCode=%s", data.get("retCode"))
                return []
            items = data.get("result", {}).get("list", [])
            usdt = [
                (item["symbol"], float(item.get("turnover24h") or 0))
                for item in items
                if item.get("symbol", "").endswith("USDT")
                and float(item.get("turnover24h") or 0) >= _MIN_VOL
            ]
            usdt.sort(key=lambda x: x[1], reverse=True)
            result = usdt[:limit]
            log.info(
                "fetch_top_usdt_symbols: %d pares (vol >= $%.0fM, top %d)",
                len(result), _MIN_VOL / 1_000_000, limit,
            )
            return result
        except Exception as exc:
            log.warning("fetch_top_usdt_symbols falló: %s — usando cache DB", exc)
            return []

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
            with executor_logger.context(req.trace_id):
                executor_logger.info("ORDER_SENT", f"Enviando orden {req.side} {req.symbol}", {"body": body})
                data = await self._post("/v5/order/create", body)
                
                if data.get("retCode") == 0:
                    result = data.get("result", {})
                    log.info("Order placed: %s %s x%s  id=%s",
                             req.side, req.symbol, req.qty, result.get("orderId"))
                    executor_logger.info("ORDER_SUCCESS", "Orden aceptada por Bybit", {"response": data})
                    return OrderResult(
                        success=True,
                        order_id=result.get("orderId", ""),
                    )
                
                # Auto-corrección positionIdx (misma lógica determinista que set_sl_tp)
                _ret_msg_p = data.get("retMsg", "")
                _is_idx_err_p = (
                    data.get("retCode") == 110025 or
                    "position idx" in _ret_msg_p.lower() or
                    "positionidx" in _ret_msg_p.lower() or
                    "position mode" in _ret_msg_p.lower()
                )
                if _is_idx_err_p:
                    mode_match = re.search(r"position mode\((\d+)\)", _ret_msg_p, re.IGNORECASE)
                    if mode_match:
                        self._hedge_mode = (int(mode_match.group(1)) == 3)
                    else:
                        self._hedge_mode = not self._hedge_mode
                    body["positionIdx"] = self._pos_idx(req.side)
                    log.warning("positionIdx incorrecto — fijando a %s y reintentando",
                                "Hedge" if self._hedge_mode else "One-way")
                    executor_logger.warning("RETRY_POS_IDX", "Reintentando con nuevo positionIdx", {"hedge_mode": self._hedge_mode})
                    data = await self._post("/v5/order/create", body)
                    if data.get("retCode") == 0:
                        result = data.get("result", {})
                        log.info("Order placed (retry): %s %s x%s  id=%s",
                                 req.side, req.symbol, req.qty, result.get("orderId"))
                        executor_logger.info("ORDER_SUCCESS", "Orden aceptada tras reintento", {"response": data})
                        return OrderResult(success=True, order_id=result.get("orderId", ""))
                
                msg = data.get("retMsg", "unknown error")
                log.warning("Order failed: %s — %s", req.symbol, msg)
                executor_logger.error("ORDER_ERROR", f"Orden rechazada: {msg}", {"response": data})
                return OrderResult(success=False, error_msg=msg)
        except Exception as e:
            log.error("place_market_bracket exception: %s", e)
            executor_logger.error("EXECUTION_EXCEPTION", f"Excepción en ejecución: {e}")
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
        symbol:   str,
        sl:       float = 0.0,
        tp:       float = 0.0,
        side:     str   = "Buy",
        clear_tp: bool  = False,
        trace_id: str   = "",
    ) -> bool:
        """
        Modifica SL y/o TP de una posición abierta.
        POST /v5/position/trading-stop
        Pasar 0 en sl o tp para no modificarlo.
        clear_tp=True envía "0" para eliminar el TP activo (captura extendida).

        Hedge Mode: positionIdx se calcula automáticamente desde _pos_idx(side).
          · Buy  → positionIdx=1
          · Sell → positionIdx=2
        Si Bybit devuelve 34040 o 110025 (positionIdx incorrecto) se reintenta
        con el modo invertido, igual que en place_market_bracket.
        """
        pos_idx = self._pos_idx(side)
        body: dict = {
            "category":    "linear",
            "symbol":      symbol,
            "positionIdx": pos_idx,
        }
        if sl > 0:
            body["stopLoss"]    = self.format_price(sl)
            body["slTriggerBy"] = "MarkPrice"
        if tp > 0:
            body["takeProfit"]  = self.format_price(tp)
            body["tpTriggerBy"] = "MarkPrice"
        elif clear_tp:
            body["takeProfit"]  = "0"   # Bybit: "0" = eliminar TP existente

        if len(body) <= 3:
            return True  # nada que modificar

        ctx = executor_logger.context(trace_id) if trace_id else executor_logger.context()
        try:
            with ctx as tid:
                executor_logger.debug("SET_SL_TP_SEND", f"Enviando trading-stop {symbol}", {
                    "symbol": symbol, "side": side, "sl": sl, "tp": tp,
                    "clear_tp": clear_tp, "positionIdx": pos_idx,
                    "hedge_mode": self._hedge_mode,
                })
                data = await self._post("/v5/position/trading-stop", body)
                ret_code = data.get("retCode")

                # Éxito o "sin cambios" (ya tenía ese SL/TP desde place_order)
                if ret_code in (0, 3400099):
                    executor_logger.debug("SET_SL_TP_OK", f"SL/TP aplicado: {symbol}", {
                        "retCode": ret_code, "positionIdx": pos_idx,
                    })
                    return True

                _ret_msg = data.get("retMsg", "")
                _ret_msg_lower = _ret_msg.lower()

                # 34040 "not modified": CONFIRMAR con REST que el SL está realmente en la posición.
                # "not modified" puede significar:
                #   a) SL ya estaba en ese valor (OK) → stopLoss > 0 en position/list
                #   b) positionIdx incorrecto y Bybit no encontró la posición (FALLO SILENCIOSO)
                #      → stopLoss == 0 → tratar como error, reintentar con idx correcto
                if ret_code == 34040 and "not modified" in _ret_msg_lower and "position mode" not in _ret_msg_lower:
                    confirmed = await self.verify_sl_on_position(symbol)
                    if confirmed:
                        executor_logger.info("SET_SL_TP_OK", f"SL/TP confirmado en posición (not modified): {symbol}", {
                            "retCode": ret_code, "positionIdx": pos_idx,
                        })
                        return True
                    # SL no encontrado → "not modified" fue fallo silencioso
                    # Intentar detectar el modo correcto y reintentar
                    log.warning(
                        "set_sl_tp: 'not modified' pero stopLoss=0 en %s (positionIdx=%d puede ser incorrecto) — reintentando",
                        symbol, pos_idx,
                    )
                    self._hedge_mode = not self._hedge_mode  # flip como fallback
                    new_idx = self._pos_idx(side)
                    body["positionIdx"] = new_idx
                    data = await self._post("/v5/position/trading-stop", body)
                    ret_code = data.get("retCode")
                    _ret_msg_r = data.get("retMsg", "")
                    if ret_code in (0, 3400099):
                        executor_logger.info("SET_SL_TP_RETRY_OK", f"SL/TP aplicado tras corrección idx: {symbol}", {
                            "retCode": ret_code, "positionIdx": new_idx,
                        })
                        return True
                    if ret_code == 34040 and "not modified" in _ret_msg_r.lower():
                        confirmed2 = await self.verify_sl_on_position(symbol)
                        if confirmed2:
                            return True
                    log.warning("set_sl_tp: falló incluso tras flip idx para %s (code=%s)", symbol, ret_code)
                    return False

                # Auto-corrección: positionIdx incorrecto
                # Parsear "position mode(X)" del mensaje para fijar el modo de forma determinista
                # (en lugar de hacer flip ciego que causa 34040↔10001 loop)
                _is_idx_err = (
                    ret_code == 110025 or
                    "position idx" in _ret_msg_lower or
                    "positionidx" in _ret_msg_lower or
                    (ret_code in (34040, 10001) and "position mode" in _ret_msg_lower)
                )
                if _is_idx_err:
                    mode_match = re.search(r"position mode\((\d+)\)", _ret_msg, re.IGNORECASE)
                    if mode_match:
                        actual_mode = int(mode_match.group(1))
                        self._hedge_mode = (actual_mode == 3)   # 3=Hedge, 0=One-way
                    else:
                        self._hedge_mode = not self._hedge_mode  # fallback: flip
                    new_idx = self._pos_idx(side)
                    log.warning(
                        "set_sl_tp positionIdx incorrecto (%s, code=%s) — "
                        "fijando a %s (idx=%d) y reintentando",
                        symbol, ret_code,
                        "Hedge" if self._hedge_mode else "One-way", new_idx,
                    )
                    executor_logger.warning("SET_SL_TP_IDX_RETRY", "positionIdx incorrecto, reintentando", {
                        "symbol": symbol, "retCode": ret_code, "retMsg": _ret_msg,
                        "old_idx": pos_idx, "new_idx": new_idx, "hedge_mode": self._hedge_mode,
                    })
                    body["positionIdx"] = new_idx
                    data = await self._post("/v5/position/trading-stop", body)
                    ret_code = data.get("retCode")
                    _ret_msg_r = data.get("retMsg", "")
                    if ret_code in (0, 3400099):
                        executor_logger.info("SET_SL_TP_RETRY_OK", f"SL/TP aplicado tras reintento: {symbol}", {
                            "retCode": ret_code, "positionIdx": new_idx,
                        })
                        return True
                    # "not modified" en el retry — verificar con REST también
                    if ret_code == 34040 and "not modified" in _ret_msg_r.lower() and "position mode" not in _ret_msg_r.lower():
                        confirmed_r = await self.verify_sl_on_position(symbol)
                        if confirmed_r:
                            executor_logger.info("SET_SL_TP_RETRY_OK", f"SL/TP confirmado tras reintento: {symbol}", {
                                "retCode": ret_code, "positionIdx": new_idx,
                            })
                            return True

                log.warning("set_sl_tp failed: %s — %s (code %s)", symbol, data.get("retMsg"), ret_code)
                executor_logger.warning("SET_SL_TP_FAILED", f"Fallo al aplicar SL/TP: {symbol}", {
                    "symbol": symbol, "side": side, "sl": sl, "tp": tp,
                    "retCode": ret_code, "retMsg": data.get("retMsg"),
                    "positionIdx": body.get("positionIdx"), "hedge_mode": self._hedge_mode,
                })
                return False
        except Exception as e:
            log.error("set_sl_tp exception: %s", e)
            executor_logger.error("SET_SL_TP_EXCEPTION", f"Excepción en set_sl_tp: {e}", {
                "symbol": symbol, "side": side, "sl": sl, "tp": tp,
            })
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
            ret_code = data.get("retCode")
            if ret_code == 0:
                result = data.get("result", {})
                log.info("Position closed: %s x%s  id=%s", symbol, qty, result.get("orderId"))
                return OrderResult(success=True, order_id=result.get("orderId", ""))

            # Auto-corrección positionIdx (mismo mecanismo determinista que set_sl_tp)
            _ret_msg = data.get("retMsg", "")
            _ret_msg_lower = _ret_msg.lower()
            _is_idx_err = (
                ret_code == 110025 or
                "position idx" in _ret_msg_lower or
                "positionidx" in _ret_msg_lower or
                (ret_code in (34040, 10001) and "position mode" in _ret_msg_lower)
            )
            if _is_idx_err:
                mode_match = re.search(r"position mode\((\d+)\)", _ret_msg, re.IGNORECASE)
                if mode_match:
                    actual_mode = int(mode_match.group(1))
                    self._hedge_mode = (actual_mode == 3)
                else:
                    self._hedge_mode = not self._hedge_mode
                body["positionIdx"] = self._pos_idx(side)
                log.warning("close_position positionIdx incorrecto (%s, code=%s) — fijando a %s y reintentando",
                            symbol, ret_code, "Hedge" if self._hedge_mode else "One-way")
                data = await self._post("/v5/order/create", body)
                if data.get("retCode") == 0:
                    result = data.get("result", {})
                    log.info("Position closed (retry): %s x%s  id=%s", symbol, qty, result.get("orderId"))
                    return OrderResult(success=True, order_id=result.get("orderId", ""))

            return OrderResult(success=False, error_msg=data.get("retMsg", "error"))
        except Exception as e:
            log.error("close_position exception: %s", e)
            return OrderResult(success=False, error_msg=str(e))

    async def wait_for_position(
        self, symbol: str, side: str, timeout_s: float = 8.0
    ) -> Tuple[bool, float]:
        """
        Espera a que Bybit refleje la posición como abierta (poll /v5/position/list).
        Market IOC llena en < 1s, pero la posición puede tardar 1-3s en aparecer.
        Retorna (encontrada: bool, avg_entry_price: float).
        """
        deadline = time.time() + timeout_s
        attempt  = 0
        while time.time() < deadline:
            await asyncio.sleep(0.4 if attempt == 0 else 0.6)
            attempt += 1
            try:
                data = await self._get("/v5/position/list", {
                    "category": "linear",
                    "symbol":   symbol,
                })
                for p in data.get("result", {}).get("list", []):
                    sz = float(p.get("size", 0))
                    if sz <= 0:
                        continue
                    # Filtrar por side si la cuenta es hedge (puede tener long y short)
                    if self._hedge_mode and p.get("side", "") != side:
                        continue
                    avg = float(p.get("avgPrice") or p.get("entryPrice") or 0)
                    log.info("wait_for_position: %s %s @ %.5g (intento %d)", side, symbol, avg, attempt)
                    return True, avg
            except Exception as e:
                log.warning("wait_for_position(%s) poll %d: %s", symbol, attempt, e)
        log.error("wait_for_position: %s no apareció en Bybit tras %.0fs", symbol, timeout_s)
        return False, 0.0

    async def verify_sl_on_position(self, symbol: str) -> bool:
        """
        Verifica que la posición tenga stopLoss > 0 en Bybit.
        Retorna True si el SL está activo, False si no hay SL o no hay posición.
        Siempre loguea a INFO el valor real encontrado para diagnóstico.
        """
        try:
            data = await self._get("/v5/position/list", {
                "category": "linear",
                "symbol":   symbol,
            })
            for p in data.get("result", {}).get("list", []):
                if float(p.get("size", 0)) > 0:
                    sl = float(p.get("stopLoss") or 0)
                    tp = float(p.get("takeProfit") or 0)
                    log.info("verify_sl_on_position %s: stopLoss=%.5g  takeProfit=%.5g  idx=%s",
                             symbol, sl, tp, p.get("positionIdx"))
                    return sl > 0
            log.warning("verify_sl_on_position %s: no hay posición con size > 0", symbol)
        except Exception as e:
            log.warning("verify_sl_on_position(%s): %s", symbol, e)
        return False

    async def get_position_sl_tp(self, symbol: str) -> tuple[float, float]:
        """
        Retorna (stopLoss, takeProfit) reales de la posición activa en Bybit.
        Devuelve (0.0, 0.0) si no hay posición o falla la llamada.
        """
        try:
            data = await self._get("/v5/position/list", {
                "category": "linear",
                "symbol":   symbol,
            })
            for p in data.get("result", {}).get("list", []):
                if float(p.get("size", 0)) > 0:
                    sl = float(p.get("stopLoss") or 0)
                    tp = float(p.get("takeProfit") or 0)
                    return sl, tp
        except Exception as e:
            log.warning("get_position_sl_tp(%s): %s", symbol, e)
        return 0.0, 0.0

    async def get_position_open_time(self, symbol: str, since_ms: int = 0) -> int:
        """
        Recupera el timestamp real de apertura de la posición consultando el historial
        de ejecuciones de Bybit. Retorna Unix timestamp en segundos, o 0 si falla.
        since_ms: sólo buscar ejecuciones a partir de este timestamp en ms (createdTime).
        """
        try:
            params: dict = {
                "category": "linear",
                "symbol":   symbol,
                "execType": "Trade",
                "limit":    "10",
            }
            # Filtrar desde 5 minutos antes del createdTime para capturar el fill de apertura
            if since_ms > 1_000_000_000_000:
                params["startTime"] = str(max(0, since_ms - 300_000))
            data = await self._get("/v5/execution/list", params)
            items = data.get("result", {}).get("list", [])
            # Ejecutions llegan en orden descendente (más reciente primero).
            # El más antiguo en la lista filtrada es el fill de apertura.
            for item in reversed(items):
                exec_time = int(item.get("execTime", 0) or 0)
                if exec_time > 1_000_000_000_000:   # ms → segundos
                    exec_time = exec_time // 1000
                if exec_time > 0:
                    return exec_time
        except Exception as e:
            log.warning("get_position_open_time(%s): %s", symbol, e)
        return 0

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
