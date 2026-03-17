"""
core/absorption.py
──────────────────
Motor de detección de absorción — Phase 2.

Absorción = flujo agresivo y direccional + precio sin moverse en esa dirección.
Hay dos tipos:

  ABSORCIÓN COMPRADORA  → sellers hacen hit agresivo en bids pero precio aguanta
                          Grandes compradores absorben toda la oferta.
                          Señal ALCISTA: cuando los vendedores se agoten, el precio sube.

  ABSORCIÓN VENDEDORA   → buyers liftan asks agresivamente pero precio no sube
                          Grandes vendedores absorben toda la demanda.
                          Señal BAJISTA: cuando los compradores se agoten, el precio cae.

Score 0-100 compuesto de cuatro componentes:
  1. Divergencia CVD / Precio     (0-40 pts)  — señal más importante
  2. Eficiencia de flujo          (0-30 pts)  — volumen vs rango de precio
  3. Agresión vs reacción         (0-20 pts)  — trades recientes vs movimiento
  4. Estrés del orderbook         (0-10 pts)  — muro aguantando
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, List, Tuple

if TYPE_CHECKING:
    from streams.market import MarketState


# ─── Señal de absorción ───────────────────────────────────────────────────────

@dataclass
class AbsorptionSignal:
    score:     int         # 0-100 total
    side:      str         # "BUY" | "SELL" | "NEUTRAL"
    label:     str         # etiqueta corta para mostrar en UI
    color_key: str         # "buy" | "sell" | "over"  (clave de HEX en gtk_app)
    reasons:   List[str]   # hasta 3 razones explicativas

    # Componentes individuales (para debug / barra de detalle)
    cvd_div:    int = 0    # (0-40)
    flow_eff:   int = 0    # (0-30)
    aggression: int = 0    # (0-20)
    ob_stress:  int = 0    # (0-10)

    @property
    def label_score(self) -> str:
        return f"{self.label}  {self.score}/100"

    @property
    def is_signal(self) -> bool:
        return self.side != "NEUTRAL" and self.score >= 20


NEUTRAL_SIGNAL = AbsorptionSignal(
    score=0, side="NEUTRAL", label="Sin señal",
    color_key="over", reasons=[],
)


# ─── Utilidades ───────────────────────────────────────────────────────────────

def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _avg_price(trades: list, start: int, end: int) -> float:
    """Precio promedio de un slice de trades (por índice)."""
    sl = trades[start:end]
    if not sl:
        return 0.0
    return sum(t.price for t in sl) / len(sl)


# ─── Detector ─────────────────────────────────────────────────────────────────

class AbsorptionDetector:
    """
    Análisis de absorción puro — sin estado propio.
    Llamar .analyze(state) en cada ciclo de UI (100ms).
    """

    MIN_TRADES    = 50    # trades mínimos para análisis
    TRADE_WINDOW  = 200   # trades recientes a analizar
    CANDLE_WINDOW = 6     # últimas N velas CVD a considerar

    # Thresholds
    CVD_NORM_THRESHOLD   = 0.08   # ~54% dominancia para activar divergencia
    CVD_NORM_STRONG      = 0.25   # ~62% dominancia = fuerte
    PRICE_FLAT_PCT       = 0.03   # precio plano si mueve < 0.03%
    DOM_AGGRESSION       = 65.0   # % mínimo de una parte para "agresión"
    OB_STRONG_BID        = 0.65   # imbalance > 65% = muro de bids fuerte
    OB_STRONG_ASK        = 0.35   # imbalance < 35% = muro de asks fuerte

    def analyze(self, state: "MarketState") -> AbsorptionSignal:
        if not state.connected:
            return NEUTRAL_SIGNAL

        trades  = list(state.trades)[-self.TRADE_WINDOW:]
        candles = list(state.cvd_candles)[-self.CANDLE_WINDOW:]

        if len(trades) < self.MIN_TRADES or len(candles) < 2:
            return NEUTRAL_SIGNAL

        reasons: List[str] = []

        # ── Componentes ───────────────────────────────────────────────────────
        c1, side1, r1 = self._cvd_divergence(trades, candles)
        c2, side2, r2 = self._flow_efficiency(trades)
        c3, side3, r3 = self._aggression_vs_price(trades)
        c4, side4, r4 = self._ob_stress(state)

        reasons += r1 + r2 + r3 + r4

        # ── Votación ponderada por el lado ────────────────────────────────────
        votes: dict[str, int] = {"BUY": 0, "SELL": 0}
        for side, pts in [(side1, c1), (side2, c2), (side3, c3), (side4, c4)]:
            if side in votes:
                votes[side] += pts

        total_votes = votes["BUY"] + votes["SELL"]
        if total_votes == 0:
            return NEUTRAL_SIGNAL

        winning_side = "BUY" if votes["BUY"] >= votes["SELL"] else "SELL"
        winning_pct  = votes[winning_side] / total_votes

        # Señales contradictorias → sin señal clara
        if winning_pct < 0.60:
            return NEUTRAL_SIGNAL

        score = min(100, c1 + c2 + c3 + c4)
        if score < 20:
            return NEUTRAL_SIGNAL

        label     = "ABSORCIÓN COMPRADORA" if winning_side == "BUY" else "ABSORCIÓN VENDEDORA"
        color_key = "buy" if winning_side == "BUY" else "sell"

        return AbsorptionSignal(
            score=score,
            side=winning_side,
            label=label,
            color_key=color_key,
            reasons=reasons[:3],
            cvd_div=c1,
            flow_eff=c2,
            aggression=c3,
            ob_stress=c4,
        )

    # ── 1. Divergencia CVD / Precio ───────────────────────────────────────────

    def _cvd_divergence(
        self, trades: list, candles: list
    ) -> Tuple[int, str, List[str]]:
        """
        Divergencia entre la tendencia del CVD acumulado y el movimiento del precio.
        Máx 40 puntos.

        · CVD bajando (más sells) + precio plano/sube  → BUY absorption
        · CVD subiendo (más buys) + precio plano/baja  → SELL absorption
        """
        reasons: List[str] = []
        n = max(5, len(trades) // 10)

        p_early = _avg_price(trades, 0, n)
        p_late  = _avg_price(trades, -n, len(trades))
        if p_early <= 0:
            return 0, "NEUTRAL", reasons

        price_pct = (p_late - p_early) / p_early * 100   # % de cambio

        # CVD normalizado [-1, +1] sobre las últimas N velas
        cvd_total = sum(c.delta for c in candles)
        cvd_vol   = sum(c.total for c in candles)
        if cvd_vol == 0:
            return 0, "NEUTRAL", reasons
        cvd_norm = cvd_total / cvd_vol                    # [-1, +1]

        if abs(cvd_norm) < self.CVD_NORM_THRESHOLD:
            return 0, "NEUTRAL", reasons                  # flujo demasiado neutral

        side      = "NEUTRAL"
        magnitude = 0.0

        if cvd_norm < -self.CVD_NORM_THRESHOLD and price_pct >= -self.PRICE_FLAT_PCT:
            # Sells dominantes pero precio no cae → BUY absorption
            side          = "BUY"
            cvd_str       = _clamp(abs(cvd_norm) / self.CVD_NORM_STRONG, 0, 1)
            price_resist  = _clamp((self.PRICE_FLAT_PCT - price_pct) / 0.10, 0, 1)
            magnitude     = cvd_str * 0.65 + price_resist * 0.35
            reasons.append(
                f"CVD↓ {cvd_norm:+.2f} · precio {price_pct:+.3f}%"
            )

        elif cvd_norm > self.CVD_NORM_THRESHOLD and price_pct <= self.PRICE_FLAT_PCT:
            # Buys dominantes pero precio no sube → SELL absorption
            side          = "SELL"
            cvd_str       = _clamp(abs(cvd_norm) / self.CVD_NORM_STRONG, 0, 1)
            price_resist  = _clamp((self.PRICE_FLAT_PCT + price_pct) / 0.10, 0, 1)
            magnitude     = cvd_str * 0.65 + price_resist * 0.35
            reasons.append(
                f"CVD↑ {cvd_norm:+.2f} · precio {price_pct:+.3f}%"
            )

        score = int(_clamp(magnitude * 40, 0, 40))
        return score, side, reasons

    # ── 2. Eficiencia de flujo ─────────────────────────────────────────────────

    def _flow_efficiency(self, trades: list) -> Tuple[int, str, List[str]]:
        """
        Mucho volumen + poco rango de precio = absorción.
        Máx 30 puntos.
        """
        reasons: List[str] = []
        if len(trades) < 10:
            return 0, "NEUTRAL", reasons

        prices    = [t.price for t in trades]
        hi, lo    = max(prices), min(prices)
        mid       = (hi + lo) / 2 if (hi + lo) > 0 else 1.0
        range_pct = (hi - lo) / mid * 100

        buy_vol  = sum(t.qty for t in trades if t.side == "Buy")
        sell_vol = sum(t.qty for t in trades if t.side == "Sell")
        total    = buy_vol + sell_vol
        if total == 0:
            return 0, "NEUTRAL", reasons

        buy_pct = buy_vol / total * 100

        # Necesitamos direccionalidad mínima
        if buy_pct >= 50:
            dom_pct = buy_pct
            side    = "SELL"   # buys dominan sin mover precio → SELL absorbe
        else:
            dom_pct = 100 - buy_pct
            side    = "BUY"

        if dom_pct < 55:
            return 0, "NEUTRAL", reasons

        # Score: cuánto más dominante el flujo × cuánto más estrecho el rango
        # Vol_density sube cuando la dominancia es alta y el rango es pequeño
        range_floor = max(range_pct, 0.005)
        raw   = ((dom_pct - 50) / 50) * (0.05 / range_floor)
        score = int(_clamp(raw * 30, 0, 30))

        if score >= 8:
            reasons.append(
                f"flujo {dom_pct:.0f}% dom · rango {range_pct:.3f}%"
            )

        return score, side, reasons

    # ── 3. Agresión vs reacción de precio ─────────────────────────────────────

    def _aggression_vs_price(self, trades: list) -> Tuple[int, str, List[str]]:
        """
        Un lado es muy agresivo en los trades recientes pero el precio no reacciona.
        Máx 20 puntos.
        """
        reasons: List[str] = []
        if len(trades) < 20:
            return 0, "NEUTRAL", reasons

        # Ventana: último tercio de los trades (más recientes)
        w      = max(20, len(trades) // 3)
        recent = trades[-w:]

        buy_vol  = sum(t.qty for t in recent if t.side == "Buy")
        sell_vol = sum(t.qty for t in recent if t.side == "Sell")
        total    = buy_vol + sell_vol
        if total == 0:
            return 0, "NEUTRAL", reasons

        buy_pct = buy_vol / total * 100

        n5      = max(3, len(recent) // 8)
        p_start = _avg_price(recent, 0, n5)
        p_end   = _avg_price(recent, -n5, len(recent))
        p_move  = (p_end - p_start) / p_start * 100 if p_start > 0 else 0.0

        side  = "NEUTRAL"
        score = 0

        if buy_pct > self.DOM_AGGRESSION and p_move < self.PRICE_FLAT_PCT:
            # Compradores muy agresivos + precio no sube → SELL absorption
            side      = "SELL"
            intensity = _clamp((buy_pct - self.DOM_AGGRESSION) / (100 - self.DOM_AGGRESSION), 0, 1)
            score     = int(intensity * 20)
            if score >= 6:
                reasons.append(
                    f"agresión compradora {buy_pct:.0f}% sin reacción"
                )

        elif (100 - buy_pct) > self.DOM_AGGRESSION and p_move > -self.PRICE_FLAT_PCT:
            # Vendedores muy agresivos + precio no baja → BUY absorption
            side      = "BUY"
            intensity = _clamp(((100 - buy_pct) - self.DOM_AGGRESSION) / (100 - self.DOM_AGGRESSION), 0, 1)
            score     = int(intensity * 20)
            if score >= 6:
                reasons.append(
                    f"agresión vendedora {100-buy_pct:.0f}% sin reacción"
                )

        return score, side, reasons

    # ── 4. Estrés del orderbook ────────────────────────────────────────────────

    def _ob_stress(self, state: "MarketState") -> Tuple[int, str, List[str]]:
        """
        Muro del orderbook siendo defendido bajo presión contraria.
        Máx 10 puntos.
        """
        reasons: List[str] = []
        imb = state.orderbook.imbalance   # 0-1, > 0.5 = bids dominan

        if imb > self.OB_STRONG_BID:
            side  = "BUY"
            score = int(_clamp((imb - self.OB_STRONG_BID) / (1.0 - self.OB_STRONG_BID) * 10, 0, 10))
            if score >= 4:
                reasons.append(f"muro bid {imb*100:.0f}% imbalance")
            return score, side, reasons

        elif imb < self.OB_STRONG_ASK:
            side  = "SELL"
            score = int(_clamp((self.OB_STRONG_ASK - imb) / self.OB_STRONG_ASK * 10, 0, 10))
            if score >= 4:
                reasons.append(f"muro ask {(1-imb)*100:.0f}% imbalance")
            return score, side, reasons

        return 0, "NEUTRAL", reasons
