"""
interface/extractor_view.py
────────────────────────────
Pestaña "🤖 Extractor" — Sesión de trading asistida por Claude.

· Campos: goal ($), max loss ($), loop interval (min)
· "Copiar prompt" → genera prompt de sesión al portapapeles
· "Loop automático" → ejecuta `claude --print` cada N minutos
· Muestra posiciones abiertas en tiempo real (estilo CommandCenter)
· Panel de log desde /tmp/qts_claude.log (auto-scroll)
· Stats de sesión: PnL acumulado, trades abiertos, estado del loop
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from streams.account import AccountState

SESSION_CONFIG = Path("/tmp/qts_session.json")
SESSION_LOG    = Path("/tmp/qts_claude.log")

# ── Paleta (igual que el resto de la app) ────────────────────────────────────
HEX = {
    "buy":    "#57e389",
    "sell":   "#ff7b63",
    "blue":   "#78aeed",
    "warn":   "#f8e45c",
    "purple": "#dc8add",
    "teal":   "#93ddc2",
    "text":   "#ebebeb",
    "sub":    "#9a9996",
    "over":   "#5e5c64",
}


def _ts() -> str:
    return time.strftime("%H:%M:%S")


def _fp(p: float) -> str:
    if p == 0:      return "──"
    if p >= 10_000: return f"{p:,.1f}"
    if p >= 1_000:  return f"{p:,.2f}"
    if p >= 10:     return f"{p:.3f}"
    return f"{p:.4f}"


def _ml() -> Gtk.Label:
    lbl = Gtk.Label()
    lbl.set_use_markup(True)
    lbl.set_xalign(0)
    lbl.set_halign(Gtk.Align.START)
    return lbl


def _sep() -> Gtk.Separator:
    sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
    sep.set_margin_top(3)
    sep.set_margin_bottom(3)
    return sep


# ─── Tarjeta compacta de posición ────────────────────────────────────────────

class PositionMiniCard(Gtk.Box):
    """Tarjeta de posición Bybit — read-only con botón Cerrar."""

    def __init__(self, on_close_cb):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.add_css_class("card")
        self.set_margin_top(2)
        self.set_margin_bottom(2)
        self.set_margin_start(6)
        self.set_margin_end(6)

        self._symbol      = None
        self._on_close_cb = on_close_cb

        # Fila 1: sym │ pnl │ progress │ close btn
        row1 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row1.set_margin_start(6)
        row1.set_margin_end(6)
        row1.set_margin_top(4)

        self._sym_lbl  = _ml()
        self._pnl_lbl  = _ml()
        self._pnl_lbl.set_hexpand(True)
        self._prog_lbl = _ml()

        close_btn = Gtk.Button(label="✗")
        close_btn.add_css_class("destructive-action")
        close_btn.set_size_request(30, -1)
        close_btn.connect("clicked", self._on_close)

        row1.append(self._sym_lbl)
        row1.append(self._pnl_lbl)
        row1.append(self._prog_lbl)
        row1.append(close_btn)
        self.append(row1)

        # Fila 2: Entry / SL / TP │ notional
        row2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        row2.set_margin_start(6)
        row2.set_margin_end(6)
        row2.set_margin_bottom(4)

        self._price_lbl = _ml()
        self._price_lbl.set_hexpand(True)
        self._meta_lbl  = _ml()

        row2.append(self._price_lbl)
        row2.append(self._meta_lbl)
        self.append(row2)

    def _on_close(self, _btn) -> None:
        if self._symbol and self._on_close_cb:
            self._on_close_cb(self._symbol)

    def update(self, pos, mark_override: float = 0.0) -> None:
        self._symbol = pos.symbol
        sym      = pos.symbol.replace("USDT", "")
        is_long  = pos.is_long
        col      = HEX["buy"] if is_long else HEX["sell"]
        arrow    = "▲" if is_long else "▼"

        self._sym_lbl.set_markup(
            f'<span color="{col}" weight="bold" size="large">{arrow} {sym}</span>'
            f'  <span color="{HEX["warn"]}" size="small">{pos.leverage}x</span>'
        )

        # Usar precio de mercado en tiempo real (ticker WebSocket) si está disponible.
        # pos.mark_price y pos.unrealized_pnl vienen del topic "position" de Bybit
        # que solo se actualiza en eventos (fill, cambio de SL…), no en cada tick.
        entry = pos.entry_price
        mark  = (mark_override if mark_override > 0
                 else pos.mark_price if pos.mark_price > 0 else entry)

        # Recalcular PnL en tiempo real (igual que Bybit: sin fees, solo precio)
        if pos.size > 0 and entry > 0 and mark > 0:
            upnl = ((mark - entry) * pos.size if is_long
                    else (entry - mark) * pos.size)
        else:
            upnl = pos.unrealized_pnl

        sign    = "+" if upnl >= 0 else ""
        pnl_col = HEX["buy"] if upnl >= 0 else HEX["sell"]

        # ROI como % del margen (igual que Bybit)
        roi_pct = (upnl / pos.margin * 100) if pos.margin > 0 else 0.0
        roi_sign = "+" if roi_pct >= 0 else ""
        self._pnl_lbl.set_markup(
            f'<span color="{pnl_col}" weight="bold" size="large">{sign}${upnl:.2f}</span>'
            f'  <span color="{pnl_col}" size="small">({roi_sign}{roi_pct:.1f}%)</span>'
        )

        # Progress toward TP
        tp = pos.take_profit
        if tp > 0 and entry > 0:
            tp_dist = abs(tp - entry)
            prog = ((mark - entry) / tp_dist if is_long else (entry - mark) / tp_dist) if tp_dist > 0 else 0.0
            frac = max(0.0, min(1.0, prog))
        else:
            frac = 0.0
        pct_col = HEX["buy"] if frac >= 0.4 else HEX["warn"] if frac >= 0.1 else HEX["sell"]
        self._prog_lbl.set_markup(
            f'<span color="{pct_col}" weight="bold">TP {frac*100:.0f}%</span>'
        )

        sl      = pos.stop_loss
        at_be   = (is_long and sl >= entry) or (not is_long and sl <= entry)
        sl_col  = HEX["buy"] if at_be else HEX["sell"]
        self._price_lbl.set_markup(
            f'<span color="{HEX["sub"]}" size="small">Entry </span>'
            f'<span color="{HEX["text"]}" size="small">{_fp(entry)}</span>'
            f'  <span color="{sl_col}" size="small">SL {_fp(sl)}</span>'
            f'  <span color="{HEX["buy"]}" size="small">TP {_fp(tp)}</span>'
        )

        notional = pos.size * entry
        self._meta_lbl.set_markup(
            f'<span color="{HEX["sub"]}" size="small">${notional:,.1f}  '
            f'qty {pos.size}</span>'
        )


# ─── Vista principal ──────────────────────────────────────────────────────────

class ExtractorView(Gtk.Box):
    """Tab 🤖 Extractor — sesión Claude + posiciones + log en tiempo real."""

    def __init__(self, executor, bridge) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        self._executor    = executor
        self._bridge      = bridge
        self._acct_state: Optional[AccountState] = None
        self._cards: dict[str, PositionMiniCard] = {}   # sym → card

        self._loop_timer: Optional[int] = None
        self._log_size = 0

        self._build_ui()

        # Poll log file every second
        GLib.timeout_add(1000, self._tick_log)

    # ── Construcción de UI ───────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # ── Barra de configuración ──────────────────────────────────────────
        cfg = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        cfg.set_margin_top(6)
        cfg.set_margin_bottom(4)
        cfg.set_margin_start(8)
        cfg.set_margin_end(8)

        def _field(label: str, default: str, width: int) -> tuple[Gtk.Label, Gtk.Entry]:
            lbl = Gtk.Label(label=label)
            lbl.add_css_class("dim-label")
            ent = Gtk.Entry()
            ent.set_text(default)
            ent.set_size_request(width, -1)
            return lbl, ent

        goal_lbl,     self._goal_ent     = _field("Meta $",    "1.00", 68)
        loss_lbl,     self._loss_ent     = _field("Max loss $", "0.30", 68)
        interval_lbl, self._interval_ent = _field("Loop c/",    "5",    44)

        min_lbl = Gtk.Label(label="min")
        min_lbl.add_css_class("dim-label")

        spacer = Gtk.Box()
        spacer.set_hexpand(True)

        self._copy_btn = Gtk.Button(label="📋 Copiar prompt")
        self._copy_btn.connect("clicked", self._on_copy_prompt)

        self._loop_btn = Gtk.Button(label="▶ Iniciar loop")
        self._loop_btn.add_css_class("suggested-action")
        self._loop_btn.connect("clicked", self._on_toggle_loop)

        self._clear_btn = Gtk.Button(label="🗑 Log")
        self._clear_btn.add_css_class("flat")
        self._clear_btn.connect("clicked", self._on_clear_log)

        for w in [goal_lbl, self._goal_ent,
                  loss_lbl, self._loss_ent,
                  interval_lbl, self._interval_ent, min_lbl,
                  spacer,
                  self._copy_btn, self._loop_btn, self._clear_btn]:
            cfg.append(w)
        self.append(cfg)

        # ── Stats bar ───────────────────────────────────────────────────────
        self._stats_lbl = _ml()
        self._stats_lbl.set_margin_start(8)
        self._stats_lbl.set_margin_end(8)
        self._stats_lbl.set_margin_bottom(4)
        self._refresh_stats(0.0, 0)
        self.append(self._stats_lbl)

        self.append(_sep())

        # ── Contenido: posiciones (izq) │ log (der) ─────────────────────────
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        content.set_vexpand(True)

        # Izquierda: lista de posiciones
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        left.set_size_request(330, -1)

        pos_hdr = Gtk.Label()
        pos_hdr.set_markup(
            f'<span color="{HEX["sub"]}" size="small">  POSICIONES ACTIVAS</span>'
        )
        pos_hdr.set_xalign(0)
        pos_hdr.set_margin_top(4)
        pos_hdr.set_margin_bottom(4)
        left.append(pos_hdr)

        self._cards_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)

        self._no_pos_lbl = Gtk.Label()
        self._no_pos_lbl.set_markup(
            f'<span color="{HEX["over"]}" size="small">  Sin posiciones abiertas</span>'
        )
        self._no_pos_lbl.set_xalign(0)
        self._no_pos_lbl.set_margin_top(8)
        self._cards_box.append(self._no_pos_lbl)

        pos_scroll = Gtk.ScrolledWindow()
        pos_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        pos_scroll.set_vexpand(True)
        pos_scroll.set_child(self._cards_box)
        left.append(pos_scroll)

        # Derecha: log panel
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        right.set_hexpand(True)

        log_hdr = Gtk.Label()
        log_hdr.set_markup(
            f'<span color="{HEX["sub"]}" size="small">  ACTIVIDAD DE CLAUDE</span>'
        )
        log_hdr.set_xalign(0)
        log_hdr.set_margin_top(4)
        log_hdr.set_margin_bottom(4)
        right.append(log_hdr)

        self._log_buf  = Gtk.TextBuffer()
        self._log_view = Gtk.TextView(buffer=self._log_buf)
        self._log_view.set_editable(False)
        self._log_view.set_cursor_visible(False)
        self._log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._log_view.set_monospace(True)
        self._log_view.set_margin_start(6)
        self._log_view.set_margin_end(6)

        self._log_scroll = Gtk.ScrolledWindow()
        self._log_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._log_scroll.set_vexpand(True)
        self._log_scroll.set_hexpand(True)
        self._log_scroll.set_child(self._log_view)
        right.append(self._log_scroll)

        vsep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        content.append(left)
        content.append(vsep)
        content.append(right)

        self.append(content)

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _goal(self) -> float:
        try:
            return max(0.01, float(self._goal_ent.get_text().replace(",", ".")))
        except ValueError:
            return 1.0

    def _max_loss(self) -> float:
        try:
            return max(0.01, float(self._loss_ent.get_text().replace(",", ".")))
        except ValueError:
            return 0.30

    def _interval_min(self) -> int:
        try:
            return max(1, int(self._interval_ent.get_text()))
        except ValueError:
            return 5

    def _write_session_config(self) -> None:
        SESSION_CONFIG.write_text(json.dumps({
            "goal":     self._goal(),
            "max_loss": self._max_loss(),
            "started":  time.time(),
        }))

    def _build_prompt(self) -> str:
        goal     = self._goal()
        max_loss = self._max_loss()
        equity_s = ""
        if self._acct_state:
            eq = self._acct_state.balance.total_equity
            equity_s = f" Balance: ${eq:.2f} USDT."

        return (
            f"Sesión de trading QTS. Meta: ${goal:.2f} USDT. Máx pérdida: ${max_loss:.2f} USDT.{equity_s} "
            f"Pasos: "
            f"1) get_signals() — identifica top setups. "
            f"2) get_account() — verifica equity y margen disponible. "
            f"3) get_positions() — revisa posiciones activas. "
            f"4) Gestionar posiciones: si progress>50% mueve SL a breakeven con modify_sl_tp(); "
            f"si momentum se debilita (score<2, CVD inverso) cierra con close_position(). "
            f"5) Si hay margen y no hay posición en el mejor símbolo, entra con place_order(). "
            f"Calcula qty para que beneficio neto (descontando fees ~0.11% RT) = meta. "
            f"No entres si el riesgo supera max_loss. Documenta cada decisión en 1 línea."
        )

    def _append_log(self, text: str) -> None:
        end = self._log_buf.get_end_iter()
        self._log_buf.insert(end, text)
        # Auto-scroll
        GLib.idle_add(self._scroll_log_to_bottom)

    def _scroll_log_to_bottom(self) -> bool:
        adj = self._log_scroll.get_vadjustment()
        adj.set_value(adj.get_upper() - adj.get_page_size())
        return False

    def _refresh_stats(self, pnl: float, trade_count: int) -> None:
        goal    = self._goal()
        pct     = min(100.0, pnl / goal * 100) if goal > 0 else 0.0
        col     = HEX["buy"] if pnl >= 0 else HEX["sell"]
        sign    = "+" if pnl >= 0 else ""
        status  = f'<span color="{HEX["teal"]}" size="small">🔄 Loop activo</span>' \
                  if self._loop_timer else \
                  f'<span color="{HEX["over"]}" size="small">💤 En espera</span>'

        self._stats_lbl.set_markup(
            f'<span color="{HEX["sub"]}" size="small">Sesión PnL: </span>'
            f'<span color="{col}" weight="bold" size="small">{sign}${pnl:.2f}</span>'
            f'<span color="{HEX["sub"]}" size="small"> / ${goal:.2f}  </span>'
            f'<span color="{HEX["teal"]}" size="small">({pct:.0f}%)  </span>'
            f'<span color="{HEX["sub"]}" size="small">Posiciones: </span>'
            f'<span color="{HEX["text"]}" size="small">{trade_count}  </span>'
            + status
        )

    # ── Callbacks UI ─────────────────────────────────────────────────────────

    def _on_copy_prompt(self, _btn) -> None:
        self._write_session_config()
        prompt = self._build_prompt()
        clipboard = self.get_clipboard()
        clipboard.set(prompt)
        self._append_log(f"[{_ts()}] Prompt copiado al portapapeles.\n")

    def _on_toggle_loop(self, _btn) -> None:
        if self._loop_timer is not None:
            GLib.source_remove(self._loop_timer)
            self._loop_timer = None
            self._loop_btn.set_label("▶ Iniciar loop")
            self._loop_btn.remove_css_class("destructive-action")
            self._loop_btn.add_css_class("suggested-action")
            self._append_log(f"[{_ts()}] Loop detenido.\n")
        else:
            self._write_session_config()
            interval_ms    = self._interval_min() * 60 * 1000
            self._loop_timer = GLib.timeout_add(interval_ms, self._fire_loop)
            self._loop_btn.set_label("⏸ Detener loop")
            self._loop_btn.remove_css_class("suggested-action")
            self._loop_btn.add_css_class("destructive-action")
            self._append_log(f"[{_ts()}] Loop iniciado (cada {self._interval_min()} min).\n")
            # Ejecutar inmediatamente
            self._fire_loop()

    def _fire_loop(self) -> bool:
        """Ejecuta claude --print en background. Retorna True para mantener el timer."""
        prompt = self._build_prompt()
        self._append_log(f"[{_ts()}] ── Ejecutando sesión claude ──\n")

        def _run():
            try:
                proc = subprocess.Popen(
                    ["claude", "--print", "--output-format", "text", prompt],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    cwd="/home/dev/Projects/trading",
                )
                out, err = proc.communicate(timeout=180)
                with open(SESSION_LOG, "a") as f:
                    if out:
                        f.write(f"\n[{_ts()}] Claude:\n{out}\n{'─'*60}\n")
                    if err and err.strip():
                        f.write(f"[{_ts()}] [stderr] {err[:300]}\n")
            except subprocess.TimeoutExpired:
                proc.kill()
                with open(SESSION_LOG, "a") as f:
                    f.write(f"[{_ts()}] [timeout] claude --print excedió 180s.\n")
            except Exception as e:
                with open(SESSION_LOG, "a") as f:
                    f.write(f"[{_ts()}] [error] {e}\n")

        threading.Thread(target=_run, daemon=True).start()
        return True  # mantener timer

    def _on_clear_log(self, _btn) -> None:
        self._log_buf.set_text("")
        if SESSION_LOG.exists():
            SESSION_LOG.write_text("")
        self._log_size = 0
        self._append_log(f"[{_ts()}] Log limpiado.\n")

    def _on_close_position(self, symbol: str) -> None:
        pos = (self._acct_state.positions.get(symbol)
               if self._acct_state else None)
        if pos and pos.size > 0:
            self._append_log(f"[{_ts()}] Cerrando {symbol}...\n")
            self._bridge.submit(
                self._executor.close_position(symbol, pos.size, pos.side)
            )

    # ── Timer: poll log file ─────────────────────────────────────────────────

    def _tick_log(self) -> bool:
        """Lee nuevas líneas del log file cada 1 s."""
        try:
            if SESSION_LOG.exists():
                size = SESSION_LOG.stat().st_size
                if size > self._log_size:
                    with open(SESSION_LOG, "r", errors="replace") as f:
                        f.seek(self._log_size)
                        new = f.read()
                    self._log_size = size
                    if new:
                        self._append_log(new)
        except Exception:
            pass
        return True

    # ── Actualización desde gtk_app ──────────────────────────────────────────

    def update(self, acct_state: AccountState, market_states: dict = None) -> None:
        """Llamado ~10fps desde _do_refresh en gtk_app."""
        self._acct_state = acct_state
        positions = acct_state.open_positions()
        current_syms = {p.symbol for p in positions}
        ms_map = market_states or {}

        # Ocultar/mostrar "sin posiciones"
        self._no_pos_lbl.set_visible(len(positions) == 0)

        # Eliminar tarjetas de posiciones cerradas
        for sym in list(self._cards.keys()):
            if sym not in current_syms:
                card = self._cards.pop(sym)
                self._cards_box.remove(card)

        # Añadir o actualizar tarjetas
        total_pnl = 0.0
        for pos in positions:
            if pos.symbol not in self._cards:
                card = PositionMiniCard(on_close_cb=self._on_close_position)
                self._cards[pos.symbol] = card
                self._cards_box.append(card)

            # Precio en tiempo real del ticker (más actualizado que pos.mark_price)
            ms = ms_map.get(pos.symbol)
            live_mark = ms.ticker.last_price if (ms and ms.ticker.last_price > 0) else 0.0
            self._cards[pos.symbol].update(pos, mark_override=live_mark)

            # PnL calculado con precio real para el total
            entry = pos.entry_price
            mark  = live_mark if live_mark > 0 else (pos.mark_price if pos.mark_price > 0 else entry)
            if pos.size > 0 and entry > 0 and mark > 0:
                pnl = ((mark - entry) * pos.size if pos.is_long
                       else (entry - mark) * pos.size)
            else:
                pnl = pos.unrealized_pnl
            total_pnl += pnl

        self._refresh_stats(total_pnl, len(positions))
