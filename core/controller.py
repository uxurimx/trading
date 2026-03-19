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
    OrderResult, ControllerState,
)
from core.strategy import StrategyEngine
from core.config import settings
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

PROPOSAL_TTL       = 60
TRAIL_MIN_MOVE_PCT = 0.003
PROFIT_LOCK_RATIO  = 0.60   # SL se mueve a entry + 60% del sl_dist (era 0.40)
MAX_SAME_DIRECTION = 4      # máximo trades simultáneos en la misma dirección
# scan_interval_s, breakeven_pct, profit_lock_pct, trailing_pct, be_hold_time_s vienen de settings

# ── Smart Profit Guard ────────────────────────────────────────────────────────
# Bloquea ganancias parciales cuando hay confluencia de señales de debilitamiento.
# Actúa a partir del 30% de progreso, antes que el breakeven estándar (40%).
SMART_GUARD_MIN_PROGRESS = 0.30   # umbral mínimo para activar el guard
SMART_GUARD_WEAKNESS_REQ = 3      # puntuación de debilidad necesaria (0-6)
SMART_GUARD_LOCK_FRAC    = 0.45   # bloquear 45% de las ganancias actuales
SMART_GUARD_ATR_ROOM     = 0.65   # dar mínimo 0.65×ATR de espacio desde el mark
SMART_GUARD_COOLDOWN     = 10.0   # segundos mínimos entre ajustes consecutivos

# ── Breakeven fee-based ───────────────────────────────────────────────────────
# RT fees (0.055% × 2) + margen de seguridad (0.05%) para garantizar PnL ≥ 0
_BE_FEE_PCT = 0.00055 * 2 + 0.0005   # 0.16% del precio de entrada


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
        self._scan_log:      str   = ""   # último resultado del scan

        self.max_duration_min: int = 0    # 0 = sin límite
        self.multi_trades:     int = 1    # cuántos trades en paralelo para alcanzar el goal
        self._close_confirm:   Dict[str, int] = {}  # ticks consecutivos sin posición
        self._pnl_captured:  Set[str]         = set()   # trades con pnl_at_open ya capturado
        self._last_upnl:     Dict[str, float]  = {}      # último unrealized PnL conocido
        self._be_since:      Dict[str, float]  = {}      # cuándo progress alcanzó BE threshold
        self._duration_set_ts: float           = time.time()  # cuándo se configuró max_duration
        self._open_ts:       Dict[str, float]  = {}  # monotonic timestamp al confirmar OPEN
        self._latest_opps:   dict              = {}  # última foto de OpportunitySignal por símbolo
        self._position_seen: Set[str]          = set()  # símbolos vistos al menos 1 vez en WS
        self._tp_removed:    Set[str]          = set()  # trades con TP eliminado (trail extendido)
        self._partial_lock_done: Set[str]      = set()  # trades con partial lock ya aplicado
        self._pct80_analyzed:    Set[str]      = set()  # trades con análisis de 80% ya ejecutado
        self._consec_losses: Dict[str, int]    = {}     # pérdidas consecutivas por símbolo
        # sym → (side, monotonic_expire): cooldown tras weak_exit/time_stop
        self._exit_cooldown: Dict[str, tuple] = {}
        # historial reciente de resultados para detector de mercado choppy
        self._recent_results: List[str]       = []   # "win"/"loss" (últimos 10)

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
        self.leverage = max(1, min(75, leverage))

    def set_max_duration(self, minutes: int) -> None:
        self.max_duration_min = max(0, minutes)
        # Registrar cuándo se configuró el límite.
        # El límite SOLO aplica a trades abiertos DESPUÉS de este momento.
        # Esto evita cerrar posiciones existentes al cambiar la configuración.
        if minutes > 0:
            self._duration_set_ts = time.time()

    def set_multi_trades(self, n: int) -> None:
        self.multi_trades = max(1, min(10, n))

    def set_trade_mode(self, symbol: str, mode: AutoMode) -> None:
        """Cambia el modo de gestión de un trade activo individual."""
        trade = self._active.get(symbol)
        if trade:
            log.info("Trade %s: modo %s → %s", symbol, trade.auto_mode.value, mode.value)
            trade.auto_mode = mode
            self._notify()

    def approve_proposal(self) -> None:
        if self._proposal and self._proposal.symbol not in self._pending_exec:
            self._execute(self._proposal)

    def reject_proposal(self) -> None:
        self._proposal = None
        self._last_scan = time.monotonic()
        self._notify()

    def close_symbol(self, symbol: str, reason: str = "manual") -> None:
        """Cierra un trade específico a mercado."""
        trade = self._active.get(symbol)
        if trade and trade.is_active and trade.request:
            if reason in ("weak_exit", "time_stop") and settings.symbol_cooldown_s > 0:
                expire = time.monotonic() + settings.symbol_cooldown_s
                self._exit_cooldown[symbol] = (trade.request.side, expire)
                log.info("Cooldown %s: no re-entrar %s por %ds",
                         reason, symbol, settings.symbol_cooldown_s)
            self._bridge.submit(self._do_close(symbol, trade.request, reason))

    def close_now(self) -> None:
        """Cierra todos los trades activos (emergencia)."""
        for sym in list(self._active.keys()):
            self.close_symbol(sym, "emergencia")

    def force_scan(self) -> None:
        self._last_scan = 0.0
        self._scan_log  = "🔍 Escaneando…"
        self._notify()

    def on_update(self, callback: Callable[[ControllerState], None]) -> None:
        self._callbacks.append(callback)

    @property
    def state(self) -> ControllerState:
        prop_age = int(time.monotonic() - self._proposal_ts) if self._proposal else 0
        scan_in  = max(0, int(settings.scan_interval_s - (time.monotonic() - self._last_scan)))
        return ControllerState(
            mode           = self.mode,
            goal_usd       = self.goal_usd,
            proposal       = self._proposal,
            proposal_age_s = prop_age,
            active_trades  = list(self._active.values()),
            last_result    = self._log[-1] if self._log else None,
            scan_in        = scan_in,
            status_msg     = self._status_msg(),
            scan_log       = self._scan_log,
        )

    @property
    def trade_log(self) -> List[TradeRecord]:
        return list(reversed(self._log[-10:]))

    # ── Tick principal (100ms, main thread) ───────────────────────────────────

    def live_scores(self, n: int = 8) -> list:
        """Top-N símbolos por score actual. Para radar en tiempo real en la UI."""
        items = [
            (sym, opp.score, opp.direction, opp.regime.label)
            for sym, opp in self._latest_opps.items()
            if opp is not None
        ]
        return sorted(items, key=lambda x: x[1], reverse=True)[:n]

    def tick(
        self,
        states:  Dict[str, "MarketState"],
        account: "AccountState",
        techs:   Dict[str, "TechSignal"],
        opps:    Dict[str, "OpportunitySignal"],
        risk:    "RiskStatus",
    ) -> None:
        self._latest_opps = opps  # actualizar radar en vivo
        self._sync_active_trades(account)

        # Capturar baseline de PnL al primer tick activo del trade
        for sym, trade in self._active.items():
            if trade.is_active and sym not in self._pnl_captured:
                trade.pnl_at_open = account.daily_pnl
                self._pnl_captured.add(sym)

        # Gestionar trades activos SIEMPRE (respeta el modo por-trade).
        # El modo global MANUAL solo bloquea nuevas entradas, no la gestión
        # de trades que el usuario ha activado individualmente con AUTO.
        if self._active:
            self._manage_active_trades(account, states)

        if self.mode == AutoMode.MANUAL:
            return

        if risk.is_breaker:
            # Solo bloquea nuevas entradas — nunca cierra posiciones activas.
            # Cerrar posiciones activas es decisión del trader, no del sistema.
            self._proposal = None
            self._scan_log = f"🔴 Circuit breaker activo — {risk.message}"
            return

        if self._pending_exec:
            return

        # Expirar propuesta vieja
        if self._proposal and (time.monotonic() - self._proposal_ts) > PROPOSAL_TTL:
            self._proposal = None
            self._notify()

        # Scan periódico
        if self._proposal is None:
            if (time.monotonic() - self._last_scan) >= settings.scan_interval_s:
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
        # Solo escanear símbolos sin posición activa, ejecución pendiente ni blacklist
        blacklist = settings.blacklist_set
        available = [s for s in self._symbols
                     if s not in self._active
                     and s not in self._pending_exec
                     and s not in blacklist]

        # Recopilar scores para feedback
        scores: dict[str, int] = {}
        for sym in available:
            opp  = opps.get(sym)
            tech = techs.get(sym)
            if opp:
                scores[sym] = opp.score
            log.debug("scan %s: score=%s has_data=%s",
                      sym,
                      opp.score if opp else "n/a",
                      tech.has_data if tech else "n/a")

        if not available:
            self._scan_log = "⏭ Todos los símbolos ya tienen posición activa"
            self._notify()
            return

        goal_per_trade = self.goal_usd / max(1, self.multi_trades)
        result = self._strategy.scan_all(
            symbols      = available,
            states       = states,
            opps         = opps,
            techs        = techs,
            account      = account,
            goal_usd     = goal_per_trade,
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
                self._scan_log = (
                    f"✓ Setup encontrado: {sym.replace('USDT','')}  "
                    f"score={proposal.opp_score}  R:R {proposal.rr_ratio:.1f}"
                )
                log.info("Nueva propuesta: %s", proposal.summary())
                if self.mode == AutoMode.SUGGEST:
                    notifier.proposal_ready(sym, proposal.side, proposal.opp_score, self.goal_usd)
                self._notify()
            else:
                self._scan_log = f"✗ {sym.replace('USDT','')}: rechazado — {reason}"
                log.info("Propuesta descartada (pre-flight): %s — %s", proposal.symbol, reason)
                self._notify()
        else:
            # Mostrar los top scores para que el usuario sepa qué tan cerca está
            if scores:
                top = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:4]
                score_str = "  ".join(
                    f"{s.replace('USDT','')}: {sc}" for s, sc in top
                )
                best = top[0][1]
                self._scan_log = f"✗ Sin setup (min {settings.min_scan_score})  —  {score_str}"
            else:
                self._scan_log = "✗ Sin datos de mercado aún"
            log.info("Scan: sin setup válido — scores: %s", scores)
            self._notify()

    # ── Pre-flight ────────────────────────────────────────────────────────────

    def _pre_flight(
        self,
        req:     OrderRequest,
        account: "AccountState",
        risk:    "RiskStatus",
    ) -> tuple[bool, str]:
        if risk.is_breaker:
            return False, "circuit breaker activo"

        # Filtro horario UTC
        if settings.trading_hours_enabled:
            import datetime
            h = datetime.datetime.utcnow().hour
            s, e = settings.trading_hours_start, settings.trading_hours_end
            in_hours = (s <= h < e) if s < e else (h >= s or h < e)  # soporta wrap 22-06
            if not in_hours:
                return False, f"fuera de horario ({h:02d}:00 UTC — trading {s:02d}h-{e:02d}h)"

        # Filtro de blacklist
        if req.symbol in settings.blacklist_set:
            return False, f"{req.symbol} está en la blacklist"

        # Cooldown por símbolo+dirección tras weak_exit / time_stop
        if req.symbol in self._exit_cooldown:
            prev_side, expire_at = self._exit_cooldown[req.symbol]
            now_m = time.monotonic()
            if now_m < expire_at:
                if req.side == prev_side:
                    remaining = int(expire_at - now_m)
                    return False, f"{req.symbol} en cooldown {prev_side} ({remaining}s)"
            else:
                del self._exit_cooldown[req.symbol]

        if req.symbol in self._active:
            return False, f"ya hay posición activa en {req.symbol}"
        if req.symbol in account.positions:
            return False, f"ya hay posición abierta en {req.symbol}"
        # Sin límite de posiciones simultáneas
        avail = account.balance.available_balance
        if avail > 0 and req.margin > avail * 0.95:
            return False, f"margen requerido ${req.margin:.2f} > disponible ${avail:.2f}"
        if req.qty <= 0:
            return False, "qty = 0"
        if req.rr_ratio < settings.min_rr:
            return False, f"R:R {req.rr_ratio:.1f} demasiado bajo (mín {settings.min_rr:.1f})"
        # Modo conservador: si el mercado está choppy, requerir score más alto
        if self.is_choppy_market:
            min_score_choppy = max(settings.min_score + 10, 80)
            if req.opp_score < min_score_choppy:
                return False, (
                    f"mercado choppy — score {req.opp_score} < {min_score_choppy} "
                    f"(6 de los últimos 8 trades perdedores)"
                )
        # Límite de exposición direccional: evitar sobre-concentración
        same_dir = sum(
            1 for t in self._active.values()
            if t.request and t.request.side == req.side and t.is_active
        )
        if same_dir >= MAX_SAME_DIRECTION:
            side_name = "longs" if req.side == "Buy" else "shorts"
            return False, f"ya tienes {same_dir} {side_name} simultáneos (máx {MAX_SAME_DIRECTION})"
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
            from core.order_model import OrderResult as _OR
            result = _OR(success=False, error_msg="excepción inesperada")
            try:
                await self._executor.set_leverage(req.symbol, req.leverage)
                result = await self._executor.place_market_bracket(req)
                if result.success and (req.sl_price > 0 or req.tp_price > 0):
                    import asyncio as _aio
                    await _aio.sleep(0.5)
                    try:
                        await self._executor.set_sl_tp(
                            req.symbol, sl=req.sl_price, tp=req.tp_price, side=req.side
                        )
                        log.info("SL/TP confirmados: %s  SL=%s  TP=%s",
                                 req.symbol, req.sl_price, req.tp_price)
                    except Exception as sl_exc:
                        log.error("set_sl_tp falló: %s — %s (orden colocada, sin SL/TP)",
                                  req.symbol, sl_exc)
                        # La orden sigue siendo exitosa; el trader puede ver la posición
                        # en Bybit sin SL/TP y gestionarla manualmente.
            except Exception as exc:
                log.error("_do error: %s — %s", req.symbol, exc)
                result = _OR(success=False, error_msg=str(exc))
            finally:
                GLib.idle_add(self._on_order_result, result, req)

        self._bridge.submit(_do())
        log.info("Ejecutando: %s", req.summary())
        self._notify()

    def _on_order_result(self, result: OrderResult, req: OrderRequest) -> bool:
        self._pending_exec.discard(req.symbol)
        trade = self._active.get(req.symbol)

        if result.success:
            if trade:
                trade.state            = TradeState.OPEN
                trade.result           = result
                trade.opened_at        = int(time.time())
                trade.signal_timeframe = settings.speed_cfg["tf_label"]
                self._open_ts[req.symbol] = time.monotonic()  # gracia para WS latency
                trade.entry_price = (result.filled_price
                                     if result.filled_price > 0
                                     else req.entry_price)
                trade.pnl_at_open = 0.0   # se establece en primer tick con account data
                self._trail_high[req.symbol] = req.entry_price
                self._trail_low[req.symbol]  = req.entry_price
                self._last_sl_upd[req.symbol] = 0.0
            log.info("Orden confirmada: %s  id=%s", req.symbol, result.order_id)
            notifier.trade_opened(
                req.symbol, req.side, req.entry_price,
                req.sl_price, req.tp_price, self.goal_usd,
            )
            # Si quedan slots de multi-trades disponibles, escanear inmediatamente
            if self.multi_trades > 1 and len(self._active) < self.multi_trades:
                self._last_scan = 0.0
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

    def _reconcile_positions(self, account: "AccountState") -> None:
        """
        Importa posiciones abiertas en Bybit que no están rastreadas localmente.
        Se llama en cada tick — solo actúa cuando encuentra algo nuevo.
        """
        if settings.paper_trading:
            return   # Paper wallet ya gestiona sus propias posiciones
        if not account.connected:
            return
        added = False
        for sym, pos in account.positions.items():
            if pos.size <= 0:
                continue
            if sym in self._active or sym in self._pending_exec:
                continue
            # Crear OrderRequest sintético desde la posición real de Bybit
            sl_dist  = abs(pos.entry_price - pos.stop_loss)  if pos.stop_loss  > 0 else 0.0
            tp_dist  = abs(pos.take_profit - pos.entry_price) if pos.take_profit > 0 else 0.0
            risk_usd = pos.size * sl_dist
            goal_usd = pos.size * tp_dist
            rr       = tp_dist / sl_dist if sl_dist > 0 else 1.5
            lev      = max(1, int(pos.leverage)) if pos.leverage else 1
            req = OrderRequest(
                symbol      = sym,
                side        = pos.side,
                qty         = pos.size,
                entry_price = pos.entry_price,
                sl_price    = pos.stop_loss,
                tp_price    = pos.take_profit,
                risk_usd    = risk_usd,
                goal_usd    = goal_usd,
                rr_ratio    = max(1.0, rr),
                leverage    = lev,
            )
            # Usar createdTime de Bybit (ms → s) como estimación inicial.
            # _resolve_open_time refinará con el historial de ejecuciones.
            created_s = pos.created_time // 1000 if pos.created_time > 1_600_000_000_000 else 0
            trade = TradeRecord(
                symbol      = sym,
                request     = req,
                state       = TradeState.OPEN,
                entry_price = pos.entry_price,
                current_sl  = pos.stop_loss,
                current_tp  = pos.take_profit,
                opened_at   = created_s if created_s > 0 else int(time.time()),
                auto_mode   = AutoMode.FULL_AUTO,  # gestión automática por defecto
            )
            self._active[sym] = trade
            self._trail_high[sym] = pos.entry_price
            self._trail_low[sym]  = pos.entry_price
            self._last_sl_upd[sym] = 0.0
            # Si había una propuesta para este símbolo, cancelarla — ya hay posición
            if self._proposal and self._proposal.symbol == sym:
                self._proposal = None
                log.info("Propuesta de %s cancelada — posición importada de Bybit", sym)
            # Intentar recuperar el tiempo real de apertura desde el historial de Bybit
            self._bridge.submit(self._resolve_open_time(sym, hint_ms=pos.created_time))
            log.info(
                "Posición importada de Bybit: %s %s %.4f @ %.5g  SL=%s  TP=%s",
                sym, pos.side, pos.size, pos.entry_price,
                pos.stop_loss, pos.take_profit,
            )
            added = True
        if added:
            self._notify()

    def _sync_active_trades(self, account: "AccountState") -> None:
        """Detecta cierres por SL/TP/manual/duración para todos los trades activos."""
        self._reconcile_positions(account)
        closed: List[str] = []
        for sym, trade in self._active.items():
            if not trade.is_active:
                continue

            # Cierre por duración máxima.
            # SOLO aplica a trades abiertos DESPUÉS de que se configuró el límite.
            # Nunca cierra trades que ya estaban activos cuando se cambió la configuración.
            if (
                self.max_duration_min > 0
                and trade.opened_at > 0
                and trade.opened_at >= self._duration_set_ts
                and (int(time.time()) - trade.opened_at) >= self.max_duration_min * 60
            ):
                log.info("Duración máxima alcanzada: %s (%dm) — cerrando", sym, self.max_duration_min)
                notifier.order_failed(sym, f"Tiempo máximo {self.max_duration_min}m")
                self.close_symbol(sym, "max_duration")
                continue

            pos = account.positions.get(sym)
            if pos is None or pos.size <= 0:
                # Período de gracia: los primeros 5 segundos después de confirmar OPEN
                # el WebSocket puede no haber enviado la posición todavía.
                # Sin esta gracia, el trade se marcaría CLOSED antes de que llegue el WS update.
                open_since = time.monotonic() - self._open_ts.get(sym, 0)
                if open_since < 5.0:
                    continue
                # Solo contar ausencias DESPUÉS de haber visto la posición al menos una vez.
                # Esto cubre el caso de latencia prolongada del WS: el trade apareció en Bybit
                # pero el snapshot de posiciones aún no lo refleja localmente.
                if sym not in self._position_seen:
                    continue
                # Requerir 15 ticks consecutivos (~1.5s) sin posición antes de marcar cerrado.
                self._close_confirm[sym] = self._close_confirm.get(sym, 0) + 1
                if self._close_confirm[sym] < 15:
                    continue
                trade.state     = TradeState.CLOSED
                trade.closed_at = int(time.time())
                trade.pnl_usd, trade.close_reason = self._compute_close_pnl(sym, trade)
                log.info("Trade cerrado: %s  PnL=$%.4f  (%s)", sym, trade.pnl_usd, trade.close_reason)
                self._log.append(trade)
                save_trade(trade)
                notifier.trade_closed(sym, trade.pnl_usd, trade.close_reason)
                self._track_symbol_perf(sym, trade.pnl_usd)
                closed.append(sym)
            else:
                self._position_seen.add(sym)          # confirmado vía WS al menos una vez
                self._last_upnl[sym] = pos.unrealized_pnl
                self._close_confirm.pop(sym, None)

                # ── Sincronizar qty/SL/TP desde la posición real de Bybit ────────
                # Maneja fill parcial (qty menor a lo pedido) y cambios manuales de
                # SL/TP hechos directamente en la app de Bybit.
                req = trade.request
                if req:
                    if pos.size > 0 and abs(pos.size - req.qty) / max(req.qty, 0.0001) > 0.02:
                        log.info("Qty sync: %s %.4f → %.4f (fill parcial o cierre parcial)",
                                 sym, req.qty, pos.size)
                        req.qty = pos.size
                    if pos.stop_loss > 0 and abs(pos.stop_loss - trade.current_sl) > 1e-7:
                        trade.current_sl = pos.stop_loss
                    if pos.take_profit > 0 and abs(pos.take_profit - trade.current_tp) > 1e-7:
                        trade.current_tp = pos.take_profit

        for sym in closed:
            self._active.pop(sym, None)
            self._trail_high.pop(sym, None)
            self._trail_low.pop(sym, None)
            self._last_sl_upd.pop(sym, None)
            self._close_confirm.pop(sym, None)
            self._pnl_captured.discard(sym)
            self._last_upnl.pop(sym, None)
            self._be_since.pop(sym, None)
            self._open_ts.pop(sym, None)
            self._position_seen.discard(sym)
            self._tp_removed.discard(sym)
            self._partial_lock_done.discard(sym)
            self._pct80_analyzed.discard(sym)
        if closed:
            self._notify()

    # ── Gestión activa (FULL_AUTO) ────────────────────────────────────────────

    def _manage_active_trades(
        self,
        account: "AccountState",
        states:  Dict[str, "MarketState"],
    ) -> None:
        for sym, trade in list(self._active.items()):
            # Solo gestiona trades con modo FULL_AUTO (global o por trade)
            effective = trade.auto_mode if trade.auto_mode != AutoMode.MANUAL else self.mode
            if effective == AutoMode.FULL_AUTO:
                self._manage_one(sym, trade, account, states)

    def _manage_one(
        self,
        sym:     str,
        trade:   TradeRecord,
        account: "AccountState",
        states:  Dict[str, "MarketState"] = None,
    ) -> None:
        req = trade.request
        if not req:
            return

        pos  = account.positions.get(sym)
        ms   = states.get(sym) if states else None
        mark = 0.0
        if ms and ms.ticker.last_price > 0:
            mark = ms.ticker.last_price
        elif pos and pos.mark_price > 0:
            mark = pos.mark_price
        elif pos and pos.entry_price > 0:
            mark = pos.entry_price
        else:
            return   # sin precio de mercado, no podemos gestionar

        # Permite gestión aunque la posición no esté en el account stream todavía
        # (latencia WS o primera aparición). Solo bloqueamos si no hay precio de mercado.
        if not pos or pos.size <= 0:
            if not ms or ms.ticker.last_price <= 0:
                return
            # Continúa con datos del trade y market state

        entry = trade.entry_price or (pos.entry_price if pos else 0.0)
        sl      = trade.current_sl
        tp      = trade.current_tp
        is_long = req.side == "Buy"

        if entry <= 0 or tp <= 0 or sl <= 0:
            return

        tp_dist = abs(tp - entry)
        if tp_dist <= 0:
            return

        # Usar siempre el SL original para medir distancias de riesgo.
        # Después del breakeven, trade.current_sl ≈ entry → sl_dist ≈ 0,
        # lo que rompe los cálculos de profit-lock y trailing.
        orig_sl = req.sl_price if req.sl_price > 0 else sl
        sl_dist = abs(entry - orig_sl)
        if sl_dist <= 0:
            return

        progress = (mark - entry) / tp_dist if is_long else (entry - mark) / tp_dist
        now      = time.monotonic()

        # ── Rastrear cuándo el precio alcanzó el umbral de breakeven ──────────
        if progress >= settings.breakeven_pct / 100:
            if sym not in self._be_since:
                self._be_since[sym] = now
                log.debug("BE threshold alcanzado: %s (%.0f%%)", sym, progress * 100)
        elif progress < settings.breakeven_pct / 100 * 0.80:
            # Hysteresis: resetear si el precio cae más del 20% bajo el umbral
            if sym in self._be_since:
                log.debug("BE threshold perdido: %s (%.0f%%)", sym, progress * 100)
            self._be_since.pop(sym, None)

        be_held = (now - self._be_since.get(sym, now)) >= settings.effective_be_hold_s
        elapsed = int(time.time()) - trade.opened_at if trade.opened_at > 0 else 9999

        # ── Weak Exit: salida si la tesis se debilita antes de producir ganancia ─
        # Solo activo en los primeros N segundos del trade y mientras no hay beneficio.
        # Requiere tiempo mínimo (evita t=0) y que el precio haya avanzado al menos
        # X% hacia el SL (confirmando que el setup realmente falló, no es ruido).
        if (
            settings.weak_exit_enabled
            and trade.state == TradeState.OPEN
            and elapsed >= settings.weak_exit_min_elapsed_s  # tiempo mínimo
            and elapsed < settings.weak_exit_window_s
            and progress <= 0.0
        ):
            # Verificar que el precio ya se movió hacia el SL (no sólo ruido de spread)
            sl_dist = abs(entry - req.sl_price) if req and req.sl_price > 0 else 0
            if sl_dist > 0:
                sl_move = (entry - mark) if req.side == "Buy" else (mark - entry)
                sl_pct = (sl_move / sl_dist) * 100
            else:
                sl_pct = 0.0
            if sl_pct >= settings.weak_exit_min_sl_pct:
                weak_now = self._weakness_score(sym, trade, mark, ms)
                if weak_now >= settings.weak_exit_min_score:
                    log.info(
                        "WeakExit: %s  weakness=%d  elapsed=%ds  sl_pct=%.0f%% — cerrando",
                        sym, weak_now, elapsed, sl_pct,
                    )
                    trade.signal_health = weak_now
                    self.close_symbol(sym, "weak_exit")
                    return

        # ── Time Stop: cerrar si no hay progreso suficiente en N segundos ────────
        # Corta trades "muertos" que consumen tiempo y capital sin moverse.
        if (
            settings.time_stop_enabled
            and trade.state == TradeState.OPEN
            and elapsed >= settings.time_stop_window_s
            and progress < settings.time_stop_min_pct / 100
        ):
            log.info(
                "TimeStop: %s  progress=%.0f%%  elapsed=%ds — sin movimiento, cerrando",
                sym, progress * 100, elapsed,
            )
            self.close_symbol(sym, "time_stop")
            return

        # ── Smart Profit Guard (30%+ con señales de debilitamiento) ─────────────
        # IMPORTANTE: bloque independiente (no `elif`) para que el chain principal
        # de BE/locks/trailing siempre pueda ejecutarse independientemente.
        # Cuando SmartGuard actúa, establece _last_sl_upd, y el chain principal
        # tiene un cooldown de 2s para evitar doble-modificación en el mismo tick.
        if (
            progress >= SMART_GUARD_MIN_PROGRESS
            and trade.state not in (TradeState.TRAILING,)
            and (now - self._last_sl_upd.get(sym, 0)) >= SMART_GUARD_COOLDOWN
        ):
            weak = self._weakness_score(sym, trade, mark, ms)
            if weak >= SMART_GUARD_WEAKNESS_REQ:
                locked_frac = progress * SMART_GUARD_LOCK_FRAC
                atr = sl_dist / 1.5
                if is_long:
                    desired_sl  = entry + locked_frac * tp_dist
                    min_room_sl = mark  - atr * SMART_GUARD_ATR_ROOM
                    new_sl = min(desired_sl, min_room_sl)
                else:
                    desired_sl  = entry - locked_frac * tp_dist
                    min_room_sl = mark  + atr * SMART_GUARD_ATR_ROOM
                    new_sl = max(desired_sl, min_room_sl)

                improves = ((is_long and new_sl > trade.current_sl) or
                            (not is_long and new_sl < trade.current_sl))
                if improves:
                    room_atr = abs(mark - new_sl) / atr if atr > 0 else 0
                    log.info(
                        "SmartGuard: %s prog=%.0f%% weak=%d  SL %.5g→%.5g "
                        "(lock=%.0f%% ganancias, room=%.1f×ATR)",
                        sym, progress * 100, weak,
                        trade.current_sl, new_sl,
                        locked_frac * 100, room_atr,
                    )
                    trade.current_sl = new_sl
                    if (is_long and new_sl > entry) or (not is_long and new_sl < entry):
                        trade.state = TradeState.BREAKEVEN
                    self._bridge.submit(
                        self._modify_sl_safe(sym, new_sl, req.side, "smart-guard")
                    )
                    self._last_sl_upd[sym] = now
                    self._notify()

        # ── Análisis inteligente al 80% — NO es impulso, analiza señales ────────
        # Bloque INDEPENDIENTE: se ejecuta una sola vez por trade cuando el precio
        # alcanza el 80% del recorrido hacia el TP.
        # · Señales fuertes (cont ≥ 3): eliminar TP y activar trailing ajustado.
        # · Señales débiles  (cont ≤ 1): cerrar ahora para asegurar ganancias.
        # · Señales medias   (cont  = 2): no hacer nada, dejar que el trailing siga.
        if (
            progress >= 0.80
            and trade.state == TradeState.TRAILING
            and trade.current_tp > 0
            and sym not in self._pct80_analyzed
            and sym not in self._tp_removed
        ):
            self._pct80_analyzed.add(sym)
            cont80 = self._continuation_score(sym, trade, mark, ms)
            if cont80 >= 3:
                log.info(
                    "Smart80: %s prog=%.0f%% cont=%d — señales fuertes, extendiendo TP",
                    sym, progress * 100, cont80,
                )
                trade.current_tp = 0
                self._tp_removed.add(sym)
                atr80 = sl_dist / 1.5
                if is_long:
                    self._trail_high[sym] = mark
                    new_sl80 = mark - atr80 * 0.8   # trail más ajustado que estándar
                else:
                    self._trail_low[sym] = mark
                    new_sl80 = mark + atr80 * 0.8
                if ((is_long  and new_sl80 > trade.current_sl) or
                    (not is_long and new_sl80 < trade.current_sl)):
                    trade.current_sl = new_sl80
                    self._bridge.submit(self._clear_tp_and_trail(sym, new_sl80, req.side))
                    self._last_sl_upd[sym] = now
                else:
                    self._bridge.submit(self._clear_tp(sym, req.side))
                notifier.trailing_activated(sym, trade.current_sl)
                self._notify()
            elif cont80 <= 1:
                log.info(
                    "Smart80: %s prog=%.0f%% cont=%d — señales débiles, asegurando ganancias",
                    sym, progress * 100, cont80,
                )
                self.close_symbol(sym, "smart_close_80")
                return
            else:
                log.debug(
                    "Smart80: %s prog=%.0f%% cont=%d — señales medias, trail continúa",
                    sym, progress * 100, cont80,
                )

        # ── Chain principal: BE → Partial Lock → Profit Lock → Trail ────────────
        # Cooldown de 2s para no solaparse con SmartGuard en el mismo tick.
        _can_modify = (now - self._last_sl_upd.get(sym, 0)) >= 2.0

        # ── Breakeven (40% por ≥ be_hold_s) ─────────────────────────────────────
        if (
            _can_modify
            and trade.state == TradeState.OPEN
            and progress >= settings.breakeven_pct / 100
            and be_held
            and sl != entry
        ):
            buffer = entry * _BE_FEE_PCT
            new_sl = (entry + buffer) if is_long else (entry - buffer)
            log.info("Breakeven: %s SL → %.5g (mantenido %.0fs)", sym, new_sl, now - self._be_since[sym])
            trade.state      = TradeState.BREAKEVEN
            trade.current_sl = new_sl
            self._bridge.submit(self._modify_sl_safe(sym, new_sl, req.side, "breakeven"))
            self._last_sl_upd[sym] = now
            notifier.breakeven_activated(sym, new_sl)
            self._notify()

        # ── Partial Lock (50%) — escalón real de ganancia antes del profit lock ──
        # Mueve el SL a entry + frac×sl_dist para garantizar un PnL positivo real.
        # Se activa desde estado BREAKEVEN y solo una vez por trade.
        elif (
            _can_modify
            and settings.partial_lock_enabled
            and trade.state == TradeState.BREAKEVEN
            and progress >= settings.partial_lock_at_pct / 100
            and sym not in self._partial_lock_done
            and (now - self._last_sl_upd.get(sym, 0)) >= 5.0
        ):
            lock_sl = (
                (entry + sl_dist * settings.partial_lock_frac) if is_long
                else (entry - sl_dist * settings.partial_lock_frac)
            )
            if (is_long and lock_sl > trade.current_sl) or (not is_long and lock_sl < trade.current_sl):
                log.info(
                    "PartialLock: %s SL → %.5g (+%.0f%% del riesgo asegurado)",
                    sym, lock_sl, settings.partial_lock_frac * 100,
                )
                trade.current_sl = lock_sl
                self._partial_lock_done.add(sym)
                self._bridge.submit(self._modify_sl_safe(sym, lock_sl, req.side, "partial-lock"))
                self._last_sl_upd[sym] = now
                self._notify()

        # ── Profit lock (60%) — asegura ganancia mayor ───────────────────────────
        elif (
            _can_modify
            and trade.state == TradeState.BREAKEVEN
            and progress >= settings.profit_lock_pct / 100
            and (now - self._last_sl_upd.get(sym, 0)) >= 5.0
        ):
            lock_sl = (entry + sl_dist * PROFIT_LOCK_RATIO) if is_long else (entry - sl_dist * PROFIT_LOCK_RATIO)
            if (is_long and lock_sl > trade.current_sl) or (not is_long and lock_sl < trade.current_sl):
                log.info("Profit lock: %s SL → %.5g (+%.0f%% del riesgo asegurado)", sym, lock_sl, PROFIT_LOCK_RATIO * 100)
                trade.current_sl = lock_sl
                self._bridge.submit(self._modify_sl_safe(sym, lock_sl, req.side, "profit-lock"))
                self._last_sl_upd[sym] = now
                self._notify()

        # ── Dynamic TP → Trail extendido (85%) ───────────────────────────────
        # Cuando el precio está cerca del TP y hay señales claras de continuación,
        # eliminamos el TP para capturar el movimiento extendido y activamos
        # trailing ajustado. Criterio: 3+ señales de momentum activas.
        elif (
            trade.state in (TradeState.BREAKEVEN, TradeState.TRAILING)
            and progress >= 0.85
            and trade.current_tp > 0          # TP activo (no ya eliminado)
            and sym not in self._tp_removed
            and (now - self._last_sl_upd.get(sym, 0)) >= 3.0
        ):
            cont = self._continuation_score(sym, trade, mark, ms)
            if cont >= 3:
                log.info(
                    "Dynamic TP: %s progress=%.0f%% cont=%d — quitando TP, trail extendido",
                    sym, progress * 100, cont,
                )
                trade.current_tp = 0
                self._tp_removed.add(sym)
                # Trail ajustado: ATR×1.0 (más apretado que el estándar 1.5×)
                # para asegurar ganancias mientras captura el movimiento extendido.
                atr = sl_dist / 1.5
                if is_long:
                    self._trail_high[sym] = mark
                    new_sl = mark - atr * 1.0
                else:
                    self._trail_low[sym] = mark
                    new_sl = mark + atr * 1.0
                if ((is_long  and new_sl > trade.current_sl) or
                    (not is_long and new_sl < trade.current_sl)):
                    trade.state      = TradeState.TRAILING
                    trade.current_sl = new_sl
                    self._bridge.submit(
                        self._clear_tp_and_trail(sym, new_sl, req.side)
                    )
                else:
                    self._bridge.submit(self._clear_tp(sym, req.side))
                self._last_sl_upd[sym] = now
                notifier.trailing_activated(sym, new_sl if new_sl > 0 else trade.current_sl)
                self._notify()

        # ── Trailing (70%) ────────────────────────────────────────────────────
        elif (
            trade.state in (TradeState.BREAKEVEN, TradeState.TRAILING)
            and progress >= settings.trailing_pct / 100
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
                    self._bridge.submit(self._modify_sl_safe(sym, new_sl, req.side, "trailing"))
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
                    self._bridge.submit(self._modify_sl_safe(sym, new_sl, req.side, "trailing"))
                    self._last_sl_upd[sym] = now
                    if first_trail:
                        notifier.trailing_activated(sym, new_sl)
                    self._notify()

        # ── Actualizar salud del setup ─────────────────────────────────────────
        trade.signal_health = self._weakness_score(sym, trade, mark, ms)

    def _continuation_score(
        self,
        sym:   str,
        trade: "TradeRecord",
        mark:  float,
        ms:    Optional["MarketState"],
    ) -> int:
        """
        Evalúa señales de continuación del movimiento actual (0-5 puntos).
        Usado para decidir si eliminar el TP y activar trailing extendido.

        Señales:
          · CVD: últimas 5 velas en la dirección del trade  → +0/+1/+2
          · OI velocity: OI creciendo (nuevas posiciones)   → +1
          · Price momentum: últimas 5 muestras de precio    → +0/+1/+2
        """
        if not ms or not trade.request:
            return 0

        is_long = trade.request.side == "Buy"
        score   = 0

        # 1. CVD: dirección de las últimas 5 velas
        candles = list(ms.cvd_candles)[-5:]
        if len(candles) >= 3:
            bull = sum(1 for c in candles if c.delta > 0)
            bear = len(candles) - bull
            if is_long:
                score += 2 if bull >= 4 else (1 if bull == 3 else 0)
            else:
                score += 2 if bear >= 4 else (1 if bear == 3 else 0)

        # 2. OI velocity: OI creciendo → nuevas posiciones abriendo
        if ms.oi_velocity > 0.5:  # > $0.5/min de crecimiento en OI
            score += 1

        # 3. Price momentum: últimas 5 muestras de precio
        ph = list(ms._price_history)[-5:]
        if len(ph) >= 3:
            prices   = [p for _, p in ph]
            up_moves = sum(1 for i in range(1, len(prices)) if prices[i] > prices[i - 1])
            dn_moves = len(prices) - 1 - up_moves
            if is_long:
                score += 2 if up_moves >= 4 else (1 if up_moves == 3 else 0)
            else:
                score += 2 if dn_moves >= 4 else (1 if dn_moves == 3 else 0)

        return score

    def _weakness_score(
        self,
        sym:   str,
        trade: "TradeRecord",
        mark:  float,
        ms:    Optional["MarketState"],
    ) -> int:
        """
        Evalúa señales de debilitamiento del movimiento actual (0-6 puntos).
        Cuanto más alto, mayor la probabilidad de pullback o reversión.

        Señales:
          · CVD: velas contra la dirección del trade (últimas 5) → +0/+1/+2
          · OI velocity: posiciones cerrándose (negativo)         → +1
          · Price momentum: precio frenando/revirtiendo           → +0/+1/+2
          · Orderbook imbalance: presión contraria al trade       → +0/+1
        """
        if not ms or not trade.request:
            return 0

        is_long = trade.request.side == "Buy"
        score   = 0

        # 1. CVD: ¿mayoría de velas van CONTRA el trade?
        candles = list(ms.cvd_candles)[-5:]
        if len(candles) >= 3:
            bear = sum(1 for c in candles if c.delta < 0)
            bull = len(candles) - bear
            if is_long:
                score += 2 if bear >= 4 else (1 if bear == 3 else 0)
            else:
                score += 2 if bull >= 4 else (1 if bull == 3 else 0)

        # 2. OI velocity: posiciones cerrándose = pérdida de convicción
        if ms.oi_velocity < -0.3:
            score += 1

        # 3. Price momentum: últimas 5 muestras revirtiendo
        ph = list(ms._price_history)[-5:]
        if len(ph) >= 3:
            prices   = [p for _, p in ph]
            up_moves = sum(1 for i in range(1, len(prices)) if prices[i] > prices[i - 1])
            dn_moves = len(prices) - 1 - up_moves
            if is_long:
                score += 2 if dn_moves >= 4 else (1 if dn_moves == 3 else 0)
            else:
                score += 2 if up_moves >= 4 else (1 if up_moves == 3 else 0)

        # 4. Orderbook imbalance: ¿más asks que bids (long) o bids que asks (short)?
        try:
            imbal = ms.orderbook.imbalance
            if is_long and imbal < -0.20:
                score += 1
            elif not is_long and imbal > 0.20:
                score += 1
        except AttributeError:
            pass

        return score

    async def _clear_tp(self, sym: str, side: str) -> None:
        """Elimina el TP de una posición para capturar movimiento extendido."""
        try:
            await self._executor.set_sl_tp(sym, side=side, clear_tp=True)
            log.info("TP eliminado: %s — trailing extendido activado", sym)
        except Exception as exc:
            log.error("_clear_tp error: %s — %s", sym, exc)

    async def _clear_tp_and_trail(self, sym: str, new_sl: float, side: str) -> None:
        """Elimina el TP y mueve el SL en una sola llamada a Bybit."""
        try:
            await self._executor.set_sl_tp(sym, sl=new_sl, side=side, clear_tp=True)
            log.info("TP→Trail: %s  SL=%.5g  TP eliminado", sym, new_sl)
        except Exception as exc:
            log.error("_clear_tp_and_trail error: %s — %s", sym, exc)

    async def _resolve_open_time(self, sym: str, hint_ms: int = 0) -> None:
        """
        Recupera el timestamp real de apertura desde el historial de Bybit y lo aplica
        al trade activo. Se ejecuta una vez tras reconciliar una posición importada.
        """
        open_time = await self._executor.get_position_open_time(sym, since_ms=hint_ms)
        if open_time > 0:
            def _apply():
                trade = self._active.get(sym)
                if trade:
                    trade.opened_at = open_time
                    log.info("Open time recuperado: %s → %s",
                             sym, time.strftime("%Y-%m-%d %H:%M", time.localtime(open_time)))
                    self._notify()
            GLib.idle_add(_apply)

    async def _modify_sl_safe(self, sym: str, new_sl: float, side: str, reason: str) -> None:
        """Envía modificación de SL con retry automático en caso de fallo."""
        import asyncio as _aio
        for attempt in range(2):
            try:
                await self._executor.set_sl_tp(sym, sl=new_sl, side=side)
                log.info("SL %s OK: %s → %.5g", reason, sym, new_sl)
                return
            except Exception as exc:
                log.error("SL %s fallo #%d: %s → %.5g: %s", reason, attempt+1, sym, new_sl, exc)
                if attempt == 0:
                    await _aio.sleep(1.5)
        log.error("SL %s no se pudo mover: %s", reason, sym)

    def _compute_close_pnl(self, sym: str, trade: "TradeRecord") -> tuple[float, str]:
        """
        Calcula el PnL neto real cuando una posición desaparece de Bybit.

        Método: detecta si fue SL o TP comparando el último uPnL con los
        gross PnL esperados, luego resta las fees de entrada y salida.

        Retorna (pnl_usd, close_reason).
        """
        req = trade.request
        if not req or req.qty <= 0 or trade.entry_price <= 0:
            return 0.0, "SL/TP"

        entry   = trade.entry_price
        qty     = req.qty
        is_long = req.side == "Buy"
        sl      = trade.current_sl
        tp      = trade.current_tp

        # Gross PnL esperado si SL o TP fue tocado
        sl_gross = ((sl  - entry) if is_long else (entry - sl))  * qty
        tp_gross = ((tp  - entry) if is_long else (entry - tp))  * qty if tp > 0 else None

        # Último unrealized PnL observado (precio justo antes de desaparecer)
        last_upnl = self._last_upnl.get(sym, 0.0)

        # Decidir qué nivel fue tocado: el que más se aproxime a last_upnl
        if tp_gross is not None:
            midpoint = (sl_gross + tp_gross) / 2
            if last_upnl >= midpoint:
                close_price = tp
                gross_pnl   = tp_gross
                reason      = "TP"
            else:
                close_price = sl
                gross_pnl   = sl_gross
                reason      = "SL"
        else:
            close_price = sl
            gross_pnl   = sl_gross
            reason      = "SL"

        # Fees taker: 0.055% por lado = 0.11% RT
        exit_fee  = abs(close_price * qty) * 0.00055
        entry_fee = abs(entry       * qty) * 0.00055
        net_pnl   = gross_pnl - exit_fee - entry_fee

        log.debug(
            "_compute_close_pnl %s: gross=%.4f fees=%.4f net=%.4f (%s)",
            sym, gross_pnl, exit_fee + entry_fee, net_pnl, reason,
        )
        return round(net_pnl, 4), reason

    async def _do_close(self, symbol: str, req: OrderRequest, reason: str = "manual") -> None:
        result = await self._executor.close_position(symbol, req.qty, req.side)
        GLib.idle_add(self._on_close_result, symbol, result, reason)

    def _on_close_result(self, symbol: str, result: OrderResult, reason: str = "manual") -> bool:
        trade = self._active.get(symbol)
        if trade:
            trade.state        = TradeState.CLOSED
            trade.closed_at    = int(time.time())
            trade.close_reason = reason if result.success else result.error_msg
            # PnL manual: último unrealized PnL conocido menos fee de salida
            req  = trade.request
            last = self._last_upnl.get(symbol, 0.0)
            if req and req.qty > 0:
                close_px  = trade.entry_price or req.entry_price
                exit_fee  = abs(close_px * req.qty) * 0.00055
                entry_fee = abs((trade.entry_price or req.entry_price) * req.qty) * 0.00055
                trade.pnl_usd = last - exit_fee - entry_fee
            else:
                trade.pnl_usd = last
            self._log.append(trade)
            save_trade(trade)
            notifier.trade_closed(symbol, trade.pnl_usd, trade.close_reason)
            self._track_symbol_perf(symbol, trade.pnl_usd)
            self._pnl_captured.discard(symbol)
            self._last_upnl.pop(symbol, None)
            self._be_since.pop(symbol, None)
            self._open_ts.pop(symbol, None)
            self._position_seen.discard(symbol)
            self._tp_removed.discard(symbol)
            self._partial_lock_done.discard(symbol)
            self._pct80_analyzed.discard(symbol)
            del self._active[symbol]
            self._trail_high.pop(symbol, None)
            self._trail_low.pop(symbol, None)
            self._last_sl_upd.pop(symbol, None)
        self._notify()
        return False

    # ── Auto-blacklist ────────────────────────────────────────────────────────

    def _track_symbol_perf(self, sym: str, pnl_usd: float) -> None:
        """Actualiza conteo de pérdidas consecutivas, auto-blacklist y detector choppy."""
        # ── Historial reciente para detector de mercado choppy ────────────────
        self._recent_results.append("loss" if pnl_usd < 0 else "win")
        if len(self._recent_results) > 10:
            self._recent_results.pop(0)

        if pnl_usd < 0:
            self._consec_losses[sym] = self._consec_losses.get(sym, 0) + 1
            n = self._consec_losses[sym]
            if settings.auto_blacklist_enabled and n >= settings.auto_blacklist_losses:
                bl = settings.blacklist_set
                if sym not in bl:
                    bl.add(sym)
                    settings.symbol_blacklist = ",".join(sorted(bl))
                    log.warning(
                        "AutoBlacklist: %s añadido — %d pérdidas consecutivas",
                        sym, n,
                    )
        else:
            # Ganancia o BE → resetear contador y quitar del auto-blacklist si estaba
            prev = self._consec_losses.pop(sym, 0)
            if prev >= settings.auto_blacklist_losses:
                bl = settings.blacklist_set
                bl.discard(sym)
                settings.symbol_blacklist = ",".join(sorted(bl))
                log.info("AutoBlacklist: %s eliminado (ganancia tras %d pérdidas)", sym, prev)

    @property
    def is_choppy_market(self) -> bool:
        """True si los últimos 8 trades tienen 6+ pérdidas → mercado desfavorable."""
        if len(self._recent_results) < 8:
            return False
        recent8 = self._recent_results[-8:]
        losses = sum(1 for r in recent8 if r == "loss")
        return losses >= 6

    def blacklist_stats(self) -> dict:
        """Devuelve estado del auto-blacklist para mostrar en la UI."""
        return {sym: n for sym, n in self._consec_losses.items() if n > 0}

    def remove_from_blacklist(self, sym: str) -> None:
        """Elimina un símbolo de la blacklist (manual o auto)."""
        bl = settings.blacklist_set
        bl.discard(sym.upper())
        settings.symbol_blacklist = ",".join(sorted(bl))
        self._consec_losses.pop(sym.upper(), None)
        log.info("Blacklist: %s eliminado manualmente", sym)

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
        remaining = int(settings.scan_interval_s - (time.monotonic() - self._last_scan))
        return f"Escaneando en {max(0, remaining)}s…"
