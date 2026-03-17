"""
core/controller.py
──────────────────
TradeController — gestiona el ciclo de vida completo de los trades.

Soporta hasta MAX_POSITIONS trades simultáneos en símbolos distintos.

Flujo FULL_AUTO:
  scan cada 30s → propuesta → pre-flight → ejecutar → monitorear →
  breakeven en +1R → trailing en +2R → reportar al cerrar

Thread model:
  · tick() corre en el main thread (GTK)
  · execute_*() coroutines corren en AsyncBridge (asyncio thread)
  · Callbacks de vuelta a GTK vía GLib.idle_add()
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Dict, List, Optional, Set, TYPE_CHECKING

from gi.repository import GLib

from core.order_model import (
    AutoMode, TradeState, TradeRecord, OrderRequest,
    OrderResult, ControllerState, MAX_POSITIONS,
)
from core.strategy import StrategyEngine
from core.db import save_trade
import core.notifier as notifier

if TYPE_CHECKING:
    from streams.market import MarketState
    from streams.account import AccountState
    from core.regime import OpportunitySignal
    from core.technicals import TechSignal
    from core.risk import RiskStatus, RiskFortress
    from core.executor import BybitExecutor
    from interface.gtk_app import AsyncBridge

log = logging.getLogger("qts.controller")

SCAN_INTERVAL      = 30
PROPOSAL_TTL       = 60
BREAKEVEN_AT_PCT   = 0.50
TRAILING_AT_PCT    = 0.80
TRAIL_MIN_MOVE_PCT = 0.003


class TradeController:
    """
    Gestiona hasta MAX_POSITIONS trades simultáneos en símbolos distintos.
    El modo controla cuánta autonomía tiene el sistema.
    """

    def __init__(
        self,
        executor:      "BybitExecutor",
        strategy:      StrategyEngine,
        risk_fortress: "RiskFortress",
        bridge:        "AsyncBridge",
        symbols:       List[str],
    ) -> None:
        self._executor      = executor
        self._strategy      = strategy
        self._risk_fortress = risk_fortress
        self._bridge        = bridge
        self._symbols       = symbols

        # Estado público
        self.mode:         AutoMode = AutoMode.MANUAL
        self.goal_usd:     float    = 1.0
        self.max_loss_usd: float    = 0.0
        self.leverage:     int      = 5

        # Estado interno — multi-posición
        self._active:        Dict[str, TradeRecord] = {}   # symbol → trade
        self._pending_exec:  Set[str]               = set()  # symbols en ejecución
        self._trail_high:    Dict[str, float]       = {}
        self._trail_low:     Dict[str, float]       = {}
        self._last_sl_upd:   Dict[str, float]       = {}

        self._proposal:      Optional[OrderRequest] = None
        self._proposal_ts:   float                  = 0.0
        self._log:           List[TradeRecord]      = []
        self._last_scan:     float                  = 0.0
        self._callbacks:     List[Callable]         = []

        self._last_error:    str   = ""
        self._last_error_ts: float = 0.0

    # ── API pública ───────────────────────────────────────────────────────────

    def set_mode(self, mode: AutoMode) -> None:
        if mode == self.mode:
            return
        log.info("AutoMode: %s → %s", self.mode, mode)
        if mode == AutoMode.MANUAL:
            self._proposal = None
        self.mode = mode
        self._notify()

    def set_goal(self, goal_usd: float) -> None:
        self.goal_usd = max(0.1, goal_usd)
        self._proposal = None
        self._notify()

    def set_max_loss(self, max_loss_usd: float) -> None:
        self.max_loss_usd = max(0.0, max_loss_usd)
        self._proposal = None
        self._notify()

    def set_leverage(self, leverage: int) -> None:
        self.leverage = max(1, min(25, leverage))

    def approve_proposal(self) -> None:
        if self._proposal and self._proposal.symbol not in self._pending_exec:
            self._execute(self._proposal)

    def reject_proposal(self) -> None:
        self._proposal = None
        self._last_scan = time.monotonic()
        self._notify()

    def close_symbol(self, symbol: str) -> None:
        """Cierra un trade específico a mercado."""
        trade = self._active.get(symbol)
        if trade and trade.is_active and trade.request:
            self._bridge.submit(self._do_close(symbol, trade.request))

    def close_now(self) -> None:
        """Cierra todos los trades activos (emergencia)."""
        for sym in list(self._active.keys()):
            self.close_symbol(sym)

    def force_scan(self) -> None:
        self._last_scan = 0.0

    def on_update(self, callback: Callable[[ControllerState], None]) -> None:
        self._callbacks.append(callback)

    @property
    def state(self) -> ControllerState:
        prop_age = int(time.monotonic() - self._proposal_ts) if self._proposal else 0
        scan_in  = max(0, int(SCAN_INTERVAL - (time.monotonic() - self._last_scan)))
        return ControllerState(
            mode           = self.mode,
            goal_usd       = self.goal_usd,
            proposal       = self._proposal,
            proposal_age_s = prop_age,
            active_trades  = list(self._active.values()),
            last_result    = self._log[-1] if self._log else None,
            scan_in        = scan_in,
            status_msg     = self._status_msg(),
        )

    @property
    def trade_log(self) -> List[TradeRecord]:
        return list(reversed(self._log[-10:]))

    # ── Tick principal (100ms, main thread) ───────────────────────────────────

    def tick(
        self,
        states:  Dict[str, "MarketState"],
        account: "AccountState",
        techs:   Dict[str, "TechSignal"],
        opps:    Dict[str, "OpportunitySignal"],
        risk:    "RiskStatus",
    ) -> None:
        self._sync_active_trades(account)

        if self.mode == AutoMode.MANUAL:
            return

        if risk.is_breaker:
            if self._active:
                log.warning("Circuit breaker — cerrando todas las posiciones")
                self.close_now()
            self._proposal = None
            return

        # Gestionar trades activos en FULL_AUTO
        if self._active and self.mode == AutoMode.FULL_AUTO:
            self._manage_active_trades(account, states)

        # Si estamos al límite de posiciones, no escanear
        if len(self._active) >= MAX_POSITIONS:
            return

        if self._pending_exec:
            return

        # Expirar propuesta vieja
        if self._proposal and (time.monotonic() - self._proposal_ts) > PROPOSAL_TTL:
            self._proposal = None
            self._notify()

        # Scan periódico
        if self._proposal is None:
            if (time.monotonic() - self._last_scan) >= SCAN_INTERVAL:
                self._last_scan = time.monotonic()
                self._run_scan(states, account, techs, opps, risk)

        # Auto-ejecución
        if self._proposal and self.mode in (AutoMode.AUTO_ENTRY, AutoMode.FULL_AUTO):
            ok, reason = self._pre_flight(self._proposal, account, risk)
            if ok:
                self._execute(self._proposal)
            else:
                log.debug("Pre-flight failed: %s", reason)
                self._proposal = None

    # ── Scan ──────────────────────────────────────────────────────────────────

    def _run_scan(
        self,
        states:  Dict[str, "MarketState"],
        account: "AccountState",
        techs:   Dict[str, "TechSignal"],
        opps:    Dict[str, "OpportunitySignal"],
        risk:    "RiskStatus",
    ) -> None:
        # Solo escanear símbolos sin posición activa
        available = [s for s in self._symbols if s not in self._active]

        for sym in available:
            opp  = opps.get(sym)
            tech = techs.get(sym)
            log.debug("scan %s: score=%s has_data=%s",
                      sym,
                      opp.score if opp else "n/a",
                      tech.has_data if tech else "n/a")

        result = self._strategy.scan_all(
            symbols      = available,
            states       = states,
            opps         = opps,
            techs        = techs,
            account      = account,
            goal_usd     = self.goal_usd,
            executor     = self._executor,
            leverage     = self.leverage,
            max_loss_usd = self.max_loss_usd,
        )
        if result:
            sym, proposal = result
            ok, reason = self._pre_flight(proposal, account, risk)
            if ok:
                self._proposal    = proposal
                self._proposal_ts = time.monotonic()
                log.info("Nueva propuesta: %s", proposal.summary())
                if self.mode == AutoMode.SUGGEST:
                    notifier.proposal_ready(sym, proposal.side, proposal.opp_score, self.goal_usd)
                self._notify()
            else:
                log.info("Propuesta descartada (pre-flight): %s — %s", proposal.symbol, reason)
        else:
            log.info("Scan: sin setup válido (score < 55 o filtros técnicos)")

    # ── Pre-flight ────────────────────────────────────────────────────────────

    def _pre_flight(
        self,
        req:     OrderRequest,
        account: "AccountState",
        risk:    "RiskStatus",
    ) -> tuple[bool, str]:
        if risk.is_breaker:
            return False, "circuit breaker activo"
        if req.symbol in self._active:
            return False, f"ya hay posición activa en {req.symbol}"
        if req.symbol in account.positions:
            return False, f"ya hay posición abierta en {req.symbol}"
        if len(self._active) >= MAX_POSITIONS:
            return False, f"máximo {MAX_POSITIONS} posiciones simultáneas"
        avail = account.balance.available_balance
        if avail > 0 and req.margin > avail * 0.95:
            return False, f"margen requerido ${req.margin:.2f} > disponible ${avail:.2f}"
        if req.qty <= 0:
            return False, "qty = 0"
        if req.rr_ratio < 1.5:
            return False, f"R:R {req.rr_ratio:.1f} demasiado bajo"
        return True, ""

    # ── Ejecución ─────────────────────────────────────────────────────────────

    def _execute(self, req: OrderRequest) -> None:
        if req.symbol in self._pending_exec:
            return
        self._pending_exec.add(req.symbol)
        self._proposal = None

        trade = TradeRecord(
            symbol     = req.symbol,
            request    = req,
            state      = TradeState.SUBMITTED,
            current_sl = req.sl_price,
            current_tp = req.tp_price,
            auto_mode  = self.mode,
        )
        self._active[req.symbol] = trade

        async def _do() -> None:
            await self._executor.set_leverage(req.symbol, req.leverage)
            result = await self._executor.place_market_bracket(req)
            if result.success and (req.sl_price > 0 or req.tp_price > 0):
                import asyncio as _aio
                await _aio.sleep(0.5)
                await self._executor.set_sl_tp(
                    req.symbol, sl=req.sl_price, tp=req.tp_price, side=req.side
                )
                log.info("SL/TP confirmados: %s  SL=%s  TP=%s",
                         req.symbol, req.sl_price, req.tp_price)
            GLib.idle_add(self._on_order_result, result, req)

        self._bridge.submit(_do())
        log.info("Ejecutando: %s", req.summary())
        self._notify()

    def _on_order_result(self, result: OrderResult, req: OrderRequest) -> bool:
        self._pending_exec.discard(req.symbol)
        trade = self._active.get(req.symbol)

        if result.success:
            if trade:
                trade.state       = TradeState.OPEN
                trade.result      = result
                trade.opened_at   = int(time.time())
                trade.entry_price = req.entry_price
                trade.pnl_at_open = 0.0   # se establece en primer tick con account data
                self._trail_high[req.symbol] = req.entry_price
                self._trail_low[req.symbol]  = req.entry_price
                self._last_sl_upd[req.symbol] = 0.0
            log.info("Orden confirmada: %s  id=%s", req.symbol, result.order_id)
            notifier.trade_opened(
                req.symbol, req.side, req.entry_price,
                req.sl_price, req.tp_price, self.goal_usd,
            )
        else:
            msg = result.error_msg or "error desconocido"
            log.warning("Orden falló: %s — %s", req.symbol, msg)
            self._last_error    = f"✗ {req.symbol}: {msg}"
            self._last_error_ts = time.monotonic()
            notifier.order_failed(req.symbol, msg)
            if trade:
                trade.state       = TradeState.FAILED
                trade.close_reason = msg
                self._log.append(trade)
                save_trade(trade)
                del self._active[req.symbol]

        self._notify()
        return False

    # ── Sincronización con la cuenta ──────────────────────────────────────────

    def _sync_active_trades(self, account: "AccountState") -> None:
        """Detecta cierres por SL/TP/manual para todos los trades activos."""
        closed: List[str] = []
        for sym, trade in self._active.items():
            if not trade.is_active:
                continue
            pos = account.positions.get(sym)
            if pos is None or pos.size <= 0:
                trade.state       = TradeState.CLOSED
                trade.closed_at   = int(time.time())
                trade.pnl_usd     = account.daily_pnl - trade.pnl_at_open
                trade.close_reason = "SL/TP/manual"
                log.info("Trade cerrado: %s  PnL≈$%.2f", sym, trade.pnl_usd)
                self._log.append(trade)
                save_trade(trade)
                notifier.trade_closed(sym, trade.pnl_usd, trade.close_reason)
                closed.append(sym)

        for sym in closed:
            self._active.pop(sym, None)
            self._trail_high.pop(sym, None)
            self._trail_low.pop(sym, None)
            self._last_sl_upd.pop(sym, None)
        if closed:
            self._notify()

    # ── Gestión activa (FULL_AUTO) ────────────────────────────────────────────

    def _manage_active_trades(
        self,
        account: "AccountState",
        states:  Dict[str, "MarketState"],
    ) -> None:
        for sym, trade in list(self._active.items()):
            self._manage_one(sym, trade, account)

    def _manage_one(
        self,
        sym:     str,
        trade:   TradeRecord,
        account: "AccountState",
    ) -> None:
        req = trade.request
        if not req:
            return

        pos = account.positions.get(sym)
        if not pos or pos.size <= 0:
            return

        mark    = pos.mark_price if pos.mark_price > 0 else pos.entry_price
        entry   = trade.entry_price or pos.entry_price
        sl      = trade.current_sl
        tp      = trade.current_tp
        is_long = req.side == "Buy"

        if entry <= 0 or tp <= 0 or sl <= 0:
            return

        sl_dist = abs(entry - sl)
        tp_dist = abs(tp - entry)
        if sl_dist <= 0 or tp_dist <= 0:
            return

        progress = (mark - entry) / tp_dist if is_long else (entry - mark) / tp_dist
        now      = time.monotonic()

        # ── Breakeven ─────────────────────────────────────────────────────
        if (
            trade.state == TradeState.OPEN
            and progress >= BREAKEVEN_AT_PCT
            and sl != entry
        ):
            new_sl = entry
            log.info("Breakeven: %s SL → %s", sym, new_sl)
            trade.state      = TradeState.BREAKEVEN
            trade.current_sl = new_sl
            self._bridge.submit(
                self._executor.set_sl_tp(sym, sl=new_sl, side=req.side)
            )
            self._last_sl_upd[sym] = now
            notifier.breakeven_activated(sym, new_sl)
            self._notify()

        # ── Trailing ──────────────────────────────────────────────────────
        elif (
            trade.state in (TradeState.BREAKEVEN, TradeState.TRAILING)
            and progress >= TRAILING_AT_PCT
            and (now - self._last_sl_upd.get(sym, 0)) >= 5.0
        ):
            atr = sl_dist / 1.5
            if is_long:
                self._trail_high[sym] = max(self._trail_high.get(sym, mark), mark)
                new_sl = self._trail_high[sym] - atr * 1.5
                if new_sl > trade.current_sl * (1 + TRAIL_MIN_MOVE_PCT):
                    log.info("Trailing LONG: %s SL %s → %s", sym, trade.current_sl, new_sl)
                    first_trail = trade.state != TradeState.TRAILING
                    trade.state      = TradeState.TRAILING
                    trade.current_sl = new_sl
                    self._bridge.submit(
                        self._executor.set_sl_tp(sym, sl=new_sl, side=req.side)
                    )
                    self._last_sl_upd[sym] = now
                    if first_trail:
                        notifier.trailing_activated(sym, new_sl)
                    self._notify()
            else:
                self._trail_low[sym] = min(self._trail_low.get(sym, mark), mark)
                new_sl = self._trail_low[sym] + atr * 1.5
                if new_sl < trade.current_sl * (1 - TRAIL_MIN_MOVE_PCT):
                    log.info("Trailing SHORT: %s SL %s → %s", sym, trade.current_sl, new_sl)
                    first_trail = trade.state != TradeState.TRAILING
                    trade.state      = TradeState.TRAILING
                    trade.current_sl = new_sl
                    self._bridge.submit(
                        self._executor.set_sl_tp(sym, sl=new_sl, side=req.side)
                    )
                    self._last_sl_upd[sym] = now
                    if first_trail:
                        notifier.trailing_activated(sym, new_sl)
                    self._notify()

    async def _do_close(self, symbol: str, req: OrderRequest) -> None:
        result = await self._executor.close_position(symbol, req.qty, req.side)
        GLib.idle_add(self._on_close_result, symbol, result)

    def _on_close_result(self, symbol: str, result: OrderResult) -> bool:
        trade = self._active.get(symbol)
        if trade:
            trade.state       = TradeState.CLOSED
            trade.closed_at   = int(time.time())
            trade.close_reason = "manual" if result.success else result.error_msg
            self._log.append(trade)
            save_trade(trade)
            notifier.trade_closed(symbol, trade.pnl_usd, trade.close_reason)
            del self._active[symbol]
            self._trail_high.pop(symbol, None)
            self._trail_low.pop(symbol, None)
            self._last_sl_upd.pop(symbol, None)
        self._notify()
        return False

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _notify(self) -> None:
        cs = self.state
        for cb in self._callbacks:
            try:
                cb(cs)
            except Exception:
                pass

    def _status_msg(self) -> str:
        if self._pending_exec:
            return f"Ejecutando orden… ({', '.join(self._pending_exec)})"
        if self._last_error and (time.monotonic() - self._last_error_ts) < 30:
            return self._last_error
        if self._active:
            state_map = {
                TradeState.SUBMITTED: "WAIT",
                TradeState.OPEN:      "OPEN",
                TradeState.BREAKEVEN: "BE",
                TradeState.TRAILING:  "TR",
            }
            parts = [
                f"{sym.replace('USDT','')}:{state_map.get(t.state, '?')}"
                for sym, t in self._active.items()
            ]
            return "Posiciones: " + "  ".join(parts)
        if self._proposal:
            age = int(time.monotonic() - self._proposal_ts)
            return f"Propuesta lista ({age}s) — confirma o rechaza"
        if self.mode == AutoMode.MANUAL:
            return "Modo MANUAL — señales activas"
        remaining = int(SCAN_INTERVAL - (time.monotonic() - self._last_scan))
        return f"Escaneando en {max(0, remaining)}s…"
