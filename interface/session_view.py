"""
interface/session_view.py
───────────────────────────
SessionView — Gestión de Sesiones / Objetivos TSAA.

Funcionalidades:
  · Lista todas las sesiones con progreso hacia su objetivo
  · "Reanudar" en sesiones ACTIVE (permite recuperar tras reinicio)
  · "Nueva Sesión" con diálogo: nombre, objetivo, drawdown, duración
  · Sesiones ACTIVE resaltadas al inicio de la lista
"""
from __future__ import annotations

import datetime
import os
import time
from typing import Callable, List, Optional

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk, Pango

from core.db import get_all_sessions, close_all_sessions


HEX = {
    "buy":    "#57e389",
    "sell":   "#ff7b63",
    "blue":   "#78aeed",
    "warn":   "#f8e45c",
    "teal":   "#93ddc2",
    "text":   "#ebebeb",
    "sub":    "#9a9996",
    "over":   "#5e5c64",
    "purple": "#c061cb",
    "green":  "#57e389",
}


def _ml(text: str = "") -> Gtk.Label:
    lbl = Gtk.Label(label=text)
    lbl.set_xalign(0)
    lbl.set_use_markup(True)
    lbl.add_css_class("qts-mono-sm")
    return lbl


def _sep() -> Gtk.Separator:
    s = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
    s.add_css_class("qts-sep")
    return s


# ─── Diálogo de Nueva Sesión ──────────────────────────────────────────────────

class _NewSessionDialog(Gtk.Dialog):
    """
    Diálogo para crear una nueva sesión con objetivos custom.
    Campos: nombre, objetivo ($), drawdown límite ($), duración (h).
    """

    def __init__(self, parent: Gtk.Window) -> None:
        super().__init__(title="Nueva Sesión", transient_for=parent, modal=True)
        self.set_default_size(380, -1)

        self.add_button("Cancelar", Gtk.ResponseType.CANCEL)
        ok_btn = self.add_button("Crear Sesión", Gtk.ResponseType.OK)
        ok_btn.add_css_class("suggested-action")

        grid = Gtk.Grid(column_spacing=12, row_spacing=10)
        grid.set_margin_top(16)
        grid.set_margin_bottom(8)
        grid.set_margin_start(16)
        grid.set_margin_end(16)

        def _lbl(t):
            l = Gtk.Label(label=t)
            l.set_xalign(1.0)
            return l

        # Nombre
        grid.attach(_lbl("Nombre / Objetivo:"), 0, 0, 1, 1)
        self._name_entry = Gtk.Entry()
        self._name_entry.set_placeholder_text("ej: Para la renta")
        self._name_entry.set_hexpand(True)
        grid.attach(self._name_entry, 1, 0, 1, 1)

        # Target PnL
        grid.attach(_lbl("Objetivo PnL ($):"), 0, 1, 1, 1)
        self._target_spin = Gtk.SpinButton()
        self._target_spin.set_adjustment(Gtk.Adjustment(value=50, lower=1, upper=100000, step_increment=10, page_increment=100))
        self._target_spin.set_digits(2)
        grid.attach(self._target_spin, 1, 1, 1, 1)

        # Max Drawdown
        grid.attach(_lbl("Drawdown máx ($):"), 0, 2, 1, 1)
        self._drawdown_spin = Gtk.SpinButton()
        self._drawdown_spin.set_adjustment(Gtk.Adjustment(value=20, lower=1, upper=10000, step_increment=5, page_increment=50))
        self._drawdown_spin.set_digits(2)
        grid.attach(self._drawdown_spin, 1, 2, 1, 1)

        # Duración
        grid.attach(_lbl("Duración (h):"), 0, 3, 1, 1)
        self._duration_spin = Gtk.SpinButton()
        self._duration_spin.set_adjustment(Gtk.Adjustment(value=12, lower=0.5, upper=72, step_increment=0.5, page_increment=4))
        self._duration_spin.set_digits(1)
        grid.attach(self._duration_spin, 1, 3, 1, 1)

        self.get_content_area().append(grid)
        self.show()

    def get_values(self) -> dict:
        return {
            "name":         self._name_entry.get_text().strip() or "Sesión",
            "target_pnl":   self._target_spin.get_value(),
            "max_drawdown": -abs(self._drawdown_spin.get_value()),  # siempre negativo
            "duration_h":   self._duration_spin.get_value(),
        }


# ─── Tarjeta de sesión ────────────────────────────────────────────────────────

class _SessionCard(Gtk.Box):
    """Fila que representa una sesión individual."""

    def __init__(
        self,
        on_resume: Callable[[dict], None],
        on_audit:  Callable[[str],  None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.set_margin_top(6)
        self.set_margin_bottom(6)

        self._on_resume_cb = on_resume
        self._on_audit_cb  = on_audit
        self._session_data: dict = {}

        # Fila principal: info + botones
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)

        self._lbl_info = _ml()
        self._lbl_info.set_hexpand(True)
        self._lbl_info.set_wrap(True)
        row.append(self._lbl_info)

        # Barra de progreso (compacta, visible solo cuando hay objetivo)
        self._progress_bar = Gtk.ProgressBar()
        self._progress_bar.set_valign(Gtk.Align.CENTER)
        self._progress_bar.set_size_request(80, -1)
        row.append(self._progress_bar)

        self._btn_resume = Gtk.Button(label="▶ Reanudar")
        self._btn_resume.add_css_class("suggested-action")
        self._btn_resume.add_css_class("qts-mono-sm")
        self._btn_resume.connect("clicked", self._on_resume_clicked)
        row.append(self._btn_resume)

        self._btn_audit = Gtk.Button(label="📄 Auditoría")
        self._btn_audit.add_css_class("qts-mono-sm")
        self._btn_audit.connect("clicked", self._on_audit_clicked)
        row.append(self._btn_audit)

        self.append(row)

    def update(self, s: dict, is_current_session: bool = False) -> None:
        self._session_data = s
        name   = s.get("name", "Sin Nombre")
        start  = s["start_ts"]
        end    = s["end_ts"]
        pnl    = s["pnl"]
        cost   = s["api_cost"]
        status = s["status"]
        target = s.get("target_pnl", 0.0)

        # Duración
        if status == "ACTIVE":
            duration_s = int(time.time() - start)
            end_str = "En curso…"
        else:
            duration_s = max(0, end - start)
            end_str = datetime.datetime.fromtimestamp(end).strftime("%H:%M")

        def _dur(sec):
            if sec < 60:   return f"{sec}s"
            if sec < 3600: return f"{sec//60}m"
            return f"{sec//3600}h {(sec%3600)//60}m"

        pnl_col = HEX["buy"] if pnl >= 0 else HEX["sell"]
        if is_current_session:
            status_col = HEX["purple"]
            status_lbl = "ACTIVA (cargada)"
        elif status == "ACTIVE":
            status_col = HEX["warn"]
            status_lbl = "ACTIVA"
        elif status == "HARVESTING":
            status_col = HEX["buy"]
            status_lbl = "HARVEST"
        elif status == "CLOSED":
            status_col = HEX["sub"]
            status_lbl = "CERRADA"
        else:
            status_col = HEX["sub"]
            status_lbl = status

        start_str = datetime.datetime.fromtimestamp(start).strftime("%d/%m %H:%M")

        # Progreso hacia objetivo
        if target > 0 and pnl >= 0:
            progress = min(1.0, pnl / target)
            self._progress_bar.set_fraction(progress)
            self._progress_bar.set_tooltip_text(f"${pnl:.2f} / ${target:.2f} ({progress*100:.0f}%)")
            self._progress_bar.set_visible(True)
        else:
            self._progress_bar.set_visible(False)

        target_str = (f'  <span color="{HEX["teal"]}">Meta:${target:.0f}</span>' if target > 0 else "")

        self._lbl_info.set_markup(
            f'<span color="{HEX["sub"]}">{start_str}</span>  '
            f'<span color="{HEX["blue"]}" weight="bold">{name}</span>'
            f'{target_str}  '
            f'<span color="{pnl_col}" weight="bold">${pnl:>+7.2f}</span>  '
            f'<span color="{HEX["over"]}">API:${cost:.3f}</span>  '
            f'<span color="{HEX["sub"]}">{_dur(duration_s)}</span>  '
            f'<span color="{status_col}" weight="bold">[{status_lbl}]</span>'
        )

        # Mostrar "Reanudar" solo si es ACTIVE y NO es la sesión ya cargada
        can_resume = (status == "ACTIVE") and (not is_current_session)
        self._btn_resume.set_visible(can_resume)

        # Auditoría: solo si existe el archivo
        path = f"storage/audits/audit_{s['id']}.md"
        self._btn_audit.set_sensitive(os.path.exists(path))
        self._btn_audit.set_tooltip_text(
            "Abrir reporte IA" if os.path.exists(path) else "Disponible al finalizar"
        )

    def _on_resume_clicked(self, _btn) -> None:
        self._on_resume_cb(self._session_data)

    def _on_audit_clicked(self, _btn) -> None:
        self._on_audit_cb(self._session_data["id"])


# ─── Vista principal ──────────────────────────────────────────────────────────

class SessionView(Gtk.Box):
    """
    Pestaña de Sesiones — historial + creación + reanudación.

    Callbacks requeridos (pasados en el constructor):
      on_start_session(name, target_pnl, max_drawdown, duration_h)
      on_resume_session(session_data: dict)
      get_active_session_id() -> str | None
    """

    def __init__(
        self,
        on_start_session:      Optional[Callable] = None,
        on_resume_session:     Optional[Callable] = None,
        get_active_session_id: Optional[Callable] = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_hexpand(True)
        self.set_vexpand(True)

        self._on_start   = on_start_session      or (lambda *a: None)
        self._on_resume  = on_resume_session      or (lambda *a: None)
        self._get_active = get_active_session_id  or (lambda: None)

        self._last_refresh = 0.0
        self._cards: List[_SessionCard] = []
        self._parent_win: Optional[Gtk.Window] = None
        self._build()

    def set_parent_window(self, win: Gtk.Window) -> None:
        self._parent_win = win

    def _build(self) -> None:
        P = 12

        # ── Header ───────────────────────────────────────────────────────────
        hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        hdr.set_margin_top(15); hdr.set_margin_bottom(10)
        hdr.set_margin_start(P); hdr.set_margin_end(P)

        lbl = Gtk.Label()
        lbl.set_markup(
            f'<span size="large" weight="bold" color="{HEX["blue"]}">SESIONES / OBJETIVOS</span>'
        )
        lbl.set_xalign(0); lbl.set_hexpand(True)
        hdr.append(lbl)

        btn_new = Gtk.Button(label="＋ Nueva Sesión")
        btn_new.add_css_class("suggested-action")
        btn_new.add_css_class("qts-mono-sm")
        btn_new.connect("clicked", self._on_new_clicked)
        hdr.append(btn_new)

        btn_close_all = Gtk.Button(label="Limpiar Huérfanas")
        btn_close_all.add_css_class("destructive-action")
        btn_close_all.add_css_class("qts-mono-sm")
        btn_close_all.connect("clicked", self._on_close_all_clicked)
        hdr.append(btn_close_all)

        self.append(hdr)

        # ── Resumen activo ────────────────────────────────────────────────────
        self._active_banner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._active_banner.set_margin_start(P); self._active_banner.set_margin_end(P)
        self._active_banner.set_margin_bottom(6)
        self._active_lbl = _ml()
        self._active_lbl.set_hexpand(True)
        self._active_banner.append(self._active_lbl)
        self._active_banner.set_visible(False)
        self.append(self._active_banner)

        self.append(_sep())

        # ── Lista con scroll ─────────────────────────────────────────────────
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        self._rows_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._rows_box.set_margin_start(P); self._rows_box.set_margin_end(P)
        scroll.set_child(self._rows_box)
        self.append(scroll)

    # ── Refresco ──────────────────────────────────────────────────────────────

    def refresh(self) -> None:
        now = time.time()
        if now - self._last_refresh < 5.0:
            return
        self._last_refresh = now

        sessions = get_all_sessions(100)
        current_id = self._get_active()

        # Actualizar banner de sesión activa cargada
        active_loaded = next((s for s in sessions if s["id"] == current_id), None)
        if active_loaded:
            pnl = active_loaded["pnl"]
            target = active_loaded.get("target_pnl", 0)
            col = HEX["buy"] if pnl >= 0 else HEX["sell"]
            target_str = f"  Meta: ${target:.0f}" if target > 0 else ""
            self._active_lbl.set_markup(
                f'<span color="{HEX["purple"]}" weight="bold">● SESIÓN ACTIVA: '
                f'{active_loaded["name"]}</span>  '
                f'<span color="{col}">PnL ${pnl:+.2f}</span>'
                f'<span color="{HEX["teal"]}">{target_str}</span>'
            )
            self._active_banner.set_visible(True)
        else:
            self._active_banner.set_visible(False)

        # Expandir cards si faltan
        while len(self._cards) < len(sessions):
            card = _SessionCard(
                on_resume=self._on_resume_card,
                on_audit=self._on_audit,
            )
            self._rows_box.append(card)
            self._rows_box.append(_sep())
            self._cards.append(card)

        for i, s in enumerate(sessions):
            self._cards[i].set_visible(True)
            self._cards[i].update(s, is_current_session=(s["id"] == current_id))

        for i in range(len(sessions), len(self._cards)):
            self._cards[i].set_visible(False)

    # ── Callbacks ─────────────────────────────────────────────────────────────

    def _on_new_clicked(self, _btn) -> None:
        dlg = _NewSessionDialog(self._parent_win)

        def _on_response(d, resp):
            if resp == Gtk.ResponseType.OK:
                vals = d.get_values()
                self._on_start(
                    vals["name"],
                    vals["target_pnl"],
                    vals["max_drawdown"],
                    vals["duration_h"],
                )
                self._last_refresh = 0.0   # forzar refresco
                GLib.timeout_add(400, self.refresh)
            d.destroy()

        dlg.connect("response", _on_response)

    def _on_resume_card(self, session_data: dict) -> None:
        self._on_resume(session_data)
        self._last_refresh = 0.0
        GLib.timeout_add(400, self.refresh)

    def _on_audit(self, session_id: str) -> None:
        path = f"storage/audits/audit_{session_id}.md"
        if os.path.exists(path):
            os.system(f"xdg-open '{path}' &")

    def _on_close_all_clicked(self, _btn) -> None:
        close_all_sessions()
        self._last_refresh = 0.0
        self.refresh()
