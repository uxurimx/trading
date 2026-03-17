"""
interface/settings_view.py
───────────────────────────
Pestaña de configuración del sistema.
Todos los cambios toman efecto de inmediato (sin reiniciar).
"""
from __future__ import annotations

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Pango

from core.config import settings


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

    def __init__(self) -> None:
        super().__init__()
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

        # ── Estrategia / Scan ────────────────────────────────────────────────
        box.append(_section("ESTRATEGIA"))

        self._score_sp = _spin(30, 95, settings.min_scan_score, 1, 0)
        self._score_sp.connect("value-changed", lambda sp: setattr(settings, "min_scan_score", int(sp.get_value())))
        box.append(_row("Score mínimo para propuesta",
                        self._score_sp,
                        "Más bajo = más señales, menos calidad"))

        self._scan_sp = _spin(10, 300, settings.scan_interval_s, 5, 0)
        self._scan_sp.connect("value-changed", lambda sp: setattr(settings, "scan_interval_s", int(sp.get_value())))
        box.append(_row("Intervalo entre scans (s)", self._scan_sp))

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
