"""
interface/order_panel.py
─────────────────────────
OrderPanel — Centro de órdenes QTS.
Soporta hasta MAX_POSITIONS trades simultáneos.
"""
from __future__ import annotations

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
    from streams.account import AccountState
    from core.risk import RiskStatus


# ─── Colores ──────────────────────────────────────────────────────────────────

HEX = {
    "buy":   "#57e389",
    "sell":  "#ff7b63",
    "blue":  "#78aeed",
    "warn":  "#f8e45c",
    "teal":  "#93ddc2",
    "text":  "#ebebeb",
    "sub":   "#9a9996",
    "over":  "#5e5c64",
}

MODE_COLORS = {
    AutoMode.MANUAL:     ("MANUAL",    "over"),
    AutoMode.SUGGEST:    ("SUGGEST",   "blue"),
    AutoMode.AUTO_ENTRY: ("AUTO",      "warn"),
    AutoMode.FULL_AUTO:  ("FULL AUTO", "sell"),
}


def _ml(text: str = "") -> Gtk.Label:
    lbl = Gtk.Label(label=text)
    lbl.set_xalign(0)
    lbl.set_use_markup(True)
    lbl.add_css_class("qts-mono-sm")
    lbl.set_max_width_chars(34)
    lbl.set_ellipsize(Pango.EllipsizeMode.END)
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
    if p >= 1000: return f"{p:.2f}"
    if p >= 10:   return f"{p:.4f}"
    return f"{p:.5f}"


# ─── Widget de un trade activo ────────────────────────────────────────────────

class _TradeRow(Gtk.Box):
    """
    Fila compacta para un trade activo.
    Se reutiliza: llamar show_trade() o clear() en cada refresh.
    """

    def __init__(self, controller: "TradeController") -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._controller = controller
        self._symbol: str = ""

        self._header = _ml()
        self._levels = _ml()
        self._pnl    = _ml()

        prog_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self._prog = Gtk.ProgressBar()
        self._prog.set_hexpand(True)
        self._prog.set_show_text(True)
        self._close_btn = Gtk.Button(label="✗")
        self._close_btn.add_css_class("destructive-action")
        self._close_btn.set_size_request(28, -1)
        self._close_btn.connect("clicked", self._on_close)
        prog_row.append(self._prog)
        prog_row.append(self._close_btn)

        # ── Razonamiento del Agente IA ────────────────────────────────────
        self._ai_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self._ai_box.set_margin_top(4)
        self._ai_box.set_visible(False)

        ai_header_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self._ai_toggle_btn = Gtk.Button()
        self._ai_toggle_btn.set_has_frame(False)
        self._ai_toggle_btn.connect("clicked", self._on_ai_toggle)
        self._ai_badge = Gtk.Label()
        self._ai_badge.set_use_markup(True)
        self._ai_badge.set_xalign(0)
        self._ai_badge.set_markup(
            f'<span color="{HEX["blue"]}" size="small" weight="bold">🤖 Ver análisis IA ▾</span>'
        )
        self._ai_toggle_btn.set_child(self._ai_badge)
        ai_header_row.append(self._ai_toggle_btn)
        self._ai_box.append(ai_header_row)

        self._ai_reasoning_lbl = Gtk.Label()
        self._ai_reasoning_lbl.set_xalign(0)
        self._ai_reasoning_lbl.set_wrap(True)
        self._ai_reasoning_lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self._ai_reasoning_lbl.set_max_width_chars(38)
        self._ai_reasoning_lbl.set_visible(False)   # colapsado por defecto
        attrs = Pango.AttrList()
        attrs.insert(Pango.attr_scale_new(0.82))
        attrs.insert(Pango.attr_foreground_new(0xcccc, 0xcccc, 0xcccc))
        self._ai_reasoning_lbl.set_attributes(attrs)
        self._ai_box.append(self._ai_reasoning_lbl)
        self._ai_expanded = False

        for w in [self._header, self._levels, self._pnl, prog_row, self._ai_box]:
            self.append(w)

    def _on_close(self, _btn) -> None:
        if self._symbol:
            self._controller.close_symbol(self._symbol)

    def _on_ai_toggle(self, _btn) -> None:
        self._ai_expanded = not self._ai_expanded
        self._ai_reasoning_lbl.set_visible(self._ai_expanded)
        arrow = "▴" if self._ai_expanded else "▾"
        self._ai_badge.set_markup(
            f'<span color="{HEX["blue"]}" size="small" weight="bold">'
            f'🤖 Análisis IA {arrow}</span>'
        )

    def show_trade(
        self,
        trade:   "TradeRecord",
        mark:    float,
        upnl:    float,
    ) -> None:
        self.set_visible(True)
        req = trade.request
        if not req:
            return

        self._symbol = trade.symbol
        sym   = trade.symbol.replace("USDT", "")
        col   = HEX["buy"] if req.side == "Buy" else HEX["sell"]
        arrow = "▲" if req.side == "Buy" else "▼"

        state_labels = {
            TradeState.SUBMITTED: ("WAIT",       "warn"),
            TradeState.OPEN:      ("OPEN",        "text"),
            TradeState.BREAKEVEN: ("BREAKEVEN ✓", "buy"),
            TradeState.TRAILING:  ("TRAILING ↑",  "teal"),
        }
        s_label, s_color = state_labels.get(trade.state, ("??", "over"))

        self._header.set_markup(
            f'<span color="{col}" weight="bold">{arrow} {sym}</span>'
            f'  <span color="{HEX[s_color]}">{s_label}</span>'
        )

        self._levels.set_markup(
            f'<span color="{HEX["sell"]}">SL {_fp(trade.current_sl)}</span>'
            f'  <span color="{HEX["buy"]}">TP {_fp(trade.current_tp)}</span>'
        )

        sign    = "+" if upnl >= 0 else ""
        pnl_col = HEX["buy"] if upnl >= 0 else HEX["sell"]
        self._pnl.set_markup(
            f'<span color="{pnl_col}" weight="bold">{sign}${upnl:.2f}</span>'
            f'  <span color="{HEX["sub"]}">mark {_fp(mark)}</span>'
        )

        # Barra de progreso
        entry   = trade.entry_price or req.entry_price
        tp      = trade.current_tp
        tp_dist = abs(tp - entry) if tp > 0 and entry > 0 else 1
        if tp_dist > 0 and entry > 0:
            is_long = req.side == "Buy"
            prog = (mark - entry) / tp_dist if is_long else (entry - mark) / tp_dist
            frac = max(0.0, min(1.0, prog))
            goal_real = req.qty * tp_dist
            self._prog.set_fraction(frac)
            self._prog.set_text(f"{sign}${upnl:.2f} / ${goal_real:.2f}  ({frac*100:.0f}%)")
        else:
            self._prog.set_fraction(0.0)
            self._prog.set_text(f"entry {_fp(entry)}")

        # Razonamiento del Agente IA
        ai_text = trade.ai_reasoning or req.ai_reasoning
        if ai_text:
            self._ai_box.set_visible(True)
            self._ai_reasoning_lbl.set_text(ai_text)
        else:
            self._ai_box.set_visible(False)

    def clear(self) -> None:
        self.set_visible(False)
        self._symbol = ""


# ─── OrderPanel ───────────────────────────────────────────────────────────────

class OrderPanel(Gtk.Box):

    N_LOG = 5

    def __init__(self, controller: "TradeController") -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add_css_class("qts-card")
        self.set_hexpand(False)
        self.set_size_request(280, -1)

        self._controller = controller
        self._jnl_last_refresh: float = 0.0
        self._jnl_log_len: int = -1
        controller.on_update(self._on_controller_update)

        self._build_ui()

    def _build_ui(self) -> None:
        title = Gtk.Label(label="⚡ CENTRO DE ÓRDENES")
        title.add_css_class("qts-title")
        title.set_xalign(0)
        self.append(title)
        self.append(_sep())

        # ── Modo ────────────────────────────────────────────────────────────
        self.append(_section("MODO"))
        self._mode_btns: dict[AutoMode, Gtk.ToggleButton] = {}
        mode_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                           spacing=0, css_classes=["symbol-group"])
        first: Optional[Gtk.ToggleButton] = None
        for mode, (label, _) in MODE_COLORS.items():
            btn = Gtk.ToggleButton(label=label)
            btn.set_active(mode == AutoMode.MANUAL)
            if first is None:
                first = btn
            else:
                btn.set_group(first)
            btn.connect("toggled", self._on_mode_toggled, mode)
            self._mode_btns[mode] = btn
            mode_box.append(btn)
        self.append(mode_box)

        self._mode_desc = _ml()
        self.append(self._mode_desc)
        self.append(_sep())

        # ── Objetivo ─────────────────────────────────────────────────────
        self.append(_section("OBJETIVO"))
        row1 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        goal_lbl = Gtk.Label(label="Ganar $")
        goal_lbl.add_css_class("qts-label")
        self._goal_spin = Gtk.SpinButton()
        self._goal_spin.set_adjustment(
            Gtk.Adjustment(value=1.0, lower=0.1, upper=500.0, step_increment=0.5, page_increment=5.0)
        )
        self._goal_spin.set_digits(2)
        self._goal_spin.set_size_request(72, -1)
        self._goal_spin.connect("value-changed", self._on_goal_changed)

        loss_lbl = Gtk.Label(label="Perder máx $")
        loss_lbl.add_css_class("qts-label")
        self._loss_spin = Gtk.SpinButton()
        self._loss_spin.set_adjustment(
            Gtk.Adjustment(value=0.50, lower=0.05, upper=500.0, step_increment=0.25, page_increment=1.0)
        )
        self._loss_spin.set_digits(2)
        self._loss_spin.set_size_request(72, -1)
        self._loss_spin.connect("value-changed", self._on_loss_changed)

        row1.append(goal_lbl); row1.append(self._goal_spin)
        row1.append(loss_lbl); row1.append(self._loss_spin)
        self.append(row1)

        row2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        lev_lbl = Gtk.Label(label="Leverage")
        lev_lbl.add_css_class("qts-label")
        self._lev_spin = Gtk.SpinButton()
        self._lev_spin.set_adjustment(
            Gtk.Adjustment(value=5, lower=1, upper=25, step_increment=1)
        )
        self._lev_spin.set_digits(0)
        self._lev_spin.set_size_request(55, -1)
        self._lev_spin.connect("value-changed", self._on_lev_changed)

        self._scan_btn = Gtk.Button(label="🔍 Scan")
        self._scan_btn.add_css_class("flat")
        self._scan_btn.connect("clicked", lambda _: self._controller.force_scan())

        row2.append(lev_lbl); row2.append(self._lev_spin); row2.append(self._scan_btn)
        self.append(row2)
        self.append(_sep())

        # ── Simulación ───────────────────────────────────────────────────
        self.append(_section("SIMULACIÓN (sin ejecutar)"))
        self._sim_sym_lbl = _ml()
        self._sim_qty_lbl = _ml()
        self._sim_rr_lbl  = _ml()
        self._sim_result  = _ml()
        self._sim_warn    = _ml()
        for w in [self._sim_sym_lbl, self._sim_qty_lbl, self._sim_rr_lbl,
                  self._sim_result, self._sim_warn]:
            self.append(w)
        self.append(_sep())

        # ── Propuesta ────────────────────────────────────────────────────
        self._prop_sec = _section("PROPUESTA")
        self.append(self._prop_sec)
        self._prop_header  = _ml()
        self._prop_levels  = _ml()
        self._prop_sizing  = _ml()
        self._prop_risk    = _ml()
        self._prop_reasons = _ml()
        self._prop_timer   = _ml()
        for w in [self._prop_header, self._prop_levels, self._prop_sizing,
                  self._prop_risk, self._prop_reasons, self._prop_timer]:
            self.append(w)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._confirm_btn = Gtk.Button(label="✓ CONFIRMAR")
        self._confirm_btn.add_css_class("suggested-action")
        self._confirm_btn.connect("clicked", lambda _: self._controller.approve_proposal())
        self._reject_btn = Gtk.Button(label="✗ RECHAZAR")
        self._reject_btn.add_css_class("destructive-action")
        self._reject_btn.connect("clicked", lambda _: self._controller.reject_proposal())
        btn_row.append(self._confirm_btn)
        btn_row.append(self._reject_btn)
        self._confirm_row = btn_row
        self.append(btn_row)
        self.append(_sep())

        # ── Trades activos (hasta MAX_POSITIONS filas) ────────────────────
        self.append(_section(f"TRADES ACTIVOS (0/{MAX_POSITIONS})"))
        self._active_section_lbl = self.get_last_child()
        self._trade_rows: list[_TradeRow] = []
        for _ in range(MAX_POSITIONS):
            row = _TradeRow(self._controller)
            row.clear()
            self._trade_rows.append(row)
            self.append(row)
        self.append(_sep())

        # ── Log de trades ────────────────────────────────────────────────
        self.append(_section("HISTORIAL"))
        self._log_lbls = [_ml() for _ in range(self.N_LOG)]
        for lbl in self._log_lbls:
            self.append(lbl)
        self.append(_sep())

        # ── Journal stats ────────────────────────────────────────────────
        self.append(_section("JOURNAL"))
        self._jnl_line1 = _ml()
        self._jnl_line2 = _ml()
        self.append(self._jnl_line1)
        self.append(self._jnl_line2)
        self.append(_sep())

        # ── Status ───────────────────────────────────────────────────────
        self._status_lbl = _ml()
        self.append(self._status_lbl)

        self._render_controller_state(self._controller.state)

    # ── Callbacks ────────────────────────────────────────────────────────────

    def _on_mode_toggled(self, btn: Gtk.ToggleButton, mode: AutoMode) -> None:
        if btn.get_active():
            self._controller.set_mode(mode)

    def _on_goal_changed(self, spin: Gtk.SpinButton) -> None:
        self._controller.set_goal(spin.get_value())

    def _on_loss_changed(self, spin: Gtk.SpinButton) -> None:
        self._controller.set_max_loss(spin.get_value())

    def _on_lev_changed(self, spin: Gtk.SpinButton) -> None:
        self._controller.set_leverage(int(spin.get_value()))

    def _on_controller_update(self, cs: ControllerState) -> None:
        GLib.idle_add(self._render_controller_state, cs)

    def _render_controller_state(self, cs: ControllerState) -> bool:
        self._render_mode(cs.mode)
        self._render_proposal(cs)
        self._render_log()
        msg = cs.status_msg
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

        descs = {
            AutoMode.MANUAL:     "Solo monitoreas. Tú ejecutas todo.",
            AutoMode.SUGGEST:    "El sistema propone. Tú confirmas con 1 clic.",
            AutoMode.AUTO_ENTRY: "El sistema entra solo. Tú gestionas el trade.",
            AutoMode.FULL_AUTO:  "Sistema autónomo: entra, trail y cierra solo.",
        }
        _, ckey = MODE_COLORS[mode]
        self._mode_desc.set_markup(
            f'<span color="{HEX[ckey]}" size="small">{descs[mode]}</span>'
        )

    def _render_proposal(self, cs: ControllerState) -> None:
        prop = cs.proposal
        is_suggest = cs.mode == AutoMode.SUGGEST

        if prop is None:
            self._prop_header.set_markup(f'<span color="{HEX["over"]}">Sin propuesta activa</span>')
            for w in [self._prop_levels, self._prop_sizing, self._prop_risk,
                      self._prop_reasons, self._prop_timer]:
                w.set_text("")
            self._confirm_row.set_visible(False)
            return

        col   = HEX["buy"] if prop.side == "Buy" else HEX["sell"]
        arrow = "▲" if prop.side == "Buy" else "▼"
        sym   = prop.symbol.replace("USDT", "")

        self._prop_header.set_markup(
            f'<span color="{col}" weight="bold">{arrow} {prop.direction}  {sym}</span>'
            f'  <span color="{HEX["sub"]}">Score {prop.opp_score}</span>'
        )
        self._prop_levels.set_markup(
            f'<span color="{HEX["sub"]}">Entry </span><span color="{HEX["text"]}">{_fp(prop.entry_price)}</span>'
            f'  <span color="{HEX["sub"]}">SL </span><span color="{HEX["sell"]}">{_fp(prop.sl_price)}</span>'
            f'  <span color="{HEX["sub"]}">TP </span><span color="{HEX["buy"]}">{_fp(prop.tp_price)}</span>'
        )
        self._prop_sizing.set_markup(
            f'<span color="{HEX["sub"]}">Qty </span><span color="{HEX["text"]}">{prop.qty}</span>'
            f'  <span color="{HEX["sub"]}">Notional </span><span color="{HEX["text"]}">${prop.notional:.1f}</span>'
            f'  <span color="{HEX["sub"]}">R:R </span><span color="{HEX["buy"]}">{prop.rr_ratio:.1f}:1</span>'
        )
        goal_real = prop.qty * abs(prop.tp_price - prop.entry_price)
        self._prop_risk.set_markup(
            f'<span color="{HEX["buy"]}">Goal +${goal_real:.2f}</span>'
            f'  <span color="{HEX["sell"]}">Riesgo -${prop.risk_usd:.2f}</span>'
            f'  <span color="{HEX["sub"]}">Margen ${prop.margin:.2f}</span>'
        )

        if prop.reasons:
            self._prop_reasons.set_markup(
                f'<span color="{HEX["over"]}" size="small">'
                + GLib.markup_escape_text(" · ".join(prop.reasons[:2]))
                + "</span>"
            )
        else:
            self._prop_reasons.set_text("")

        age = cs.proposal_age_s
        ttl = 60 - age
        self._prop_timer.set_markup(
            f'<span color="{HEX["warn"] if ttl < 20 else HEX["over"]}" size="small">'
            f'Expira en {ttl}s</span>'
        )
        self._confirm_row.set_visible(is_suggest)

    def _render_log(self) -> None:
        log_entries = self._controller.trade_log
        for i, lbl in enumerate(self._log_lbls):
            if i < len(log_entries):
                t     = log_entries[i]
                req   = t.request
                side  = req.side if req else "Buy"
                arrow = "▲" if side == "Buy" else "▼"
                sym   = t.symbol.replace("USDT", "")
                if t.state == TradeState.FAILED:
                    col    = HEX["sell"]
                    amount = "FALLÓ"
                else:
                    sign   = "+" if t.pnl_usd >= 0 else ""
                    col    = HEX["buy"] if t.pnl_usd >= 0 else HEX["sell"]
                    amount = f"{sign}${t.pnl_usd:.2f}"
                reason = t.close_reason[:18] if t.close_reason else t.state.value
                lbl.set_markup(
                    f'<span color="{col}">{arrow} {sym}  {amount}</span>'
                    f'  <span color="{HEX["over"]}" size="small">[{reason}]</span>'
                )
            else:
                lbl.set_text("")

        # Journal stats — refrescar máx 1 vez cada 10s o cuando cambia el log
        now = time.monotonic()
        if len(log_entries) != self._jnl_log_len or (now - self._jnl_last_refresh) > 10:
            self._jnl_log_len = len(log_entries)
            self._jnl_last_refresh = now
            self._render_journal()

    def _render_journal(self) -> None:
        stats = get_journal_stats()
        total = stats["total"]
        if total == 0:
            self._jnl_line1.set_markup(f'<span color="{HEX["over"]}">Sin trades en el journal</span>')
            self._jnl_line2.set_text("")
            return

        wr_col = HEX["buy"] if stats["win_rate"] >= 50 else HEX["sell"]
        pnl_col = HEX["buy"] if stats["total_pnl"] >= 0 else HEX["sell"]
        sign = "+" if stats["total_pnl"] >= 0 else ""
        self._jnl_line1.set_markup(
            f'<span color="{HEX["sub"]}">W:</span>'
            f'<span color="{wr_col}">{stats["wins"]}/{total} ({stats["win_rate"]}%)</span>'
            f'  <span color="{HEX["sub"]}">PnL:</span>'
            f'<span color="{pnl_col}">{sign}${stats["total_pnl"]}</span>'
        )
        self._jnl_line2.set_markup(
            f'<span color="{HEX["sub"]}">Best: </span>'
            f'<span color="{HEX["teal"]}">{stats["best_symbol"]}</span>'
            f'  <span color="{HEX["sub"]}">AvgScore: </span>'
            f'<span color="{HEX["text"]}">{stats["avg_score"]}</span>'
            f'  <span color="{HEX["sub"]}">R:R: </span>'
            f'<span color="{HEX["text"]}">{stats["avg_rr"]}</span>'
        )

    def _render_simulation(self, sim: Optional[dict]) -> None:
        if not sim or "error" in sim:
            msg = sim.get("error", "esperando datos…") if sim else "esperando datos…"
            self._sim_sym_lbl.set_markup(f'<span color="{HEX["over"]}">{msg}</span>')
            for w in [self._sim_qty_lbl, self._sim_rr_lbl, self._sim_result, self._sim_warn]:
                w.set_text("")
            return

        goal        = sim["goal_requested"]
        real_profit = sim["real_profit"]
        real_loss   = sim["real_loss"]
        is_capped   = sim["is_capped"]
        binding     = sim["binding_limit"]
        sym         = sim.get("symbol", "??").replace("USDT", "")

        self._sim_sym_lbl.set_markup(
            f'<span color="{HEX["blue"]}">{sym}</span>'
            f'  <span color="{HEX["sub"]}">Entry≈</span>'
            f'<span color="{HEX["text"]}">{_fp(sim["entry"])}</span>'
        )
        self._sim_qty_lbl.set_markup(
            f'<span color="{HEX["sub"]}">Qty </span><span color="{HEX["text"]}">{sim["qty"]}</span>'
            f'  <span color="{HEX["sub"]}">Notional </span><span color="{HEX["text"]}">${sim["notional"]}</span>'
            f'  <span color="{HEX["sub"]}">Margen </span><span color="{HEX["text"]}">${sim["margin"]}</span>'
        )
        self._sim_rr_lbl.set_markup(
            f'<span color="{HEX["sell"]}">SL {_fp(sim["sl"])}</span>'
            f'  <span color="{HEX["buy"]}">TP {_fp(sim["tp"])}</span>'
            f'  <span color="{HEX["sub"]}">R:R </span>'
            f'<span color="{HEX["buy"] if sim["rr"] >= 2 else HEX["warn"]}">{sim["rr"]:.1f}:1</span>'
        )
        self._sim_result.set_markup(
            f'<span color="{HEX["buy"]}" weight="bold">✓ TP: +${real_profit:.2f}</span>'
            f'  <span color="{HEX["sell"]}" weight="bold">✗ SL: -${real_loss:.2f}</span>'
        )

        warns = []
        if is_capped and real_profit < goal * 0.8:
            warns.append(f"⚠ Solo puedes ganar ${real_profit:.2f} de ${goal:.2f}")
            warns.append(f"  Límite: {binding}")
            gap = sim.get("equity_gap", 0)
            if gap > 0:
                warns.append(f"  Necesitas ${gap:.2f} más de equity")
        if warns:
            self._sim_warn.set_markup(
                f'<span color="{HEX["warn"]}" size="small">'
                + GLib.markup_escape_text("\n".join(warns)) + "</span>"
            )
        elif real_profit >= goal * 0.95:
            self._sim_warn.set_markup(
                f'<span color="{HEX["buy"]}" size="small">✓ Meta alcanzable</span>'
            )
        else:
            self._sim_warn.set_text("")

    # ── Update (100ms desde _refresh) ────────────────────────────────────────

    def update(
        self,
        account: "AccountState",
        risk:    "RiskStatus",
        sim:     Optional[dict] = None,
    ) -> None:
        self._render_simulation(sim)
        self._render_active_trades(account)

    def _render_active_trades(self, account: "AccountState") -> None:
        actives = list(self._controller._active.values())
        n = len(actives)

        # Actualizar título de la sección
        self._active_section_lbl.set_text(f"TRADES ACTIVOS ({n}/{MAX_POSITIONS})")

        for i, row in enumerate(self._trade_rows):
            if i < len(actives):
                trade = actives[i]
                sym   = trade.symbol
                pos   = account.positions.get(sym)
                mark  = pos.mark_price if pos and pos.mark_price > 0 else (pos.entry_price if pos else 0.0)
                upnl  = pos.unrealized_pnl if pos else 0.0
                row.show_trade(trade, mark, upnl)
            else:
                row.clear()
