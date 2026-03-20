"""
interface/journal_view.py
──────────────────────────
JournalView — Historial completo de trades con equity curve y estadísticas.
"""
from __future__ import annotations

import csv
import datetime
import math
import os
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


# ─── TradeCard ────────────────────────────────────────────────────────────────

class _TradeCard(Gtk.Box):
    """Fila de trade con resumen compacto y detalle expandible al hacer click."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._expanded = False

        # ── Fila resumen (clickable) ──────────────────────────────────────────
        self._summary = _ml()
        self._summary.set_margin_top(2)
        self._summary.set_margin_bottom(2)
        self._summary.set_cursor_from_name("pointer")

        click = Gtk.GestureClick()
        click.connect("released", self._on_click)
        self._summary.add_controller(click)
        self.append(self._summary)

        # ── Detalle expandible ────────────────────────────────────────────────
        self._revealer = Gtk.Revealer()
        self._revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self._revealer.set_transition_duration(150)
        self._revealer.set_reveal_child(False)

        detail_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        detail_box.set_margin_start(20)
        detail_box.set_margin_end(8)
        detail_box.set_margin_top(3)
        detail_box.set_margin_bottom(5)

        self._det_position = _ml()
        self._det_pnl      = _ml()
        self._det_time     = _ml()
        detail_box.append(self._det_position)
        detail_box.append(self._det_pnl)
        detail_box.append(self._det_time)

        self._revealer.set_child(detail_box)
        self.append(self._revealer)
        self.append(_sep())

    def _on_click(self, _gc, _n, _x, _y) -> None:
        self._expanded = not self._expanded
        self._revealer.set_reveal_child(self._expanded)

    def update(self, idx: int, t: dict) -> None:
        sym    = t["symbol"]
        side   = t["side"]
        pnl    = t["pnl_usd"]
        reason = (t["close_reason"] or "──")[:14]
        dur    = _dur(t["duration_s"])
        entry  = t["entry_price"]
        sl     = t["sl_price"]
        tp     = t["tp_price"]
        rr     = t["rr_ratio"]
        score  = t["opp_score"]
        qty    = t["qty"]
        risk   = t["risk_usd"]
        auto   = t["auto_mode"] or ""
        opened = t["opened_at"]
        closed = t["closed_at"]

        # Hora de cierre
        hora = (datetime.datetime.fromtimestamp(closed).strftime("%H:%M")
                if closed > 0 else "──")

        pnl_col  = HEX["buy"] if pnl >= 0 else HEX["sell"]
        side_col = HEX["buy"] if side.upper() in ("BUY", "LONG") else HEX["sell"]
        sign     = "+" if pnl >= 0 else ""

        # ── Fila resumen ──────────────────────────────────────────────────────
        self._summary.set_markup(
            f'<span color="{HEX["sub"]}">{idx:>3}</span>'
            f'  <span color="{HEX["text"]}" weight="bold">{sym:<7}</span>'
            f' <span color="{side_col}">{side[:4]:^6}</span>'
            f' <span color="{pnl_col}" weight="bold">{sign}${pnl:>6.2f}</span>  '
            f'<span color="{HEX["warn"]}">{reason:<14}</span>'
            f' <span color="{HEX["sub"]}">{dur:>7}</span>  '
            f'<span color="{HEX["sub"]}">'
            f'{_fp(entry):>10} {_fp(sl):>10} {_fp(tp):>10}'
            f'</span>  '
            f'<span color="{HEX["teal"]}">{rr:>5.1f}</span>'
            f' <span color="{HEX["blue"]}">{score:>4}</span>'
            f'  <span color="{HEX["sub"]}">{hora}</span>'
        )

        # ── Detalle ───────────────────────────────────────────────────────────
        notional  = qty * entry if entry > 0 else 0.0
        fee_entry = entry * qty * 0.00055 if entry > 0 else 0.0
        # Estimar precio de cierre según SL/TP y signo del PnL
        close_est = (tp if (pnl > 0 and tp > 0) else (sl if sl > 0 else entry))
        fee_exit  = close_est * qty * 0.00055 if close_est > 0 else 0.0
        total_fee = fee_entry + fee_exit
        gross_pnl = pnl + total_fee

        open_str  = (datetime.datetime.fromtimestamp(opened)
                     .strftime("%Y-%m-%d %H:%M:%S") if opened > 0 else "──")
        close_str = (datetime.datetime.fromtimestamp(closed)
                     .strftime("%Y-%m-%d %H:%M:%S") if closed > 0 else "──")

        strategy = t.get("strategy_tag", "absorcion")
        strat_color = {
            "tendencia": HEX["buy"],
            "momentum":  HEX["warn"],
            "absorcion": HEX["teal"],
        }.get(strategy, HEX["sub"])

        self._det_position.set_markup(
            f'<span color="{HEX["sub"]}">Qty: </span>'
            f'<span color="{HEX["text"]}">{qty:.4f}</span>'
            f'  <span color="{HEX["sub"]}">Nocional: </span>'
            f'<span color="{HEX["text"]}">${notional:,.2f}</span>'
            f'  <span color="{HEX["sub"]}">Riesgo: </span>'
            f'<span color="{HEX["sell"]}">${risk:.2f}</span>'
            f'  <span color="{HEX["sub"]}">Modo: </span>'
            f'<span color="{HEX["blue"]}">{auto}</span>'
            f'  <span color="{HEX["sub"]}">Estrategia: </span>'
            f'<span color="{strat_color}" weight="bold">{strategy.upper()}</span>'
        )
        self._det_pnl.set_markup(
            f'<span color="{HEX["sub"]}">Bruto≈: </span>'
            f'<span color="{pnl_col}">{sign}${gross_pnl:.4f}</span>'
            f'  <span color="{HEX["sub"]}">Fees≈: </span>'
            f'<span color="{HEX["sell"]}">-${total_fee:.4f}</span>'
            f'  <span color="{HEX["sub"]}">Neto: </span>'
            f'<span color="{pnl_col}" weight="bold">{sign}${pnl:.4f}</span>'
            f'  <span color="{HEX["sub"]}">Entry fee: </span>'
            f'<span color="{HEX["sell"]}">-${fee_entry:.4f}</span>'
            f'  <span color="{HEX["sub"]}">Exit fee: </span>'
            f'<span color="{HEX["sell"]}">-${fee_exit:.4f}</span>'
        )
        self._det_time.set_markup(
            f'<span color="{HEX["sub"]}">Apertura: </span>'
            f'<span color="{HEX["text"]}">{open_str}</span>'
            f'  <span color="{HEX["sub"]}">Cierre: </span>'
            f'<span color="{HEX["text"]}">{close_str}</span>'
        )


# ─── JournalView ──────────────────────────────────────────────────────────────

def _period_cutoff(period: str) -> float:
    """Retorna timestamp Unix (segundos) de inicio del período solicitado."""
    now = time.time()
    if period == "hora":
        return now - 3600
    if period == "hoy":
        t = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return t.timestamp()
    if period == "semana":
        return now - 7 * 86400
    return 0.0  # "todo" — sin filtro


def _stats_from_trades(trades: list) -> dict:
    """Calcula estadísticas agregadas a partir de una lista de trades ya filtrados."""
    closed = [t for t in trades if t.get("state") == "CLOSED"]
    total  = len(closed)
    wins   = sum(1 for t in closed if t["pnl_usd"] > 0)
    losses = sum(1 for t in closed if t["pnl_usd"] < 0)
    pnls   = [t["pnl_usd"] for t in closed]
    total_pnl = sum(pnls)
    best  = max(pnls) if pnls else 0.0
    worst = min(pnls) if pnls else 0.0
    avg_rr  = sum(t["rr_ratio"]  for t in closed) / total if total else 0.0
    avg_sc  = sum(t["opp_score"] for t in closed) / total if total else 0.0
    by_sym  = {}
    for t in closed:
        by_sym[t["symbol"]] = by_sym.get(t["symbol"], 0.0) + t["pnl_usd"]
    best_sym = max(by_sym, key=by_sym.get).replace("USDT", "") if by_sym else "──"
    return {
        "total": total, "wins": wins, "losses": losses,
        "win_rate": round(wins / total * 100, 1) if total else 0.0,
        "total_pnl": round(total_pnl, 2),
        "best_trade": round(best, 2), "worst_trade": round(worst, 2),
        "avg_rr": round(avg_rr, 2), "avg_score": round(avg_sc, 1),
        "best_symbol": best_sym,
    }


class JournalView(Gtk.Box):
    """
    Pestaña de Journal: equity curve + estadísticas + tabla de trades.
    """

    _PERIODS = [("todo", "Todo"), ("hoy", "Hoy"), ("semana", "Semana"), ("hora", "1h")]

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self._last_refresh: float = 0.0
        self._trade_cards:  list  = []
        self._all_trades:   list  = []
        self._period:       str   = "todo"
        self._build()

    def _build(self) -> None:
        P = 8

        # ── Filtros de período ────────────────────────────────────────────────
        flt_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        flt_box.set_margin_start(P); flt_box.set_margin_end(P)
        flt_box.set_margin_top(4);   flt_box.set_margin_bottom(2)

        flt_lbl = Gtk.Label(label="Período:")
        flt_lbl.add_css_class("qts-mono-sm")
        flt_lbl.set_margin_end(4)
        flt_box.append(flt_lbl)

        self._period_btns: dict = {}
        first_btn = None
        for key, label in self._PERIODS:
            btn = Gtk.ToggleButton(label=label)
            btn.add_css_class("qts-mono-sm")
            if first_btn is None:
                first_btn = btn
            else:
                btn.set_group(first_btn)
            if key == self._period:
                btn.set_active(True)
            btn.connect("toggled", self._on_period_toggled, key)
            flt_box.append(btn)
            self._period_btns[key] = btn

        flt_box.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        # ── Búsqueda ──
        self._search_entry = Gtk.SearchEntry(placeholder_text="Buscar (ej. BTC)")
        self._search_entry.add_css_class("qts-mono-sm")
        self._search_entry.set_hexpand(True)
        self._search_entry.set_halign(Gtk.Align.END)
        self._search_entry.connect("search-changed", self._on_search_changed)
        flt_box.append(self._search_entry)

        # ── Ordenamiento ──
        sort_model = Gtk.StringList.new(["Fecha ↓", "Fecha ↑", "PnL ↓", "PnL ↑", "Sym A-Z"])
        self._sort_dd = Gtk.DropDown.new(model=sort_model)
        self._sort_dd.connect("notify::selected", self._on_sort_changed)
        flt_box.append(self._sort_dd)

        # ── Exportar CSV ──
        csv_btn = Gtk.Button(icon_name="document-save-symbolic")
        csv_btn.set_tooltip_text("Exportar Journal a CSV")
        csv_btn.add_css_class("flat")
        csv_btn.connect("clicked", self._export_csv)
        flt_box.append(csv_btn)

        self.append(flt_box)

        # ── Header con stats resumidas ────────────────────────────────────────
        hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        hdr.set_margin_start(P); hdr.set_margin_end(P)
        hdr.set_margin_top(2);   hdr.set_margin_bottom(2)

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

    def _on_period_toggled(self, btn: Gtk.ToggleButton, key: str) -> None:
        if btn.get_active():
            self._period = key
            self._apply_filter()

    def _on_search_changed(self, _entry) -> None:
        self._apply_filter()

    def _on_sort_changed(self, _dd, _pspec) -> None:
        self._apply_filter()

    def _apply_filter(self) -> None:
        cutoff = _period_cutoff(self._period)
        query = self._search_entry.get_text().strip().upper()
        
        filtered = []
        for t in self._all_trades:
            if cutoff > 0 and t.get("closed_at", 0) < cutoff:
                continue
            if query and query not in t.get("symbol", "").upper():
                continue
            filtered.append(t)

        # ── Aplicar Ordenamiento ──
        sort_idx = self._sort_dd.get_selected()
        if sort_idx == 0:   # Fecha ↓
            filtered.sort(key=lambda x: x.get("closed_at", 0), reverse=True)
        elif sort_idx == 1: # Fecha ↑
            filtered.sort(key=lambda x: x.get("closed_at", 0))
        elif sort_idx == 2: # PnL ↓
            filtered.sort(key=lambda x: x.get("pnl_usd", 0.0), reverse=True)
        elif sort_idx == 3: # PnL ↑
            filtered.sort(key=lambda x: x.get("pnl_usd", 0.0))
        elif sort_idx == 4: # Sym A-Z
            filtered.sort(key=lambda x: x.get("symbol", ""))

        stats = _stats_from_trades(filtered)
        self._update_stats(stats)

        # Equity curve filtrada
        cum = 0.0
        curve = []
        for t in sorted(filtered, key=lambda x: x.get("closed_at", 0)):
            if t.get("state") == "CLOSED" and t.get("closed_at", 0) > 0:
                cum += t.get("pnl_usd", 0.0)
                curve.append((t["closed_at"], cum))
        self._chart.update(curve)
        self._update_trades(filtered)

    # ── Actualización ─────────────────────────────────────────────────────────

    def refresh(self) -> None:
        now = time.time()
        if (now - self._last_refresh) < 8.0:
            return
        self._last_refresh = now

        self._all_trades = get_all_trades(300)
        self._apply_filter()

    def _export_csv(self, _btn) -> None:
        if not self._all_trades:
            return
        
        # Guardar en Documentos o Home
        docs_dir = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOCUMENTS) or os.path.expanduser("~")
        filename = os.path.join(docs_dir, f"qts_journal_{int(time.time())}.csv")
        
        try:
            with open(filename, mode='w', newline='') as f:
                # Usar los campos del primer trade como cabeceras
                fieldnames = list(self._all_trades[0].keys())
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                for trade in self._all_trades:
                    writer.writerow(trade)
            print(f"Journal exportado a {filename}")
        except Exception as e:
            print(f"Error exportando CSV: {e}")

    def _update_stats(self, s: dict) -> None:
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

        period_label = {"todo": "Todo", "hoy": "Hoy", "semana": "Semana", "hora": "1h"}.get(self._period, "")
        pnl_col = HEX["buy"] if tpnl >= 0 else HEX["sell"]
        wr_col  = HEX["buy"] if wr >= 50 else HEX["sell"]

        self._stat1.set_markup(
            f'<span color="{HEX["blue"]}" weight="bold">[{period_label}]</span>'
            f'  <span color="{HEX["sub"]}">Trades: </span>'
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
        needed  = len(trades)
        current = len(self._trade_cards)

        # Añadir TradeCards que faltan
        while len(self._trade_cards) < needed:
            card = _TradeCard()
            self._rows_box.append(card)
            self._trade_cards.append(card)

        # Ocultar sobrantes
        for i in range(needed, current):
            self._trade_cards[i].set_visible(False)

        for i, t in enumerate(trades):
            self._trade_cards[i].set_visible(True)
            self._trade_cards[i].update(i + 1, t)
