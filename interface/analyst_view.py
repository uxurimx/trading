"""
interface/analyst_view.py
──────────────────────────
Pestaña "🔬 Analista" — Análisis automático de logs + sugerencias de mejora.

Se activa automáticamente al cerrar una sesión TSAA y muestra:
  · Hallazgos detectados (problemas reales con severidad)
  · Causa raíz y solución específica
  · Código del fix listo para leer
  · Botón "Analizar ahora" para disparar manualmente
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk, Pango  # noqa: E402

from core.log_analyst import log_analyst, Finding, AnalysisReport

# ── Colores (paleta QTS) ──────────────────────────────────────────────────────
HEX = {
    "critical": "#ff7b63",
    "warning":  "#f8e45c",
    "info":     "#78aeed",
    "ok":       "#57e389",
    "text":     "#ebebeb",
    "sub":      "#9a9996",
    "bg":       "#1e1e2e",
    "card":     "#2a2a3e",
}


def _mk(text: str, color: str = "", bold: bool = False, small: bool = False) -> str:
    """Genera markup Pango."""
    span_attrs = []
    if color:
        span_attrs.append(f'color="{color}"')
    if bold:
        span_attrs.append('font_weight="bold"')
    if small:
        span_attrs.append('font_size="small"')
    if span_attrs:
        return f'<span {" ".join(span_attrs)}>{GLib.markup_escape_text(text)}</span>'
    return GLib.markup_escape_text(text)


def _lbl(text: str = "", markup: str = "", xalign: float = 0.0, wrap: bool = False) -> Gtk.Label:
    lbl = Gtk.Label()
    lbl.set_xalign(xalign)
    lbl.set_use_markup(True)
    if wrap:
        lbl.set_wrap(True)
        lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    if markup:
        lbl.set_markup(markup)
    elif text:
        lbl.set_label(text)
    return lbl


# ─── Tarjeta de hallazgo ──────────────────────────────────────────────────────

class FindingCard(Gtk.Box):
    """Muestra un hallazgo individual con su causa, solución y código."""

    def __init__(self, finding: Finding):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.set_margin_start(4)
        self.set_margin_end(4)
        self.set_margin_top(4)
        self.set_margin_bottom(4)
        self.add_css_class("card")

        # ── Header (severity + título) ───────────────────────────────────────
        hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hdr.set_margin_start(10)
        hdr.set_margin_end(10)
        hdr.set_margin_top(8)

        icon = "🔴" if finding.severity == "CRITICAL" else "🟡" if finding.severity == "WARNING" else "🔵"
        color = HEX["critical"] if finding.severity == "CRITICAL" else \
                HEX["warning"] if finding.severity == "WARNING" else HEX["info"]

        sev_lbl = _lbl(markup=f'<span color="{color}" font_weight="bold">{icon} {finding.severity}</span>')
        hdr.append(sev_lbl)

        if finding.frequency > 1:
            freq_lbl = _lbl(markup=_mk(f"×{finding.frequency}", color=HEX["sub"], small=True))
            hdr.append(freq_lbl)

        self.append(hdr)

        # ── Problema ──────────────────────────────────────────────────────────
        prob = _lbl(markup=_mk(finding.problem, bold=True), wrap=True)
        prob.set_margin_start(10)
        prob.set_margin_end(10)
        self.append(prob)

        # ── Causa ─────────────────────────────────────────────────────────────
        cause_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        cause_row.set_margin_start(10)
        cause_row.set_margin_end(10)
        lbl_tag = _lbl(markup=_mk("Causa:", color=HEX["sub"]))
        cause_txt = _lbl(markup=_mk(finding.root_cause, color=HEX["text"]), wrap=True)
        cause_row.append(lbl_tag)
        cause_row.append(cause_txt)
        self.append(cause_row)

        # ── Solución ──────────────────────────────────────────────────────────
        sol_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        sol_row.set_margin_start(10)
        sol_row.set_margin_end(10)
        lbl_sol = _lbl(markup=_mk("Solución:", color=HEX["ok"], bold=True))
        sol_txt = _lbl(markup=_mk(finding.solution, color=HEX["text"]), wrap=True)
        sol_row.append(lbl_sol)
        sol_row.append(sol_txt)
        self.append(sol_row)

        # ── Símbolos afectados ────────────────────────────────────────────────
        if finding.affected:
            aff_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            aff_row.set_margin_start(10)
            aff_row.set_margin_end(10)
            aff_lbl = _lbl(markup=_mk("Afectado:", color=HEX["sub"]))
            aff_val = _lbl(markup=_mk(", ".join(finding.affected), color=HEX["warning"]))
            aff_row.append(aff_lbl)
            aff_row.append(aff_val)
            self.append(aff_row)

        # ── Código del fix (expandible) ───────────────────────────────────────
        if finding.code_fix:
            exp = Gtk.Expander()
            exp.set_label("💾 Ver código del fix")
            exp.set_margin_start(10)
            exp.set_margin_end(10)
            exp.set_margin_bottom(8)

            code_frame = Gtk.Frame()
            code_frame.add_css_class("code-frame")

            tv = Gtk.TextView()
            tv.set_editable(False)
            tv.set_cursor_visible(False)
            tv.set_monospace(True)
            tv.set_wrap_mode(Gtk.WrapMode.NONE)
            tv.set_margin_start(8)
            tv.set_margin_end(8)
            tv.set_margin_top(6)
            tv.set_margin_bottom(6)
            tv.get_buffer().set_text(finding.code_fix.strip())
            tv.add_css_class("code-view")

            code_frame.set_child(tv)
            exp.set_child(code_frame)
            self.append(exp)
        else:
            # Espaciado inferior si no hay código
            spacer = Gtk.Box()
            spacer.set_size_request(-1, 8)
            self.append(spacer)


# ─── Vista principal ──────────────────────────────────────────────────────────

class AnalystView(Gtk.Box):
    """
    Pestaña de análisis automático de logs.
    Se auto-ejecuta al cerrar una sesión TSAA y permite disparo manual.
    """

    def __init__(self, bridge):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._bridge = bridge
        self._last_session_id: Optional[str] = None
        self._analyzing: bool = False

        # ── CSS específico ───────────────────────────────────────────────────
        css_provider = Gtk.CssProvider()
        css_provider.load_from_data(b"""
            .code-view {
                background-color: #1a1a2e;
                color: #a6e3a1;
                font-family: monospace;
                font-size: 11px;
            }
            .code-frame {
                border: 1px solid #3a3a4e;
                border-radius: 4px;
            }
            .analyst-header {
                background-color: #2a2a3e;
                border-bottom: 1px solid #3a3a4e;
            }
            .finding-scroll {
                background: transparent;
            }
        """)
        Gtk.StyleContext.add_provider_for_display(
            self.get_display() if hasattr(self, 'get_display') else
            Gtk.Widget.get_display(self),
            css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        ) if False else None  # Se aplica en realize

        self._css = css_provider

        # ── Header ───────────────────────────────────────────────────────────
        hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        hdr.set_margin_start(12)
        hdr.set_margin_end(12)
        hdr.set_margin_top(10)
        hdr.set_margin_bottom(10)
        hdr.add_css_class("analyst-header")

        title = _lbl(markup='<span font_weight="bold" font_size="large">🔬 Analista de Sistema</span>')
        title.set_hexpand(True)
        hdr.append(title)

        # Horas a analizar
        hours_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        hours_lbl = _lbl(markup=_mk("Horas:", color=HEX["sub"]))
        self._hours_spin = Gtk.SpinButton()
        adj = Gtk.Adjustment(value=4, lower=1, upper=72, step_increment=1, page_increment=6)
        self._hours_spin.set_adjustment(adj)
        self._hours_spin.set_numeric(True)
        self._hours_spin.set_size_request(70, -1)
        hours_box.append(hours_lbl)
        hours_box.append(self._hours_spin)
        hdr.append(hours_box)

        # Botón analizar
        self._btn_analyze = Gtk.Button(label="🔍 Analizar ahora")
        self._btn_analyze.add_css_class("suggested-action")
        self._btn_analyze.connect("clicked", self._on_analyze_clicked)
        hdr.append(self._btn_analyze)

        self.append(hdr)

        # ── Status bar ───────────────────────────────────────────────────────
        self._status_lbl = _lbl(markup=_mk("Esperando sesión o análisis manual…", color=HEX["sub"]))
        self._status_lbl.set_margin_start(14)
        self._status_lbl.set_margin_top(4)
        self._status_lbl.set_margin_bottom(4)
        self.append(self._status_lbl)

        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        self.append(sep)

        # ── Área de hallazgos (scrollable) ───────────────────────────────────
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_hexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self._findings_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._findings_box.set_margin_start(8)
        self._findings_box.set_margin_end(8)
        self._findings_box.set_margin_top(8)
        self._findings_box.set_margin_bottom(8)

        # Placeholder inicial
        self._placeholder = self._make_placeholder()
        self._findings_box.append(self._placeholder)

        scroll.set_child(self._findings_box)
        self.append(scroll)

        # Aplicar CSS al realizarse el widget
        self.connect("realize", self._on_realize)

    def _on_realize(self, _widget) -> None:
        Gtk.StyleContext.add_provider_for_display(
            self.get_display(),
            self._css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    def _make_placeholder(self) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_halign(Gtk.Align.CENTER)
        box.set_valign(Gtk.Align.CENTER)
        box.set_vexpand(True)
        box.set_margin_top(40)

        icon = _lbl(markup='<span font_size="xx-large">🔬</span>', xalign=0.5)
        icon.set_halign(Gtk.Align.CENTER)
        box.append(icon)

        msg = _lbl(
            markup=_mk("El análisis se ejecuta automáticamente\nal finalizar cada sesión TSAA.", color=HEX["sub"]),
            xalign=0.5, wrap=True,
        )
        msg.set_halign(Gtk.Align.CENTER)
        msg.set_justify(Gtk.Justification.CENTER)
        box.append(msg)

        hint = _lbl(
            markup=_mk('También puedes pulsar "Analizar ahora" en cualquier momento.', color=HEX["sub"], small=True),
            xalign=0.5, wrap=True,
        )
        hint.set_halign(Gtk.Align.CENTER)
        hint.set_justify(Gtk.Justification.CENTER)
        box.append(hint)

        return box

    # ── Disparo ───────────────────────────────────────────────────────────────

    def _on_analyze_clicked(self, _btn) -> None:
        if self._analyzing:
            return
        hours = int(self._hours_spin.get_value())
        self._run_analysis(hours)

    def notify_session_closed(self, session_id: str) -> None:
        """Llamado desde gtk_app cuando una sesión TSAA se cierra."""
        if session_id == self._last_session_id:
            return
        self._last_session_id = session_id
        GLib.idle_add(self._start_auto_analysis, session_id)

    def _start_auto_analysis(self, session_id: str) -> bool:
        hours = int(self._hours_spin.get_value())
        self._set_status(f"⏳ Sesión {session_id[:8]} cerrada — analizando últimas {hours}h…", HEX["warning"])
        self._run_analysis(hours)
        return False

    def _run_analysis(self, hours: int) -> None:
        if self._analyzing:
            return
        self._analyzing = True
        self._btn_analyze.set_sensitive(False)
        self._btn_analyze.set_label("⏳ Analizando…")
        self._set_status("Analizando logs…", HEX["warning"])

        async def _task():
            report = await log_analyst.analyze(hours=hours)
            GLib.idle_add(self._show_report, report)

        self._bridge.submit(_task())

    # ── Renderizado ───────────────────────────────────────────────────────────

    def _show_report(self, report: AnalysisReport) -> bool:
        self._analyzing = False
        self._btn_analyze.set_sensitive(True)
        self._btn_analyze.set_label("🔍 Analizar ahora")

        # Limpiar findings anteriores
        child = self._findings_box.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self._findings_box.remove(child)
            child = nxt

        ts_str = time.strftime("%H:%M:%S")

        _report_error = getattr(report, 'error', None)
        if _report_error:
            self._set_status(f"❌ Error: {_report_error}", HEX["critical"])
            err_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            err_card.add_css_class("card")
            err_card.set_margin_start(4)
            err_card.set_margin_end(4)
            err_card.set_margin_top(4)
            err_lbl = _lbl(
                markup=_mk(f"Error al analizar logs: {_report_error}", color=HEX["critical"]),
                wrap=True,
            )
            err_lbl.set_margin_start(10)
            err_lbl.set_margin_top(8)
            err_lbl.set_margin_bottom(8)
            err_card.append(err_lbl)
            self._findings_box.append(err_card)
            return False

        if not report.findings:
            self._set_status(f"✅ Sin problemas detectados — {ts_str}", HEX["ok"])
            ok_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            ok_box.set_halign(Gtk.Align.CENTER)
            ok_box.set_valign(Gtk.Align.CENTER)
            ok_box.set_vexpand(True)
            ok_box.set_margin_top(40)
            icon = _lbl(markup='<span font_size="xx-large">✅</span>', xalign=0.5)
            icon.set_halign(Gtk.Align.CENTER)
            ok_box.append(icon)
            ok_lbl = _lbl(
                markup=_mk("No se detectaron problemas en los logs del período analizado.", color=HEX["ok"]),
                xalign=0.5, wrap=True,
            )
            ok_lbl.set_halign(Gtk.Align.CENTER)
            ok_lbl.set_justify(Gtk.Justification.CENTER)
            ok_box.append(ok_lbl)
            if report.summary:
                sum_lbl = _lbl(markup=_mk(report.summary, color=HEX["sub"], small=True), xalign=0.5, wrap=True)
                sum_lbl.set_halign(Gtk.Align.CENTER)
                sum_lbl.set_justify(Gtk.Justification.CENTER)
                ok_box.append(sum_lbl)
            self._findings_box.append(ok_box)
            return False

        # Resumen
        critical = sum(1 for f in report.findings if f.severity == "CRITICAL")
        warning  = sum(1 for f in report.findings if f.severity == "WARNING")
        status_parts = []
        if critical:
            status_parts.append(f"🔴 {critical} crítico{'s' if critical > 1 else ''}")
        if warning:
            status_parts.append(f"🟡 {warning} warning{'s' if warning > 1 else ''}")
        self._set_status(
            f"{' | '.join(status_parts)} — {ts_str}",
            HEX["critical"] if critical else HEX["warning"],
        )

        # Tarjeta de resumen
        sum_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        sum_card.add_css_class("card")
        sum_card.set_margin_start(4)
        sum_card.set_margin_end(4)
        sum_card.set_margin_top(4)
        sum_lbl = _lbl(
            markup=f'<span font_weight="bold">{GLib.markup_escape_text(report.summary)}</span>',
            wrap=True,
        )
        sum_lbl.set_margin_start(10)
        sum_lbl.set_margin_end(10)
        sum_lbl.set_margin_top(8)
        sum_lbl.set_margin_bottom(8)
        sum_card.append(sum_lbl)
        self._findings_box.append(sum_card)

        # Tarjeta por hallazgo
        for finding in report.findings:
            card = FindingCard(finding)
            self._findings_box.append(card)

        return False

    def _set_status(self, text: str, color: str = "") -> None:
        if color:
            self._status_lbl.set_markup(f'<span color="{color}">{GLib.markup_escape_text(text)}</span>')
        else:
            self._status_lbl.set_text(text)
