"""
interface/gtk_app.py
────────────────────
QTS — Quantum Trading System · Ventana nativa GNOME / GTK4 + libadwaita.

Arquitectura async:
  · WebSocket corre en un thread background con su propio asyncio loop.
  · GTK corre en el main thread usando GLib.timeout_add(100ms).
  · Los widgets leen MarketState directamente (compartido sin locks).

Diseño:
  · Colores 100% nativos GNOME/Adwaita — idéntico a Terminal, Reloj, Nautilus.
  · Fondos via @tokens de libadwaita (adaptan al sistema automáticamente).
  · Colores de trading: paleta GNOME estándar (success/destructive/warning).
  · Cairo para CVD sparkline y barra de presión compradora.
"""
from __future__ import annotations

import asyncio
import math
import os
import threading
import time
from pathlib import Path
from typing import List, Optional

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk, Pango  # noqa: E402

from core.config import settings
from streams.market import CandleCVD, MarketState, MarketStream


# ─── Paleta GNOME / Adwaita (RGB 0-1 para Cairo) ─────────────────────────────
# Fuente: GNOME HIG + libadwaita tokens en modo oscuro.
# Estos colores son idénticos a los que usa GNOME Terminal, Reloj, etc.

RGB = {
    # Fondos (modo oscuro Adwaita)
    "bg":    (0.141, 0.141, 0.141),   # @window_bg_color  ~#242424
    "card":  (0.180, 0.180, 0.180),   # @card_bg_color    ~#2e2e2e
    "surf":  (0.220, 0.220, 0.220),   # superficie media  ~#383838
    # Trading — colores semánticos GNOME
    "buy":   (0.341, 0.890, 0.537),   # @success_color    #57e389  (verde GNOME)
    "sell":  (1.000, 0.482, 0.388),   # @destructive_color #ff7b63 (rojo GNOME)
    "blue":  (0.471, 0.624, 0.918),   # @accent_color     #78aeed  (azul GNOME)
    "warn":  (0.973, 0.894, 0.361),   # @warning_color    #f8e45c  (amarillo)
    "over":  (0.369, 0.361, 0.392),   # dim               #5e5c64
}

HEX = {
    # Trading — paleta GNOME estándar
    "buy":    "#57e389",   # @success_color   — verde GNOME (buys, positivo)
    "sell":   "#ff7b63",   # @destructive_color — rojo GNOME (sells, negativo)
    "blue":   "#78aeed",   # @accent_color    — azul GNOME (títulos, acento)
    "warn":   "#f8e45c",   # @warning_color   — amarillo (funding, alertas)
    "purple": "#dc8add",   # GNOME purple     — liquidaciones, OI
    "teal":   "#93ddc2",   # GNOME cyan       — spot, neutro
    # Texto
    "text":   "#ebebeb",   # @window_fg_color — texto principal
    "sub":    "#9a9996",   # dim text         — labels secundarios
    "over":   "#5e5c64",   # muy dim          — separadores, placeholders
}


# ─── Utilidades de formato ────────────────────────────────────────────────────

def fp(p: float) -> str:
    """Formatea precio según magnitud."""
    if p == 0:      return "──────"
    if p >= 10_000: return f"{p:,.1f}"
    if p >= 1_000:  return f"{p:,.2f}"
    if p >= 10:     return f"{p:.3f}"
    return          f"{p:.4f}"


def fq(q: float) -> str:
    """Formatea cantidad."""
    if q >= 1_000_000: return f"{q/1_000_000:.2f}M"
    if q >= 1_000:     return f"{q:,.0f}"
    return             f"{q:.1f}"


def fm(v: float, sign: bool = False) -> str:
    """Formatea valor monetario."""
    pfx = "+" if sign and v > 0 else ""
    av = abs(v)
    if av >= 1e9: return f"{pfx}{v/1e9:.2f}B"
    if av >= 1e6: return f"{pfx}{v/1e6:.2f}M"
    if av >= 1e3: return f"{pfx}{v:,.0f}"
    return        f"{pfx}{v:.2f}"


def sc(val: float) -> str:
    """Color semántico según signo."""
    return HEX["buy"] if val >= 0 else HEX["sell"]


def mk(text: str, color: str, bold: bool = False) -> str:
    """Shortcut Pango markup."""
    w = ' weight="bold"' if bold else ""
    return f'<span color="{color}"{w}>{GLib.markup_escape_text(text)}</span>'


def row_markup(cols: list[tuple[str, str, bool]]) -> str:
    """Construye markup de una fila con columnas (text, color, bold)."""
    return "  " + "  ".join(mk(t, c, b) for t, c, b in cols)


# ─── Async Bridge ─────────────────────────────────────────────────────────────

class AsyncBridge:
    """
    Ejecuta un event loop de asyncio en un thread daemon.
    Permite correr coroutines de WebSocket junto con el loop de GLib/GTK.
    """

    def __init__(self) -> None:
        self._loop   = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True, name="qts-async")

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def start(self) -> None:
        self._thread.start()

    def submit(self, coro) -> None:
        asyncio.run_coroutine_threadsafe(coro, self._loop)


# ─── Widget: CVD Sparkline (Cairo) ───────────────────────────────────────────

class CVDChart(Gtk.DrawingArea):
    """
    Gráfica de barras del CVD por vela usando Cairo.
    Barras verdes = compras dominan  |  rojas = ventas dominan.
    Línea central de referencia.
    """

    def __init__(self) -> None:
        super().__init__()
        self.set_size_request(-1, 52)
        self.set_hexpand(True)
        self._candles: List[CandleCVD] = []
        self.set_draw_func(self._draw)

    def update(self, candles: list) -> None:
        self._candles = list(candles)[-20:]
        self.queue_draw()

    def _draw(self, _area, cr, width: int, height: int) -> None:
        # Fondo
        cr.set_source_rgba(*RGB["card"], 1.0)
        cr.rectangle(0, 0, width, height)
        cr.fill()

        n = len(self._candles)
        if n == 0:
            # Placeholder
            cr.set_source_rgba(*RGB["over"], 0.4)
            cr.set_line_width(1)
            cr.move_to(0, height / 2)
            cr.line_to(width, height / 2)
            cr.stroke()
            return

        max_abs = max(abs(c.delta) for c in self._candles) or 1.0
        bar_w   = width / n
        pad     = 2
        h_half  = (height - pad * 2) / 2
        mid_y   = height / 2

        for i, c in enumerate(self._candles):
            x = i * bar_w
            ratio = abs(c.delta) / max_abs

            if ratio < 0.03:
                # Vela plana → línea gris
                cr.set_source_rgba(*RGB["surf"], 0.6)
                cr.rectangle(x + 1, mid_y - 1, bar_w - 2, 2)
                cr.fill()
                continue

            h = ratio * h_half
            color = RGB["buy"] if c.delta > 0 else RGB["sell"]
            alpha = 0.6 + ratio * 0.4  # más brillante cuanto más grande

            cr.set_source_rgba(*color, alpha)
            if c.delta > 0:
                cr.rectangle(x + 1, mid_y - h, bar_w - 2, h)
            else:
                cr.rectangle(x + 1, mid_y, bar_w - 2, h)
            cr.fill()

        # Línea central
        cr.set_source_rgba(*RGB["surf"], 0.5)
        cr.set_line_width(0.8)
        cr.move_to(0, mid_y)
        cr.line_to(width, mid_y)
        cr.stroke()


# ─── Widget: Buy% bar (Cairo) ────────────────────────────────────────────────

class BuyPctBar(Gtk.DrawingArea):
    """Barra de presión compradora: verde izquierda, rojo derecha."""

    def __init__(self) -> None:
        super().__init__()
        self.set_size_request(-1, 10)
        self.set_hexpand(True)
        self._pct: float = 50.0
        self.set_draw_func(self._draw)

    def update(self, pct: float) -> None:
        self._pct = max(0.0, min(100.0, pct))
        self.queue_draw()

    def _draw(self, _area, cr, width: int, height: int) -> None:
        radius = height / 2

        # Fondo
        cr.set_source_rgba(*RGB["surf"], 1.0)
        cr.arc(radius, radius, radius, math.pi / 2, 3 * math.pi / 2)
        cr.arc(width - radius, radius, radius, -math.pi / 2, math.pi / 2)
        cr.close_path()
        cr.fill()

        # Relleno
        fill_w = max(radius * 2, width * self._pct / 100)
        color  = RGB["buy"] if self._pct >= 50 else RGB["sell"]
        cr.set_source_rgba(*color, 0.85)
        cr.arc(radius, radius, radius, math.pi / 2, 3 * math.pi / 2)
        cr.arc(fill_w - radius, radius, radius, -math.pi / 2, math.pi / 2)
        cr.close_path()
        cr.fill()


# ─── Panel: Orderbook ────────────────────────────────────────────────────────

class OrderBookPanel(Gtk.Box):
    """10 asks · spread+imbalance · 10 bids con barras de volumen relativo."""

    N = 10

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add_css_class("qts-card")

        title = Gtk.Label(label="ORDERBOOK")
        title.add_css_class("qts-title")
        title.set_xalign(0)
        self.append(title)

        # Cabecera de columnas
        hdr = Gtk.Label()
        hdr.set_markup(
            f'<span color="{HEX["over"]}" size="small">'
            f'{"PRECIO":>14}  {"CANTIDAD":>10}  {"VOL":<8}</span>'
        )
        hdr.add_css_class("qts-mono-sm")
        hdr.set_xalign(0)
        self.append(hdr)

        self.ask_lbls    = [self._row_label() for _ in range(self.N)]
        self.spread_lbl  = self._row_label()
        self.bid_lbls    = [self._row_label() for _ in range(self.N)]

        for lbl in self.ask_lbls + [self.spread_lbl] + self.bid_lbls:
            self.append(lbl)

    def _row_label(self) -> Gtk.Label:
        lbl = Gtk.Label()
        lbl.add_css_class("qts-mono")
        lbl.set_xalign(0)
        lbl.set_use_markup(True)
        return lbl

    def update(self, state: MarketState) -> None:
        ob   = state.orderbook
        asks = ob.top_asks(self.N)[::-1]
        bids = ob.top_bids(self.N)

        all_q   = [q for _, q in asks + bids]
        max_q   = max(all_q) if all_q else 1.0

        def bar(q: float) -> str:
            n = min(int(q / max_q * 8), 8)
            return f'<span color="{HEX["over"]}">{"█" * n}{"░" * (8 - n)}</span>'

        SELL = HEX["sell"]
        BUY  = HEX["buy"]
        OVER = HEX["over"]

        for i, lbl in enumerate(self.ask_lbls):
            if i < len(asks):
                p, q = asks[i]
                lbl.set_markup(
                    f'<span color="{SELL}" font_family="monospace">'
                    f'{fp(p):>14}  {fq(q):>10}  </span>{bar(q)}'
                )
            else:
                lbl.set_text("")

        # Spread + imbalance
        imb   = ob.imbalance
        imb_c = BUY if imb > 0.55 else (SELL if imb < 0.45 else HEX["warn"])
        self.spread_lbl.set_markup(
            f'<span color="{OVER}">{"─" * 14}  '
            f'spr {fp(ob.spread).strip():<8}  </span>'
            f'<span color="{imb_c}" weight="bold">imb {imb * 100:.0f}%</span>'
        )

        for i, lbl in enumerate(self.bid_lbls):
            if i < len(bids):
                p, q = bids[i]
                lbl.set_markup(
                    f'<span color="{BUY}" font_family="monospace">'
                    f'{fp(p):>14}  {fq(q):>10}  </span>{bar(q)}'
                )
            else:
                lbl.set_text("")


# ─── Panel: Inteligencia ─────────────────────────────────────────────────────

class IntelPanel(Gtk.Box):
    """
    Precio futuros + spot · Basis · Funding countdown · OI + velocidad ·
    CVD sparkline (Cairo) · Presión compradora · Liquidaciones de sesión.
    """

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.add_css_class("qts-card")
        self.set_size_request(310, -1)

        # Título
        title = Gtk.Label(label="INTELIGENCIA")
        title.add_css_class("qts-title")
        title.set_xalign(0)
        self.append(title)

        # Precio
        self._price_lbl  = self._mlabel(size="x-large", bold=True)
        self._spot_lbl   = self._mlabel()
        self._basis_lbl  = self._mlabel()
        self._chg_lbl    = self._mlabel()
        for w in [self._price_lbl, self._spot_lbl, self._basis_lbl, self._chg_lbl]:
            self.append(w)

        self.append(self._sep())

        # Orderbook
        self._bid_lbl = self._mlabel()
        self._ask_lbl = self._mlabel()
        self._mid_lbl = self._mlabel()
        for w in [self._bid_lbl, self._ask_lbl, self._mid_lbl]:
            self.append(w)

        self.append(self._sep())

        # Funding
        self._fund_lbl = self._mlabel()
        self._fund_cd_lbl = self._mlabel()
        self.append(self._fund_lbl)
        self.append(self._fund_cd_lbl)

        self.append(self._sep())

        # OI
        self._oi_lbl     = self._mlabel()
        self._oi_vel_lbl = self._mlabel()
        self._vol_lbl    = self._mlabel()
        for w in [self._oi_lbl, self._oi_vel_lbl, self._vol_lbl]:
            self.append(w)

        self.append(self._sep())

        # CVD sparkline
        sec_cvd = Gtk.Label(label="CVD 1m")
        sec_cvd.add_css_class("qts-section")
        sec_cvd.set_xalign(0)
        self.append(sec_cvd)
        self._cvd_chart = CVDChart()
        self.append(self._cvd_chart)

        self.append(self._sep())

        # Buy% bar
        buy_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._buy_pct_lbl = self._mlabel()
        buy_row.append(self._buy_pct_lbl)
        self._buy_bar = BuyPctBar()
        self._buy_bar.set_valign(Gtk.Align.CENTER)
        buy_row.append(self._buy_bar)
        self.append(buy_row)

        self.append(self._sep())

        # Liquidaciones de sesión
        liq_sec = Gtk.Label(label="LIQ SESIÓN")
        liq_sec.add_css_class("qts-section")
        liq_sec.set_xalign(0)
        self.append(liq_sec)
        self._liq_long_lbl  = self._mlabel()
        self._liq_short_lbl = self._mlabel()
        for w in [self._liq_long_lbl, self._liq_short_lbl]:
            self.append(w)

        self.append(self._sep())

        # Status
        self._status_lbl = self._mlabel()
        self.append(self._status_lbl)

    def _mlabel(self, size: str = "", bold: bool = False) -> Gtk.Label:
        lbl = Gtk.Label()
        lbl.add_css_class("qts-mono")
        lbl.set_xalign(0)
        lbl.set_use_markup(True)
        return lbl

    def _sep(self) -> Gtk.Separator:
        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        sep.add_css_class("qts-sep")
        return sep

    def _kv(self, key: str, val: str, val_color: str, bold: bool = False) -> str:
        w = ' weight="bold"' if bold else ""
        k = GLib.markup_escape_text(key)
        v = GLib.markup_escape_text(val)
        return (
            f'<span color="{HEX["sub"]}">{k:<10}</span>'
            f'<span color="{val_color}"{w}>{v}</span>'
        )

    def update(self, state: MarketState) -> None:
        tk  = state.ticker
        ob  = state.orderbook

        # Color de precio: verde si subió, rojo si bajó, blanco si sin datos
        pc  = sc(tk.price_change_pct) if tk.last_price > 0 else HEX["over"]
        # Funding: rojo cuando longs pagan (positivo = sobrecargado al alza)
        fc  = HEX["sell"] if tk.funding_rate > 0 else HEX["buy"]

        # Precio futuros
        price_str = fp(tk.last_price).strip() if tk.last_price > 0 else "conectando…"
        self._price_lbl.set_markup(
            f'<span color="{pc}" weight="bold" size="x-large">{price_str}</span>'
        )

        # Spot + Basis (solo mostrar basis cuando ambos precios están disponibles)
        if state.spot_connected:
            self._spot_lbl.set_markup(
                self._kv("Spot    ", fp(state.spot_price).strip(), HEX["teal"])
            )
            if tk.last_price > 0 and state.spot_price > 0:
                bc = sc(state.basis)
                self._basis_lbl.set_markup(
                    self._kv(
                        "Basis   ",
                        f"{fp(state.basis).strip()}  ({state.basis_pct:+.3f}%)",
                        bc, bold=True,
                    )
                )
            else:
                self._basis_lbl.set_markup(self._kv("Basis   ", "──", HEX["over"]))
        else:
            self._spot_lbl.set_markup(self._kv("Spot    ", "conectando…", HEX["over"]))
            self._basis_lbl.set_text("")

        chg_str = f"{tk.price_change_pct:+.2f}%" if tk.last_price > 0 else "──"
        self._chg_lbl.set_markup(self._kv("24h     ", chg_str, pc))

        # Bid/Ask/Mid
        self._bid_lbl.set_markup(self._kv("Bid     ", fp(tk.bid).strip(),     HEX["buy"]))
        self._ask_lbl.set_markup(self._kv("Ask     ", fp(tk.ask).strip(),     HEX["sell"]))
        self._mid_lbl.set_markup(self._kv("Mid     ", fp(ob.mid_price).strip(), HEX["blue"]))

        # Funding
        fund_str = f"{tk.funding_rate:+.4f}%" if tk.last_price > 0 else "──"
        self._fund_lbl.set_markup(self._kv("Funding ", fund_str, fc, bold=True))
        self._fund_cd_lbl.set_markup(self._kv("Próximo ", state.funding_countdown, HEX["sub"]))

        # OI
        vc = sc(state.oi_velocity)
        self._oi_lbl.set_markup(
            self._kv("OI      ", fm(tk.open_interest) if tk.open_interest > 0 else "──",
                     HEX["purple"])
        )
        self._oi_vel_lbl.set_markup(
            self._kv("OI vel  ", f"{fm(state.oi_velocity, sign=True)}/min", vc, bold=True)
        )
        self._vol_lbl.set_markup(
            self._kv("Vol 24h ", fm(tk.volume_24h) if tk.volume_24h > 0 else "──", HEX["text"])
        )

        # CVD sparkline
        self._cvd_chart.update(list(state.cvd_candles))

        # Buy%
        buy   = state.buy_pct
        buy_c = HEX["buy"] if buy >= 50 else HEX["sell"]
        self._buy_pct_lbl.set_markup(
            f'<span color="{HEX["sub"]}">Compras </span>'
            f'<span color="{buy_c}" weight="bold">{buy:.1f}%</span>'
        )
        self._buy_bar.update(buy)

        # Liquidaciones de sesión
        self._liq_long_lbl.set_markup(
            self._kv("Liq LONG  ", fm(state.liq_long_total),  HEX["sell"])
        )
        self._liq_short_lbl.set_markup(
            self._kv("Liq SHORT ", fm(state.liq_short_total), HEX["buy"])
        )

        # Status
        if state.connected:
            elapsed = time.time() - state.last_update
            if elapsed < 2.0:
                st, cls = "● FUTUROS", "status-live"
            else:
                st, cls = f"◐ {elapsed:.0f}s", "status-slow"
        else:
            st, cls = "○ conectando…", "status-offline"

        spot_s = (
            f'<span color="{HEX["teal"]}"> · SPOT ●</span>'
            if state.spot_connected
            else f'<span color="{HEX["over"]}"> · SPOT ○</span>'
        )
        self._status_lbl.set_markup(
            f'<span class="{cls}" weight="bold">{st}</span>{spot_s}'
        )


# ─── Panel: Tape + Liquidaciones ─────────────────────────────────────────────

class TapePanel(Gtk.Box):
    """
    Flujo de transacciones recientes (compras verde / ventas rojo)
    y sección de liquidaciones en tiempo real con indicador de tamaño.
    """

    N_TRADES = 14
    N_LIQS   = 7

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add_css_class("qts-card")

        # Título tape
        t1 = Gtk.Label(label="TAPE")
        t1.add_css_class("qts-title")
        t1.set_xalign(0)
        self.append(t1)

        # Cabecera
        hdr = Gtk.Label()
        hdr.set_use_markup(True)
        hdr.set_markup(
            f'<span color="{HEX["over"]}" size="small">'
            f'{"PRECIO":>12}  {"LADO":^6}  {"CANTIDAD":>10}</span>'
        )
        hdr.add_css_class("qts-mono-sm")
        hdr.set_xalign(0)
        self.append(hdr)

        self.trade_lbls = [self._row() for _ in range(self.N_TRADES)]
        for lbl in self.trade_lbls:
            self.append(lbl)

        # Sección liquidaciones
        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        sep.add_css_class("qts-sep")
        self.append(sep)

        t2 = Gtk.Label(label="LIQUIDACIONES")
        t2.add_css_class("qts-section")
        t2.set_xalign(0)
        self.append(t2)

        lhdr = Gtk.Label()
        lhdr.set_use_markup(True)
        lhdr.set_markup(
            f'<span color="{HEX["over"]}" size="small">'
            f'{"TIPO":^7}  {"PRECIO":>12}  {"USD":>12}</span>'
        )
        lhdr.add_css_class("qts-mono-sm")
        lhdr.set_xalign(0)
        self.append(lhdr)

        self.liq_lbls = [self._row() for _ in range(self.N_LIQS)]
        for lbl in self.liq_lbls:
            self.append(lbl)

    def _row(self) -> Gtk.Label:
        lbl = Gtk.Label()
        lbl.add_css_class("qts-mono")
        lbl.set_xalign(0)
        lbl.set_use_markup(True)
        return lbl

    def update(self, state: MarketState) -> None:
        # Trades
        trades = state.recent_trades(self.N_TRADES)
        for i, lbl in enumerate(self.trade_lbls):
            if i < len(trades):
                tr  = trades[i]
                col = HEX["buy"] if tr.side == "Buy" else HEX["sell"]
                sym = "▲ BUY " if tr.side == "Buy" else "▼ SELL"
                lbl.set_markup(
                    f'<span color="{col}" font_family="monospace">'
                    f'{fp(tr.price):>12}  {sym}  {fq(tr.qty):>10}</span>'
                )
            else:
                lbl.set_text("")

        # Liquidaciones
        liqs = state.recent_liquidations(self.N_LIQS)
        for i, lbl in enumerate(self.liq_lbls):
            if i < len(liqs):
                liq = liqs[i]
                if liq.notional >= 500_000:
                    col  = HEX["warn"]
                    icon = "💀"
                elif liq.is_long_liq:
                    col  = HEX["sell"]
                    icon = "⚡"
                else:
                    col  = HEX["buy"]
                    icon = "⚡"
                lbl.set_markup(
                    f'<span color="{col}" font_family="monospace" weight="bold">'
                    f'{icon} {liq.position_type:<5}  {fp(liq.price):>12}  '
                    f'${fm(liq.notional):>10}</span>'
                )
            else:
                lbl.set_text("")


# ─── Stats Bar ────────────────────────────────────────────────────────────────

class StatsBar(Gtk.Box):
    """Barra de métricas de sesión siempre visible en la parte inferior."""

    def __init__(self) -> None:
        super().__init__(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=24,
        )
        self.add_css_class("qts-statsbar")
        self.set_margin_start(8)
        self.set_margin_end(8)

        self._lbls: dict[str, Gtk.Label] = {}
        for key in ["CVD", "Δ", "Spot", "Basis", "Compras", "Régimen", "Absorción", "Score"]:
            box = Gtk.Box(spacing=6)
            k   = Gtk.Label(label=f"{key}:")
            k.add_css_class("qts-label")
            v = Gtk.Label(label="──")
            v.add_css_class("qts-mono-sm")
            v.set_use_markup(True)
            box.append(k)
            box.append(v)
            self.append(box)
            self._lbls[key] = v

    def _set(self, key: str, text: str, color: str, bold: bool = False) -> None:
        w = ' weight="bold"' if bold else ""
        self._lbls[key].set_markup(
            f'<span color="{color}"{w}>{GLib.markup_escape_text(text)}</span>'
        )

    def update(self, state: MarketState) -> None:
        self._set("CVD",      fm(state.cvd, sign=True),      sc(state.cvd), bold=True)
        self._set("Δ",        fm(state.session_delta, sign=True), sc(state.session_delta), bold=True)
        self._set("Compras",  f"{state.buy_pct:.1f}%",        sc(state.buy_pct - 50))

        if state.spot_connected:
            self._set("Spot",  fp(state.spot_price).strip(), HEX["teal"])
            self._set("Basis", f"{state.basis_pct:+.3f}%",   sc(state.basis), bold=True)
        else:
            self._set("Spot",  "──", HEX["over"])
            self._set("Basis", "──", HEX["over"])

        # Placeholders para Fases 2-4
        self._set("Régimen",    "──", HEX["over"])
        self._set("Absorción",  "──", HEX["over"])
        self._set("Score",      "──", HEX["over"])


# ─── Ventana Principal ────────────────────────────────────────────────────────

class MainWindow(Adw.ApplicationWindow):

    def __init__(self, app: Adw.Application, stream: MarketStream) -> None:
        super().__init__(application=app)
        self.stream  = stream
        self._sym    = settings.default_symbol
        self._sym_btns: dict[str, Gtk.ToggleButton] = {}

        self.set_title("QTS — Quantum Trading System")
        self.set_default_size(1440, 900)
        self.set_size_request(900, 600)
        self.add_css_class("qts-window")

        # ── Layout raíz ────────────────────────────────────────
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_content(root)

        # ── Header bar ─────────────────────────────────────────
        root.append(self._build_header())

        # ── Paneles principales ────────────────────────────────
        content = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=0,
            hexpand=True,
            vexpand=True,
        )
        self._ob_panel    = OrderBookPanel()
        self._intel_panel = IntelPanel()
        self._tape_panel  = TapePanel()

        self._ob_panel.set_hexpand(True)
        self._tape_panel.set_hexpand(True)

        content.append(self._ob_panel)
        content.append(self._intel_panel)
        content.append(self._tape_panel)
        root.append(content)

        # ── Stats bar ──────────────────────────────────────────
        self._stats = StatsBar()
        root.append(self._stats)

        # ── Timer de refresco (100ms = 10fps) ─────────────────
        GLib.timeout_add(100, self._refresh)

    def _build_header(self) -> Adw.HeaderBar:
        header = Adw.HeaderBar()
        header.set_decoration_layout("icon:minimize,maximize,close")

        # Título
        title_lbl = Gtk.Label(label="⚡ QTS")
        title_lbl.add_css_class("qts-title")
        header.set_title_widget(title_lbl)

        # Botones de símbolo (linked pill group)
        sym_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=0,
            css_classes=["symbol-group"],
        )
        syms = [("XRP", "XRPUSDT"), ("SOL", "SOLUSDT"), ("BTC", "BTCUSDT"),
                ("ETH", "ETHUSDT"), ("XLM", "XLMUSDT")]

        first_btn: Optional[Gtk.ToggleButton] = None
        for label, sym in syms:
            btn = Gtk.ToggleButton(label=label)
            if first_btn is None:
                first_btn = btn
                btn.set_active(sym == self._sym)
            else:
                btn.set_group(first_btn)
                if sym == self._sym:
                    btn.set_active(True)
            btn.connect("toggled", self._on_sym_toggled, sym)
            self._sym_btns[sym] = btn
            sym_box.append(btn)

        header.pack_start(sym_box)

        # Botón reset CVD
        reset_btn = Gtk.Button(label="Reset CVD")
        reset_btn.add_css_class("flat")
        reset_btn.connect("clicked", self._on_reset_cvd)
        header.pack_end(reset_btn)

        return header

    # ── Señales ────────────────────────────────────────────────────────────────

    def _on_sym_toggled(self, btn: Gtk.ToggleButton, sym: str) -> None:
        if btn.get_active():
            self._sym = sym

    def _on_reset_cvd(self, _btn) -> None:
        state = self.stream.states.get(self._sym)
        if state:
            state.reset_session()

    # ── Loop de refresco ───────────────────────────────────────────────────────

    def _refresh(self) -> bool:
        state = self.stream.states.get(self._sym)
        if state:
            self._ob_panel.update(state)
            self._intel_panel.update(state)
            self._tape_panel.update(state)
            self._stats.update(state)
        return True   # True = continuar el timer


# ─── Aplicación ───────────────────────────────────────────────────────────────

class QTSApplication(Adw.Application):

    def __init__(self) -> None:
        super().__init__(application_id="com.qts.trading")
        self._stream = MarketStream()
        self._bridge = AsyncBridge()

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)

        # Forzar modo oscuro
        Adw.StyleManager.get_default().set_color_scheme(
            Adw.ColorScheme.FORCE_DARK
        )

        # Cargar CSS
        css_path = Path(__file__).parent / "gtk_style.css"
        provider = Gtk.CssProvider()
        provider.load_from_path(str(css_path))
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        # Arrancar async bridge + streams
        self._bridge.start()
        self._bridge.submit(self._stream.start())

    def do_activate(self) -> None:
        win = MainWindow(app=self, stream=self._stream)
        win.present()


# ─── Entrypoint ───────────────────────────────────────────────────────────────

def run() -> None:
    app = QTSApplication()
    app.run(None)
