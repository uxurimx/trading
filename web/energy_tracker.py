"""
web/energy_tracker.py — energía de niveles y zonas.

"Energía" = intensidad reciente de actividad agresiva (trades agresores +
liquidaciones) cerca de un precio. Direccionalidad = CVD signed neto. La idea:
cuando un nivel acumula mucha energía direccional, el frontend lo pinta con
"glow + ⚡ flechas" indicando hacia dónde está siendo empujado el precio.

Modelo:
  · Ventana corta: 60s de trades → energía instantánea
  · Ventana media: 300s de trades → línea base de "ruido"
  · Radio de proximidad: ±band_pct alrededor de cada nivel (default 0.15%)
  · CVD signed neto en la ventana → dirección + magnitud direccional
  · Liquidaciones del lado opuesto cuentan como "carga" (long_liq carga bajistas)
  · Score 0..100 = clipped log(notional_short / max(notional_long_baseline, ε))

Salida (por nivel):
  · energy:    0..100   (intensidad relativa)
  · dir:       'up' | 'down' | 'flat'
  · cvd_pct:   −1..+1   (sesgo direccional normalizado)
  · liq_pull:  −1..+1   (long liqs empujan abajo, short liqs empujan arriba)
  · pulse_ts:  ms del último spike (>=70 energy)

Global por símbolo:
  · vitality: 0..100    (energía agregada del libro reciente)
  · pulse_hz: trades/seg de últimos 30s
"""
from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING, Dict, List, Optional

if TYPE_CHECKING:
    from streams.market import MarketState


_WINDOW_FAST_S = 60.0
_WINDOW_BASE_S = 300.0
_BAND_PCT      = 0.0015              # ±0.15% alrededor del nivel
_PULSE_TH      = 70                  # energy ≥ pulse_threshold → pulso


def _slice_trades(trades: list, now_ms: int, window_s: float) -> list:
    """Devuelve trades dentro de la ventana [now - window, now] en ms."""
    cutoff_ms = now_ms - int(window_s * 1000)
    return [t for t in trades if t.timestamp >= cutoff_ms]


def _trades_near(trades: list, target: float, band_pct: float) -> list:
    """Filtra trades dentro del radio ±band_pct alrededor de target."""
    if target <= 0:
        return []
    band = target * band_pct
    lo, hi = target - band, target + band
    return [t for t in trades if lo <= t.price <= hi]


def _signed_cvd(trades_near: list) -> tuple:
    """Devuelve (buy_qty, sell_qty) del slice."""
    buy = sum(t.qty for t in trades_near if t.side == "Buy")
    sell = sum(t.qty for t in trades_near if t.side == "Sell")
    return buy, sell


def compute_energy(
    state: "MarketState",
    levels: List[Dict],
    view_min: float = 0.0,
    view_max: float = 0.0,
) -> Optional[Dict]:
    """Calcula energía por nivel + vitalidad global del símbolo."""
    now_s  = time.time()
    now_ms = int(now_s * 1000)
    trades = list(state.trades)
    fast   = _slice_trades(trades, now_ms, _WINDOW_FAST_S)
    base   = _slice_trades(trades, now_ms, _WINDOW_BASE_S)

    # Notional baseline para normalizar (60s → 300s con escalado)
    base_notional = sum(t.qty * t.price for t in base) / 5.0  # /5 = comparable a 60s
    fast_notional = sum(t.qty * t.price for t in fast)
    eps = max(1.0, base_notional * 0.01)

    # Liquidaciones recientes (últimos 60s)
    recent_liqs = [lq for lq in state.liquidations
                   if (now_ms - lq.timestamp) <= int(_WINDOW_FAST_S * 1000)]

    per_level: List[Dict] = []
    for lv in levels or []:
        try:
            price = float(lv.get("price"))
            ltype = str(lv.get("type") or "UNK")
        except Exception:
            continue
        if price <= 0:
            continue
        if view_min > 0 and price < view_min: continue
        if view_max > 0 and price > view_max: continue

        near_fast = _trades_near(fast, price, _BAND_PCT)
        if not near_fast:
            per_level.append({
                "price":    price,
                "type":     ltype,
                "energy":   0.0,
                "dir":      "flat",
                "cvd_pct":  0.0,
                "liq_pull": 0.0,
                "pulse_ts": None,
            })
            continue

        buy, sell = _signed_cvd(near_fast)
        total = buy + sell
        cvd_pct = ((buy - sell) / total) if total > 0 else 0.0

        notional = sum(t.qty * t.price for t in near_fast)
        # energy = compresión logarítmica del notional vs baseline global
        ratio = notional / eps
        energy = max(0.0, min(100.0, 25.0 * math.log10(1.0 + ratio)))

        # liquidaciones cercanas (mismo radio)
        band = price * _BAND_PCT
        near_liqs = [lq for lq in recent_liqs if abs(lq.price - price) <= band]
        liq_long  = sum(lq.notional for lq in near_liqs if lq.is_long_liq)
        liq_short = sum(lq.notional for lq in near_liqs if not lq.is_long_liq)
        liq_total = liq_long + liq_short
        liq_pull  = ((liq_short - liq_long) / liq_total) if liq_total > 0 else 0.0
        # Boost de energía cuando hay liquidaciones cercanas
        if liq_total > 0:
            energy = min(100.0, energy + 18.0 * min(1.0, liq_total / 50000.0))

        # Combinar cvd + liq_pull para dirección dominante
        bias = 0.6 * cvd_pct + 0.4 * liq_pull
        if   bias > 0.18: direction = "up"
        elif bias < -0.18: direction = "down"
        else: direction = "flat"

        per_level.append({
            "price":    round(price, 8),
            "type":     ltype,
            "energy":   round(energy, 1),
            "dir":      direction,
            "cvd_pct":  round(cvd_pct, 3),
            "liq_pull": round(liq_pull, 3),
            "pulse_ts": now_ms if energy >= _PULSE_TH else None,
        })

    # Global vitality
    pulse_hz = len(fast) / _WINDOW_FAST_S if fast else 0.0
    if base_notional > 0:
        vitality = min(100.0, 35.0 * math.log10(1.0 + fast_notional / max(1.0, base_notional)))
    else:
        vitality = 0.0
    if vitality < 0:
        vitality = 0.0

    return {
        "ts":         now_ms,
        "symbol":     state.symbol,
        "vitality":   round(vitality, 1),
        "pulse_hz":   round(pulse_hz, 2),
        "fast_notional": round(fast_notional, 2),
        "levels":     per_level,
    }
