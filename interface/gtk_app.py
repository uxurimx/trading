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

from core.absorption import AbsorptionDetector, AbsorptionSignal, NEUTRAL_SIGNAL
from core.liquidity import LiquidityAnalyzer, LiquidityMap, LiquidityLevel, _EMPTY_MAP
from core.trend import TrendAnalyzer, TrendSignal, NEUTRAL_TREND, TIMEFRAMES
from core.regime import (
    RegimeClassifier, OpportunityScorer,
    RegimeSignal, OpportunitySignal,
    NEUTRAL_REGIME, NEUTRAL_OPP,
)
from core.config import settings
from core.risk import RiskFortress, RiskStatus, OK_STATUS
from core.status_writer import StatusWriter
from core.technicals import TradeContextAnalyzer, TechSignal, NEUTRAL_TECH
from core.executor import BybitExecutor
from core.db import save_monitored_symbols, load_monitored_symbols
from core.paper_wallet import PaperWallet, PaperExecutor
from core.strategy import StrategyEngine
from core.controller import TradeController
from core.order_model import AutoMode
from interface.order_panel import OrderPanel
from interface.command_center import CommandCenter
from interface.journal_view import JournalView
from interface.settings_view import SettingsView
from interface.session_view import SessionView
from interface.extractor_view import ExtractorView
from interface.analyst_view import AnalystView
from streams.account import AccountStream, AccountState, Position, AccountBalance
from streams.klines import KlineStream
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


# ─── Widget: Trend Bar ───────────────────────────────────────────────────────

class TrendBar(Gtk.Box):
    """
    Barra horizontal de tendencia multi-timeframe.
    Muestra un bloque coloreado por cada TF con su dirección,
    más el score de alineación total.
    """

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.add_css_class("qts-trendbar")

        # Etiqueta fija
        hdr = Gtk.Label(label="TENDENCIA")
        hdr.add_css_class("qts-label")
        hdr.set_margin_start(12)
        hdr.set_margin_end(10)
        self.append(hdr)

        sep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        sep.set_margin_start(4)
        sep.set_margin_end(4)
        self.append(sep)

        # Bloques por timeframe
        self._tf_boxes: dict[str, Gtk.Label] = {}
        for label, _, _ in TIMEFRAMES:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            box.set_margin_start(6)
            box.set_margin_end(6)

            val_lbl = Gtk.Label()
            val_lbl.add_css_class("qts-mono")
            val_lbl.set_use_markup(True)
            val_lbl.set_markup(
                f'<span color="{HEX["over"]}"><b>─</b></span>'
            )

            key_lbl = Gtk.Label(label=label)
            key_lbl.add_css_class("qts-label")

            box.append(val_lbl)
            box.append(key_lbl)
            self.append(box)
            self._tf_boxes[label] = val_lbl

        sep2 = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        sep2.set_margin_start(6)
        sep2.set_margin_end(8)
        self.append(sep2)

        # Dirección + score
        self._dir_lbl = Gtk.Label()
        self._dir_lbl.add_css_class("qts-mono-sm")
        self._dir_lbl.set_use_markup(True)
        self._dir_lbl.set_margin_end(8)
        self._dir_lbl.set_width_chars(22)
        self._dir_lbl.set_xalign(0)
        self.append(self._dir_lbl)

        # Barra de score
        self._score_bar = ScoreBar()
        self._score_bar.set_size_request(90, 8)
        self._score_bar.set_hexpand(False)
        self._score_bar.set_valign(Gtk.Align.CENTER)
        self._score_bar.set_margin_end(8)
        self.append(self._score_bar)

        self._score_lbl = Gtk.Label()
        self._score_lbl.add_css_class("qts-mono-sm")
        self._score_lbl.set_use_markup(True)
        self._score_lbl.set_margin_end(12)
        self.append(self._score_lbl)

    def update(self, sig: TrendSignal) -> None:
        for tf in sig.timeframes:
            lbl = self._tf_boxes.get(tf.label)
            if lbl is None:
                continue
            col = HEX[tf.color_key]
            lbl.set_markup(f'<span color="{col}" weight="bold">{tf.glyph}</span>')

        col = HEX[sig.color_key]
        if sig.direction != "NEUTRAL":
            self._dir_lbl.set_markup(
                f'<span color="{col}" weight="bold">{sig.label}</span>'
                f'<span color="{HEX["sub"]}">  {sig.aligned}/{sig.total} TF</span>'
            )
        else:
            self._dir_lbl.set_markup(
                f'<span color="{HEX["over"]}">SIN TENDENCIA</span>'
            )

        self._score_bar.update(sig.score, sig.color_key)
        self._score_lbl.set_markup(
            f'<span color="{col}" weight="bold">{sig.score}%</span>'
        )


# ─── Widget: Score Bar (Cairo) ───────────────────────────────────────────────

class ScoreBar(Gtk.DrawingArea):
    """
    Barra de score 0-100 con color configurable.
    Usada para el score de absorción.
    """

    def __init__(self) -> None:
        super().__init__()
        self.set_size_request(-1, 10)
        self.set_hexpand(True)
        self._score: float = 0.0
        self._color: tuple  = RGB["over"]
        self.set_draw_func(self._draw)

    def update(self, score: float, color_key: str) -> None:
        self._score = max(0.0, min(100.0, score))
        self._color = RGB.get(color_key, RGB["over"])
        self.queue_draw()

    def _draw(self, _area, cr, width: int, height: int) -> None:
        radius = height / 2

        # Fondo
        cr.set_source_rgba(*RGB["surf"], 1.0)
        cr.arc(radius, radius, radius, math.pi / 2, 3 * math.pi / 2)
        cr.arc(width - radius, radius, radius, -math.pi / 2, math.pi / 2)
        cr.close_path()
        cr.fill()

        if self._score < 1:
            return

        # Relleno proporcional al score
        fill_w = max(radius * 2, width * self._score / 100)
        alpha  = 0.5 + self._score / 200   # 0.5 en score=0, 1.0 en score=100
        cr.set_source_rgba(*self._color, alpha)
        cr.arc(radius, radius, radius, math.pi / 2, 3 * math.pi / 2)
        cr.arc(fill_w - radius, radius, radius, -math.pi / 2, math.pi / 2)
        cr.close_path()
        cr.fill()


# ─── Widget: Position Bar ────────────────────────────────────────────────────

class PositionBar(Gtk.Box):
    """
    Barra horizontal que muestra posiciones abiertas + estado de riesgo.
    Se actualiza desde AccountStream (datos de cuenta privada).
    """

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.add_css_class("qts-posbar")

        # Posiciones (izquierda, hexpand)
        self._pos_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        self._pos_box.set_hexpand(True)
        self._pos_box.set_margin_start(12)
        self._pos_lbl = Gtk.Label()
        self._pos_lbl.add_css_class("qts-mono-sm")
        self._pos_lbl.set_use_markup(True)
        self._pos_lbl.set_xalign(0)
        self._pos_lbl.set_hexpand(True)
        self._pos_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        self._pos_box.append(self._pos_lbl)
        self.append(self._pos_box)

        sep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        sep.set_margin_start(8)
        sep.set_margin_end(8)
        self.append(sep)

        # Balance (centro)
        self._bal_lbl = Gtk.Label()
        self._bal_lbl.add_css_class("qts-mono-sm")
        self._bal_lbl.set_use_markup(True)
        self._bal_lbl.set_margin_end(12)
        self._bal_lbl.set_width_chars(26)
        self.append(self._bal_lbl)

        sep2 = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        sep2.set_margin_end(8)
        self.append(sep2)

        # Riesgo (derecha)
        self._risk_lbl = Gtk.Label()
        self._risk_lbl.add_css_class("qts-mono-sm")
        self._risk_lbl.set_use_markup(True)
        self._risk_lbl.set_margin_end(12)
        self._risk_lbl.set_width_chars(34)
        self._risk_lbl.set_xalign(0)
        self.append(self._risk_lbl)

    def update(self, acct: AccountState, risk: RiskStatus) -> None:
        self._render_positions(acct)
        self._render_balance(acct.balance, risk)
        self._render_risk(risk)

    def _render_positions(self, acct: AccountState) -> None:
        positions = acct.open_positions()

        if not acct.connected and not positions:
            key_color = HEX["over"]
            if acct.error:
                self._pos_lbl.set_markup(
                    f'<span color="{HEX["sell"]}" size="small">⚠ {GLib.markup_escape_text(acct.error)}</span>'
                )
            else:
                self._pos_lbl.set_markup(
                    f'<span color="{HEX["over"]}">○ conectando cuenta…</span>'
                )
            return

        if not positions:
            self._pos_lbl.set_markup(
                f'<span color="{HEX["over"]}">── Sin posiciones abiertas</span>'
            )
            return

        parts = []
        for pos in positions:
            col   = HEX["buy"] if pos.is_long else HEX["sell"]
            pnlc  = HEX["buy"] if pos.unrealized_pnl >= 0 else HEX["sell"]
            arrow = "▲" if pos.is_long else "▼"
            pnl_s = f"{pos.unrealized_pnl:+.2f}"
            pct_s = f"{pos.pnl_pct:+.1f}%"

            liq_str = ""
            if pos.liquidation_price > 0:
                d = pos.distance_to_liq_pct
                liq_col = HEX["sell"] if d < 5 else (HEX["warn"] if d < 10 else HEX["sub"])
                liq_str = (
                    f'  <span color="{liq_col}">Liq {fp(pos.liquidation_price)}</span>'
                )

            sl_str = f'  <span color="{HEX["sub"]}">SL {fp(pos.stop_loss)}</span>' if pos.stop_loss > 0 else ""
            tp_str = f'  <span color="{HEX["sub"]}">TP {fp(pos.take_profit)}</span>' if pos.take_profit > 0 else ""

            parts.append(
                f'<span color="{col}" weight="bold">{arrow} {pos.side_label} {pos.symbol}</span>'
                f'<span color="{HEX["text"]}">  {fq(pos.size)} @ {fp(pos.entry_price)}</span>'
                f'  <span color="{HEX["sub"]}">▶</span>'
                f'  <span color="{HEX["text"]}">{fp(pos.mark_price)}</span>'
                f'  <span color="{pnlc}" weight="bold">{pnl_s} ({pct_s})</span>'
                f'{liq_str}{sl_str}{tp_str}'
            )

        self._pos_lbl.set_markup("    ".join(parts))

    def _render_balance(self, b: AccountBalance, risk: RiskStatus) -> None:
        if b.total_equity <= 0:
            self._bal_lbl.set_markup(f'<span color="{HEX["over"]}">Equity ──</span>')
            return

        dpnl_col = HEX["buy"] if risk.daily_pnl_usd >= 0 else HEX["sell"]
        upnl_col = HEX["buy"] if b.unrealized_pnl >= 0 else HEX["sell"]

        self._bal_lbl.set_markup(
            f'<span color="{HEX["sub"]}">Equity </span>'
            f'<span color="{HEX["text"]}" weight="bold">${b.total_equity:,.0f}</span>'
            f'<span color="{HEX["sub"]}">  Día </span>'
            f'<span color="{dpnl_col}" weight="bold">{risk.daily_pnl_usd:+.0f}</span>'
            f'<span color="{HEX["sub"]}">  nPnL </span>'
            f'<span color="{upnl_col}">{b.unrealized_pnl:+.2f}</span>'
        )

    def _render_risk(self, risk: RiskStatus) -> None:
        col = HEX[risk.color_key]

        if risk.is_breaker:
            self._risk_lbl.set_markup(
                f'<span color="{col}" weight="bold">{risk.icon} CIRCUIT BREAKER  </span>'
                f'<span color="{HEX["sell"]}">{GLib.markup_escape_text(risk.message)}</span>'
            )
        elif risk.is_warning:
            self._risk_lbl.set_markup(
                f'<span color="{col}" weight="bold">{risk.icon} {GLib.markup_escape_text(risk.message)}</span>'
                f'<span color="{HEX["sub"]}">  Mrgn {risk.margin_pct:.0f}%</span>'
            )
        else:
            self._risk_lbl.set_markup(
                f'<span color="{col}">{risk.icon} OK</span>'
                f'<span color="{HEX["sub"]}">  Día {risk.daily_pnl_pct:+.2f}%  Mrgn {risk.margin_pct:.0f}%</span>'
            )


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
        self.set_size_request(320, -1)

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

        # Absorción
        abs_sec = Gtk.Label(label="ABSORCIÓN")
        abs_sec.add_css_class("qts-section")
        abs_sec.set_xalign(0)
        self.append(abs_sec)

        # Fila: label de señal + score
        self._abs_signal_lbl = self._mlabel()
        self.append(self._abs_signal_lbl)

        # Barra de score
        self._abs_bar = ScoreBar()
        self.append(self._abs_bar)

        # Componentes del score
        self._abs_detail_lbl = self._mlabel()
        self.append(self._abs_detail_lbl)

        # Razones
        self._abs_reason_lbl = self._mlabel()
        self.append(self._abs_reason_lbl)

        self.append(self._sep())

        # Mapa de Liquidez
        liq_sec = Gtk.Label(label="LIQUIDEZ")
        liq_sec.add_css_class("qts-section")
        liq_sec.set_xalign(0)
        self.append(liq_sec)

        # Filas: N_LIQ_ROWS arriba + precio + N_LIQ_ROWS abajo
        N = 4
        self._liq_above = [self._row_label() for _ in range(N)]
        self._liq_price = self._row_label()
        self._liq_below = [self._row_label() for _ in range(N)]
        self._liq_ctx   = self._mlabel()

        for lbl in self._liq_above:
            self.append(lbl)
        self.append(self._liq_price)
        for lbl in self._liq_below:
            self.append(lbl)
        self.append(self._liq_ctx)

        self.append(self._sep())

        # ── Técnicos (Phase 6+) ────────────────────────────────────────────
        tech_sec = Gtk.Label(label="TÉCNICOS  15m · 1h")
        tech_sec.add_css_class("qts-section")
        tech_sec.set_xalign(0)
        self.append(tech_sec)

        self._tech_ema15_lbl  = self._row_label()
        self._tech_ema1h_lbl  = self._row_label()
        self._tech_rsi_lbl    = self._row_label()
        self._tech_sr_lbl     = self._row_label()
        self._tech_atr_lbl    = self._row_label()
        for w in [self._tech_ema15_lbl, self._tech_ema1h_lbl,
                  self._tech_rsi_lbl, self._tech_sr_lbl, self._tech_atr_lbl]:
            self.append(w)

        # Score del setup de la posición abierta
        self._tech_score_bar  = ScoreBar()
        self._tech_score_lbl  = self._row_label()
        self.append(self._tech_score_bar)
        self.append(self._tech_score_lbl)

        # Observaciones: ✓ / ⚠ / →
        self._tech_obs_lbls = [self._row_label() for _ in range(5)]
        for w in self._tech_obs_lbls:
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
        # Ancho máximo fijo: evita que el panel se ensanche cuando cambia el texto
        lbl.set_max_width_chars(36)
        lbl.set_ellipsize(Pango.EllipsizeMode.END)
        return lbl

    def _row_label(self) -> Gtk.Label:
        lbl = Gtk.Label()
        lbl.add_css_class("qts-mono-sm")
        lbl.set_xalign(0)
        lbl.set_use_markup(True)
        lbl.set_max_width_chars(38)
        lbl.set_ellipsize(Pango.EllipsizeMode.END)
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

    # ── Renderizado del mapa de liquidez ───────────────────────────────────────

    def _render_liquidity(self, lmap: "LiquidityMap", price: float) -> None:
        N = len(self._liq_above)

        def level_markup(lv: "LiquidityLevel") -> str:
            col   = HEX[lv.color_key]
            arrow = "▲" if lv.is_above else "▼"
            if lv.level_type in ("HVN", "LVN") and lv.vol_pct > 0:
                bars = min(8, int(lv.vol_pct * 8))
                bar  = "█" * bars + "░" * (8 - bars)
            elif lv.level_type in ("EQ_H", "EQ_L"):
                bar  = f"×{lv.count}     "
            else:
                bar  = "○      "
            dist_s = f"{lv.dist_pct:+.2f}%"
            return (
                f'<span color="{col}" font_family="monospace">'
                f'{arrow} {fp(lv.price):>10}  {lv.label}  '
                f'<span size="small">{bar:8}  {dist_s:>7}</span>'
                f'</span>'
            )

        # Niveles ARRIBA — lejano arriba en pantalla, cercano abajo
        above_rev = list(reversed(lmap.above[:N]))
        padding   = N - len(above_rev)
        for i, lbl in enumerate(self._liq_above):
            idx = i - padding
            if 0 <= idx < len(above_rev):
                lbl.set_markup(level_markup(above_rev[idx]))
            else:
                lbl.set_text("")

        # Precio actual
        if price > 0:
            self._liq_price.set_markup(
                f'<span color="{HEX["blue"]}" weight="bold" font_family="monospace">'
                f'● {fp(price):>10}  ←── PRECIO</span>'
            )
        else:
            self._liq_price.set_text("")

        # Niveles ABAJO — más cercano primero
        for i, lbl in enumerate(self._liq_below):
            if i < len(lmap.below):
                lbl.set_markup(level_markup(lmap.below[i]))
            else:
                lbl.set_text("")

        # Contexto
        self._liq_ctx.set_markup(
            f'<span color="{HEX["sub"]}" size="small">{lmap.context}</span>'
        )

    def _render_technicals(self, tech: "TechSignal") -> None:
        if not tech.has_data:
            dim = HEX["over"]
            self._tech_ema15_lbl.set_markup(f'<span color="{dim}">EMA 9/21 (15m)  ──</span>')
            self._tech_ema1h_lbl.set_markup(f'<span color="{dim}">EMA 50 (1h)     ──</span>')
            self._tech_rsi_lbl.set_markup(f'<span color="{dim}">RSI             ──</span>')
            self._tech_sr_lbl.set_markup(f'<span color="{dim}">Sup / Res       ──</span>')
            self._tech_atr_lbl.set_markup(f'<span color="{dim}">ATR (15m)       ──</span>')
            self._tech_score_bar.update(0, "over")
            self._tech_score_lbl.set_text("")
            for lbl in self._tech_obs_lbls:
                lbl.set_text("")
            return

        # EMA 15m
        ema15c = HEX["buy"] if tech.ema15m_bull else HEX["sell"]
        ema15s = "▲ ALCISTA" if tech.ema15m_bull else "▼ BAJISTA"
        ema200_badge = f'  <span color="{HEX["warn"]}" size="small">⚑ EN EMA200 1h</span>' if tech.at_ema200 else ""
        self._tech_ema15_lbl.set_markup(
            f'<span color="{HEX["sub"]}">EMA 9/21 (15m) </span>'
            f'<span color="{ema15c}" weight="bold">{ema15s}</span>'
        )

        # EMA 1h
        ema1h_c = HEX["buy"] if tech.ema1h_bull else HEX["sell"]
        ema1h_s = "▲ sobre EMA50" if tech.ema1h_bull else "▼ bajo EMA50"
        self._tech_ema1h_lbl.set_markup(
            f'<span color="{HEX["sub"]}">EMA 50  (1h)   </span>'
            f'<span color="{ema1h_c}" weight="bold">{ema1h_s}</span>'
            f'{ema200_badge}'
        )

        # RSI
        rsi15c = (HEX["sell"] if tech.rsi_15m > 70 else
                  HEX["buy"]  if tech.rsi_15m < 30 else HEX["text"])
        rsi1hc = (HEX["sell"] if tech.rsi_1h  > 70 else
                  HEX["buy"]  if tech.rsi_1h  < 30 else HEX["sub"])
        self._tech_rsi_lbl.set_markup(
            f'<span color="{HEX["sub"]}">RSI             </span>'
            f'<span color="{rsi15c}" weight="bold">{tech.rsi_15m:.1f}</span>'
            f'<span color="{HEX["over"]}"> 15m  </span>'
            f'<span color="{rsi1hc}">{tech.rsi_1h:.1f}</span>'
            f'<span color="{HEX["over"]}"> 1h</span>'
        )

        # Soporte / Resistencia
        self._tech_sr_lbl.set_markup(
            f'<span color="{HEX["sub"]}">Sop / Res       </span>'
            f'<span color="{HEX["buy"]}">{fp(tech.support)}</span>'
            f'<span color="{HEX["over"]}"> · </span>'
            f'<span color="{HEX["sell"]}">{fp(tech.resistance)}</span>'
        )

        # ATR
        if tech.atr_15m > 0:
            atr_pct = tech.atr_15m / tech.ema9_15m * 100 if tech.ema9_15m > 0 else 0
            self._tech_atr_lbl.set_markup(
                f'<span color="{HEX["sub"]}">ATR (15m)       </span>'
                f'<span color="{HEX["text"]}">{fp(tech.atr_15m)}</span>'
                f'<span color="{HEX["over"]}"> ({atr_pct:.2f}%)</span>'
            )
        else:
            self._tech_atr_lbl.set_text("")

        # Score bar del setup
        self._tech_score_bar.update(tech.score, tech.score_color)
        self._tech_score_lbl.set_markup(
            f'<span color="{HEX[tech.score_color]}" weight="bold">'
            f'{tech.verdict}  {tech.score}</span>'
            + (f'<span color="{HEX["sub"]}">  R:R {tech.rr_ratio:.1f}:1</span>'
               if tech.rr_ratio > 0 else "")
        )

        # Observaciones
        all_obs = (
            [("buy",  t) for t in tech.good]  +
            [("warn", t) for t in tech.risks] +
            [("blue", t) for t in tech.tips]
        )
        icons = {"buy": "✓", "warn": "⚠", "blue": "→"}
        for i, lbl in enumerate(self._tech_obs_lbls):
            if i < len(all_obs):
                key, text = all_obs[i]
                icon = icons[key]
                lbl.set_markup(
                    f'<span color="{HEX[key]}" size="small">'
                    f'{icon} {GLib.markup_escape_text(text)}</span>'
                )
            else:
                lbl.set_text("")

    def update(
        self,
        state: MarketState,
        sig:   "AbsorptionSignal",
        lmap:  "LiquidityMap",
        opp:   "OpportunitySignal",
        tech:  "TechSignal" = None,
    ) -> None:
        if tech is None:
            tech = NEUTRAL_TECH
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

        # ── Absorción ──────────────────────────────────────────────────────────
        col = HEX[sig.color_key]

        if sig.is_signal:
            strength = (
                "FUERTE"   if sig.score >= 70 else
                "MODERADA" if sig.score >= 45 else
                "DÉBIL"
            )
            self._abs_signal_lbl.set_markup(
                f'<span color="{col}" weight="bold">{sig.label}</span>'
                f'<span color="{HEX["sub"]}">  {strength}</span>'
            )
            self._abs_bar.update(sig.score, sig.color_key)
            self._abs_detail_lbl.set_markup(
                f'<span color="{HEX["sub"]}" size="small">'
                f'CVD:{sig.cvd_div}  Flujo:{sig.flow_eff}  Agr:{sig.aggression}  OB:{sig.ob_stress}'
                f'  <b>{sig.score}/100</b></span>'
            )
            if sig.reasons:
                self._abs_reason_lbl.set_markup(
                    f'<span color="{HEX["over"]}" size="small">'
                    + " · ".join(GLib.markup_escape_text(r) for r in sig.reasons)
                    + "</span>"
                )
            else:
                self._abs_reason_lbl.set_text("")
        else:
            self._abs_signal_lbl.set_markup(
                f'<span color="{HEX["over"]}">Sin señal</span>'
            )
            self._abs_bar.update(0, "over")
            self._abs_detail_lbl.set_text("")
            self._abs_reason_lbl.set_text("")

        # ── Mapa de Liquidez ───────────────────────────────────────────────────
        self._render_liquidity(lmap, tk.last_price)

        # ── Técnicos (klines REST) ─────────────────────────────────────────────
        self._render_technicals(tech)

        # ── Oportunidad (componentes debajo de absorción) ──────────────────────
        if opp.score >= 20:
            opp_col = HEX[opp.color_key]
            self._abs_reason_lbl.set_markup(
                f'<span color="{HEX["over"]}" size="small">'
                + " · ".join(GLib.markup_escape_text(r) for r in (sig.reasons + opp.reasons)[:3])
                + "</span>"
            )
        else:
            # razones solo de absorción
            if sig.reasons:
                self._abs_reason_lbl.set_markup(
                    f'<span color="{HEX["over"]}" size="small">'
                    + " · ".join(GLib.markup_escape_text(r) for r in sig.reasons)
                    + "</span>"
                )

        # Status
        if state.connected:
            elapsed = time.time() - state.last_update
            if elapsed < 2.0:
                st, st_col = "● FUTUROS", HEX["buy"]
            else:
                st, st_col = f"◐ {elapsed:.0f}s", HEX["warn"]
        else:
            st, st_col = "○ conectando…", HEX["over"]

        spot_s = (
            f'<span color="{HEX["teal"]}"> · SPOT ●</span>'
            if state.spot_connected
            else f'<span color="{HEX["over"]}"> · SPOT ○</span>'
        )
        self._status_lbl.set_markup(
            f'<span color="{st_col}" weight="bold">{st}</span>{spot_s}'
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

    def update(
        self,
        state: MarketState,
        sig:   "AbsorptionSignal",
        opp:   "OpportunitySignal",
    ) -> None:
        self._set("CVD",     fm(state.cvd, sign=True),           sc(state.cvd),           bold=True)
        self._set("Δ",       fm(state.session_delta, sign=True), sc(state.session_delta), bold=True)
        self._set("Compras", f"{state.buy_pct:.1f}%",            sc(state.buy_pct - 50))

        if state.spot_connected:
            self._set("Spot",  fp(state.spot_price).strip(), HEX["teal"])
            self._set("Basis", f"{state.basis_pct:+.3f}%",   sc(state.basis), bold=True)
        else:
            self._set("Spot",  "──", HEX["over"])
            self._set("Basis", "──", HEX["over"])

        # Absorción
        if sig.is_signal:
            short = "COMP" if sig.side == "BUY" else "VEND"
            self._set("Absorción", short, HEX[sig.color_key], bold=True)
        else:
            self._set("Absorción", "──", HEX["over"])

        # Régimen (Fase 4)
        regime = opp.regime
        self._set("Régimen", regime.label, HEX[regime.color_key], bold=(regime.confidence >= 60))

        # Score de oportunidad combinado (Fase 4)
        if opp.is_actionable:
            self._set("Score", f"{opp.score}", HEX[opp.color_key], bold=True)
        else:
            self._set("Score", "──", HEX["over"])


# ─── Ventana Principal ────────────────────────────────────────────────────────

class MainWindow(Adw.ApplicationWindow):

    def __init__(
        self,
        app:          Adw.Application,
        stream:       MarketStream,
        acct:         AccountStream,
        klines:       KlineStream,
        controller:   "TradeController",
        strategy:     "StrategyEngine",
        executor:     "PaperExecutor",
        bridge:       "AsyncBridge",
        paper_wallet: "PaperWallet",
    ) -> None:
        super().__init__(application=app)
        self.stream        = stream
        self.acct          = acct
        self.klines        = klines
        self.controller    = controller
        self._strategy     = strategy
        self._executor     = executor
        self._bridge       = bridge
        self._paper_wallet = paper_wallet
        self._sym    = settings.default_symbol
        self._sym_btns: dict[str, Gtk.ToggleButton] = {}

        self.set_title("QTS — Quantum Trading System")
        self.set_default_size(1200, 720)
        self.set_size_request(700, 300)
        self.set_resizable(True)
        self.add_css_class("qts-window")

        # F11 para maximizar/restaurar
        ctrl = Gtk.EventControllerKey()
        ctrl.connect("key-pressed", self._on_key_press)
        self.add_controller(ctrl)

        self._trend_analyzer  = TrendAnalyzer()
        self._abs_detector    = AbsorptionDetector()
        self._liq_analyzer    = LiquidityAnalyzer()
        self._regime_clf      = RegimeClassifier()
        self._opp_scorer      = OpportunityScorer()
        self._risk_fortress   = RiskFortress()
        self._status_writer   = StatusWriter()
        self._tech_analyzer   = TradeContextAnalyzer()
        self._tech_signal     = NEUTRAL_TECH
        self._kline_req_ctr   = 199  # forzar fetch inmediato en primer ciclo
        # Cache multi-símbolo para el controller (actualizado cada ~3s = 30 ciclos)
        self._multi_opp:  dict = {}
        self._multi_tech: dict = {}
        self._multi_ctr:  int  = 0

        # ── ViewStack (dos pestañas) ────────────────────────────
        self._stack = Adw.ViewStack()
        self._stack.set_hexpand(True)
        self._stack.set_vexpand(True)

        # ── Layout raíz: ToolbarView (forma correcta en libadwaita)
        # Garantiza que el header tiene altura fija y el stack recibe
        # exactamente el resto — esto permite que los ScrolledWindow
        # dentro del stack tengan un techo real y puedan hacer scroll.
        toolbar_view = Adw.ToolbarView()
        toolbar_view.add_top_bar(self._build_header())
        toolbar_view.set_content(self._stack)
        self.set_content(toolbar_view)

        # ── Pestaña 1: CommandCenter (sin barras extra — más espacio vertical)
        self._cmd_center = CommandCenter(self.controller, self._strategy, self._executor,
                                         klines_store=self.klines.store)
        self._stack.add_titled_with_icon(
            self._cmd_center, "orders", "⚡ Órdenes", "go-next-symbolic"
        )

        # ── Pestaña 2: Dashboard de mercado ────────────────────
        market_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=0,
            hexpand=True,
            vexpand=True,
        )

        # TrendBar y PositionBar solo en el tab de mercado (ahorran ~95px en Órdenes)
        self._trend_bar = TrendBar()
        market_box.append(self._trend_bar)
        self._pos_bar = PositionBar()
        market_box.append(self._pos_bar)

        content = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=0,
            hexpand=True,
            vexpand=True,
        )

        self._ob_panel   = OrderBookPanel()
        self._tape_panel = TapePanel()
        self._ob_panel.set_hexpand(True)
        self._tape_panel.set_hexpand(True)

        # IntelPanel dentro de ScrolledWindow — ancho 100% fijo
        # set_propagate_natural_width(False): el ScrolledWindow NO hereda el
        # ancho natural del hijo → etiquetas largas no ensanchan la columna.
        self._intel_panel = IntelPanel()
        self._intel_panel.set_hexpand(False)
        intel_scroll = Gtk.ScrolledWindow()
        intel_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        intel_scroll.set_child(self._intel_panel)
        intel_scroll.set_size_request(310, -1)
        intel_scroll.set_propagate_natural_width(False)
        intel_scroll.set_propagate_natural_height(False)
        intel_scroll.set_hexpand(False)
        intel_scroll.set_vexpand(True)

        self._order_panel = OrderPanel(self.controller)

        content.append(self._ob_panel)
        content.append(intel_scroll)
        content.append(self._tape_panel)
        content.append(self._order_panel)

        # CRÍTICO: envolver content en ScrolledWindow con propagate_natural_height=False.
        # Adw.ViewStack mide TODAS las páginas para calcular la altura mínima de la ventana.
        # Sin este wrap, OrderBookPanel+TapePanel (400-500px de mínimo) fuerzan
        # el mínimo del ViewStack a ~650px → la ventana queda fija cerca de la altura de pantalla.
        content_scroll = Gtk.ScrolledWindow()
        content_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        content_scroll.set_vexpand(True)
        content_scroll.set_hexpand(True)
        content_scroll.set_propagate_natural_height(False)
        content_scroll.set_child(content)
        market_box.append(content_scroll)

        # Stats bar dentro del tab de mercado
        self._stats = StatsBar()
        market_box.append(self._stats)

        self._stack.add_titled_with_icon(
            market_box, "market", "📊 Mercado", "view-grid-symbolic"
        )

        # ── Pestaña 3: Journal ──────────────────────────────────────────
        self._journal_view = JournalView()
        self._stack.add_titled_with_icon(
            self._journal_view, "journal", "📋 Journal", "document-open-symbolic"
        )

        # ── Pestaña 4: Sesiones ─────────────────────────────────────────
        self._session_view = SessionView()
        self._stack.add_titled_with_icon(
            self._session_view, "sessions", "📁 Sesiones", "folder-open-symbolic"
        )

        # ── Pestaña 4: Configuración ────────────────────────────────────
        self._settings_view = SettingsView(paper_wallet=self._paper_wallet,
                                           on_paper_toggle=self._on_paper_toggle)
        self._stack.add_titled_with_icon(
            self._settings_view, "settings", "⚙ Config", "preferences-system-symbolic"
        )

        # ── Pestaña 5: Extractor (sesión Claude) ────────────────────────
        self._extractor_view = ExtractorView(self._executor, self._bridge)
        self._stack.add_titled_with_icon(
            self._extractor_view, "extractor", "🤖 Extractor", "applications-science-symbolic"
        )

        # ── Pestaña 6: Analista de Sistema ───────────────────────────────
        self._analyst_view = AnalystView(self._bridge)
        self._stack.add_titled_with_icon(
            self._analyst_view, "analyst", "🔬 Analista", "system-search-symbolic"
        )

        # Estado para detectar cierre de sesión
        self._last_seen_session_status: str = ""
        self._last_seen_session_id:     str = ""

        # ── Timer de refresco (100ms = 10fps) ─────────────────
        GLib.timeout_add(100, self._refresh)

    def _on_key_press(self, ctrl, keyval, keycode, state) -> bool:
        from gi.repository import Gdk
        if keyval == Gdk.KEY_F11:
            if self.is_maximized():
                self.unmaximize()
            else:
                self.maximize()
            return True
        return False

    def _build_header(self) -> Adw.HeaderBar:
        header = Adw.HeaderBar()
        header.set_decoration_layout("icon:minimize,maximize,close")

        # ViewSwitcher centrado en el header
        switcher = Adw.ViewSwitcher()
        switcher.set_stack(self._stack)
        switcher.set_policy(Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)

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

    def _on_paper_toggle(self, active: bool) -> None:
        """Activa/desactiva paper trading. Limpia posiciones activas del controller."""
        if not active:
            # Switching to REAL mode — validate API keys first
            if not settings.bybit_api_key or not settings.bybit_api_secret:
                import gi
                gi.require_version("Adw", "1")
                from gi.repository import Adw
                dialog = Adw.AlertDialog(
                    heading="API Keys no configuradas",
                    body="No se puede activar el modo real sin BYBIT_API_KEY y BYBIT_API_SECRET en el .env.\n\nConfigúralas y reinicia la app.",
                )
                dialog.add_response("ok", "Entendido")
                dialog.present(self)
                return  # No switch to real mode
        settings.paper_trading = active
        # Limpiar trades activos del controller para evitar mezcla live/paper
        self.controller._active.clear()
        self.controller._proposal = None

    # ── Loop de refresco ───────────────────────────────────────────────────────

    def _refresh(self) -> bool:
        """Timer de 100ms — SIEMPRE retorna True para no matar el loop."""
        try:
            self._do_refresh()
        except Exception:
            import logging as _log
            _log.getLogger("qts.ui").exception("Error en _refresh — UI continúa")
        return True   # CRÍTICO: nunca dejar de retornar True

    def _do_refresh(self) -> None:
        state = self.stream.states.get(self._sym)
        if not state:
            return

        # ── Watchdog: si los datos llevan >20s sin actualizarse, reconectar stream ──
        stale_s = time.time() - state.last_update if state.last_update > 0 else 0
        if stale_s > 20 and state.last_update > 0:
            # Reiniciar stream del símbolo en el loop async
            self._bridge.submit(self.stream._connect_futures(self._sym))
            state.last_update = time.time()  # evitar re-disparar en el siguiente tick

        # ── Calcular todos los signals (orden importa: cada uno usa el anterior)
        trend  = self._trend_analyzer.analyze(state)
        sig    = self._abs_detector.analyze(state)
        lmap   = self._liq_analyzer.analyze(state)
        regime = self._regime_clf.classify(state, trend)
        opp    = self._opp_scorer.score(sig, regime, trend, lmap)

        # ── Paper trading: actualizar wallet y chequear SL/TP ─────────────
        if settings.paper_trading:
            self._paper_wallet.update_mark_prices(self.stream.states)
            self._paper_wallet.tick(self.stream.states)

        # ── Fuente de cuenta: real o paper ────────────────────────────────
        account = self._paper_wallet.state if settings.paper_trading else self.acct.state

        # ── Riesgo de cuenta ───────────────────────────────────────────────
        risk = self._risk_fortress.check(account)

        # ── Técnicos: solicitar klines para TODOS los símbolos cada ~20 s ────
        self._kline_req_ctr += 1
        if self._kline_req_ctr >= 200:
            self._kline_req_ctr = 0
            for _s in settings.symbol_list:
                self.klines.request(_s)

        positions = account.open_positions()
        if positions:
            k15 = self.klines.store.get(self._sym, settings.fast_kline)
            k1h = self.klines.store.get(self._sym, settings.slow_kline)
            if k15 and k1h:
                self._tech_signal = self._tech_analyzer.analyze(positions[0], k15, k1h)

        # ── Actualizar widgets del tab de mercado ───────────────────────────
        self._trend_bar.update(trend)
        self._pos_bar.update(account, risk)
        self._ob_panel.update(state)
        self._intel_panel.update(state, sig, lmap, opp, self._tech_signal)
        self._tape_panel.update(state)
        self._stats.update(state, sig, opp)

        # ── Cache multi-símbolo en background (cada ~5 s) ──────────────────
        # Se corre en el bridge thread para no bloquear GTK con 15 símbolos.
        self._multi_ctr += 1
        if self._multi_ctr >= 50:
            self._multi_ctr = 0
            self._bridge.submit(self._compute_multi_signals())

        # ── Tick del controller ─────────────────────────────────────────────
        self.controller.tick(
            states  = self.stream.states,
            account = account,
            techs   = self._multi_tech,
            opps    = self._multi_opp,
            risk    = risk,
        )

        # ── Simulación para el OrderPanel ──────────────────────────────────
        cs      = self.controller.state
        _first_active = cs.active_trades[0] if cs.active_trades else None
        sim_sym = _first_active.symbol if _first_active else self._sym
        sim_state = self.stream.states.get(sim_sym)
        sim_tech  = self._multi_tech.get(sim_sym) or self._tech_signal
        sim_entry = sim_state.ticker.last_price if sim_state else 0.0
        sim_atr   = sim_tech.atr_15m if (sim_tech and sim_tech.has_data) else 0.0
        equity    = account.balance.total_equity

        sim_dict: Optional[dict] = None
        if sim_entry > 0 and sim_atr > 0 and equity > 0:
            sim_dict = self._strategy.simulate(
                equity       = equity,
                goal_usd     = self.controller.goal_usd,
                max_loss_usd = self.controller.max_loss_usd,
                entry        = sim_entry,
                atr          = sim_atr,
                leverage     = self.controller.leverage,
                executor     = self._executor,
                symbol       = sim_sym,
            )

        # ── Actualizar paneles ──────────────────────────────────────────────
        self._order_panel.update(account, risk, sim_dict)
        self._cmd_center.update(account, risk, sim_dict,
                                market_states=self.stream.states)
        self._extractor_view.update(account, market_states=self.stream.states)
        self._journal_view.refresh()
        self._session_view.refresh()
        if settings.paper_trading:
            self._settings_view.refresh_paper_stats()

        # ── Detectar cierre de sesión TSAA → disparar analista ───────────────
        sess = self.controller._session
        if sess is None:
            # Sesión recién cerrada: detectar por el ID que guardamos
            if self._last_seen_session_status == "ACTIVE" and self._last_seen_session_id:
                self._analyst_view.notify_session_closed(self._last_seen_session_id)
                self._last_seen_session_status = "CLOSED"
        else:
            self._last_seen_session_id = sess.id
            self._last_seen_session_status = sess.status.value if hasattr(sess.status, "value") else str(sess.status)

        # ── Escribir JSON para la extensión GNOME Shell (cada ~2 s) ────────
        self._status_writer.tick(
            self._sym, state, sig, opp, risk, trend, account
        )

    async def _compute_multi_signals(self) -> None:
        """Corre en el bridge thread: analiza 15 símbolos sin bloquear GTK."""
        from streams.account import Position as _Pos
        new_opp:  dict = {}
        new_tech: dict = {}
        for sym in settings.symbol_list:
            try:
                st = self.stream.states.get(sym)
                if not st:
                    continue
                tr_s  = self._trend_analyzer.analyze(st)
                sig_s = self._abs_detector.analyze(st)
                lm_s  = self._liq_analyzer.analyze(st)
                rg_s  = self._regime_clf.classify(st, tr_s)
                new_opp[sym] = self._opp_scorer.score(sig_s, rg_s, tr_s, lm_s)
                k15 = self.klines.store.get(sym, settings.fast_kline)
                k1h = self.klines.store.get(sym, settings.slow_kline)
                if k15 and k1h:
                    dummy = _Pos(
                        symbol=sym, side="Buy", size=1,
                        entry_price=st.ticker.last_price,
                        mark_price=st.ticker.last_price,
                        leverage=5, unrealized_pnl=0,
                        liquidation_price=0, take_profit=0,
                        stop_loss=0, margin=1, created_time=0,
                    )
                    new_tech[sym] = self._tech_analyzer.analyze(dummy, k15, k1h)
            except Exception:
                pass
        # Merge results back (GIL protege los dict writes simples)
        self._multi_opp.update(new_opp)
        self._multi_tech.update(new_tech)


# ─── Carga de símbolos al inicio ──────────────────────────────────────────────

def _load_symbols_at_startup() -> None:
    """
    Política de 3 niveles para cargar el universo de símbolos:
      1. Bybit live (fetch_top_usdt_symbols_sync) → filtra blacklist → guarda en DB
      2. Cache DB (load_monitored_symbols) si Bybit no responde
      3. settings.symbols (.env) como último recurso
    En todos los casos actualiza settings.symbols para que MarketStream use
    la lista correcta.
    """
    import logging as _log
    _l = _log.getLogger("qts.startup")
    bl = settings.blacklist_set

    if settings.auto_load_symbols:
        fetched = BybitExecutor.fetch_top_usdt_symbols_sync(
            limit   = settings.max_symbols,
            testnet = settings.bybit_testnet,
        )
        if fetched:
            # Aplicar blacklist y guardar en DB
            filtered = [(sym, vol) for sym, vol in fetched if sym not in bl]
            save_monitored_symbols(filtered)
            syms = [sym for sym, _ in filtered]
            settings.symbols = ",".join(syms)
            _l.info("Símbolos cargados desde Bybit: %d pares (vol ≥ $10M)", len(syms))
            return

        # Bybit no respondió → intentar cache DB
        cached = load_monitored_symbols()
        cached = [s for s in cached if s not in bl]
        if cached:
            settings.symbols = ",".join(cached)
            _l.warning(
                "Bybit no disponible — usando cache DB: %d pares", len(cached)
            )
            return

        _l.warning("Sin datos de Bybit ni cache DB — usando .env como fallback")
    else:
        _l.info("auto_load_symbols desactivado — usando .env")


# ─── Aplicación ───────────────────────────────────────────────────────────────

class QTSApplication(Adw.Application):

    def __init__(self) -> None:
        super().__init__(application_id="com.qts.trading")

        # ── Carga dinámica de símbolos desde Bybit → DB ───────────────────────
        # Prioridad: (1) Bybit live  →  (2) Cache DB  →  (3) .env fallback
        # Se hace ANTES de crear MarketStream para que use la lista actualizada.
        _load_symbols_at_startup()

        self._stream   = MarketStream()
        self._acct     = AccountStream()
        self._klines   = KlineStream()
        self._bridge        = AsyncBridge()
        self._executor      = BybitExecutor()
        self._paper_wallet  = PaperWallet(settings.paper_balance)
        self._paper_exec    = PaperExecutor(self._paper_wallet, self._executor)
        self._strategy      = StrategyEngine()
        # El controller siempre usa PaperExecutor que adapta según settings.paper_trading
        self._controller = TradeController(
            executor      = self._paper_exec,
            strategy      = self._strategy,
            risk_fortress = RiskFortress(),
            bridge        = self._bridge,
            symbols       = settings.symbol_list,
        )

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

        # Inyectar market_states en el PaperExecutor (referencia compartida)
        self._paper_exec.market_states = self._stream.states

        # Arrancar async bridge + streams (mercado + cuenta + klines en paralelo)
        self._bridge.start()
        self._bridge.submit(self._stream.start())
        self._bridge.submit(self._acct.start())
        self._bridge.submit(self._klines.start())
        # Pre-cargar info de instrumentos para validaciones de orden
        self._bridge.submit(
            self._paper_exec.load_all_instruments(settings.symbol_list)
        )

    def do_activate(self) -> None:
        win = MainWindow(
            app          = self,
            stream       = self._stream,
            acct         = self._acct,
            klines       = self._klines,
            controller   = self._controller,
            strategy     = self._strategy,
            executor     = self._paper_exec,
            bridge       = self._bridge,
            paper_wallet = self._paper_wallet,
        )
        win.present()


# ─── Entrypoint ───────────────────────────────────────────────────────────────

def run() -> None:
    app = QTSApplication()
    app.run(None)
