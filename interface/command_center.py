"""
interface/command_center.py
────────────────────────────
CommandCenter — Pantalla principal de operaciones.

Layout limpio, minimalista, todo en una sola vista:
  · Modo + objetivos + controles de tiempo
  · Trades activos como tarjetas expandibles (con gráfico + análisis)
  · Propuesta con confirmar/rechazar
  · Simulación + Journal compacto
"""
from __future__ import annotations

import math
import time
from typing import Optional, TYPE_CHECKING

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk, Pango

from core.order_model import AutoMode, TradeState, ControllerState
from core.db import get_journal_stats, get_recent_trades

if TYPE_CHECKING:
    from core.controller import TradeController
    from core.order_model import TradeRecord
    from streams.account import AccountState, Position
    from core.risk import RiskStatus


# ─── Paleta ───────────────────────────────────────────────────────────────────

HEX = {
    "buy":  "#57e389", "sell": "#ff7b63", "blue": "#78aeed",
    "warn": "#f8e45c", "teal": "#93ddc2", "text": "#ebebeb",
    "sub":  "#9a9996", "over": "#5e5c64",
}
RGB = {
    "buy":  (0.341, 0.890, 0.537), "sell": (1.000, 0.482, 0.388),
    "card": (0.180, 0.180, 0.180), "surf": (0.220, 0.220, 0.220),
    "bg":   (0.141, 0.141, 0.141),
}

MODE_META = {
    AutoMode.MANUAL:     ("MANUAL",    "over", "Solo monitoreas. Tú ejecutas todo."),
    AutoMode.SUGGEST:    ("SUGGEST",   "blue", "El sistema propone. Tú confirmas con 1 clic."),
    AutoMode.AUTO_ENTRY: ("AUTO",      "warn", "Entra solo. Tú gestionas el trade."),
    AutoMode.FULL_AUTO:  ("FULL AUTO", "sell", "Autónomo: entra, trail y cierra solo."),
}


# ─── Utilidades ───────────────────────────────────────────────────────────────

def _ml(text: str = "", bold: bool = False, size: str = "") -> Gtk.Label:
    lbl = Gtk.Label(label=text)
    lbl.set_xalign(0)
    lbl.set_use_markup(True)
    lbl.add_css_class("qts-mono-sm")
    lbl.set_max_width_chars(60)
    lbl.set_ellipsize(Pango.EllipsizeMode.END)
    lbl.set_wrap(False)
    return lbl


def _sep() -> Gtk.Separator:
    s = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
    s.add_css_class("qts-sep")
    return s


def _section(text: str) -> Gtk.Label:
    lbl = Gtk.Label(label=text)
    lbl.add_css_class("qts-section")
    lbl.set_xalign(0)
    return lbl


def _fp(p: float) -> str:
    if p <= 0:    return "──"
    if p >= 1000: return f"{p:,.2f}"
    if p >= 10:   return f"{p:.4f}"
    return f"{p:.5f}"


def _fmt_duration(opened_at: int) -> str:
    if not opened_at:
        return "──"
    elapsed = int(time.time() - opened_at)
    if elapsed < 0:
        return "??"
    if elapsed < 60:
        return f"{elapsed}s"
    if elapsed < 3600:
        return f"{elapsed // 60}m {elapsed % 60}s"
    if elapsed < 86400:
        h = elapsed // 3600
        m = (elapsed % 3600) // 60
        return f"{h}h {m}m"
    d = elapsed // 86400
    h = (elapsed % 86400) // 3600
    return f"{d}d {h}h"


def _estimate_ttp(entry: float, current: float, tp: float,
                  opened_at: int, side: str) -> str:
    """Estima tiempo hasta TP basado en velocidad actual del precio."""
    if not opened_at or not entry or not tp or not current:
        return "──"
    elapsed = time.time() - opened_at
    if elapsed < 60:
        return "──"
    covered   = (current - entry) if side == "Buy" else (entry - current)
    remaining = (tp - current)    if side == "Buy" else (current - tp)
    if covered <= 0 or remaining <= 0:
        return "──"
    rate = covered / elapsed   # precio por segundo
    secs = remaining / rate
    if secs > 86400:
        return ">24h"
    elif secs > 3600:
        return f"~{int(secs/3600)}h {int((secs%3600)/60)}m"
    elif secs > 60:
        return f"~{int(secs/60)}m"
    else:
        return f"~{int(secs)}s"


def _estimate_proposal_ttp(symbol: str, entry: float, tp: float,
                           market_states: dict) -> str:
    """
    Estima el tiempo para alcanzar el TP de una propuesta nueva,
    basado en la velocidad de precios observada en los últimos 2 minutos.
    """
    ms = market_states.get(symbol) if market_states else None
    if not ms or not ms.trades:
        return "──"
    trades = list(ms.trades)
    now_ms = time.time() * 1000
    recent = [t for t in trades if now_ms - t.timestamp < 120_000]
    if len(recent) < 8:
        recent = trades[-20:]
    if len(recent) < 5:
        return "──"
    prices     = [t.price for t in recent]
    span_s     = max((recent[-1].timestamp - recent[0].timestamp) / 1000, 1)
    price_range = max(prices) - min(prices)
    if price_range <= 0:
        return "──"
    velocity = price_range / span_s           # precio por segundo
    tp_dist  = abs(tp - entry)
    if velocity <= 0 or tp_dist <= 0:
        return "──"
    secs = tp_dist / velocity
    if secs > 86400:
        return ">24h"
    if secs > 3600:
        return f"~{int(secs/3600)}h{int((secs % 3600)/60)}m"
    if secs > 60:
        return f"~{int(secs/60)}m"
    return f"~{int(secs)}s"


def _fmt_duration_s(seconds: int) -> str:
    if seconds <= 0:
        return "──"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds//60}m{seconds%60:02d}s"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f"{h}h{m:02d}m"


def _trade_risk_analysis(trade: "TradeRecord", pos: Optional["Position"],
                         mark: float) -> list[str]:
    """Genera análisis de riesgo contextual para el trade."""
    lines = []
    req = trade.request
    if not req:
        return lines

    elapsed_min = (time.time() - trade.opened_at) / 60 if trade.opened_at else 0

    # Progreso
    entry   = trade.entry_price or req.entry_price
    tp_dist = abs(req.tp_price - entry)
    sl_dist = abs(req.sl_price - entry)
    if tp_dist > 0 and entry > 0:
        prog = ((mark - entry) / tp_dist if req.side == "Buy"
                else (entry - mark) / tp_dist)
        prog_pct = max(0, min(100, prog * 100))
    else:
        prog_pct = 0

    # Funding estimate (8h cycle, ~0.01% avg)
    notional = req.qty * entry
    funding_cost_4h = notional * 0.0001 * (4 / 8)
    if funding_cost_4h > 0.01:
        lines.append(f"💸 Funding est. 4h: -${funding_cost_4h:.3f} (notional ${notional:.1f})")

    # Tiempo vs progreso
    elapsed_str = _fmt_duration_s(int(elapsed_min * 60))
    if elapsed_min > 30 and prog_pct < 20:
        lines.append(f"⚠ {elapsed_str} y solo {prog_pct:.0f}% de progreso — mercado lento")
        lines.append("  → Considera aumentar el límite de tiempo o ajustar el SL")
    elif elapsed_min > 60 and prog_pct < 50:
        lines.append(f"⏱ {elapsed_str} transcurridos — progreso moderado ({prog_pct:.0f}%)")
    elif prog_pct >= 50:
        lines.append(f"✓ Buen progreso: {prog_pct:.0f}% del camino al TP")

    # R:R actual (SL dinámico)
    current_sl_dist = abs(mark - trade.current_sl)
    current_tp_dist = abs(req.tp_price - mark)
    if current_sl_dist > 0 and current_tp_dist > 0:
        live_rr = current_tp_dist / current_sl_dist
        lines.append(f"R:R actual: {live_rr:.1f}:1  (inicio: {req.rr_ratio:.1f}:1)")

    if trade.state == TradeState.BREAKEVEN:
        lines.append("🛡 Breakeven activo — no puedes perder en este trade")
    elif trade.state == TradeState.TRAILING:
        lines.append("📈 Trailing activo — ganancia protegida y creciendo")

    return lines[:4]


# ─── Gráfico mini del trade ───────────────────────────────────────────────────

class TradePriceChart(Gtk.DrawingArea):
    """
    Mini gráfico horizontal mostrando SL → Entry → TP con precio actual.
    El relleno de progreso muestra cuánto ha avanzado el trade.
    """

    def __init__(self) -> None:
        super().__init__()
        self.set_size_request(-1, 52)
        self.set_hexpand(True)
        self._entry = self._sl = self._tp = self._mark = 0.0
        self._side  = "Buy"
        self.set_draw_func(self._draw)

    def update(self, entry: float, sl: float, tp: float,
               mark: float, side: str) -> None:
        self._entry = entry
        self._sl    = sl
        self._tp    = tp
        self._mark  = mark if mark > 0 else entry
        self._side  = side
        self.queue_draw()

    def _draw(self, _area, cr, w: int, h: int) -> None:
        prices = [p for p in [self._sl, self._tp, self._mark, self._entry] if p > 0]
        if not prices:
            return
        lo  = min(prices) * 0.9985
        hi  = max(prices) * 1.0015
        rng = hi - lo or 1e-9

        def px(price: float) -> float:
            return (price - lo) / rng * w

        # Fondo
        cr.set_source_rgba(*RGB["card"], 1.0)
        cr.rectangle(0, 0, w, h)
        cr.fill()

        mid = h / 2

        # Zona de pérdida (SL → Entry): rojo tenue
        x_sl    = px(self._sl)
        x_entry = px(self._entry)
        x_tp    = px(self._tp)
        x_mark  = px(self._mark)

        cr.set_source_rgba(*RGB["sell"], 0.12)
        if self._side == "Buy":
            cr.rectangle(x_sl, 0, x_entry - x_sl, h)
        else:
            cr.rectangle(x_entry, 0, x_sl - x_entry, h)
        cr.fill()

        # Zona de ganancia (Entry → TP): verde tenue
        cr.set_source_rgba(*RGB["buy"], 0.12)
        if self._side == "Buy":
            cr.rectangle(x_entry, 0, x_tp - x_entry, h)
        else:
            cr.rectangle(x_tp, 0, x_entry - x_tp, h)
        cr.fill()

        # Progreso real (Entry → Mark)
        is_winning = (self._mark > self._entry) if self._side == "Buy" else (self._mark < self._entry)
        prog_color = RGB["buy"] if is_winning else RGB["sell"]
        cr.set_source_rgba(*prog_color, 0.35)
        if self._side == "Buy":
            x0 = min(x_entry, x_mark)
            cr.rectangle(x0, mid - 6, abs(x_mark - x_entry), 12)
        else:
            x0 = min(x_entry, x_mark)
            cr.rectangle(x0, mid - 6, abs(x_mark - x_entry), 12)
        cr.fill()

        # ── Líneas verticales ──────────────────────────────────────────
        # SL (rojo)
        cr.set_source_rgba(*RGB["sell"], 0.9)
        cr.set_line_width(2)
        cr.move_to(x_sl, 8); cr.line_to(x_sl, h - 8); cr.stroke()

        # TP (verde)
        cr.set_source_rgba(*RGB["buy"], 0.9)
        cr.move_to(x_tp, 8); cr.line_to(x_tp, h - 8); cr.stroke()

        # Entry (blanco tenue)
        cr.set_source_rgba(0.9, 0.9, 0.9, 0.5)
        cr.set_line_width(1)
        cr.set_dash([4, 3], 0)
        cr.move_to(x_entry, 0); cr.line_to(x_entry, h); cr.stroke()
        cr.set_dash([], 0)

        # Mark price (círculo blanco relleno)
        cr.set_source_rgba(1.0, 1.0, 1.0, 1.0)
        cr.arc(x_mark, mid, 5, 0, 2 * math.pi); cr.fill()
        # borde de color
        cr.set_source_rgba(*prog_color, 1.0)
        cr.set_line_width(2)
        cr.arc(x_mark, mid, 5, 0, 2 * math.pi); cr.stroke()

        # ── Etiquetas de precio ────────────────────────────────────────
        cr.set_font_size(9)

        def _label(price: float, color, align: str = "center") -> None:
            text = _fp(price)
            xb, _yb, tw, _th, _dx, _dy = cr.text_extents(text)
            xp = px(price)
            if align == "left":
                tx = max(2.0, xp + 3)
            elif align == "right":
                tx = max(2.0, xp - tw - 3)
            else:
                tx = max(2.0, xp - tw / 2)
            cr.set_source_rgba(*color, 0.9)
            cr.move_to(tx, h - 4)
            cr.show_text(text)

        _label(self._sl,    RGB["sell"], "left")
        _label(self._tp,    RGB["buy"],  "right")
        _label(self._entry, (0.9, 0.9, 0.9), "center")

        # Mark price label above circle
        text = _fp(self._mark)
        xb, _yb, tw, _th, _dx, _dy = cr.text_extents(text)
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.9)
        cr.move_to(max(2.0, x_mark - tw / 2), mid - 9)
        cr.show_text(text)


# ─── Tarjeta de trade activo ──────────────────────────────────────────────────

class TradeCard(Gtk.Box):
    """
    Tarjeta expandible para un trade activo.
    Summary siempre visible; detalle con gráfico al hacer clic.
    """

    def __init__(self, controller: "TradeController") -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add_css_class("qts-card")
        self.set_margin_bottom(6)

        self._controller = controller
        self._symbol: str = ""
        self._expanded = False

        self._build()

    def _build(self) -> None:
        # ── Fila 1: símbolo · estado · PnL · duración · modo · expand · cerrar ──
        summary = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        summary.set_margin_start(6); summary.set_margin_end(6)
        summary.set_margin_top(4);   summary.set_margin_bottom(2)

        self._sym_lbl   = _ml()
        self._state_lbl = _ml()
        self._pnl_lbl   = _ml()
        self._dur_lbl   = _ml()   # duración siempre visible
        self._auto_lbl  = _ml()   # muestra estado AUTO / MANUAL
        self._sym_lbl.set_hexpand(True)

        self._mode_btn = Gtk.Button(label="▶ AUTO")
        self._mode_btn.add_css_class("flat")
        self._mode_btn.connect("clicked", self._on_mode_toggle)

        close_btn = Gtk.Button(label="✗")
        close_btn.add_css_class("destructive-action")
        close_btn.set_size_request(30, -1)
        close_btn.connect("clicked", self._on_close)

        expand_btn = Gtk.Button(label="▼ Detalle")
        expand_btn.add_css_class("flat")
        expand_btn.connect("clicked", self._on_toggle)
        self._expand_btn = expand_btn

        summary.append(self._sym_lbl)
        summary.append(self._state_lbl)
        summary.append(self._pnl_lbl)
        summary.append(self._dur_lbl)
        summary.append(self._auto_lbl)
        summary.append(self._mode_btn)
        summary.append(expand_btn)
        summary.append(close_btn)
        self.append(summary)

        # ── Fila 2: gráfico siempre visible ────────────────────────────
        self._chart = TradePriceChart()
        self._chart.set_margin_start(6); self._chart.set_margin_end(6)
        self.append(self._chart)

        # ── Fila 3: progreso + lev + notional + ETA ────────────────────
        prog_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        prog_row.set_margin_start(6); prog_row.set_margin_end(6)
        prog_row.set_margin_bottom(2)

        self._prog = Gtk.ProgressBar()
        self._prog.set_show_text(True)
        self._prog.set_hexpand(True)
        self._lev_lbl      = _ml()
        self._notional_lbl = _ml()
        self._eta_lbl      = _ml()
        self._risk_now_lbl = _ml()
        prog_row.append(self._prog)
        prog_row.append(self._lev_lbl)
        prog_row.append(self._notional_lbl)
        prog_row.append(self._eta_lbl)
        prog_row.append(self._risk_now_lbl)
        self.append(prog_row)

        # ── Fila 4: advertencia inline (solo visible cuando hay alerta) ─
        self._warn_lbl = _ml()
        self._warn_lbl.set_margin_start(8); self._warn_lbl.set_margin_end(8)
        self._warn_lbl.set_margin_bottom(2)
        self._warn_lbl.set_visible(False)
        self.append(self._warn_lbl)

        # ── Detalle (revealer) ─────────────────────────────────────────
        self._revealer = Gtk.Revealer()
        self._revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self._revealer.set_transition_duration(200)

        detail_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        detail_box.set_margin_start(8); detail_box.set_margin_end(8)
        detail_box.set_margin_bottom(6)
        detail_box.append(_sep())

        self._levels_lbl  = _ml()
        self._sizing_lbl  = _ml()
        self._opened_lbl  = _ml()
        self._reasons_lbl = _ml()
        self._reasons_lbl.set_wrap(True)
        self._reasons_lbl.set_max_width_chars(50)
        self._risk_lbl1 = _ml()
        self._risk_lbl2 = _ml()
        self._risk_lbl3 = _ml()
        self._risk_lbl4 = _ml()

        for w in [self._levels_lbl, self._sizing_lbl, self._opened_lbl,
                  self._reasons_lbl, self._risk_lbl1, self._risk_lbl2,
                  self._risk_lbl3, self._risk_lbl4]:
            detail_box.append(w)

        self._revealer.set_child(detail_box)
        self.append(self._revealer)

    def _on_close(self, _btn) -> None:
        if self._symbol:
            self._controller.close_symbol(self._symbol)

    def _on_toggle(self, _btn) -> None:
        self._expanded = not self._expanded
        self._revealer.set_reveal_child(self._expanded)
        self._expand_btn.set_label("▲ Ocultar" if self._expanded else "▼ Detalles")

    def _on_mode_toggle(self, _btn) -> None:
        if not self._symbol:
            return
        trade = self._controller._active.get(self._symbol)
        if trade:
            new_mode = (AutoMode.MANUAL
                        if trade.auto_mode == AutoMode.FULL_AUTO
                        else AutoMode.FULL_AUTO)
            self._controller.set_trade_mode(self._symbol, new_mode)
            # Feedback inmediato sin esperar el próximo _refresh
            if new_mode == AutoMode.FULL_AUTO:
                self._mode_btn.set_label("⏸ MANUAL")
                self._mode_btn.set_tooltip_text(
                    "AUTO activo — el sistema moverá SL a breakeven y aplicará trailing"
                )
            else:
                self._mode_btn.set_label("▶ AUTO")
                self._mode_btn.set_tooltip_text(
                    "Click para activar gestión automática (breakeven + trailing)"
                )

    def show_trade(self, trade: "TradeRecord", mark: float, upnl: float) -> None:
        self.set_visible(True)
        req = trade.request
        if not req:
            return

        self._symbol = trade.symbol
        sym   = trade.symbol.replace("USDT", "")
        col   = HEX["buy"] if req.side == "Buy" else HEX["sell"]
        arrow = "▲" if req.side == "Buy" else "▼"

        state_map = {
            TradeState.SUBMITTED: ("ESPERANDO",  "warn"),
            TradeState.OPEN:      ("OPEN",        "text"),
            TradeState.BREAKEVEN: ("BREAKEVEN ✓", "buy"),
            TradeState.TRAILING:  ("TRAILING ↑",  "teal"),
        }
        s_label, s_color = state_map.get(trade.state, ("??", "over"))

        self._sym_lbl.set_markup(
            f'<span color="{col}" weight="bold" size="large">{arrow} {sym}</span>'
        )
        self._state_lbl.set_markup(
            f'<span color="{HEX[s_color]}" weight="bold">{s_label}</span>'
        )

        # Botón de modo: muestra el estado actual y permite cambiarlo
        if trade.auto_mode == AutoMode.FULL_AUTO:
            self._mode_btn.set_label("⏸ MANUAL")
            self._mode_btn.set_tooltip_text("Click para volver a modo MANUAL")
        else:
            self._mode_btn.set_label("▶ AUTO")
            self._mode_btn.set_tooltip_text("Click para activar gestión automática (breakeven + trailing)")

        sign    = "+" if upnl >= 0 else ""
        pnl_col = HEX["buy"] if upnl >= 0 else HEX["sell"]
        self._pnl_lbl.set_markup(
            f'<span color="{pnl_col}" weight="bold" size="large">{sign}${upnl:.2f}</span>'
        )

        # Barra de progreso
        entry   = trade.entry_price or req.entry_price
        tp_dist = abs(req.tp_price - entry) if entry > 0 else 1
        if tp_dist > 0 and entry > 0:
            prog = ((mark - entry) / tp_dist if req.side == "Buy"
                    else (entry - mark) / tp_dist)
            frac = max(0.0, min(1.0, prog))
            goal_real = req.qty * tp_dist
            self._prog.set_fraction(frac)
            self._prog.set_text(f"{sign}${upnl:.2f} / ${goal_real:.2f}  ({frac*100:.0f}%)")
        else:
            self._prog.set_fraction(0.0)
            self._prog.set_text(f"entry {_fp(entry)}")

        # Duración (siempre visible en header)
        dur = _fmt_duration(trade.opened_at)
        eta = _estimate_ttp(entry, mark, req.tp_price, trade.opened_at, req.side)
        self._dur_lbl.set_markup(
            f'<span color="{HEX["sub"]}" size="small">⏱ {dur}</span>'
        )
        self._eta_lbl.set_markup(
            f'<span color="{HEX["teal"]}" size="small">→TP {eta}</span>'
        )

        # Apalancamiento y valor nocional
        lev = req.leverage if req.leverage else 1
        notional = req.qty * entry
        self._lev_lbl.set_markup(
            f'<span color="{HEX["warn"]}" size="small">{lev}x</span>'
        )
        self._notional_lbl.set_markup(
            f'<span color="{HEX["sub"]}" size="small">${notional:,.1f}</span>'
        )

        # Riesgo actual en SL: cuánto ganarías/perderías si el SL se activa ahora
        if trade.current_sl > 0 and entry > 0 and req.qty > 0:
            if req.side == "Buy":
                risk_now = req.qty * (trade.current_sl - entry)
            else:
                risk_now = req.qty * (entry - trade.current_sl)
            sign_r = "+" if risk_now >= 0 else ""
            risk_col = HEX["buy"] if risk_now >= 0 else HEX["sell"]
            self._risk_now_lbl.set_markup(
                f'<span color="{HEX["sub"]}" size="small">SL↓</span>'
                f'<span color="{risk_col}" size="small" weight="bold">{sign_r}${risk_now:.2f}</span>'
            )
        else:
            self._risk_now_lbl.set_text("")

        # Indicador de gestión automática (inline, compacto)
        if trade.auto_mode == AutoMode.FULL_AUTO:
            auto_map = {
                TradeState.OPEN:      ("🤖",  "blue"),
                TradeState.BREAKEVEN: ("🛡",  "buy"),
                TradeState.TRAILING:  ("📈",  "teal"),
            }
            icon, acol = auto_map.get(trade.state, ("🤖", "blue"))
            self._auto_lbl.set_markup(
                f'<span color="{HEX[acol]}" size="small">{icon} AUTO</span>'
            )
        else:
            self._auto_lbl.set_markup(
                f'<span color="{HEX["over"]}" size="small">MANUAL</span>'
            )

        # Detalle (siempre actualizado aunque no esté visible)
        self._levels_lbl.set_markup(
            f'<span color="{HEX["sub"]}">Entry </span><span color="{HEX["text"]}">{_fp(entry)}</span>'
            f'  <span color="{HEX["sell"]}">SL {_fp(trade.current_sl)}</span>'
            f'  <span color="{HEX["buy"]}">TP {_fp(req.tp_price)}</span>'
            f'  <span color="{HEX["sub"]}">R:R </span>'
            f'<span color="{HEX["buy"]}">{req.rr_ratio:.1f}:1</span>'
        )
        self._sizing_lbl.set_markup(
            f'<span color="{HEX["sub"]}">Qty </span><span color="{HEX["text"]}">{req.qty}</span>'
            f'  <span color="{HEX["sub"]}">Score </span>'
            f'<span color="{HEX["blue"]}">{req.opp_score}</span>'
            f'  <span color="{HEX["sub"]}">Meta </span>'
            f'<span color="{HEX["buy"]}">+${req.qty * abs(req.tp_price - entry):.2f}</span>'
            f'  <span color="{HEX["sub"]}">Riesgo </span>'
            f'<span color="{HEX["sell"]}">-${req.risk_usd:.2f}</span>'
        )

        opened_str = (time.strftime("%H:%M:%S", time.localtime(trade.opened_at))
                      if trade.opened_at else "──")
        self._opened_lbl.set_markup(
            f'<span color="{HEX["sub"]}">Abierto: </span>'
            f'<span color="{HEX["text"]}">{opened_str}</span>'
            f'  <span color="{HEX["sub"]}">Duración: </span>'
            f'<span color="{HEX["text"]}">{dur}</span>'
            f'  <span color="{HEX["sub"]}">Lev: </span>'
            f'<span color="{HEX["text"]}">{req.leverage}x</span>'
        )

        if req.reasons:
            self._reasons_lbl.set_markup(
                f'<span color="{HEX["over"]}" size="small">'
                + GLib.markup_escape_text("  ·  ".join(req.reasons))
                + "</span>"
            )
        else:
            self._reasons_lbl.set_text("")

        # Gráfico (siempre visible)
        self._chart.update(entry, trade.current_sl, req.tp_price, mark, req.side)

        # Análisis de riesgo
        risk_lines = _trade_risk_analysis(trade, None, mark)
        risk_lbls  = [self._risk_lbl1, self._risk_lbl2, self._risk_lbl3, self._risk_lbl4]
        for i, lbl in enumerate(risk_lbls):
            if i < len(risk_lines):
                lbl.set_markup(
                    f'<span color="{HEX["warn"]}" size="small">'
                    + GLib.markup_escape_text(risk_lines[i]) + "</span>"
                )
            else:
                lbl.set_text("")

        # Advertencia inline (primera línea crítica siempre visible)
        warn = next((l for l in risk_lines if l.startswith(("⚠", "⏱", "💸"))), "")
        if warn:
            self._warn_lbl.set_markup(
                f'<span color="{HEX["warn"]}" size="small">'
                + GLib.markup_escape_text(warn) + "</span>"
            )
            self._warn_lbl.set_visible(True)
        else:
            self._warn_lbl.set_visible(False)

    def clear(self) -> None:
        self.set_visible(False)
        self._symbol = ""


# ─── CommandCenter ────────────────────────────────────────────────────────────

class CommandCenter(Gtk.Box):
    """
    Pantalla principal de operaciones.
    Layout 2 columnas:  Izquierda = trades activos (scroll)
                        Derecha   = propuesta + simulación + journal (scroll)
    """

    def __init__(
        self,
        controller: "TradeController",
        strategy,
        executor,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_hexpand(True)
        self.set_vexpand(True)

        self._controller    = controller
        self._strategy      = strategy
        self._executor      = executor
        self._jnl_ts:       float = 0.0
        self._jnl_log_len:  int   = -1
        self._market_states: dict = {}
        self._hist_ts:      float = 0.0

        controller.on_update(self._on_controller_update)
        self._build()

    def _build(self) -> None:
        P = 8  # padding estándar

        def _make_spin(label: str, lo: float, hi: float, val: float,
                       step: float, digits: int, w: int = 68) -> tuple:
            lbl = Gtk.Label(label=label)
            lbl.add_css_class("qts-label")
            sp  = Gtk.SpinButton()
            sp.set_adjustment(Gtk.Adjustment(value=val, lower=lo, upper=hi,
                                             step_increment=step, page_increment=step*5))
            sp.set_digits(digits)
            sp.set_size_request(w, -1)
            return lbl, sp

        # ── Barra superior: balance + modo + parámetros + scan (todo en 2 filas)
        top = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        top.set_margin_start(P); top.set_margin_end(P)
        top.set_margin_top(3);   top.set_margin_bottom(2)

        # Fila A: balance (full width, tamaño pequeño)
        self._balance_lbl = _ml()
        self._balance_lbl.set_markup(f'<span color="{HEX["over"]}" size="small">Conectando…</span>')
        top.append(self._balance_lbl)

        # Fila B: modo pills + spinners + scan (todo en una fila)
        ctrl_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        ctrl_row.set_margin_top(2)

        self._mode_btns: dict[AutoMode, Gtk.ToggleButton] = {}
        mode_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                           spacing=0, css_classes=["symbol-group"])
        first: Optional[Gtk.ToggleButton] = None
        for mode, (label, _, desc) in MODE_META.items():
            btn = Gtk.ToggleButton(label=label)
            btn.set_active(mode == AutoMode.MANUAL)
            btn.set_tooltip_text(desc)
            if first is None:
                first = btn
            else:
                btn.set_group(first)
            btn.connect("toggled", self._on_mode_toggled, mode)
            self._mode_btns[mode] = btn
            mode_box.append(btn)
        ctrl_row.append(mode_box)

        vsep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        vsep.set_margin_start(4); vsep.set_margin_end(4)
        ctrl_row.append(vsep)

        gl, self._goal_spin  = _make_spin("$",  0.1, 500, 1.0,  0.5,  2, 60)
        ll, self._loss_spin  = _make_spin("↓$", 0.05, 500, 0.5, 0.25, 2, 60)
        lel, self._lev_spin  = _make_spin("x",  1,   25,  5,    1,    0, 42)
        dl, self._dur_spin   = _make_spin("⏱",  0,  480,  0,   15,    0, 50)
        ml, self._multi_spin = _make_spin("×",  1,   10,  1,    1,    0, 38)

        self._goal_spin.set_tooltip_text("Meta de ganancia en USD por trade")
        self._loss_spin.set_tooltip_text("Pérdida máxima aceptada por trade")
        self._lev_spin.set_tooltip_text("Apalancamiento")
        self._dur_spin.set_tooltip_text("Duración máxima del trade (0 = sin límite)")
        self._multi_spin.set_tooltip_text("Número de trades en paralelo para el objetivo")

        self._goal_spin.connect("value-changed",  self._on_goal_changed)
        self._loss_spin.connect("value-changed",  self._on_loss_changed)
        self._lev_spin.connect("value-changed",   self._on_lev_changed)
        self._dur_spin.connect("value-changed",   self._on_dur_changed)
        self._multi_spin.connect("value-changed", self._on_multi_changed)

        self._scan_btn = Gtk.Button(label="🔍 Scan")
        self._scan_btn.add_css_class("suggested-action")
        self._scan_btn.connect("clicked", lambda _: self._controller.force_scan())

        # modo_desc: inline, compacto
        self._mode_desc = _ml()
        self._dur_hint  = _ml()   # se actualiza en _on_dur_changed

        for w in [gl, self._goal_spin, ll, self._loss_spin,
                  lel, self._lev_spin, dl, self._dur_spin,
                  ml, self._multi_spin, self._scan_btn,
                  self._mode_desc, self._dur_hint]:
            ctrl_row.append(w)
        top.append(ctrl_row)
        self.append(top)
        self.append(_sep())

        # ── Área principal: 2 columnas ───────────────────────────────────
        main = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        main.set_vexpand(True)
        main.set_hexpand(True)

        # ── Columna izquierda: trades activos ────────────────────────────
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        left.set_hexpand(True)
        left.set_vexpand(True)

        # Header de trades
        trades_hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        trades_hdr.set_margin_start(P); trades_hdr.set_margin_end(P)
        trades_hdr.set_margin_top(4);   trades_hdr.set_margin_bottom(2)
        self._trades_title = _section("ACTIVOS (0)")
        self._total_pnl_lbl = _ml()
        self._total_pnl_lbl.set_hexpand(True)
        close_all_btn = Gtk.Button(label="✗ Todo")
        close_all_btn.add_css_class("destructive-action")
        close_all_btn.add_css_class("flat")
        close_all_btn.connect("clicked", lambda _: self._controller.close_now())
        trades_hdr.append(self._trades_title)
        trades_hdr.append(self._total_pnl_lbl)
        trades_hdr.append(close_all_btn)
        left.append(trades_hdr)

        self._no_trades_lbl = _ml()
        self._no_trades_lbl.set_markup(
            f'<span color="{HEX["over"]}" size="small">Sin trades activos</span>'
        )
        self._no_trades_lbl.set_margin_start(P)
        left.append(self._no_trades_lbl)

        # Scroll para las cards de trades
        left_scroll = Gtk.ScrolledWindow()
        left_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        left_scroll.set_vexpand(True)
        left_scroll.set_hexpand(True)
        left_scroll.set_propagate_natural_height(False)
        self._trade_cards: dict[str, TradeCard] = {}
        self._cards_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._cards_box.set_margin_start(P); self._cards_box.set_margin_end(P)
        left_scroll.set_child(self._cards_box)
        left.append(left_scroll)

        # Status bar al fondo de la columna izquierda
        self._status_lbl = _ml()
        self._status_lbl.set_margin_start(P)
        self._status_lbl.set_margin_bottom(4)
        left.append(self._status_lbl)

        main.append(left)

        # Separador vertical
        vsep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        main.append(vsep)

        # ── Columna derecha: propuesta + sim + journal ───────────────────
        right_scroll = Gtk.ScrolledWindow()
        right_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        right_scroll.set_vexpand(True)
        right_scroll.set_size_request(310, -1)
        right_scroll.set_hexpand(False)
        right_scroll.set_propagate_natural_height(False)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        right.set_margin_start(6); right.set_margin_end(6)
        right.set_margin_top(3);   right.set_margin_bottom(4)

        # Propuesta
        right.append(_section("PROPUESTA"))
        self._prop_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        self._prop_card.add_css_class("qts-card")
        self._prop_card.set_margin_bottom(4)
        self._prop_header = _ml()
        self._prop_levels = _ml()
        self._prop_sizing = _ml()
        self._prop_ttp    = _ml()
        self._prop_timer  = _ml()
        for w in [self._prop_header, self._prop_levels,
                  self._prop_sizing, self._prop_ttp, self._prop_timer]:
            self._prop_card.append(w)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        btn_row.set_margin_top(4)
        self._confirm_btn = Gtk.Button(label="✓ CONFIRMAR")
        self._confirm_btn.add_css_class("suggested-action")
        self._confirm_btn.set_hexpand(True)
        self._confirm_btn.connect("clicked", lambda _: self._controller.approve_proposal())
        self._reject_btn = Gtk.Button(label="✗ RECHAZAR")
        self._reject_btn.add_css_class("destructive-action")
        self._reject_btn.set_hexpand(True)
        self._reject_btn.connect("clicked", lambda _: self._controller.reject_proposal())
        btn_row.append(self._confirm_btn)
        btn_row.append(self._reject_btn)
        self._confirm_row = btn_row
        self._prop_card.append(btn_row)
        right.append(self._prop_card)
        right.append(_sep())

        # Simulación
        right.append(_section("SIMULACIÓN"))
        sim_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        sim_card.add_css_class("qts-card")
        sim_card.set_margin_bottom(4)
        self._sim_line1 = _ml()
        self._sim_line2 = _ml()
        self._sim_line3 = _ml()
        self._sim_warn  = _ml()
        for w in [self._sim_line1, self._sim_line2, self._sim_line3, self._sim_warn]:
            sim_card.append(w)
        right.append(sim_card)
        right.append(_sep())

        # Journal + historial
        right.append(_section("JOURNAL"))
        jnl_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        jnl_card.add_css_class("qts-card")
        self._jnl_line1 = _ml()
        self._jnl_line2 = _ml()
        self._jnl_line3 = _ml()
        for w in [self._jnl_line1, self._jnl_line2, self._jnl_line3]:
            jnl_card.append(w)
        jnl_card.append(_sep())
        self._hist_labels: list = [_ml() for _ in range(8)]
        for lbl in self._hist_labels:
            jnl_card.append(lbl)
        right.append(jnl_card)

        right_scroll.set_child(right)
        main.append(right_scroll)

        self.append(main)

        self._render_controller_state(self._controller.state)

    # ── Callbacks de controles ────────────────────────────────────────────────

    def _on_mode_toggled(self, btn: Gtk.ToggleButton, mode: AutoMode) -> None:
        if btn.get_active():
            self._controller.set_mode(mode)

    def _on_goal_changed(self, sp: Gtk.SpinButton) -> None:
        self._controller.set_goal(sp.get_value())

    def _on_loss_changed(self, sp: Gtk.SpinButton) -> None:
        self._controller.set_max_loss(sp.get_value())

    def _on_lev_changed(self, sp: Gtk.SpinButton) -> None:
        self._controller.set_leverage(int(sp.get_value()))

    def _on_multi_changed(self, sp: Gtk.SpinButton) -> None:
        self._controller.set_multi_trades(int(sp.get_value()))

    def _on_dur_changed(self, sp: Gtk.SpinButton) -> None:
        minutes = int(sp.get_value())
        self._controller.set_max_duration(minutes)
        if minutes > 0:
            self._dur_hint.set_markup(
                f'<span color="{HEX["warn"]}" size="small">⏱ máx {minutes}m</span>'
            )
        else:
            self._dur_hint.set_text("")

    def _on_controller_update(self, cs: ControllerState) -> None:
        GLib.idle_add(self._render_controller_state, cs)

    def _render_controller_state(self, cs: ControllerState) -> bool:
        self._render_mode(cs.mode)
        self._render_proposal(cs)
        self._render_log_for_journal()
        msg     = cs.status_msg
        msg_col = HEX["sell"] if msg.startswith("✗") else HEX["sub"]
        self._status_lbl.set_markup(
            f'<span color="{msg_col}" size="small">{GLib.markup_escape_text(msg)}</span>'
        )
        return False

    # ── Renderizado ───────────────────────────────────────────────────────────

    def _render_mode(self, mode: AutoMode) -> None:
        for m, btn in self._mode_btns.items():
            if btn.get_active() != (m == mode):
                btn.handler_block_by_func(self._on_mode_toggled)
                btn.set_active(m == mode)
                btn.handler_unblock_by_func(self._on_mode_toggled)
        _, ckey, _desc = MODE_META[mode]
        short = {"over": "monitoreo", "blue": "sugiere", "warn": "auto-entrada", "sell": "full-auto"}
        self._mode_desc.set_markup(
            f'<span color="{HEX[ckey]}" size="small">{short.get(ckey, "")}</span>'
        )

    def _render_proposal(self, cs: ControllerState) -> None:
        prop = cs.proposal
        if prop is None:
            self._prop_header.set_markup(
                f'<span color="{HEX["over"]}">Sin propuesta — escaneando oportunidades…</span>'
            )
            for w in [self._prop_levels, self._prop_sizing, self._prop_ttp, self._prop_timer]:
                w.set_text("")
            self._confirm_row.set_visible(False)
            return

        col   = HEX["buy"] if prop.side == "Buy" else HEX["sell"]
        arrow = "▲" if prop.side == "Buy" else "▼"
        sym   = prop.symbol.replace("USDT", "")
        goal_real = prop.qty * abs(prop.tp_price - prop.entry_price)

        self._prop_header.set_markup(
            f'<span color="{col}" weight="bold" size="large">{arrow} {prop.direction}  {sym}</span>'
            f'  <span color="{HEX["blue"]}">Score {prop.opp_score}/100</span>'
        )
        self._prop_levels.set_markup(
            f'<span color="{HEX["sub"]}">Entry </span><span color="{HEX["text"]}">{_fp(prop.entry_price)}</span>'
            f'  <span color="{HEX["sell"]}">SL {_fp(prop.sl_price)}</span>'
            f'  <span color="{HEX["buy"]}">TP {_fp(prop.tp_price)}</span>'
            f'  <span color="{HEX["sub"]}">R:R </span><span color="{HEX["buy"]}">{prop.rr_ratio:.1f}:1</span>'
        )
        self._prop_sizing.set_markup(
            f'<span color="{HEX["sub"]}">Qty </span><span color="{HEX["text"]}">{prop.qty}</span>'
            f'  <span color="{HEX["sub"]}">Notional </span><span color="{HEX["text"]}">${prop.notional:.1f}</span>'
            f'  <span color="{HEX["sub"]}">Margen </span><span color="{HEX["text"]}">${prop.margin:.2f}</span>'
            f'  <span color="{HEX["buy"]}">Goal +${goal_real:.2f}</span>'
            f'  <span color="{HEX["sell"]}">Riesgo -${prop.risk_usd:.2f}</span>'
        )

        # Tiempo estimado para alcanzar el TP
        ttp = _estimate_proposal_ttp(
            prop.symbol, prop.entry_price, prop.tp_price, self._market_states
        )
        max_dur = self._controller.max_duration_min
        ttp_col = HEX["teal"]
        ttp_warn = ""
        if ttp != "──" and max_dur > 0:
            # Check if estimate exceeds max_duration
            try:
                # rough parse of ttp string to minutes
                raw = ttp.lstrip("~>")
                if "h" in raw:
                    parts = raw.split("h")
                    est_min = int(parts[0]) * 60 + (int(parts[1].replace("m", "")) if parts[1].replace("m", "") else 0)
                else:
                    est_min = int(raw.replace("m", "").replace("s", "")) // (1 if "s" in ttp else 1)
                    if "s" in raw and "m" not in raw:
                        est_min = 1
                if est_min > max_dur:
                    ttp_col  = HEX["warn"]
                    ttp_warn = f"  ⚠ más de {max_dur}m"
            except Exception:
                pass
        self._prop_ttp.set_markup(
            f'<span color="{HEX["sub"]}" size="small">⏱ TP est. </span>'
            f'<span color="{ttp_col}" size="small" weight="bold">{ttp}{ttp_warn}</span>'
        )

        ttl = 60 - cs.proposal_age_s
        self._prop_timer.set_markup(
            f'<span color="{HEX["warn"] if ttl < 20 else HEX["over"]}" size="small">'
            f'Expira en {ttl}s</span>'
        )
        self._confirm_row.set_visible(cs.mode == AutoMode.SUGGEST)

    def _render_journal(self) -> None:
        stats = get_journal_stats()
        total = stats["total"]
        if total == 0:
            self._jnl_line1.set_markup(f'<span color="{HEX["over"]}">Sin trades registrados aún</span>')
            self._jnl_line2.set_text("")
            self._jnl_line3.set_text("")
        else:
            wr_col  = HEX["buy"] if stats["win_rate"] >= 50 else HEX["sell"]
            pnl_col = HEX["buy"] if stats["total_pnl"] >= 0 else HEX["sell"]
            sign    = "+" if stats["total_pnl"] >= 0 else ""
            self._jnl_line1.set_markup(
                f'<span color="{HEX["sub"]}">W: </span>'
                f'<span color="{wr_col}" weight="bold">{stats["wins"]}/{total}  ({stats["win_rate"]}%)</span>'
            )
            self._jnl_line2.set_markup(
                f'<span color="{HEX["sub"]}">PnL: </span>'
                f'<span color="{pnl_col}" weight="bold">{sign}${stats["total_pnl"]}</span>'
                f'  <span color="{HEX["sub"]}">avg </span>'
                f'<span color="{HEX["text"]}">{sign}${stats["avg_pnl"]}</span>'
            )
            self._jnl_line3.set_markup(
                f'<span color="{HEX["sub"]}">Best: </span>'
                f'<span color="{HEX["teal"]}">{stats["best_symbol"]}</span>'
                f'  <span color="{HEX["sub"]}">R:R: </span>'
                f'<span color="{HEX["text"]}">{stats["avg_rr"]}</span>'
            )

        # Historial reciente
        now = time.monotonic()
        if (now - self._hist_ts) > 15:
            self._hist_ts = now
            recent = get_recent_trades(limit=8)
            for i, lbl in enumerate(self._hist_labels):
                if i >= len(recent):
                    lbl.set_text("")
                    continue
                t     = recent[i]
                sym   = t["symbol"].replace("USDT", "")
                arrow = "▲" if t["side"] == "Buy" else "▼"
                pnl   = t["pnl_usd"]
                pcol  = HEX["buy"] if pnl >= 0 else HEX["sell"]
                sign  = "+" if pnl >= 0 else ""
                ts_str = (time.strftime("%m/%d %H:%M", time.localtime(t["closed_at"]))
                          if t["closed_at"] > 0 else "──")
                dur   = _fmt_duration_s(t["duration_s"])
                reason = t["close_reason"] or t["state"]
                lbl.set_markup(
                    f'<span color="{pcol}" size="small">{arrow} {sym}</span>'
                    f'  <span color="{pcol}" weight="bold" size="small">{sign}${pnl:.2f}</span>'
                    f'  <span color="{HEX["sub"]}" size="small">{dur}  {ts_str}'
                    + (f'  {GLib.markup_escape_text(reason)}' if reason not in ("CLOSED", "SL/TP/manual") else "")
                    + "</span>"
                )

    def _render_log_for_journal(self) -> None:
        log_entries = self._controller.trade_log
        now = time.monotonic()
        if (len(log_entries) != self._jnl_log_len or
                (now - self._jnl_ts) > 10):
            self._jnl_log_len = len(log_entries)
            self._jnl_ts = now
            self._render_journal()

    # ── Update (llamado desde _refresh cada 100ms) ────────────────────────────

    def update(
        self,
        account:       "AccountState",
        risk:          "RiskStatus",
        sim:           Optional[dict] = None,
        market_states: Optional[dict] = None,
    ) -> None:
        self._market_states = market_states or {}
        self._render_balance(account)
        self._render_active_trades(account, self._market_states)
        self._render_simulation(sim)

    def _render_balance(self, account: "AccountState") -> None:
        bal = account.balance
        if not account.connected or bal.total_equity <= 0:
            self._balance_lbl.set_markup(
                f'<span color="{HEX["over"]}" size="small">Cuenta: conectando…</span>'
            )
            return
        dpnl_col  = HEX["buy"] if account.daily_pnl >= 0 else HEX["sell"]
        dpnl_sign = "+" if account.daily_pnl >= 0 else ""
        self._balance_lbl.set_markup(
            f'<span color="{HEX["sub"]}" size="small">Equity </span>'
            f'<span color="{HEX["text"]}" weight="bold" size="small">${bal.total_equity:.2f}</span>'
            f'  <span color="{HEX["sub"]}" size="small">Disp </span>'
            f'<span color="{HEX["teal"]}" size="small">${bal.available_balance:.2f}</span>'
            f'  <span color="{HEX["sub"]}" size="small">Margen </span>'
            f'<span color="{HEX["warn"]}" size="small">${bal.used_margin:.2f}</span>'
            f'  <span color="{HEX["sub"]}" size="small">PnL día </span>'
            f'<span color="{dpnl_col}" weight="bold" size="small">'
            f'{dpnl_sign}${account.daily_pnl:.2f}</span>'
        )

    def _render_active_trades(self, account: "AccountState", market_states: dict) -> None:
        actives = list(self._controller._active.values())
        n = len(actives)
        active_syms = {t.symbol for t in actives}

        # Eliminar cards de trades ya cerrados
        for sym in list(self._trade_cards.keys()):
            if sym not in active_syms:
                card = self._trade_cards.pop(sym)
                self._cards_box.remove(card)

        total_upnl = 0.0

        # Crear/actualizar cards
        for trade in actives:
            sym = trade.symbol
            if sym not in self._trade_cards:
                card = TradeCard(self._controller)
                self._trade_cards[sym] = card
                self._cards_box.append(card)

            pos = account.positions.get(sym)

            # Precio en tiempo real: usar ticker del MarketStream (actualiza con cada trade)
            # Fallback: mark_price de la posición (solo se actualiza en cambios de posición)
            ms   = market_states.get(sym)
            mark = ms.ticker.last_price if (ms and ms.ticker.last_price > 0) else (
                   pos.mark_price if pos and pos.mark_price > 0 else (
                   pos.entry_price if pos else 0.0))

            # PnL no realizado en tiempo real calculado desde el ticker
            if pos and pos.size > 0 and mark > 0:
                if pos.side == "Buy":
                    upnl = (mark - pos.entry_price) * pos.size
                else:
                    upnl = (pos.entry_price - mark) * pos.size
            else:
                upnl = pos.unrealized_pnl if pos else 0.0

            total_upnl += upnl
            self._trade_cards[sym].show_trade(trade, mark, upnl)

        # Actualizar encabezado
        self._trades_title.set_text(f"ACTIVOS ({n})")
        self._no_trades_lbl.set_visible(n == 0)

        if n > 0:
            sign    = "+" if total_upnl >= 0 else ""
            col     = HEX["buy"] if total_upnl >= 0 else HEX["sell"]
            self._total_pnl_lbl.set_markup(
                f'<span color="{HEX["sub"]}" size="small">PnL total </span>'
                f'<span color="{col}" weight="bold">{sign}${total_upnl:.2f}</span>'
            )
        else:
            self._total_pnl_lbl.set_text("")

    def _render_simulation(self, sim: Optional[dict]) -> None:
        if not sim or "error" in sim:
            msg = sim.get("error", "esperando datos…") if sim else "esperando datos…"
            self._sim_line1.set_markup(f'<span color="{HEX["over"]}">{msg}</span>')
            self._sim_line2.set_text("")
            self._sim_line3.set_text("")
            self._sim_warn.set_text("")
            return

        sym         = sim.get("symbol", "??").replace("USDT", "")
        real_profit = sim["real_profit"]
        real_loss   = sim["real_loss"]
        goal        = sim["goal_requested"]
        is_capped   = sim["is_capped"]

        self._sim_line1.set_markup(
            f'<span color="{HEX["blue"]}">{sym}</span>'
            f'  <span color="{HEX["sub"]}">Entry≈</span>'
            f'<span color="{HEX["text"]}">{_fp(sim["entry"])}</span>'
            f'  <span color="{HEX["sub"]}">R:R </span>'
            f'<span color="{HEX["buy"] if sim["rr"] >= 2 else HEX["warn"]}">{sim["rr"]:.1f}:1</span>'
        )
        self._sim_line2.set_markup(
            f'<span color="{HEX["sub"]}">Qty </span><span color="{HEX["text"]}">{sim["qty"]}</span>'
            f'  <span color="{HEX["sub"]}">Margen </span><span color="{HEX["text"]}">${sim["margin"]}</span>'
        )
        self._sim_line3.set_markup(
            f'<span color="{HEX["buy"]}" weight="bold">✓ +${real_profit:.2f}</span>'
            f'  <span color="{HEX["sell"]}" weight="bold">✗ -${real_loss:.2f}</span>'
        )

        if is_capped and real_profit < goal * 0.8:
            gap = sim.get("equity_gap", 0)
            warn = f"⚠ Meta ${goal:.2f} → alcanzable ${real_profit:.2f}  (${gap:.2f} más de equity)"
            self._sim_warn.set_markup(
                f'<span color="{HEX["warn"]}" size="small">{GLib.markup_escape_text(warn)}</span>'
            )
        elif real_profit >= goal * 0.95:
            self._sim_warn.set_markup(
                f'<span color="{HEX["buy"]}" size="small">✓ Meta alcanzable</span>'
            )
        else:
            self._sim_warn.set_text("")
