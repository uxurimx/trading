"""
web/calculator.py — métricas de posición para el dashboard QTS.

PnL bruto = (precio − entry) × qty × dirn  (coincide con Bybit, sin fees).
Breakeven = precio donde PnL bruto cubre entry_fee + exit_fee.
Hitos 25/50/75 = precios intermedios entre entry y TP con ROI esperado.
"""
from __future__ import annotations
import time

TAKER_FEE      = 0.00055
FUNDING_PER_8H = 0.0001


def format_elapsed(seconds: float) -> str:
    s = int(seconds)
    if s <= 0:    return "—"
    if s < 60:    return f"{s}s"
    if s < 3600:  m, sec = divmod(s, 60); return f"{m}m {sec:02d}s"
    if s < 86400: h, rem = divmod(s, 3600); return f"{h}h {rem // 60:02d}m"
    d, rem = divmod(s, 86400); return f"{d}d {rem // 3600}h"


def calc_position_metrics(pos, live_mark: float = 0.0, elapsed_override: float | None = None) -> dict:
    entry  = pos.entry_price
    mark   = live_mark if live_mark > 0 else (pos.mark_price if pos.mark_price > 0 else entry)
    sl     = pos.stop_loss
    tp     = pos.take_profit
    qty    = pos.size
    dirn   = 1 if pos.is_long else -1
    margin = max(pos.margin, 1.0)

    # ── Fees ─────────────────────────────────────────────────────────────────
    notional_e    = qty * entry
    entry_fee     = notional_e * TAKER_FEE
    exit_fee_mark = qty * mark * TAKER_FEE
    exit_fee_sl   = qty * sl   * TAKER_FEE if sl > 0 else 0.0
    exit_fee_tp   = qty * tp   * TAKER_FEE if tp > 0 else 0.0

    # ── Elapsed / funding ─────────────────────────────────────────────────────
    if elapsed_override is not None:
        elapsed_display = max(0.0, elapsed_override)
    else:
        elapsed_raw     = max(0.0, time.time() - pos.created_time / 1000) if pos.created_time > 0 else 0.0
        elapsed_display = elapsed_raw if elapsed_raw < 365 * 24 * 3600 else 0.0
    # Funding solo para detalle; cap 7 días para no distorsionar con timestamps malos
    elapsed_h_fund = min(elapsed_display, 7 * 24 * 3600) / 3600
    funding_est    = notional_e * FUNDING_PER_8H * (elapsed_h_fund / 8)

    # ── PnL bruto y ROI ───────────────────────────────────────────────────────
    gross_now     = (mark - entry) * qty * dirn
    net_pnl_now   = gross_now - exit_fee_mark
    full_net_pnl  = gross_now - entry_fee - exit_fee_mark  # incluye fee entrada; = 0 en BE
    roi_pct       = gross_now / margin * 100
    roi_entry_pct = (mark - entry) / entry * 100 * dirn if entry > 0 else 0.0
    full_net_pct  = full_net_pnl / margin * 100

    # ── EN SL / EN TP: neto real = bruto − entry_fee − exit_fee ─────────────
    # Mismo criterio que full_net_pnl: lo que realmente entraría/saldría al bolsillo.
    # SL: el loss es peor por los fees. TP: la ganancia es menor por los fees.
    net_at_sl = (sl - entry) * qty * dirn - entry_fee - exit_fee_sl if sl > 0 else None
    net_at_tp = (tp - entry) * qty * dirn - entry_fee - exit_fee_tp if tp > 0 else None

    # ── Progreso ──────────────────────────────────────────────────────────────
    if tp > 0 and entry > 0:
        tp_dist  = abs(tp - entry)
        sl_dist  = abs(sl - entry) if sl > 0 else 0.0
        progress = ((mark - entry) * dirn / tp_dist) if tp_dist > 0 else 0.0
        rr       = tp_dist / sl_dist if sl_dist > 0 else 0.0
    else:
        progress = rr = 0.0

    # ── Barra SL→TP ───────────────────────────────────────────────────────────
    has_bar = sl > 0 and tp > 0 and tp != sl
    if has_bar:
        rng           = tp - sl
        entry_pct_bar = (entry - sl) / rng * 100
        mark_pct_bar  = (mark  - sl) / rng * 100
    else:
        entry_pct_bar = 20.0
        mark_pct_bar  = 20.0 + max(-20.0, min(80.0, progress * 80.0))

    def to_bar(price: float) -> float:
        if has_bar:
            return max(0.0, min(100.0, (price - sl) / (tp - sl) * 100))
        return entry_pct_bar

    # ── Breakeven (precio donde PnL bruto cubre ambos fees) ──────────────────
    # Ecuación: (be - entry) * qty * dirn = entry_fee + exit_fee_at_be
    # be * (dirn - TAKER_FEE) = entry * (dirn + TAKER_FEE)
    denom = dirn - TAKER_FEE
    be_price    = entry * (dirn + TAKER_FEE) / denom if abs(denom) > 1e-9 else entry
    be_pct_bar  = to_bar(be_price)

    # ── Hitos 25 / 50 / 75 % hacia TP ────────────────────────────────────────
    milestones = []
    if tp > 0 and entry > 0 and margin > 1:
        for frac in (0.25, 0.50, 0.75):
            m_price   = entry + frac * (tp - entry)
            m_gross   = frac * (tp - entry) * qty * dirn
            m_roi     = m_gross / margin * 100
            milestones.append({
                "pct":     int(frac * 100),
                "price":   round(m_price, 6),
                "gross":   round(m_gross, 4),
                "roi":     round(m_roi, 2),
                "bar_pct": round(to_bar(m_price), 2),
            })

    r = lambda v, d=4: round(v, d) if v is not None else None

    # ── Geometría cruda (para reproyección con zoom en el frontend) ─────────
    # Mantiene los precios invariantes; el frontend transforma con scale.js
    geometry = {
        "sl":      r(sl, 6),
        "entry":   r(entry, 6),
        "tp":      r(tp, 6),
        "be":      r(be_price, 6),
        "mark":    r(mark, 6),
        "is_long": bool(pos.is_long),
        "milestones": [{"pct": m["pct"], "price": m["price"]} for m in milestones],
    }

    return {
        "gross_pnl":        r(gross_now),
        "net_pnl_now":      r(net_pnl_now),
        "full_net_pnl":     r(full_net_pnl),
        "full_net_pct":     r(full_net_pct, 2),
        "roi_pct":          r(roi_pct, 2),
        "roi_entry_pct":    r(roi_entry_pct, 4),
        "net_at_sl":        r(net_at_sl),
        "net_at_tp":        r(net_at_tp),
        "entry_fee":        r(entry_fee),
        "exit_fee_now":     r(exit_fee_mark),
        "exit_fee_sl":      r(exit_fee_sl),
        "exit_fee_tp":      r(exit_fee_tp),
        "funding_est":      r(funding_est, 6),
        "progress_pct":     r(max(-50.0, min(150.0, progress * 100.0)), 1),
        "rr_ratio":         r(rr, 2),
        "entry_pct_bar":    r(max(0.0, min(100.0, entry_pct_bar)), 2),
        "mark_pct_bar":     r(max(0.0, min(100.0, mark_pct_bar)), 2),
        "breakeven_price":  r(be_price, 6),
        "be_pct_bar":       r(be_pct_bar, 2),
        "milestones":       milestones,
        "elapsed_s":        int(elapsed_display),
        "elapsed_fmt":      format_elapsed(elapsed_display),
        "mark_used":        r(mark, 6),
        "geometry":         geometry,
    }
