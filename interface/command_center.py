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

from core.order_model import AutoMode, TradeState, ControllerState, MAX_POSITIONS
from core.db import get_journal_stats

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
    if elapsed < 60:
        return f"{elapsed}s"
    elif elapsed < 3600:
        return f"{elapsed // 60}m {elapsed % 60}s"
    else:
        h = elapsed // 3600
        m = (elapsed % 3600) // 60
        return f"{h}h {m}m"


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
    if elapsed_min > 30 and prog_pct < 20:
        lines.append(f"⚠ {int(elapsed_min)}min y solo {prog_pct:.0f}% de progreso — mercado lento")
        lines.append("  → Considera aumentar el límite de tiempo o ajustar el SL")
    elif elapsed_min > 60 and prog_pct < 50:
        lines.append(f"⏱ {int(elapsed_min)}min transcurridos — progreso moderado ({prog_pct:.0f}%)")
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
        self.set_size_request(-1, 72)
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
        lo  = min(self._sl, self._mark) * 0.9985
        hi  = max(self._tp, self._mark) * 1.0015
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
        # ── Summary (siempre visible) ──────────────────────────────────
        summary = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        summary.set_margin_start(8); summary.set_margin_end(8)
        summary.set_margin_top(6);   summary.set_margin_bottom(4)

        self._sym_lbl   = _ml()
        self._state_lbl = _ml()
        self._pnl_lbl   = _ml()
        self._time_lbl  = _ml()
        self._sym_lbl.set_hexpand(True)

        close_btn = Gtk.Button(label="✗")
        close_btn.add_css_class("destructive-action")
        close_btn.set_size_request(32, -1)
        close_btn.connect("clicked", self._on_close)

        expand_btn = Gtk.Button(label="▼ Detalles")
        expand_btn.add_css_class("flat")
        expand_btn.connect("clicked", self._on_toggle)
        self._expand_btn = expand_btn

        summary.append(self._sym_lbl)
        summary.append(self._state_lbl)
        summary.append(self._pnl_lbl)
        summary.append(expand_btn)
        summary.append(close_btn)
        self.append(summary)

        # Barra de progreso
        self._prog = Gtk.ProgressBar()
        self._prog.set_show_text(True)
        self._prog.set_margin_start(8); self._prog.set_margin_end(8)
        self._prog.set_margin_bottom(4)
        self.append(self._prog)

        # Tiempo + ETA
        time_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        time_row.set_margin_start(8); time_row.set_margin_end(8)
        time_row.set_margin_bottom(6)
        self._dur_lbl = _ml()
        self._eta_lbl = _ml()
        time_row.append(self._dur_lbl)
        time_row.append(self._eta_lbl)
        self.append(time_row)

        # ── Detalle (revealer) ─────────────────────────────────────────
        self._revealer = Gtk.Revealer()
        self._revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self._revealer.set_transition_duration(200)

        detail_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        detail_box.set_margin_start(8); detail_box.set_margin_end(8)
        detail_box.set_margin_bottom(8)
        detail_box.append(_sep())

        # Niveles
        self._levels_lbl  = _ml()
        self._sizing_lbl  = _ml()
        self._opened_lbl  = _ml()
        self._reasons_lbl = _ml()
        self._reasons_lbl.set_wrap(True)
        self._reasons_lbl.set_max_width_chars(50)

        # Gráfico
        self._chart = TradePriceChart()

        # Análisis de riesgo
        self._risk_lbl1 = _ml()
        self._risk_lbl2 = _ml()
        self._risk_lbl3 = _ml()
        self._risk_lbl4 = _ml()

        for w in [self._levels_lbl, self._sizing_lbl, self._opened_lbl,
                  self._chart,
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

        # Tiempo
        dur = _fmt_duration(trade.opened_at)
        eta = _estimate_ttp(entry, mark, req.tp_price, trade.opened_at, req.side)
        self._dur_lbl.set_markup(
            f'<span color="{HEX["sub"]}">⏱ </span>'
            f'<span color="{HEX["text"]}">{dur}</span>'
        )
        self._eta_lbl.set_markup(
            f'<span color="{HEX["sub"]}">🎯 TP est. </span>'
            f'<span color="{HEX["teal"]}">{eta}</span>'
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

        # Gráfico
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

    def clear(self) -> None:
        self.set_visible(False)
        self._symbol = ""


# ─── CommandCenter ────────────────────────────────────────────────────────────

class CommandCenter(Gtk.ScrolledWindow):
    """Pantalla principal de operaciones. Se usa como primera pestaña."""

    def __init__(
        self,
        controller: "TradeController",
        strategy,
        executor,
    ) -> None:
        super().__init__()
        self.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.set_hexpand(True)
        self.set_vexpand(True)

        self._controller = controller
        self._strategy   = strategy
        self._executor   = executor
        self._jnl_ts: float = 0.0
        self._jnl_log_len: int = -1

        controller.on_update(self._on_controller_update)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        inner.set_margin_start(16)
        inner.set_margin_end(16)
        inner.set_margin_top(8)
        inner.set_margin_bottom(16)
        self.set_child(inner)
        self._inner = inner

        self._build()

    def _build(self) -> None:
        inner = self._inner

        # ── Modo ────────────────────────────────────────────────────────
        inner.append(_section("MODO"))
        self._mode_btns: dict[AutoMode, Gtk.ToggleButton] = {}
        mode_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                           spacing=0, css_classes=["symbol-group"])
        first: Optional[Gtk.ToggleButton] = None
        for mode, (label, _, _desc) in MODE_META.items():
            btn = Gtk.ToggleButton(label=label)
            btn.set_active(mode == AutoMode.MANUAL)
            if first is None:
                first = btn
            else:
                btn.set_group(first)
            btn.connect("toggled", self._on_mode_toggled, mode)
            self._mode_btns[mode] = btn
            mode_box.append(btn)
        inner.append(mode_box)

        self._mode_desc = _ml()
        inner.append(self._mode_desc)
        inner.append(_sep())

        # ── Objetivo + Controles ─────────────────────────────────────────
        inner.append(_section("OBJETIVO"))
        goal_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        goal_row.set_margin_bottom(4)

        def _make_spin(label: str, lo: float, hi: float, val: float,
                       step: float, digits: int, w: int = 80) -> tuple:
            lbl = Gtk.Label(label=label)
            lbl.add_css_class("qts-label")
            sp  = Gtk.SpinButton()
            sp.set_adjustment(Gtk.Adjustment(value=val, lower=lo, upper=hi,
                                             step_increment=step, page_increment=step*5))
            sp.set_digits(digits)
            sp.set_size_request(w, -1)
            return lbl, sp

        gl, self._goal_spin = _make_spin("Ganar $", 0.1, 500, 1.0, 0.5, 2)
        ll, self._loss_spin = _make_spin("Perder máx $", 0.05, 500, 0.5, 0.25, 2)
        lel, self._lev_spin = _make_spin("Lev", 1, 25, 5, 1, 0, 55)
        dl, self._dur_spin  = _make_spin("Dur máx", 0, 480, 0, 15, 0, 65)
        dur_note = Gtk.Label(label="min")
        dur_note.add_css_class("qts-label")

        self._goal_spin.connect("value-changed", self._on_goal_changed)
        self._loss_spin.connect("value-changed", self._on_loss_changed)
        self._lev_spin.connect("value-changed",  self._on_lev_changed)
        self._dur_spin.connect("value-changed",  self._on_dur_changed)

        self._scan_btn = Gtk.Button(label="🔍 SCAN AHORA")
        self._scan_btn.add_css_class("suggested-action")
        self._scan_btn.connect("clicked", lambda _: self._controller.force_scan())

        for w in [gl, self._goal_spin, ll, self._loss_spin,
                  lel, self._lev_spin, dl, self._dur_spin, dur_note,
                  self._scan_btn]:
            goal_row.append(w)
        inner.append(goal_row)

        self._dur_hint = _ml()
        inner.append(self._dur_hint)
        inner.append(_sep())

        # ── Trades activos ───────────────────────────────────────────────
        trades_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._trades_title = _section(f"TRADES ACTIVOS (0/{MAX_POSITIONS})")
        trades_header.append(self._trades_title)
        close_all_btn = Gtk.Button(label="✗ Cerrar todo")
        close_all_btn.add_css_class("destructive-action")
        close_all_btn.add_css_class("flat")
        close_all_btn.connect("clicked", lambda _: self._controller.close_now())
        trades_header.append(close_all_btn)
        inner.append(trades_header)

        self._trade_cards: list[TradeCard] = []
        for _ in range(MAX_POSITIONS):
            card = TradeCard(self._controller)
            card.clear()
            self._trade_cards.append(card)
            inner.append(card)

        self._no_trades_lbl = _ml()
        self._no_trades_lbl.set_markup(
            f'<span color="{HEX["over"]}">Sin trades activos</span>'
        )
        inner.append(self._no_trades_lbl)
        inner.append(_sep())

        # ── Propuesta ────────────────────────────────────────────────────
        inner.append(_section("PROPUESTA"))
        self._prop_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._prop_card.add_css_class("qts-card")
        self._prop_card.set_margin_bottom(4)

        self._prop_header  = _ml()
        self._prop_levels  = _ml()
        self._prop_sizing  = _ml()
        self._prop_timer   = _ml()
        for w in [self._prop_header, self._prop_levels,
                  self._prop_sizing, self._prop_timer]:
            self._prop_card.append(w)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
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
        inner.append(self._prop_card)
        inner.append(_sep())

        # ── Simulación + Journal (2 columnas) ────────────────────────────
        bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        bottom.set_margin_top(4)

        sim_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        sim_box.add_css_class("qts-card")
        sim_box.set_hexpand(True)
        sim_box.append(_section("SIMULACIÓN"))
        self._sim_line1 = _ml()
        self._sim_line2 = _ml()
        self._sim_line3 = _ml()
        self._sim_warn  = _ml()
        for w in [self._sim_line1, self._sim_line2, self._sim_line3, self._sim_warn]:
            sim_box.append(w)

        jnl_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        jnl_box.add_css_class("qts-card")
        jnl_box.set_hexpand(True)
        jnl_box.append(_section("JOURNAL"))
        self._jnl_line1 = _ml()
        self._jnl_line2 = _ml()
        self._jnl_line3 = _ml()
        for w in [self._jnl_line1, self._jnl_line2, self._jnl_line3]:
            jnl_box.append(w)

        bottom.append(sim_box)
        bottom.append(jnl_box)
        inner.append(bottom)
        inner.append(_sep())

        # ── Status ───────────────────────────────────────────────────────
        self._status_lbl = _ml()
        inner.append(self._status_lbl)

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

    def _on_dur_changed(self, sp: Gtk.SpinButton) -> None:
        minutes = int(sp.get_value())
        self._controller.set_max_duration(minutes)
        if minutes > 0:
            self._dur_hint.set_markup(
                f'<span color="{HEX["warn"]}" size="small">'
                f'⏱ Posiciones se cerrarán automáticamente si no alcanzan TP en {minutes} min</span>'
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
        _, ckey, desc = MODE_META[mode]
        self._mode_desc.set_markup(
            f'<span color="{HEX[ckey]}" size="small">{desc}</span>'
        )

    def _render_proposal(self, cs: ControllerState) -> None:
        prop = cs.proposal
        if prop is None:
            self._prop_header.set_markup(
                f'<span color="{HEX["over"]}">Sin propuesta — escaneando oportunidades…</span>'
            )
            for w in [self._prop_levels, self._prop_sizing, self._prop_timer]:
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
            return
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
            f'  <span color="{HEX["sub"]}">AvgScore: </span>'
            f'<span color="{HEX["text"]}">{stats["avg_score"]}</span>'
            f'  <span color="{HEX["sub"]}">R:R: </span>'
            f'<span color="{HEX["text"]}">{stats["avg_rr"]}</span>'
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
        account: "AccountState",
        risk:    "RiskStatus",
        sim:     Optional[dict] = None,
    ) -> None:
        self._render_active_trades(account)
        self._render_simulation(sim)

    def _render_active_trades(self, account: "AccountState") -> None:
        actives = list(self._controller._active.values())
        n = len(actives)

        # Actualizar título de la sección
        self._trades_title.set_text(f"TRADES ACTIVOS ({n}/{MAX_POSITIONS})")
        self._no_trades_lbl.set_visible(n == 0)

        for i, card in enumerate(self._trade_cards):
            if i < len(actives):
                trade = actives[i]
                sym   = trade.symbol
                pos   = account.positions.get(sym)
                mark  = pos.mark_price if pos and pos.mark_price > 0 else (
                    pos.entry_price if pos else 0.0)
                upnl  = pos.unrealized_pnl if pos else 0.0
                card.show_trade(trade, mark, upnl)
            else:
                card.clear()

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
