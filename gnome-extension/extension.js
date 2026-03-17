/**
 * QTS — Quantum Trading System · GNOME Shell Extension
 * ──────────────────────────────────────────────────────
 * GNOME Shell 45+ (ES module format — required for Shell ≥ 45).
 *
 * IPC: reads /tmp/qts_status.json written by core/status_writer.py every ~2s.
 *
 * Panel label:  ⚡ XRP  2.5040  +0.8%  ▲65
 * Popup menu:   market · regime · positions · risk
 */

import GLib from 'gi://GLib';
import Gio  from 'gi://Gio';
import St   from 'gi://St';

import { Extension }          from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main              from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu         from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu         from 'resource:///org/gnome/shell/ui/popupMenu.js';


// ── Constantes ──────────────────────────────────────────────────────────────

const STATUS_FILE  = '/tmp/qts_status.json';
const REFRESH_MS   = 2000;   // leer JSON cada 2 s

// Colores GNOME compatibles con el panel (modo oscuro)
const CLR = {
    buy:  '#57e389',   // verde  GNOME success
    sell: '#ff7b63',   // rojo   GNOME destructive
    warn: '#f8e45c',   // amarillo GNOME warning
    blue: '#78aeed',   // azul   GNOME accent
    teal: '#93ddc2',   // cyan
    over: '#9a9996',   // dim text
};


// ── Helpers ──────────────────────────────────────────────────────────────────

function _fmt_price(p) {
    if (!p || p <= 0) return '──';
    if (p >= 1000)  return p.toFixed(1);
    if (p >= 10)    return p.toFixed(3);
    if (p >= 1)     return p.toFixed(4);
    return p.toFixed(5);
}

function _fmt_pnl(v) {
    if (v === undefined || v === null) return '──';
    const sign = v >= 0 ? '+' : '';
    return `${sign}${v.toFixed(2)}`;
}

function _color(hex, text) {
    return `<span color="${hex}">${text}</span>`;
}

function _bold(text) {
    return `<span weight="bold">${text}</span>`;
}

function _read_json() {
    try {
        const file    = Gio.File.new_for_path(STATUS_FILE);
        const [ok, contents] = file.load_contents(null);
        if (!ok) return null;
        const text = new TextDecoder().decode(contents);
        return JSON.parse(text);
    } catch (_e) {
        return null;
    }
}

// Convierte age en segundos a string legible
function _age_str(ts) {
    const age = Math.floor(Date.now() / 1000) - ts;
    if (age < 5)   return '';
    if (age < 60)  return ` (${age}s)`;
    return ` (${Math.floor(age / 60)}m)`;
}


// ── Indicador ────────────────────────────────────────────────────────────────

class QTSIndicator extends PanelMenu.Button {

    constructor(ext) {
        super(0.0, 'QTS Trading');
        this._ext = ext;

        // ── Etiqueta del panel ──────────────────────────────────────────────
        this._label = new St.Label({
            text:             '⚡ QTS',
            y_align:          1,   // Clutter.ActorAlign.CENTER
            style_class:      'qts-panel-label',
        });
        this.add_child(this._label);

        // ── Popup ───────────────────────────────────────────────────────────
        this._build_menu();

        // ── Timer ───────────────────────────────────────────────────────────
        this._timer_id = GLib.timeout_add(GLib.PRIORITY_DEFAULT, REFRESH_MS, () => {
            this._refresh();
            return GLib.SOURCE_CONTINUE;
        });

        // Primera lectura inmediata
        this._refresh();
    }

    // ── Construcción del menú ───────────────────────────────────────────────

    _build_menu() {
        const menu = this.menu;

        // Cabecera del popup (título + símbolo)
        this._mi_header = this._add_markup_item(menu, '⚡ QTS — Quantum Trading System');
        this._mi_header.label.style = 'font-weight: bold;';
        menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        // ── Sección: Mercado ────────────────────────────────────────────────
        this._mi_price    = this._add_markup_item(menu, 'Precio:  ──');
        this._mi_change   = this._add_markup_item(menu, 'Cambio:  ──');
        this._mi_abs      = this._add_markup_item(menu, 'Absorción: ──');
        menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        // ── Sección: Señal ──────────────────────────────────────────────────
        this._mi_regime   = this._add_markup_item(menu, 'Régimen:  ──');
        this._mi_trend    = this._add_markup_item(menu, 'Tendencia: ──');
        this._mi_opp      = this._add_markup_item(menu, 'Score:    ──');
        menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        // ── Sección: Cuenta ─────────────────────────────────────────────────
        this._mi_balance  = this._add_markup_item(menu, 'Equity:   ──');
        this._mi_margin   = this._add_markup_item(menu, 'Margen:   ──');
        this._mi_dpnl     = this._add_markup_item(menu, 'PnL día:  ──');
        menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        // ── Sección: Posiciones (máx 5, dinámicas) ─────────────────────────
        this._pos_header = this._add_markup_item(menu, _color(CLR.over, 'POSICIONES ABIERTAS'));
        this._pos_items  = [];
        for (let i = 0; i < 5; i++) {
            const mi = this._add_markup_item(menu, '');
            mi.actor.hide();
            this._pos_items.push(mi);
        }
        this._mi_no_pos = this._add_markup_item(menu, _color(CLR.over, '  Sin posiciones abiertas'));

        menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        // ── Riesgo ──────────────────────────────────────────────────────────
        this._mi_risk = this._add_markup_item(menu, 'Riesgo:   ──');

        menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        // Acción: abrir QTS (lanza la app si no está corriendo)
        const open_item = new PopupMenu.PopupMenuItem('Abrir QTS');
        open_item.connect('activate', () => {
            try {
                GLib.spawn_command_line_async('bash -c "cd ~/Projects/trading && python -m interface.gtk_app 2>/dev/null &"');
            } catch (_e) {}
        });
        menu.addMenuItem(open_item);
    }

    _add_markup_item(menu, markup) {
        const item = new PopupMenu.PopupMenuItem('');
        item.label.clutter_text.set_use_markup(true);
        item.label.set_text(markup);
        // Reemplazar el texto plano por markup
        item.label.clutter_text.set_markup(markup);
        menu.addMenuItem(item);
        return item;
    }

    _set_markup(mi, markup) {
        mi.label.clutter_text.set_markup(markup);
    }

    // ── Refresco ────────────────────────────────────────────────────────────

    _refresh() {
        const d = _read_json();
        if (!d) {
            this._label.set_text('⚡ QTS');
            this._set_markup(this._mi_header, _color(CLR.over, '⚡ QTS — sin datos'));
            return;
        }
        this._update_panel(d);
        this._update_menu(d);
    }

    _update_panel(d) {
        const sym     = d.symbol || '??';
        const price   = _fmt_price(d.price);
        const chg     = d.change_pct !== undefined ? `${d.change_pct >= 0 ? '+' : ''}${d.change_pct.toFixed(2)}%` : '';
        const opp     = d.opportunity || {};
        const risk    = d.risk || {};

        // Arrow + score si hay señal accionable
        let signal_str = '';
        if (opp.is_actionable) {
            const arrow = opp.direction === 'LONG' ? '▲' : '▼';
            signal_str = ` ${arrow}${opp.score}`;
        }

        // Icono de riesgo
        let risk_icon = '';
        if (risk.level === 'CIRCUIT_BREAKER') risk_icon = ' 🔴';
        else if (risk.level === 'ALERT')       risk_icon = ' ⚠';
        else if (risk.level === 'WARNING')     risk_icon = ' ⚠';

        this._label.set_text(`⚡ ${sym}  ${price}  ${chg}${signal_str}${risk_icon}`);
    }

    _update_menu(d) {
        const sym     = d.symbol || '??';
        const opp     = d.opportunity || {};
        const regime  = d.regime    || {};
        const trend   = d.trend     || {};
        const risk    = d.risk      || {};
        const balance = d.balance   || {};
        const abs     = d.absorption || {};
        const age     = _age_str(d.ts || 0);

        // Cabecera
        this._set_markup(this._mi_header,
            `${_bold('⚡ QTS')} — ${_color(CLR.blue, sym)}${_color(CLR.over, age)}`
        );

        // Precio y cambio
        const price = _fmt_price(d.price);
        const chg   = d.change_pct !== undefined ? d.change_pct : 0;
        const chg_c = chg >= 0 ? CLR.buy : CLR.sell;
        const chg_s = `${chg >= 0 ? '+' : ''}${chg.toFixed(2)}%`;

        this._set_markup(this._mi_price,
            `Precio:   ${_bold(_color(chg_c, price))}`
        );
        this._set_markup(this._mi_change,
            `24h:      ${_color(chg_c, chg_s)}`
        );

        // Absorción
        if (abs.is_signal) {
            const abs_c = abs.side === 'BUY' ? CLR.buy : CLR.sell;
            const abs_s = abs.side === 'BUY' ? 'COMPRADORA' : 'VENDEDORA';
            this._set_markup(this._mi_abs,
                `Absorción: ${_bold(_color(abs_c, abs_s))}  ${_color(CLR.over, String(abs.score))}`
            );
        } else {
            this._set_markup(this._mi_abs,
                `Absorción: ${_color(CLR.over, '──')}`
            );
        }

        // Régimen
        const reg_c = CLR[regime.color_key] || CLR.over;
        this._set_markup(this._mi_regime,
            `Régimen:  ${_color(reg_c, regime.label || '──')}  ${_color(CLR.over, `(${regime.confidence || 0}%)`)}`
        );

        // Tendencia
        const trend_c = trend.direction === 'ALCISTA' ? CLR.buy :
                        trend.direction === 'BAJISTA' ? CLR.sell : CLR.over;
        this._set_markup(this._mi_trend,
            `Tendencia: ${_color(trend_c, trend.label || '──')}`
        );

        // Score de oportunidad
        if (opp.is_actionable) {
            const opp_c = opp.direction === 'LONG' ? CLR.buy : CLR.sell;
            const arrow = opp.direction === 'LONG' ? '▲' : '▼';
            this._set_markup(this._mi_opp,
                `Score:    ${_bold(_color(opp_c, `${arrow} ${opp.score}`))}  ${_color(CLR.over, opp.direction)}`
            );
        } else {
            this._set_markup(this._mi_opp,
                `Score:    ${_color(CLR.over, '──')}`
            );
        }

        // Balance
        const eq  = balance.equity    ? `$${balance.equity.toFixed(2)}` : '──';
        const mgn = balance.margin_pct ? `${balance.margin_pct.toFixed(1)}%` : '──';
        this._set_markup(this._mi_balance,
            `Equity:   ${_color(CLR.teal, eq)}`
        );
        const mgn_c = (balance.margin_pct || 0) >= 80 ? CLR.sell :
                      (balance.margin_pct || 0) >= 60 ? CLR.warn : CLR.over;
        this._set_markup(this._mi_margin,
            `Margen:   ${_color(mgn_c, mgn)}`
        );

        // PnL diario
        const dpnl   = risk.daily_pnl_usd || 0;
        const dpnl_p = risk.daily_pnl_pct  || 0;
        const dpnl_c = dpnl >= 0 ? CLR.buy : CLR.sell;
        this._set_markup(this._mi_dpnl,
            `PnL día:  ${_color(dpnl_c, `${_fmt_pnl(dpnl)}$ (${dpnl_p >= 0 ? '+' : ''}${dpnl_p.toFixed(2)}%)`)}`
        );

        // Posiciones
        const positions = d.positions || [];
        if (positions.length === 0) {
            this._mi_no_pos.actor.show();
            this._pos_items.forEach(mi => mi.actor.hide());
        } else {
            this._mi_no_pos.actor.hide();
            this._pos_items.forEach((mi, i) => {
                if (i < positions.length) {
                    const p     = positions[i];
                    const p_c   = p.side === 'LONG' ? CLR.buy : CLR.sell;
                    const pnl_c = p.unrealized_pnl >= 0 ? CLR.buy : CLR.sell;
                    const lev   = p.leverage > 1 ? ` ${p.leverage}x` : '';
                    this._set_markup(mi,
                        `  ${_bold(_color(p_c, p.side))} ${_color(CLR.blue, p.symbol)}${lev}` +
                        `  ${_color(CLR.over, _fmt_price(p.entry_price))}→${_fmt_price(p.mark_price)}` +
                        `  ${_bold(_color(pnl_c, _fmt_pnl(p.unrealized_pnl) + '$'))}` +
                        `  ${_color(pnl_c, '(' + (p.pnl_pct >= 0 ? '+' : '') + p.pnl_pct.toFixed(2) + '%)')}`
                    );
                    mi.actor.show();
                } else {
                    mi.actor.hide();
                }
            });
        }

        // Riesgo
        const risk_c = risk.level === 'CIRCUIT_BREAKER' ? CLR.sell :
                       risk.level === 'ALERT'            ? CLR.sell :
                       risk.level === 'WARNING'          ? CLR.warn : CLR.over;
        const risk_icon = risk.level === 'CIRCUIT_BREAKER' ? '🔴 ' :
                          risk.level === 'ALERT'            ? '⚠ '  :
                          risk.level === 'WARNING'          ? '⚠ '  : '✓ ';
        const risk_msg = risk.message || 'OK';
        this._set_markup(this._mi_risk,
            `Riesgo:   ${_color(risk_c, risk_icon + risk_msg)}`
        );
    }

    // ── Destrucción ─────────────────────────────────────────────────────────

    destroy() {
        if (this._timer_id) {
            GLib.source_remove(this._timer_id);
            this._timer_id = null;
        }
        super.destroy();
    }
}


// ── Extension entry point ────────────────────────────────────────────────────

export default class QTSExtension extends Extension {

    enable() {
        this._indicator = new QTSIndicator(this);
        Main.panel.addToStatusArea(this.uuid, this._indicator, 0, 'right');
    }

    disable() {
        if (this._indicator) {
            this._indicator.destroy();
            this._indicator = null;
        }
    }
}
