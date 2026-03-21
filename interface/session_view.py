"""
interface/session_view.py
───────────────────────────
SessionView — Historial de sesiones TSAA y acceso a auditorías.
"""
from __future__ import annotations

import datetime
import os
import time
from typing import List

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk, Pango

from core.db import get_all_sessions, close_all_sessions


HEX = {
    "buy":  "#57e389", "sell": "#ff7b63", "blue": "#78aeed",
    "warn": "#f8e45c", "teal": "#93ddc2", "text": "#ebebeb",
    "sub":  "#9a9996", "over": "#5e5c64",
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

class _SessionCard(Gtk.Box):
    """Fila que representa una sesión individual."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.set_margin_top(4)
        self.set_margin_bottom(4)

        self._lbl_info = _ml()
        self._lbl_info.set_hexpand(True)
        self.append(self._lbl_info)

        self._btn_audit = Gtk.Button(label="Ver Auditoría")
        self._btn_audit.add_css_class("suggested-action")
        self._btn_audit.add_css_class("qts-mono-sm")
        self._btn_audit.connect("clicked", self._on_audit_clicked)
        self.append(self._btn_audit)
        
        self._session_id = ""

    def update(self, s: dict) -> None:
        self._session_id = s["id"]
        name   = s.get("name", "Sin Nombre")
        start  = s["start_ts"]
        end    = s["end_ts"]
        pnl    = s["pnl"]
        cost   = s["api_cost"]
        status = s["status"]

        # Calcular duración
        if status == "ACTIVE":
            duration_s = int(time.time() - start)
            end_str = "En curso..."
        else:
            duration_s = max(0, end - start)
            end_str = datetime.datetime.fromtimestamp(end).strftime("%H:%M")

        def _dur(sec):
            if sec < 60: return f"{sec}s"
            if sec < 3600: return f"{sec//60}m"
            return f"{sec//3600}h {(sec%3600)//60}m"

        pnl_col = HEX["buy"] if pnl >= 0 else HEX["sell"]
        status_col = HEX["warn"] if status != "CLOSED" else HEX["sub"]
        
        start_str = datetime.datetime.fromtimestamp(start).strftime("%d/%m %H:%M")
        
        self._lbl_info.set_markup(
            f'<span color="{HEX["sub"]}">{start_str}</span> '
            f'<span color="{HEX["blue"]}" weight="bold">{name:<16}</span> '
            f'<span color="{pnl_col}" weight="bold">${pnl:>7.2f}</span> '
            f'<span color="{HEX["teal"]}">API:${cost:>5.3f}</span> '
            f'<span color="{HEX["sub"]}">{_dur(duration_s)}</span> '
            f'<span color="{status_col}">[{status}]</span>'
        )
        
        # Solo permitir ver auditoría si existe el archivo
        path = f"storage/audits/audit_{self._session_id}.md"
        exists = os.path.exists(path)
        self._btn_audit.set_sensitive(exists)
        self._btn_audit.set_tooltip_text("Disponible al finalizar la sesión" if not exists else "Abrir reporte IA")

    def _on_audit_clicked(self, _btn) -> None:
        path = f"storage/audits/audit_{self._session_id}.md"
        if os.path.exists(path):
            os.system(f"xdg-open '{path}' &")

class SessionView(Gtk.Box):
    """Pestaña de Historial de Sesiones."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self._last_refresh = 0.0
        self._cards: List[_SessionCard] = []
        self._build()

    def _build(self) -> None:
        P = 12
        # Header Box
        hdr_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        hdr_box.set_margin_top(15); hdr_box.set_margin_bottom(10)
        hdr_box.set_margin_start(P); hdr_box.set_margin_end(P)

        lbl = Gtk.Label()
        lbl.set_markup(f'<span size="large" weight="bold" color="{HEX["blue"]}">HISTORIAL DE SESIONES TSAA</span>')
        lbl.set_xalign(0); lbl.set_hexpand(True)
        hdr_box.append(lbl)

        btn_close_all = Gtk.Button(label="Cerrar Todo (Limpiar)")
        btn_close_all.add_css_class("destructive-action")
        btn_close_all.add_css_class("qts-mono-sm")
        btn_close_all.connect("clicked", self._on_close_all_clicked)
        hdr_box.append(btn_close_all)

        self.append(hdr_box)
        self.append(_sep())

        # Scroll
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        self._rows_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._rows_box.set_margin_start(P); self._rows_box.set_margin_end(P)
        scroll.set_child(self._rows_box)
        self.append(scroll)

    def refresh(self) -> None:
        now = time.time()
        if now - self._last_refresh < 5.0:
            return
        self._last_refresh = now
        
        sessions = get_all_sessions(50)
        
        # Reusar cards
        current_len = len(self._cards)
        needed_len = len(sessions)
        
        while len(self._cards) < needed_len:
            card = _SessionCard()
            self._rows_box.append(card)
            self._rows_box.append(_sep())
            self._cards.append(card)
            
        for i, s in enumerate(sessions):
            self._cards[i].set_visible(True)
            self._cards[i].update(s)
            
        for i in range(needed_len, len(self._cards)):
            self._cards[i].set_visible(False)

    def _on_close_all_clicked(self, _btn) -> None:
        # Diálogo de confirmación simple o ejecución directa
        close_all_sessions()
        self._last_refresh = 0 # forzar refresh
        self.refresh()
