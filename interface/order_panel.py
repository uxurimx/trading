"""
interface/order_panel.py
─────────────────────────
OrderPanel — Centro de órdenes QTS.

Layout vertical:
  ┌─ MODO ──────────────────────────────────┐
  │  [MANUAL] [SUGGEST] [AUTO] [FULL AUTO]  │
  ├─ OBJETIVO ──────────────────────────────┤
  │  Goal: $ [1.00]  Lev: [5x]  [SCAN]     │
  ├─ PROPUESTA ─────────────────────────────┤
  │  ▲ LONG XRPUSDT   Score: 72            │
  │  Entry 1.5261  SL 1.5135  TP 1.5651   │
  │  Qty 13  Margen $1.90  R:R 2.1:1      │
  │  Riesgo: -$0.17                        │
  │  [✓ CONFIRMAR]      [✗ RECHAZAR]       │
  ├─ TRADE ACTIVO ──────────────────────────┤
  │  [████████░░░░] +$0.34 / $1.00         │
  │  Estado: OPEN → SL: 1.5135             │
  ├─ LOG ───────────────────────────────────┤
  │  ▲ XRPUSDT +$0.87 [TP hit]            │
  │  ▼ SOLUSDT -$0.15 [SL hit]            │
  └─────────────────────────────────────────┘
"""
from __future__ import annotations

import math
from typing import Optional, TYPE_CHECKING

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk, Pango

from core.order_model import AutoMode, TradeState, ControllerState, TradeRecord

if TYPE_CHECKING:
    from core.controller import TradeController
    from streams.account import AccountState
    from core.risk import RiskStatus


# ─── Colores (mismo estilo que gtk_app.py) ────────────────────────────────────

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
    AutoMode.MANUAL:     ("MANUAL",     "over"),
    AutoMode.SUGGEST:    ("SUGGEST",    "blue"),
    AutoMode.AUTO_ENTRY: ("AUTO",       "warn"),
    AutoMode.FULL_AUTO:  ("FULL AUTO",  "sell"),
}


def _ml(text: str = "", use_markup: bool = True) -> Gtk.Label:
    lbl = Gtk.Label(label=text)
    lbl.set_xalign(0)
    lbl.set_use_markup(use_markup)
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
    if p <= 0:  return "──"
    if p >= 1000: return f"{p:.2f}"
    if p >= 10:   return f"{p:.4f}"
    return f"{p:.5f}"


# ─── OrderPanel ───────────────────────────────────────────────────────────────

class OrderPanel(Gtk.Box):
    """
    Panel de control de órdenes. Se añade como cuarta columna en MainWindow.
    Ancho fijo 280px.
    """

    N_LOG = 5   # entradas del historial de trades

    def __init__(self, controller: "TradeController") -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add_css_class("qts-card")
        self.set_hexpand(False)
        self.set_size_request(280, -1)

        self._controller = controller
        controller.on_update(self._on_controller_update)

        self._build_ui()

    # ── Construcción ─────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # Título
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
        for mode, (label, _color) in MODE_COLORS.items():
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

        # Descripción del modo
        self._mode_desc = _ml()
        self.append(self._mode_desc)

        self.append(_sep())

        # ── Goal + Max Loss + Leverage ───────────────────────────────────────
        self.append(_section("OBJETIVO"))

        # Fila 1: Ganar / Perder máximo
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

        row1.append(goal_lbl)
        row1.append(self._goal_spin)
        row1.append(loss_lbl)
        row1.append(self._loss_spin)
        self.append(row1)

        # Fila 2: Leverage + Scan
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
        self._scan_btn.connect("clicked", self._on_scan_clicked)

        row2.append(lev_lbl)
        row2.append(self._lev_spin)
        row2.append(self._scan_btn)
        self.append(row2)

        # ── Simulación en tiempo real ────────────────────────────────────────
        self.append(_sep())
        self.append(_section("SIMULACIÓN (sin ejecutar)"))
        self._sim_sym_lbl  = _ml()   # símbolo + dirección si hay señal
        self._sim_qty_lbl  = _ml()   # qty · notional · margen
        self._sim_rr_lbl   = _ml()   # SL / TP / R:R
        self._sim_result   = _ml()   # ✓ Si TP: +$X  /  ✗ Si SL: -$Y
        self._sim_warn     = _ml()   # ⚠ warnings
        for w in [self._sim_sym_lbl, self._sim_qty_lbl, self._sim_rr_lbl,
                  self._sim_result, self._sim_warn]:
            self.append(w)

        self.append(_sep())

        # ── Propuesta ────────────────────────────────────────────────────────
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
        self._reject_btn  = Gtk.Button(label="✗ RECHAZAR")
        self._reject_btn.add_css_class("destructive-action")
        self._reject_btn.connect("clicked", lambda _: self._controller.reject_proposal())
        btn_row.append(self._confirm_btn)
        btn_row.append(self._reject_btn)
        self._confirm_row = btn_row
        self.append(btn_row)

        # Botón cerrar posición (visible cuando hay trade activo)
        self._close_btn = Gtk.Button(label="✗ CERRAR POSICIÓN")
        self._close_btn.add_css_class("destructive-action")
        self._close_btn.connect("clicked", lambda _: self._controller.close_now())
        self.append(self._close_btn)

        self.append(_sep())

        # ── Trade activo ─────────────────────────────────────────────────────
        self.append(_section("TRADE ACTIVO"))

        self._trade_state  = _ml()
        self._trade_pnl    = _ml()
        self._trade_sl     = _ml()
        self._trade_prog   = Gtk.ProgressBar()
        self._trade_prog.set_show_text(True)
        for w in [self._trade_state, self._trade_prog, self._trade_pnl, self._trade_sl]:
            self.append(w)

        self.append(_sep())

        # ── Log de trades ────────────────────────────────────────────────────
        self.append(_section("HISTORIAL"))
        self._log_lbls = [_ml() for _ in range(self.N_LOG)]
        for lbl in self._log_lbls:
            self.append(lbl)

        # ── Status ───────────────────────────────────────────────────────────
        self.append(_sep())
        self._status_lbl = _ml()
        self.append(self._status_lbl)

        # Render inicial
        self._render_controller_state(self._controller.state)

    # ── Callbacks de controles ────────────────────────────────────────────────

    def _on_mode_toggled(self, btn: Gtk.ToggleButton, mode: AutoMode) -> None:
        if btn.get_active():
            self._controller.set_mode(mode)

    def _on_goal_changed(self, spin: Gtk.SpinButton) -> None:
        self._controller.set_goal(spin.get_value())

    def _on_loss_changed(self, spin: Gtk.SpinButton) -> None:
        self._controller.set_max_loss(spin.get_value())

    def _on_lev_changed(self, spin: Gtk.SpinButton) -> None:
        self._controller.set_leverage(int(spin.get_value()))

    def _on_scan_clicked(self, _btn) -> None:
        self._controller.force_scan()

    # ── Callback del controlador (main thread vía on_update) ─────────────────

    def _on_controller_update(self, cs: ControllerState) -> None:
        # Esto se llama desde el thread del controlador —
        # necesita correr en el main thread GTK
        GLib.idle_add(self._render_controller_state, cs)

    def _render_controller_state(self, cs: ControllerState) -> bool:
        self._render_mode(cs.mode)
        self._render_proposal(cs)
        self._render_active_trade(cs)
        self._render_log()
        msg = cs.status_msg
        msg_col = HEX["sell"] if msg.startswith("✗") else HEX["sub"]
        self._status_lbl.set_markup(
            f'<span color="{msg_col}" size="small">{GLib.markup_escape_text(msg)}</span>'
        )
        return False   # para GLib.idle_add

    # ── Renderizado ───────────────────────────────────────────────────────────

    def _render_mode(self, mode: AutoMode) -> None:
        # Asegurarse que el botón correcto está activo
        for m, btn in self._mode_btns.items():
            if btn.get_active() != (m == mode):
                btn.handler_block_by_func(self._on_mode_toggled)
                btn.set_active(m == mode)
                btn.handler_unblock_by_func(self._on_mode_toggled)

        # Descripción
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
            self._close_btn.set_visible(False)
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
            reasons_text = " · ".join(prop.reasons[:2])
            self._prop_reasons.set_markup(
                f'<span color="{HEX["over"]}" size="small">{GLib.markup_escape_text(reasons_text)}</span>'
            )
        else:
            self._prop_reasons.set_text("")

        age = cs.proposal_age_s
        ttl = 60 - age
        self._prop_timer.set_markup(
            f'<span color="{HEX["warn"] if ttl < 20 else HEX["over"]}" size="small">'
            f'Expira en {ttl}s</span>'
        )

        # Botones: CONFIRMAR/RECHAZAR solo en SUGGEST mode
        self._confirm_row.set_visible(is_suggest)
        self._close_btn.set_visible(False)

    def _render_active_trade(self, cs: ControllerState) -> None:
        trade = cs.active_trade

        # Mostrar botón cerrar si hay trade activo (cualquier modo)
        has_active = trade is not None and trade.is_active
        self._close_btn.set_visible(has_active)

        if not trade or not trade.is_active:
            self._trade_state.set_markup(f'<span color="{HEX["over"]}">Sin trade activo</span>')
            self._trade_pnl.set_text("")
            self._trade_sl.set_text("")
            self._trade_prog.set_fraction(0)
            self._trade_prog.set_text("")
            return

        req = trade.request
        if not req:
            return

        # Estado
        state_labels = {
            TradeState.SUBMITTED: ("ESPERANDO FILL", "warn"),
            TradeState.OPEN:      ("OPEN",       "text"),
            TradeState.BREAKEVEN: ("BREAKEVEN ✓", "buy"),
            TradeState.TRAILING:  ("TRAILING ↑",  "teal"),
        }
        s_label, s_color = state_labels.get(trade.state, ("??", "over"))
        col = HEX["buy"] if req.side == "Buy" else HEX["sell"]
        arrow = "▲" if req.side == "Buy" else "▼"
        sym = req.symbol.replace("USDT", "")
        self._trade_state.set_markup(
            f'<span color="{col}" weight="bold">{arrow} {sym}</span>'
            f'  <span color="{HEX[s_color]}" weight="bold">{s_label}</span>'
        )

        # SL actual
        self._trade_sl.set_markup(
            f'<span color="{HEX["sub"]}">SL </span>'
            f'<span color="{HEX["sell"]}">{_fp(trade.current_sl)}</span>'
            f'  <span color="{HEX["sub"]}">TP </span>'
            f'<span color="{HEX["buy"]}">{_fp(trade.current_tp)}</span>'
        )

        # Barra de progreso (requiere mark_price — aproximado con entry hasta que llegue del WS)
        entry = trade.entry_price or req.entry_price
        tp    = trade.current_tp
        sl    = trade.current_sl
        tp_dist = abs(tp - entry) if tp > 0 else 1
        # Aproximar PnL con entry (se actualiza cuando AccountState tiene el dato)
        self._trade_prog.set_fraction(0.0)
        self._trade_prog.set_text(f"entry {_fp(entry)}")
        self._trade_pnl.set_markup(
            f'<span color="{HEX["sub"]}">Entry </span>'
            f'<span color="{HEX["text"]}">{_fp(entry)}</span>'
        )

    def _render_log(self) -> None:
        log_entries = self._controller.trade_log
        for i, lbl in enumerate(self._log_lbls):
            if i < len(log_entries):
                t     = log_entries[i]
                req   = t.request
                side  = req.side if req else "Buy"
                arrow = "▲" if side == "Buy" else "▼"
                sym   = t.symbol.replace("USDT", "")
                from core.order_model import TradeState as _TS
                if t.state == _TS.FAILED:
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

    def _render_simulation(self, sim: dict) -> None:
        """Muestra la simulación en tiempo real (sin ejecutar)."""
        if not sim or "error" in sim:
            msg = sim.get("error", "esperando datos…") if sim else "esperando datos…"
            self._sim_sym_lbl.set_markup(f'<span color="{HEX["over"]}">{msg}</span>')
            for w in [self._sim_qty_lbl, self._sim_rr_lbl, self._sim_result, self._sim_warn]:
                w.set_text("")
            return

        goal  = sim["goal_requested"]
        loss  = sim["loss_requested"]
        real_profit = sim["real_profit"]
        real_loss   = sim["real_loss"]
        is_capped   = sim["is_capped"]
        binding     = sim["binding_limit"]
        sym         = sim.get("symbol", "??").replace("USDT", "")

        # Encabezado: símbolo + niveles
        self._sim_sym_lbl.set_markup(
            f'<span color="{HEX["blue"]}">{sym}</span>'
            f'  <span color="{HEX["sub"]}">Entry≈</span>'
            f'<span color="{HEX["text"]}">{_fp(sim["entry"])}</span>'
        )

        # Qty · notional · margen
        self._sim_qty_lbl.set_markup(
            f'<span color="{HEX["sub"]}">Qty </span><span color="{HEX["text"]}">{sim["qty"]}</span>'
            f'  <span color="{HEX["sub"]}">Notional </span><span color="{HEX["text"]}">${sim["notional"]}</span>'
            f'  <span color="{HEX["sub"]}">Margen </span><span color="{HEX["text"]}">${sim["margin"]}</span>'
        )

        # SL · TP · R:R
        self._sim_rr_lbl.set_markup(
            f'<span color="{HEX["sell"]}">SL {_fp(sim["sl"])}</span>'
            f'  <span color="{HEX["buy"]}">TP {_fp(sim["tp"])}</span>'
            f'  <span color="{HEX["sub"]}">R:R </span>'
            f'<span color="{HEX["buy"] if sim["rr"] >= 2 else HEX["warn"]}">{sim["rr"]:.1f}:1</span>'
        )

        # Resultado esperado
        profit_col = HEX["buy"]
        loss_col   = HEX["sell"]
        self._sim_result.set_markup(
            f'<span color="{profit_col}" weight="bold">✓ TP: +${real_profit:.2f}</span>'
            f'  <span color="{loss_col}" weight="bold">✗ SL: -${real_loss:.2f}</span>'
        )

        # Warnings
        warns = []
        if is_capped:
            gap = sim.get("equity_gap", 0)
            if real_profit < goal * 0.8:
                warns.append(
                    f"⚠ Solo puedes ganar ${real_profit:.2f} de ${goal:.2f} pedidos"
                )
                warns.append(f"  Límite: {binding}")
                if gap > 0:
                    warns.append(f"  Necesitas ${gap:.2f} más de equity para lograrlo")
        if real_loss > loss * 1.05 and loss > 0:
            warns.append(f"⚠ Pérdida real ${real_loss:.2f} > límite ${loss:.2f}")

        if warns:
            self._sim_warn.set_markup(
                f'<span color="{HEX["warn"]}" size="small">'
                + GLib.markup_escape_text("\n".join(warns))
                + "</span>"
            )
        else:
            if real_profit >= goal * 0.95:
                self._sim_warn.set_markup(
                    f'<span color="{HEX["buy"]}" size="small">✓ Meta alcanzable con este setup</span>'
                )
            else:
                self._sim_warn.set_text("")

    # ── Actualización de PnL en tiempo real (desde MainWindow._refresh) ───────

    def update(self, account: "AccountState", risk: "RiskStatus", sim: Optional[dict] = None) -> None:
        """
        Llamado cada 100ms desde MainWindow._refresh().
        Actualiza el PnL del trade activo y la simulación en tiempo real.
        """
        self._render_simulation(sim)
        trade = self._controller._active
        if not trade or not trade.is_active or not trade.request:
            return

        sym = trade.symbol
        pos = account.positions.get(sym)
        if not pos:
            return

        mark    = pos.mark_price if pos.mark_price > 0 else pos.entry_price
        entry   = trade.entry_price or pos.entry_price
        tp      = trade.current_tp
        sl      = trade.current_sl
        upnl    = pos.unrealized_pnl
        pnl_pct = pos.pnl_pct

        pnl_col = HEX["buy"] if upnl >= 0 else HEX["sell"]
        sign    = "+" if upnl >= 0 else ""

        # PnL en tiempo real
        self._trade_pnl.set_markup(
            f'<span color="{pnl_col}" weight="bold">{sign}${upnl:.2f}</span>'
            f'  <span color="{HEX["sub"]}">({sign}{pnl_pct:.1f}%)</span>'
            f'  <span color="{HEX["sub"]}">mark {_fp(mark)}</span>'
        )

        # Barra de progreso
        if tp > 0 and entry > 0:
            tp_dist = abs(tp - entry)
            if tp_dist > 0:
                is_long = trade.request.side == "Buy"
                prog = (mark - entry) / tp_dist if is_long else (entry - mark) / tp_dist
                frac = max(0.0, min(1.0, prog))
                goal_real = trade.request.qty * tp_dist if trade.request else 0
                self._trade_prog.set_fraction(frac)
                self._trade_prog.set_text(
                    f"{sign}${upnl:.2f} / ${goal_real:.2f}  ({frac*100:.0f}%)"
                )

        # Actualizar el SL actual (puede haber sido movido por breakeven/trail)
        self._trade_sl.set_markup(
            f'<span color="{HEX["sub"]}">SL </span>'
            f'<span color="{HEX["sell"]}">{_fp(trade.current_sl)}</span>'
            f'  <span color="{HEX["sub"]}">TP </span>'
            f'<span color="{HEX["buy"]}">{_fp(trade.current_tp)}</span>'
        )
