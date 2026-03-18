"""
core/paper_wallet.py
────────────────────
Paper trading — wallet virtual para probar estrategias sin dinero real.

· Fills instantáneos al precio de mercado actual
· Comprueba SL/TP en cada tick con precios reales del WebSocket
· Mantiene un AccountState sintético compatible con todo el pipeline
· PaperExecutor: drop-in replacement de BybitExecutor para el controller
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, TYPE_CHECKING

from core.order_model import OrderRequest, OrderResult
from streams.account import AccountState, AccountBalance, Position

if TYPE_CHECKING:
    from core.executor import BybitExecutor

log = logging.getLogger("qts.paper")

TAKER_FEE = 0.00055   # 0.055% por lado (igual que Bybit)


# ─── Posición virtual ────────────────────────────────────────────────────────

@dataclass
class PaperPosition:
    symbol:      str
    side:        str     # "Buy" | "Sell"
    size:        float
    entry_price: float
    sl_price:    float
    tp_price:    float
    leverage:    int   = 1
    margin:      float = 0.0
    opened_at:   int   = 0    # ms epoch


# ─── PaperWallet ─────────────────────────────────────────────────────────────

class PaperWallet:
    """
    Cartera virtual que simula ejecuciones y lleva la cuenta de PnL.

    Expone `self.state` (AccountState) que el pipeline usa exactamente
    igual que el estado de la cuenta real.
    """

    def __init__(self, starting_balance: float = 10_000.0) -> None:
        self.starting_balance: float = starting_balance
        self._cash:      float = starting_balance   # saldo líquido (sin margin usado)
        self._margin:    float = 0.0                # margen total en uso
        self._daily_pnl: float = 0.0
        self._total:     int   = 0
        self._wins:      int   = 0
        self._positions: Dict[str, PaperPosition] = {}

        # AccountState sintético — mismo tipo que usa todo el resto del sistema
        self.state = AccountState()
        self.state.connected = True
        self._sync_state()

    # ── Abrir posición ────────────────────────────────────────────────────────

    def open_position(self, req: OrderRequest, fill_price: float) -> OrderResult:
        """Simula fill instantáneo al precio actual de mercado."""
        lev       = req.leverage or 1
        notional  = req.qty * fill_price
        margin_req = notional / lev
        entry_fee  = notional * TAKER_FEE

        available = self._cash - self._margin
        if available < margin_req + entry_fee:
            return OrderResult(
                success=False, order_id="", filled_price=0.0, filled_qty=0.0,
                timestamp=int(time.time() * 1000),
                error_msg=(
                    f"[PAPER] Margen insuficiente — necesitas ${margin_req + entry_fee:.2f},"
                    f" disponible ${available:.2f}"
                ),
            )

        sym = req.symbol
        self._positions[sym] = PaperPosition(
            symbol      = sym,
            side        = req.side,
            size        = req.qty,
            entry_price = fill_price,
            sl_price    = req.sl_price,
            tp_price    = req.tp_price,
            leverage    = lev,
            margin      = margin_req,
            opened_at   = int(time.time() * 1000),
        )
        self._margin += margin_req
        self._cash   -= entry_fee
        self._sync_state()
        log.info("[PAPER] Opened %s %s @ %.4f  qty=%.2f  margin=$%.2f",
                 req.side, sym, fill_price, req.qty, margin_req)
        return OrderResult(
            success=True, order_id=f"paper-{uuid.uuid4().hex[:8]}",
            error_msg="", filled_price=fill_price,
            filled_qty=req.qty, timestamp=int(time.time() * 1000),
        )

    # ── Cerrar posición ───────────────────────────────────────────────────────

    def close_position(self, symbol: str, fill_price: float) -> float:
        """Cierra posición y retorna PnL neto (después de fees). 0 si no existe."""
        pp = self._positions.pop(symbol, None)
        if pp is None:
            return 0.0

        if pp.side == "Buy":
            gross = (fill_price - pp.entry_price) * pp.size
        else:
            gross = (pp.entry_price - fill_price) * pp.size

        exit_fee = fill_price * pp.size * TAKER_FEE
        net      = gross - exit_fee

        self._margin     = max(0.0, self._margin - pp.margin)
        self._cash      += pp.margin + net   # devolver margen + PnL
        self._daily_pnl += net
        self._total     += 1
        if net > 0:
            self._wins += 1

        self._sync_state()
        log.info("[PAPER] Closed %s %s @ %.4f  PnL net=$%.2f",
                 pp.side, symbol, fill_price, net)
        return net

    # ── Actualizar SL/TP ──────────────────────────────────────────────────────

    def update_sl_tp(self, symbol: str, sl: float, tp: float) -> bool:
        pp = self._positions.get(symbol)
        if pp is None:
            return False
        if sl > 0:
            pp.sl_price = sl
        if tp == 0.0:          # clear_tp → remove TP for trailing
            pp.tp_price = 0.0
        elif tp > 0:
            pp.tp_price = tp
        return True

    # ── Tick: comprobar SL/TP ─────────────────────────────────────────────────

    def tick(self, market_states: dict) -> List[tuple]:
        """
        Comprueba si alguna posición tocó SL o TP.
        Retorna lista de (symbol, reason) de posiciones cerradas este tick.
        Llamar desde GTK main thread (ya estamos en el tick del UI).
        """
        closed = []
        for sym, pp in list(self._positions.items()):
            ms    = market_states.get(sym)
            price = ms.ticker.last_price if (ms and ms.ticker.last_price > 0) else 0.0
            if price <= 0:
                continue

            hit_tp = (
                (pp.side == "Buy"  and pp.tp_price > 0 and price >= pp.tp_price) or
                (pp.side == "Sell" and pp.tp_price > 0 and price <= pp.tp_price)
            )
            hit_sl = (
                (pp.side == "Buy"  and pp.sl_price > 0 and price <= pp.sl_price) or
                (pp.side == "Sell" and pp.sl_price > 0 and price >= pp.sl_price)
            )

            if hit_tp:
                self.close_position(sym, pp.tp_price)
                closed.append((sym, "TP"))
            elif hit_sl:
                self.close_position(sym, pp.sl_price)
                closed.append((sym, "SL"))

        return closed

    # ── Actualizar PnL no realizado ───────────────────────────────────────────

    def update_mark_prices(self, market_states: dict) -> None:
        """Recalcula unrealized_pnl del balance con precios actuales."""
        upnl = 0.0
        for sym, pp in self._positions.items():
            ms    = market_states.get(sym)
            price = ms.ticker.last_price if (ms and ms.ticker.last_price > 0) else pp.entry_price
            # Actualizar mark_price en la posición sintética
            if sym in self.state.positions:
                self.state.positions[sym].mark_price      = price
                self.state.positions[sym].unrealized_pnl  = (
                    (price - pp.entry_price) * pp.size if pp.side == "Buy"
                    else (pp.entry_price - price) * pp.size
                )
                upnl += self.state.positions[sym].unrealized_pnl

        b = self.state.balance
        b.unrealized_pnl    = upnl
        b.total_equity      = self._cash + self._margin + upnl
        b.available_balance = max(0.0, self._cash - self._margin)

    # ── Reset ─────────────────────────────────────────────────────────────────

    def reset(self, new_balance: Optional[float] = None) -> None:
        """Reinicia el wallet a cero. Cierra todas las posiciones virtuales."""
        if new_balance is not None:
            self.starting_balance = new_balance
        self._cash      = self.starting_balance
        self._margin    = 0.0
        self._positions.clear()
        self._daily_pnl = 0.0
        self._total     = 0
        self._wins      = 0
        self._sync_state()
        log.info("[PAPER] Wallet reset — balance=$%.2f", self.starting_balance)

    # ── Propiedades derivadas ─────────────────────────────────────────────────

    @property
    def win_rate(self) -> float:
        return (self._wins / self._total * 100) if self._total > 0 else 0.0

    @property
    def total_pnl(self) -> float:
        return self._cash + self._margin - self.starting_balance

    @property
    def n_positions(self) -> int:
        return len(self._positions)

    # ── Sincronizar estado sintético ──────────────────────────────────────────

    def _sync_state(self) -> None:
        b = self.state.balance
        b.wallet_balance    = self._cash + self._margin
        b.total_equity      = self._cash + self._margin
        b.available_balance = max(0.0, self._cash - self._margin)
        b.used_margin       = self._margin
        b.unrealized_pnl    = 0.0
        self.state.daily_pnl = self._daily_pnl
        self.state.connected = True

        # Reconstruir positions dict
        self.state.positions.clear()
        for sym, pp in self._positions.items():
            self.state.positions[sym] = Position(
                symbol            = sym,
                side              = pp.side,
                size              = pp.size,
                entry_price       = pp.entry_price,
                mark_price        = pp.entry_price,   # se actualiza en update_mark_prices
                leverage          = float(pp.leverage),
                unrealized_pnl    = 0.0,
                liquidation_price = 0.0,
                take_profit       = pp.tp_price,
                stop_loss         = pp.sl_price,
                margin            = pp.margin,
                created_time      = pp.opened_at,
            )


# ─── PaperExecutor ───────────────────────────────────────────────────────────

class PaperExecutor:
    """
    Drop-in replacement de BybitExecutor para el controller.

    · Si settings.paper_trading=True  → desvía órdenes al PaperWallet
    · Si settings.paper_trading=False → delega al executor real
    · El caché de instrumentos (min_qty, qty_step, etc.) siempre viene del real
    · market_states debe inyectarse desde la app antes de empezar a operar
    """

    def __init__(self, wallet: PaperWallet, real: "BybitExecutor") -> None:
        self._wallet = wallet
        self._real   = real
        self.market_states: dict = {}   # inyectado desde gtk_app

    # ── Delegar info de instrumentos al real ─────────────────────────────────

    @property
    def _instruments(self):
        return self._real._instruments

    @property
    def _hedge_mode(self):
        return self._real._hedge_mode

    def get_info(self, symbol: str):
        return self._real.get_info(symbol)

    def round_qty(self, symbol: str, qty: float) -> float:
        return self._real.round_qty(symbol, qty)

    def validate_order(self, symbol: str, qty: float, entry_price: float):
        return self._real.validate_order(symbol, qty, entry_price)

    async def load_instrument_info(self, symbol: str):
        return await self._real.load_instrument_info(symbol)

    async def load_all_instruments(self, symbols: list) -> None:
        await self._real.load_all_instruments(symbols)

    def _pos_idx(self, side: str) -> int:
        return self._real._pos_idx(side)

    # ── Modo de operación ────────────────────────────────────────────────────

    @property
    def _paper(self) -> bool:
        from core.config import settings
        return settings.paper_trading

    def _price(self, symbol: str, fallback: float) -> float:
        ms = self.market_states.get(symbol)
        return (ms.ticker.last_price
                if ms and ms.ticker.last_price > 0 else fallback)

    # ── Órdenes ──────────────────────────────────────────────────────────────

    async def place_market_bracket(self, req: OrderRequest) -> OrderResult:
        if self._paper:
            price = self._price(req.symbol, req.entry_price)
            return self._wallet.open_position(req, price)
        return await self._real.place_market_bracket(req)

    async def place_limit_bracket(self, req: OrderRequest) -> OrderResult:
        if self._paper:
            # Límites se ejecutan como mercado en paper trading
            return await self.place_market_bracket(req)
        return await self._real.place_limit_bracket(req)

    async def set_sl_tp(
        self, symbol: str, sl: float = 0, tp: float = 0,
        side: str = "Buy", clear_tp: bool = False,
    ) -> bool:
        if self._paper:
            return self._wallet.update_sl_tp(symbol, sl, 0.0 if clear_tp else tp)
        return await self._real.set_sl_tp(symbol, sl, tp, side, clear_tp)

    async def close_position(
        self, symbol: str, qty: float, side: str,
    ) -> OrderResult:
        if self._paper:
            price = self._price(symbol, 0.0)
            pnl   = self._wallet.close_position(symbol, price)
            return OrderResult(
                success=True,
                order_id=f"paper-close-{int(time.time()*1000)}",
                error_msg="", filled_price=price,
                filled_qty=qty, timestamp=int(time.time() * 1000),
            )
        return await self._real.close_position(symbol, qty, side)

    async def get_position_open_time(
        self, symbol: str, since_ms: int = 0,
    ) -> int:
        if self._paper:
            pp = self._wallet._positions.get(symbol)
            return (pp.opened_at // 1000) if pp else 0
        return await self._real.get_position_open_time(symbol, since_ms)

    async def cancel_all_orders(self, symbol: str) -> bool:
        if self._paper:
            return True
        return await self._real.cancel_all_orders(symbol)

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        if self._paper:
            return True
        return await self._real.set_leverage(symbol, leverage)

    async def detect_position_mode(self) -> None:
        if not self._paper:
            await self._real.detect_position_mode()
