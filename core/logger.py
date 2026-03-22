"""
core/logger.py
──────────────
Sistema de Observabilidad Estructurada de QTS.

Cada evento es un registro semántico (no texto plano) con:
  · trace_id   — agrupa todos los eventos de una misma operación
  · component  — quién emite el evento
  · event      — código de evento (QUÉ pasó, e.g. ORDER_SUCCESS)
  · payload    — estado del mundo en ese instante (JSON)

Uso básico:
    from core.logger import executor_logger

    # Contexto sincrónico
    with executor_logger.context(trace_id) as tid:
        executor_logger.info("ORDER_SENT", "Enviando orden", {"symbol": "BTCUSDT"})

    # Sin context manager (usa trace_id del contextvars activo)
    executor_logger.error("ORDER_ERROR", "Fallo", {"retCode": 34040})

    # Contexto desde un trade existente
    with executor_logger.from_trade(trade_record):
        executor_logger.info("BREAKEVEN", "SL movido a entrada", {"sl": 1.234})

Proveedores:
    strategy_logger   — AI_STRATEGY
    executor_logger   — EXECUTOR
    controller_logger — CONTROLLER
    risk_logger       — RISK
    system_logger     — SYSTEM
"""
from __future__ import annotations

import contextvars
import time
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Optional, TYPE_CHECKING

from core.db import enqueue_system_log

if TYPE_CHECKING:
    from core.order_model import TradeRecord

# ContextVar propaga el trace_id automáticamente en el hilo/task actual
_trace_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "trace_id", default=None
)


class StructuredLogger:
    """
    Logger que envía eventos estructurados a la base de datos de forma asíncrona.
    Soporta Trace IDs para correlacionar todos los eventos de una operación.
    """

    def __init__(self, component: str):
        self.component = component

    # ── Context managers ───────────────────────────────────────────────────────

    def context(self, trace_id: Optional[str] = None):
        """
        Context manager sincrónico para establecer un Trace ID.
        Si no se provee uno, se genera un UUID corto de 8 caracteres.

        Uso:
            with logger.context("abc12345") as tid:
                logger.info("EVENT", "mensaje")
        """
        if not trace_id:
            trace_id = str(uuid.uuid4())[:8]
        token = _trace_id_var.set(trace_id)
        return _TraceContext(token, trace_id)

    def from_trade(self, trade: "TradeRecord"):
        """
        Context manager que hereda el trace_id de un TradeRecord.
        Conveniente en el controller para mantener la trazabilidad.
        """
        return self.context(trade.trace_id if trade else None)

    # ── Registro de eventos ───────────────────────────────────────────────────

    def log(
        self,
        level:   str,
        event:   str,
        message: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Registra un evento estructurado en la cola asíncrona de DB."""
        entry = {
            "trace_id":  _trace_id_var.get(),
            "ts":        int(time.time() * 1000),
            "level":     level.upper(),
            "component": self.component,
            "event":     event.upper(),
            "message":   message,
            "payload":   payload or {},
        }
        enqueue_system_log(entry)

    def debug(self, event: str, message: str, payload: Optional[Dict[str, Any]] = None) -> None:
        self.log("DEBUG", event, message, payload)

    def info(self, event: str, message: str, payload: Optional[Dict[str, Any]] = None) -> None:
        self.log("INFO", event, message, payload)

    def warning(self, event: str, message: str, payload: Optional[Dict[str, Any]] = None) -> None:
        self.log("WARNING", event, message, payload)

    def error(self, event: str, message: str, payload: Optional[Dict[str, Any]] = None) -> None:
        self.log("ERROR", event, message, payload)

    def critical(self, event: str, message: str, payload: Optional[Dict[str, Any]] = None) -> None:
        self.log("CRITICAL", event, message, payload)


class _TraceContext:
    """Contexto sincrónico que establece y restaura el trace_id."""

    def __init__(self, token: contextvars.Token, trace_id: str):
        self.token    = token
        self.trace_id = trace_id

    def __enter__(self) -> str:
        return self.trace_id

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        _trace_id_var.reset(self.token)


# ── Instancias pre-configuradas por componente ────────────────────────────────

strategy_logger   = StructuredLogger("AI_STRATEGY")
executor_logger   = StructuredLogger("EXECUTOR")
controller_logger = StructuredLogger("CONTROLLER")
risk_logger       = StructuredLogger("RISK")
system_logger     = StructuredLogger("SYSTEM")
