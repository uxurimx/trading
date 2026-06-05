#!/usr/bin/env python3
"""
Trade Monitor — GTK4 live position tracker.
Run: python interface/trade_monitor.py
"""
import gi, threading, time, hmac, hashlib, urllib.parse, json, urllib.request, os, sys

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Gdk, Pango

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
try:
    from core.config import settings
    API_KEY = settings.bybit_api_key
    API_SECRET = settings.bybit_api_secret
except Exception:
    API_KEY = os.getenv("BYBIT_API_KEY", "")
    API_SECRET = os.getenv("BYBIT_API_SECRET", "")


def bybit_get(endpoint, params={}):
    ts = str(int(time.time() * 1000))
    recv = "10000"
    q = urllib.parse.urlencode(params)
    pre = ts + API_KEY + recv + q
    sig = hmac.new(API_SECRET.encode(), pre.encode(), hashlib.sha256).hexdigest()
    headers = {
        "X-BAPI-API-KEY": API_KEY, "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": recv, "X-BAPI-SIGN": sig,
    }
    url = f"https://api.bybit.com{endpoint}?{q}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read())


class TradeMonitorApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id="dev.trading.TradeMonitor")
        self.connect("activate", self.on_activate)

    def on_activate(self, app):
        win = TradeMonitorWindow(application=app)
        win.present()


class TradeMonitorWindow(Adw.ApplicationWindow):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.set_title("Trade Monitor — QTS")
        self.set_default_size(780, 600)

        self._tick = 0
        self._log_store = Gtk.ListStore(str, str, str, str, str, str)  # time, sym, side, price, pnl, sl/tp
        self._event_buf = Gtk.TextBuffer()

        self._build_ui()
        self._start_refresh()

    # ─── UI ────────────────────────────────────────────────────────────────

    def _build_ui(self):
        header = Adw.HeaderBar()
        refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh_btn.connect("clicked", lambda _: threading.Thread(target=self._do_refresh, daemon=True).start())
        header.pack_end(refresh_btn)

        self._status_lbl = Gtk.Label(label="Conectando…")
        self._status_lbl.set_css_classes(["caption", "dim-label"])
        header.set_title_widget(self._status_lbl)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        root.append(header)

        notebook = Gtk.Notebook()
        notebook.set_vexpand(True)
        notebook.set_margin_top(8)
        notebook.set_margin_bottom(8)
        notebook.set_margin_start(8)
        notebook.set_margin_end(8)

        notebook.append_page(self._build_overview_tab(), Gtk.Label(label="Resumen"))
        notebook.append_page(self._build_live_tab(),     Gtk.Label(label="Live Ticks"))

        root.append(notebook)
        self.set_content(root)

    # ── Tab 1: Overview ─────────────────────────────────────────────────────

    def _build_overview_tab(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(12); box.set_margin_bottom(12)
        box.set_margin_start(12); box.set_margin_end(12)

        # Balance row
        self._balance_row = Adw.ActionRow(title="Balance total")
        self._balance_row.set_activatable(False)
        self._eq_lbl = Gtk.Label(label="—")
        self._eq_lbl.set_css_classes(["title-2"])
        self._eq_lbl.set_halign(Gtk.Align.END)
        self._balance_row.add_suffix(self._eq_lbl)
        self._usdt_lbl = Gtk.Label(label="—")
        self._usdt_lbl.set_css_classes(["caption", "dim-label"])
        self._usdt_lbl.set_halign(Gtk.Align.END)
        self._balance_row.add_suffix(self._usdt_lbl)

        top_group = Adw.PreferencesGroup()
        top_group.add(self._balance_row)

        # Positions
        pos_group = Adw.PreferencesGroup(title="Posiciones abiertas")
        self._pos_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        pos_group.add(self._pos_box)

        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        inner.append(top_group)
        inner.append(pos_group)
        scroll.set_child(inner)
        box.append(scroll)
        return box

    # ── Tab 2: Live Ticks ──────────────────────────────────────────────────

    def _build_live_tab(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(8); box.set_margin_bottom(8)
        box.set_margin_start(8); box.set_margin_end(8)

        # Event log (trail, BE, closes)
        event_group = Adw.PreferencesGroup(title="Eventos del trader")
        self._event_view = Gtk.TextView(buffer=self._event_buf)
        self._event_view.set_editable(False)
        self._event_view.set_monospace(True)
        self._event_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        css = Gtk.CssProvider()
        css.load_from_data(b"textview { font-size: 11px; }")
        self._event_view.get_style_context().add_provider(css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        ev_scroll = Gtk.ScrolledWindow()
        ev_scroll.set_child(self._event_view)
        ev_scroll.set_min_content_height(120)
        ev_scroll.set_vexpand(False)
        event_group.add(ev_scroll)
        self._ev_adj = ev_scroll.get_vadjustment()

        # Tick table
        tick_group = Adw.PreferencesGroup(title="Ticks en vivo")
        tv = Gtk.TreeView(model=self._log_store, headers_visible=True)
        tv.set_vexpand(True)
        cols = [("Hora", 70), ("Par", 80), ("Dir", 55), ("Precio", 95), ("PnL", 90), ("SL / TP dist", 150)]
        for i, (col, w) in enumerate(cols):
            r = Gtk.CellRendererText()
            r.set_property("font", "Monospace 9")
            c = Gtk.TreeViewColumn(col, r, text=i)
            c.set_fixed_width(w)
            tv.append_column(c)
        self._tv = tv
        self._tv_adj = None

        tv_scroll = Gtk.ScrolledWindow()
        tv_scroll.set_child(tv)
        tv_scroll.set_vexpand(True)
        self._tv_adj = tv_scroll.get_vadjustment()
        tick_group.add(tv_scroll)

        box.append(event_group)
        box.append(tick_group)
        return box

    # ─── Data refresh ──────────────────────────────────────────────────────

    def _start_refresh(self):
        threading.Thread(target=self._do_refresh, daemon=True).start()
        GLib.timeout_add(3000, self._periodic)

    def _periodic(self):
        threading.Thread(target=self._do_refresh, daemon=True).start()
        return True

    def _do_refresh(self):
        try:
            bal = bybit_get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
            pos = bybit_get("/v5/position/list", {"category": "linear", "settleCoin": "USDT"})
            GLib.idle_add(self._update_ui, bal, pos)
        except Exception as e:
            GLib.idle_add(self._set_status, f"Error: {e}")

    def _update_ui(self, bal, pos):
        self._tick += 1
        ts = time.strftime("%H:%M:%S")

        # ── Balance ────────────────────────────────────────────────────────
        eq = float(bal["result"]["list"][0]["totalEquity"])
        usdt_coin = next(
            (c for c in bal["result"]["list"][0]["coin"] if c["coin"] == "USDT"), None
        )
        usdt_eq  = float(usdt_coin["equity"])         if usdt_coin else 0.0
        usdt_im  = float(usdt_coin["totalPositionIM"]) if usdt_coin else 0.0
        avail    = max(0.0, usdt_eq - usdt_im)

        self._eq_lbl.set_label(f"${eq:.5f}")
        self._usdt_lbl.set_label(f"avail ${avail:.5f}")

        open_pos = [p for p in pos["result"]["list"] if float(p.get("size", 0)) > 0]

        # ── Overview tab positions ─────────────────────────────────────────
        while child := self._pos_box.get_first_child():
            self._pos_box.remove(child)

        if not open_pos:
            lbl = Gtk.Label(label="Sin posiciones abiertas")
            lbl.set_css_classes(["dim-label"])
            lbl.set_margin_top(8); lbl.set_margin_bottom(8)
            self._pos_box.append(lbl)
        else:
            for p in open_pos:
                self._pos_box.append(self._make_pos_row(p))

        # ── Live Ticks tab ─────────────────────────────────────────────────
        for p in open_pos:
            sym   = p["symbol"]
            side  = "LONG" if p["side"] == "Buy" else "SHORT"
            mark  = float(p["markPrice"])
            pnl   = float(p["unrealisedPnl"])
            sl_p  = float(p["stopLoss"])  if p["stopLoss"]  else 0
            tp_p  = float(p["takeProfit"]) if p["takeProfit"] else 0
            sl_d  = abs(mark - sl_p) / mark * 100 if sl_p else 0
            tp_d  = abs(tp_p - mark) / mark * 100 if tp_p else 0
            color = "+" if pnl >= 0 else "-"

            self._log_store.prepend([
                ts, sym.replace("USDT", ""), side,
                f"${mark:.5f}",
                f"${pnl:+.4f}",
                f"SL-{sl_d:.2f}%  TP+{tp_d:.2f}%",
            ])

            # Detect events to show in event log
            self._detect_event(ts, sym, side, mark, pnl, sl_p, tp_p, sl_d, tp_d, p)

        # Trim table to 300 rows
        while len(self._log_store) > 300:
            it = self._log_store.get_iter_first()
            if it:
                self._log_store.remove(it)

        self._set_status(f"Tick #{self._tick}  •  {ts}  •  {len(open_pos)} posición(es)  •  Equity ${eq:.5f}")

    # ── Event detection ────────────────────────────────────────────────────

    _prev_sl: dict = {}   # sym → last known SL
    _prev_tp: dict = {}   # sym → last known TP
    _near_tp_warned: set = set()

    def _detect_event(self, ts, sym, side, mark, pnl, sl_p, tp_p, sl_d, tp_d, raw):
        lines = []

        # SL moved (trail or BE)
        prev_sl = self._prev_sl.get(sym)
        if prev_sl is not None and sl_p != prev_sl and sl_p > 0:
            entry = float(raw.get("avgPrice", mark))
            if side == "LONG":
                be = sl_p >= entry * 0.999
                label = "🔒 SL→BE" if be else "📈 trail SL"
            else:
                be = sl_p <= entry * 1.001
                label = "🔒 SL→BE" if be else "📈 trail SL"
            lines.append(f"[{ts}] {label}  {sym.replace('USDT','')} {side}  ${sl_p:.6f}  pnl={pnl:+.4f}")
        self._prev_sl[sym] = sl_p

        # Near TP warning (≤ 0.30%)
        if tp_d <= 0.30 and sym not in self._near_tp_warned:
            lines.append(f"[{ts}] 💰 CERCA TP  {sym.replace('USDT','')} {side}  TP a {tp_d:.2f}%  pnl={pnl:+.4f}")
            self._near_tp_warned.add(sym)
        elif tp_d > 0.50 and sym in self._near_tp_warned:
            self._near_tp_warned.discard(sym)

        for line in lines:
            end = self._event_buf.get_end_iter()
            self._event_buf.insert(end, line + "\n")
            # Auto-scroll
            GLib.idle_add(self._scroll_events)

    def _scroll_events(self):
        self._ev_adj.set_value(self._ev_adj.get_upper())

    # ── Position row ────────────────────────────────────────────────────────

    def _make_pos_row(self, p):
        sym   = p["symbol"]
        side  = p["side"]
        size  = p["size"]
        entry = float(p["avgPrice"])
        mark  = float(p["markPrice"])
        pnl   = float(p["unrealisedPnl"])
        liq   = p.get("liqPrice", "—")
        sl_p  = p.get("stopLoss", "—")
        tp_p  = p.get("takeProfit", "—")
        is_profit = pnl >= 0
        pct = (mark - entry) / entry * 100 if side == "Buy" else (entry - mark) / entry * 100

        sl_v = float(sl_p) if sl_p and sl_p != "—" else 0
        tp_v = float(tp_p) if tp_p and tp_p != "—" else 0
        sl_d = abs(mark - sl_v) / mark * 100 if sl_v else 0
        tp_d = abs(tp_v - mark) / mark * 100 if tp_v else 0

        row = Adw.ActionRow()
        row.set_title(f"{sym}  {'LONG' if side == 'Buy' else 'SHORT'}  qty={size}")
        row.set_subtitle(
            f"Entry: ${entry}  •  Liq: ${liq}  •  "
            f"SL: {sl_p} (-{sl_d:.2f}%)  •  TP: {tp_p} (+{tp_d:.2f}%)"
        )

        mark_lbl = Gtk.Label(label=f"${mark:.5f}")
        mark_lbl.set_css_classes(["monospace", "title-4"])
        mark_lbl.set_valign(Gtk.Align.CENTER)
        mark_lbl.set_margin_end(12)

        pnl_lbl = Gtk.Label(label=f"${pnl:+.4f}  ({pct:+.2f}%)")
        pnl_lbl.set_css_classes(["success" if is_profit else "error"])
        pnl_lbl.set_valign(Gtk.Align.CENTER)

        row.add_suffix(mark_lbl)
        row.add_suffix(pnl_lbl)
        return row

    def _set_status(self, msg):
        self._status_lbl.set_label(msg)


def main():
    app = TradeMonitorApp()
    app.run(sys.argv)


if __name__ == "__main__":
    main()
