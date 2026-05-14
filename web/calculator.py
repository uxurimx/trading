"""
web/calculator.py
─────────────────
Cálculos de PnL y métricas de posición para el dashboard.

Semántica (coincide con lo que muestra Bybit):
  net_at_sl / net_at_tp = PnL BRUTO = (precio_sl/tp - entry) × qty × dirn
    Sin descontar fees ni funding — igual que el display de Bybit.
  gross_pnl = PnL bruto actual desde entrada.
  net_pnl_now = PnL bruto − fee de salida (solo el costo real inmediato).
"""
from __future__ import annotations
import time

TAKER_FEE      = 0.00055   # 0.055% por lado
FUNDING_PER_8H = 0.0001    # 0.01% cada 8h — solo para la línea de detalles


def format_elapsed(seconds: float) -> str:
    s = int(seconds)
    if s < 60:   return f"{s}s"
    if s < 3600: m, sec = divmod(s, 60); return f"{m}m {sec:02d}s"
    h, rem = divmod(s, 3600); return f"{h}h {rem // 60:02d}m"


def calc_position_metrics(pos, live_mark: float = 0.0) -> dict:
    entry  = pos.entry_price
    mark   = live_mark if live_mark > 0 else (pos.mark_price if pos.mark_price > 0 else entry)
    sl     = pos.stop_loss
    tp     = pos.take_profit
    qty    = pos.size
    dirn   = 1 if pos.is_long else -1
    margin = max(pos.margin, 1.0)

    # ── Fees de referencia (se muestran en el footer, NO se restan de SL/TP) ──
    notional_e    = qty * entry
    entry_fee     = notional_e * TAKER_FEE
    exit_fee_mark = qty * mark * TAKER_FEE
    exit_fee_sl   = qty * sl   * TAKER_FEE if sl > 0 else 0.0
    exit_fee_tp   = qty * tp   * TAKER_FEE if tp > 0 else 0.0

    # ── Funding estimado (solo para mostrar en detalles, no en PnL principal) ─
    elapsed_s   = max(0.0, time.time() - pos.created_time / 1000) if pos.created_time > 0 else 0.0
    elapsed_s   = min(elapsed_s, 7 * 24 * 3600)   # cap 7 días — evita distorsión por created_time incorrecto
    elapsed_h   = elapsed_s / 3600
    funding_est = notional_e * FUNDING_PER_8H * (elapsed_h / 8)

    # ── PnL bruto actual (coincide con unrealized_pnl de Bybit) ──────────────
    gross_now = (mark - entry) * qty * dirn

    # ── PnL neto ahora = bruto − fee de salida (costo real si cierras ahora) ─
    net_pnl_now = gross_now - exit_fee_mark

    # ── ROI ──────────────────────────────────────────────────────────────────
    roi_pct       = gross_now / margin * 100          # sobre margen, sin fees
    roi_entry_pct = (mark - entry) / entry * 100 * dirn if entry > 0 else 0.0

    # ── EN SL / EN TP: PnL bruto = lo que muestra Bybit ─────────────────────
    # Bybit muestra gross sin descontar fees ni funding.
    net_at_sl = (sl - entry) * qty * dirn if sl > 0 else None
    net_at_tp = (tp - entry) * qty * dirn if tp > 0 else None

    # ── Progreso y barra ──────────────────────────────────────────────────────
    if tp > 0 and entry > 0:
        tp_dist  = abs(tp - entry)
        sl_dist  = abs(sl - entry) if sl > 0 else 0.0
        progress = ((mark - entry) * dirn / tp_dist) if tp_dist > 0 else 0.0
        rr       = tp_dist / sl_dist if sl_dist > 0 else 0.0
    else:
        progress = rr = 0.0

    if sl > 0 and tp > 0 and tp != sl:
        rng           = tp - sl
        entry_pct_bar = (entry - sl) / rng * 100
        mark_pct_bar  = (mark  - sl) / rng * 100
    else:
        entry_pct_bar = 20.0
        mark_pct_bar  = 20.0 + max(-20.0, min(80.0, progress * 80.0))

    r = lambda v, d=4: round(v, d) if v is not None else None
    return {
        "gross_pnl":      r(gross_now),
        "net_pnl_now":    r(net_pnl_now),
        "roi_pct":        r(roi_pct, 2),
        "roi_entry_pct":  r(roi_entry_pct, 4),
        # EN SL/TP: bruto sin fees (coincide con Bybit)
        "net_at_sl":      r(net_at_sl),
        "net_at_tp":      r(net_at_tp),
        # Fees para el footer
        "entry_fee":      r(entry_fee),
        "exit_fee_now":   r(exit_fee_mark),
        "exit_fee_sl":    r(exit_fee_sl),
        "exit_fee_tp":    r(exit_fee_tp),
        "funding_est":    r(funding_est, 6),
        # Barra
        "progress_pct":   r(max(-50.0, min(150.0, progress * 100.0)), 1),
        "rr_ratio":       r(rr, 2),
        "entry_pct_bar":  r(max(0.0, min(100.0, entry_pct_bar)), 2),
        "mark_pct_bar":   r(max(0.0, min(100.0, mark_pct_bar)), 2),
        # Tiempo
        "elapsed_s":      int(elapsed_s),
        "elapsed_fmt":    format_elapsed(elapsed_s),
        "mark_used":      r(mark, 6),
    }
