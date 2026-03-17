"""
interface/terminal.py
─────────────────────
Dashboard terminal QTS — Phase 1: Inteligencia de Mercado.

Layout:
  ┌──────────────┬──────────────────┬──────────────────────┐
  │  ORDERBOOK   │  INTELIGENCIA    │  TAPE  +  LIQS       │
  ├──────────────┴──────────────────┴──────────────────────┤
  │  STATS BAR (CVD · Δ · Spot · Basis)                    │
  └────────────────────────────────────────────────────────┘

Paleta: Catppuccin Mocha (GNOME dark theme compatible)
"""
from __future__ import annotations

import time
from typing import List

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.reactive import reactive
from textual.widgets import Footer, Header, Static

from core.config import settings
from streams.market import CandleCVD, MarketState, MarketStream


# ─── Paleta Catppuccin Mocha ─────────────────────────────────────────────────

C = {
    "bg":       "#1e1e2e",
    "mantle":   "#181825",
    "surface0": "#313244",
    "surface1": "#45475a",
    "overlay0": "#6c7086",
    "text":     "#cdd6f4",
    "subtext1": "#bac2de",
    "subtext0": "#a6adc8",
    "red":      "#f38ba8",
    "green":    "#a6e3a1",
    "blue":     "#89b4fa",
    "lavender": "#b4befe",
    "yellow":   "#f9e2af",
    "peach":    "#fab387",
    "mauve":    "#cba6f7",
    "teal":     "#94e2d5",
    "sky":      "#89dceb",
}


# ─── Utilidades de formato ───────────────────────────────────────────────────

def fmt_price(p: float) -> str:
    if p == 0:      return "──────"
    if p >= 10_000: return f"{p:>10,.1f}"
    if p >= 1_000:  return f"{p:>10,.2f}"
    if p >= 10:     return f"{p:>10,.3f}"
    return          f"{p:>10,.4f}"


def fmt_qty(q: float) -> str:
    if q >= 1_000_000: return f"{q / 1_000_000:>8.2f}M"
    if q >= 1_000:     return f"{q:>8,.0f}"
    return             f"{q:>8.2f}"


def fmt_money(v: float, sign: bool = False) -> str:
    prefix = "+" if sign and v > 0 else ""
    av = abs(v)
    if av >= 1_000_000_000: return f"{prefix}{v / 1_000_000_000:.2f}B"
    if av >= 1_000_000:     return f"{prefix}{v / 1_000_000:.2f}M"
    if av >= 1_000:         return f"{prefix}{v:,.0f}"
    return                  f"{prefix}{v:.2f}"


def sign_color(val: float, pos: str = "green", neg: str = "red") -> str:
    return C[pos] if val >= 0 else C[neg]


def cvd_sparkline(candles: List[CandleCVD], n: int = 14) -> Text:
    """
    Mini gráfica de barras de los últimos N deltas de CVD.
    Verde = vela con más compras  |  Rojo = vela con más ventas
    Altura proporcional a la magnitud relativa.
    """
    BARS = "▁▂▃▄▅▆▇█"
    recent = list(candles)[-n:]

    if not recent:
        return Text("─" * n, style=C["overlay0"])

    max_abs = max(abs(c.delta) for c in recent) or 1.0
    t = Text()
    for c in recent:
        if abs(c.delta) < max_abs * 0.04:
            t.append("─", style=C["overlay0"])
        else:
            idx   = min(int(abs(c.delta) / max_abs * 7), 7)
            color = C["green"] if c.delta > 0 else C["red"]
            t.append(BARS[idx], style=color)
    return t


# ─── Widget: OrderBook ───────────────────────────────────────────────────────

class OrderBookWidget(Static):
    """
    Orderbook en tiempo real.
    Asks (rojo) arriba → spread → Bids (verde) abajo.
    Barras de volumen relativo + imbalance para detectar presión.
    """

    def update_state(self, state: MarketState) -> None:
        ob   = state.orderbook
        asks = ob.top_asks(10)[::-1]   # mayor ask primero (arriba)
        bids = ob.top_bids(10)          # mejor bid primero

        all_qtys = [q for _, q in asks + bids]
        max_qty  = max(all_qtys) if all_qtys else 1.0

        t = Text(overflow="fold")
        t.append("  ORDERBOOK\n", style=f"bold {C['blue']}")
        t.append(f"  {'PRECIO':>10}  {'CANTIDAD':>8}  {'':8}\n", style=C["overlay0"])

        def bar(qty: float) -> str:
            filled = min(int(qty / max_qty * 8), 8)
            return "█" * filled + "░" * (8 - filled)

        for price, qty in asks:
            t.append(f"  {fmt_price(price)}  {fmt_qty(qty)}  ", style=C["red"])
            t.append(f"{bar(qty)}\n", style=f"dim {C['red']}")

        # Spread + imbalance
        spread = ob.spread
        imb    = ob.imbalance
        imb_c  = C["green"] if imb > 0.55 else C["red"] if imb < 0.45 else C["yellow"]
        t.append(
            f"  {'─' * 10}  spr {fmt_price(spread).strip():<7}  imb ",
            style=C["overlay0"],
        )
        t.append(f"{imb * 100:.0f}%\n", style=f"bold {imb_c}")

        for price, qty in bids:
            t.append(f"  {fmt_price(price)}  {fmt_qty(qty)}  ", style=C["green"])
            t.append(f"{bar(qty)}\n", style=f"dim {C['green']}")

        self.update(t)


# ─── Widget: Inteligencia (Ticker + Phase 1 metrics) ─────────────────────────

class TickerWidget(Static):
    """
    Panel central: precio, spot, basis, funding countdown,
    OI + velocidad, CVD sparkline, presión compradora.
    """

    def update_state(self, state: MarketState) -> None:
        tk  = state.ticker
        ob  = state.orderbook
        t   = Text(overflow="fold")

        price_c = sign_color(tk.price_change_pct)
        fund_c  = C["red"] if tk.funding_rate > 0 else C["green"]

        t.append("  INTELIGENCIA\n", style=f"bold {C['blue']}")

        # ── Precio ────────────────────────────────────────────────
        t.append("\n")
        t.append("  Futuros  ", style=C["subtext0"])
        t.append(f"{fmt_price(tk.last_price).strip()}\n", style=f"bold {price_c}")

        if state.spot_connected:
            t.append("  Spot     ", style=C["subtext0"])
            t.append(f"{fmt_price(state.spot_price).strip()}\n", style=C["text"])

            # Basis
            basis_c = sign_color(state.basis)
            t.append("  Basis    ", style=C["subtext0"])
            t.append(
                f"{fmt_price(state.basis).strip()}  ({state.basis_pct:+.3f}%)\n",
                style=f"bold {basis_c}",
            )
        else:
            t.append("  Spot     ", style=C["subtext0"])
            t.append("conectando…\n", style=C["overlay0"])

        t.append("  24h      ", style=C["subtext0"])
        t.append(f"{tk.price_change_pct:+.2f}%\n", style=price_c)

        t.append(f"  {'─' * 22}\n", style=C["overlay0"])

        # ── Orderbook ─────────────────────────────────────────────
        t.append("  Bid      ", style=C["subtext0"])
        t.append(f"{fmt_price(tk.bid).strip()}\n",      style=C["green"])
        t.append("  Ask      ", style=C["subtext0"])
        t.append(f"{fmt_price(tk.ask).strip()}\n",      style=C["red"])
        t.append("  Mid      ", style=C["subtext0"])
        t.append(f"{fmt_price(ob.mid_price).strip()}\n", style=C["lavender"])

        t.append(f"  {'─' * 22}\n", style=C["overlay0"])

        # ── Funding ───────────────────────────────────────────────
        t.append("  Funding  ", style=C["subtext0"])
        t.append(f"{tk.funding_rate:+.4f}%  ", style=f"bold {fund_c}")
        t.append(f"{state.funding_countdown}\n", style=C["subtext0"])

        t.append(f"  {'─' * 22}\n", style=C["overlay0"])

        # ── Open Interest ─────────────────────────────────────────
        oi_vel_c = sign_color(state.oi_velocity)
        t.append("  OI       ", style=C["subtext0"])
        t.append(f"{fmt_money(tk.open_interest)}\n", style=C["mauve"])

        t.append("  OI vel   ", style=C["subtext0"])
        t.append(
            f"{fmt_money(state.oi_velocity, sign=True)}/min\n",
            style=f"bold {oi_vel_c}",
        )

        t.append("  Vol 24h  ", style=C["subtext0"])
        t.append(f"{fmt_money(tk.volume_24h)}\n", style=C["text"])

        t.append(f"  {'─' * 22}\n", style=C["overlay0"])

        # ── CVD sparkline ─────────────────────────────────────────
        interval_label = f"{settings.candle_interval // 60}m"
        t.append(f"  CVD {interval_label}  ", style=C["subtext0"])
        t.append_text(cvd_sparkline(list(state.cvd_candles)))
        t.append("\n")

        t.append(f"  {'─' * 22}\n", style=C["overlay0"])

        # ── Presión compradora ────────────────────────────────────
        buy_pct = state.buy_pct
        buy_c   = C["green"] if buy_pct >= 50 else C["red"]
        bar_n   = int(buy_pct / 10)
        buy_bar = "█" * bar_n + "░" * (10 - bar_n)
        t.append("  Compras  ", style=C["subtext0"])
        t.append(f"{buy_pct:.1f}%  ", style=f"bold {buy_c}")
        t.append(f"{buy_bar}\n",       style=buy_c)

        # Liquidaciones de sesión
        if state.liq_long_total > 0 or state.liq_short_total > 0:
            t.append(f"  {'─' * 22}\n", style=C["overlay0"])
            t.append("  Liq LONG  ", style=C["subtext0"])
            t.append(f"{fmt_money(state.liq_long_total)}\n",  style=C["red"])
            t.append("  Liq SHORT ", style=C["subtext0"])
            t.append(f"{fmt_money(state.liq_short_total)}\n", style=C["green"])

        t.append(f"  {'─' * 22}\n", style=C["overlay0"])

        # ── Status ────────────────────────────────────────────────
        if state.connected:
            elapsed = time.time() - state.last_update
            if elapsed < 2.0:
                t.append("  ● FUTUROS  ", style=f"bold {C['green']}")
            else:
                t.append(f"  ◐ {elapsed:.0f}s   ", style=f"bold {C['yellow']}")
        else:
            t.append("  ○ conectando…  ", style=f"bold {C['red']}")

        spot_sym = "●" if state.spot_connected else "○"
        spot_c   = C["teal"] if state.spot_connected else C["overlay0"]
        t.append(f"{spot_sym} SPOT\n", style=spot_c)

        self.update(t)


# ─── Widget: Tape + Liquidaciones ────────────────────────────────────────────

class TapeWidget(Static):
    """
    Panel derecho dividido en dos secciones:
      · TAPE — flujo de transacciones recientes
      · LIQUIDACIONES — posiciones forzadas en tiempo real
    """

    def update_state(self, state: MarketState) -> None:
        trades = state.recent_trades(14)
        liqs   = state.recent_liquidations(7)
        t = Text(overflow="fold")

        # ── Trades ────────────────────────────────────────────────
        t.append("  TAPE\n", style=f"bold {C['blue']}")
        t.append(
            f"  {'PRECIO':>10}  {'LADO':6}  {'CANTIDAD':>10}\n",
            style=C["overlay0"],
        )
        for tr in trades:
            is_buy   = tr.side == "Buy"
            col      = C["green"] if is_buy else C["red"]
            side_lbl = "▲ BUY " if is_buy else "▼ SELL"
            t.append(
                f"  {fmt_price(tr.price)}  {side_lbl}  {fmt_qty(tr.qty)}\n",
                style=col,
            )

        # ── Liquidaciones ─────────────────────────────────────────
        t.append(f"\n  {'─' * 26}\n", style=C["overlay0"])
        t.append("  LIQUIDACIONES\n", style=f"bold {C['mauve']}")

        if not liqs:
            t.append("  —\n", style=C["overlay0"])
        else:
            t.append(
                f"  {'TIPO':5}  {'PRECIO':>10}  {'USD':>10}\n",
                style=C["overlay0"],
            )
            for liq in liqs:
                # LONG liq = precio bajó y eliminó longs = rojo
                # SHORT liq = precio subió y eliminó shorts = verde
                col  = C["red"] if liq.is_long_liq else C["green"]
                icon = "💀" if liq.notional >= 100_000 else "⚡"
                pos  = liq.position_type
                t.append(
                    f"  {icon} {pos:<5}  {fmt_price(liq.price)}  {fmt_money(liq.notional):>10}\n",
                    style=col,
                )

        self.update(t)


# ─── Widget: Stats Bar ───────────────────────────────────────────────────────

class StatsBar(Static):
    """
    Barra inferior: CVD acumulado, delta, spot, basis.
    Placeholders para Fases 2-4 (régimen, absorción, score).
    """

    def update_state(self, state: MarketState) -> None:
        cvd   = state.cvd
        delta = state.session_delta
        t     = Text()

        t.append("  CVD ", style=C["subtext0"])
        t.append(f"{fmt_money(cvd, sign=True)}", style=f"bold {sign_color(cvd)}")

        t.append("   Δ ", style=C["subtext0"])
        t.append(f"{fmt_money(delta, sign=True)}", style=f"bold {sign_color(delta)}")

        if state.spot_connected:
            t.append("   Spot ", style=C["subtext0"])
            t.append(f"{fmt_price(state.spot_price).strip()}", style=C["teal"])
            t.append("   Basis ", style=C["subtext0"])
            basis_c = sign_color(state.basis)
            t.append(f"{state.basis_pct:+.3f}%", style=f"bold {basis_c}")

        t.append("   Compras ", style=C["subtext0"])
        t.append(f"{state.buy_pct:.1f}%", style=sign_color(state.buy_pct - 50))

        t.append("   RÉGIMEN ", style=C["subtext0"])
        t.append("──", style=C["overlay0"])

        t.append("   ABSORCIÓN ", style=C["subtext0"])
        t.append("──", style=C["overlay0"])

        t.append("   SCORE ", style=C["subtext0"])
        t.append("──", style=C["overlay0"])

        self.update(t)


# ─── App principal ───────────────────────────────────────────────────────────

class TradingApp(App):
    """QTS — Quantum Trading System · Terminal Dashboard · Phase 1."""

    TITLE = "⚡ QTS — Quantum Trading System"

    CSS = f"""
    Screen {{
        background: {C['bg']};
        color: {C['text']};
    }}
    Header {{
        background: {C['mantle']};
        color: {C['blue']};
        text-style: bold;
    }}
    Footer {{
        background: {C['mantle']};
        color: {C['overlay0']};
    }}
    #main {{
        height: 1fr;
    }}
    OrderBookWidget {{
        width: 1fr;
        height: 100%;
        border: solid {C['surface0']};
        background: {C['mantle']};
    }}
    TickerWidget {{
        width: 34;
        height: 100%;
        border: solid {C['surface0']};
        background: {C['mantle']};
    }}
    TapeWidget {{
        width: 1fr;
        height: 100%;
        border: solid {C['surface0']};
        background: {C['mantle']};
    }}
    StatsBar {{
        height: 3;
        background: {C['surface0']};
        border-top: solid {C['surface1']};
        padding: 1 0;
        content-align: left middle;
    }}
    """

    BINDINGS = [
        Binding("1", "select('XRPUSDT')", "XRP",  show=True),
        Binding("2", "select('SOLUSDT')", "SOL",  show=True),
        Binding("3", "select('BTCUSDT')", "BTC",  show=True),
        Binding("4", "select('ETHUSDT')", "ETH",  show=True),
        Binding("5", "select('XLMUSDT')", "XLM",  show=True),
        Binding("r", "reset_cvd",         "Reset CVD", show=True),
        Binding("q", "quit",              "Salir", show=True),
    ]

    current_symbol: reactive[str] = reactive(settings.default_symbol)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main"):
            yield OrderBookWidget(id="orderbook")
            yield TickerWidget(id="ticker")
            yield TapeWidget(id="tape")
        yield StatsBar(id="stats")
        yield Footer()

    def on_mount(self) -> None:
        self.stream = MarketStream()
        self.run_worker(self.stream.start(), exclusive=False, name="market_stream")
        self.set_interval(0.1, self._refresh)

    def _refresh(self) -> None:
        state = self.stream.states.get(self.current_symbol)
        if state is None:
            return
        self.query_one("#orderbook", OrderBookWidget).update_state(state)
        self.query_one("#ticker",    TickerWidget).update_state(state)
        self.query_one("#tape",      TapeWidget).update_state(state)
        self.query_one("#stats",     StatsBar).update_state(state)

        status = "● LIVE" if state.connected else "○ conectando"
        spot   = " · SPOT ●" if state.spot_connected else ""
        self.sub_title = f"{self.current_symbol}  │  {status}{spot}"

    def action_select(self, symbol: str) -> None:
        if symbol in self.stream.states:
            self.current_symbol = symbol
            self._refresh()

    def action_reset_cvd(self) -> None:
        state = self.stream.states.get(self.current_symbol)
        if state:
            state.reset_session()
