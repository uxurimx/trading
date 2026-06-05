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
import math
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
import psycopg2
import psycopg2.extras
from datetime import datetime, timezone

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
# Pares con 0% WR en historial — eliminados: LINK(-$0.17), AVAX(-$0.10), HBAR(-$0.05), DOT(-$0.04), FIL(-$0.03)
# ADA: 75% WR +$0.06 (el único consistente). Enfoque en líquidos de alto volumen.
DEFAULT_SYMBOLS = [
    "XRPUSDT", "ADAUSDT", "NEARUSDT",
    "INJUSDT", "XLMUSDT", "ALGOUSDT",
    "ATOMUSDT", "TRXUSDT", "DOGEUSDT",
]

MIN_STREAK   = 3      # mínimo 3 velas — 2 era ruido puro (30% WR)
MAX_STREAK   = 7      # máximo razonable para no perseguir
MIN_M3_PCT   = 0.18   # 0.12% generaba falsas señales — subido
SL_PCT       = 0.002  # 0.2% SL → R:R real 2:1 con TP 0.4%
TP_PCT       = 0.004  # 0.4% TP — 30-60s con 50x leverage
LEVERAGE     = 50     # 50x
MAX_POSITIONS = 2
MIN_NOTIONAL = 5.0
COOLDOWN_S   = 15     # 15s cooldown — evita re-entrar en ruido
BE_TRIGGER   = 0.4    # BE al 40% del riesgo
MIN_RVOL     = 0.8    # exige más volumen relativo
TRAIL_TIGHT  = 0.001  # 0.1% trail post-BE
TRAIL_LOOSE  = 0.002
MIN_DIVERGE  = 0.15   # divergencia mínima vs BTC restaurada

WS_PUBLIC  = "wss://stream.bybit.com/v5/public/linear"
WS_PRIVATE = "wss://stream.bybit.com/v5/private"

PG_DSN = "postgresql://dev@/trading?host=/var/run/postgresql"

# ─── POSTGRES LOGGER ───────────────────────────────────────────────────────
_pg_conn = None

def _pg():
    global _pg_conn
    try:
        if _pg_conn is None or _pg_conn.closed:
            _pg_conn = psycopg2.connect(PG_DSN)
            _pg_conn.autocommit = True
    except Exception as e:
        print(f"[PG] conexión fallida: {e}")
        _pg_conn = None
    return _pg_conn

def pg_log_trade_open(sym, side, entry, qty, notional, leverage, sl, tp, sl_label, tp_label, rr, sig):
    """Inserta fila de trade abierto — sin pnl ni close_reason todavía."""
    try:
        con = _pg()
        if not con: return None
        cur = con.cursor()
        cur.execute("""
            INSERT INTO qts_trades
              (symbol, side, entry_price, qty, notional, leverage,
               sl_price, tp_price, sl_label, tp_label, rr,
               stk, m3, rvol, divergence, atr5, htf_info, opened_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
        """, (
            sym, side, entry, qty, notional, leverage,
            sl, tp, sl_label, tp_label, rr,
            sig.get("streak"), sig.get("m3"), sig.get("rvol"),
            sig.get("divergence"), sig.get("atr5"), sig.get("htf"),
            datetime.now(timezone.utc),
        ))
        row = cur.fetchone()
        return row[0] if row else None
    except Exception as e:
        print(f"[PG] log_trade_open err: {e}")
        return None

def pg_log_trade_close(trade_id, exit_price, pnl, close_reason, be_moved, duration_s):
    try:
        con = _pg()
        if not con: return
        cur = con.cursor()
        cur.execute("""
            UPDATE qts_trades SET
              exit_price=%s, pnl=%s, close_reason=%s,
              be_moved=%s, duration_s=%s, closed_at=%s
            WHERE id=%s
        """, (exit_price, pnl, close_reason, be_moved, duration_s,
              datetime.now(timezone.utc), trade_id))
    except Exception as e:
        print(f"[PG] log_trade_close err: {e}")

def pg_log_tick(sym, side, mark, pnl, sl_dist, tp_dist, be_moved, event=None):
    try:
        con = _pg()
        if not con: return
        cur = con.cursor()
        cur.execute("""
            INSERT INTO qts_ticks (symbol, side, mark_price, pnl, sl_dist_pct, tp_dist_pct, be_moved, event)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        """, (sym, side, mark, pnl, sl_dist, tp_dist, be_moved, event))
    except Exception as e:
        print(f"[PG] log_tick err: {e}")

def pg_log_signal(sym, bias, sig, executed, reject_reason=None):
    try:
        con = _pg()
        if not con: return
        cur = con.cursor()
        cur.execute("""
            INSERT INTO qts_signals (symbol, bias, stk, m3, rvol, divergence, atr5, htf_info, executed, reject_reason)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            sym, bias,
            sig.get("streak"), sig.get("m3"), sig.get("rvol"),
            sig.get("divergence"), sig.get("atr5"), sig.get("htf"),
            executed, reject_reason,
        ))
    except Exception as e:
        print(f"[PG] log_signal err: {e}")

def pg_log_equity(equity, avail, open_pos):
    try:
        con = _pg()
        if not con: return
        cur = con.cursor()
        cur.execute("""
            INSERT INTO qts_equity (equity, avail, open_positions) VALUES (%s,%s,%s)
        """, (equity, avail, open_pos))
    except Exception as e:
        print(f"[PG] log_equity err: {e}")

# ─── GLOBALS ───────────────────────────────────────────────────────────────
active_positions: dict[str, dict] = {}   # keyed by symbol
position_lock    = asyncio.Lock()
last_trade_time: dict[str, float] = {}
last_signal_time: dict[str, float] = {}
btc_closes_1m: deque = deque(maxlen=10)  # BTC reference for beta-adjusted signals

# ─── REST HELPERS ──────────────────────────────────────────────────────────
def rest_get(endpoint: str, params: dict = {}) -> dict:
    ts    = str(int(time.time() * 1000))
    recv  = "10000"
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
    recv      = "10000"
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
    closes_1m:  deque = field(default_factory=lambda: deque(maxlen=60))
    closes_3m:  deque = field(default_factory=lambda: deque(maxlen=30))
    closes_15m: deque = field(default_factory=lambda: deque(maxlen=30))
    closes_1h:  deque = field(default_factory=lambda: deque(maxlen=24))
    vols_1m:    deque = field(default_factory=lambda: deque(maxlen=20))
    vols_3m:    deque = field(default_factory=lambda: deque(maxlen=20))
    ts_1m: int = 0
    ts_3m: int = 0
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

    # ── Higher timeframe trend ──────────────────────────────────────────
    def htf_bias(self) -> Optional[str]:
        """Returns '1h:L/S 15m:L/S' or None if not enough data."""
        parts = []
        if len(self.closes_1h) >= 9:
            t = "L" if self._ema(self.closes_1h, 9) > self._ema(self.closes_1h, min(21, len(self.closes_1h))) else "S"
            parts.append(f"1h:{t}")
        if len(self.closes_15m) >= 9:
            t = "L" if self._ema(self.closes_15m, 9) > self._ema(self.closes_15m, min(21, len(self.closes_15m))) else "S"
            parts.append(f"15m:{t}")
        return " ".join(parts) if parts else None

    def htf_allows(self, bias: str) -> tuple[bool, str]:
        """Returns (allowed, reason). Blocks entries against 1h trend."""
        h1_trend = None
        m15_trend = None
        if len(self.closes_1h) >= 9:
            h1_trend = "L" if self._ema(self.closes_1h, 9) > self._ema(self.closes_1h, min(21, len(self.closes_1h))) else "S"
        if len(self.closes_15m) >= 9:
            m15_trend = "L" if self._ema(self.closes_15m, 9) > self._ema(self.closes_15m, min(21, len(self.closes_15m))) else "S"

        expected = "L" if bias == "LONG" else "S"

        if h1_trend and h1_trend != expected:
            return False, f"1h={h1_trend} contra {bias} — tendencia real opuesta"
        if m15_trend and m15_trend != expected:
            return False, f"15m={m15_trend} contra {bias}"
        htf_str = f"1h={h1_trend or '?'} 15m={m15_trend or '?'}"
        return True, htf_str

    # ── Swing S/R detection from kline history ──────────────────────────
    def nearest_resistance(self, price: float, lookback: int = 4) -> Optional[float]:
        """Nearest swing high above price from 1m klines."""
        cl = list(self.closes_1m)
        n = len(cl)
        if n < lookback * 2 + 1:
            return None
        highs = []
        for i in range(lookback, n - lookback):
            if all(cl[i] >= cl[i-j] for j in range(1, lookback+1)) and \
               all(cl[i] >= cl[i+j] for j in range(1, lookback+1)):
                if cl[i] > price * 1.001:   # at least 0.1% above
                    highs.append(cl[i])
        # Also add nearest round number above
        mag = 10 ** (len(str(int(price))) - 2)
        rnd = math.ceil(price / mag) * mag
        if rnd > price * 1.001:
            highs.append(rnd)
        return min(highs) if highs else None

    def nearest_support(self, price: float, lookback: int = 4) -> Optional[float]:
        """Nearest swing low below price from 1m klines."""
        cl = list(self.closes_1m)
        n = len(cl)
        if n < lookback * 2 + 1:
            return None
        lows = []
        for i in range(lookback, n - lookback):
            if all(cl[i] <= cl[i-j] for j in range(1, lookback+1)) and \
               all(cl[i] <= cl[i+j] for j in range(1, lookback+1)):
                if cl[i] < price * 0.999:
                    lows.append(cl[i])
        mag = 10 ** (len(str(int(price))) - 2)
        rnd = math.floor(price / mag) * mag
        if rnd < price * 0.999:
            lows.append(rnd)
        return max(lows) if lows else None

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

        stk = self._streak(self.closes_1m)
        price = self.live_price or cl1[-1]
        m3  = (cl1[-1] - cl1[-4]) / cl1[-4] * 100 if len(cl1) >= 4 else 0.0

        avg_v = sum(vl1[-12:-2]) / 10 if len(vl1) >= 12 else 0
        rvol  = (self.live_vol_1m / avg_v) if avg_v > 0 else 1.0

        # ── Momentum override: strong consecutive closes ignoran EMA lag ─────
        # Si 3 velas seguidas bajan/suben >0.15% c/u con volumen alto → señal directa
        momentum_bias = None
        if len(cl1) >= 4:
            moves = [(cl1[i] - cl1[i-1]) / cl1[i-1] * 100 for i in range(-3, 0)]
            if all(m < -0.15 for m in moves) and rvol >= 1.2:
                momentum_bias = "SHORT"
            elif all(m > 0.15 for m in moves) and rvol >= 1.2:
                momentum_bias = "LONG"

        # EMA-based bias (requiere ambas TF alineadas)
        ema_bias = None
        if t1 == t3:
            ema_bias = "LONG" if t1 == "L" else "SHORT"

        # Usar momentum si EMA está rezagada (EMA contra momentum = lag)
        if momentum_bias and ema_bias and momentum_bias != ema_bias:
            bias = momentum_bias   # momentum override — EMA no ha catcheado
            signal_type = "MOMENTUM"
        elif ema_bias:
            bias = ema_bias
            signal_type = "EMA"
        else:
            return None   # ni EMA alineada ni momentum claro

        stk_abs = abs(stk)
        m3_ok   = (m3 > MIN_M3_PCT)   if bias == "LONG"  else (m3 < -MIN_M3_PCT)
        stk_ok  = stk_abs >= MIN_STREAK
        if stk_abs > MAX_STREAK:
            stk_ok = rvol >= 2.0 and abs(m3) >= 0.40
        rvol_ok = rvol >= MIN_RVOL

        # Momentum override tiene requisitos más altos (sin confirmación EMA)
        if signal_type == "MOMENTUM":
            if rvol < 1.5 or abs(m3) < 0.25:
                return None
        elif not (m3_ok and stk_ok and rvol_ok):
            return None

        # ── ATR check: symbol must have enough range to reach TP ────────────
        ranges = [abs(cl1[i] - cl1[i-1]) / cl1[i-1] * 100 for i in range(-5, 0)]
        atr5 = sum(ranges) / len(ranges) if ranges else 0
        if atr5 < 0.10:   # market sleeping — less than 0.10% avg candle
            return None

        # ── BTC trend filter (EMA9 vs EMA21 of last 10 BTC candles) ─────────
        btc_cl = list(btc_closes_1m)
        btc_bias = None
        if len(btc_cl) >= 10:
            btc_e9  = self._ema(deque(btc_cl, maxlen=len(btc_cl)), 9)
            btc_e21 = self._ema(deque(btc_cl, maxlen=len(btc_cl)), min(21, len(btc_cl)))
            btc_bias = "LONG" if btc_e9 > btc_e21 else "SHORT"

        # ── Beta-adjusted divergence vs BTC (3-candle window, not 1) ────────
        if len(btc_cl) >= 4 and self.symbol != "BTCUSDT":
            btc_return  = (btc_cl[-1] - btc_cl[-4]) / btc_cl[-4] * 100   # 3-candle BTC return
            sym_return  = (cl1[-1]    - cl1[-4])    / cl1[-4]    * 100
            divergence  = sym_return - btc_return
            div_ok = (divergence >= MIN_DIVERGE)  if bias == "LONG" \
                else (divergence <= -MIN_DIVERGE)
            # If BTC is moving against us, require stronger divergence
            if btc_bias and btc_bias != bias:
                div_ok = (divergence >= MIN_DIVERGE * 2) if bias == "LONG" \
                    else (divergence <= -MIN_DIVERGE * 2)
        else:
            divergence = 0.0
            div_ok     = True

        if not div_ok:
            return None

        # ── Higher timeframe filter — NEVER fight 1h or 15m trend ───────────
        htf_ok, htf_reason = self.htf_allows(bias)
        if not htf_ok:
            return None   # signal is against the real trend — hard block

        return {
            "bias": bias, "price": price,
            "m3": round(m3, 3), "streak": stk,
            "rvol": round(rvol, 2),
            "divergence": round(divergence, 3),
            "atr5": round(atr5, 3),
            "htf": htf_reason,
            "signal_type": signal_type,
            "e9_1": round(e9_1, 6), "e21_1": round(e21_1, 6),
            "e9_3": round(e9_3, 6), "e21_3": round(e21_3, 6),
        }


# ─── STATE REGISTRY ────────────────────────────────────────────────────────
pair_states: dict[str, PairState] = {}


# ─── ORDER EXECUTION ───────────────────────────────────────────────────────
async def execute_entry(sym: str, sig: dict, avail: float, dry_run: bool) -> bool:
    global active_positions

    bias  = sig["bias"]
    price = sig["price"]

    atr5 = sig.get("atr5", SL_PCT * 100)
    st   = pair_states.get(sym)

    # ── SL: structural swing level, not arbitrary % ──────────────────────
    # LONG: SL just below nearest swing LOW → structurally invalid if broken
    # SHORT: SL just above nearest swing HIGH → same logic
    # "Just below/above" = 0.1% buffer so we're NOT on the exact level
    # (sitting on the level = stop hunt bait)
    structural_sl = None
    if st:
        structural_sl = st.nearest_support(price) if bias == "LONG" else st.nearest_resistance(price)

    if structural_sl:
        # Place SL 0.15% beyond the structural level (not on it)
        if bias == "LONG":
            sl = round(structural_sl * 0.9985, 6)
        else:
            sl = round(structural_sl * 1.0015, 6)
        sl_pct = abs(price - sl) / price
        sl_label = "swing low/high"
    else:
        # No structural level found → ATR fallback, clamped
        sl_pct = max(0.005, min(0.012, atr5 / 100 * 1.5))
        sl = round(price * (1 - sl_pct) if bias == "LONG" else price * (1 + sl_pct), 6)
        sl_label = "ATR×1.5"

    # Sanity: SL must be between 0.3% and 2.0% from price
    sl_pct = abs(price - sl) / price
    if sl_pct < 0.003 or sl_pct > 0.020:
        sl_pct = max(0.005, min(0.012, atr5 / 100 * 1.5))
        sl = round(price * (1 - sl_pct) if bias == "LONG" else price * (1 + sl_pct), 6)
        sl_label = "ATR×1.5 (fallback)"

    # ── TP: just BEFORE nearest resistance/support — not on the level ────
    # Price often reverses at resistance without breaking it. Place TP
    # 0.1% before the level so we capture the move, not wait for the break.
    swing_tp = None
    if st:
        swing_tp = st.nearest_resistance(price) if bias == "LONG" else st.nearest_support(price)

    if swing_tp:
        # TP = 0.15% before the level (we exit before the wall, not into it)
        raw_tp = swing_tp * 0.9985 if bias == "LONG" else swing_tp * 1.0015
        tp_pct = abs(raw_tp - price) / price
        rr = tp_pct / sl_pct if sl_pct > 0 else 0
        if rr >= 1.5:
            tp = round(raw_tp, 6)
            tp_label = f"swing S/R (R:R {rr:.1f}:1)"
        else:
            tp_pct = sl_pct * 2
            tp = round(price * (1 + tp_pct) if bias == "LONG" else price * (1 - tp_pct), 6)
            tp_label = f"ATR×2 (swing R:R {rr:.1f} insuficiente)"
    else:
        tp_pct = sl_pct * 2
        tp = round(price * (1 + tp_pct) if bias == "LONG" else price * (1 - tp_pct), 6)
        tp_label = "ATR×2 (sin swing)"

    # Final R:R
    rr = abs(tp - price) / abs(price - sl) if abs(price - sl) > 0 else 0

    # Skip trade if R:R < 1.5 (structure doesn't support the trade)
    if rr < 1.5:
        print(f"   ⛔ {sym} {bias} rechazado: R:R={rr:.1f} < 1.5  SL={sl_pct*100:.2f}%  TP={tp_pct*100:.2f}%")
        return False

    # Risk-based sizing: risk 2% of available equity per trade
    # qty = risk_usd / distance_to_sl_per_unit
    eq, _ = get_balance()
    risk_usd   = max(0.02, eq * 0.02)          # 2% of equity, min $0.02
    sl_dist_pp = abs(price - sl)               # distance per unit in price
    qty_by_risk = risk_usd / sl_dist_pp if sl_dist_pp > 0 else 0
    notional_by_risk = qty_by_risk * price

    # Clamp to Bybit min notional and available margin
    notional = max(MIN_NOTIONAL, min(notional_by_risk, avail * 0.30 * LEVERAGE, MIN_NOTIONAL * 1.5))
    if notional < MIN_NOTIONAL:
        print(f"   ⏭  {sym} saltado — notional ${notional:.2f} < mínimo ${MIN_NOTIONAL}")
        return False

    qty_raw = notional / price
    qty     = max(1, int(qty_raw))

    actual_notional = qty * price
    # Ensure we still meet minimum after int rounding
    if actual_notional < MIN_NOTIONAL:
        qty = int(MIN_NOTIONAL / price) + 1
        actual_notional = qty * price

    required_margin = actual_notional / LEVERAGE
    if required_margin > avail:
        print(f"   ⏭  {sym} saltado — margen req ${required_margin:.3f} > disponible ${avail:.3f}")
        return False

    risk_usd = actual_notional * sl_pct
    gain_usd = actual_notional * tp_pct

    ts = time.strftime("%H:%M:%S")
    side_str = "LONG" if bias == "LONG" else "SHORT"
    print(f"\n{'='*60}")
    print(f"⚡ [{ts}] {sym} {side_str}  [{len(active_positions)+1}/{MAX_POSITIONS}]")
    print(f"   price=${price:.5f}  m3={sig['m3']:+.3f}%  stk={sig['streak']:+d}  rvol={sig['rvol']:.2f}x  atr={atr5:.2f}%  div={sig['divergence']:+.3f}%")
    print(f"   SL=${sl} [{sl_label}] -{sl_pct*100:.2f}%  →  TP=${tp} [{tp_label}] +{tp_pct*100:.2f}%  R:R {rr:.1f}:1")
    print(f"   qty={qty}  notional=${actual_notional:.2f}  riesgo=${risk_usd:.4f} (2% equity)  lev={LEVERAGE}x")
    print(f"   Riesgo=${risk_usd:.4f}  Potencial=${gain_usd:.4f}")

    if dry_run:
        print("   [DRY RUN — no se ejecuta]")
        return False

    # Reserve slot before placing order to prevent duplicate signals
    active_positions[sym] = {"symbol": sym, "side": bias, "entry": price,
                              "sl": sl, "tp": tp, "qty": qty,
                              "order_id": None, "be_moved": False, "liq": "?"}

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
            del active_positions[sym]
            return False

        order_id = resp["result"]["orderId"]
        await asyncio.sleep(1.5)

        pos = get_open_position(sym)
        if pos:
            entry = float(pos["avgPrice"])
            real_sl = float(pos["stopLoss"])
            real_tp = float(pos["takeProfit"])
            real_qty = float(pos["size"])
            active_positions[sym] = {
                "symbol": sym, "side": bias,
                "entry": entry, "sl": real_sl,
                "tp": real_tp, "qty": real_qty,
                "order_id": order_id, "be_moved": False,
                "liq": pos.get("liqPrice", "?"),
                "opened_at": time.time(),
            }
            # ── Log a PostgreSQL ──────────────────────────────────────────
            trade_id = pg_log_trade_open(
                sym, bias, entry, real_qty, real_qty * entry, LEVERAGE,
                real_sl, real_tp, sl_label, tp_label, rr, sig,
            )
            active_positions[sym]["pg_id"] = trade_id
            pg_log_signal(sym, bias, sig, executed=True)
            # ─────────────────────────────────────────────────────────────
            print(f"   ✅ ABIERTA @ ${entry}  Liq=${pos.get('liqPrice','?')}  [PG id={trade_id}]")
            last_trade_time[sym] = time.time()
            return True
        else:
            print(f"   ⚠️  Orden enviada pero posición no encontrada")
            pg_log_signal(sym, bias, sig, executed=False, reject_reason="position not found after order")
            return False

    except Exception as e:
        print(f"   ❌ Exception: {e}")
        active_positions.pop(sym, None)
        return False


def close_position(sym: str, side: str, qty: float) -> bool:
    """Market-close an open position."""
    close_side = "Sell" if side == "LONG" else "Buy"
    pos_idx    = 1 if side == "LONG" else 2
    resp = rest_post("/v5/order/create", {
        "category": "linear", "symbol": sym,
        "side": close_side, "orderType": "Market",
        "qty": str(qty), "positionIdx": pos_idx,
        "reduceOnly": True, "timeInForce": "IOC",
    })
    return resp["retCode"] == 0


def update_sl(sym: str, side: str, new_sl: float) -> bool:
    """Move stop-loss to new_sl."""
    resp = rest_post("/v5/position/trading-stop", {
        "category": "linear", "symbol": sym,
        "positionIdx": 1 if side == "LONG" else 2,
        "stopLoss": str(new_sl), "slTriggerBy": "LastPrice",
    })
    return resp["retCode"] == 0


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
                exit_price, final_pnl = 0.0, 0.0
                try:
                    hist = rest_get("/v5/execution/list", {"category": "linear", "symbol": sym, "limit": "5"})
                    execs = hist["result"]["list"]
                    for ex in execs[:2]:
                        print(f"   {ex['side']} {ex['execQty']} @ ${ex['execPrice']}")
                    if execs:
                        exit_price = float(execs[0]["execPrice"])
                    eq, avail = get_balance()
                    print(f"   💰 Equity=${eq:.5f}  Disponible=${avail:.5f}")
                    pg_log_equity(eq, avail, len(active_positions) - 1)
                except Exception:
                    pass
                # Calcular PnL aproximado para el registro
                dur = int(time.time() - ap.get("opened_at", time.time()))
                if exit_price:
                    if side == "LONG":
                        final_pnl = (exit_price - ap["entry"]) * ap["qty"]
                    else:
                        final_pnl = (ap["entry"] - exit_price) * ap["qty"]
                pg_log_trade_close(ap.get("pg_id"), exit_price, final_pnl, "SL/TP", ap["be_moved"], dur)
                del active_positions[sym]
                continue

            mark  = float(pos["markPrice"])
            pnl   = float(pos["unrealisedPnl"])
            entry = ap["entry"]
            sl    = float(pos["stopLoss"]) if pos.get("stopLoss") else 0.0
            tp    = float(pos["takeProfit"]) if pos.get("takeProfit") else 0.0

            sl_dist = abs(mark - sl) / mark * 100
            tp_dist = abs(tp - mark) / mark * 100 if tp > 0 else 0
            color   = "🟢" if pnl >= 0 else "🔴"
            ts      = time.strftime("%H:%M:%S")
            print(f"[{ts}] {color} {sym} {side}  ${mark:.5f}  pnl=${pnl:+.4f}  SL-{sl_dist:.2f}%  TP+{tp_dist:.2f}%")

            # Log tick a PG
            pg_log_tick(sym, side, mark, pnl, sl_dist, tp_dist, ap["be_moved"])

            # ── Reversal tick-by-tick (precio, sin esperar candle) ─────────────
            if not dry_run:
                last_mark = ap.get("last_mark", mark)
                ap["last_mark"] = mark
                tick_move = (mark - last_mark) / last_mark * 100
                adverse   = tick_move if side == "SHORT" else -tick_move
                if adverse >= 0.15:
                    if close_position(sym, side, ap["qty"]):
                        dur = int(time.time() - ap.get("opened_at", time.time()))
                        pg_log_trade_close(ap.get("pg_id"), mark, pnl, "EMERGENCY", ap["be_moved"], dur)
                        pg_log_tick(sym, side, mark, pnl, sl_dist, tp_dist, ap["be_moved"], event="EMERGENCY")
                        print(f"   🚨 CIERRE EMERGENCIA {sym} {side}: precio +{adverse:.2f}% contra  pnl=${pnl:+.4f}")
                        del active_positions[sym]
                    continue

            # ── Auto-close cerca del TP ──────────────────────────────────────────
            if tp > 0 and not dry_run:
                dist_to_tp = abs(tp - mark) / mark * 100
                if dist_to_tp <= 0.05:
                    if close_position(sym, side, ap["qty"]):
                        dur = int(time.time() - ap.get("opened_at", time.time()))
                        pg_log_trade_close(ap.get("pg_id"), mark, pnl, "NEAR_TP", ap["be_moved"], dur)
                        pg_log_tick(sym, side, mark, pnl, sl_dist, tp_dist, ap["be_moved"], event="NEAR_TP")
                        print(f"   💰 AUTO-CLOSE {sym}: a {dist_to_tp:.3f}% del TP  pnl=${pnl:+.4f}")
                        del active_positions[sym]
                    continue

            # ── Reversal candle-based (flip) ────────────────────────────────────
            if not dry_run:
                st = pair_states.get(sym)
                rev_sig = st.check_signal() if st else None
                if rev_sig and rev_sig["bias"] != side:
                    streak_against = abs(rev_sig["streak"]) >= MIN_STREAK
                    m3_strong      = abs(rev_sig["m3"]) >= MIN_M3_PCT
                    if streak_against and m3_strong:
                        qty_close = ap["qty"]
                        if close_position(sym, side, qty_close):
                            dur = int(time.time() - ap.get("opened_at", time.time()))
                            pg_log_trade_close(ap.get("pg_id"), mark, pnl, "FLIP", ap["be_moved"], dur)
                            pg_log_tick(sym, side, mark, pnl, sl_dist, tp_dist, ap["be_moved"], event="FLIP")
                            print(f"   🔄 FLIP {sym}: {side}→{rev_sig['bias']}  "
                                  f"stk={rev_sig['streak']}  m3={rev_sig['m3']:+.3f}%  pnl=${pnl:+.4f}")
                            del active_positions[sym]
                            await asyncio.sleep(0.5)
                            eq2, avail2 = get_balance()
                            same_dir = sum(1 for p in active_positions.values() if p["side"] == rev_sig["bias"])
                            if avail2 >= 0.15 and same_dir == 0:
                                await execute_entry(sym, rev_sig, avail2, dry_run)
                            else:
                                print(f"   ⏸  Flip bloqueado: avail=${avail2:.4f}  same_dir={same_dir}")
                        continue

            # ── Phase 1: BE ─────────────────────────────────────────────────────
            if not ap["be_moved"] and not dry_run:
                risk = abs(entry - ap["sl"]) * ap["qty"]
                if pnl >= risk * BE_TRIGGER:
                    new_sl = round(entry * 1.001 if side == "LONG" else entry * 0.999, 6)
                    resp = rest_post("/v5/position/trading-stop", {
                        "category": "linear", "symbol": sym,
                        "positionIdx": 1 if side == "LONG" else 2,
                        "stopLoss": str(new_sl), "slTriggerBy": "LastPrice",
                        "takeProfit": "0",
                    })
                    if resp["retCode"] == 0:
                        ap["be_moved"] = True
                        ap["sl"] = new_sl
                        ap["peak_pnl"] = pnl
                        pg_log_tick(sym, side, mark, pnl, sl_dist, tp_dist, True, event="BE")
                        print(f"   🔒 SL→BE: ${new_sl}  TP fijo cancelado — trail activo  (pnl=${pnl:+.4f})")

            # ── Phase 2: Trail ──────────────────────────────────────────────────
            elif ap["be_moved"] and not dry_run:
                ap["peak_pnl"] = max(ap.get("peak_pnl", pnl), pnl)
                new_sl = round(mark * (1 - TRAIL_TIGHT) if side == "LONG"
                               else mark * (1 + TRAIL_TIGHT), 6)
                better = (new_sl > ap["sl"]) if side == "LONG" else (new_sl < ap["sl"])
                if better and update_sl(sym, side, new_sl):
                    ap["sl"] = new_sl
                    pg_log_tick(sym, side, mark, pnl, sl_dist, tp_dist, True, event="TRAIL")
                    print(f"   📈 trail {sym}: SL→${new_sl}  pnl=${pnl:+.4f}")

        except Exception as e:
            print(f"   monitor err {sym}: {e}")


# ─── WEBSOCKET HANDLER ─────────────────────────────────────────────────────
async def ws_handler(symbols: list[str], dry_run: bool):
    global active_positions

    # Always include BTC as reference (beta-adjusted divergence), never traded
    ref_sym   = "BTCUSDT"
    trade_syms = [s for s in symbols if s != ref_sym]

    # Build subscription topics: kline.1 + kline.3 for tradeable symbols + BTC 1m ref
    topics_1m  = [f"kline.1.{s}" for s in trade_syms]
    topics_3m  = [f"kline.3.{s}" for s in trade_syms]
    topics_btc = [f"kline.1.{ref_sym}"]
    all_topics = topics_1m + topics_3m + topics_btc

    # Initialize state for tradeable symbols only
    for sym in trade_syms:
        pair_states[sym] = PairState(symbol=sym)

    print(f"\n{'='*60}")
    print(f"QTS Fast Auto-Trader {'[DRY RUN]' if dry_run else '[LIVE]'}")
    print(f"Símbolos: {len(trade_syms)}  |  SL={SL_PCT*100:.1f}%  TP={TP_PCT*100:.1f}%  R:R 2:1")
    print(f"Condiciones: 1m+3m alineados  stk={MIN_STREAK}-{MAX_STREAK}  m3≥{MIN_M3_PCT}%  div≥{MIN_DIVERGE}%")
    print(f"Referencia BTC: {ref_sym} (beta-adjusted divergence activo)")
    print(f"{'='*60}")

    # Pre-load historical candles via REST to warm up EMAs
    print("Cargando historial para warm-up EMA...")
    # Warm up BTC reference first
    try:
        d = rest_get("/v5/market/kline", {"category": "linear", "symbol": ref_sym, "interval": "1", "limit": "10"})
        for c in reversed(d["result"]["list"]):
            btc_closes_1m.append(float(c[4]))
        print(f"  BTC ref warm-up: {len(btc_closes_1m)} candles")
    except Exception as e:
        print(f"  BTC warm-up err: {e}")

    for sym in trade_syms:
        try:
            st = pair_states[sym]
            for tf, cl_attr, vl_attr, limit in [
                ("1",  "closes_1m",  "vols_1m",  "30"),
                ("3",  "closes_3m",  "vols_3m",  "30"),
                ("15", "closes_15m", None,        "30"),
                ("60", "closes_1h",  None,        "24"),
            ]:
                d = rest_get("/v5/market/kline", {
                    "category": "linear", "symbol": sym, "interval": tf, "limit": limit
                })
                candles = list(reversed(d["result"]["list"]))
                for c in candles:
                    getattr(st, cl_attr).append(float(c[4]))   # close
                    if vl_attr:
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
                    if now - last_monitor >= 2 and active_positions:
                        await monitor_position(dry_run)
                        last_monitor = now

                    # Print alive status every 30 seconds
                    if now - last_scan_report >= 30:
                        eq, avail = get_balance()
                        n = len(active_positions)
                        print(f"[{time.strftime('%H:%M:%S')}] 👁 {n}/{MAX_POSITIONS} pos  Equity=${eq:.5f}  Disponible=${avail:.5f}")
                        pg_log_equity(eq, avail, n)
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

                    # BTC reference update — update deque but don't trade
                    if sym == "BTCUSDT":
                        candle = msg["data"][0] if msg.get("data") else None
                        if candle and candle.get("confirm", False):
                            btc_closes_1m.append(float(candle["close"]))
                        continue

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
                    pg_log_signal(sym, sig["bias"], sig, executed=False, reject_reason="pending_execution_check")

                    # Detect market regime: if >60% of aligned pairs are SHORT → allow 2 SHORTs
                    aligned = [(s2, st2) for s2, st2 in pair_states.items()
                               if len(st2.closes_1m) >= 12 and len(st2.closes_3m) >= 6]
                    short_count = sum(1 for _, st2 in aligned
                                      if st2._ema(st2.closes_1m,9) < st2._ema(st2.closes_1m,21)
                                      and st2._ema(st2.closes_3m,9) < st2._ema(st2.closes_3m,21))
                    market_short = len(aligned) > 0 and short_count / len(aligned) >= 0.60
                    market_long  = len(aligned) > 0 and (len(aligned) - short_count) / len(aligned) >= 0.60

                    # Allow 2 positions in dominant direction, else max 1 per side
                    same_dir = sum(1 for p in active_positions.values() if p["side"] == sig["bias"])
                    dominant_dir = "SHORT" if market_short else ("LONG" if market_long else None)
                    max_same = 2 if sig["bias"] == dominant_dir else 1
                    if same_dir >= max_same:
                        continue

                    last_signal_time[sym] = now
                    regime_tag = f" [{dominant_dir or 'MIXTO'} {short_count}/{len(aligned)}]"
                    print(f"[{time.strftime('%H:%M:%S')}] 📡 SEÑAL {sym} {sig['bias']}  m3={sig['m3']:+.3f}%  stk={sig['streak']:+d}  div={sig.get('divergence', 0):+.3f}%  p=${sig['price']:.5f}{regime_tag}")

                    async with position_lock:
                        if sym in active_positions:
                            continue
                        same_dir = sum(1 for p in active_positions.values() if p["side"] == sig["bias"])
                        if same_dir >= max_same:
                            print(f"   ⏸  Ya hay {same_dir} {sig['bias']} activo(s)")
                            continue
                        if len(active_positions) >= MAX_POSITIONS:
                            print(f"   ⏸  Máximo de posiciones alcanzado ({MAX_POSITIONS})")
                            continue

                        eq, avail = get_balance()
                        if avail < 0.15:
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
