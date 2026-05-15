"""
web/sentiment.py — cabina del piloto.

Tres instrumentos que el frontend renderiza como gauges:

  · Tacómetro de presión emocional (0-100, side: fear|greed|neutral)
      Combina imbalance del libro, pulso CVD, liquidaciones recientes y funding.
      Es la "presión sobre los participantes" — qué tan fuerte está el sesgo
      direccional y cuánto pánico/euforia hay en la calle.

  · Velocímetro (0-100, km/h normalizado)
      Velocidad real del precio: |Δprice| por minuto en los últimos ~60s,
      escalado contra la volatilidad ambiental (ATR proxy de cvd_candles).

  · Tipo de carretera (highway|curvy|rough|gridlock)
      Mapeo desde RegimeClassifier + volatilidad → hint de apalancamiento:
        highway  (TRENDING)   → 5×–10×    : terreno limpio, momentum claro
        curvy    (RANGING)    → 2×–3×     : rebotes en S/R, scalping
        rough    (VOLATILE)   → 1×        : choppy, riesgo de stop-hunt
        gridlock (ACCUMULATION) → esperar : sin combustible direccional
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Dict, Optional

if TYPE_CHECKING:
    from streams.market import MarketState


_ROAD = {
    "TRENDING_UP":   ("highway",  "Autopista alcista",   "10×",  10),
    "TRENDING_DOWN": ("highway",  "Autopista bajista",   "10×",  10),
    "RANGING":       ("curvy",    "Curvas — rebotes",    "3×",    3),
    "VOLATILE":      ("rough",    "Terracería — choppy", "1×",    1),
    "ACCUMULATION":  ("gridlock", "Atasco — sin flujo",  "espera", 0),
}


def _pressure(state: "MarketState", sig: Dict) -> Dict:
    """Presión emocional 0-100 + side direccional."""
    components = {}

    # 1) Orderbook imbalance: 0.5 = neutro, distancia a 0.5 → presión
    imb = state.orderbook.imbalance if state.orderbook.best_bid > 0 else 0.5
    imb_dev = (imb - 0.5) * 2   # [-1, +1]  +1 = bid presiona, -1 = ask presiona
    imb_score = abs(imb_dev) * 100
    components["imbalance"] = round(imb_score, 1)

    # 2) Pulso CVD: delta_pct de la vela actual
    candles = list(state.cvd_candles)
    cvd_delta = candles[-1].delta_pct if candles else 0.0   # [-100, +100]
    components["cvd_pulse"] = round(cvd_delta, 1)

    # 3) Liquidaciones recientes (últimos 60s): notional como % del OI
    now_ms = int(time.time() * 1000)
    liq_long = 0.0; liq_short = 0.0
    for liq in list(state.liquidations):
        if now_ms - liq.timestamp > 60_000:
            continue
        if liq.is_long_liq: liq_long  += liq.notional
        else:               liq_short += liq.notional
    liq_total = liq_long + liq_short
    oi_usd = max(1.0, state.ticker.open_interest * state.ticker.last_price)
    liq_pct = min(100.0, liq_total / oi_usd * 100_000)   # escala empírica
    components["liq_60s_usd"] = round(liq_total, 0)
    components["liq_score"]   = round(liq_pct, 1)
    # Dirección: longs liquidados → pánico bajista; shorts → pánico alcista
    liq_side = (liq_short - liq_long) / liq_total if liq_total > 0 else 0.0

    # 4) Funding extremo: |funding%| → presión de financiamiento
    fund = state.ticker.funding_rate or 0.0   # ya en %
    fund_score = min(100.0, abs(fund) * 500)  # 0.02% → 10pts; 0.2% → 100pts
    components["funding_pct"] = round(fund, 4)

    # ── Score compuesto (ponderado) ──────────────────────────────────────────
    score = (
        imb_score   * 0.30 +
        abs(cvd_delta) * 0.30 +
        liq_pct     * 0.25 +
        fund_score  * 0.15
    )
    score = max(0.0, min(100.0, score))

    # ── Dirección global ────────────────────────────────────────────────────
    # Pondera signos: imb_dev (bid+/ask−), cvd_delta, liq_side, -fund (funding+
    # implica longs pagan → presión bajista latente)
    bias = (
        imb_dev    * 0.30 +
        (cvd_delta / 100) * 0.35 +
        liq_side   * 0.25 +
        (-fund / max(0.05, abs(fund) or 0.05)) * 0.10 if fund else 0
    ) if any([imb_dev, cvd_delta, liq_side, fund]) else 0

    if score < 25:
        side = "neutral"
    elif bias > 0.10:
        side = "greed"     # presión compradora
    elif bias < -0.10:
        side = "fear"      # presión vendedora
    else:
        side = "neutral"

    return {
        "score":      round(score, 1),
        "side":       side,
        "bias":       round(bias, 3),
        "components": components,
    }


def _velocity(state: "MarketState") -> Dict:
    """Velocidad del precio: %/min de los últimos 60s contra volatilidad ambiental."""
    samples = list(state._price_samples)
    if len(samples) < 8:
        return {"score": 0.0, "pct_per_min": 0.0, "ref_pct": 0.0}

    now = time.time()
    recent = [(t, p) for t, p in samples if now - t <= 60]
    if len(recent) < 4:
        recent = samples[-8:]
    if len(recent) < 2:
        return {"score": 0.0, "pct_per_min": 0.0, "ref_pct": 0.0}

    t0, p0 = recent[0]
    t1, p1 = recent[-1]
    dt_min = max(1/60, (t1 - t0) / 60)
    pct_per_min = abs(p1 - p0) / max(1e-9, p0) * 100 / dt_min

    # Referencia: rango de las últimas 5 velas / minuto
    candles = list(state.cvd_candles)[-5:]
    if candles and candles[0].close > 0:
        ranges = []
        for c in candles:
            hi = max(c.open, c.close); lo = min(c.open, c.close)
            if hi > lo and c.close > 0:
                ranges.append((hi - lo) / c.close * 100)
        ref = sum(ranges) / len(ranges) if ranges else 0.1
    else:
        ref = 0.1

    ref = max(0.05, ref)
    # score: 50 = velocidad normal; 100 = 2× la velocidad de referencia
    score = min(100.0, pct_per_min / (ref * 2) * 100)
    return {
        "score":       round(score, 1),
        "pct_per_min": round(pct_per_min, 3),
        "ref_pct":     round(ref, 3),
    }


def _road(state: "MarketState", sig: Dict, velocity_score: float) -> Dict:
    """Tipo de carretera + hint de apalancamiento."""
    rg = sig.get("regime") if sig else None
    regime = getattr(rg, "regime", None) if rg else None
    label = getattr(rg, "label", "?") if rg else "?"
    conf  = getattr(rg, "confidence", 0) if rg else 0

    road_type, desc, lev_lbl, lev_n = _ROAD.get(
        regime or "RANGING",
        ("curvy", "Curvas — sin clasificar", "2×", 2),
    )

    # Override: si velocidad > 80 y road es highway, degradar a rough (peligro)
    if velocity_score > 85 and road_type == "highway":
        road_type, desc, lev_lbl, lev_n = ("rough",
            "Autopista en velocidad límite",
            f"{max(1, lev_n // 2)}×", max(1, lev_n // 2))

    return {
        "type":          road_type,
        "regime":        regime or "UNKNOWN",
        "regime_label":  label,
        "confidence":    int(conf),
        "description":   desc,
        "leverage_hint": lev_lbl,
        "leverage_n":    lev_n,
    }


def compute_pilot(state: "MarketState", sig: Optional[Dict]) -> Optional[Dict]:
    """Construye el payload completo de los tres gauges. None si no hay precio."""
    if not state or state.ticker.last_price <= 0:
        return None
    sig = sig or {}
    vel = _velocity(state)
    return {
        "ts":       int(time.time() * 1000),
        "symbol":   state.symbol,
        "price":    state.ticker.last_price,
        "pressure": _pressure(state, sig),
        "velocity": vel,
        "road":     _road(state, sig, vel["score"]),
    }
