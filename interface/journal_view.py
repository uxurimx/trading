"""
interface/journal_view.py
──────────────────────────
JournalView — Historial completo de trades con equity curve y estadísticas.
"""
from __future__ import annotations

import math
import time
from typing import List

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk, Pango

from core.db import get_journal_stats, get_all_trades, get_cumulative_pnl


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


def _ml(text: str = "") -> Gtk.Label:
    lbl = Gtk.Label(label=text)
    lbl.set_xalign(0)
    lbl.set_use_markup(True)
    lbl.add_css_class("qts-mono-sm")
    lbl.set_max_width_chars(120)
    lbl.set_ellipsize(Pango.EllipsizeMode.END)
    lbl.set_wrap(False)
    return lbl


def _sep() -> Gtk.Separator:
    s = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
    s.add_css_class("qts-sep")
    return s


def _fp(p: float) -> str:
    if p <= 0:    return "──"
    if p >= 1000: return f"{p:,.1f}"
    if p >= 10:   return f"{p:.3f}"
    return f"{p:.4f}"


def _dur(seconds: int) -> str:
    if seconds <= 0: return "──"
    if seconds < 60: return f"{seconds}s"
    if seconds < 3600: return f"{seconds//60}m"
    if seconds < 86400:
        h = seconds // 3600; m = (seconds % 3600) // 60
        return f"{h}h{m:02d}m"
    d = seconds // 86400; h = (seconds % 86400) // 3600
    return f"{d}d{h}h"


# ─── Equity Curve Chart ───────────────────────────────────────────────────────

class EquityChart(Gtk.DrawingArea):
    """Gráfico de equity curve (PnL acumulado) con Cairo."""

    def __init__(self) -> None:
        super().__init__()
        self.set_size_request(-1, 90)
        self.set_hexpand(True)
        self._points: List[tuple] = []
        self.set_draw_func(self._draw)

    def update(self, points: List[tuple]) -> None:
        self._points = points
        self.queue_draw()

    def _draw(self, _area, cr, w: int, h: int) -> None:
        cr.set_source_rgba(*RGB["card"], 1.0)
        cr.rectangle(0, 0, w, h)
        cr.fill()

        if len(self._points) < 2:
            cr.set_source_rgba(*RGB["surf"], 0.4)
            cr.set_line_width(1)
            cr.move_to(0, h / 2); cr.line_to(w, h / 2); cr.stroke()
            return

        values = [p[1] for p in self._points]
        lo = min(values); hi = max(values)
        span = hi - lo or 0.01
        pad = 8

        def px(i: int, v: float) -> tuple:
            x = pad + i / (len(values) - 1) * (w - 2 * pad)
            y = pad + (1 - (v - lo) / span) * (h - 2 * pad)
            return x, y

        # Línea cero
        if lo <= 0 <= hi:
            zy = pad + (1 - (0 - lo) / span) * (h - 2 * pad)
            cr.set_source_rgba(*RGB["surf"], 0.6)
            cr.set_line_width(0.8)
            cr.move_to(pad, zy); cr.line_to(w - pad, zy); cr.stroke()

        final = values[-1]
        col   = RGB["buy"] if final >= 0 else RGB["sell"]

        # Relleno
        cr.set_source_rgba(*col, 0.15)
        x0, y0 = px(0, values[0])
        cr.move_to(x0, y0)
        for i, v in enumerate(values):
            cr.line_to(*px(i, v))
        xl, _ = px(len(values) - 1, values[-1])
        zero_y = max(pad, min(h - pad, pad + (1 - (0 - lo) / span) * (h - 2 * pad)))
        cr.line_to(xl, zero_y); cr.line_to(x0, zero_y)
        cr.close_path(); cr.fill()

        # Línea principal
        cr.set_source_rgba(*col, 0.95)
        cr.set_line_width(1.8)
        for i, v in enumerate(values):
            x, y = px(i, v)
            cr.move_to(x, y) if i == 0 else cr.line_to(x, y)
        cr.stroke()

        # Punto final
        xf, yf = px(len(values) - 1, values[-1])
        cr.set_source_rgba(*col, 1.0)
        cr.arc(xf, yf, 4, 0, 2 * math.pi); cr.fill()

        # Etiqueta PnL final
        cr.set_font_size(10)
        sign = "+" if final >= 0 else ""
        label = f"{sign}${final:.2f}"
        ext = cr.text_extents(label)
        tx = max(2, min(w - ext[2] - 4, xf - ext[2] / 2))
        ty = max(12, yf - 6)
        cr.set_source_rgba(*col, 0.9)
        cr.move_to(tx, ty); cr.show_text(label)


# ─── JournalView ──────────────────────────────────────────────────────────────

class JournalView(Gtk.Box):
    """
    Pestaña de Journal: equity curve + estadísticas + tabla de trades.
    """

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self._last_refresh: float = 0.0
        self._row_labels:   list  = []
        self._build()

    def _build(self) -> None:
        P = 8

        # ── Header con stats resumidas ────────────────────────────────────────
        hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        hdr.set_margin_start(P); hdr.set_margin_end(P)
        hdr.set_margin_top(6);   hdr.set_margin_bottom(2)

        self._stat1 = _ml()
        self._stat1.set_hexpand(True)
        self._stat2 = _ml()
        self._stat2.set_hexpand(True)
        hdr.append(self._stat1)
        hdr.append(self._stat2)
        self.append(hdr)

        # ── Equity curve ─────────────────────────────────────────────────────
        self._chart = EquityChart()
        self._chart.set_margin_start(P); self._chart.set_margin_end(P)
        self._chart.set_margin_bottom(4)
        self.append(self._chart)

        self.append(_sep())

        # ── Cabecera de columnas ──────────────────────────────────────────────
        col_hdr = _ml()
        col_hdr.set_margin_start(P); col_hdr.set_margin_end(P)
        col_hdr.set_markup(
            f'<span color="{HEX["over"]}" size="small" font_family="monospace">'
            f'{"#":>3}  {"SYM":<7} {"LADO":^6} {"PnL":>8}  '
            f'{"RAZÓN":<14} {"DUR":>7}  '
            f'{"ENTRADA":>10} {"SL":>10} {"TP":>10}  '
            f'{"R:R":>5} {"SCR":>4}  HORA'
            f'</span>'
        )
        self.append(col_hdr)
        self.append(_sep())

        # ── Tabla de trades (scroll) ──────────────────────────────────────────
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.set_propagate_natural_height(False)

        self._rows_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._rows_box.set_margin_start(P); self._rows_box.set_margin_end(P)
        scroll.set_child(self._rows_box)
        self.append(scroll)

    # ── Actualización ─────────────────────────────────────────────────────────

    def refresh(self) -> None:
        now = time.time()
        if (now - self._last_refresh) < 8.0:
            return
        self._last_refresh = now

        stats  = get_journal_stats()
        trades = get_all_trades(150)
        curve  = get_cumulative_pnl()

        self._update_stats(stats, len(trades))
        self._chart.update(curve)
        self._update_trades(trades)

    def _update_stats(self, s: dict, n_loaded: int) -> None:
        total  = s.get("total", 0)
        wins   = s.get("wins", 0)
        losses = s.get("losses", 0)
        wr     = s.get("win_rate", 0.0)
        tpnl   = s.get("total_pnl", 0.0)
        best   = s.get("best_trade", 0.0)
        worst  = s.get("worst_trade", 0.0)
        avg_rr = s.get("avg_rr", 0.0)
        avg_sc = s.get("avg_score", 0.0)
        bsym   = s.get("best_symbol", "──")

        pnl_col = HEX["buy"] if tpnl >= 0 else HEX["sell"]
        wr_col  = HEX["buy"] if wr >= 50 else HEX["sell"]

        self._stat1.set_markup(
            f'<span color="{HEX["sub"]}">Trades: </span>'
            f'<span color="{HEX["text"]}" weight="bold">{total}</span>'
            f'  <span color="{HEX["buy"]}">{wins}W</span>'
            f' <span color="{HEX["sell"]}">{losses}L</span>'
            f'  <span color="{HEX["sub"]}">WR: </span>'
            f'<span color="{wr_col}" weight="bold">{wr:.0f}%</span>'
            f'  <span color="{HEX["sub"]}">PnL: </span>'
            f'<span color="{pnl_col}" weight="bold">${tpnl:+.2f}</span>'
        )
        self._stat2.set_markup(
            f'<span color="{HEX["sub"]}">Mejor: </span>'
            f'<span color="{HEX["buy"]}">+${best:.2f}</span>'
            f'  <span color="{HEX["sub"]}">Peor: </span>'
            f'<span color="{HEX["sell"]}">${worst:.2f}</span>'
            f'  <span color="{HEX["sub"]}">R:R: </span>'
            f'<span color="{HEX["teal"]}">{avg_rr:.1f}</span>'
            f'  <span color="{HEX["sub"]}">Score: </span>'
            f'<span color="{HEX["blue"]}">{avg_sc:.0f}</span>'
            f'  <span color="{HEX["sub"]}">Top: </span>'
            f'<span color="{HEX["warn"]}">{bsym}</span>'
        )

    def _update_trades(self, trades: list) -> None:
        # Reconstruir filas (max 150 trades, muy rápido)
        needed = len(trades)
        current = len(self._row_labels)

        # Añadir labels que faltan
        while len(self._row_labels) < needed:
            lbl = _ml()
            lbl.set_margin_bottom(1)
            self._rows_box.append(lbl)
            self._row_labels.append(lbl)

        # Ocultar labels sobrantes
        for i in range(needed, current):
            self._row_labels[i].set_visible(False)

        for i, t in enumerate(trades):
            lbl = self._row_labels[i]
            lbl.set_visible(True)
            lbl.set_markup(self._trade_markup(i + 1, t))

    def _trade_markup(self, n: int, t: dict) -> str:
        sym    = t["symbol"].replace("USDT", "")
        side   = t["side"] or "?"
        pnl    = t["pnl_usd"]
        reason = (t["close_reason"] or t["state"] or "?")[:13]
        dur    = _dur(t["duration_s"])
        entry  = _fp(t["entry_price"])
        sl     = _fp(t["sl_price"])
        tp_p   = _fp(t["tp_price"])
        rr     = t["rr_ratio"]
        score  = t["opp_score"]
        ts     = t["closed_at"]

        arrow    = "▲" if side == "Buy" else "▼"
        side_col = HEX["buy"] if side == "Buy" else HEX["sell"]
        pnl_col  = HEX["buy"] if pnl >= 0 else HEX["sell"]
        pnl_str  = f"{'+' if pnl >= 0 else ''}${pnl:.2f}"

        time_str = (time.strftime("%m/%d %H:%M", time.localtime(ts))
                    if ts > 0 else "──")

        rr_col = HEX["buy"] if rr >= 2.0 else (HEX["warn"] if rr >= 1.5 else HEX["sell"])
        sc_col = HEX["buy"] if score >= 70 else (HEX["warn"] if score >= 55 else HEX["sub"])

        return (
            f'<span color="{HEX["over"]}" font_family="monospace">{n:>3}</span>'
            f'  <span color="{side_col}" font_family="monospace" weight="bold">'
            f'{arrow} {sym:<6}</span>'
            f' <span color="{side_col}" font_family="monospace">{side:^6}</span>'
            f' <span color="{pnl_col}" font_family="monospace" weight="bold">{pnl_str:>8}</span>'
            f'  <span color="{HEX["sub"]}" font_family="monospace">{reason:<14}</span>'
            f' <span color="{HEX["over"]}" font_family="monospace">{dur:>7}</span>'
            f'  <span color="{HEX["text"]}" font_family="monospace">{entry:>10}</span>'
            f' <span color="{HEX["sell"]}" font_family="monospace">{sl:>10}</span>'
            f' <span color="{HEX["buy"]}" font_family="monospace">{tp_p:>10}</span>'
            f'  <span color="{rr_col}" font_family="monospace">{rr:>5.1f}</span>'
            f' <span color="{sc_col}" font_family="monospace">{score:>4}</span>'
            f'  <span color="{HEX["sub"]}" font_family="monospace">{time_str}</span>'
        )
