"""
streams/account.py
──────────────────
Stream de cuenta privada Bybit v5.

  · REST (aiohttp) → estado inicial al arrancar
  · WebSocket privado → updates en tiempo real sin polling

Autenticación HMAC-SHA256 estándar Bybit v5:
  timestamp + api_key + recv_window + params  → signature

Tópicos suscritos:
  position   — cambios en posiciones abiertas
  execution  — fills (acumula PnL realizado diario)
  order      — estado de órdenes
  wallet     — balance de cuenta
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import aiohttp
import websockets
import websockets.exceptions

from core.config import settings


# ─── URLs ─────────────────────────────────────────────────────────────────────

_REST_LIVE = "https://api.bybit.com"
_REST_TEST = "https://api-testnet.bybit.com"
_WS_LIVE   = "wss://stream.bybit.com/v5/private"
_WS_TEST   = "wss://stream-testnet.bybit.com/v5/private"


# ─── Dataclasses ──────────────────────────────────────────────────────────────

@dataclass
class Position:
    symbol:            str
    side:              str    # "Buy" = LONG | "Sell" = SHORT
    size:              float  # contratos / cantidad
    entry_price:       float
    mark_price:        float
    leverage:          float
    unrealized_pnl:    float  # en USDT
    liquidation_price: float
    take_profit:       float
    stop_loss:         float
    margin:            float  # USDT de margen usado
    created_time:      int    # ms epoch

    @property
    def is_long(self) -> bool:
        return self.side == "Buy"

    @property
    def side_label(self) -> str:
        return "LONG" if self.is_long else "SHORT"

    @property
    def pnl_pct(self) -> float:
        """PnL no realizado como % del margen."""
        if self.margin <= 0:
            return 0.0
        return self.unrealized_pnl / self.margin * 100

    @property
    def notional(self) -> float:
        return self.size * (self.mark_price if self.mark_price > 0 else self.entry_price)

    @property
    def distance_to_liq_pct(self) -> float:
        """Distancia al precio de liquidación como % del precio de entrada."""
        if self.entry_price <= 0 or self.liquidation_price <= 0:
            return 0.0
        return abs(self.liquidation_price - self.entry_price) / self.entry_price * 100


@dataclass
class AccountBalance:
    total_equity:      float = 0.0
    wallet_balance:    float = 0.0
    available_balance: float = 0.0
    used_margin:       float = 0.0
    unrealized_pnl:    float = 0.0

    @property
    def margin_pct(self) -> float:
        """% del equity usado como margen."""
        if self.total_equity <= 0:
            return 0.0
        return self.used_margin / self.total_equity * 100


@dataclass
class AccountState:
    positions:   Dict[str, Position]  = field(default_factory=dict)
    balance:     AccountBalance       = field(default_factory=AccountBalance)
    daily_pnl:   float                = 0.0   # PnL realizado acumulado hoy
    connected:   bool                 = False
    error:       str                  = ""

    def open_positions(self) -> List[Position]:
        return [p for p in self.positions.values() if p.size > 0]


# ─── AccountStream ────────────────────────────────────────────────────────────

class AccountStream:
    """
    Gestiona la conexión a la cuenta privada de Bybit.
    Compartida con la UI para leer posiciones y balance en tiempo real.
    """

    def __init__(self) -> None:
        self.state    = AccountState()
        self._running = False

    # ── Autenticación ─────────────────────────────────────────────────────────

    def _rest_headers(self, params: str = "") -> dict:
        ts      = str(int(time.time() * 1000))
        rw      = "5000"
        pre     = f"{ts}{settings.bybit_api_key}{rw}{params}"
        sig     = hmac.new(
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

    def _ws_auth_msg(self) -> str:
        expires = str(int(time.time() * 1000) + 5000)
        val     = f"GET/realtime{expires}"
        sig     = hmac.new(
            settings.bybit_api_secret.encode(),
            val.encode(),
            hashlib.sha256,
        ).hexdigest()
        return json.dumps({
            "op":   "auth",
            "args": [settings.bybit_api_key, expires, sig],
        })

    # ── REST ──────────────────────────────────────────────────────────────────

    async def _get(self, session: aiohttp.ClientSession, path: str, params: dict) -> dict:
        base    = _REST_TEST if settings.bybit_testnet else _REST_LIVE
        qs      = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        headers = self._rest_headers(qs)
        async with session.get(f"{base}{path}?{qs}", headers=headers) as r:
            return await r.json()

    async def _fetch_initial(self) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                await asyncio.gather(
                    self._fetch_positions(session),
                    self._fetch_balance(session),
                    self._fetch_daily_pnl(session),
                )
            self.state.connected = True
            self.state.error     = ""
        except Exception as e:
            self.state.error = f"REST error: {e}"

    async def _fetch_positions(self, session: aiohttp.ClientSession) -> None:
        data = await self._get(session, "/v5/position/list", {
            "category":   "linear",
            "settleCoin": "USDT",
            "limit":      "200",
        })
        if data.get("retCode") != 0:
            return
        for item in data.get("result", {}).get("list", []):
            size = float(item.get("size", 0))
            if size <= 0:
                continue
            sym = item.get("symbol", "")
            self.state.positions[sym] = self._parse_position(item)

    async def _fetch_balance(self, session: aiohttp.ClientSession) -> None:
        data = await self._get(session, "/v5/account/wallet-balance", {
            "accountType": "UNIFIED",
        })
        if data.get("retCode") != 0:
            return
        for acc in data.get("result", {}).get("list", []):
            if acc.get("accountType") in ("UNIFIED", "CONTRACT"):
                b = self.state.balance
                b.total_equity      = float(acc.get("totalEquity", 0) or 0)
                b.wallet_balance    = float(acc.get("totalWalletBalance", 0) or 0)
                b.available_balance = float(acc.get("totalAvailableBalance", 0) or 0)
                b.used_margin       = float(acc.get("totalInitialMargin", 0) or 0)
                b.unrealized_pnl    = float(acc.get("totalUnrealisedPnl", 0) or 0)
                break

    async def _fetch_daily_pnl(self, session: aiohttp.ClientSession) -> None:
        """PnL realizado del día (posiciones cerradas desde 00:00 UTC)."""
        import datetime
        today_ms = int(datetime.datetime.utcnow().replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp() * 1000)
        data = await self._get(session, "/v5/position/closed-pnl", {
            "category":  "linear",
            "startTime": str(today_ms),
            "limit":     "200",
        })
        if data.get("retCode") != 0:
            return
        total = sum(
            float(item.get("closedPnl", 0) or 0)
            for item in data.get("result", {}).get("list", [])
        )
        self.state.daily_pnl += total

    # ── WebSocket privado ─────────────────────────────────────────────────────

    async def _connect_private(self) -> None:
        url     = _WS_TEST if settings.bybit_testnet else _WS_LIVE
        backoff = 1.0
        while self._running:
            try:
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=15,
                ) as ws:
                    # Auth
                    await ws.send(self._ws_auth_msg())
                    auth_resp = json.loads(await ws.recv())
                    if not auth_resp.get("success"):
                        self.state.error = "WS auth failed — verifica API keys"
                        await asyncio.sleep(30)
                        continue

                    # Suscribir tópicos privados
                    await ws.send(json.dumps({
                        "op":   "subscribe",
                        "args": ["position", "execution", "order", "wallet"],
                    }))
                    backoff = 1.0
                    self.state.connected = True

                    async for raw in ws:
                        if not self._running:
                            return
                        try:
                            msg = json.loads(raw)
                            self._handle_private(msg)
                        except (json.JSONDecodeError, KeyError):
                            pass

            except (
                websockets.exceptions.ConnectionClosed,
                websockets.exceptions.WebSocketException,
                OSError,
            ):
                if self._running:
                    self.state.connected = False
                    await asyncio.sleep(min(backoff, 60.0))
                    backoff = min(backoff * 2, 60.0)
            except asyncio.CancelledError:
                return

    def _handle_private(self, msg: dict) -> None:
        topic = msg.get("topic", "")
        data  = msg.get("data", [])
        if not isinstance(data, list):
            data = [data]

        if topic == "position":
            for item in data:
                size = float(item.get("size", 0) or 0)
                sym  = item.get("symbol", "")
                if not sym:
                    continue
                if size <= 0:
                    self.state.positions.pop(sym, None)
                else:
                    self.state.positions[sym] = self._parse_position(item)

        elif topic == "execution":
            for item in data:
                # execPnl = gross position PnL (sin fees)
                # execFee = fee de esta ejecución (siempre positivo = costo)
                realized = float(item.get("execPnl", 0) or 0)
                fee      = float(item.get("execFee",  0) or 0)
                self.state.daily_pnl += realized - fee

        elif topic == "wallet":
            for item in data:
                if item.get("accountType") in ("UNIFIED", "CONTRACT"):
                    b = self.state.balance
                    for coin in item.get("coin", []):
                        if coin.get("coin") in ("USDT", ""):
                            pass
                    b.total_equity      = float(item.get("totalEquity", b.total_equity) or b.total_equity)
                    b.wallet_balance    = float(item.get("totalWalletBalance", b.wallet_balance) or b.wallet_balance)
                    b.available_balance = float(item.get("totalAvailableBalance", b.available_balance) or b.available_balance)
                    b.used_margin       = float(item.get("totalInitialMargin", b.used_margin) or b.used_margin)
                    b.unrealized_pnl    = float(item.get("totalUnrealisedPnl", b.unrealized_pnl) or b.unrealized_pnl)

    def _parse_position(self, item: dict) -> Position:
        def f(key: str, default: float = 0.0) -> float:
            v = item.get(key, default)
            try:
                return float(v) if v not in (None, "", "0") or default == 0 else default
            except (ValueError, TypeError):
                return default

        return Position(
            symbol            = item.get("symbol", ""),
            side              = item.get("side", "Buy"),
            size              = f("size"),
            entry_price       = f("avgPrice") or f("entryPrice"),
            mark_price        = f("markPrice"),
            leverage          = f("leverage", 1.0),
            unrealized_pnl    = f("unrealisedPnl"),
            liquidation_price = f("liqPrice"),
            take_profit       = f("takeProfit"),
            stop_loss         = f("stopLoss"),
            margin            = f("positionIM") or f("positionMM"),
            created_time      = int(item.get("createdTime", 0) or 0),
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        if not settings.bybit_api_key or not settings.bybit_api_secret:
            self.state.error = "API keys no configuradas en .env"
            return
        self._running = True
        await self._fetch_initial()
        await self._connect_private()

    def stop(self) -> None:
        self._running = False
