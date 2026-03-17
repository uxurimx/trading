"""
core/regime.py
──────────────
Clasificador de régimen de mercado + Score de Oportunidad — Phase 4.

Régimen: el «contexto» en el que ocurre la señal de absorción.
  RANGING      — precio lateralizando entre dos niveles
  TRENDING_UP  — tendencia alcista sostenida
  TRENDING_DOWN— tendencia bajista sostenida
  VOLATILE     — movimientos erráticos en ambos sentidos
  ACCUMULATION — baja volatilidad + OI creciendo + CVD equilibrado

Score de Oportunidad (0-100):
  Combina absorción + régimen + tendencia + liquidez en un único número.
  Solo tiene significado cuando hay señal de absorción activa.
  El objetivo es responder: «¿qué tan buena es ESTA entrada AHORA?»

Mejores setups:
  · RANGING  + absorción compradora en equal lows  → 85-100
  · ACUM     + absorción compradora + OI creciendo → 80-95
  · TRENDING + absorción EN dirección del trend    → 70-85  (continuación)
  · TRENDING + absorción CONTRA el trend           → 20-40  (peligroso)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from streams.market import MarketState
    from core.absorption import AbsorptionSignal
    from core.liquidity import LiquidityMap
    from core.trend import TrendSignal


# ─── Definición de regímenes ──────────────────────────────────────────────────

REGIME_META = {
    # (label_corto, label_largo, color_key)
    "RANGING":       ("RANGO",   "Lateralizando",   "blue"),
    "TRENDING_UP":   ("TEND ↑",  "Tendencia alcista","buy"),
    "TRENDING_DOWN": ("TEND ↓",  "Tendencia bajista","sell"),
    "VOLATILE":      ("VOLÁTIL", "Volátil / choppy", "warn"),
    "ACCUMULATION":  ("ACUM",    "Acumulación",      "teal"),
}


# ─── Dataclasses de salida ────────────────────────────────────────────────────

@dataclass
class RegimeSignal:
    regime:         str    # clave interna
    label:          str    # corto p.e. "RANGO"
    label_long:     str    # largo p.e. "Lateralizando"
    color_key:      str    # para HEX[]
    confidence:     int    # 0-100
    volatility_pct: float  # % promedio por sample de precio

    @property
    def is_ranging(self) -> bool:
        return self.regime == "RANGING"

    @property
    def is_trending(self) -> bool:
        return self.regime in ("TRENDING_UP", "TRENDING_DOWN")

    @property
    def is_accumulation(self) -> bool:
        return self.regime == "ACCUMULATION"


@dataclass
class OpportunitySignal:
    score:      int     # 0-100
    direction:  str     # "LONG" | "SHORT" | "NEUTRAL"
    color_key:  str     # "buy" | "sell" | "over"
    regime:     RegimeSignal
    reasons:    List[str] = field(default_factory=list)

    # Componentes para debug
    abs_pts:    int = 0   # contribución absorción (0-50)
    regime_pts: int = 0   # contribución régimen   (0-25)
    trend_pts:  int = 0   # contribución tendencia (0-15)
    liq_pts:    int = 0   # contribución liquidez  (0-10)

    @property
    def label(self) -> str:
        if self.direction == "NEUTRAL" or self.score < 20:
            return "──"
        lvl = "ALTA" if self.score >= 70 else ("MEDIA" if self.score >= 45 else "BAJA")
        return f"{lvl}  {self.score}"

    @property
    def is_actionable(self) -> bool:
        return self.score >= 45 and self.direction != "NEUTRAL"


NEUTRAL_REGIME = RegimeSignal(
    regime="RANGING", label="RANGO", label_long="Sin datos",
    color_key="over", confidence=0, volatility_pct=0.0,
)

NEUTRAL_OPP = OpportunitySignal(
    score=0, direction="NEUTRAL", color_key="over",
    regime=NEUTRAL_REGIME,
)


# ─── Clasificador de régimen ──────────────────────────────────────────────────

class RegimeClassifier:
    """
    Determina el régimen de mercado usando:
    · Volatilidad del precio (historial de 30s)
    · Consistencia del CVD por velas
    · Score de tendencia (TrendSignal)
    · Velocidad del OI
    """

    # Thresholds de volatilidad (% cambio por sample de 30s)
    VOL_HIGH  = 0.12    # > 0.12% por 30s = volátil
    VOL_LOW   = 0.025   # < 0.025% por 30s = tranquilo
    VOL_MICRO = 0.010   # < 0.010% = muy tranquilo → acumulación

    TREND_SCORE_MIN   = 58    # trend_score >= 58% para "trending"
    CVD_CONSIST_MIN   = 0.50  # consistencia CVD >= 50% para "trending"
    OI_VEL_ACUM       = 1.0   # OI creciendo > X% / min → acumulación

    PRICE_SAMPLES     = 20    # cuántos samples usar para volatilidad (~10 min)
    CVD_CANDLES       = 10    # cuántas velas CVD usar para consistencia

    def classify(
        self,
        state:  "MarketState",
        trend:  "TrendSignal",
    ) -> RegimeSignal:

        vol      = self._volatility(state)
        cvd_cons = self._cvd_consistency(state)
        oi_vel   = state.oi_velocity

        # ── VOLATILE: movimientos erráticos ───────────────────────────────────
        if vol > self.VOL_HIGH:
            conf = min(100, int((vol / self.VOL_HIGH - 1) * 60 + 50))
            return self._make(state, "VOLATILE", conf, vol)

        # ── TRENDING: trend fuerte + CVD direccional ──────────────────────────
        if trend.score >= self.TREND_SCORE_MIN and cvd_cons >= self.CVD_CONSIST_MIN:
            regime = (
                "TRENDING_UP"   if trend.direction == "ALCISTA"  else
                "TRENDING_DOWN" if trend.direction == "BAJISTA"  else
                "RANGING"
            )
            conf = min(100, int((trend.score - self.TREND_SCORE_MIN) / 42 * 70 + 30))
            return self._make(state, regime, conf, vol)

        # ── ACCUMULATION: muy quieto + OI entrando + CVD equilibrado ─────────
        if (
            vol < self.VOL_MICRO
            and cvd_cons < 0.25          # CVD muy alternado (vendedores/compradores equilibrados)
            and oi_vel > self.OI_VEL_ACUM
        ):
            conf = min(100, int((self.VOL_MICRO - vol) / self.VOL_MICRO * 50 + 40))
            return self._make(state, "ACCUMULATION", conf, vol)

        # ── RANGING: todo lo demás ─────────────────────────────────────────────
        conf = min(100, int((1 - vol / self.VOL_HIGH) * 60 + 20))
        return self._make(state, "RANGING", conf, vol)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _make(self, state, regime: str, conf: int, vol: float) -> RegimeSignal:
        label, label_long, color_key = REGIME_META[regime]
        return RegimeSignal(
            regime=regime,
            label=label,
            label_long=label_long,
            color_key=color_key,
            confidence=conf,
            volatility_pct=vol * 100,
        )

    def _volatility(self, state: "MarketState") -> float:
        """% promedio de cambio entre samples de precio consecutivos."""
        hist = list(state._price_history)[-self.PRICE_SAMPLES:]
        if len(hist) < 3:
            return 0.0
        prices = [p for _, p in hist]
        returns = [
            abs(prices[i] - prices[i - 1]) / prices[i - 1]
            for i in range(1, len(prices))
            if prices[i - 1] > 0
        ]
        return sum(returns) / len(returns) if returns else 0.0

    def _cvd_consistency(self, state: "MarketState") -> float:
        """
        Qué tan consistente es la dirección del CVD candle a candle.
        0 = alternando perfectamente | 1 = todas en la misma dirección.
        """
        candles = list(state.cvd_candles)[-self.CVD_CANDLES:]
        if len(candles) < 3:
            return 0.0
        bull = sum(1 for c in candles if c.delta > 0)
        bear = len(candles) - bull
        return abs(bull - bear) / len(candles)


# ─── Score de Oportunidad ─────────────────────────────────────────────────────

class OpportunityScorer:
    """
    Combina absorción + régimen + tendencia + liquidez en un score 0-100.
    Refleja «qué tan buena es esta entrada para la estrategia de absorción».
    """

    def score(
        self,
        absorption: "AbsorptionSignal",
        regime:     RegimeSignal,
        trend:      "TrendSignal",
        lmap:       "LiquidityMap",
    ) -> OpportunitySignal:

        if not absorption.is_signal:
            return OpportunitySignal(
                score=0, direction="NEUTRAL", color_key="over",
                regime=regime,
            )

        reasons: List[str] = []
        direction = "LONG" if absorption.side == "BUY" else "SHORT"

        # ── 1. Absorción (0-50 pts) ────────────────────────────────────────────
        abs_pts = int(absorption.score * 0.50)

        # ── 2. Régimen (0-25 pts) ─────────────────────────────────────────────
        regime_pts = 0
        if regime.regime == "RANGING":
            regime_pts = 22
            reasons.append("régimen RANGO → ideal para absorción")
        elif regime.regime == "ACCUMULATION":
            regime_pts = 25
            reasons.append("ACUMULACIÓN → máxima convicción")
        elif regime.regime == "TRENDING_UP" and absorption.side == "BUY":
            regime_pts = 14
            reasons.append("absorción CON tendencia alcista")
        elif regime.regime == "TRENDING_DOWN" and absorption.side == "SELL":
            regime_pts = 14
            reasons.append("absorción CON tendencia bajista")
        elif regime.regime == "TRENDING_UP" and absorption.side == "SELL":
            regime_pts = -8   # contra el trend → penalizar
            reasons.append("absorción CONTRA tendencia alcista")
        elif regime.regime == "TRENDING_DOWN" and absorption.side == "BUY":
            regime_pts = -8
            reasons.append("absorción CONTRA tendencia bajista")
        elif regime.regime == "VOLATILE":
            regime_pts = -10  # mercado caótico
            reasons.append("mercado VOLÁTIL → señal poco fiable")
        else:
            regime_pts = 10

        # ── 3. Alineación de tendencia (0-15 pts) ─────────────────────────────
        trend_pts = 0
        if trend.direction != "NEUTRAL" and trend.score >= 40:
            aligned = (
                (trend.direction == "ALCISTA" and absorption.side == "BUY") or
                (trend.direction == "BAJISTA" and absorption.side == "SELL")
            )
            if aligned:
                trend_pts = int(trend.score / 100 * 15)
                if trend_pts >= 8:
                    reasons.append(f"tendencia {trend.direction} alineada ({trend.score}%)")
            else:
                trend_pts = -int(trend.score / 100 * 8)

        # ── 4. Confluencia de liquidez (0-10 pts) ─────────────────────────────
        liq_pts = 0
        if lmap.at_hvn:
            liq_pts = 10
            reasons.append("precio EN HVN — nivel clave")
        elif lmap.at_lvn:
            liq_pts = 6
            reasons.append("precio en LVN — zona de velocidad")
        elif absorption.side == "BUY" and lmap.nearest_stop_below:
            d = abs(lmap.nearest_stop_below.dist_pct)
            if d < 0.5:
                liq_pts = 8
                reasons.append(f"stops abajo a {d:.2f}% — combustible alcista")
        elif absorption.side == "SELL" and lmap.nearest_stop_above:
            d = lmap.nearest_stop_above.dist_pct
            if d < 0.5:
                liq_pts = 8
                reasons.append(f"stops arriba a {d:.2f}% — combustible bajista")

        # ── Total ──────────────────────────────────────────────────────────────
        raw   = abs_pts + regime_pts + trend_pts + liq_pts
        total = max(0, min(100, raw))

        color_key = absorption.color_key if total >= 20 else "over"

        return OpportunitySignal(
            score=total,
            direction=direction,
            color_key=color_key,
            regime=regime,
            reasons=reasons[:3],
            abs_pts=abs_pts,
            regime_pts=max(0, regime_pts),
            trend_pts=max(0, trend_pts),
            liq_pts=liq_pts,
        )
