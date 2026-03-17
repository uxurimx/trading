"""
core/order_model.py
───────────────────
Modelos de datos para ejecución de órdenes y automatización.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


# ─── Enums ────────────────────────────────────────────────────────────────────

class AutoMode(str, Enum):
    MANUAL     = "MANUAL"       # solo monitorea, tú ejecutas todo
    SUGGEST    = "SUGGEST"      # propone órdenes, tú confirmas con 1 clic
    AUTO_ENTRY = "AUTO_ENTRY"   # entra automáticamente, tú gestionas SL/TP
    FULL_AUTO  = "FULL_AUTO"    # entra + gestiona trail + cierra solo

class TradeState(str, Enum):
    PENDING    = "PENDING"      # propuesta generada, pendiente de envío
    SUBMITTED  = "SUBMITTED"    # enviada a Bybit, esperando fill
    OPEN       = "OPEN"         # posición activa
    BREAKEVEN  = "BREAKEVEN"    # SL movido a entrada (+1R alcanzado)
    TRAILING   = "TRAILING"     # trailing stop activo (+2R alcanzado)
    CLOSED     = "CLOSED"       # trade cerrado con PnL calculado
    FAILED     = "FAILED"       # error en algún punto


# ─── Orden ────────────────────────────────────────────────────────────────────

@dataclass
class OrderRequest:
    """Propuesta de orden calculada por StrategyEngine. No ejecuta nada."""
    symbol:       str
    side:         str           # "Buy" | "Sell"
    qty:          float
    order_type:   str = "Market"   # "Market" | "Limit"
    price:        float = 0.0      # solo para Limit
    sl_price:     float = 0.0
    tp_price:     float = 0.0

    # Metadatos del setup
    entry_price:  float = 0.0      # precio esperado de entrada (mark en moment of proposal)
    goal_usd:     float = 0.0      # objetivo de ganancia
    risk_usd:     float = 0.0      # pérdida máxima si SL se activa
    rr_ratio:     float = 0.0
    opp_score:    int   = 0
    notional:     float = 0.0
    margin:       float = 0.0
    leverage:     int   = 1

    # Razones para mostrar en UI
    reasons:      List[str] = field(default_factory=list)

    @property
    def direction(self) -> str:
        return "LONG" if self.side == "Buy" else "SHORT"

    @property
    def is_valid(self) -> bool:
        return (
            self.qty > 0 and
            self.sl_price > 0 and
            self.tp_price > 0 and
            self.rr_ratio >= 1.5
        )

    def summary(self) -> str:
        arrow = "▲" if self.side == "Buy" else "▼"
        return (
            f"{arrow} {self.direction} {self.symbol}  "
            f"qty={self.qty}  entry≈{self.entry_price:.5g}  "
            f"SL={self.sl_price:.5g}  TP={self.tp_price:.5g}  "
            f"R:R {self.rr_ratio:.1f}:1  Score={self.opp_score}"
        )


@dataclass
class OrderResult:
    success:      bool
    order_id:     str   = ""
    error_msg:    str   = ""
    filled_price: float = 0.0
    filled_qty:   float = 0.0
    timestamp:    int   = field(default_factory=lambda: int(time.time() * 1000))


# ─── Trade record ─────────────────────────────────────────────────────────────

@dataclass
class TradeRecord:
    """Ciclo de vida completo de un trade gestionado por TradeController."""
    id:              str          = field(default_factory=lambda: str(uuid.uuid4())[:8])
    symbol:          str          = ""
    request:         Optional[OrderRequest] = None
    result:          Optional[OrderResult]  = None

    state:           TradeState   = TradeState.PENDING
    entry_price:     float        = 0.0
    current_sl:      float        = 0.0
    current_tp:      float        = 0.0
    highest_price:   float        = 0.0    # para trailing (LONG)
    lowest_price:    float        = 9e9    # para trailing (SHORT)

    pnl_usd:         float        = 0.0
    pnl_at_open:     float        = 0.0    # daily_pnl snapshot al abrir (para calcular PnL del trade)
    close_reason:    str          = ""
    auto_mode:       AutoMode     = AutoMode.MANUAL

    opened_at:       int          = 0
    closed_at:       int          = 0

    @property
    def is_active(self) -> bool:
        return self.state in (TradeState.OPEN, TradeState.BREAKEVEN, TradeState.TRAILING)

    @property
    def duration_s(self) -> int:
        if self.opened_at <= 0:
            return 0
        end = self.closed_at if self.closed_at > 0 else int(time.time())
        return end - self.opened_at

    def result_line(self) -> str:
        """Una línea resumen para el log de trades."""
        arrow = "▲" if self.request and self.request.side == "Buy" else "▼"
        sign  = "+" if self.pnl_usd >= 0 else ""
        return (
            f"{arrow} {self.symbol}  {sign}${self.pnl_usd:.2f}"
            f"  [{self.close_reason or self.state.value}]"
        )


# ─── Estado del controlador (para notificar a la UI) ─────────────────────────

MAX_POSITIONS: int = 3    # máximo de trades simultáneos


@dataclass
class ControllerState:
    mode:            AutoMode         = AutoMode.MANUAL
    goal_usd:        float            = 1.0
    proposal:        Optional[OrderRequest]  = None
    proposal_age_s:  int              = 0
    active_trades:   List[TradeRecord] = field(default_factory=list)
    last_result:     Optional[TradeRecord]   = None
    status_msg:      str              = ""
    error_msg:       str              = ""
    scan_in:         int              = 0
