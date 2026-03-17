"""
core/liquidity.py
─────────────────
Mapa de liquidez — Phase 3.

La liquidez latente es el combustible del precio:
  · HVN  — High Volume Node: precio magnético, el mercado vuelve a él
  · LVN  — Low Volume Node:  precio "fino", cruza rápido (sin soporte)
  · EQ·H — Equal Highs:      stops de compradores acumulados justo encima
  · EQ·L — Equal Lows:       stops de vendedores acumulados justo debajo
  · ○    — Round Number:     niveles psicológicos (1.50, 2.00, 50,000…)

Cuando la absorción (Fase 2) coincide con un nivel clave de liquidez,
la señal tiene mucha más convicción:

  Absorción compradora AT equal lows → los vendedores que pusieron
  stops ahí no se están activando. Cuando se agoten, los stops
  se convierten en combustible alcista.

  Absorción vendedora AT equal highs → mismo razonamiento, bajista.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from streams.market import MarketState


# ─── Tipos de niveles ─────────────────────────────────────────────────────────

LEVEL_COLORS: dict[str, str] = {
    "HVN":   "teal",   # magnético — precio regresa
    "LVN":   "warn",   # fino — precio viaja rápido
    "EQ_H":  "sell",   # equal highs — sell-side liquidity (stops arriba)
    "EQ_L":  "buy",    # equal lows  — buy-side liquidity  (stops abajo)
    "ROUND": "purple", # número redondo
}

LEVEL_LABELS: dict[str, str] = {
    "HVN":   "HVN  ",
    "LVN":   "LVN  ",
    "EQ_H":  "STOP↑",
    "EQ_L":  "STOP↓",
    "ROUND": "○    ",
}


# ─── VolumeProfile ────────────────────────────────────────────────────────────

class VolumeProfile:
    """
    Acumula volumen negociado por nivel de precio (sesión completa).
    Bucket size adaptativo según la magnitud del precio.
    """

    __slots__ = ("_data",)

    def __init__(self) -> None:
        self._data: Dict[float, float] = {}   # bucket_price → volumen

    @staticmethod
    def bucket_size(price: float) -> float:
        if price >= 50_000: return 50.0
        if price >= 10_000: return 10.0
        if price >= 5_000:  return 5.0
        if price >= 1_000:  return 1.0
        if price >= 100:    return 0.1
        if price >= 10:     return 0.01
        if price >= 1:      return 0.001
        return round(price * 0.0005, 8)

    def add(self, price: float, qty: float) -> None:
        if price <= 0 or qty <= 0:
            return
        bs     = self.bucket_size(price)
        bucket = round(round(price / bs) * bs, 10)
        self._data[bucket] = self._data.get(bucket, 0.0) + qty

    def near(self, price: float, window_pct: float = 0.04) -> List[Tuple[float, float]]:
        """Devuelve [(precio, volumen)] dentro del window_pct% del precio, ordenados."""
        if price <= 0 or not self._data:
            return []
        lo = price * (1 - window_pct)
        hi = price * (1 + window_pct)
        return sorted(
            ((p, v) for p, v in self._data.items() if lo <= p <= hi),
            key=lambda x: x[0],
        )

    def reset(self) -> None:
        self._data.clear()


# ─── Dataclasses de salida ────────────────────────────────────────────────────

@dataclass
class LiquidityLevel:
    price:       float
    level_type:  str    # "HVN" | "LVN" | "EQ_H" | "EQ_L" | "ROUND"
    dist_pct:    float  # % desde precio actual  (+ = arriba, - = abajo)
    volume:      float = 0.0   # para HVN/LVN
    vol_pct:     float = 0.0   # % del max vol (0-1), para barra visual
    strength:    int   = 0     # 0-100
    count:       int   = 1     # cuántas veces fue testado (equal highs/lows)

    @property
    def is_above(self) -> bool:
        return self.dist_pct > 0

    @property
    def color_key(self) -> str:
        return LEVEL_COLORS.get(self.level_type, "sub")

    @property
    def label(self) -> str:
        return LEVEL_LABELS.get(self.level_type, "     ")


@dataclass
class LiquidityMap:
    levels:  List[LiquidityLevel]  # todos los niveles ordenados por precio desc (arriba primero)
    above:   List[LiquidityLevel]  # niveles sobre el precio (desc)
    below:   List[LiquidityLevel]  # niveles bajo el precio (asc → más cercano primero)
    price:   float
    context: str                   # "EN HVN" | "EN LVN" | "ENTRE NIVELES" | etc.
    nearest_stop_above: Optional[LiquidityLevel] = None
    nearest_stop_below: Optional[LiquidityLevel] = None
    at_hvn: bool = False
    at_lvn: bool = False

    @property
    def has_data(self) -> bool:
        return bool(self.above or self.below)


_EMPTY_MAP = LiquidityMap(
    levels=[], above=[], below=[],
    price=0, context="sin datos",
)


# ─── Analizador ───────────────────────────────────────────────────────────────

class LiquidityAnalyzer:
    """
    Construye el mapa de liquidez a partir del MarketState.
    Sin estado propio — análisis puro.
    """

    VP_WINDOW      = 0.04    # ±4% alrededor del precio para VP
    ROUND_WINDOW   = 0.025   # ±2.5% para números redondos
    SWING_LOOKBACK = 4       # velas a cada lado para detectar swing
    SWING_INTERVAL = 30.0    # segundos por barra para swing detection
    EQ_TOLERANCE   = 0.12    # % para agrupar swings como "equal"
    HVN_RATIO      = 2.0     # vol > 2× mediana → HVN
    LVN_RATIO      = 0.4     # vol < 0.4× mediana → LVN
    AT_LEVEL_PCT   = 0.05    # precio "en" nivel si está a < 0.05%
    MAX_LEVELS     = 5       # niveles a mostrar arriba y abajo

    def analyze(self, state: "MarketState") -> LiquidityMap:
        price = state.ticker.last_price
        if price <= 0:
            return _EMPTY_MAP

        all_levels: List[LiquidityLevel] = []

        # ── 1. Volume Profile (HVN / LVN) ─────────────────────────────────────
        all_levels += self._vp_levels(state.volume_profile, price)

        # ── 2. Equal Highs / Equal Lows (swing detection) ─────────────────────
        samples = list(state._price_samples)
        if len(samples) >= 40:
            all_levels += self._swing_levels(samples, price)

        # ── 3. Round Numbers ───────────────────────────────────────────────────
        all_levels += self._round_levels(price)

        # ── Separar arriba / abajo y deduplicar ───────────────────────────────
        above = sorted(
            [lv for lv in all_levels if lv.dist_pct > self.AT_LEVEL_PCT],
            key=lambda x: x.dist_pct,        # más cercano primero
        )[:self.MAX_LEVELS]

        below = sorted(
            [lv for lv in all_levels if lv.dist_pct < -self.AT_LEVEL_PCT],
            key=lambda x: -x.dist_pct,       # más cercano primero (menos negativo)
        )[:self.MAX_LEVELS]

        # Niveles justo EN el precio
        at = [lv for lv in all_levels if abs(lv.dist_pct) <= self.AT_LEVEL_PCT]

        # ── Contexto ──────────────────────────────────────────────────────────
        at_hvn = any(lv.level_type == "HVN" for lv in at)
        at_lvn = any(lv.level_type == "LVN" for lv in at)

        if at_hvn:
            ctx = "EN HVN — resistencia/soporte fuerte"
        elif at_lvn:
            ctx = "EN LVN — zona de velocidad"
        elif above and below:
            d_up  = above[0].dist_pct
            d_dn  = abs(below[0].dist_pct)
            ratio = d_dn / d_up if d_up > 0 else 1.0
            if ratio > 1.8:
                ctx = "MÁS ESPACIO ARRIBA"
            elif ratio < 0.55:
                ctx = "MÁS ESPACIO ABAJO"
            else:
                ctx = "ENTRE NIVELES"
        else:
            ctx = "sin referencia cercana"

        nearest_stop_above = next(
            (lv for lv in above if lv.level_type in ("EQ_H", "ROUND")), None
        )
        nearest_stop_below = next(
            (lv for lv in below if lv.level_type in ("EQ_L", "ROUND")), None
        )

        return LiquidityMap(
            levels=sorted(all_levels, key=lambda x: -x.price),
            above=above,
            below=below,
            price=price,
            context=ctx,
            nearest_stop_above=nearest_stop_above,
            nearest_stop_below=nearest_stop_below,
            at_hvn=at_hvn,
            at_lvn=at_lvn,
        )

    # ── Volume Profile ─────────────────────────────────────────────────────────

    def _vp_levels(self, vp: VolumeProfile, price: float) -> List[LiquidityLevel]:
        snapshot = vp.near(price, self.VP_WINDOW)
        if len(snapshot) < 4:
            return []

        volumes  = [v for _, v in snapshot]
        max_vol  = max(volumes)
        median_v = sorted(volumes)[len(volumes) // 2]
        if median_v == 0:
            return []

        levels: List[LiquidityLevel] = []
        for p, v in snapshot:
            dist = (p - price) / price * 100
            if abs(dist) < 0.005:       # justo en el precio → saltamos
                continue

            vol_pct = v / max_vol

            if v >= median_v * self.HVN_RATIO:
                ltype    = "HVN"
                strength = min(100, int(v / median_v / self.HVN_RATIO * 60) + 30)
            elif v <= median_v * self.LVN_RATIO:
                ltype    = "LVN"
                strength = min(100, int((1 - v / (median_v * self.LVN_RATIO)) * 50) + 20)
            else:
                continue   # nivel normal, sin interés especial

            levels.append(LiquidityLevel(
                price=p, level_type=ltype, dist_pct=dist,
                volume=v, vol_pct=vol_pct, strength=strength,
            ))

        return levels

    # ── Swing Highs / Lows → Equal Highs / Equal Lows ─────────────────────────

    def _swing_levels(
        self, samples: List[Tuple[float, float]], price: float
    ) -> List[LiquidityLevel]:
        """
        1. Resamplea trades a barras de SWING_INTERVAL segundos
        2. Detecta swing highs / lows con lookback SWING_LOOKBACK
        3. Agrupa niveles cercanos (equal highs/lows)
        """
        bars = self._resample(samples, self.SWING_INTERVAL)
        if len(bars) < self.SWING_LOOKBACK * 2 + 2:
            return []

        swing_hi = self._find_swings([b[1] for b in bars], high=True)
        swing_lo = self._find_swings([b[2] for b in bars], high=False)

        levels: List[LiquidityLevel] = []

        for p, count in self._cluster(swing_hi, self.EQ_TOLERANCE):
            if count < 2:          # necesita al menos 2 toques
                continue
            dist = (p - price) / price * 100
            if abs(dist) < 0.02:   # demasiado cerca del precio
                continue
            levels.append(LiquidityLevel(
                price=p,
                level_type="EQ_H" if p > price else "EQ_L",
                dist_pct=dist,
                strength=min(100, 30 + count * 20),
                count=count,
            ))

        for p, count in self._cluster(swing_lo, self.EQ_TOLERANCE):
            if count < 2:
                continue
            dist = (p - price) / price * 100
            if abs(dist) < 0.02:
                continue
            levels.append(LiquidityLevel(
                price=p,
                level_type="EQ_L" if p < price else "EQ_H",
                dist_pct=dist,
                strength=min(100, 30 + count * 20),
                count=count,
            ))

        return levels

    def _resample(
        self, samples: List[Tuple[float, float]], interval: float
    ) -> List[Tuple[float, float, float]]:
        """(ts, price) → [(ts_bar, high, low)]"""
        bars: Dict[float, List[float]] = {}
        for ts, p in samples:
            key = (ts // interval) * interval
            bars.setdefault(key, []).append(p)
        result = []
        for ts in sorted(bars):
            ps = bars[ts]
            result.append((ts, max(ps), min(ps)))
        return result

    def _find_swings(self, values: List[float], high: bool) -> List[float]:
        lb  = self.SWING_LOOKBACK
        fn  = max if high else min
        out = []
        for i in range(lb, len(values) - lb):
            window = values[i - lb: i + lb + 1]
            pivot  = fn(window)
            if values[i] == pivot and values[i] != values[i - 1]:
                out.append(values[i])
        return out

    def _cluster(
        self, levels: List[float], tol_pct: float
    ) -> List[Tuple[float, int]]:
        """Agrupa niveles dentro de tol_pct% entre sí. Devuelve (precio_medio, count)."""
        if not levels:
            return []
        sorted_lv = sorted(levels)
        clusters:  List[Tuple[float, int]] = []
        i = 0
        while i < len(sorted_lv):
            ref   = sorted_lv[i]
            group = [ref]
            j     = i + 1
            while j < len(sorted_lv) and sorted_lv[j] <= ref * (1 + tol_pct / 100):
                group.append(sorted_lv[j])
                j += 1
            clusters.append((sum(group) / len(group), len(group)))
            i = j
        return clusters

    # ── Round Numbers ──────────────────────────────────────────────────────────

    def _round_levels(self, price: float) -> List[LiquidityLevel]:
        lo  = price * (1 - self.ROUND_WINDOW)
        hi  = price * (1 + self.ROUND_WINDOW)

        # Intervalos significativos según magnitud del precio
        if price >= 50_000:   intervals = [1000, 500]
        elif price >= 10_000: intervals = [500,  100]
        elif price >= 1_000:  intervals = [100,   50]
        elif price >= 100:    intervals = [10,     5]
        elif price >= 10:     intervals = [1,    0.5]
        elif price >= 1:      intervals = [0.1, 0.05]
        else:                 intervals = [0.001]

        levels: List[LiquidityLevel] = []
        seen: set[float] = set()

        for rank, interval in enumerate(intervals):
            n_lo = math.ceil(lo / interval)
            n_hi = math.floor(hi / interval)
            for n in range(n_lo, n_hi + 1):
                p = round(n * interval, 10)
                if p in seen:
                    continue
                seen.add(p)
                dist = (p - price) / price * 100
                if abs(dist) < 0.01:
                    continue
                strength = 80 if rank == 0 else 45
                levels.append(LiquidityLevel(
                    price=p,
                    level_type="ROUND",
                    dist_pct=dist,
                    strength=strength,
                ))

        return levels
