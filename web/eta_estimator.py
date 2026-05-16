"""
web/eta_estimator.py — estimador de tiempo a niveles clave.

Dado el precio actual, su velocidad reciente y los niveles del trade
(SL/BE/TP/milestones), proyecta el tiempo esperado de arribo (ETA) a cada
nivel con una banda de confianza basada en la dispersión histórica.

Modelo v1:
  · velocity_signed = Δprice / Δt sobre los últimos ~5min de _price_samples
  · σ_velocity      = desviación estándar de 12 velocidades de 5min (≈1h)
  · ETA(target)     = (target - mark) / velocity_signed
  · banda           = ETA con velocity ± σ

Ajustes por regimen:
  · RANGING       → confidence='low'   (es probable que rebote antes)
  · TRENDING_*    → confidence='high'
  · VOLATILE      → confidence='medium' (banda ensanchada ×1.5)
  · ACCUMULATION  → confidence='none'  (sin dirección clara)

Nunca devuelve ETA si el target queda en sentido contrario a la velocidad —
en ese caso el frontend muestra "→" gris (precio se aleja).
"""
from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING, Dict, List, Optional

if TYPE_CHECKING:
    from streams.market import MarketState


_WINDOW_SHORT_S = 300.0       # 5min para velocidad instantánea
_WINDOW_LONG_S  = 3600.0      # 1h para σ
_SIGMA_BUCKETS  = 12          # ventanas de 5min dentro de la hora

_REGIME_CONF = {
    "TRENDING_UP":   ("high",   1.0),
    "TRENDING_DOWN": ("high",   1.0),
    "RANGING":       ("low",    1.3),
    "VOLATILE":      ("medium", 1.6),
    "ACCUMULATION":  ("none",   2.5),
    "UNKNOWN":       ("medium", 1.2),
}


def _samples_in_window(samples: list, now_s: float, window_s: float) -> list:
    """Recorta _price_samples a [now - window, now]."""
    cut = now_s - window_s
    return [(t, p) for (t, p) in samples if t >= cut]


def _velocity(samples: list) -> Optional[float]:
    """Velocidad signed price/seg sobre la muestra dada. None si insuficiente."""
    if len(samples) < 2:
        return None
    t0, p0 = samples[0]
    t1, p1 = samples[-1]
    dt = t1 - t0
    if dt < 5.0:                       # menos de 5s — ruido puro
        return None
    return (p1 - p0) / dt


def _sigma_velocity(samples: list, now_s: float) -> Optional[float]:
    """σ de velocidades por bucket de 5min sobre la última hora."""
    if not samples:
        return None
    bucket_size = _WINDOW_LONG_S / _SIGMA_BUCKETS
    velocities: List[float] = []
    for i in range(_SIGMA_BUCKETS):
        b_end   = now_s - i * bucket_size
        b_start = b_end - bucket_size
        bucket  = [(t, p) for (t, p) in samples if b_start <= t < b_end]
        v = _velocity(bucket)
        if v is not None:
            velocities.append(v)
    if len(velocities) < 3:
        return None
    mean = sum(velocities) / len(velocities)
    var  = sum((v - mean) ** 2 for v in velocities) / len(velocities)
    return math.sqrt(var)


def _project_eta(
    mark: float,
    target: float,
    v: float,
    v_sigma: Optional[float],
    band_mult: float,
) -> Dict:
    """Calcula ETA + banda para un solo target. v en price/seg."""
    delta = target - mark
    feasible = (delta > 0 and v > 0) or (delta < 0 and v < 0)

    if not feasible or abs(v) < 1e-12:
        return {
            "feasible":      False,
            "eta_seconds":   None,
            "eta_low":       None,
            "eta_high":      None,
        }

    eta = delta / v
    eta_low  = None
    eta_high = None
    if v_sigma and v_sigma > 0:
        v_hi = v + v_sigma * band_mult           # más rápido → ETA menor
        v_lo = v - v_sigma * band_mult           # más lento → ETA mayor
        # Solo banda si v_lo conserva el signo (precio sigue avanzando)
        if (v > 0 and v_lo > 1e-12) or (v < 0 and v_lo < -1e-12):
            eta_low  = abs(delta / v_hi)
            eta_high = abs(delta / v_lo)
        else:
            # demasiada incertidumbre — banda abierta
            eta_low  = abs(delta / v_hi) if abs(v_hi) > 1e-12 else None
            eta_high = None     # frontend lo pinta como "indefinido superior"
    return {
        "feasible":    True,
        "eta_seconds": round(abs(eta), 1),
        "eta_low":     round(eta_low,  1) if eta_low  is not None else None,
        "eta_high":    round(eta_high, 1) if eta_high is not None else None,
    }


def compute_eta(
    state: "MarketState",
    geom: Dict,
    regime: str = "UNKNOWN",
) -> Optional[Dict]:
    """
    Proyecta ETA a SL/BE/TP/milestones desde el precio actual de `state`.

    geom requiere: sl, entry, be, tp, is_long, milestones (lista de {pct, price})
    regime: string del RegimeClassifier ("TRENDING_UP", "RANGING", etc.)
    """
    mark = state.ticker.last_price or state.orderbook.mid_price
    if mark <= 0:
        return None
    samples = list(state._price_samples)
    if not samples:
        return None
    now_s = time.time()

    short = _samples_in_window(samples, now_s, _WINDOW_SHORT_S)
    v     = _velocity(short)
    sigma = _sigma_velocity(samples, now_s)

    conf_label, band_mult = _REGIME_CONF.get(regime, _REGIME_CONF["UNKNOWN"])

    targets_def: List[Dict] = []
    if geom.get("sl"):    targets_def.append({"key": "sl",    "label": "SL",    "price": float(geom["sl"]),    "color": "red"})
    if geom.get("be"):    targets_def.append({"key": "be",    "label": "BE",    "price": float(geom["be"]),    "color": "amber"})
    if geom.get("tp"):    targets_def.append({"key": "tp",    "label": "TP",    "price": float(geom["tp"]),    "color": "green"})
    for m in geom.get("milestones") or []:
        try:
            targets_def.append({
                "key":   f"m{int(m.get('pct'))}",
                "label": f"{int(m.get('pct'))}%",
                "price": float(m.get("price")),
                "color": "slate",
            })
        except Exception:
            continue

    out_targets: List[Dict] = []
    for td in targets_def:
        if v is None:
            out_targets.append({**td, "feasible": False,
                                "eta_seconds": None, "eta_low": None, "eta_high": None})
            continue
        proj = _project_eta(mark, td["price"], v, sigma, band_mult)
        out_targets.append({**td, **proj})

    # Pulso de velocidad en %/min (siempre, aunque no haya feasible)
    v_per_min  = (v or 0.0) * 60.0
    v_pct_min  = (v_per_min / mark * 100.0) if mark > 0 else 0.0
    sigma_pct  = ((sigma or 0.0) * 60.0 / mark * 100.0) if mark > 0 else 0.0

    return {
        "ts":              int(now_s * 1000),
        "symbol":          state.symbol,
        "mark":            mark,
        "regime":          regime,
        "confidence":      conf_label,
        "band_mult":       band_mult,
        "velocity_per_s":  round(v or 0.0, 8),
        "velocity_pct_min": round(v_pct_min, 4),
        "sigma_pct_min":   round(sigma_pct, 4),
        "samples_short":   len(short),
        "targets":         out_targets,
    }
