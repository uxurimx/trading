"""
web/liquidity_map.py — gravimetría de liquidez.

Agrega orderbook, volume profile, liquidaciones, niveles del LiquidityAnalyzer
y las órdenes propias del usuario en un único payload con coordenadas filtradas
al viewport visible. El frontend (gravity.js) renderiza esto como un campo
gravitacional: cada masa (qty/volumen/liq notional) curva el espacio del precio.

Diseño:
  · La función NO mantiene estado; recibe MarketState + open_orders.
  · El viewport (view_min, view_max) viene del zoom activo en el frontend
    para limitar la carga y enfocar la escala.
  · Cada bucket se cuantiza con VolumeProfile.bucket_size(price) × bucket_mult
    para emparejar la malla del backend con la resolución del canvas.
  · Salidas normalizadas con max_qty/max_vol/max_liq para que el frontend
    pueda mapear masa → opacidad/radio sin recalcular máximos.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Dict, List, Optional

from core.liquidity import LiquidityAnalyzer, VolumeProfile

if TYPE_CHECKING:
    from streams.market import MarketState
    from streams.account import Order


_analyzer = LiquidityAnalyzer()


def _bucketize(
    items: List[tuple],
    bucket: float,
    view_min: float,
    view_max: float,
) -> List[Dict]:
    """Agrupa [(price, qty)] en buckets dentro del viewport. Devuelve ordenado por precio."""
    if bucket <= 0:
        return []
    agg: Dict[float, float] = {}
    for price, qty in items:
        if price < view_min or price > view_max or qty <= 0:
            continue
        key = round(round(price / bucket) * bucket, 10)
        agg[key] = agg.get(key, 0.0) + qty
    return [{"price": p, "qty": round(q, 6)} for p, q in sorted(agg.items())]


def build_liquidity_map(
    state: "MarketState",
    open_orders: Optional[Dict[str, "Order"]],
    view_min: float,
    view_max: float,
    bucket_mult: float = 1.0,
    max_liqs: int = 60,
) -> Optional[Dict]:
    """
    Construye el snapshot de liquidez para el viewport [view_min, view_max].

    Args:
        state:        MarketState del símbolo
        open_orders:  dict order_id → Order (puede contener otros símbolos; se filtran)
        view_min:     límite inferior de precio del viewport
        view_max:     límite superior
        bucket_mult:  multiplicador del bucket adaptativo (1 = nativo, 4 = más grueso)
    """
    price = state.ticker.last_price or state.orderbook.mid_price
    if price <= 0 or view_max <= view_min:
        return None

    bucket = VolumeProfile.bucket_size(price) * max(0.25, bucket_mult)

    # ── Orderbook ────────────────────────────────────────────────────────────
    bids_raw = list(state.orderbook.bids.items())
    asks_raw = list(state.orderbook.asks.items())
    bids = _bucketize(bids_raw, bucket, view_min, view_max)
    asks = _bucketize(asks_raw, bucket, view_min, view_max)

    # ── Volume Profile ───────────────────────────────────────────────────────
    vp_items = list(state.volume_profile._data.items())
    vp = _bucketize(vp_items, bucket, view_min, view_max)
    vp_out = [{"price": x["price"], "vol": x["qty"]} for x in vp]

    # ── Liquidaciones (raw, no bucketizadas — son eventos puntuales) ─────────
    now_ms = int(time.time() * 1000)
    liqs: List[Dict] = []
    for liq in list(state.liquidations)[-max_liqs:]:
        if liq.price < view_min or liq.price > view_max:
            continue
        liqs.append({
            "price":       liq.price,
            "notional":    round(liq.notional, 2),
            "side":        liq.side,
            "is_long_liq": liq.is_long_liq,
            "age_s":       round(max(0.0, (now_ms - liq.timestamp) / 1000.0), 1),
        })

    # ── Niveles estructurales (HVN/LVN/EQ/ROUND) ─────────────────────────────
    levels_out: List[Dict] = []
    try:
        lmap = _analyzer.analyze(state)
        for lv in lmap.levels:
            if lv.price < view_min or lv.price > view_max:
                continue
            levels_out.append({
                "price":    lv.price,
                "type":     lv.level_type,
                "strength": lv.strength,
                "count":    lv.count,
                "vol_pct":  round(lv.vol_pct, 3),
            })
    except Exception:
        pass

    # ── Órdenes propias del símbolo ──────────────────────────────────────────
    my_orders: List[Dict] = []
    if open_orders:
        for od in open_orders.values():
            if od.symbol != state.symbol:
                continue
            if od.price <= 0 or od.price < view_min or od.price > view_max:
                continue
            my_orders.append({
                "price":    od.price,
                "side":     od.side,
                "qty":      od.qty,
                "type":     od.order_type,
                "order_id": od.order_id,
                "status":   od.status,
            })

    # ── Normalizadores para el frontend ──────────────────────────────────────
    max_qty = max((x["qty"] for x in bids + asks), default=0.0)
    max_vol = max((x["vol"] for x in vp_out), default=0.0)
    max_liq = max((x["notional"] for x in liqs), default=0.0)

    return {
        "ts":         now_ms,
        "symbol":     state.symbol,
        "current":    price,
        "bucket":     bucket,
        "view_min":   view_min,
        "view_max":   view_max,
        "bids":       bids,
        "asks":       asks,
        "vp":         vp_out,
        "liqs":       liqs,
        "levels":     levels_out,
        "my_orders":  my_orders,
        "max_qty":    round(max_qty, 6),
        "max_vol":    round(max_vol, 6),
        "max_liq":    round(max_liq, 2),
    }
