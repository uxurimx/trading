#!/usr/bin/env python3
"""
QTS Fast Auto-Trader — WebSocket multi-pair scanner with automatic execution.
Reacts in <1 second when entry conditions align on 1m + 3m simultaneously.

Usage:
    python scripts/fast_auto_trader.py
    python scripts/fast_auto_trader.py --dry-run        # no real orders
    python scripts/fast_auto_trader.py --symbols XRP,SOL,NEAR
"""
import asyncio
import json
import hmac
import hashlib
import time
import urllib.parse
import urllib.request
import argparse
import sys
import os
from collections import deque
from dataclasses import dataclass, field
from typing import Optional
import websockets

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from core.config import settings
    API_KEY    = settings.bybit_api_key
    API_SECRET = settings.bybit_api_secret
    PAPER_MODE = settings.paper_trading
except Exception:
    API_KEY    = os.getenv("BYBIT_API_KEY", "")
    API_SECRET = os.getenv("BYBIT_API_SECRET", "")
    PAPER_MODE = os.getenv("PAPER_TRADING", "false").lower() == "true"

# ─── STRATEGY CONFIG ───────────────────────────────────────────────────────
DEFAULT_SYMBOLS = [
    "XRPUSDT", "DOGEUSDT", "ADAUSDT", "DOTUSDT", "NEARUSDT",
    "INJUSDT", "LTCUSDT", "XLMUSDT", "ALGOUSDT", "VETUSDT",
    "HBARUSDT", "ATOMUSDT", "LINKUSDT", "TRXUSDT", "FILUSDT",
]

MIN_STREAK   = 2      # consecutive candles aligned
MIN_M3_PCT   = 0.15   # 3-candle momentum %
SL_PCT       = 0.005  # 0.5%
TP_PCT       = 0.010  # 1.0%   → R:R 2:1
LEVERAGE     = 20     # higher leverage → smaller margin needed per trade
MAX_POSITIONS = 2     # max simultaneous open positions
MIN_NOTIONAL = 5.5    # USD (Bybit min is 5)
COOLDOWN_S   = 60     # seconds between trades on same symbol
BE_TRIGGER   = 0.5    # move SL to BE when PnL reaches 50% of SL risk

WS_PUBLIC  = "wss://stream.bybit.com/v5/public/linear"
WS_PRIVATE = "wss://stream.bybit.com/v5/private"

# ─── GLOBALS ───────────────────────────────────────────────────────────────
active_positions: dict[str, dict] = {}   # keyed by symbol
position_lock    = asyncio.Lock()
last_trade_time: dict[str, float] = {}
last_signal_time: dict[str, float] = {}

# ─── REST HELPERS ──────────────────────────────────────────────────────────
def rest_get(endpoint: str, params: dict = {}) -> dict:
    ts    = str(int(time.time() * 1000))
    recv  = "5000"
    q     = urllib.parse.urlencode(params)
    pre   = ts + API_KEY + recv + q
    sig   = hmac.new(API_SECRET.encode(), pre.encode(), hashlib.sha256).hexdigest()
    heads = {"X-BAPI-API-KEY": API_KEY, "X-BAPI-TIMESTAMP": ts,
             "X-BAPI-RECV-WINDOW": recv, "X-BAPI-SIGN": sig}
    req = urllib.request.Request(f"https://api.bybit.com{endpoint}?{q}", headers=heads)
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read())


def rest_post(endpoint: str, body: dict) -> dict:
    ts        = str(int(time.time() * 1000))
    recv      = "5000"
    body_str  = json.dumps(body)
    pre       = ts + API_KEY + recv + body_str
    sig       = hmac.new(API_SECRET.encode(), pre.encode(), hashlib.sha256).hexdigest()
    heads = {"X-BAPI-API-KEY": API_KEY, "X-BAPI-TIMESTAMP": ts,
             "X-BAPI-RECV-WINDOW": recv, "X-BAPI-SIGN": sig,
             "Content-Type": "application/json"}
    req = urllib.request.Request(
        f"https://api.bybit.com{endpoint}",
        data=body_str.encode(), headers=heads, method="POST"
    )
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read())


def get_balance() -> tuple[float, float]:
    """Returns (total_equity, available_margin).
    Available = USDT equity - initial margin already in use.
    """
    try:
        d = rest_get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
        info = d["result"]["list"][0]
        equity = float(info["totalEquity"])
        usdt = next((c for c in info["coin"] if c["coin"] == "USDT"), None)
        if usdt:
            u_eq  = float(usdt["equity"])
            u_im  = float(usdt["totalPositionIM"])
            avail = max(0.0, u_eq - u_im)
        else:
            avail = equity * 0.4   # conservative fallback
        return equity, avail
    except Exception as e:
        print(f"   balance err: {e}")
        return 0.0, 0.0


def get_open_position(symbol: str) -> Optional[dict]:
    try:
        d = rest_get("/v5/position/list", {"category": "linear", "symbol": symbol})
        for p in d["result"]["list"]:
            if float(p.get("size", 0)) > 0:
                return p
    except Exception:
        pass
    return None


def has_any_open_position() -> bool:
    global _pos_cache
    now = time.time()
    if now - _pos_cache["ts"] < 5.0:
        return _pos_cache["result"]
    try:
        d = rest_get("/v5/position/list", {"category": "linear", "settleCoin": "USDT"})
        result = any(float(p.get("size", 0)) > 0 for p in d["result"]["list"])
        _pos_cache = {"result": result, "ts": now}
        return result
    except Exception:
        return True   # safe assumption


# ─── PER-SYMBOL STATE ──────────────────────────────────────────────────────
@dataclass
class PairState:
    symbol: str
    closes_1m: deque = field(default_factory=lambda: deque(maxlen=30))
    closes_3m: deque = field(default_factory=lambda: deque(maxlen=20))
    vols_1m:   deque = field(default_factory=lambda: deque(maxlen=20))
    vols_3m:   deque = field(default_factory=lambda: deque(maxlen=20))
    ts_1m: int = 0   # timestamp of last confirmed 1m candle
    ts_3m: int = 0   # timestamp of last confirmed 3m candle
    live_price: float = 0.0
    live_vol_1m: float = 0.0
    signal_count: int = 0

    # ── EMA ─────────────────────────────────────────────────────────────
    def _ema(self, seq: deque, n: int) -> float:
        lst = list(seq)
        if not lst:
            return 0.0
        k = 2 / (n + 1)
        e = lst[0]
        for p in lst[1:]:
            e = p * k + e * (1 - k)
        return e

    # ── Streak of last confirmed candles ────────────────────────────────
    def _streak(self, seq: deque) -> int:
        lst = list(seq)
        if len(lst) < 2:
            return 0
        d = 1 if lst[-1] > lst[-2] else -1
        s = 0
        for i in range(len(lst) - 1, 0, -1):
            if (lst[i] > lst[i - 1]) == (d == 1):
                s += 1
            else:
                break
        return s * d

    # ── Main signal check ───────────────────────────────────────────────
    def check_signal(self) -> Optional[dict]:
        if len(self.closes_1m) < 12 or len(self.closes_3m) < 6:
            return None

        cl1 = list(self.closes_1m)
        cl3 = list(self.closes_3m)
        vl1 = list(self.vols_1m)

        e9_1  = self._ema(self.closes_1m, 9)
        e21_1 = self._ema(self.closes_1m, 21)
        e9_3  = self._ema(self.closes_3m, 9)
        e21_3 = self._ema(self.closes_3m, 21)

        t1 = "L" if e9_1 > e21_1 else "S"
        t3 = "L" if e9_3 > e21_3 else "S"

        if t1 != t3:
            return None

        stk = self._streak(self.closes_1m)
        price = self.live_price or cl1[-1]
        m3  = (cl1[-1] - cl1[-4]) / cl1[-4] * 100 if len(cl1) >= 4 else 0.0

        avg_v = sum(vl1[-12:-2]) / 10 if len(vl1) >= 12 else 0
        rvol  = (self.live_vol_1m / avg_v) if avg_v > 0 else 1.0

        bias = "LONG" if t1 == "L" else "SHORT"
        m3_ok  = (m3 > MIN_M3_PCT)  if bias == "LONG"  else (m3 < -MIN_M3_PCT)
        stk_ok = (stk >= MIN_STREAK) if bias == "LONG"  else (stk <= -MIN_STREAK)

        if m3_ok and stk_ok:
            return {
                "bias": bias, "price": price,
                "m3": round(m3, 3), "streak": stk,
                "rvol": round(rvol, 2),
                "e9_1": round(e9_1, 6), "e21_1": round(e21_1, 6),
                "e9_3": round(e9_3, 6), "e21_3": round(e21_3, 6),
            }
        return None


# ─── STATE REGISTRY ────────────────────────────────────────────────────────
pair_states: dict[str, PairState] = {}


# ─── ORDER EXECUTION ───────────────────────────────────────────────────────
async def execute_entry(sym: str, sig: dict, avail: float, dry_run: bool) -> bool:
    global active_positions

    bias  = sig["bias"]
    price = sig["price"]

    sl = round(price * (1 - SL_PCT) if bias == "LONG" else price * (1 + SL_PCT), 6)
    tp = round(price * (1 + TP_PCT) if bias == "LONG" else price * (1 - TP_PCT), 6)

    # Use available margin, cap at 80% of it per trade
    margin_to_use = avail * 0.80
    notional = max(MIN_NOTIONAL, margin_to_use * LEVERAGE)
    qty_raw  = notional / price
    qty      = max(1, int(qty_raw))

    actual_notional = qty * price
    if actual_notional < MIN_NOTIONAL:
        qty = int(MIN_NOTIONAL / price) + 1
        actual_notional = qty * price

    required_margin = actual_notional / LEVERAGE
    if required_margin > avail * 0.95:
        print(f"   ⏭  {sym} saltado — margen req ${required_margin:.3f} > disponible ${avail:.3f}")
        return False

    risk_usd = actual_notional * SL_PCT
    gain_usd = actual_notional * TP_PCT

    ts = time.strftime("%H:%M:%S")
    side_str = "LONG" if bias == "LONG" else "SHORT"
    print(f"\n{'='*60}")
    print(f"⚡ [{ts}] {sym} {side_str}  [{len(active_positions)+1}/{MAX_POSITIONS}]")
    print(f"   price=${price:.5f}  m3={sig['m3']:+.3f}%  stk={sig['streak']:+d}  rvol={sig['rvol']:.2f}x")
    print(f"   qty={qty}  notional=${actual_notional:.2f}  lev={LEVERAGE}x  avail=${avail:.4f}")
    print(f"   SL={sl}  TP={tp}  Riesgo=${risk_usd:.4f}  Potencial=${gain_usd:.4f}")

    if dry_run:
        print("   [DRY RUN — no se ejecuta]")
        return False

    try:
        rest_post("/v5/position/set-leverage", {
            "category": "linear", "symbol": sym,
            "buyLeverage": str(LEVERAGE), "sellLeverage": str(LEVERAGE),
        })

        order_side = "Buy" if bias == "LONG" else "Sell"
        pos_idx    = 1 if bias == "LONG" else 2

        resp = rest_post("/v5/order/create", {
            "category": "linear", "symbol": sym,
            "side": order_side, "orderType": "Market",
            "qty": str(qty), "positionIdx": pos_idx,
            "stopLoss": str(sl), "takeProfit": str(tp),
            "slTriggerBy": "LastPrice", "tpTriggerBy": "LastPrice",
            "timeInForce": "IOC",
        })

        if resp["retCode"] != 0:
            print(f"   ❌ Error orden: {resp['retMsg']}")
            return False

        order_id = resp["result"]["orderId"]
        await asyncio.sleep(1.5)

        pos = get_open_position(sym)
        if pos:
            entry = float(pos["avgPrice"])
            active_positions[sym] = {
                "symbol": sym, "side": bias,
                "entry": entry, "sl": float(pos["stopLoss"]),
                "tp": float(pos["takeProfit"]), "qty": float(pos["size"]),
                "order_id": order_id, "be_moved": False,
                "liq": pos.get("liqPrice", "?"),
            }
            print(f"   ✅ ABIERTA @ ${entry}  Liq=${pos.get('liqPrice','?')}")
            last_trade_time[sym] = time.time()
            return True
        else:
            print(f"   ⚠️  Orden enviada pero posición no encontrada")
            return False

    except Exception as e:
        print(f"   ❌ Exception: {e}")
        return False


async def monitor_position(dry_run: bool):
    """Polls all open positions, moves SL to BE, detects closes."""
    global active_positions

    if not active_positions:
        return

    for sym in list(active_positions.keys()):
        ap   = active_positions[sym]
        side = ap["side"]
        try:
            pos = get_open_position(sym)

            if not pos:
                ts = time.strftime("%H:%M:%S")
                print(f"\n[{ts}] ✅ {sym} CERRADA (TP/SL)")
                try:
                    hist = rest_get("/v5/execution/list", {"category": "linear", "symbol": sym, "limit": "5"})
                    for ex in hist["result"]["list"][:2]:
                        print(f"   {ex['side']} {ex['execQty']} @ ${ex['execPrice']}")
                    eq, avail = get_balance()
                    print(f"   💰 Equity=${eq:.5f}  Disponible=${avail:.5f}")
                except Exception:
                    pass
                del active_positions[sym]
                continue

            mark  = float(pos["markPrice"])
            pnl   = float(pos["unrealisedPnl"])
            entry = ap["entry"]
            sl    = float(pos["stopLoss"])
            tp    = float(pos["takeProfit"])

            sl_dist = abs(mark - sl) / mark * 100
            tp_dist = abs(tp - mark) / mark * 100
            color   = "🟢" if pnl >= 0 else "🔴"
            ts      = time.strftime("%H:%M:%S")
            print(f"[{ts}] {color} {sym} {side}  ${mark:.5f}  pnl=${pnl:+.4f}  SL-{sl_dist:.2f}%  TP+{tp_dist:.2f}%")

            if not ap["be_moved"] and not dry_run:
                risk = abs(entry - ap["sl"]) * ap["qty"]
                if pnl >= risk * BE_TRIGGER:
                    new_sl = round(entry * 1.001 if side == "LONG" else entry * 0.999, 6)
                    resp = rest_post("/v5/position/trading-stop", {
                        "category": "linear", "symbol": sym,
                        "positionIdx": 1 if side == "LONG" else 2,
                        "stopLoss": str(new_sl), "slTriggerBy": "LastPrice",
                    })
                    if resp["retCode"] == 0:
                        ap["be_moved"] = True
                        ap["sl"] = new_sl
                        print(f"   🔒 SL→BE: ${new_sl} (pnl=${pnl:+.4f})")

        except Exception as e:
            print(f"   monitor err {sym}: {e}")


# ─── WEBSOCKET HANDLER ─────────────────────────────────────────────────────
async def ws_handler(symbols: list[str], dry_run: bool):
    global active_positions

    # Build subscription topics: kline.1 + kline.3 for all symbols
    topics_1m = [f"kline.1.{s}"  for s in symbols]
    topics_3m = [f"kline.3.{s}"  for s in symbols]
    all_topics = topics_1m + topics_3m

    # Initialize state
    for sym in symbols:
        pair_states[sym] = PairState(symbol=sym)

    print(f"\n{'='*60}")
    print(f"QTS Fast Auto-Trader {'[DRY RUN]' if dry_run else '[LIVE]'}")
    print(f"Símbolos: {len(symbols)}  |  SL={SL_PCT*100:.1f}%  TP={TP_PCT*100:.1f}%  R:R 2:1")
    print(f"Condiciones: 1m+3m alineados  stk≥{MIN_STREAK}  m3≥{MIN_M3_PCT}%")
    print(f"{'='*60}")

    # Pre-load historical candles via REST to warm up EMAs
    print("Cargando historial para warm-up EMA...")
    for sym in symbols:
        try:
            st = pair_states[sym]
            for tf, cl_attr, vl_attr in [("1", "closes_1m", "vols_1m"), ("3", "closes_3m", "vols_3m")]:
                d = rest_get("/v5/market/kline", {
                    "category": "linear", "symbol": sym, "interval": tf, "limit": "30"
                })
                candles = list(reversed(d["result"]["list"]))
                for c in candles:
                    getattr(st, cl_attr).append(float(c[4]))   # close
                    getattr(st, vl_attr).append(float(c[5]))   # volume
        except Exception as e:
            print(f"  {sym} warm-up err: {e}")
    print(f"Warm-up completo. Conectando WebSocket...\n")

    # Pre-load existing open positions so we don't double-enter
    try:
        d = rest_get("/v5/position/list", {"category": "linear", "settleCoin": "USDT"})
        for p in d["result"]["list"]:
            if float(p.get("size", 0)) > 0:
                sym2 = p["symbol"]
                side2 = "LONG" if p["side"] == "Buy" else "SHORT"
                active_positions[sym2] = {
                    "symbol": sym2, "side": side2,
                    "entry": float(p["avgPrice"]),
                    "sl": float(p["stopLoss"]) if p["stopLoss"] else 0,
                    "tp": float(p["takeProfit"]) if p["takeProfit"] else 0,
                    "qty": float(p["size"]), "be_moved": False, "liq": p.get("liqPrice", "?"),
                }
                print(f"  📍 Posición existente cargada: {sym2} {side2} @ ${p['avgPrice']}  SL={p['stopLoss']}  TP={p['takeProfit']}")
    except Exception as e:
        print(f"  Pre-load posiciones err: {e}")

    reconnect_delay = 3
    while True:
        try:
            async with websockets.connect(WS_PUBLIC, ping_interval=20, ping_timeout=10) as ws:
                # Subscribe in chunks of 10
                for i in range(0, len(all_topics), 10):
                    chunk = all_topics[i:i+10]
                    await ws.send(json.dumps({"op": "subscribe", "args": chunk}))
                    await asyncio.sleep(0.1)

                print(f"[{time.strftime('%H:%M:%S')}] ✅ WebSocket conectado — {len(symbols)} pares activos\n")
                reconnect_delay = 3

                last_monitor = time.time()
                last_scan_report = time.time()

                async for raw_msg in ws:
                    msg = json.loads(raw_msg)

                    # Position monitor every 5 seconds
                    now = time.time()
                    if now - last_monitor >= 5 and active_positions:
                        await monitor_position(dry_run)
                        last_monitor = now

                    # Print alive status every 30 seconds
                    if now - last_scan_report >= 30:
                        eq, avail = get_balance()
                        n = len(active_positions)
                        print(f"[{time.strftime('%H:%M:%S')}] 👁 {n}/{MAX_POSITIONS} pos  Equity=${eq:.5f}  Disponible=${avail:.5f}")
                        # Show top candidates
                        candidates = []
                        for s, st2 in pair_states.items():
                            if len(st2.closes_1m) < 12 or len(st2.closes_3m) < 6:
                                continue
                            e9_1  = st2._ema(st2.closes_1m, 9)
                            e21_1 = st2._ema(st2.closes_1m, 21)
                            e9_3  = st2._ema(st2.closes_3m, 9)
                            e21_3 = st2._ema(st2.closes_3m, 21)
                            t1 = "L" if e9_1 > e21_1 else "S"
                            t3 = "L" if e9_3 > e21_3 else "S"
                            cl1 = list(st2.closes_1m)
                            m3 = (cl1[-1] - cl1[-4]) / cl1[-4] * 100 if len(cl1) >= 4 else 0
                            stk = st2._streak(st2.closes_1m)
                            aligned = "✓" if t1 == t3 else "✗"
                            candidates.append((s.replace("USDT",""), aligned, t1, t3, stk, m3))
                        top = sorted(candidates, key=lambda x: abs(x[5]), reverse=True)[:5]
                        for c in top:
                            tag = "📍" if c[0]+"USDT" in active_positions else "  "
                            print(f" {tag}{c[0]:8s} {c[1]} 1m:{c[2]} 3m:{c[3]}  stk={c[4]:+d}  m3={c[5]:+.3f}%")
                        last_scan_report = now

                    # Skip non-kline or pong messages
                    if "topic" not in msg:
                        continue

                    topic = msg["topic"]
                    # topic format: "kline.1.SYMBOL" or "kline.3.SYMBOL"
                    parts = topic.split(".")
                    if len(parts) < 3 or parts[0] != "kline":
                        continue

                    tf  = parts[1]   # "1" or "3"
                    sym = parts[2]   # "XRPUSDT" etc

                    if sym not in pair_states:
                        continue

                    st = pair_states[sym]
                    data = msg["data"]
                    if not data:
                        continue

                    candle = data[0]
                    close  = float(candle["close"])
                    vol    = float(candle["volume"])
                    confirmed = candle.get("confirm", False)

                    # Always update live price from 1m
                    if tf == "1":
                        st.live_price   = close
                        st.live_vol_1m  = vol

                    # On confirmed close: append to history
                    if confirmed:
                        ts_candle = int(candle["start"])
                        if tf == "1" and ts_candle != st.ts_1m:
                            st.closes_1m.append(close)
                            st.vols_1m.append(vol)
                            st.ts_1m = ts_candle
                        elif tf == "3" and ts_candle != st.ts_3m:
                            st.closes_3m.append(close)
                            st.vols_3m.append(vol)
                            st.ts_3m = ts_candle

                    # Check signal only on confirmed candles to avoid tick spam
                    if not confirmed:
                        continue

                    # Skip if already have a position on this symbol
                    if sym in active_positions:
                        continue

                    # Skip if at max simultaneous positions
                    if len(active_positions) >= MAX_POSITIONS:
                        continue

                    # Cooldown per symbol (trade + signal)
                    if now - last_trade_time.get(sym, 0) < COOLDOWN_S:
                        continue
                    if now - last_signal_time.get(sym, 0) < 10.0:
                        continue

                    sig = st.check_signal()
                    if not sig:
                        continue

                    last_signal_time[sym] = now
                    print(f"[{time.strftime('%H:%M:%S')}] 📡 SEÑAL {sym} {sig['bias']}  m3={sig['m3']:+.3f}%  stk={sig['streak']:+d}  p=${sig['price']:.5f}")

                    async with position_lock:
                        if sym in active_positions:
                            continue
                        if len(active_positions) >= MAX_POSITIONS:
                            print(f"   ⏸  Máximo de posiciones alcanzado ({MAX_POSITIONS})")
                            continue

                        eq, avail = get_balance()
                        if avail < 0.3:
                            print(f"   ⚠️  Margen disponible insuficiente: ${avail:.4f}")
                            continue

                        ok = await execute_entry(sym, sig, avail, dry_run)
                        if ok:
                            last_monitor = time.time()

        except websockets.exceptions.ConnectionClosed as e:
            print(f"\n⚠️  WS desconectado: {e}. Reconectando en {reconnect_delay}s...")
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 60)
        except Exception as e:
            print(f"\n❌ Error WS: {e}. Reconectando en {reconnect_delay}s...")
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 60)


# ─── ENTRY POINT ───────────────────────────────────────────────────────────
async def main():
    global SL_PCT, TP_PCT, MIN_STREAK
    parser = argparse.ArgumentParser(description="QTS Fast Auto-Trader")
    parser.add_argument("--dry-run",  action="store_true", help="No ejecutar órdenes reales")
    parser.add_argument("--symbols",  type=str,  help="Comma-separated list, ej: XRP,SOL,NEAR")
    parser.add_argument("--sl",       type=float, default=SL_PCT * 100, help="SL%% (default 0.5)")
    parser.add_argument("--tp",       type=float, default=TP_PCT * 100, help="TP%% (default 1.0)")
    parser.add_argument("--streak",   type=int,   default=MIN_STREAK,   help="Min streak (default 2)")
    args = parser.parse_args()

    SL_PCT     = args.sl / 100
    TP_PCT     = args.tp / 100
    MIN_STREAK = args.streak

    if args.symbols:
        syms = [s.strip().upper() for s in args.symbols.split(",")]
        syms = [s if s.endswith("USDT") else s + "USDT" for s in syms]
    else:
        syms = DEFAULT_SYMBOLS

    if PAPER_MODE and not args.dry_run:
        print("⚠️  PAPER_TRADING=true detectado — corriendo en modo dry-run")
        args.dry_run = True

    try:
        await ws_handler(syms, dry_run=args.dry_run)
    except KeyboardInterrupt:
        print("\n\nDetenido por usuario.")


if __name__ == "__main__":
    asyncio.run(main())
