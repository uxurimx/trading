"""
interface/settings_view.py
───────────────────────────
Pestaña de configuración del sistema.
Todos los cambios toman efecto de inmediato (sin reiniciar).
"""
from __future__ import annotations

from typing import Callable, Optional

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk, Pango

from core.config import settings, SPEED_CONFIGS


# ─── Helpers de layout ────────────────────────────────────────────────────────

def _section(text: str) -> Gtk.Label:
    lbl = Gtk.Label(label=text)
    lbl.set_xalign(0)
    lbl.set_margin_top(12)
    lbl.set_margin_bottom(4)
    attrs = Pango.AttrList()
    attrs.insert(Pango.attr_weight_new(Pango.Weight.BOLD))
    lbl.set_attributes(attrs)
    return lbl


def _row(label: str, widget: Gtk.Widget, hint: str = "") -> Gtk.Box:
    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    row.set_margin_start(8)
    row.set_margin_end(8)
    row.set_margin_top(2)
    row.set_margin_bottom(2)

    lbl = Gtk.Label(label=label)
    lbl.set_xalign(0)
    lbl.set_size_request(220, -1)
    lbl.set_wrap(True)
    row.append(lbl)
    row.append(widget)

    if hint:
        hint_lbl = Gtk.Label(label=hint)
        hint_lbl.set_xalign(0)
        hint_lbl.set_margin_start(6)
        attrs = Pango.AttrList()
        attrs.insert(Pango.attr_scale_new(0.85))
        attrs.insert(Pango.attr_foreground_new(0x6666, 0x6666, 0x6666))
        hint_lbl.set_attributes(attrs)
        row.append(hint_lbl)

    return row


def _spin(lo: float, hi: float, val: float, step: float, digits: int,
          w: int = 90) -> Gtk.SpinButton:
    sp = Gtk.SpinButton()
    sp.set_adjustment(Gtk.Adjustment(
        value=val, lower=lo, upper=hi,
        step_increment=step, page_increment=step * 5,
    ))
    sp.set_digits(digits)
    sp.set_size_request(w, -1)
    return sp


def _sep() -> Gtk.Separator:
    s = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
    s.set_margin_top(4)
    s.set_margin_bottom(4)
    return s


# ─── SettingsView ─────────────────────────────────────────────────────────────

class SettingsView(Gtk.ScrolledWindow):

    def __init__(
        self,
        paper_wallet=None,
        on_paper_toggle: Optional[Callable[[bool], None]] = None,
    ) -> None:
        super().__init__()
        self._paper_wallet    = paper_wallet
        self._on_paper_toggle = on_paper_toggle
        self.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.set_propagate_natural_height(False)
        self.set_vexpand(True)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.set_margin_start(16)
        box.set_margin_end(16)
        box.set_margin_top(8)
        box.set_margin_bottom(16)
        box.set_halign(Gtk.Align.CENTER)
        box.set_size_request(560, -1)

        self._build(box)
        self.set_child(box)

    def _build(self, box: Gtk.Box) -> None:

        # ── Paper Trading ────────────────────────────────────────────────────
        box.append(_section("PAPER TRADING"))

        # Toggle principal
        pt_sw = Gtk.Switch()
        pt_sw.set_active(settings.paper_trading)
        pt_sw.set_valign(Gtk.Align.CENTER)
        pt_sw.connect("notify::active", self._on_pt_toggle)

        pt_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        pt_row.set_margin_start(8); pt_row.set_margin_end(8)
        pt_row.set_margin_top(4);   pt_row.set_margin_bottom(4)
        pt_lbl = Gtk.Label(label="Modo Paper Trading")
        pt_lbl.set_xalign(0)
        pt_lbl.set_size_request(220, -1)
        attrs = Pango.AttrList()
        attrs.insert(Pango.attr_weight_new(Pango.Weight.SEMIBOLD))
        pt_lbl.set_attributes(attrs)
        self._pt_hint = Gtk.Label()
        self._pt_hint.set_xalign(0)
        self._pt_hint.set_margin_start(8)
        self._update_pt_hint(settings.paper_trading)
        pt_row.append(pt_lbl)
        pt_row.append(pt_sw)
        pt_row.append(self._pt_hint)
        box.append(pt_row)

        # Balance inicial
        self._pt_balance_sp = _spin(100.0, 1_000_000.0, settings.paper_balance, 500.0, 0, w=110)
        self._pt_balance_sp.connect(
            "value-changed",
            lambda sp: setattr(settings, "paper_balance", sp.get_value()),
        )
        box.append(_row("Balance inicial (USDT)", self._pt_balance_sp,
                        "Se aplica al resetear el wallet"))

        # Fila de estadísticas + botón reset
        stats_reset_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        stats_reset_row.set_margin_start(8); stats_reset_row.set_margin_end(8)
        stats_reset_row.set_margin_top(2);   stats_reset_row.set_margin_bottom(4)

        self._pt_stats_lbl = Gtk.Label()
        self._pt_stats_lbl.set_xalign(0)
        self._pt_stats_lbl.set_hexpand(True)
        self._update_pt_stats()

        reset_btn = Gtk.Button(label="↺ Reiniciar wallet")
        reset_btn.add_css_class("destructive-action")
        reset_btn.connect("clicked", self._on_pt_reset)

        stats_reset_row.append(self._pt_stats_lbl)
        stats_reset_row.append(reset_btn)
        box.append(stats_reset_row)

        box.append(_sep())

        # ── Protección de riesgo ─────────────────────────────────────────────
        box.append(_section("PROTECCIÓN DE RIESGO"))

        # Circuit Breaker toggle — lo más importante
        cb_sw = Gtk.Switch()
        cb_sw.set_active(settings.circuit_breaker_enabled)
        cb_sw.set_valign(Gtk.Align.CENTER)
        cb_sw.connect("notify::active", self._on_circuit_breaker)
        self._cb_hint = Gtk.Label()
        self._cb_hint.set_xalign(0)
        self._cb_hint.set_margin_start(8)
        self._update_cb_hint(settings.circuit_breaker_enabled)

        cb_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        cb_row.set_margin_start(8); cb_row.set_margin_end(8)
        cb_row.set_margin_top(4);   cb_row.set_margin_bottom(4)
        cb_lbl = Gtk.Label(label="Circuit Breaker")
        cb_lbl.set_xalign(0)
        cb_lbl.set_size_request(220, -1)
        attrs = Pango.AttrList()
        attrs.insert(Pango.attr_weight_new(Pango.Weight.SEMIBOLD))
        cb_lbl.set_attributes(attrs)
        cb_row.append(cb_lbl)
        cb_row.append(cb_sw)
        cb_row.append(self._cb_hint)
        box.append(cb_row)

        # Pérdida máxima diaria
        self._loss_sp = _spin(0.1, 20.0, settings.max_daily_loss_pct, 0.5, 1)
        self._loss_sp.connect("value-changed", lambda sp: setattr(settings, "max_daily_loss_pct", sp.get_value()))
        box.append(_row("Pérdida máxima diaria (%)",
                        self._loss_sp,
                        "El CB se activa al alcanzar este % de pérdida"))

        # Máx trades por día
        self._trades_sp = _spin(1, 50, settings.max_trades_per_day, 1, 0)
        self._trades_sp.connect("value-changed", lambda sp: setattr(settings, "max_trades_per_day", int(sp.get_value())))
        box.append(_row("Máximo trades por día", self._trades_sp))

        box.append(_sep())

        # ── Gestión automática ───────────────────────────────────────────────
        box.append(_section("GESTIÓN AUTOMÁTICA (FULL AUTO)"))

        self._be_sp = _spin(10, 90, settings.breakeven_pct, 5, 0)
        self._be_sp.connect("value-changed", lambda sp: setattr(settings, "breakeven_pct", sp.get_value()))
        box.append(_row("Breakeven en (%)",
                        self._be_sp,
                        "% del recorrido al TP para mover SL a entrada"))

        self._pl_sp = _spin(20, 95, settings.profit_lock_pct, 5, 0)
        self._pl_sp.connect("value-changed", lambda sp: setattr(settings, "profit_lock_pct", sp.get_value()))
        box.append(_row("Profit lock en (%)",
                        self._pl_sp,
                        "% para asegurar ganancia parcial"))

        self._tr_sp = _spin(30, 95, settings.trailing_pct, 5, 0)
        self._tr_sp.connect("value-changed", lambda sp: setattr(settings, "trailing_pct", sp.get_value()))
        box.append(_row("Trailing stop en (%)", self._tr_sp))

        self._hold_sp = _spin(5, 120, settings.be_hold_time_s, 5, 0)
        self._hold_sp.connect("value-changed", lambda sp: setattr(settings, "be_hold_time_s", int(sp.get_value())))
        box.append(_row("Hold-time breakeven (s)",
                        self._hold_sp,
                        "El precio debe mantenerse N segundos antes de mover SL"))

        box.append(_sep())

        # ── Salidas de protección ─────────────────────────────────────────────
        box.append(_section("SALIDAS DE PROTECCIÓN"))

        # Weak Exit
        we_sw = Gtk.Switch()
        we_sw.set_active(settings.weak_exit_enabled)
        we_sw.set_valign(Gtk.Align.CENTER)
        we_sw.connect("notify::active",
                      lambda sw, _: setattr(settings, "weak_exit_enabled", sw.get_active()))
        box.append(_row("Weak Exit", we_sw,
                        "Cierra si el setup se debilita antes de generar ganancia"))

        self._we_win_sp = _spin(30, 600, settings.weak_exit_window_s, 10, 0)
        self._we_win_sp.connect("value-changed",
                                lambda sp: setattr(settings, "weak_exit_window_s", int(sp.get_value())))
        box.append(_row("  Ventana weak exit (s)", self._we_win_sp,
                        "Solo activo en los primeros N segundos del trade"))

        self._we_sc_sp = _spin(2, 6, settings.weak_exit_min_score, 1, 0)
        self._we_sc_sp.connect("value-changed",
                               lambda sp: setattr(settings, "weak_exit_min_score", int(sp.get_value())))
        box.append(_row("  Score mínimo debilidad", self._we_sc_sp,
                        "0-6 puntos — más bajo = más sensible"))

        # Time Stop
        ts_sw = Gtk.Switch()
        ts_sw.set_active(settings.time_stop_enabled)
        ts_sw.set_valign(Gtk.Align.CENTER)
        ts_sw.connect("notify::active",
                      lambda sw, _: setattr(settings, "time_stop_enabled", sw.get_active()))
        box.append(_row("Time Stop", ts_sw,
                        "Cierra si no hay progreso suficiente en N segundos"))

        self._ts_win_sp = _spin(30, 1800, settings.time_stop_window_s, 30, 0)
        self._ts_win_sp.connect("value-changed",
                                lambda sp: setattr(settings, "time_stop_window_s", int(sp.get_value())))
        box.append(_row("  Ventana time stop (s)", self._ts_win_sp,
                        "Scalp: 90s  ·  Fast: 300s  ·  Standard: 600s"))

        self._ts_pct_sp = _spin(5, 50, settings.time_stop_min_pct, 5, 0)
        self._ts_pct_sp.connect("value-changed",
                                lambda sp: setattr(settings, "time_stop_min_pct", sp.get_value()))
        box.append(_row("  Progreso mínimo (%)", self._ts_pct_sp,
                        "% del recorrido al TP requerido antes de cerrar"))

        # Partial Lock
        pl_sw = Gtk.Switch()
        pl_sw.set_active(settings.partial_lock_enabled)
        pl_sw.set_valign(Gtk.Align.CENTER)
        pl_sw.connect("notify::active",
                      lambda sw, _: setattr(settings, "partial_lock_enabled", sw.get_active()))
        box.append(_row("Partial Lock", pl_sw,
                        "Escalón entre BE y profit lock — asegura ganancia real"))

        self._pl_at_sp = _spin(30, 80, settings.partial_lock_at_pct, 5, 0)
        self._pl_at_sp.connect("value-changed",
                               lambda sp: setattr(settings, "partial_lock_at_pct", sp.get_value()))
        box.append(_row("  Activar en (%)", self._pl_at_sp,
                        "% de progreso al TP para mover SL a ganancia real"))

        self._pl_fr_sp = _spin(10, 90, int(settings.partial_lock_frac * 100), 5, 0)
        self._pl_fr_sp.connect("value-changed",
                               lambda sp: setattr(settings, "partial_lock_frac", sp.get_value() / 100))
        box.append(_row("  Fracción del riesgo (%)", self._pl_fr_sp,
                        "SL = entry ± sl_dist × frac  (40% = recuperas 40% de lo arriesgado)"))

        box.append(_sep())

        # ── Estrategia / Scan ────────────────────────────────────────────────
        box.append(_section("ESTRATEGIA"))

        # Selector de nivel de velocidad
        speed_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self._speed_btns: dict[str, Gtk.ToggleButton] = {}
        first_btn = None
        for key in ("nano", "scalp", "fast", "standard"):
            cfg  = SPEED_CONFIGS[key]
            btn  = Gtk.ToggleButton(label=f"{cfg['label']}\n{cfg['desc']}")
            btn.set_hexpand(True)
            btn.get_child().set_justify(Gtk.Justification.CENTER)
            if first_btn is None:
                first_btn = btn
            else:
                btn.set_group(first_btn)
            if settings.speed_level == key:
                btn.set_active(True)
            btn.connect("toggled", self._on_speed_toggled, key)
            speed_box.append(btn)
            self._speed_btns[key] = btn

        self._speed_hint = Gtk.Label()
        self._speed_hint.set_xalign(0)
        self._speed_hint.set_margin_start(8)
        self._speed_hint.set_margin_bottom(4)
        self._update_speed_hint()

        speed_lbl = Gtk.Label(label="Velocidad de trades")
        speed_lbl.set_xalign(0)
        speed_lbl.set_size_request(220, -1)
        speed_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        speed_row.set_margin_start(8)
        speed_row.set_margin_end(8)
        speed_row.set_margin_top(2)
        speed_row.set_margin_bottom(2)
        speed_row.append(speed_lbl)
        speed_row.append(speed_box)
        box.append(speed_row)
        box.append(self._speed_hint)

        self._score_sp = _spin(30, 95, settings.min_scan_score, 1, 0)
        self._score_sp.connect("value-changed", lambda sp: setattr(settings, "min_scan_score", int(sp.get_value())))
        box.append(_row("Score mínimo para propuesta",
                        self._score_sp,
                        "Más bajo = más señales, menos calidad"))

        self._scan_sp = _spin(10, 300, settings.scan_interval_s, 5, 0)
        self._scan_sp.connect("value-changed", lambda sp: setattr(settings, "scan_interval_s", int(sp.get_value())))
        box.append(_row("Intervalo entre scans (s)", self._scan_sp))

        box.append(_sep())

        # ── Símbolos y Filtros ────────────────────────────────────────────────
        box.append(_section("SÍMBOLOS Y FILTROS"))

        # Carga automática
        al_sw = Gtk.Switch()
        al_sw.set_active(settings.auto_load_symbols)
        al_sw.set_valign(Gtk.Align.CENTER)
        al_sw.connect("notify::active",
                      lambda sw, _: setattr(settings, "auto_load_symbols", sw.get_active()))
        box.append(_row("Carga dinámica de símbolos", al_sw,
                        "Descarga top-N pares por volumen desde Bybit al iniciar"))

        self._max_sym_sp = _spin(10, 500, settings.max_symbols, 10, 0)
        self._max_sym_sp.connect("value-changed",
                                 lambda sp: setattr(settings, "max_symbols", int(sp.get_value())))

        sym_count_lbl = Gtk.Label()
        sym_count_lbl.set_xalign(0)
        sym_count_lbl.set_markup(
            f'<span foreground="#9a9996" size="small">'
            f'Activos ahora: {len(settings.symbol_list)}</span>'
        )
        sym_row = _row("  Máx símbolos a monitorear", self._max_sym_sp)
        sym_row.append(sym_count_lbl)
        box.append(sym_row)

        # Filtro horario
        th_sw = Gtk.Switch()
        th_sw.set_active(settings.trading_hours_enabled)
        th_sw.set_valign(Gtk.Align.CENTER)
        th_sw.connect("notify::active",
                      lambda sw, _: setattr(settings, "trading_hours_enabled", sw.get_active()))
        box.append(_row("Filtro horario (UTC)", th_sw,
                        "Solo abre trades dentro del rango horario"))

        self._th_start_sp = _spin(0, 23, settings.trading_hours_start, 1, 0)
        self._th_start_sp.connect("value-changed",
                                  lambda sp: setattr(settings, "trading_hours_start", int(sp.get_value())))
        box.append(_row("  Inicio (hora UTC)", self._th_start_sp, "0-23"))

        self._th_end_sp = _spin(0, 23, settings.trading_hours_end, 1, 0)
        self._th_end_sp.connect("value-changed",
                                lambda sp: setattr(settings, "trading_hours_end", int(sp.get_value())))
        box.append(_row("  Fin (hora UTC)", self._th_end_sp, "0-23"))

        # Auto-blacklist
        ab_sw = Gtk.Switch()
        ab_sw.set_active(settings.auto_blacklist_enabled)
        ab_sw.set_valign(Gtk.Align.CENTER)
        ab_sw.connect("notify::active",
                      lambda sw, _: setattr(settings, "auto_blacklist_enabled", sw.get_active()))
        box.append(_row("Auto-blacklist", ab_sw,
                        "Excluye pares con N pérdidas seguidas"))

        self._ab_sp = _spin(1, 10, settings.auto_blacklist_losses, 1, 0)
        self._ab_sp.connect("value-changed",
                            lambda sp: setattr(settings, "auto_blacklist_losses", int(sp.get_value())))
        box.append(_row("  Pérdidas para excluir", self._ab_sp,
                        "Se reactiva automáticamente cuando gana"))

        # Blacklist manual
        box.append(_row("Blacklist manual", Gtk.Label(label=""), "Pares excluidos del scan"))

        self._bl_lbl = Gtk.Label()
        self._bl_lbl.set_xalign(0)
        self._bl_lbl.set_wrap(True)
        self._bl_lbl.set_margin_start(8)
        self._bl_lbl.set_margin_bottom(4)
        self._update_bl_label()
        box.append(self._bl_lbl)

        bl_ctrl = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        bl_ctrl.set_margin_start(8)
        bl_ctrl.set_margin_bottom(6)
        self._bl_entry = Gtk.Entry()
        self._bl_entry.set_placeholder_text("SIMBOLOUSDT")
        self._bl_entry.set_max_length(20)
        self._bl_entry.set_hexpand(True)
        bl_add_btn = Gtk.Button(label="Añadir")
        bl_add_btn.connect("clicked", self._on_bl_add)
        bl_rm_btn = Gtk.Button(label="Limpiar todo")
        bl_rm_btn.add_css_class("destructive-action")
        bl_rm_btn.connect("clicked", self._on_bl_clear)
        bl_ctrl.append(self._bl_entry)
        bl_ctrl.append(bl_add_btn)
        bl_ctrl.append(bl_rm_btn)
        box.append(bl_ctrl)

        box.append(_sep())

        # ── Agente IA (OpenAI) ───────────────────────────────────────────────
        box.append(_section("AGENTE IA · ESTRATEGIA EN TIEMPO REAL"))

        # Toggle principal
        ai_sw = Gtk.Switch()
        ai_sw.set_active(settings.ai_strategy_mode)
        ai_sw.set_valign(Gtk.Align.CENTER)
        ai_sw.connect("notify::active", self._on_ai_mode_toggle)
        self._ai_hint = Gtk.Label()
        self._ai_hint.set_xalign(0)
        self._ai_hint.set_margin_start(8)
        self._update_ai_hint(settings.ai_strategy_mode)

        ai_mode_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        ai_mode_row.set_margin_start(8); ai_mode_row.set_margin_end(8)
        ai_mode_row.set_margin_top(4);   ai_mode_row.set_margin_bottom(4)
        ai_lbl = Gtk.Label(label="Estrategia por Agente IA")
        ai_lbl.set_xalign(0)
        ai_lbl.set_size_request(220, -1)
        attrs2 = Pango.AttrList()
        attrs2.insert(Pango.attr_weight_new(Pango.Weight.SEMIBOLD))
        ai_lbl.set_attributes(attrs2)
        ai_mode_row.append(ai_lbl)
        ai_mode_row.append(ai_sw)
        ai_mode_row.append(self._ai_hint)
        box.append(ai_mode_row)

        # API Key de OpenAI
        ai_key_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        ai_key_row.set_margin_start(8); ai_key_row.set_margin_end(8)
        ai_key_row.set_margin_top(2);   ai_key_row.set_margin_bottom(2)
        ai_key_lbl = Gtk.Label(label="OpenAI API Key")
        ai_key_lbl.set_xalign(0)
        ai_key_lbl.set_size_request(220, -1)
        ai_key_row.append(ai_key_lbl)

        self._ai_key_entry = Gtk.Entry()
        self._ai_key_entry.set_visibility(False)          # ocultar como password
        self._ai_key_entry.set_placeholder_text("sk-…")
        self._ai_key_entry.set_hexpand(True)
        if settings.openai_api_key:
            self._ai_key_entry.set_text(settings.openai_api_key)
        self._ai_key_entry.connect("changed", self._on_ai_key_changed)
        ai_key_row.append(self._ai_key_entry)

        # Botón mostrar/ocultar
        self._ai_key_vis_btn = Gtk.Button(label="👁")
        self._ai_key_vis_btn.set_size_request(34, -1)
        self._ai_key_vis_btn.connect("clicked", self._on_ai_key_vis)
        ai_key_row.append(self._ai_key_vis_btn)
        box.append(ai_key_row)

        # Estado de la API Key
        self._ai_key_status = Gtk.Label()
        self._ai_key_status.set_xalign(0)
        self._ai_key_status.set_margin_start(12)
        self._ai_key_status.set_margin_bottom(4)
        self._update_ai_key_status()
        box.append(self._ai_key_status)

        # Modelo de OpenAI
        ai_model_combo = Gtk.ComboBoxText()
        for m in ("gpt-4o", "gpt-4o-mini", "o3-mini", "gpt-4-turbo"):
            ai_model_combo.append_text(m)
        models = ("gpt-4o", "gpt-4o-mini", "o3-mini", "gpt-4-turbo")
        cur_model = getattr(settings, "openai_model", "gpt-4o")
        ai_model_combo.set_active(models.index(cur_model) if cur_model in models else 0)
        ai_model_combo.connect("changed", self._on_ai_model_changed)
        box.append(_row("Modelo OpenAI", ai_model_combo, "Recomendado: gpt-4o"))

        # Descripción
        ai_desc = Gtk.Label()
        ai_desc.set_xalign(0)
        ai_desc.set_margin_start(8)
        ai_desc.set_margin_bottom(8)
        ai_desc.set_wrap(True)
        ai_desc.set_markup(
            '<span foreground="#9a9996" size="small">'
            'El agente analiza todos los mercados en tiempo real: tendencia, CVD, OI, '
            'absorción, soporte/resistencia y momentum. Razona antes de confirmar cada '
            'operación. El análisis se guarda en cada trade y se muestra en los detalles.'
            '</span>'
        )
        box.append(ai_desc)

        box.append(_sep())

        # ── Conexión (solo lectura) ──────────────────────────────────────────
        box.append(_section("CONEXIÓN"))

        testnet_lbl = Gtk.Label()
        testnet_lbl.set_xalign(0)
        if settings.bybit_testnet:
            testnet_lbl.set_markup('<span foreground="#f8e45c" weight="bold">⚠ TESTNET activo</span>')
        else:
            testnet_lbl.set_markup('<span foreground="#57e389">● Mainnet</span>')
        box.append(_row("Red", testnet_lbl))

        key = settings.bybit_api_key
        key_display = (key[:4] + "●●●●" + key[-4:]) if len(key) >= 10 else ("(no configurado)" if not key else "●●●●")
        key_lbl = Gtk.Label(label=key_display)
        key_lbl.set_xalign(0)
        box.append(_row("API Key", key_lbl))

        db_lbl = Gtk.Label(label=settings.db_path)
        db_lbl.set_xalign(0)
        db_lbl.set_ellipsize(Pango.EllipsizeMode.START)
        box.append(_row("Base de datos", db_lbl))

        box.append(_sep())

        # ── Nota al pie ──────────────────────────────────────────────────────
        note = Gtk.Label()
        note.set_xalign(0)
        note.set_margin_top(8)
        note.set_wrap(True)
        note.set_markup(
            '<span foreground="#9a9996" size="small">'
            'Los cambios toman efecto de inmediato. Para persistirlos entre sesiones '
            'edita el archivo <tt>.env</tt> en el directorio del proyecto.'
            '</span>'
        )
        box.append(note)

    # ── Speed level handlers ──────────────────────────────────────────────────

    def _on_speed_toggled(self, btn: Gtk.ToggleButton, key: str) -> None:
        if btn.get_active():
            settings.speed_level = key
            self._update_speed_hint()

    def _update_speed_hint(self) -> None:
        cfg = SPEED_CONFIGS.get(settings.speed_level, SPEED_CONFIGS["standard"])
        colors = {"nano": "#ff3c3c", "scalp": "#ff7b00", "fast": "#f8e45c", "standard": "#57e389"}
        color  = colors.get(settings.speed_level, "#9a9996")
        self._speed_hint.set_markup(
            f'<span foreground="{color}" size="small" weight="bold">{cfg["label"]}</span>'
            f'<span foreground="#9a9996" size="small"> — kline base: {cfg["tf_label"]}  '
            f'contexto: {cfg["slow"]}m  {cfg["desc"]}</span>'
        )

    # ─────────────────────────────────────────────────────────────────────────

    def _on_circuit_breaker(self, sw: Gtk.Switch, _param) -> None:
        active = sw.get_active()
        settings.circuit_breaker_enabled = active
        self._update_cb_hint(active)

    def _update_cb_hint(self, active: bool) -> None:
        if active:
            self._cb_hint.set_markup(
                '<span foreground="#57e389" size="small">Activo — bloquea nuevas entradas al alcanzar el límite de pérdida</span>'
            )
        else:
            self._cb_hint.set_markup(
                '<span foreground="#f8e45c" size="small">⚠ Desactivado — sin protección automática de pérdida diaria</span>'
            )

    # ── Paper Trading handlers ────────────────────────────────────────────────

    def _on_pt_toggle(self, sw: Gtk.Switch, _param) -> None:
        active = sw.get_active()
        self._update_pt_hint(active)
        if self._on_paper_toggle:
            self._on_paper_toggle(active)

    def _on_pt_reset(self, _btn) -> None:
        if self._paper_wallet:
            new_bal = settings.paper_balance
            self._paper_wallet.reset(new_bal)
            self._update_pt_stats()

    def _update_pt_hint(self, active: bool) -> None:
        if active:
            self._pt_hint.set_markup(
                '<span foreground="#f8e45c" weight="bold" size="small">'
                '⚠ PAPER TRADING ACTIVO — no se ejecutan órdenes reales'
                '</span>'
            )
        else:
            self._pt_hint.set_markup(
                '<span foreground="#9a9996" size="small">'
                'Desactivado — modo live'
                '</span>'
            )

    def _update_pt_stats(self) -> None:
        if not self._paper_wallet:
            self._pt_stats_lbl.set_text("")
            return
        pw = self._paper_wallet
        pnl = pw.total_pnl
        sign = "+" if pnl >= 0 else ""
        col  = "#57e389" if pnl >= 0 else "#ff7b63"
        self._pt_stats_lbl.set_markup(
            f'<span foreground="#9a9996" size="small">Balance: </span>'
            f'<span foreground="#ebebeb" size="small">'
            f'${pw.state.balance.total_equity:,.2f}</span>'
            f'  <span foreground="#9a9996" size="small">PnL total: </span>'
            f'<span foreground="{col}" size="small">{sign}${pnl:.2f}</span>'
            f'  <span foreground="#9a9996" size="small">Trades: </span>'
            f'<span foreground="#ebebeb" size="small">{pw._total}</span>'
            f'  <span foreground="#9a9996" size="small">Win%: </span>'
            f'<span foreground="#93ddc2" size="small">{pw.win_rate:.0f}%</span>'
        )

    def refresh_paper_stats(self) -> None:
        """Llamar periódicamente desde el tick para mantener las stats actualizadas."""
        self._update_pt_stats()

    # ── Blacklist handlers ────────────────────────────────────────────────────

    def _update_bl_label(self) -> None:
        bl = settings.blacklist_set
        if not bl:
            self._bl_lbl.set_markup(
                '<span foreground="#9a9996" size="small">Sin pares excluidos</span>'
            )
        else:
            items = ", ".join(sorted(bl))
            self._bl_lbl.set_markup(
                f'<span foreground="#ff7b63" size="small">'
                f'{GLib.markup_escape_text(items)}</span>'
            )

    def _on_bl_add(self, _btn) -> None:
        sym = self._bl_entry.get_text().strip().upper()
        if sym:
            bl = settings.blacklist_set
            bl.add(sym)
            settings.symbol_blacklist = ",".join(sorted(bl))
            self._bl_entry.set_text("")
            self._update_bl_label()

    def _on_bl_clear(self, _btn) -> None:
        settings.symbol_blacklist = ""
        self._update_bl_label()

    def refresh_blacklist(self) -> None:
        """Llamar periódicamente para reflejar cambios del auto-blacklist."""
        self._update_bl_label()

    # ── AI Strategy Agent handlers ────────────────────────────────────────────

    def _on_ai_mode_toggle(self, sw: Gtk.Switch, _param) -> None:
        active = sw.get_active()
        settings.ai_strategy_mode = active
        self._update_ai_hint(active)

    def _update_ai_hint(self, active: bool) -> None:
        if active:
            has_key = bool(getattr(settings, "openai_api_key", ""))
            if has_key:
                self._ai_hint.set_markup(
                    '<span foreground="#57e389" weight="bold">🤖 ACTIVO</span>'
                )
            else:
                self._ai_hint.set_markup(
                    '<span foreground="#f8e45c">⚠ Necesita API Key</span>'
                )
        else:
            self._ai_hint.set_markup(
                '<span foreground="#9a9996" size="small">Estrategia del sistema</span>'
            )

    def _on_ai_key_changed(self, entry: Gtk.Entry) -> None:
        key = entry.get_text().strip()
        settings.openai_api_key = key
        self._update_ai_key_status()
        self._update_ai_hint(settings.ai_strategy_mode)

    def _update_ai_key_status(self) -> None:
        key = getattr(settings, "openai_api_key", "")
        if not key:
            self._ai_key_status.set_markup(
                '<span foreground="#9a9996" size="small">Sin API key — '
                'configura en openai.com</span>'
            )
        elif key.startswith("sk-") and len(key) >= 20:
            masked = key[:7] + "●●●●●●●" + key[-4:]
            self._ai_key_status.set_markup(
                f'<span foreground="#57e389" size="small">● Key detectada: {masked}</span>'
            )
        else:
            self._ai_key_status.set_markup(
                '<span foreground="#f8e45c" size="small">⚠ Formato inusual — verifica que sea correcta</span>'
            )

    def _on_ai_key_vis(self, _btn) -> None:
        """Alternar visibilidad del campo de API key."""
        visible = self._ai_key_entry.get_visibility()
        self._ai_key_entry.set_visibility(not visible)
        self._ai_key_vis_btn.set_label("🙈" if not visible else "👁")

    def _on_ai_model_changed(self, combo: Gtk.ComboBoxText) -> None:
        model = combo.get_active_text()
        if model:
            settings.openai_model = model
