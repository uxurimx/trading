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
    recv = "5000"
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
        self.set_default_size(680, 520)

        self._data = {}
        self._history = []   # last N ticks for chart-like display
        self._tick = 0

        self._build_ui()
        self._start_refresh()

    # ─── UI ────────────────────────────────────────────────────────────────

    def _build_ui(self):
        header = Adw.HeaderBar()
        refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh_btn.connect("clicked", lambda _: self._do_refresh())
        header.pack_end(refresh_btn)

        self._status_lbl = Gtk.Label(label="Conectando…")
        self._status_lbl.set_css_classes(["caption", "dim-label"])
        header.set_title_widget(self._status_lbl)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        root.append(header)

        # Top: balance bar
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

        # Middle: positions list
        pos_group = Adw.PreferencesGroup(title="Posiciones abiertas")
        self._pos_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        pos_group.add(self._pos_box)

        # Bottom: ticker log
        log_group = Adw.PreferencesGroup(title="Historial de ticks")
        self._log_store = Gtk.ListStore(str, str, str, str, str)  # tick, color, price, pnl, dist
        tv = Gtk.TreeView(model=self._log_store, headers_visible=True)
        tv.set_vexpand(True)

        for i, (col, w) in enumerate([("Tick", 50), ("", 30), ("Precio", 100), ("PnL", 100), ("SL/TP", 160)]):
            renderer = Gtk.CellRendererText()
            renderer.set_property("font", "Monospace 9")
            col_obj = Gtk.TreeViewColumn(col, renderer, text=i)
            col_obj.set_fixed_width(w)
            tv.append_column(col_obj)

        scroll = Gtk.ScrolledWindow()
        scroll.set_child(tv)
        scroll.set_min_content_height(200)
        self._tv = tv
        log_group.add(scroll)

        self._adj = scroll.get_vadjustment()

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.set_margin_top(12); content.set_margin_bottom(12)
        content.set_margin_start(12); content.set_margin_end(12)
        content.append(top_group)
        content.append(pos_group)
        content.append(log_group)

        scroll_outer = Gtk.ScrolledWindow()
        scroll_outer.set_child(content)
        scroll_outer.set_vexpand(True)
        root.append(scroll_outer)

        self.set_content(root)

    # ─── Data refresh ──────────────────────────────────────────────────────

    def _start_refresh(self):
        self._do_refresh()
        GLib.timeout_add(4000, self._periodic)

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
        eq = float(bal["result"]["list"][0]["totalEquity"])
        usdt = next(
            (float(c["walletBalance"]) for c in bal["result"]["list"][0]["coin"] if c["coin"] == "USDT"),
            0.0,
        )
        self._eq_lbl.set_label(f"${eq:.5f}")
        self._usdt_lbl.set_label(f"USDT: ${usdt:.5f}")

        open_pos = [p for p in pos["result"]["list"] if float(p.get("size", 0)) > 0]

        # Clear positions box
        while child := self._pos_box.get_first_child():
            self._pos_box.remove(child)

        if not open_pos:
            lbl = Gtk.Label(label="Sin posiciones abiertas")
            lbl.set_css_classes(["dim-label"])
            lbl.set_margin_top(8); lbl.set_margin_bottom(8)
            self._pos_box.append(lbl)
        else:
            for p in open_pos:
                row = self._make_pos_row(p)
                self._pos_box.append(row)
                # Add to tick log
                mark = float(p["markPrice"])
                pnl = float(p["unrealisedPnl"])
                sl_p = float(p["stopLoss"]) if p["stopLoss"] else 0
                tp_p = float(p["takeProfit"]) if p["takeProfit"] else 0
                sl_d = abs(mark - sl_p) / mark * 100 if sl_p else 0
                tp_d = abs(tp_p - mark) / mark * 100 if tp_p else 0
                color = "🟢" if pnl >= 0 else "🔴"
                self._log_store.prepend([
                    str(self._tick), color,
                    f"${mark:.5f}", f"${pnl:+.4f}",
                    f"SL-{sl_d:.2f}%  TP+{tp_d:.2f}%",
                ])
                if len(self._log_store) > 200:
                    it = self._log_store.get_iter_first()
                    if it: self._log_store.remove(it)

        self._set_status(f"Tick #{self._tick}  •  {time.strftime('%H:%M:%S')}")

    def _make_pos_row(self, p):
        sym = p["symbol"]
        side = p["side"]
        size = p["size"]
        entry = float(p["avgPrice"])
        mark = float(p["markPrice"])
        pnl = float(p["unrealisedPnl"])
        liq = p.get("liqPrice", "—")
        sl_p = p.get("stopLoss", "—")
        tp_p = p.get("takeProfit", "—")

        is_profit = pnl >= 0
        pct = (mark - entry) / entry * 100 if side == "Buy" else (entry - mark) / entry * 100

        row = Adw.ActionRow()
        row.set_title(f"{sym}  {'LONG' if side == 'Buy' else 'SHORT'}  {size}")
        row.set_subtitle(f"Entry: ${entry}  •  Liq: ${liq}  •  SL: {sl_p}  •  TP: {tp_p}")

        pnl_lbl = Gtk.Label(label=f"${pnl:+.4f}  ({pct:+.2f}%)")
        pnl_lbl.set_css_classes(["success" if is_profit else "error"])
        pnl_lbl.set_valign(Gtk.Align.CENTER)

        mark_lbl = Gtk.Label(label=f"${mark:.5f}")
        mark_lbl.set_css_classes(["monospace", "title-4"])
        mark_lbl.set_valign(Gtk.Align.CENTER)
        mark_lbl.set_margin_end(12)

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
