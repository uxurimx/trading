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
    API_EXHAUSTED = "API_EXHAUSTED" # Límite de gasto en API alcanzado
    CLOSED      = "CLOSED"      # Sesión finalizada

class SessionManager:
    """
    Gestiona el estado global de la sesión actual.
    Calcula el PnL acumulado (cerrado + flotante) y vigila los límites.
    """

    def __init__(
        self,
        initial_balance: float,
        name:         str   = "",
        target_pnl:   float = 0.0,
        max_drawdown: float = 0.0,
        duration_h:   float = 0.0,
    ) -> None:
        self.id: str = f"sess-{uuid.uuid4().hex[:6]}"
        self.start_ts: int = int(time.time())
        self.initial_balance: float = initial_balance

        self.current_balance: float = initial_balance
        self.floating_pnl:    float = 0.0
        self.closed_pnl:      float = 0.0

        self.status: SessionStatus = SessionStatus.ACTIVE
        self.end_ts: int = 0
        self.api_cost: float = 0.0

        # Parámetros — los argumentos explícitos tienen prioridad sobre settings
        self.name:         str   = name        or settings.session_name
        self.target_pnl:   float = target_pnl  if target_pnl  != 0.0 else settings.session_target_pnl
        self.max_drawdown: float = max_drawdown if max_drawdown != 0.0 else settings.session_max_drawdown
        self.duration_h:   float = duration_h  if duration_h  != 0.0 else settings.session_duration_h
        self.max_duration_s: int = int(self.duration_h * 3600)
        self.api_limit:    float = settings.session_api_limit

        log.info("[TSAA] Nueva sesión '%s' iniciada: %s  Balance=$%.2f",
                 self.name, self.id, initial_balance)
        log.info("[TSAA] Límites: TP=$%.1f SL=$%.1f API=$%.2f Dur=%.1fh",
                 self.target_pnl, self.max_drawdown, self.api_limit, self.duration_h)

        self._persist()

    @classmethod
    def from_snapshot(cls, data: dict) -> "SessionManager":
        """
        Restaura un SessionManager desde un registro de DB.
        Permite reanudar una sesión activa tras reinicio del sistema.
        El closed_pnl se toma directamente del snapshot; la sesión continúa
        acumulando desde ese punto.
        """
        mgr: "SessionManager" = object.__new__(cls)
        mgr.id              = data["id"]
        mgr.name            = data.get("name", "Sesión")
        mgr.start_ts        = int(data["start_ts"])
        mgr.end_ts          = int(data.get("end_ts", 0))
        mgr.initial_balance = float(data["initial_balance"])
        mgr.current_balance = float(data.get("final_balance", data["initial_balance"]))
        mgr.floating_pnl    = 0.0
        mgr.closed_pnl      = float(data.get("pnl", 0.0))
        mgr.api_cost        = float(data.get("api_cost", 0.0))
        try:
            mgr.status = SessionStatus(data["status"])
        except (ValueError, KeyError):
            mgr.status = SessionStatus.ACTIVE

        # Objetivos almacenados en DB, fallback a settings si están a 0
        mgr.target_pnl   = float(data.get("target_pnl", 0.0))   or settings.session_target_pnl
        mgr.max_drawdown = float(data.get("max_drawdown", 0.0))  or settings.session_max_drawdown
        mgr.duration_h   = float(data.get("duration_h", 0.0))   or settings.session_duration_h
        mgr.max_duration_s = int(mgr.duration_h * 3600)
        mgr.api_limit    = settings.session_api_limit

        log.info(
            "[TSAA] Sesión '%s' reanudada desde DB: %s  PnL acumulado=$%.2f  "
            "Objetivo=$%.1f  Drawdown=$%.1f",
            mgr.name, mgr.id, mgr.closed_pnl, mgr.target_pnl, mgr.max_drawdown,
        )
        return mgr

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
                self.status = SessionStatus.LIQUIDATING

        # 4. Límite de API
        elif self.api_cost >= self.api_limit:
            if self.status == SessionStatus.ACTIVE:
                log.warning("[TSAA] Límite de gasto en API alcanzado ($%.2f). Deteniendo nuevas consultas.", self.api_cost)
                self.status = SessionStatus.API_EXHAUSTED

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
            "name":            self.name,
            "start_ts":        self.start_ts,
            "end_ts":          self.end_ts,
            "initial_balance": self.initial_balance,
            "final_balance":   self.current_balance,
            "pnl":             self.closed_pnl,
            "api_cost":        self.api_cost,
            "status":          self.status.value,
            "target_pnl":      self.target_pnl,
            "max_drawdown":    self.max_drawdown,
            "duration_h":      self.duration_h,
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

    def add_api_usage(self, model: str, prompt_tokens: int, completion_tokens: int) -> float:
        """
        Calcula el costo del request y lo acumula.
        Precios aprox (USD por 1M de tokens):
          gpt-4o:      In $5.00,  Out $15.00
          gpt-4o-mini: In $0.15,  Out $0.60
          o3-mini:     In $1.10,  Out $4.40
        """
        # Costos por 1 token
        costs = {
            "gpt-4o":       (5.00/1e6, 15.00/1e6),
            "gpt-4o-mini":  (0.15/1e6,  0.60/1e6),
            "o3-mini":      (1.10/1e6,  4.40/1e6),
            "gpt-4-turbo":  (10.0/1e6,  30.0/1e6),
        }
        
        c_in, c_out = costs.get(model, (1.0/1e6, 3.0/1e6)) # Fallback genérico
        
        cost = (prompt_tokens * c_in) + (completion_tokens * c_out)
        self.api_cost += cost
        
        log.debug("[TSAA] API usage: %s (+ $%.4f) | Total: $%.4f", model, cost, self.api_cost)
        self._persist()
        return cost
