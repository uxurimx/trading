"""
core/trend.py
─────────────
Tendencia multi-timeframe — Phase 3 (extensión).

Compara el precio actual contra precios históricos muestreados cada 30s.
Cada timeframe vota: alcista (+1) | bajista (-1) | neutral (0).

El score es una suma ponderada (Fibonacci: TFs altos pesan más):

  Timeframes:  1m    3m    5m   30m    1h    6h
  Pesos:        1     2     3     8    13    21   → total 48

Score 0-100:
  48/48 = todos alcistas  → 100%  (ALCISTA FUERTE)
  24/48 = mitad alcista   →  50%  (ALCISTA DÉBIL)
  0     = todos neutral   →   0%  (SIN TENDENCIA)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, List, Tuple

if TYPE_CHECKING:
    from streams.market import MarketState


# ─── Config ───────────────────────────────────────────────────────────────────

TIMEFRAMES: List[Tuple[str, int, int]] = [
    # (etiqueta, segundos, peso)
    ("1m",   60,      1),
    ("3m",   180,     2),
    ("5m",   300,     3),
    ("30m",  1800,    8),
    ("1h",   3600,   13),
    ("6h",   21600,  21),
]

TOTAL_WEIGHT  = sum(w for _, _, w in TIMEFRAMES)   # 48
THRESHOLD_PCT = 0.03    # % mínimo de movimiento para considerar direccional
SAMPLE_SECS   = 30.0    # cada cuántos segundos muestreamos el precio en MarketState


# ─── Dataclasses ──────────────────────────────────────────────────────────────

@dataclass
class TFTrend:
    label:    str
    seconds:  int
    weight:   int
    direction: int     # +1 alcista | -1 bajista | 0 neutral
    pct:      float    # cambio porcentual real (+ = subió)
    has_data: bool     # True si hay suficiente historial

    @property
    def glyph(self) -> str:
        if not self.has_data: return "·"
        if self.direction > 0: return "▲"
        if self.direction < 0: return "▼"
        return "─"

    @property
    def color_key(self) -> str:
        if not self.has_data or self.direction == 0: return "over"
        return "buy" if self.direction > 0 else "sell"


@dataclass
class TrendSignal:
    timeframes: List[TFTrend]
    direction:  str    # "ALCISTA" | "BAJISTA" | "NEUTRAL"
    score:      int    # 0-100 (intensidad de la alineación)
    color_key:  str    # "buy" | "sell" | "over"
    aligned:    int    # cuántos TFs están alineados con la dirección dominante
    total:      int    # cuántos TFs tienen datos

    @property
    def label(self) -> str:
        if self.direction == "NEUTRAL":
            return "SIN TENDENCIA"
        strength = (
            "FUERTE"   if self.score >= 75 else
            "MODERADA" if self.score >= 40 else
            "DÉBIL"
        )
        return f"{self.direction} {strength}"


NEUTRAL_TREND = TrendSignal(
    timeframes=[], direction="NEUTRAL",
    score=0, color_key="over", aligned=0, total=0,
)


# ─── Analizador ───────────────────────────────────────────────────────────────

class TrendAnalyzer:
    """
    Calcula la tendencia por timeframe usando el historial de precios muestreado.
    Sin estado propio — llamar .analyze(state) cada ciclo de UI.
    """

    def analyze(self, state: "MarketState") -> TrendSignal:
        price = state.ticker.last_price
        if price <= 0:
            return NEUTRAL_TREND

        history = list(state._price_history)   # [(ts, price), ...]
        if not history:
            return NEUTRAL_TREND

        now     = history[-1][0]   # timestamp del último sample
        tfs: List[TFTrend] = []

        for label, seconds, weight in TIMEFRAMES:
            tf = self._eval_tf(label, seconds, weight, price, history, now)
            tfs.append(tf)

        # ── Score ponderado ────────────────────────────────────────────────────
        bull_w = sum(tf.weight for tf in tfs if tf.direction > 0)
        bear_w = sum(tf.weight for tf in tfs if tf.direction < 0)
        data_w = sum(tf.weight for tf in tfs if tf.has_data)

        if data_w == 0:
            return NEUTRAL_TREND

        if bull_w > bear_w:
            direction = "ALCISTA"
            score     = int(bull_w / data_w * 100)
            color_key = "buy"
            aligned   = sum(1 for tf in tfs if tf.direction > 0 and tf.has_data)
        elif bear_w > bull_w:
            direction = "BAJISTA"
            score     = int(bear_w / data_w * 100)
            color_key = "sell"
            aligned   = sum(1 for tf in tfs if tf.direction < 0 and tf.has_data)
        else:
            direction = "NEUTRAL"
            score     = 0
            color_key = "over"
            aligned   = 0

        total = sum(1 for tf in tfs if tf.has_data)

        return TrendSignal(
            timeframes=tfs,
            direction=direction,
            score=score,
            color_key=color_key,
            aligned=aligned,
            total=total,
        )

    def _eval_tf(
        self,
        label: str,
        seconds: int,
        weight: int,
        current_price: float,
        history: List[Tuple[float, float]],
        now: float,
    ) -> TFTrend:
        target_ts = now - seconds
        # Buscar el sample más cercano al target_ts
        past_price = None
        best_diff  = float("inf")
        for ts, p in history:
            diff = abs(ts - target_ts)
            if diff < best_diff:
                best_diff = diff
                past_price = p

        # Toleramos hasta 50% de desfase (si pedimos 60s, aceptamos entre 30-90s)
        max_diff = seconds * 0.5
        has_data = past_price is not None and best_diff <= max_diff

        if not has_data or past_price is None or past_price <= 0:
            return TFTrend(label, seconds, weight, 0, 0.0, False)

        pct = (current_price - past_price) / past_price * 100

        if pct > THRESHOLD_PCT:
            direction = +1
        elif pct < -THRESHOLD_PCT:
            direction = -1
        else:
            direction = 0

        return TFTrend(label, seconds, weight, direction, pct, True)
