"""
core/controller.py
──────────────────
TradeController — gestiona el ciclo de vida completo de los trades.

Conecta: StrategyEngine + BybitExecutor + RiskFortress.
Se llama .tick() desde MainWindow._refresh() cada 100ms.

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
from typing import Callable, Dict, List, Optional, TYPE_CHECKING

from gi.repository import GLib

from core.order_model import (
    AutoMode, TradeState, TradeRecord, OrderRequest,
    OrderResult, ControllerState,
)
from core.strategy import StrategyEngine

if TYPE_CHECKING:
    from streams.market import MarketState
    from streams.account import AccountState
    from core.regime import OpportunitySignal
    from core.technicals import TechSignal
    from core.risk import RiskStatus, RiskFortress
    from core.executor import BybitExecutor
    from interface.gtk_app import AsyncBridge

log = logging.getLogger("qts.controller")

# Segundos entre scans (evitar llamadas excesivas a la estrategia)
SCAN_INTERVAL    = 30
# Segundos antes de que una propuesta SUGGEST expire
PROPOSAL_TTL     = 60
# Porcentaje del camino hacia el TP para activar breakeven (~50% = +1R)
BREAKEVEN_AT_PCT = 0.50
# Porcentaje del camino hacia el TP para activar trailing
TRAILING_AT_PCT  = 0.80
# Mínimo movimiento de SL para enviar actualización (evitar spam API)
TRAIL_MIN_MOVE_PCT = 0.003   # 0.3% del precio


class TradeController:
    """
    Gestiona el ciclo de vida de trades de forma autónoma o semi-autónoma.
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
        self.mode:           AutoMode                  = AutoMode.MANUAL
        self.goal_usd:       float                     = 1.0
        self.max_loss_usd:   float                     = 0.0   # 0 = usar % del equity
        self.leverage:       int                       = 5

        # Estado interno
        self._proposal:      Optional[OrderRequest]   = None
        self._proposal_ts:   float                     = 0.0
        self._active:        Optional[TradeRecord]    = None
        self._log:           List[TradeRecord]         = []   # historial
        self._last_scan:     float                     = 0.0
        self._scan_ctr:      int                       = 0    # ticks desde último scan
        self._trailing_high: float                     = 0.0
        self._trailing_low:  float                     = 9e9
        self._last_sl_update: float                    = 0.0
        self._callbacks:     List[Callable]            = []

        # Para recibir resultados de AsyncBridge (thread-safe via GLib.idle_add)
        self._pending_exec:  bool                      = False
        self._last_error:    str                       = ""
        self._last_error_ts: float                     = 0.0

    # ── API pública ───────────────────────────────────────────────────────────

    def set_mode(self, mode: AutoMode) -> None:
        if mode == self.mode:
            return
        log.info("AutoMode: %s → %s", self.mode, mode)
        if mode == AutoMode.MANUAL:
            self._proposal = None  # cancelar propuesta al pasar a manual
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
        """SUGGEST: usuario aprobó. Ejecutar la propuesta."""
        if self._proposal and not self._pending_exec:
            self._execute(self._proposal)

    def reject_proposal(self) -> None:
        """SUGGEST: usuario rechazó."""
        self._proposal = None
        self._last_scan = time.monotonic()  # esperar otro intervalo
        self._notify()

    def close_now(self) -> None:
        """Cierre de emergencia a mercado."""
        if self._active and self._active.is_active:
            req = self._active.request
            if req:
                self._bridge.submit(
                    self._do_close(self._active.symbol, self._active.request)
                )

    def force_scan(self) -> None:
        """Forzar un scan inmediato (usuario hizo click en 'Scan ahora')."""
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
            active_trade   = self._active,
            last_result    = self._log[-1] if self._log else None,
            scan_in        = scan_in,
            status_msg     = self._status_msg(),
        )

    @property
    def trade_log(self) -> List[TradeRecord]:
        return list(reversed(self._log[-10:]))   # últimos 10, más reciente primero

    # ── Tick principal (100ms, main thread) ───────────────────────────────────

    def tick(
        self,
        states:   Dict[str, "MarketState"],
        account:  "AccountState",
        techs:    Dict[str, "TechSignal"],
        opps:     Dict[str, "OpportunitySignal"],
        risk:     "RiskStatus",
    ) -> None:

        # ── Sincronizar trade activo con la cuenta real ────────────────────
        self._sync_active_trade(account)

        if self.mode == AutoMode.MANUAL:
            return

        # ── Circuit breaker: parar todo ────────────────────────────────────
        if risk.is_breaker:
            if self._active and self._active.is_active:
                log.warning("Circuit breaker activo — cerrando posición")
                self.close_now()
            self._proposal = None
            return

        # ── Gestión del trade activo ───────────────────────────────────────
        if self._active and self._active.is_active:
            if self.mode == AutoMode.FULL_AUTO:
                self._manage_active(account, states)
            return  # no buscar nuevas entradas mientras hay trade activo

        # ── Scan de oportunidades ──────────────────────────────────────────
        if self._pending_exec:
            return  # esperando resultado de ejecución

        # Expirar propuesta antigua
        if self._proposal and (time.monotonic() - self._proposal_ts) > PROPOSAL_TTL:
            log.debug("Propuesta expirada")
            self._proposal = None
            self._notify()

        # Scan periódico
        if self._proposal is None:
            if (time.monotonic() - self._last_scan) >= SCAN_INTERVAL:
                self._last_scan = time.monotonic()
                self._run_scan(states, account, techs, opps, risk)

        # Auto-ejecución en modos AUTO
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
        # Log de diagnóstico antes del scan
        for sym in self._symbols:
            opp  = opps.get(sym)
            tech = techs.get(sym)
            log.debug("scan %s: score=%s has_data=%s",
                      sym,
                      opp.score if opp else "n/a",
                      tech.has_data if tech else "n/a")

        result = self._strategy.scan_all(
            symbols      = self._symbols,
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
                self._notify()
            else:
                log.info("Propuesta descartada (pre-flight): %s — %s", proposal.symbol, reason)
        else:
            log.info("Scan: sin setup válido (score < %d o filtros técnicos)", 55)

    # ── Pre-flight ────────────────────────────────────────────────────────────

    def _pre_flight(
        self,
        req:     OrderRequest,
        account: "AccountState",
        risk:    "RiskStatus",
    ) -> tuple[bool, str]:
        if risk.is_breaker:
            return False, "circuit breaker activo"
        if req.symbol in account.positions:
            return False, f"ya hay posición abierta en {req.symbol}"
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
        if self._pending_exec:
            return
        self._pending_exec = True
        self._proposal = None

        # Crear TradeRecord preliminar
        trade = TradeRecord(
            symbol     = req.symbol,
            request    = req,
            state      = TradeState.SUBMITTED,
            current_sl = req.sl_price,
            current_tp = req.tp_price,
            auto_mode  = self.mode,
        )
        self._active = trade

        async def _do() -> None:
            await self._executor.set_leverage(req.symbol, req.leverage)
            result = await self._executor.place_market_bracket(req)
            # Bybit ignora SL/TP en market orders si el fill es instantáneo.
            # Enviamos set_sl_tp inmediatamente después del fill como confirmación.
            if result.success and (req.sl_price > 0 or req.tp_price > 0):
                import asyncio as _aio
                await _aio.sleep(0.5)   # pequeña pausa para que Bybit registre la posición
                await self._executor.set_sl_tp(
                    req.symbol,
                    sl   = req.sl_price,
                    tp   = req.tp_price,
                    side = req.side,
                )
                log.info("SL/TP confirmados: %s  SL=%s  TP=%s",
                         req.symbol, req.sl_price, req.tp_price)
            GLib.idle_add(self._on_order_result, result, req)

        self._bridge.submit(_do())
        log.info("Ejecutando: %s", req.summary())
        self._notify()

    def _on_order_result(self, result: OrderResult, req: OrderRequest) -> bool:
        """Callback en main thread (via GLib.idle_add)."""
        self._pending_exec = False

        if result.success:
            if self._active:
                self._active.state      = TradeState.OPEN
                self._active.result     = result
                self._active.opened_at  = int(time.time())
                # El precio de entrada real vendrá del WebSocket privado
                self._active.entry_price = req.entry_price
                self._trailing_high      = req.entry_price
                self._trailing_low       = req.entry_price
            log.info("Orden confirmada: %s  id=%s", req.symbol, result.order_id)
        else:
            msg = result.error_msg or "error desconocido"
            log.warning("Orden falló: %s — %s", req.symbol, msg)
            self._last_error    = f"✗ {req.symbol}: {msg}"
            self._last_error_ts = time.monotonic()
            if self._active:
                self._active.state        = TradeState.FAILED
                self._active.close_reason = msg
                self._log.append(self._active)
                self._active = None

        self._notify()
        return False   # GLib.idle_add no repetir

    # ── Gestión del trade activo (FULL_AUTO) ──────────────────────────────────

    def _sync_active_trade(self, account: "AccountState") -> None:
        """
        Detecta si la posición fue cerrada por SL/TP/manual y actualiza estado.
        """
        if not self._active or not self._active.is_active:
            return

        sym = self._active.symbol
        pos = account.positions.get(sym)

        if pos is None or pos.size <= 0:
            # Posición cerrada
            self._active.state      = TradeState.CLOSED
            self._active.closed_at  = int(time.time())
            self._active.pnl_usd    = account.daily_pnl   # aproximación
            self._active.close_reason = "SL/TP/manual"
            log.info("Trade cerrado: %s  PnL≈${:.2f}".format(self._active.pnl_usd),
                     sym)
            self._log.append(self._active)
            self._active = None
            self._notify()

    def _manage_active(
        self,
        account: "AccountState",
        states:  Dict[str, "MarketState"],
    ) -> None:
        """
        Gestión activa: breakeven + trailing stop.
        Solo en FULL_AUTO.
        """
        if not self._active:
            return

        sym = self._active.symbol
        pos = account.positions.get(sym)
        if not pos or pos.size <= 0:
            return

        mark      = pos.mark_price if pos.mark_price > 0 else pos.entry_price
        entry     = self._active.entry_price or pos.entry_price
        sl        = self._active.current_sl
        tp        = self._active.current_tp
        is_long   = self._active.request and self._active.request.side == "Buy"

        if entry <= 0 or tp <= 0 or sl <= 0:
            return

        sl_dist = abs(entry - sl)
        tp_dist = abs(tp - entry)
        if sl_dist <= 0 or tp_dist <= 0:
            return

        # Progreso hacia el TP (0.0 = en entrada, 1.0 = en TP)
        if is_long:
            progress = (mark - entry) / tp_dist
        else:
            progress = (entry - mark) / tp_dist

        now = time.monotonic()

        # ── Breakeven: mover SL a entrada cuando progress >= 50% ──────────
        if (
            self._active.state == TradeState.OPEN
            and progress >= BREAKEVEN_AT_PCT
            and (sl != entry)
        ):
            new_sl = entry
            log.info("Breakeven: %s SL → %s", sym, new_sl)
            self._active.state      = TradeState.BREAKEVEN
            self._active.current_sl = new_sl
            self._bridge.submit(
                self._executor.set_sl_tp(sym, sl=new_sl, side=req.side)
            )
            self._last_sl_update = now
            self._notify()

        # ── Trailing: activar cuando progress >= 80% ──────────────────────
        elif (
            self._active.state in (TradeState.BREAKEVEN, TradeState.TRAILING)
            and progress >= TRAILING_AT_PCT
            and (now - self._last_sl_update) >= 5.0   # debounce 5s
        ):
            # Trailing de 1.5x ATR
            # Usamos ATR guardado en el request (de la propuesta original)
            # Si no tenemos ATR, usamos el sl_dist como proxy
            atr = sl_dist / 1.5  # reversing: sl = entry - atr*1.5

            if is_long:
                self._trailing_high = max(self._trailing_high, mark)
                new_sl = self._trailing_high - atr * 1.5
                # Solo actualizar si mejora significativamente
                if new_sl > self._active.current_sl * (1 + TRAIL_MIN_MOVE_PCT):
                    log.info("Trailing LONG: %s SL %s → %s", sym,
                             self._active.current_sl, new_sl)
                    self._active.state      = TradeState.TRAILING
                    self._active.current_sl = new_sl
                    self._bridge.submit(
                        self._executor.set_sl_tp(sym, sl=new_sl, side=req.side)
                    )
                    self._last_sl_update = now
                    self._notify()
            else:
                self._trailing_low = min(self._trailing_low, mark)
                new_sl = self._trailing_low + atr * 1.5
                if new_sl < self._active.current_sl * (1 - TRAIL_MIN_MOVE_PCT):
                    log.info("Trailing SHORT: %s SL %s → %s", sym,
                             self._active.current_sl, new_sl)
                    self._active.state      = TradeState.TRAILING
                    self._active.current_sl = new_sl
                    self._bridge.submit(
                        self._executor.set_sl_tp(sym, sl=new_sl, side=req.side)
                    )
                    self._last_sl_update = now
                    self._notify()

    async def _do_close(self, symbol: str, req: OrderRequest) -> None:
        result = await self._executor.close_position(symbol, req.qty, req.side)
        GLib.idle_add(self._on_close_result, result)

    def _on_close_result(self, result: OrderResult) -> bool:
        if self._active:
            self._active.state       = TradeState.CLOSED
            self._active.closed_at   = int(time.time())
            self._active.close_reason = "manual close" if result.success else result.error_msg
            self._log.append(self._active)
            self._active = None
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
            return "Ejecutando orden…"
        if self._active:
            t = self._active
            if t.state == TradeState.SUBMITTED:
                return f"Esperando fill: {t.symbol}"
            if t.state == TradeState.OPEN:
                return f"Posición abierta: {t.symbol}"
            if t.state == TradeState.BREAKEVEN:
                return f"Breakeven activo: {t.symbol}"
            if t.state == TradeState.TRAILING:
                return f"Trailing activo: {t.symbol}"
        if self._proposal:
            age = int(time.monotonic() - self._proposal_ts)
            return f"Propuesta lista ({age}s) — confirma o rechaza"
        if self._last_error and (time.monotonic() - self._last_error_ts) < 30:
            return self._last_error
        if self.mode == AutoMode.MANUAL:
            return "Modo MANUAL — señales activas"
        remaining = int(SCAN_INTERVAL - (time.monotonic() - self._last_scan))
        return f"Escaneando en {max(0, remaining)}s…"
