"""
core/session.py
───────────────
Orquestador de sesiones TSAA. Controla el ciclo de vida, límites de PnL 
y duración de la jornada operativa.
"""
from __future__ import annotations

import logging
import time
import uuid
from enum import Enum
from typing import Optional

from core.config import settings
from core.db import save_session

log = logging.getLogger("qts.session")

class SessionStatus(Enum):
    ACTIVE      = "ACTIVE"      # Operación normal
    HARVESTING  = "HARVESTING"  # Objetivo alcanzado: no nuevas entradas, dejar cerrar activas
    LIQUIDATING = "LIQUIDATING" # Stop-Loss global: cerrar todo inmediatamente
    CLOSED      = "CLOSED"      # Sesión finalizada

class SessionManager:
    """
    Gestiona el estado global de la sesión actual.
    Calcula el PnL acumulado (cerrado + flotante) y vigila los límites.
    """

    def __init__(self, initial_balance: float) -> None:
        self.id: str = f"sess-{uuid.uuid4().hex[:6]}"
        self.start_ts: int = int(time.time())
        self.initial_balance: float = initial_balance
        
        self.current_balance: float = initial_balance
        self.floating_pnl:    float = 0.0
        self.closed_pnl:      float = 0.0
        
        self.status: SessionStatus = SessionStatus.ACTIVE
        self.end_ts: int = 0
        
        # Límites desde settings
        self.max_duration_s: int = int(settings.session_duration_h * 3600)
        self.target_pnl:     float = settings.session_target_pnl
        self.max_drawdown:   float = settings.session_max_drawdown

        log.info("[TSAA] Nueva sesión iniciada: %s  Balance=$%.2f  Límites: TP=$%.1f SL=$%.1f Dur=%.1fh",
                 self.id, initial_balance, self.target_pnl, self.max_drawdown, settings.session_duration_h)
        
        self._persist()

    def update(self, current_balance: float, floating_pnl: float) -> SessionStatus:
        """
        Actualiza el estado de la sesión con los datos más recientes de la billetera.
        Retorna el status actual para que el controlador decida qué hacer.
        """
        if self.status == SessionStatus.CLOSED:
            return self.status

        self.current_balance = current_balance
        self.floating_pnl    = floating_pnl
        self.closed_pnl      = current_balance - self.initial_balance
        
        total_pnl = self.closed_pnl + self.floating_pnl
        elapsed   = time.time() - self.start_ts

        # 1. Prioridad Absoluta: Max Drawdown (Stop Loss Global)
        if total_pnl <= self.max_drawdown:
            if self.status != SessionStatus.LIQUIDATING:
                log.warning("[TSAA] CRÍTICO: Max Drawdown alcanzado ($%.2f). Iniciando LIQUIDACIÓN.", total_pnl)
                self.status = SessionStatus.LIQUIDATING

        # 2. Objetivo de Beneficio (Harvest)
        elif self.closed_pnl >= self.target_pnl:
            if self.status == SessionStatus.ACTIVE:
                log.info("[TSAA] Objetivo de sesión alcanzado ($%.2f). Modo HARVEST activo.", self.closed_pnl)
                self.status = SessionStatus.HARVESTING

        # 3. Límite de Tiempo
        elif elapsed >= self.max_duration_s:
            if self.status != SessionStatus.LIQUIDATING:
                log.info("[TSAA] Tiempo de sesión agotado. Cerrando jornada.")
                # Si hay trades abiertos, entramos en modo liquidación suave o directa
                self.status = SessionStatus.LIQUIDATING

        if self.status != SessionStatus.ACTIVE:
            self._persist()

        return self.status

    def close(self) -> None:
        """Finaliza formalmente la sesión."""
        if self.status == SessionStatus.CLOSED:
            return
            
        self.status = SessionStatus.CLOSED
        self.end_ts = int(time.time())
        log.info("[TSAA] Sesión %s cerrada. PnL Final: $%.2f", self.id, self.closed_pnl)
        self._persist()

    def _persist(self) -> None:
        """Guarda el estado actual en la base de datos."""
        data = {
            "id":              self.id,
            "start_ts":        self.start_ts,
            "end_ts":          self.end_ts,
            "initial_balance": self.initial_balance,
            "final_balance":   self.current_balance,
            "pnl":             self.closed_pnl,
            "status":          self.status.value
        }
        save_session(data)

    @property
    def total_pnl(self) -> float:
        return self.closed_pnl + self.floating_pnl

    @property
    def elapsed_s(self) -> int:
        return int(time.time() - self.start_ts)

    @property
    def time_left_s(self) -> int:
        return max(0, self.max_duration_s - self.elapsed_s)
