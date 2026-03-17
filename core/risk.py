"""
core/risk.py
────────────
Risk Fortress — Phase 5.

Circuit breakers automáticos que protegen el capital:

  CIRCUIT_BREAKER  — pérdida diaria superó el límite → PARA de operar
  ALERT            — margen > 80% del equity (peligro de liquidación masiva)
  WARNING          — pérdida diaria > 50% del límite (cuidado)
  OK               — todo dentro de parámetros

El objetivo no es solo mostrar números; es que el sistema te obligue
a hacer una pausa cuando las emociones toman el control.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, List

from core.config import settings

if TYPE_CHECKING:
    from streams.account import AccountState, Position, AccountBalance


# ─── Risk Status ──────────────────────────────────────────────────────────────

@dataclass
class RiskStatus:
    level:          str     # "OK" | "WARNING" | "ALERT" | "CIRCUIT_BREAKER"
    color_key:      str     # "buy" | "warn" | "sell"
    icon:           str     # "✓" | "⚠" | "🔴"
    message:        str

    daily_pnl_usd:  float   # PnL realizado del día
    daily_pnl_pct:  float   # como % del equity
    margin_pct:     float   # % del equity usado como margen
    unrealized_usd: float   # PnL no realizado total

    @property
    def is_breaker(self) -> bool:
        return self.level == "CIRCUIT_BREAKER"

    @property
    def is_warning(self) -> bool:
        return self.level in ("WARNING", "ALERT", "CIRCUIT_BREAKER")


OK_STATUS = RiskStatus(
    level="OK", color_key="buy", icon="✓",
    message="Todo dentro de parámetros",
    daily_pnl_usd=0.0, daily_pnl_pct=0.0,
    margin_pct=0.0, unrealized_usd=0.0,
)


# ─── Risk Fortress ────────────────────────────────────────────────────────────

class RiskFortress:
    """
    Evalúa el estado de riesgo de la cuenta.
    Llamar .check(account_state) en cada ciclo de UI.
    """

    # Thresholds adicionales (los principales vienen de settings)
    MARGIN_ALERT_PCT   = 80.0   # margen > 80% del equity → ALERT
    MARGIN_WARNING_PCT = 60.0   # margen > 60% → WARNING
    LOSS_WARNING_RATIO = 0.50   # pérdida > 50% del límite diario → WARNING

    def check(self, acct: "AccountState") -> RiskStatus:
        b     = acct.balance
        daily = acct.daily_pnl   # PnL realizado del día (negativo = pérdida)

        equity = b.total_equity
        if equity <= 0:
            return OK_STATUS   # sin datos aún

        # ── Métricas ──────────────────────────────────────────────────────────
        daily_pnl_pct  = daily / equity * 100
        margin_pct     = b.margin_pct
        unrealized_usd = b.unrealized_pnl

        # También considerar pérdida no realizada en el cálculo total
        total_exposure_pct = (daily + min(unrealized_usd, 0)) / equity * 100

        max_loss = settings.max_daily_loss_pct   # negativo: -2.0 = -2%

        # ── CIRCUIT BREAKER: pérdida total (realizada + no realizada) ─────────
        if settings.circuit_breaker_enabled and total_exposure_pct <= -abs(max_loss):
            return RiskStatus(
                level="CIRCUIT_BREAKER", color_key="sell", icon="🔴",
                message=f"STOP — pérdida diaria {total_exposure_pct:.1f}% ≥ límite -{abs(max_loss):.1f}%",
                daily_pnl_usd=daily, daily_pnl_pct=daily_pnl_pct,
                margin_pct=margin_pct, unrealized_usd=unrealized_usd,
            )

        # ── ALERT: margen muy alto ─────────────────────────────────────────────
        if margin_pct >= self.MARGIN_ALERT_PCT:
            return RiskStatus(
                level="ALERT", color_key="sell", icon="⚠",
                message=f"Margen {margin_pct:.0f}% — riesgo de liquidación",
                daily_pnl_usd=daily, daily_pnl_pct=daily_pnl_pct,
                margin_pct=margin_pct, unrealized_usd=unrealized_usd,
            )

        # ── WARNING: pérdida > 50% del límite ─────────────────────────────────
        if daily_pnl_pct <= -abs(max_loss) * self.LOSS_WARNING_RATIO:
            return RiskStatus(
                level="WARNING", color_key="warn", icon="⚠",
                message=f"Pérdida diaria {daily_pnl_pct:.1f}% — acercándose al límite",
                daily_pnl_usd=daily, daily_pnl_pct=daily_pnl_pct,
                margin_pct=margin_pct, unrealized_usd=unrealized_usd,
            )

        # ── WARNING: margen elevado ────────────────────────────────────────────
        if margin_pct >= self.MARGIN_WARNING_PCT:
            return RiskStatus(
                level="WARNING", color_key="warn", icon="⚠",
                message=f"Margen elevado {margin_pct:.0f}%",
                daily_pnl_usd=daily, daily_pnl_pct=daily_pnl_pct,
                margin_pct=margin_pct, unrealized_usd=unrealized_usd,
            )

        # ── OK ────────────────────────────────────────────────────────────────
        return RiskStatus(
            level="OK", color_key="buy", icon="✓",
            message="",
            daily_pnl_usd=daily, daily_pnl_pct=daily_pnl_pct,
            margin_pct=margin_pct, unrealized_usd=unrealized_usd,
        )


# ─── Position Sizer ───────────────────────────────────────────────────────────

class PositionSizer:
    """
    Calcula el tamaño de posición basado en riesgo fijo por trade.
    risk_pct = % del equity que quieres arriesgar.
    stop_dist_pct = distancia al stop en % del precio de entrada.
    """

    @staticmethod
    def size(
        equity:         float,
        entry_price:    float,
        stop_price:     float,
        risk_pct:       float = 1.0,    # arriesgar 1% del equity
        leverage:       float = 1.0,
    ) -> dict:
        if equity <= 0 or entry_price <= 0 or stop_price <= 0:
            return {"contracts": 0.0, "notional": 0.0, "margin": 0.0, "risk_usd": 0.0}

        risk_usd    = equity * risk_pct / 100
        stop_dist   = abs(entry_price - stop_price)
        if stop_dist <= 0:
            return {"contracts": 0.0, "notional": 0.0, "margin": 0.0, "risk_usd": 0.0}

        contracts   = risk_usd / stop_dist
        notional    = contracts * entry_price
        margin      = notional / leverage

        return {
            "contracts": round(contracts, 2),
            "notional":  round(notional, 2),
            "margin":    round(margin, 2),
            "risk_usd":  round(risk_usd, 2),
        }
