"""
streams/market.py
─────────────────
Pipeline de datos de mercado en tiempo real — Bybit WebSocket v5.

Conexiones:
  · LINEAR  (USDT perpetuals / futuros) — orderbook, trades, ticker, liquidaciones
  · SPOT                                — ticker (para calcular basis futuros-spot)

Estado por símbolo (MarketState):
  · OrderBook con imbalance
  · Trade tape + CVD acumulado
  · CVD por vela (sparkline temporal)
  · Ticker: precio, funding, OI
  · Spot price → Basis
  · Liquidaciones en tiempo real
  · OI velocity (tasa de cambio del OI)
"""
from __future__ import annotations

import asyncio
import datetime
import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import websockets
import websockets.exceptions

from core.config import settings
from core.liquidity import VolumeProfile


# ─── Dataclasses ─────────────────────────────────────────────────────────────

@dataclass(slots=True)
class Trade:
    timestamp: int   # ms epoch
    price:     float
    qty:       float
    side:      str   # "Buy" | "Sell"


@dataclass(slots=True)
class Liquidation:
    timestamp: int
    side:      str    # "Buy" = SHORT liq'd  |  "Sell" = LONG liq'd
    size:      float  # contratos
    price:     float
    notional:  float  # size × price  (valor en USD)

    @property
    def is_long_liq(self) -> bool:
        """True si fue una posición LONG la que se liquidó."""
        return self.side == "Sell"

    @property
    def position_type(self) -> str:
        return "LONG" if self.is_long_liq else "SHORT"


@dataclass
class CandleCVD:
    """CVD acumulado dentro de una vela de tiempo fijo."""
    ts:       int    # timestamp apertura (segundos)
    interval: int    # duración en segundos
    buy_vol:  float = 0.0
    sell_vol: float = 0.0

    @property
    def delta(self) -> float:
        return self.buy_vol - self.sell_vol

    @property
    def total(self) -> float:
        return self.buy_vol + self.sell_vol

    @property
    def is_bullish(self) -> bool:
        return self.delta >= 0

    @property
    def delta_pct(self) -> float:
        """Porcentaje de presión compradora [-100, +100]."""
        if self.total == 0:
            return 0.0
        return (self.buy_vol / self.total * 100 - 50) * 2


@dataclass
class Ticker:
    symbol:           str
    last_price:       float = 0.0
    mark_price:       float = 0.0
    bid:              float = 0.0
    ask:              float = 0.0
    funding_rate:     float = 0.0   # en porcentaje (×100)
    open_interest:    float = 0.0
    volume_24h:       float = 0.0
    price_change_pct: float = 0.0   # en porcentaje (×100)


# ─── OrderBook ───────────────────────────────────────────────────────────────

class OrderBook:
    """
    Libro de órdenes con soporte para snapshot + deltas incrementales.
    Calcula imbalance, mid price y liquidez visible.
    """

    __slots__ = ("bids", "asks")

    def __init__(self) -> None:
        self.bids: Dict[float, float] = {}
        self.asks: Dict[float, float] = {}

    def apply_snapshot(self, data: dict) -> None:
        self.bids = {float(p): float(q) for p, q in data.get("b", [])}
        self.asks = {float(p): float(q) for p, q in data.get("a", [])}

    def apply_delta(self, data: dict) -> None:
        for p_s, q_s in data.get("b", []):
            p, q = float(p_s), float(q_s)
            if q == 0.0:
                self.bids.pop(p, None)
            else:
                self.bids[p] = q
        for p_s, q_s in data.get("a", []):
            p, q = float(p_s), float(q_s)
            if q == 0.0:
                self.asks.pop(p, None)
            else:
                self.asks[p] = q

    def top_bids(self, n: int = 12) -> List[Tuple[float, float]]:
        return sorted(self.bids.items(), reverse=True)[:n]

    def top_asks(self, n: int = 12) -> List[Tuple[float, float]]:
        return sorted(self.asks.items())[:n]

    @property
    def best_bid(self) -> float:
        return max(self.bids.keys(), default=0.0)

    @property
    def best_ask(self) -> float:
        return min(self.asks.keys(), default=0.0)

    @property
    def spread(self) -> float:
        b, a = self.best_bid, self.best_ask
        return (a - b) if b > 0 and a > 0 else 0.0

    @property
    def mid_price(self) -> float:
        b, a = self.best_bid, self.best_ask
        return (b + a) / 2 if b > 0 and a > 0 else 0.0

    @property
    def bid_wall(self) -> float:
        return sum(q for _, q in self.top_bids(10))

    @property
    def ask_wall(self) -> float:
        return sum(q for _, q in self.top_asks(10))

    @property
    def imbalance(self) -> float:
        """
        Ratio de presión: bid_wall / (bid_wall + ask_wall)
        > 0.55 → presión compradora  |  < 0.45 → presión vendedora
        Rango: [0.0, 1.0]
        """
        bw, aw = self.bid_wall, self.ask_wall
        total = bw + aw
        return bw / total if total > 0 else 0.5


# ─── MarketState ─────────────────────────────────────────────────────────────

class MarketState:
    """
    Snapshot completo del estado de mercado para un símbolo.
    Combina datos de futuros (linear) y spot.
    Es la única fuente de verdad que leen los widgets de UI.
    """

    def __init__(self, symbol: str) -> None:
        self.symbol    = symbol
        self.orderbook = OrderBook()
        self.trades:       deque[Trade]       = deque(maxlen=200)
        self.liquidations: deque[Liquidation] = deque(maxlen=100)
        self.ticker        = Ticker(symbol=symbol)
        self.cvd_candles:  deque[CandleCVD]  = deque(maxlen=30)

        # CVD acumulado (sesión completa)
        self.cvd: float = 0.0

        # Volúmenes de sesión
        self.session_buy_vol:  float = 0.0
        self.session_sell_vol: float = 0.0

        # Liquidaciones de sesión (valor USD)
        self.liq_long_total:  float = 0.0
        self.liq_short_total: float = 0.0

        # Precio spot (recibido desde el stream spot para calcular basis)
        self.spot_price: float = 0.0

        # Historial de OI para calcular velocidad
        self._oi_history: deque[Tuple[float, float]] = deque(maxlen=120)

        # Fase 3: perfil de volumen de sesión + muestras de precio para swings
        self.volume_profile = VolumeProfile()
        self._price_samples: deque[Tuple[float, float]] = deque(maxlen=500)   # (ts_s, price) — trades

        # Fase 3: historial grueso de precio para tendencia multi-timeframe
        # Muestreado cada 30s, maxlen=1000 → cubre ~8.3 horas
        self._price_history: deque[Tuple[float, float]] = deque(maxlen=1000)  # (ts_s, price)
        self._last_price_sample: float = 0.0

        # Meta
        self.connected:        bool  = False
        self.spot_connected:   bool  = False
        self.last_update:      float = 0.0

    # ── Trade ingestion ───────────────────────────────────────────────────────

    def add_trade(self, trade: Trade) -> None:
        self.trades.append(trade)
        if trade.side == "Buy":
            self.session_buy_vol += trade.qty
            self.cvd += trade.qty
        else:
            self.session_sell_vol += trade.qty
            self.cvd -= trade.qty
        self._update_cvd_candle(trade)
        self.volume_profile.add(trade.price, trade.qty)
        self._price_samples.append((trade.timestamp / 1000, trade.price))
        self.last_update = time.time()

    def _update_cvd_candle(self, trade: Trade) -> None:
        interval = settings.candle_interval
        ts_sec   = trade.timestamp // 1000
        candle_ts = (ts_sec // interval) * interval

        if not self.cvd_candles or self.cvd_candles[-1].ts != candle_ts:
            self.cvd_candles.append(CandleCVD(ts=candle_ts, interval=interval))

        candle = self.cvd_candles[-1]
        if trade.side == "Buy":
            candle.buy_vol += trade.qty
        else:
            candle.sell_vol += trade.qty

    # ── Liquidation ingestion ─────────────────────────────────────────────────

    def add_liquidation(self, liq: Liquidation) -> None:
        self.liquidations.append(liq)
        if liq.is_long_liq:
            self.liq_long_total += liq.notional
        else:
            self.liq_short_total += liq.notional

    # ── Reset ─────────────────────────────────────────────────────────────────

    def reset_session(self) -> None:
        self.cvd = 0.0
        self.session_buy_vol  = 0.0
        self.session_sell_vol = 0.0
        self.liq_long_total   = 0.0
        self.liq_short_total  = 0.0
        self.volume_profile.reset()

    # ── Propiedades derivadas ─────────────────────────────────────────────────

    @property
    def session_delta(self) -> float:
        return self.session_buy_vol - self.session_sell_vol

    @property
    def buy_pct(self) -> float:
        total = self.session_buy_vol + self.session_sell_vol
        return self.session_buy_vol / total * 100 if total > 0 else 50.0

    @property
    def basis(self) -> float:
        """Diferencia absoluta: futuros − spot."""
        if self.spot_price == 0:
            return 0.0
        return self.ticker.last_price - self.spot_price

    @property
    def basis_pct(self) -> float:
        """Basis como % del precio spot."""
        if self.spot_price == 0:
            return 0.0
        return self.basis / self.spot_price * 100

    @property
    def oi_velocity(self) -> float:
        """
        Velocidad de cambio del Open Interest en USD/minuto.
        Positivo = capital entrando (nuevas posiciones)
        Negativo = capital saliendo (posiciones cerrándose)
        """
        if len(self._oi_history) < 2:
            return 0.0
        ts0, oi0 = self._oi_history[0]
        ts1, oi1 = self._oi_history[-1]
        elapsed = ts1 - ts0
        if elapsed < 5:
            return 0.0
        return (oi1 - oi0) / elapsed * 60

    @property
    def funding_countdown(self) -> str:
        """
        Tiempo al próximo funding de Bybit (00:00, 08:00, 16:00 UTC).
        Retorna string formateado: '2h 34m'
        """
        now = datetime.datetime.utcnow()
        funding_hours = [0, 8, 16]
        next_h = next((h for h in funding_hours if h > now.hour), 24)
        if next_h == 24:
            next_dt = (now + datetime.timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
        else:
            next_dt = now.replace(hour=next_h, minute=0, second=0, microsecond=0)
        remaining = next_dt - now
        h = int(remaining.total_seconds() // 3600)
        m = int((remaining.total_seconds() % 3600) // 60)
        return f"{h}h {m:02d}m"

    def recent_trades(self, n: int = 15) -> List[Trade]:
        trades = list(self.trades)
        return trades[-n:][::-1]

    def recent_liquidations(self, n: int = 8) -> List[Liquidation]:
        liqs = list(self.liquidations)
        return liqs[-n:][::-1]


# ─── MarketStream ─────────────────────────────────────────────────────────────

class MarketStream:
    """
    Conecta a Bybit WebSocket v5 en dos endpoints:
      · LINEAR — datos completos de futuros (orderbook, trades, tickers, liquidaciones)
      · SPOT   — solo ticker (precio spot para calcular basis)

    Reconexión automática con backoff exponencial.
    """

    _URL = {
        "linear_live": "wss://stream.bybit.com/v5/public/linear",
        "spot_live":   "wss://stream.bybit.com/v5/public/spot",
        "linear_test": "wss://stream-testnet.bybit.com/v5/public/linear",
        "spot_test":   "wss://stream-testnet.bybit.com/v5/public/spot",
    }

    def __init__(self) -> None:
        self.states: Dict[str, MarketState] = {
            sym: MarketState(sym) for sym in settings.symbol_list
        }
        self._running = False

    # ── Handlers de mensajes ──────────────────────────────────────────────────

    def _handle_futures(self, symbol: str, msg: dict) -> None:
        topic: str = msg.get("topic", "")
        state  = self.states[symbol]

        if "orderbook" in topic:
            if msg.get("type") == "snapshot":
                state.orderbook.apply_snapshot(msg["data"])
            else:
                state.orderbook.apply_delta(msg["data"])
            state.connected  = True
            state.last_update = time.time()

        elif "publicTrade" in topic:
            for t in msg.get("data", []):
                state.add_trade(Trade(
                    timestamp=int(t["T"]),
                    price=float(t["p"]),
                    qty=float(t["v"]),
                    side=t["S"],
                ))

        elif "tickers" in topic:
            d  = msg.get("data", {})
            tk = state.ticker
            if "lastPrice" in d:
                tk.last_price = float(d["lastPrice"])
                # Muestreo de precio para MTF trend (~30s)
                _now = time.time()
                if _now - state._last_price_sample >= 30.0 and tk.last_price > 0:
                    state._price_history.append((_now, tk.last_price))
                    state._last_price_sample = _now
            if "markPrice"         in d: tk.mark_price        = float(d["markPrice"])
            if "bid1Price"         in d: tk.bid               = float(d["bid1Price"])
            if "ask1Price"         in d: tk.ask               = float(d["ask1Price"])
            if "fundingRate"       in d: tk.funding_rate      = float(d["fundingRate"]) * 100
            if "openInterestValue" in d:
                oi = float(d["openInterestValue"])
                tk.open_interest = oi
                state._oi_history.append((time.time(), oi))
            if "volume24h"         in d: tk.volume_24h        = float(d["volume24h"])
            if "price24hPcnt"      in d: tk.price_change_pct  = float(d["price24hPcnt"]) * 100
            state.last_update = time.time()

        elif "allLiquidation" in topic:
            # Bybit v5: "allLiquidation.{symbol}" — datos dentro de data.list[]
            for item in msg.get("data", {}).get("list", [msg.get("data", {})]):
                try:
                    size  = float(item.get("size", 0))
                    price = float(item.get("price", 0))
                    if size > 0 and price > 0:
                        state.add_liquidation(Liquidation(
                            timestamp=int(item.get("updatedTime", time.time() * 1000)),
                            side=item.get("side", ""),
                            size=size,
                            price=price,
                            notional=size * price,
                        ))
                except (KeyError, ValueError):
                    pass

    def _handle_spot(self, symbol: str, msg: dict) -> None:
        if "tickers" not in msg.get("topic", ""):
            return
        d = msg.get("data", {})
        if "lastPrice" in d:
            self.states[symbol].spot_price     = float(d["lastPrice"])
            self.states[symbol].spot_connected = True

    # ── Conexión genérica con reconexión ─────────────────────────────────────

    async def _connect(
        self,
        url: str,
        topics: List[str],
        symbol: str,
        handler: Callable[[str, dict], None],
    ) -> None:
        backoff = 1.0
        while self._running:
            try:
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=15,
                    max_size=10 * 1024 * 1024,
                ) as ws:
                    await ws.send(json.dumps({"op": "subscribe", "args": topics}))
                    backoff = 1.0

                    async for raw in ws:
                        if not self._running:
                            return
                        try:
                            msg = json.loads(raw)
                            if "topic" in msg:
                                handler(symbol, msg)
                        except (json.JSONDecodeError, KeyError):
                            pass

            except (
                websockets.exceptions.ConnectionClosed,
                websockets.exceptions.WebSocketException,
                OSError,
            ):
                if self._running:
                    self.states[symbol].connected = False
                    await asyncio.sleep(min(backoff, 30.0))
                    backoff = min(backoff * 2, 30.0)

            except asyncio.CancelledError:
                return

    # ── Conexiones por símbolo ────────────────────────────────────────────────

    async def _connect_futures(self, symbol: str) -> None:
        env   = "test" if settings.bybit_testnet else "live"
        url   = self._URL[f"linear_{env}"]
        # Nota: topic correcto para liquidaciones en Bybit v5 es "allLiquidation.{symbol}"
        # Se envían por separado para que un fallo no cancele toda la suscripción.
        market_topics = [
            f"orderbook.50.{symbol}",
            f"publicTrade.{symbol}",
            f"tickers.{symbol}",
        ]
        liq_topics = [f"allLiquidation.{symbol}"]
        # Conectar mercado + liquidaciones en el mismo WebSocket pero suscribir por separado
        await self._connect_futures_ws(url, market_topics, liq_topics, symbol)

    async def _connect_futures_ws(
        self,
        url: str,
        market_topics: list,
        liq_topics: list,
        symbol: str,
    ) -> None:
        backoff = 1.0
        while self._running:
            try:
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=15,
                    max_size=10 * 1024 * 1024,
                ) as ws:
                    # Suscribir tópicos de mercado
                    await ws.send(json.dumps({"op": "subscribe", "args": market_topics}))
                    # Suscribir liquidaciones por separado (topic diferente)
                    await ws.send(json.dumps({"op": "subscribe", "args": liq_topics}))
                    backoff = 1.0

                    async for raw in ws:
                        if not self._running:
                            return
                        try:
                            msg = json.loads(raw)
                            if "topic" in msg:
                                self._handle_futures(symbol, msg)
                        except (json.JSONDecodeError, KeyError):
                            pass

            except (
                websockets.exceptions.ConnectionClosed,
                websockets.exceptions.WebSocketException,
                OSError,
            ):
                if self._running:
                    self.states[symbol].connected = False
                    await asyncio.sleep(min(backoff, 30.0))
                    backoff = min(backoff * 2, 30.0)

            except asyncio.CancelledError:
                return

    async def _connect_spot(self, symbol: str) -> None:
        env   = "test" if settings.bybit_testnet else "live"
        url   = self._URL[f"spot_{env}"]
        topics = [f"tickers.{symbol}"]
        await self._connect(url, topics, symbol, self._handle_spot)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        futures_tasks = [self._connect_futures(sym) for sym in settings.symbol_list]
        spot_tasks    = [self._connect_spot(sym)    for sym in settings.symbol_list]
        await asyncio.gather(*futures_tasks, *spot_tasks)

    def stop(self) -> None:
        self._running = False
