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
from core.session import SessionManager, SessionStatus
from core.audit_agent import audit_agent
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

# ─── Auxiliares de cálculo ────────────────────────────────────────────────────
_BE_FEE_PCT        = 0.0015   # 0.15% para cubrir fees + micro-profit al entrar en BE
_MIN_BE_PROGRESS   = 0.01     # progreso mínimo absoluto para mover a BE (evita t=0)


class TradeController:
    """
    Controlador central de trading. Orquestador de la estrategia y ejecución.
    """

    def __init__(
        self,
        strategy:     StrategyEngine,
        risk_fortress: RiskFortress,
        executor:     "BybitExecutor",
        bridge:       "AsyncBridge",
        symbols:      List[str],
    ) -> None:
        self._strategy      = strategy
        self._risk_fortress = risk_fortress
        self._executor      = executor
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
        # AI Strategy Agent — estado del scan asíncrono
        self._ai_scanning:   bool             = False  # hay una llamada a OpenAI en curso
        # SL más protector jamás enviado por trade — NUNCA retrocede
        self._best_sl: Dict[str, float]       = {}
        # Sistema de Salud de Símbolos (SHPP): sym → score (-10 a +10)
        self._symbol_scores: Dict[str, float] = {}

        # ── Módulo de Sesiones (TSAA) ──
        self._session: Optional[SessionManager] = None
        self._last_balance: float = 0.0

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
        self.leverage = max(1, min(50, leverage))
        self._proposal = None
        self._notify()

    def set_trade_mode(self, symbol: str, mode: AutoMode) -> None:
        """Cambia el modo de gestión para UN trade específico."""
        trade = self._active.get(symbol)
        if trade:
            trade.auto_mode = mode
            log.info("%s mode changed to %s", symbol, mode)
            self._notify()

    def set_max_duration(self, minutes: int) -> None:
        self.max_duration_min = max(0, minutes)
        self._duration_set_ts = time.time()
        self._notify()

    def start_session(self) -> bool:
        """Inicia manualmente una nueva sesión TSAA."""
        if self._session:
            return False
        if self._last_balance <= 0:
            log.warning("[TSAA] No se puede iniciar sesión sin balance conocido.")
            return False
            
        self._session = SessionManager(self._last_balance)
        self.set_mode(AutoMode.FULL_AUTO)
        self._notify()
        return True

    def stop_session(self) -> None:
        """Detiene manualmente la sesión actual y dispara auditoría."""
        if not self._session:
            return
        log.info("[TSAA] Parada manual de sesión solicitada.")
        self._finalize_session_and_audit()
        self.set_mode(AutoMode.MANUAL)
        self._notify()

    def _finalize_session_and_audit(self) -> None:
        """Lógica común para cerrar sesión y lanzar agente de auditoría."""
        if not self._session:
            return
            
        summary = {
            "pnl": self._session.closed_pnl,
            "duration_s": self._session.elapsed_s,
            "target_pnl": self._session.target_pnl,
        }
        session_id = self._session.id
        self._session.close()
        self._session = None
        
        async def _run_audit():
            report, path = await audit_agent.audit_session(session_id, summary)
            log.info("[TSAA] Auditoría Completada para sesión %s. Reporte en: %s", session_id, path)
            if path:
                notifier.session_report_ready(session_id, path)
        
        self._bridge.submit(_run_audit())

    def set_multi_trades(self, n: int) -> None:
        self.multi_trades = max(1, min(10, n))
        self._notify()

    def on_update(self, cb: Callable) -> None:
        self._callbacks.append(cb)

    def force_scan(self) -> None:
        """Fuerza un escaneo inmediato ignorando el intervalo."""
        self._last_scan = 0
        log.info("Scan forzado por el usuario")

    def approve_proposal(self) -> None:
        """Alias para execute_proposal (usado por CommandCenter)."""
        if self._proposal:
            log.info("Propuesta aprobada manualmente: %s", self._proposal.symbol)
            self._execute(self._proposal)

    def reject_proposal(self) -> None:
        """Descarta la propuesta actual."""
        if self._proposal:
            log.info("Propuesta rechazada por el usuario: %s", self._proposal.symbol)
            self._proposal = None
            self._notify()

    def execute_proposal(self) -> None:
        self.approve_proposal()

    def close_now(self, reason: str = "manual_all") -> None:
        """Alias para close_all."""
        self.close_all(reason)

    def close_all(self, reason: str = "manual_all") -> None:
        for sym in list(self._active.keys()):
            self.close_symbol(sym, reason)

    def close_symbol(self, symbol: str, reason: str = "manual") -> None:
        trade = self._active.get(symbol)
        if trade and trade.is_active:
            log.info("Cerrando %s (%s)", symbol, reason)
            self._bridge.submit(self._do_close(symbol, trade.request, reason))

    @property
    def state(self) -> ControllerState:
        return self.get_state()

    @property
    def trade_log(self) -> List[TradeRecord]:
        return self._log

    def live_scores(self, n: int = 8) -> List[Tuple[str, int, str, str]]:
        """Devuelve los top N scores actuales del mercado en formato indexable."""
        # Extraer de latest_opps
        items = []
        for s, opp in self._latest_opps.items():
            if hasattr(opp, 'score'):
                direction = getattr(opp, 'direction', 'Neutral')
                regime = getattr(opp.regime, 'label', 'N/A') if hasattr(opp, 'regime') else 'N/A'
                items.append((s, int(opp.score), direction, regime))
        
        return sorted(items, key=lambda x: x[1], reverse=True)[:n]

    def get_state(self) -> ControllerState:
        age = int(time.monotonic() - self._proposal_ts) if self._proposal else 0
        return ControllerState(
            mode           = self.mode,
            goal_usd       = self.goal_usd,
            proposal       = self._proposal,
            proposal_age_s = age,
            active_trades  = sorted(self._active.values(), key=lambda t: t.symbol),
            last_result    = self._log[-1] if self._log else None,
            scan_log       = self._scan_log,
        )

    # ── Ciclo de vida ─────────────────────────────────────────────────────────

    def tick(
        self,
        states:  Dict[str, "MarketState"],
        account: "AccountState",
        techs:   Dict[str, "TechSignal"],
        opps:    Dict[str, "OpportunitySignal"],
        risk:    "RiskStatus",
    ) -> None:
        # ── 0. Gestión de Sesión (TSAA) ──
        self._last_balance = account.balance.wallet_balance
        upnl = sum(p.unrealized_pnl for p in account.positions.values())

        # Auto-inicio eliminado - ahora es manual vía UI
        
        status = SessionStatus.ACTIVE
        if self._session:
            status = self._session.update(self._last_balance, upnl)

        # Si la sesión está liquidando o terminó tiempo, cerramos todo y notificamos
        if status in [SessionStatus.LIQUIDATING, SessionStatus.CLOSED]:
            if self._active:
                log.warning("[TSAA] CIERRE DE SESIÓN: Liquidando posiciones activas.")
                self.close_all("tsaa_end")
            
            if self._session:
                self._finalize_session_and_audit()
            return

        # Detectar trades cerrados externamente (o por SL/TP de Bybit)
        self._detect_closed_trades(states, account)

        # Capturar baseline de PnL al primer tick activo del trade
        for sym, trade in self._active.items():
            if trade.is_active and sym not in self._pnl_captured:
                trade.pnl_at_open = account.daily_pnl
                self._pnl_captured.add(sym)

        # Gestionar trades activos SIEMPRE (respeta el modo por-trade).
        # El modo global MANUAL solo bloquea nuevas entradas, no la gestión
        # de trades que el usuario ha activado individualmente con AUTO.
        if self._active:
            self._manage_active_trades(account, states, techs)

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
                log.info("Auto-ejecución bloqueada: %s — %s", self._proposal.symbol, reason)
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

        # ── API Optimization Guard: Slots Ocupados ──
        if len(self._active) >= self.multi_trades:
            self._scan_log = f"⌛ Slots llenos ({len(self._active)}/{self.multi_trades}) — esperando cierre"
            self._notify()
            return

        # ── Margin Guard ──
        # Verificamos si hay suficiente margen para al menos UN trade mínimo
        av_margin = account.balance.available_balance
        if av_margin < settings.min_trade_margin:
            self._scan_log = f"⚠️ Margen insuficiente (${av_margin:.2f}) — esperando liberación"
            log.warning("[MARGIN GUARD] Saldo insuficiente para operar ($%.2f < $%.2f). Saltando IA.",
                        av_margin, settings.min_trade_margin)
            self._notify()
            return

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

        # ── Modo AI: delegar al agente de OpenAI ──────────────────────────
        if settings.ai_strategy_mode:
            if self._ai_scanning:
                self._scan_log = "🤖 Agente IA analizando… (esperando respuesta)"
                self._notify()
                return
            from core.ai_strategy import ai_agent
            wait = ai_agent.seconds_until_ready()
            if wait > 0:
                self._scan_log = f"🤖 AI: próximo análisis en {wait}s"
                self._notify()
                return
            self._ai_scanning = True
            self._scan_log    = f"🤖 Agente IA consultando {settings.openai_model}…"
            self._notify()
            self._bridge.submit(
                self._run_ai_scan(available, states, account, techs, opps)
            )
            return

        # ── Modo sistema: StrategyEngine estándar ─────────────────────────
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
            symbol_scores = self._symbol_scores,
        )
        self._latest_opps = opps  # guardar para live_scores
        if result:
            sym, proposal = result
            ok, reason = self._pre_flight(proposal, account, risk)
            if ok:
                self._proposal    = proposal
                self._proposal_ts = time.monotonic()
                self._scan_log = (
                    f"✓ Setup encontrado: {sym.replace('USDT', '')}  "
                    f"score={proposal.opp_score}  R:R {proposal.rr_ratio:.1f}"
                )
                log.info("Nueva propuesta: %s", proposal.summary())
                if self.mode == AutoMode.SUGGEST:
                    notifier.proposal_ready(sym, proposal.side, proposal.opp_score, self.goal_usd)
                self._notify()
            else:
                self._scan_log = f"✗ {sym.replace('USDT', '')}: rechazado — {reason}"
                log.info("Propuesta descartada (pre-flight): %s — %s", proposal.symbol, reason)
                self._notify()
        else:
            # Mostrar los top scores para que el usuario sepa qué tan cerca está
            if scores:
                top = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:4]
                score_str = "  ".join(
                    f"{s.replace('USDT', '')}: {sc}" for s, sc in top
                )
                best = top[0][1]
                self._scan_log = f"✗ Sin setup (min {settings.min_scan_score})  —  {score_str}"
            else:
                self._scan_log = "✗ Sin datos de mercado aún"
            #log.info("Scan: sin setup válido — scores: %s", scores)
            self._notify()

    # ── AI Strategy scan (asíncrono) ──────────────────────────────────────────

    async def _run_ai_scan(
        self,
        available: list,
        states:    Dict[str, "MarketState"],
        account:   "AccountState",
        techs:     Dict[str, "TechSignal"],
        opps:      Dict[str, "OpportunitySignal"],
    ) -> None:
        """Corre en el AsyncBridge thread — llama al agente IA y devuelve resultado al GTK thread."""
        from core.ai_strategy import ai_agent
        goal_per_trade = self.goal_usd / max(1, self.multi_trades)
        try:
            result = await ai_agent.generate_proposal(
                symbols       = available,
                states        = states,
                opps          = opps,
                techs         = techs,
                account       = account,
                active_trades = list(self._active.values()),
                goal_usd      = goal_per_trade,
                executor      = self._executor,
                leverage      = self.leverage,
            )
        except Exception as e:
            log.error("_run_ai_scan: excepción inesperada: %s", e)
            result = None
        GLib.idle_add(self._on_ai_result, result)

    def _on_ai_result(self, result) -> bool:
        """Callback en GTK thread — recibe la propuesta del agente IA."""
        self._ai_scanning = False
        
        # Verificar modo Harvest: no aceptar nuevas propuestas de entrada
        if self._session and self._session.status == SessionStatus.HARVESTING:
            log.info("[TSAA] Propuesta rechazada: Modo HARVEST activo.")
            self._scan_log = "TSAA: Modo HARVEST - No se permiten nuevas entradas."
            return False

        if result:
            sym, proposal = result
            
            # Enriquecer propuesta con metadatos de sesión
            if self._session:
                proposal.session_id = self._session.id
            self._proposal    = proposal
            self._proposal_ts = time.monotonic()
            self._scan_log = (
                f"🤖 AI Setup: {sym.replace('USDT', '')}  "
                f"R:R {proposal.rr_ratio:.1f}  conf={proposal.opp_score}%"
            )
            log.info("AI propuesta lista: %s", proposal.summary())
            if self.mode == AutoMode.SUGGEST:
                notifier.proposal_ready(sym, proposal.side, proposal.opp_score, self.goal_usd)
        else:
            self._scan_log = "🤖 AI: sin setup válido en este momento"
            log.info("AI Strategy: sin propuesta")
        self._notify()
        return False   # no repetir idle_add

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
            session_id = req.session_id,
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
                log.error("Error crítico ejecutando orden %s: %s", req.symbol, exc)
                result = _OR(success=False, error_msg=str(exc))
            GLib.idle_add(self._on_order_result, result, req)

        self._bridge.submit(_do())

    def _on_order_result(self, result: OrderResult, req: OrderRequest) -> bool:
        """Callback en GTK thread."""
        self._pending_exec.discard(req.symbol)
        trade = self._active.get(req.symbol)
        if not trade:
            return False

        if result.success:
            trade.state  = TradeState.OPEN
            trade.result = result
            trade.entry_price = result.filled_price or req.entry_price
            trade.opened_at   = int(time.time())
            trade.ai_reasoning = req.ai_reasoning
            log.info("Orden ejecutada con éxito: %s a %.5g", req.symbol, trade.entry_price)
            notifier.trade_opened(
                req.symbol, req.side, trade.entry_price, 
                req.sl_price, req.tp_price, req.goal_usd
            )
        else:
            log.error("Orden falló: %s — %s", req.symbol, result.error_msg)
            notifier.order_failed(req.symbol, result.error_msg)
            self._active.pop(req.symbol, None)

        self._notify()
        return False

    # ── Monitoreo ─────────────────────────────────────────────────────────────

    def _detect_closed_trades(self, states: Dict[str, "MarketState"], account: "AccountState") -> None:
        """Compara estado local con Bybit para detectar cierres externos (SL/TP/Manual)."""
        active_symbols = list(self._active.keys())
        for symbol in active_symbols:
            trade = self._active[symbol]
            if not trade.is_active:
                continue

            # Si el símbolo no aparece en account.positions, Bybit cerró el trade.
            if symbol not in account.positions:
                # Caso especial: Bybit tarda unos mseg en actualizar.
                # Confirmar con un contador de 2 ticks (2s)
                self._close_confirm[symbol] = self._close_confirm.get(symbol, 0) + 1
                if self._close_confirm[symbol] >= 2:
                    self._finalize_trade(symbol, "bybit_close")
            else:
                self._close_confirm[symbol] = 0

    def _finalize_trade(self, symbol: str, reason: str) -> None:
        """Limpia el estado y mueve el trade al log histórico."""
        trade = self._active.pop(symbol, None)
        if trade:
            trade.state = TradeState.CLOSED
            trade.close_reason = reason
            trade.closed_at = int(time.time())
            # intentional bypass of account pnl for simple calculation (if needed)

            self._log.append(trade)
            save_trade(trade)
            notifier.trade_closed(symbol, trade.pnl_usd, trade.close_reason)
            self._track_symbol_perf(symbol, trade.pnl_usd, trade.duration_s)
            self._pnl_captured.discard(symbol)
            self._last_upnl.pop(symbol, None)
            self._be_since.pop(symbol, None)
            self._position_seen.discard(symbol)
            self._tp_removed.discard(symbol)
            self._partial_lock_done.discard(symbol)
            self._pct80_analyzed.discard(symbol)
            self._best_sl.pop(symbol, None)
            self._trail_high.pop(symbol, None)
            self._trail_low.pop(symbol, None)
            self._notify()

    def _manage_active_trades(self, account: "AccountState", states: Dict[str, "MarketState"], techs: Dict[str, "TechSignal"]) -> None:
        """Itera sobre posiciones activas y delega gestión a _manage_one."""
        for sym, trade in list(self._active.items()):
            if trade.is_active:
                ms = states.get(sym)
                tech = techs.get(sym)
                self._manage_one(sym, trade, account, ms, tech)

    def _manage_one(
        self,
        sym:     str,
        trade:   "TradeRecord",
        account: "AccountState",
        ms:      Optional["MarketState"],
        tech:    Optional["TechSignal"] = None,
    ) -> None:
        if not ms or not trade.request:
            return

        pos = account.positions.get(sym)
        mark = ms.ticker.last_price

        # High/Low Water Mark para trailing
        if mark > 0:
            trade.highest_price = max(trade.highest_price, mark)
            trade.lowest_price  = min(trade.lowest_price,  mark)

        if not pos or pos.size <= 0:
            # Detectado cierre en este tick
            return

        trade.pnl_usd = pos.unrealized_pnl

        entry = trade.entry_price or (pos.entry_price if pos else 0.0)
        sl      = trade.current_sl
        tp      = trade.current_tp
        is_long = trade.request.side == "Buy"

        # Referencia de TP para cálculos de progreso (aunque ya no exista el TP real)
        tp_ref = (trade.request.tp_price if (trade.request and trade.request.tp_price > 0) else tp)

        if entry <= 0 or tp_ref <= 0 or sl <= 0:
            return

        tp_dist = abs(tp_ref - entry)
        if tp_dist <= 0:
            return

        # Usar siempre el SL original para medir distancias de riesgo.
        orig_sl = trade.request.sl_price if trade.request.sl_price > 0 else sl
        sl_dist = abs(entry - orig_sl)
        if sl_dist <= 0:
            return

        progress = (mark - entry) / tp_dist if is_long else (entry - mark) / tp_dist
        now      = time.monotonic()

        # ── Rastrear breakeven threshold ──────────
        if progress >= settings.breakeven_pct / 100:
            if sym not in self._be_since:
                self._be_since[sym] = now
        elif progress < settings.breakeven_pct / 100 * 0.80:
            self._be_since.pop(sym, None)

        be_held = (now - self._be_since.get(sym, now)) >= settings.effective_be_hold_s
        elapsed = int(time.time()) - trade.opened_at if trade.opened_at > 0 else 9999
        
        # ── Umbral de Breakeven "Sin Miedo" (0.5% beneficio real) ────────────
        # No basta el 20% del TP; debe haber al menos 0.5% de colchón neto.
        be_dist = entry * 0.005
        current_benefit = (mark - entry) if is_long else (entry - mark)
        has_min_cushion = current_benefit >= be_dist

        # ── Weak Exit ────────────────────────
        if (
            settings.weak_exit_enabled
            and trade.state == TradeState.OPEN
            and elapsed >= settings.weak_exit_min_elapsed_s
        ):
            # score de debilidad
            w_score = self._weakness_score(sym, trade, ms, tech)
            trade.signal_health = 6 - w_score
            if w_score >= settings.weak_exit_min_score:
                # ¿Hacia el SL?
                sl_progress = (entry - mark) / sl_dist if is_long else (mark - entry) / sl_dist
                if sl_progress >= settings.weak_exit_min_sl_pct / 100:
                    self.close_symbol(sym, f"weak_exit({w_score}/6)")
                    self._exit_cooldown[sym] = (trade.request.side, now + settings.symbol_cooldown_s)
                    return

        # ── Salida por Pérdida de Fuerza (Volume Drop / RSI) ───────────────
        if trade.state == TradeState.OPEN and progress > 0.15:
            # 1. Caída de volumen > 50% vs media reciente
            if ms.vol_drop_50:
                log.warning("LossOfStrength: %s exit por caída súbita de volumen", sym)
                self.close_symbol(sym, "vol_drop_50")
                return
            
            # 2. RSI 1m agotado (cruzando 70 abajo para LONG o 30 arriba para SHORT)
            rsi = ms.rsi_1m
            if is_long and rsi < 70 and getattr(trade, '_rsi_peak', 0) >= 70:
                log.warning("LossOfStrength: %s exit por RSI 1m agotado (%.1f)", sym, rsi)
                self.close_symbol(sym, "rsi_exhaustion")
                return
            if not is_long and rsi > 30 and getattr(trade, '_rsi_bottom', 100) <= 30:
                log.warning("LossOfStrength: %s exit por RSI 1m agotado (%.1f)", sym, rsi)
                self.close_symbol(sym, "rsi_exhaustion")
                return
            
            # Track RSI peaks
            if is_long: trade._rsi_peak = max(getattr(trade, '_rsi_peak', 0), rsi)
            else: trade._rsi_bottom = min(getattr(trade, '_rsi_bottom', 100), rsi)

        # ── Time Stop ────────────────────────
        if (
            settings.time_stop_enabled
            and trade.state == TradeState.OPEN
            and elapsed >= settings.time_stop_window_s
        ):
            if progress < settings.time_stop_min_pct / 100:
                self.close_symbol(sym, f"time_stop({elapsed}s)")
                self._exit_cooldown[sym] = (trade.request.side, now + settings.symbol_cooldown_s)
                return

        # ── Gestión Automática de Posición (Breakeven / Trailing) ───────────
        # Determinar el modo efectivo
        effective = trade.auto_mode if trade.auto_mode != AutoMode.MANUAL else self.mode
        if effective == AutoMode.FULL_AUTO:
            self._manage_auto_protections(sym, trade, mark, entry, sl_dist, progress, now, be_held, ms, tech)

    def _manage_auto_protections(self, sym, trade, mark, entry, sl_dist, progress, now, be_held, ms, tech):
        """Lógica de Breakeven y Micro-Trailing elástico."""
        is_long = trade.request.side == "Buy"
        req = trade.request

        # ── Smart80 ──────────
        if (
            progress >= 0.80
            and trade.state == TradeState.TRAILING
            and trade.current_tp > 0
            and sym not in self._pct80_analyzed
            and sym not in self._tp_removed
        ):
            self._pct80_analyzed.add(sym)
            cont80 = self._continuation_score(sym, trade, mark, ms, tech)
            if cont80 >= 3:
                log.info("Smart80: %s prog=%.0f%% cont=%d — señales fuertes, extendiendo TP",
                         sym, progress * 100, cont80)
                trade.current_tp = 0
                self._tp_removed.add(sym)
                atr80 = sl_dist / 1.5
                if is_long:
                    self._trail_high[sym] = mark
                    new_sl80 = mark - atr80 * 0.8
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
                log.info("Smart80: %s prog=%.0f%% cont=%d — señales débiles, asegurando ganancias",
                         sym, progress * 100, cont80)
        current_benefit = (mark - entry) if is_long else (entry - mark)
        has_min_cushion = current_benefit >= (entry * 0.005)

        # ── Profit Guard Continuo ──────────
        _can_modify = (now - self._last_sl_upd.get(sym, 0)) >= 2.0
        if _can_modify and trade.state in (TradeState.OPEN, TradeState.BREAKEVEN, TradeState.TRAILING):
            # ── Breakeven Dinámico ──────────
            # Requiere progreso y el colchón mínimo de 0.5%
            if progress >= settings.breakeven_pct / 100 and has_min_cushion:
                if be_held and trade.state == TradeState.OPEN:
                    new_sl = entry + (entry * 0.0005) if is_long else entry - (entry * 0.0005) # pequeño lock
                    trade.state = TradeState.TRAILING
                    log.info("Profit Guard: %s SL → %.5g (Breakeven 0.5%% asegurado)", sym, new_sl)
                    trade.current_sl = new_sl
                    notifier.breakeven_activated(sym, new_sl)

            fee_buffer = entry * _BE_FEE_PCT
            fee_be_price = (entry + fee_buffer) if is_long else (entry - fee_buffer)
            atr = sl_dist / 1.5
            
            is_safe = trade.state != TradeState.OPEN or has_min_cushion
            
            if is_safe:
                # ── Trailing Elástico (Dynamic Trailing) ───────────────────────
                # Si hay mucha fuerza (Tape Speed > 1.5), dejar respirar (1.0 ATR).
                # Si hay poca fuerza (Tape Speed < 0.5), ceñir (0.4 ATR).
                ts = ms.tape_speed
                if ts > 1.5:
                    trail_atr_mult = 1.0  # Amplio
                elif ts < 0.5:
                    trail_atr_mult = 0.4  # Ceñido
                else:
                    trail_atr_mult = 0.7  # Standard
                
                t_mult = trail_atr_mult if progress < 1.0 else 0.4
                
                if is_long:
                    self._trail_high[sym] = max(self._trail_high.get(sym, mark), mark)
                    proposed_sl = self._trail_high[sym] - atr * t_mult
                else:
                    self._trail_low[sym] = min(self._trail_low.get(sym, mark), mark)
                    proposed_sl = self._trail_low[sym] + atr * t_mult
                
                # El SL nunca retrocede y respeta el Break-Even como suelo
                floor_sl = max(trade.current_sl, fee_be_price) if is_long else min(trade.current_sl, fee_be_price)
                new_sl = max(floor_sl, proposed_sl) if is_long else min(floor_sl, proposed_sl)
                
                if is_long:
                    is_meaningful = new_sl > trade.current_sl * (1 + TRAIL_MIN_MOVE_PCT)
                else:
                    is_meaningful = new_sl < trade.current_sl * (1 - TRAIL_MIN_MOVE_PCT)
                
                # Forzar primer movimiento de protección
                if trade.state == TradeState.OPEN and new_sl != trade.current_sl:
                    is_meaningful = True
                    
                if is_meaningful:
                    if trade.state == TradeState.OPEN:
                        trade.state = TradeState.BREAKEVEN
                        log.info("Profit Guard: %s SL → %.5g (Risk-Free asegurado)", sym, new_sl)
                        notifier.breakeven_activated(sym, new_sl)
                    else:
                        log.info("Profit Guard: %s SL trailing → %.5g (momentum=%.1f)", sym, new_sl, ts)
                        if trade.state != TradeState.TRAILING: # evitar doble notify si ya es trailing
                            trade.state = TradeState.TRAILING
                            notifier.trailing_activated(sym, new_sl)
                    
                    trade.current_sl = new_sl
                    self._bridge.submit(self._modify_sl_safe(sym, new_sl, req.side, "profit-guard"))
                    self._last_sl_upd[sym] = now
                    self._notify()

    # ── Métricas de señal ───────────────────────────────────────────────────

    def _weakness_score(self, sym: str, trade: TradeRecord, ms: MarketState, tech: Optional[TechSignal]) -> int:
        """Puntúa debilidad/pérdida de setup de 0 (fuerte) a 6 (crítico)."""
        score = 0
        is_long = trade.request.side == "Buy"
        tk = ms.ticker

        # 1. Dirección vs Precio
        if (is_long and tk.last_price < trade.entry_price) or (not is_long and tk.last_price > trade.entry_price):
            score += 1
        
        # 2. CVD en contra (velas 1m)
        cvd_candles = list(ms.cvd_candles)[-5:] if hasattr(ms, 'cvd_candles') else []
        if len(cvd_candles) >= 3:
            bull = sum(1 for c in cvd_candles if c.delta > 0)
            bear = len(cvd_candles) - bull
            if (is_long and bear >= 3) or (not is_long and bull >= 3):
                score += 1
            if (is_long and bear >= 5) or (not is_long and bull >= 5):
                score += 1
        
        # 3. RSI / Momentum alignment
        if tech and tech.has_data:
            if tech.ema15m_bull != is_long:
                score += 1
        
        return score

    def _continuation_score(self, sym: str, trade: TradeRecord, mark: float, ms: MarketState, tech: Optional[TechSignal]) -> int:
        """Puntúa momentum para decidir si extender TP. 0-5."""
        score = 0
        is_long = trade.request.side == "Buy"
        
        # 1. CVD fuerte
        cvd_candles = list(ms.cvd_candles)[-5:] if hasattr(ms, 'cvd_candles') else []
        if cvd_candles:
            bull = sum(1 for c in cvd_candles if c.delta > 0)
            if (is_long and bull >= 4) or (not is_long and bull <= 1):
                score += 2
            elif (is_long and bull >= 3) or (not is_long and bull <= 2):
                score += 1
        
        # 2. Dirección de velas CVD (proxy de fuerza actual)
        candle = ms.cvd_candles[-1] if ms.cvd_candles else None
        if candle:
            is_bull = candle.delta > 0
            if (is_long and is_bull) or (not is_long and not is_bull):
                score += 1
        
        # 3. EMA alignment
        if tech and tech.has_data:
            if tech.ema15m_bull == is_long:
                score += 2
            
        return score

    # ── Bridge Tasks (llamadas a Executor) ───────────────────────────────────

    async def _resolve_open_time(self, sym: str, hint_ms: int) -> None:
        """Intenta sincronizar el opened_at desde el historial de Bybit."""
        # Implementar si es necesario para logs precisos
        pass

    async def _modify_sl_safe(self, sym: str, sl: float, side: str, reason: str) -> None:
        try:
            await self._executor.set_sl_tp(sym, sl=sl, side=side)
            # log.debug("SL modificado (%s): %s → %.5g", reason, sym, sl)
        except Exception as e:
            log.error("Error modificando SL %s: %s", sym, e)

    async def _clear_tp(self, sym: str, side: str) -> None:
        try:
            await self._executor.set_sl_tp(sym, tp=0, side=side)
            log.info("TP elminado: %s", sym)
        except Exception as e:
            log.error("Error eliminando TP %s: %s", sym, e)

    async def _clear_tp_and_trail(self, sym: str, sl: float, side: str) -> None:
        try:
            # En Bybit, poner TP=0 lo elimina.
            await self._executor.set_sl_tp(sym, sl=sl, tp=0, side=side)
            # log.debug("SL Trail (TP=0): %s → %.5g", sym, sl)
        except Exception as e:
            log.error("Error en trail %s: %s", sym, e)

    async def _do_close(self, symbol: str, req: OrderRequest, reason: str = "manual") -> None:
        try:
            result = await self._executor.close_position(symbol, req.qty, req.side)
            if result.success:
                GLib.idle_add(self._finalize_trade, symbol, reason)
            else:
                log.error("Cierre falló: %s — %s", symbol, result.error_msg)
        except Exception as e:
            log.error("Excepción al cerrar %s: %s", symbol, e)

    # ── Auto-blacklist ────────────────────────

    def _track_symbol_perf(self, sym: str, pnl_usd: float, duration_s: int = 0) -> None:
        """Actualiza conteo de pérdidas consecutivas, auto-blacklist y SHPP."""
        # ── Historial reciente para detector de mercado choppy ────────────────
        self._recent_results.append("loss" if pnl_usd < 0 else "win")
        if len(self._recent_results) > 10:
            self._recent_results.pop(0)

        # ── Sistema de Salud de Símbolos (SHPP) ──────────────────────────────
        score = self._symbol_scores.get(sym, 0.0)
        if pnl_usd > 0:
            score += 2.0
            # Resetear pérdidas consecutivas al ganar
            self._consec_losses.pop(sym, 0)
        elif pnl_usd < 0:
            self._consec_losses[sym] = self._consec_losses.get(sym, 0) + 1
            # Penalización extra si el trade duró poco (volatilidad/ruido)
            if duration_s > 0 and duration_s < 180:   # < 3 minutos
                score -= 5.0
                log.warning("SHPP: %s penalización fuerte por caída rápida (%ds)", sym, duration_s)
            else:
                score -= 1.0

        # Clipping -10 a +10
        self._symbol_scores[sym] = max(-10.0, min(10.0, score))

        # ── Auto-blacklist (Basado en SHPP soft-blacklist o consecutivas) ────
        n = self._consec_losses.get(sym, 0)
        shpp_soft = self._symbol_scores[sym] < -7.0
        
        if settings.auto_blacklist_enabled:
            # Bloqueo si: muchas consecutivas O salud muy baja (soft blacklist)
            should_block = (n >= settings.auto_blacklist_losses) or shpp_soft
            bl = settings.blacklist_set
            
            if should_block and sym not in bl:
                bl.add(sym)
                settings.symbol_blacklist = ",".join(sorted(bl))
                reason = "soft-blacklist SHPP" if shpp_soft else f"{n} pérdidas seguidas"
                log.warning("AutoBlacklist: %s añadido — %s", sym, reason)
            
            elif not should_block and sym in bl:
                if pnl_usd > 0:
                    bl.discard(sym)
                    settings.symbol_blacklist = ",".join(sorted(bl))
                    log.info("AutoBlacklist: %s eliminado (recuperación confirmada)", sym)

    @property
    def is_choppy_market(self) -> bool:
        """Analiza resultados recientes para detectar series de pérdidas (choppy)."""
        if len(self._recent_results) < 5:
            return False
        losses = sum(1 for r in self._recent_results[-8:] if r == "loss")
        return losses >= 6

    def _notify(self) -> None:
        st = self.get_state()
        for cb in self._callbacks:
            cb(st)
