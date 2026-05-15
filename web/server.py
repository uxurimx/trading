"""
web/server.py — QTS Dashboard
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp
import uvicorn
import websockets as websockets_lib
import uuid
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import settings, SPEED_CONFIGS
from core.absorption import AbsorptionDetector
from core.liquidity import LiquidityAnalyzer
from core.trend import TrendAnalyzer
from core.regime import RegimeClassifier, OpportunityScorer
from core.technicals import TechIndicators
from core.executor import BybitExecutor
from core.order_model import OrderRequest
from streams.market import MarketStream
from streams.account import AccountStream
from streams.klines import KlineStream
from web.calculator import calc_position_metrics, format_elapsed

logging.basicConfig(level=logging.WARNING)
log = logging.getLogger("qts.web")

# ─── Estado global ────────────────────────────────────────────────────────────

_market  = MarketStream()
_account = AccountStream()
_klines  = KlineStream()
_exec    = BybitExecutor()

_abs_det  = AbsorptionDetector()
_liq_an   = LiquidityAnalyzer()
_trend_an = TrendAnalyzer()
_regime   = RegimeClassifier()
_scorer   = OpportunityScorer()

_signals:        dict = {}
_mark_prices:    dict = {}   # sym → float, actualizado por WS público (tick a tick)
_pos_first_seen: dict = {}   # pos_key → server timestamp when first observed

# ─── Tipo de cambio MXN ───────────────────────────────────────────────────────

_mxn_rate       = 17.5
_mxn_last_fetch = 0.0
_MXN_TTL        = 300

async def _refresh_mxn() -> None:
    global _mxn_rate, _mxn_last_fetch
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                "https://open.er-api.com/v6/latest/USD",
                timeout=aiohttp.ClientTimeout(total=6),
            ) as r:
                data = await r.json()
                rate = float(data.get("rates", {}).get("MXN", _mxn_rate))
                if rate > 1:
                    _mxn_rate = rate
                    _mxn_last_fetch = time.time()
    except Exception as e:
        log.debug("mxn fetch: %s", e)

async def _mxn_loop() -> None:
    while True:
        if time.time() - _mxn_last_fetch > _MXN_TTL:
            await _refresh_mxn()
        await asyncio.sleep(60)

# ─── Mark price vía WebSocket público Bybit (tick a tick) ────────────────────

async def _ticker_ws_loop() -> None:
    """
    Suscribe al WS público linear de Bybit para obtener markPrice en tiempo real
    de todos los símbolos con posición abierta. Actualiza _mark_prices por tick.
    """
    url = (
        "wss://stream-testnet.bybit.com/v5/public/linear"
        if settings.bybit_testnet
        else "wss://stream.bybit.com/v5/public/linear"
    )
    while True:
        syms = {pos.symbol for pos in _account.state.open_positions()}
        if not syms:
            await asyncio.sleep(3)
            continue
        topics = [f"tickers.{s}" for s in syms]
        try:
            async with websockets_lib.connect(
                url, ping_interval=20, ping_timeout=15,
                max_size=2 * 1024 * 1024,
            ) as sock:
                await sock.send(json.dumps({"op": "subscribe", "args": topics}))
                subscribed = syms.copy()
                log.warning("ticker_ws: suscrito a %s", sorted(subscribed))
                async for raw in sock:
                    msg   = json.loads(raw)
                    topic = msg.get("topic", "")
                    if "tickers" in topic:
                        d   = msg.get("data", {})
                        sym = topic.split("tickers.")[-1]
                        # markPrice es el precio oficial de Bybit para PnL de futuros
                        p = float(d.get("markPrice") or d.get("lastPrice") or 0)
                        if p > 0:
                            _mark_prices[sym] = p
                    # Reconectar si hay nuevas posiciones
                    cur = {pos.symbol for pos in _account.state.open_positions()}
                    if cur - subscribed:
                        break
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.debug("ticker_ws_loop: %s", e)
            await asyncio.sleep(2)

# ─── Signal loop ──────────────────────────────────────────────────────────────

async def _signal_loop() -> None:
    await asyncio.sleep(12)
    while True:
        try:
            for sym, ms in list(_market.states.items()):
                if not ms.connected or ms.ticker.last_price <= 0:
                    continue
                trend      = _trend_an.analyze(ms)
                absorption = _abs_det.analyze(ms)
                lmap       = _liq_an.analyze(ms)
                regime     = _regime.classify(ms, trend)
                opp        = _scorer.score(absorption, regime, trend, lmap)
                fast_key   = SPEED_CONFIGS.get(settings.speed_level, SPEED_CONFIGS["standard"])["fast"]
                k15  = _klines.store.get(sym, fast_key)
                atr  = TechIndicators.atr(k15, 14) if k15 else 0.0
                rsi  = TechIndicators.rsi(TechIndicators.closes(k15), 14) if k15 else 50.0
                _signals[sym] = {
                    "opp": opp, "absorption": absorption,
                    "trend": trend, "regime": regime,
                    "atr": atr, "rsi": rsi,
                }
                _klines.request(sym)
        except Exception as e:
            log.error("signal_loop: %s", e)
        await asyncio.sleep(5)

# ─── Snapshot builder ─────────────────────────────────────────────────────────

def _build_snapshot() -> dict:
    """Construye el snapshot completo. Nunca lanza excepción — devuelve lo que puede."""
    try:
        st = _account.state
        b  = st.balance
        open_orders = getattr(st, "open_orders", {})  # compatibilidad si no existe el campo

        positions_data = []
        for pos in st.open_positions():
            try:
                sym = pos.symbol

                # Precio live: WS público ticker (tick a tick) > MarketStream > stale
                ms        = _market.states.get(sym)
                ms_price  = ms.ticker.last_price if (ms and ms.connected and ms.ticker.last_price > 0) else 0.0
                live_mark = _mark_prices.get(sym) or ms_price or 0.0

                # Duración: usa tiempo del servidor para evitar created_time corrupto de Bybit
                pos_key      = f"{sym}_{pos.side}"
                server_elapsed = time.time() - _pos_first_seen.setdefault(pos_key, time.time())
                bybit_elapsed  = max(0.0, time.time() - pos.created_time / 1000) if pos.created_time > 0 else 0.0
                # Si Bybit da un timestamp razonable (< 30 días), usamos el menor; sino solo servidor
                if 0 < bybit_elapsed < 30 * 24 * 3600:
                    elapsed_override = min(bybit_elapsed, server_elapsed)
                else:
                    elapsed_override = server_elapsed

                metrics = calc_position_metrics(pos, live_mark=live_mark,
                                                elapsed_override=elapsed_override)

                sig = _signals.get(sym, {})
                opp = sig.get("opp")
                ab  = sig.get("absorption")
                tr  = sig.get("trend")
                rg  = sig.get("regime")

                orders_data = [
                    {
                        "order_id": o.order_id,
                        "side":     o.side,
                        "type":     o.order_type,
                        "qty":      o.qty,
                        "price":    round(o.price, 6),
                        "status":   o.status,
                    }
                    for o in open_orders.values()
                    if o.symbol == sym and o.price > 0
                ]

                positions_data.append({
                    "symbol":    sym.replace("USDT", ""),
                    "full_sym":  sym,
                    "side":      pos.side,
                    "direction": "LONG" if pos.is_long else "SHORT",
                    "qty":       pos.size,
                    "leverage":  int(pos.leverage),
                    "entry":     round(pos.entry_price, 6),
                    "mark":      round(live_mark or pos.mark_price or pos.entry_price, 6),
                    "sl":        round(pos.stop_loss, 6),
                    "tp":        round(pos.take_profit, 6),
                    "liq":       round(pos.liquidation_price, 6),
                    "margin":    round(pos.margin, 2),
                    "notional":  round(pos.size * pos.entry_price, 2),
                    "score":     opp.score if opp else 0,
                    "ab_side":   ab.side   if ab  else "NEUTRAL",
                    "trend_dir": tr.direction if tr else "NEUTRAL",
                    "regime":    rg.regime if rg else "UNKNOWN",
                    "atr":       round(sig.get("atr", 0), 6),
                    "rsi":       round(sig.get("rsi", 50), 1),
                    "orders":    orders_data,
                    **metrics,
                })
            except Exception as e:
                log.error("snapshot pos %s: %s", getattr(pos, "symbol", "?"), e)

        # Limpiar entradas de posiciones cerradas del tracker de duración
        active_keys = {f"{pos.symbol}_{pos.side}" for pos in st.open_positions()}
        for k in list(_pos_first_seen):
            if k not in active_keys:
                del _pos_first_seen[k]

        symbol_pnl = sorted(
            [{"symbol": k.replace("USDT", ""), "pnl": round(v, 4)}
             for k, v in st.symbol_pnl.items() if v != 0],
            key=lambda x: abs(x["pnl"]), reverse=True,
        )

        # Mark prices de todos los símbolos monitoreados (para panel de análisis)
        marks = dict(_mark_prices)
        for sym, ms in _market.states.items():
            if ms.connected and ms.ticker.last_price > 0 and sym not in marks:
                marks[sym] = ms.ticker.last_price

        return {
            "ts":       int(time.time() * 1000),
            "mxn_rate": round(_mxn_rate, 4),
            "marks":    {k: round(v, 6) for k, v in marks.items()},
            "account": {
                "connected":      st.connected,
                "equity":         round(b.total_equity, 2),
                "wallet_balance": round(b.wallet_balance, 2),
                "available":      round(b.available_balance, 2),
                "used_margin":    round(b.used_margin, 2),
                "margin_pct":     round(b.margin_pct, 2),
                "unrealized_pnl": round(b.unrealized_pnl, 2),
                "daily_pnl":      round(st.daily_pnl, 2),
                "open_count":     len(positions_data),
                "error":          st.error or None,
            },
            "positions":   positions_data,
            "symbol_pnl":  symbol_pnl,
        }

    except Exception as e:
        log.error("_build_snapshot fatal: %s", e)
        # Heartbeat mínimo — nunca congela el timestamp del cliente
        return {
            "ts":       int(time.time() * 1000),
            "mxn_rate": round(_mxn_rate, 4),
            "account":  {"connected": False, "error": str(e), "equity": 0,
                         "available": 0, "used_margin": 0, "margin_pct": 0,
                         "unrealized_pnl": 0, "daily_pnl": 0, "open_count": 0},
            "positions":  [],
            "symbol_pnl": [],
        }

# ─── FastAPI ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await _exec.detect_position_mode()
    except Exception as e:
        log.warning("detect_position_mode: %s", e)

    await _refresh_mxn()

    tasks = [
        asyncio.create_task(_market.start(),      name="market"),
        asyncio.create_task(_account.start(),     name="account"),
        asyncio.create_task(_klines.start(),      name="klines"),
        asyncio.create_task(_signal_loop(),       name="signals"),
        asyncio.create_task(_mxn_loop(),          name="mxn"),
        asyncio.create_task(_ticker_ws_loop(),    name="ticker_ws"),
    ]
    log.warning("QTS Web Dashboard → http://0.0.0.0:%s", WEB_PORT)
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="QTS Dashboard", lifespan=lifespan)

_static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    return (_static_dir / "index.html").read_text()


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    """
    Cada cliente conectado recibe un snapshot cada segundo en su propio loop.
    No depende de un broadcast loop externo — elimina el punto de fallo.
    """
    await ws.accept()

    async def _send_loop():
        while True:
            try:
                await ws.send_text(json.dumps(_build_snapshot(), ensure_ascii=False))
            except Exception:
                return   # conexión cerrada — terminar
            await asyncio.sleep(1)

    send_task = asyncio.create_task(_send_loop())
    try:
        # Recibir mensajes del cliente (keepalive / ping)
        while True:
            await ws.receive_text()
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        send_task.cancel()


@app.get("/api/snapshot")
async def api_snapshot():
    return JSONResponse(_build_snapshot())


@app.get("/api/history")
async def api_history():
    """Historial de trades cerrados vía Bybit closed-pnl."""
    try:
        data = await _exec._get("/v5/position/closed-pnl", {
            "category": "linear",
            "limit":    "50",
        })
        items = data.get("result", {}).get("list", [])
        history = []
        for x in items:
            sym        = x.get("symbol", "")
            open_ts    = int(x.get("createdTime")  or 0)
            close_ts   = int(x.get("updatedTime")  or 0)
            duration_s = max(0, (close_ts - open_ts) // 1000) if open_ts and close_ts else 0
            history.append({
                "symbol":      sym.replace("USDT", ""),
                "full_sym":    sym,
                "side":        x.get("side", ""),
                "qty":         float(x.get("qty") or 0),
                "entry_price": float(x.get("avgEntryPrice") or 0),
                "exit_price":  float(x.get("avgExitPrice")  or 0),
                "closed_pnl":  float(x.get("closedPnl")     or 0),
                "leverage":    int(float(x.get("leverage") or 1)),
                "open_ts":     open_ts,
                "close_ts":    close_ts,
                "duration_s":  duration_s,
                "duration_fmt": format_elapsed(duration_s),
            })
        return JSONResponse({"history": history})
    except Exception as e:
        log.error("api_history: %s", e)
        return JSONResponse({"history": [], "error": str(e)})


@app.get("/api/symbols")
async def api_symbols():
    """Lista símbolos activos con mark price y score de señal."""
    result = []
    all_syms = set(_market.states) | set(_signals)
    for sym in sorted(all_syms):
        ms    = _market.states.get(sym)
        mark  = _mark_prices.get(sym) or (ms.ticker.last_price if ms and ms.connected else 0)
        sig   = _signals.get(sym, {})
        opp   = sig.get("opp")
        result.append({
            "symbol": sym,
            "label":  sym.replace("USDT", ""),
            "mark":   round(mark, 6),
            "score":  opp.score if opp else 0,
        })
    result.sort(key=lambda x: -x["score"])
    return JSONResponse({"symbols": result})


@app.get("/api/analyze/{symbol}")
async def api_analyze(symbol: str):
    """Señales de mercado para un símbolo."""
    sym = symbol.upper()
    if not sym.endswith("USDT"):
        sym += "USDT"
    sig  = _signals.get(sym, {})
    opp  = sig.get("opp")
    ab   = sig.get("absorption")
    tr   = sig.get("trend")
    rg   = sig.get("regime")
    mark = _mark_prices.get(sym) or 0.0
    ms   = _market.states.get(sym)
    if not mark and ms and ms.connected:
        mark = ms.ticker.last_price
    return JSONResponse({
        "symbol":    sym,
        "mark":      round(mark, 6),
        "score":     opp.score    if opp else 0,
        "ab_side":   ab.side      if ab  else "NEUTRAL",
        "trend_dir": tr.direction if tr  else "NEUTRAL",
        "regime":    rg.regime    if rg  else "UNKNOWN",
        "atr":       round(sig.get("atr", 0), 6),
        "rsi":       round(sig.get("rsi", 50), 1),
    })


@app.post("/api/trade")
async def api_trade(req: Request):
    """Ejecuta una orden de mercado o límite vía BybitExecutor."""
    try:
        body       = await req.json()
        symbol     = str(body.get("symbol", "")).upper()
        if not symbol.endswith("USDT"):
            symbol += "USDT"
        side       = str(body.get("side", "Buy"))        # "Buy" | "Sell"
        order_type = str(body.get("order_type", "Market"))
        entry      = float(body.get("entry", 0))
        sl         = float(body.get("sl", 0))
        tp         = float(body.get("tp", 0))
        size_usdt  = float(body.get("size_usdt", 0))
        leverage   = int(body.get("leverage", 10))

        if not symbol or sl <= 0 or tp <= 0 or size_usdt <= 0:
            return JSONResponse({"success": False, "error": "Parámetros incompletos"})

        # Precio de referencia para calcular qty
        mark = _mark_prices.get(symbol) or 0.0
        ms   = _market.states.get(symbol)
        if not mark and ms and ms.connected:
            mark = ms.ticker.last_price
        ref_price = entry if (order_type == "Limit" and entry > 0) else (mark or entry)
        if ref_price <= 0:
            return JSONResponse({"success": False, "error": "Precio de referencia no disponible"})

        # Calcular qty según lotSizeFilter
        info    = await _exec.load_instrument_info(symbol)
        step    = float(info.qty_step)
        raw_qty = size_usdt / ref_price
        qty     = max(float(info.min_qty), round(round(raw_qty / step) * step, 8))

        order = OrderRequest(
            symbol     = symbol,
            side       = side,
            qty        = qty,
            order_type = order_type,
            price      = entry if order_type == "Limit" else 0.0,
            sl_price   = sl,
            tp_price   = tp,
            entry_price= ref_price,
            leverage   = leverage,
            trace_id   = str(uuid.uuid4())[:8],
            strategy_tag = "manual_web",
        )

        if order_type == "Market":
            result = await _exec.place_market_bracket(order)
        else:
            result = await _exec.place_limit_bracket(order)

        return JSONResponse({
            "success":  result.success,
            "order_id": result.order_id,
            "qty":      qty,
            "error":    result.error_msg,
        })
    except Exception as e:
        log.error("api_trade: %s", e)
        return JSONResponse({"success": False, "error": str(e)})


WEB_PORT = int(getattr(settings, "web_port", 8080))

if __name__ == "__main__":
    uvicorn.run("web.server:app", host="0.0.0.0", port=WEB_PORT,
                log_level="warning", reload=False)
