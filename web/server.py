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
import json as _json_mod
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
from web.zone_tracker import tracker as _zone_tracker
from web.liquidity_map import build_liquidity_map

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

# ─── Análisis mentales persistidos ───────────────────────────────────────────

_ANALYSES_PATH = Path(__file__).parent.parent / "storage" / "analyses.json"
_analyses: dict = {}          # id → analysis dict

def _load_analyses_file() -> None:
    global _analyses
    if _ANALYSES_PATH.exists():
        try:
            _analyses = _json_mod.loads(_ANALYSES_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("analyses load error: %s", e)

def _save_analyses_file() -> None:
    try:
        _ANALYSES_PATH.parent.mkdir(parents=True, exist_ok=True)
        _ANALYSES_PATH.write_text(
            _json_mod.dumps(_analyses, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        log.warning("analyses save error: %s", e)

_load_analyses_file()

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

def _enrich_analyses() -> list:
    """Convierte _analyses en dicts enriquecidos con calc_position_metrics."""
    from types import SimpleNamespace
    result = []
    for aid, a in _analyses.items():
        try:
            sym     = a.get("symbol", "")
            mark    = _mark_prices.get(sym) or 0.0
            ms      = _market.states.get(sym)
            if not mark and ms and ms.connected:
                mark = ms.ticker.last_price
            entry   = float(a.get("entry", 0))
            sl      = float(a.get("sl", 0))
            tp      = float(a.get("tp", 0))
            size_u  = float(a.get("size", 0))
            lev     = float(a.get("leverage", 10))
            is_long = a.get("direction", "Buy") == "Buy"
            qty     = size_u / entry if entry > 0 else 0.0
            margin  = size_u / lev   if lev   > 0 else size_u

            mock = SimpleNamespace(
                entry_price       = entry,
                mark_price        = mark,
                stop_loss         = sl,
                take_profit       = tp,
                size              = qty,
                is_long           = is_long,
                margin            = max(margin, 1.0),
                created_time      = int(a.get("created_at", 0)),
                leverage          = lev,
                liquidation_price = 0.0,
            )
            metrics = calc_position_metrics(mock, live_mark=mark if mark > 0 else entry)
            result.append({
                "id":        aid,
                "symbol":    sym.replace("USDT", ""),
                "full_sym":  sym,
                "side":      a.get("direction", "Buy"),
                "direction": "LONG" if is_long else "SHORT",
                "entry":     round(entry, 6),
                "mark":      round(mark or entry, 6),
                "sl":        round(sl, 6),
                "tp":        round(tp, 6),
                "leverage":  int(lev),
                "margin":    round(margin, 2),
                "size_usdt": round(size_u, 2),
                "notes":     a.get("notes", ""),
                "created_at": a.get("created_at", 0),
                **metrics,
            })
        except Exception as e:
            log.debug("enrich_analysis %s: %s", aid, e)
    result.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    return result


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

                # Sampler de zona (cronotopología): clasifica el mark y acumula tiempo.
                # Corre dentro del snapshot loop (1 Hz) — suficiente para zonas con
                # duración de minutos/horas; el sampler dedicado complementa a 0.5 Hz.
                _zone_tracker().sample(pos_key, metrics["geometry"], live_mark or pos.entry_price)
                zones_summary = _zone_tracker().summary(pos_key)

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
                    "zones":     zones_summary,
                    **metrics,
                })
            except Exception as e:
                log.error("snapshot pos %s: %s", getattr(pos, "symbol", "?"), e)

        # Limpiar entradas de posiciones cerradas del tracker de duración + zonas
        active_keys = {f"{pos.symbol}_{pos.side}" for pos in st.open_positions()}
        for k in list(_pos_first_seen):
            if k not in active_keys:
                del _pos_first_seen[k]
        for k in _zone_tracker().known_keys():
            if k not in active_keys:
                _zone_tracker().forget(k)

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
            "analyses":    _enrich_analyses(),
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
            "analyses":   [],
        }

async def _account_refresh_loop_once() -> None:
    """Un solo refresh REST de posiciones y balance (usado como trigger on-demand)."""
    import aiohttp as _aio
    await asyncio.sleep(1)   # pequeño delay para que Bybit actualice su estado
    try:
        async with _aio.ClientSession() as session:
            await _account._fetch_positions(session)
            await _account._fetch_balance(session)
    except Exception as e:
        log.debug("account_refresh_once: %s", e)


async def _account_refresh_loop() -> None:
    """Refresca posiciones y balance via REST cada 15 s como respaldo al WS privado."""
    await asyncio.sleep(15)   # primera corrida: dejar que el WS arranque primero
    while True:
        await _account_refresh_loop_once()
        await asyncio.sleep(15)


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
        asyncio.create_task(_account_refresh_loop(), name="account_refresh"),
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
async def api_history(limit: int = 50):
    """Historial de trades cerrados vía Bybit closed-pnl."""
    try:
        data = await _exec._get("/v5/position/closed-pnl", {
            "category": "linear",
            "limit":    str(min(limit, 200)),
        })
        items = data.get("result", {}).get("list", [])
        history = []
        for x in items:
            sym        = x.get("symbol", "")
            open_ts    = int(x.get("createdTime")  or 0)
            close_ts   = int(x.get("updatedTime")  or 0)
            duration_s = max(0, (close_ts - open_ts) // 1000) if open_ts and close_ts else 0
            pnl        = float(x.get("closedPnl")     or 0)
            avg_entry  = float(x.get("avgEntryPrice") or 0)
            avg_exit   = float(x.get("avgExitPrice")  or 0)
            cum_entry  = float(x.get("cumEntryValue") or 0)
            cum_exit   = float(x.get("cumExitValue")  or 0)
            total_fees = abs(cum_entry - cum_exit - pnl) if (cum_entry or cum_exit) else 0.0

            # ── Dirección: derivada del movimiento de precio + signo del PnL ─────
            # Bybit puede devolver el lado de la orden de cierre (invertido).
            # La fuente de verdad es: si precio subió y ganaste → LONG; si bajó → SHORT.
            price_delta = avg_exit - avg_entry
            if avg_entry > 0 and abs(price_delta) / avg_entry > 0.0001:
                is_long = (price_delta > 0) == (pnl >= 0)
            else:
                # Fallback al campo side cuando el movimiento es despreciable
                is_long = (x.get("side", "Buy").lower() == "buy")

            history.append({
                "symbol":      sym.replace("USDT", ""),
                "full_sym":    sym,
                "side":        "Buy" if is_long else "Sell",
                "direction":   "LONG" if is_long else "SHORT",
                "qty":         float(x.get("qty") or 0),
                "entry_price": avg_entry,
                "exit_price":  avg_exit,
                "closed_pnl":  pnl,
                "total_fees":  round(total_fees, 4),
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


@app.post("/api/trade-analysis")
async def api_trade_analysis(req: Request):
    """Análisis IA de un trade cerrado. Devuelve pepitas de oro."""
    try:
        body   = await req.json()
        trade  = body.get("trade", {})
        klines = body.get("klines", [])   # opcional: velas durante el trade

        direction  = trade.get("direction", "LONG")
        symbol     = trade.get("symbol", "?")
        entry      = trade.get("entry_price", 0)
        exit_price = trade.get("exit_price", 0)
        pnl        = trade.get("closed_pnl", 0)
        leverage   = trade.get("leverage", 1)
        duration   = trade.get("duration_fmt", "?")
        fees       = trade.get("total_fees", 0)
        price_chg  = ((exit_price - entry) / entry * 100) if entry > 0 else 0

        kline_summary = ""
        if klines:
            highs = [k["h"] for k in klines]
            lows  = [k["l"] for k in klines]
            kline_summary = (
                f"\nVelas durante el trade ({len(klines)} velas): "
                f"máximo={max(highs):.4f}, mínimo={min(lows):.4f}, "
                f"rango={max(highs)-min(lows):.4f}"
            )

        prompt = f"""Eres un coach de trading de futuros perpetuos (Bybit). Analiza este trade cerrado y extrae insights accionables.

TRADE:
- Par: {symbol} {direction} {leverage}x
- Entrada: {entry:.4f} → Salida: {exit_price:.4f} ({price_chg:+.3f}%)
- PnL neto: {pnl:+.4f} USDT
- Fees totales: {fees:.4f} USDT
- Duración: {duration}{kline_summary}

Responde SOLO con este JSON (sin markdown, sin texto extra):
{{
  "veredicto": "win|loss|breakeven",
  "resumen": "1 oración sobre qué pasó",
  "fortalezas": ["punto 1", "punto 2"],
  "debilidades": ["punto 1", "punto 2"],
  "lecciones": ["lección accionable 1", "lección 2"],
  "patron": "si detectas un patrón recurrente de comportamiento del trader en este trade",
  "score": 1-10
}}"""

        from core.ai_strategy import AIStrategyAgent
        agent = AIStrategyAgent()
        client, model, use_json = agent._make_client_and_model()

        kwargs: dict = {"model": model, "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.4, "max_tokens": 600}
        if use_json:
            kwargs["response_format"] = {"type": "json_object"}

        resp = await client.chat.completions.create(**kwargs)
        raw  = resp.choices[0].message.content.strip()

        import json as _json
        try:
            result = _json.loads(raw)
        except Exception:
            import re
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            result = _json.loads(m.group()) if m else {"resumen": raw}

        return JSONResponse({"ok": True, "analysis": result})
    except Exception as e:
        log.error("api_trade_analysis: %s", e)
        return JSONResponse({"ok": False, "error": str(e)})


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


@app.get("/api/klines/{symbol}")
async def api_klines(symbol: str, tf: str = "15", limit: int = 60):
    """Velas OHLCV del símbolo para el mini chart de Salud."""
    sym = symbol.upper()
    try:
        data = await _exec._get("/v5/market/kline", {
            "category": "linear",
            "symbol":   sym,
            "interval": tf,
            "limit":    str(min(int(limit), 200)),
        })
        raw = data.get("result", {}).get("list", []) or []
        klines = [
            {"t": int(k[0]), "o": float(k[1]), "h": float(k[2]),
             "l": float(k[3]), "c": float(k[4]), "v": float(k[5])}
            for k in raw
        ]
        klines.reverse()   # Bybit devuelve más reciente primero
        return JSONResponse({"klines": klines, "tf": tf})
    except Exception as e:
        log.error("api_klines %s: %s", sym, e)
        return JSONResponse({"klines": [], "tf": tf, "error": str(e)})


@app.get("/api/liquidity/{symbol}")
async def api_liquidity(
    symbol: str,
    view_min: float = 0.0,
    view_max: float = 0.0,
    bucket_mult: float = 1.0,
):
    """Mapa de liquidez para el viewport [view_min, view_max] del símbolo."""
    sym = symbol.upper()
    if not sym.endswith("USDT"):
        sym += "USDT"
    ms = _market.states.get(sym)
    if not ms:
        return JSONResponse({"error": "symbol not streaming"}, status_code=404)

    # Defaults: ±2% alrededor del precio actual si no se especifica viewport
    price = ms.ticker.last_price or ms.orderbook.mid_price
    if view_min <= 0 or view_max <= 0 or view_max <= view_min:
        if price <= 0:
            return JSONResponse({"error": "no price"}, status_code=503)
        view_min = price * 0.98
        view_max = price * 1.02

    payload = build_liquidity_map(
        ms,
        _account.state.open_orders,
        view_min=view_min,
        view_max=view_max,
        bucket_mult=max(0.25, min(8.0, bucket_mult)),
    )
    if payload is None:
        return JSONResponse({"error": "no data"}, status_code=503)
    return JSONResponse(payload)


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


@app.post("/api/close/{symbol}/{side}")
async def api_close_position(symbol: str, side: str):
    """Cierra una posición a mercado (reduceOnly=True)."""
    sym = symbol.upper()
    pos = next(
        (p for p in _account.state.open_positions() if p.symbol == sym and p.side == side),
        None,
    )
    if not pos:
        return JSONResponse({"success": False, "error": "Posición no encontrada"})
    close_side = "Sell" if pos.is_long else "Buy"
    body = {
        "category":    "linear",
        "symbol":      sym,
        "side":        close_side,
        "orderType":   "Market",
        "qty":         str(pos.size),
        "timeInForce": "IOC",
        "reduceOnly":  True,
        "positionIdx": _exec._pos_idx(side),
    }
    try:
        data = await _exec._post("/v5/order/create", body)
        if data.get("retCode") == 0:
            log.warning("Position closed: %s %s", sym, side)
            # Refresh inmediato: no esperar al WS privado para actualizar el estado
            asyncio.create_task(_account_refresh_loop_once())
            return JSONResponse({"success": True, "order_id": data["result"].get("orderId", "")})
        return JSONResponse({"success": False, "error": data.get("retMsg", "error")})
    except Exception as e:
        log.error("api_close_position: %s", e)
        return JSONResponse({"success": False, "error": str(e)})


@app.post("/api/move-sl")
async def api_move_sl_endpoint(req: Request):
    """Mueve el SL de una posición al precio indicado."""
    try:
        body   = await req.json()
        symbol = str(body.get("symbol", "")).upper()
        side   = str(body.get("side", "Buy"))
        new_sl = float(body.get("new_sl", 0))
        if not symbol or new_sl <= 0:
            return JSONResponse({"success": False, "error": "Parámetros inválidos"})
        ok = await _exec.set_sl_tp(symbol=symbol, sl=new_sl, side=side)
        return JSONResponse({"success": ok, "error": "" if ok else "No se pudo mover el SL"})
    except Exception as e:
        log.error("api_move_sl: %s", e)
        return JSONResponse({"success": False, "error": str(e)})


@app.post("/api/analyses")
async def api_analyses_save(req: Request):
    """Guarda un nuevo análisis mental."""
    try:
        body = await req.json()
        sym  = str(body.get("symbol", "")).upper()
        if not sym.endswith("USDT"):
            sym += "USDT"
        aid = str(uuid.uuid4())[:12]
        _analyses[aid] = {
            "symbol":     sym,
            "direction":  str(body.get("direction", "Buy")),
            "entry":      float(body.get("entry", 0)),
            "sl":         float(body.get("sl", 0)),
            "tp":         float(body.get("tp", 0)),
            "size":       float(body.get("size", 0)),
            "leverage":   int(body.get("leverage", 10)),
            "notes":      str(body.get("notes", "")),
            "created_at": int(time.time() * 1000),
        }
        _save_analyses_file()
        return JSONResponse({"success": True, "id": aid})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)})


@app.delete("/api/analyses/{aid}")
async def api_analyses_delete(aid: str):
    _analyses.pop(aid, None)
    _save_analyses_file()
    return JSONResponse({"success": True})


WEB_PORT = int(getattr(settings, "web_port", 8080))

if __name__ == "__main__":
    uvicorn.run("web.server:app", host="0.0.0.0", port=WEB_PORT,
                log_level="warning", reload=False)
