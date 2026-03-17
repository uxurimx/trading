"""
core/technicals.py
──────────────────
Análisis técnico clásico sobre klines fetched de Bybit REST.

Módulos:
  · TechIndicators  — EMA, RSI, ATR, soporte/resistencia
  · TradeContext    — evalúa setup de una posición abierta
  · TechSignal      — dataclass resultado final

Diseñado para ser llamado cada ~60 s con klines REST (no tiempo real).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from streams.account import Position


# ─── Indicadores ──────────────────────────────────────────────────────────────

class TechIndicators:

    @staticmethod
    def ema(closes: List[float], period: int) -> float:
        """EMA sobre lista de cierres en orden cronológico (viejo → reciente)."""
        if len(closes) < period:
            return closes[-1] if closes else 0.0
        k   = 2.0 / (period + 1)
        val = sum(closes[:period]) / period
        for c in closes[period:]:
            val = c * k + val * (1 - k)
        return val

    @staticmethod
    def rsi(closes: List[float], period: int = 14) -> float:
        if len(closes) < period + 2:
            return 50.0
        gains, losses = [], []
        for i in range(1, len(closes)):
            d = closes[i] - closes[i - 1]
            gains.append(max(0.0, d))
            losses.append(max(0.0, -d))
        ag = sum(gains[-period:]) / period
        al = sum(losses[-period:]) / period
        return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)

    @staticmethod
    def atr(klines_recent_first: List[List], period: int = 14) -> float:
        """
        Bybit kline format (recent first):
          [0]=startTime [1]=open [2]=high [3]=low [4]=close [5]=vol
        """
        if len(klines_recent_first) < period + 1:
            return 0.0
        trs = []
        lim = min(len(klines_recent_first) - 1, period * 2)
        for i in range(lim):
            h      = float(klines_recent_first[i][2])
            l      = float(klines_recent_first[i][3])
            c_prev = float(klines_recent_first[i + 1][4])
            trs.append(max(h - l, abs(h - c_prev), abs(l - c_prev)))
        return sum(trs[:period]) / period

    @staticmethod
    def sr(klines_recent_first: List[List], n: int = 20):
        """Soporte y resistencia del rango de n velas. Retorna (sup, res)."""
        if not klines_recent_first:
            return 0.0, 0.0
        highs = [float(k[2]) for k in klines_recent_first[:n]]
        lows  = [float(k[3]) for k in klines_recent_first[:n]]
        return min(lows), max(highs)

    @classmethod
    def closes(cls, klines_recent_first: List[List]) -> List[float]:
        """Cierres en orden cronológico (viejo → reciente)."""
        return [float(k[4]) for k in reversed(klines_recent_first)]


# ─── Resultado ────────────────────────────────────────────────────────────────

@dataclass
class TechSignal:
    # Indicadores
    ema9_15m:   float = 0.0
    ema21_15m:  float = 0.0
    ema50_1h:   float = 0.0
    ema200_1h:  float = 0.0
    rsi_15m:    float = 50.0
    rsi_1h:     float = 50.0
    atr_15m:    float = 0.0
    support:    float = 0.0   # 20 velas 15m
    resistance: float = 0.0

    # Evaluación del trade
    score:        int         = 50       # 0-100
    score_color:  str         = "over"   # "buy" | "sell" | "warn" | "over"
    verdict:      str         = "──"
    good:         List[str]   = field(default_factory=list)
    risks:        List[str]   = field(default_factory=list)
    tips:         List[str]   = field(default_factory=list)

    # Flags útiles para el UI
    ema15m_bull:  bool        = False
    ema1h_bull:   bool        = False    # precio > EMA50
    at_ema200:    bool        = False    # precio dentro de 0.5% de EMA200 1h
    rr_ratio:     float       = 0.0

    has_data: bool = False


NEUTRAL_TECH = TechSignal()


# ─── Contexto de Trade ────────────────────────────────────────────────────────

class TradeContextAnalyzer:
    """
    Evalúa la calidad técnica de una posición abierta usando klines REST.
    Devuelve TechSignal con score, flags y recomendaciones.
    """

    def analyze(
        self,
        pos:     "Position",
        klines_15m: List[List],
        klines_1h:  List[List],
    ) -> TechSignal:
        if not klines_15m or not klines_1h:
            return NEUTRAL_TECH

        ti = TechIndicators
        c15 = ti.closes(klines_15m)
        c1h = ti.closes(klines_1h)

        ema9   = ti.ema(c15, 9)
        ema21  = ti.ema(c15, 21)
        ema50  = ti.ema(c1h, 50)
        ema200 = ti.ema(c1h, 200)
        rsi15  = ti.rsi(c15, 14)
        rsi1h  = ti.rsi(c1h, 14)
        atr15  = ti.atr(klines_15m, 14)
        sup, res = ti.sr(klines_15m, 20)

        mark    = pos.mark_price if pos.mark_price > 0 else pos.entry_price
        is_long = pos.is_long

        ema15_bull  = ema9 > ema21
        ema1h_bull  = mark > ema50
        at_ema200   = ema200 > 0 and abs(mark - ema200) / ema200 < 0.005  # dentro del 0.5%

        # ── R:R ──────────────────────────────────────────────────────────────
        rr = 0.0
        if pos.stop_loss > 0 and pos.take_profit > 0 and pos.entry_price > 0:
            sl_d = abs(pos.stop_loss   - pos.entry_price)
            tp_d = abs(pos.take_profit - pos.entry_price)
            rr   = tp_d / sl_d if sl_d > 0 else 0.0

        # ── Evaluación ───────────────────────────────────────────────────────
        good: List[str] = []
        risks: List[str] = []
        tips: List[str] = []

        # EMA alignment
        if is_long and ema15_bull:
            good.append("EMA 9/21 (15m) alcistas — impulso a favor")
        elif is_long and not ema15_bull:
            risks.append("EMAs 15m bajistas — pullback activo")

        if is_long and ema1h_bull:
            good.append("Precio sobre EMA50 (1h) — tendencia sana")
        elif is_long and not ema1h_bull:
            risks.append("Precio bajo EMA50 1h — presión bajista")

        if at_ema200:
            if is_long:
                good.append(f"Precio en EMA200 1h ({ema200:.5f}) — soporte dinámico clave")
            else:
                risks.append(f"Precio en EMA200 1h — resistencia dinámica")

        # RSI
        if is_long:
            if rsi15 > 70:
                risks.append(f"RSI 15m sobrecomprado ({rsi15:.1f})")
            elif rsi15 < 40:
                good.append(f"RSI 15m bajo ({rsi15:.1f}) — margen de rebote")
            if rsi1h > 68:
                risks.append(f"RSI 1h elevado ({rsi1h:.1f}) — posible agotamiento")
        else:
            if rsi15 < 30:
                risks.append(f"RSI 15m sobrevendido ({rsi15:.1f}) — rebote posible")
            elif rsi15 > 60:
                good.append(f"RSI 15m elevado ({rsi15:.1f}) — margen de caída")

        # SL / TP
        if pos.stop_loss <= 0:
            risks.append("SIN stop loss — riesgo no controlado")
            tips.append(f"Coloca SL en ~{pos.entry_price - atr15 * 1.5:.5f} (1.5x ATR)" if is_long else
                        f"Coloca SL en ~{pos.entry_price + atr15 * 1.5:.5f} (1.5x ATR)")
        else:
            sl_dist_pct = abs(pos.stop_loss - pos.entry_price) / pos.entry_price * 100
            if sl_dist_pct < 0.3:
                risks.append(f"SL muy ajustado ({sl_dist_pct:.2f}%) — riesgo de stop-hunt")
            elif sl_dist_pct > 5.0:
                risks.append(f"SL alejado ({sl_dist_pct:.2f}%) — pérdida potencial alta")
            else:
                good.append(f"SL bien colocado ({sl_dist_pct:.2f}%)")

        if rr > 0:
            if rr >= 2.0:
                good.append(f"R:R {rr:.1f}:1 — favorece la posición")
            elif rr < 1.5:
                risks.append(f"R:R {rr:.1f}:1 — relación desfavorable")
            else:
                tips.append(f"R:R {rr:.1f}:1 — considera mover TP para >2:1")
        elif pos.take_profit <= 0:
            tips.append(f"Define TP. Sugerido: {pos.entry_price + atr15 * 3:.5f} (3x ATR)" if is_long else
                        f"Define TP. Sugerido: {pos.entry_price - atr15 * 3:.5f} (3x ATR)")

        # ATR tips
        if atr15 > 0:
            sl_sug = pos.entry_price - atr15 * 1.5 if is_long else pos.entry_price + atr15 * 1.5
            tp_sug = pos.entry_price + atr15 * 3.0 if is_long else pos.entry_price - atr15 * 3.0
            if pos.stop_loss <= 0 or pos.take_profit <= 0:
                tips.append(f"ATR: SL={sl_sug:.5f}  TP={tp_sug:.5f}")

        # ── Score ─────────────────────────────────────────────────────────────
        pts = 50 + len(good) * 15 - len(risks) * 12
        score = max(0, min(100, pts))

        if score >= 65:
            color, verdict = "buy",  "Setup SÓLIDO"
        elif score >= 42:
            color, verdict = "warn", "Setup ACEPTABLE"
        else:
            color, verdict = "sell", "Setup DÉBIL"

        return TechSignal(
            ema9_15m=ema9, ema21_15m=ema21,
            ema50_1h=ema50, ema200_1h=ema200,
            rsi_15m=rsi15, rsi_1h=rsi1h,
            atr_15m=atr15,
            support=sup, resistance=res,
            score=score, score_color=color,
            verdict=verdict,
            good=good[:3], risks=risks[:3], tips=tips[:2],
            ema15m_bull=ema15_bull,
            ema1h_bull=ema1h_bull,
            at_ema200=at_ema200,
            rr_ratio=rr,
            has_data=True,
        )
