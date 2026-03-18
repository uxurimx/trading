"""
mcp_server.py
─────────────
Servidor MCP para que Claude opere trades en tiempo real.

Expone las herramientas del sistema de trading como MCP tools:
  · get_signals()        — top símbolos por score de oportunidad
  · get_account()        — balance, equity, posiciones, PnL del día
  · get_positions()      — posiciones activas con progress, SL, TP
  · get_symbol_data()    — datos profundos de un símbolo (CVD, OI, precio)
  · place_order()        — ejecutar trade (market order con SL/TP)
  · close_position()     — cerrar posición a mercado
  · modify_sl_tp()       — ajustar SL y/o TP de posición activa

Arquitectura:
  · Loop asyncio en thread background → corre streams + calcula señales cada 5s
  · MCP tools son síncronos → leen estado compartido o envían coroutines al loop
  · Claude lee señales y decide: entrar, ajustar, cerrar, esperar
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import threading
import time
from concurrent.futures import Future
from typing import Any, Dict, List, Optional

import mcp.server.stdio
import mcp.types as types
from mcp.server import Server, NotificationOptions
from mcp.server.models import InitializationOptions

# ── Módulos del proyecto ──────────────────────────────────────────────────────
sys.path.insert(0, "/home/dev/Projects/trading")

from core.absorption   import AbsorptionDetector
from core.liquidity    import LiquidityAnalyzer
from core.trend        import TrendAnalyzer
from core.regime       import RegimeClassifier, OpportunityScorer
from core.technicals   import TechIndicators
from core.executor     import BybitExecutor
from core.config       import settings
from streams.market    import MarketStream
from streams.account   import AccountStream
from streams.klines    import KlineStream

log = logging.getLogger("qts.mcp")
logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

# ─── Log de sesión ────────────────────────────────────────────────────────────
_SESSION_LOG = "/tmp/qts_claude.log"


def _log_action(msg: str) -> None:
    """Escribe una acción en el log visible en la pestaña Extractor."""
    try:
        ts = time.strftime("%H:%M:%S")
        with open(_SESSION_LOG, "a") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass

# ─── Estado global compartido ─────────────────────────────────────────────────

_market  = MarketStream()
_account = AccountStream()
_klines  = KlineStream()
_exec    = BybitExecutor()

_abs_det  = AbsorptionDetector()
_liq_an   = LiquidityAnalyzer()
_trend_an = TrendAnalyzer()
_regime   = RegimeClassifier()
_scorer   = OpportunityScorer()

# Señales pre-calculadas por símbolo  { sym: { opp, absorption, trend, regime, atr } }
_signals: Dict[str, dict] = {}

# Loop asyncio del thread background (para enviar coroutines desde tools síncronos)
_loop: Optional[asyncio.AbstractEventLoop] = None


# ─── Thread background: streams + señales ─────────────────────────────────────

def _run_background() -> None:
    """Corre en un thread daemon: inicia todos los streams y calcula señales."""
    global _loop
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    _loop.run_until_complete(_async_main())


async def _async_main() -> None:
    # Detectar modo de posición antes de arrancar streams
    try:
        await _exec.detect_position_mode()
    except Exception as e:
        log.warning("detect_position_mode falló: %s", e)

    await asyncio.gather(
        _market.start(),
        _account.start(),
        _klines.start(),
        _signal_loop(),
    )


async def _signal_loop() -> None:
    """Calcula señales para todos los símbolos cada 5 segundos."""
    # Esperar a que haya datos antes de empezar
    await asyncio.sleep(10)

    while True:
        try:
            for sym, ms in _market.states.items():
                if not ms.connected:
                    continue
                if ms.ticker.last_price <= 0:
                    continue

                trend      = _trend_an.analyze(ms)
                absorption = _abs_det.analyze(ms)
                lmap       = _liq_an.analyze(ms)
                regime     = _regime.classify(ms, trend)
                opp        = _scorer.score(absorption, regime, trend, lmap)

                # ATR desde klines (si disponibles)
                k15  = _klines.store.get(sym, "15")
                atr  = TechIndicators.atr(k15, 14) if k15 else 0.0
                rsi  = TechIndicators.rsi(TechIndicators.closes(k15), 14) if k15 else 50.0

                _signals[sym] = {
                    "opp":        opp,
                    "absorption": absorption,
                    "trend":      trend,
                    "regime":     regime,
                    "atr":        atr,
                    "rsi":        rsi,
                    "price":      ms.ticker.last_price,
                    "funding":    ms.ticker.funding_rate,
                    "oi":         ms.ticker.open_interest,
                    "oi_vel":     ms.oi_velocity,
                    "cvd":        ms.cvd,
                    "ob_imbal":   ms.orderbook.imbalance,
                }

                # Solicitar klines si no están actualizados
                _klines.request(sym)

        except Exception as e:
            log.error("signal_loop error: %s", e)

        await asyncio.sleep(5)


def _submit(coro) -> Any:
    """Envía una coroutine al loop background y espera el resultado (bloqueante)."""
    if _loop is None:
        raise RuntimeError("Background loop not started")
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    return future.result(timeout=10)


# ─── Servidor MCP ─────────────────────────────────────────────────────────────

server = Server("qts-trading")


@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="get_signals",
            description=(
                "Retorna los top N símbolos ordenados por score de oportunidad. "
                "Incluye precio, absorción, régimen, tendencia, ATR, RSI, OI velocity y CVD. "
                "Usar para identificar el mejor setup antes de entrar."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Número de símbolos a retornar (default: 8)",
                        "default": 8,
                    }
                },
            },
        ),
        types.Tool(
            name="get_account",
            description=(
                "Retorna estado de la cuenta: equity, balance disponible, "
                "PnL realizado del día, margen usado, número de posiciones abiertas."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="get_positions",
            description=(
                "Retorna todas las posiciones activas con: símbolo, lado, "
                "precio de entrada, precio actual, PnL no realizado, "
                "progress hacia TP (0-1), distancia a SL y TP, leverage, "
                "distancia a liquidación."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="get_symbol_data",
            description=(
                "Datos profundos de un símbolo específico: "
                "últimas 10 velas CVD (dirección del flujo), "
                "OI velocity (nuevas posiciones entrando/saliendo), "
                "imbalance del orderbook, momentum de precio (últimas 5 muestras), "
                "funding rate actual."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "symbol": {
                        "type": "string",
                        "description": "Símbolo Bybit, ej: XRPUSDT, SOLUSDT",
                    }
                },
                "required": ["symbol"],
            },
        ),
        types.Tool(
            name="place_order",
            description=(
                "Ejecuta una orden de mercado con SL y TP. "
                "Calcula qty automáticamente para alcanzar goal_usd si TP se cumple. "
                "Incluye fees en el cálculo. "
                "side: 'Buy' para LONG, 'Sell' para SHORT."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "symbol":    {"type": "string",  "description": "Ej: XRPUSDT"},
                    "side":      {"type": "string",  "description": "'Buy' o 'Sell'"},
                    "qty":       {"type": "number",  "description": "Cantidad de contratos"},
                    "sl_price":  {"type": "number",  "description": "Precio del Stop Loss"},
                    "tp_price":  {"type": "number",  "description": "Precio del Take Profit"},
                    "leverage":  {"type": "integer", "description": "Apalancamiento (1-75)"},
                },
                "required": ["symbol", "side", "qty", "sl_price", "tp_price"],
            },
        ),
        types.Tool(
            name="close_position",
            description=(
                "Cierra una posición activa a precio de mercado. "
                "Usar cuando el momentum se debilita y quieres tomar ganancias "
                "antes de que el precio revierta."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Ej: XRPUSDT"}
                },
                "required": ["symbol"],
            },
        ),
        types.Tool(
            name="modify_sl_tp",
            description=(
                "Modifica el SL y/o TP de una posición activa. "
                "Pasar 0 para no modificar un valor. "
                "Usar para mover SL a breakeven, tomar ganancias parciales, "
                "o ajustar el TP cuando hay señales de continuación."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "symbol":   {"type": "string", "description": "Ej: XRPUSDT"},
                    "sl_price": {"type": "number", "description": "Nuevo SL (0 = no cambiar)"},
                    "tp_price": {"type": "number", "description": "Nuevo TP (0 = no cambiar)"},
                },
                "required": ["symbol", "sl_price", "tp_price"],
            },
        ),
        types.Tool(
            name="get_session_config",
            description=(
                "Lee la configuración de la sesión actual: meta en USD y pérdida máxima. "
                "Configurado por el usuario en la pestaña Extractor. "
                "Usar al inicio de cada sesión para saber los límites."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


@server.call_tool()
async def handle_call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    try:
        if name == "get_signals":
            result = _tool_get_signals(arguments.get("limit", 8))
        elif name == "get_account":
            result = _tool_get_account()
        elif name == "get_positions":
            result = _tool_get_positions()
        elif name == "get_symbol_data":
            result = _tool_get_symbol_data(arguments["symbol"])
        elif name == "place_order":
            result = await _tool_place_order(arguments)
        elif name == "close_position":
            result = await _tool_close_position(arguments["symbol"])
        elif name == "modify_sl_tp":
            result = await _tool_modify_sl_tp(arguments)
        elif name == "get_session_config":
            result = _tool_get_session_config()
        else:
            result = {"error": f"Tool desconocida: {name}"}
    except Exception as e:
        result = {"error": str(e)}

    return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]


# ─── Implementación de tools ──────────────────────────────────────────────────

def _tool_get_signals(limit: int = 8) -> dict:
    """Top N símbolos por score de oportunidad."""
    if not _signals:
        return {"status": "Calentando... espera 15-20 segundos e intenta de nuevo", "signals": []}

    items = []
    for sym, s in _signals.items():
        opp = s["opp"]
        if opp.score <= 0:
            continue
        ab  = s["absorption"]
        tr  = s["trend"]
        rg  = s["regime"]
        items.append({
            "symbol":     sym.replace("USDT", ""),
            "full_sym":   sym,
            "score":      opp.score,
            "direction":  opp.direction,
            "price":      round(s["price"], 6),
            "atr":        round(s["atr"], 6),
            "atr_pct":    round(s["atr"] / s["price"] * 100, 3) if s["price"] > 0 else 0,
            "rsi":        round(s["rsi"], 1),
            # Absorción
            "absorption": {
                "score":     ab.score,
                "side":      ab.side,
                "is_signal": ab.is_signal,
                "reasons":   ab.reasons[:2],
            },
            # Régimen
            "regime": {
                "type":       rg.regime,
                "label":      rg.label,
                "confidence": rg.confidence,
                "volatility_pct": round(rg.volatility_pct, 3),
            },
            # Tendencia
            "trend": {
                "direction": tr.direction,
                "score":     tr.score,
            },
            # Liquidez / flujo
            "oi_velocity": round(s["oi_vel"], 2),
            "cvd":         round(s["cvd"], 1),
            "ob_imbalance": round(s["ob_imbal"], 3),
            # Score breakdown
            "score_breakdown": {
                "absorption": opp.abs_pts,
                "regime":     opp.regime_pts,
                "trend":      opp.trend_pts,
                "liquidity":  opp.liq_pts,
            },
            "reasons": opp.reasons[:3],
        })

    items.sort(key=lambda x: x["score"], reverse=True)
    return {
        "status": "ok",
        "symbols_monitored": len(_signals),
        "signals_above_zero": len(items),
        "signals": items[:limit],
    }


def _tool_get_account() -> dict:
    """Estado de la cuenta."""
    st = _account.state
    b  = st.balance
    positions = st.open_positions()

    return {
        "connected":       st.connected,
        "equity":          round(b.total_equity,      4),
        "wallet_balance":  round(b.wallet_balance,    4),
        "available":       round(b.available_balance, 4),
        "used_margin":     round(b.used_margin,       4),
        "margin_pct":      round(b.margin_pct,        2),
        "unrealized_pnl":  round(b.unrealized_pnl,   4),
        "daily_pnl":       round(st.daily_pnl,        4),
        "open_positions":  len(positions),
        "error":           st.error or None,
    }


def _tool_get_positions() -> dict:
    """Posiciones activas con datos para gestión."""
    positions = []
    for pos in _account.state.open_positions():
        entry  = pos.entry_price
        mark   = pos.mark_price if pos.mark_price > 0 else entry
        is_long = pos.is_long
        tp     = pos.take_profit
        sl     = pos.stop_loss

        # Progress hacia TP (0 = entrada, 1 = TP)
        if tp > 0 and entry > 0:
            tp_dist = abs(tp - entry)
            if tp_dist > 0:
                if is_long:
                    progress = (mark - entry) / tp_dist
                else:
                    progress = (entry - mark) / tp_dist
            else:
                progress = 0.0
        else:
            progress = 0.0

        # Fees acumuladas
        notional    = pos.size * entry
        fees_paid   = notional * 0.00055 * 2  # entry fee ya pagada
        elapsed_h   = (time.time() - (pos.created_time / 1000)) / 3600 if pos.created_time > 0 else 0
        funding_acc = notional * abs(pos.mark_price * 0 + 0.0001) * max(0, elapsed_h / 8)
        net_upnl    = pos.unrealized_pnl - fees_paid - funding_acc

        # Señales actuales del símbolo (si disponibles)
        sig = _signals.get(pos.symbol, {})
        opp = sig.get("opp")
        ab  = sig.get("absorption")
        mo_score = opp.score if opp else 0
        ab_side  = ab.side if ab else "NEUTRAL"

        positions.append({
            "symbol":         pos.symbol.replace("USDT", ""),
            "full_sym":       pos.symbol,
            "side":           pos.side,
            "direction":      "LONG" if is_long else "SHORT",
            "qty":            pos.size,
            "entry":          round(entry, 6),
            "mark":           round(mark, 6),
            "sl":             round(sl, 6),
            "tp":             round(tp, 6),
            "leverage":       pos.leverage,
            "notional":       round(notional, 2),
            "margin":         round(pos.margin, 2),
            # PnL
            "gross_upnl":     round(pos.unrealized_pnl, 4),
            "net_upnl":       round(net_upnl, 4),
            "pnl_pct":        round(pos.pnl_pct, 2),
            # Progress
            "progress_pct":   round(progress * 100, 1),
            "progress":       round(progress, 3),
            # Riesgo
            "liq_price":      round(pos.liquidation_price, 6),
            "liq_dist_pct":   round(pos.distance_to_liq_pct, 2),
            # Señales actuales
            "current_score":  mo_score,
            "absorption_side": ab_side,
        })

    return {
        "count":     len(positions),
        "positions": positions,
    }


def _tool_get_symbol_data(symbol: str) -> dict:
    """Datos profundos para análisis de momentum y decisión de cierre."""
    sym  = symbol.upper()
    if not sym.endswith("USDT"):
        sym += "USDT"

    ms = _market.states.get(sym)
    if not ms:
        return {"error": f"Símbolo {sym} no monitoreado"}

    # CVD por velas (dirección del flujo)
    candles = list(ms.cvd_candles)[-10:]
    cvd_candles = [
        {
            "bull": round(c.buy_vol, 1),
            "bear": round(c.sell_vol, 1),
            "delta": round(c.delta, 1),
            "direction": "▲" if c.delta > 0 else "▼",
            "delta_pct": round(c.delta_pct, 1),
        }
        for c in candles
    ]

    # Price momentum (últimas 5 muestras de precio)
    ph = list(ms._price_history)[-6:]
    price_samples = [round(p, 6) for _, p in ph]
    if len(price_samples) >= 2:
        price_direction = "UP" if price_samples[-1] > price_samples[0] else "DOWN"
        price_change_pct = round((price_samples[-1] - price_samples[0]) / price_samples[0] * 100, 4) if price_samples[0] > 0 else 0
    else:
        price_direction = "UNKNOWN"
        price_change_pct = 0

    # Señales del símbolo
    sig = _signals.get(sym, {})
    opp = sig.get("opp")
    ab  = sig.get("absorption")
    tr  = sig.get("trend")
    rg  = sig.get("regime")

    # Liquidaciones recientes
    liqs = list(ms.liquidations)[-5:]
    recent_liqs = [
        {
            "side":     l.position_type,
            "notional": round(l.notional, 1),
            "price":    round(l.price, 6),
        }
        for l in liqs
    ]

    # Top orderbook
    top_bids = [(round(p, 6), round(q, 2)) for p, q in ms.orderbook.top_bids(5)]
    top_asks = [(round(p, 6), round(q, 2)) for p, q in ms.orderbook.top_asks(5)]

    return {
        "symbol":       sym,
        "price":        round(ms.ticker.last_price, 6),
        "bid":          round(ms.orderbook.best_bid, 6),
        "ask":          round(ms.orderbook.best_ask, 6),
        "spread_pct":   round(ms.orderbook.spread / ms.ticker.last_price * 100, 4) if ms.ticker.last_price > 0 else 0,
        "funding_rate": round(ms.ticker.funding_rate, 4),
        "open_interest": round(ms.ticker.open_interest, 2),
        "oi_velocity":  round(ms.oi_velocity, 3),
        # CVD
        "cvd_total":    round(ms.cvd, 1),
        "cvd_candles":  cvd_candles,
        "cvd_summary":  f"{sum(1 for c in candles if c.delta > 0)}/{len(candles)} velas alcistas (últimas {len(candles)})",
        # Momentum de precio
        "price_samples":    price_samples,
        "price_direction":  price_direction,
        "price_change_pct": price_change_pct,
        # Orderbook
        "ob_imbalance":  round(ms.orderbook.imbalance, 3),
        "ob_bid_wall":   round(ms.orderbook.bid_wall, 1),
        "ob_ask_wall":   round(ms.orderbook.ask_wall, 1),
        "top_bids":      top_bids,
        "top_asks":      top_asks,
        # Señales
        "score":         opp.score if opp else 0,
        "direction":     opp.direction if opp else "NEUTRAL",
        "ab_side":       ab.side if ab else "NEUTRAL",
        "ab_score":      ab.score if ab else 0,
        "trend_dir":     tr.direction if tr else "NEUTRAL",
        "trend_score":   tr.score if tr else 0,
        "regime":        rg.regime if rg else "UNKNOWN",
        "atr":           round(sig.get("atr", 0), 6),
        "rsi":           round(sig.get("rsi", 50), 1),
        # Liquidaciones
        "recent_liquidations": recent_liqs,
    }


def _tool_get_session_config() -> dict:
    """Lee la configuración de sesión desde /tmp/qts_session.json."""
    from pathlib import Path
    cfg_path = Path("/tmp/qts_session.json")
    if not cfg_path.exists():
        return {"goal": 1.0, "max_loss": 0.30, "status": "no_session",
                "note": "Sin sesión activa. Usa la pestaña Extractor para configurar."}
    try:
        cfg = json.loads(cfg_path.read_text())
        return {
            "goal":     cfg.get("goal",     1.0),
            "max_loss": cfg.get("max_loss", 0.30),
            "status":   "ok",
        }
    except Exception as e:
        return {"goal": 1.0, "max_loss": 0.30, "status": f"error: {e}"}


async def _tool_place_order(args: dict) -> dict:
    """Ejecuta una orden de mercado con SL/TP."""
    symbol   = args["symbol"].upper()
    if not symbol.endswith("USDT"):
        symbol += "USDT"
    side     = args["side"]       # "Buy" o "Sell"
    qty      = float(args["qty"])
    sl_price = float(args["sl_price"])
    tp_price = float(args["tp_price"])
    leverage = int(args.get("leverage", 10))

    # Validaciones básicas
    if side not in ("Buy", "Sell"):
        return {"success": False, "error": "side debe ser 'Buy' o 'Sell'"}
    if qty <= 0:
        return {"success": False, "error": "qty debe ser > 0"}

    # Redondear qty al step del instrumento
    qty = _exec.round_qty(symbol, qty)
    if qty <= 0:
        return {"success": False, "error": "qty = 0 tras redondear (demasiado pequeño)"}

    ok, reason = _exec.validate_order(symbol, qty, args.get("entry_price", 0) or _market.states.get(symbol, type("x", (), {"ticker": type("t", (), {"last_price": 0})()})()).ticker.last_price)
    if not ok:
        return {"success": False, "error": reason}

    from core.order_model import OrderRequest
    req = OrderRequest(
        symbol      = symbol,
        side        = side,
        qty         = qty,
        order_type  = "Market",
        entry_price = _market.states[symbol].ticker.last_price if symbol in _market.states else 0,
        sl_price    = sl_price,
        tp_price    = tp_price,
        leverage    = leverage,
    )

    try:
        # Configurar leverage
        await _exec.set_leverage(symbol, leverage)
        await asyncio.sleep(0.3)

        # Colocar orden
        result = await _exec.place_market_bracket(req)
        if not result.success:
            return {"success": False, "error": result.error_msg}

        # Confirmar SL/TP
        await asyncio.sleep(0.5)
        await _exec.set_sl_tp(symbol, sl=sl_price, tp=tp_price, side=side)

        entry = req.entry_price
        tp_dist = abs(tp_price - entry)
        sl_dist = abs(sl_price - entry)
        fee_rt  = entry * 0.00055 * 2
        notional = qty * entry

        res = {
            "success":   True,
            "order_id":  result.order_id,
            "symbol":    symbol,
            "side":      side,
            "qty":       qty,
            "entry":     round(entry, 6),
            "sl":        round(sl_price, 6),
            "tp":        round(tp_price, 6),
            "leverage":  leverage,
            "notional":  round(notional, 2),
            "margin":    round(notional / leverage, 2),
            "rr":        round(tp_dist / sl_dist, 2) if sl_dist > 0 else 0,
            "fees_est":  round(notional * 0.0011, 4),
            "net_goal":  round(qty * tp_dist - notional * 0.0011, 4),
        }
        _log_action(
            f"ORDEN: {side.upper()} {symbol} qty={qty} "
            f"entry={round(entry,6)} SL={round(sl_price,6)} TP={round(tp_price,6)} "
            f"RR={res['rr']} net_goal=${res['net_goal']:.4f}"
        )
        return res
    except Exception as e:
        _log_action(f"ERROR place_order {symbol}: {e}")
        return {"success": False, "error": str(e)}


async def _tool_close_position(symbol: str) -> dict:
    """Cierra posición a mercado."""
    sym = symbol.upper()
    if not sym.endswith("USDT"):
        sym += "USDT"

    pos = _account.state.positions.get(sym)
    if not pos or pos.size <= 0:
        return {"success": False, "error": f"No hay posición activa en {sym}"}

    try:
        result = await _exec.close_position(sym, pos.size, pos.side)
        if result.success:
            _log_action(
                f"CERRAR: {sym} qty={pos.size} upnl=${pos.unrealized_pnl:.4f}"
            )
            return {
                "success":    True,
                "symbol":     sym,
                "qty_closed": pos.size,
                "side":       pos.side,
                "upnl_at_close": round(pos.unrealized_pnl, 4),
                "msg":        "Posición cerrada a mercado",
            }
        _log_action(f"ERROR cerrar {sym}: {result.error_msg}")
        return {"success": False, "error": result.error_msg}
    except Exception as e:
        _log_action(f"ERROR cerrar {sym}: {e}")
        return {"success": False, "error": str(e)}


async def _tool_modify_sl_tp(args: dict) -> dict:
    """Modifica SL y/o TP de una posición."""
    sym = args["symbol"].upper()
    if not sym.endswith("USDT"):
        sym += "USDT"

    sl = float(args.get("sl_price", 0))
    tp = float(args.get("tp_price", 0))

    pos = _account.state.positions.get(sym)
    if not pos or pos.size <= 0:
        return {"success": False, "error": f"No hay posición activa en {sym}"}

    if sl <= 0 and tp <= 0:
        return {"success": False, "error": "Debes especificar al menos sl_price o tp_price"}

    try:
        ok = await _exec.set_sl_tp(sym, sl=sl, tp=tp, side=pos.side)
        sl_s = f"SL={sl}" if sl > 0 else ""
        tp_s = f"TP={tp}" if tp > 0 else ""
        _log_action(f"MODIFY: {sym} {sl_s} {tp_s}".strip())
        return {
            "success": ok,
            "symbol":  sym,
            "new_sl":  sl if sl > 0 else "sin cambio",
            "new_tp":  tp if tp > 0 else "sin cambio",
        }
    except Exception as e:
        _log_action(f"ERROR modify_sl_tp {sym}: {e}")
        return {"success": False, "error": str(e)}


# ─── Main ──────────────────────────────────────────────────────────────────────

async def run_server() -> None:
    """Inicia el thread de streams y luego el servidor MCP en stdio."""
    # Arrancar streams en thread daemon
    t = threading.Thread(target=_run_background, daemon=True)
    t.start()

    # Dar tiempo al loop background para arrancar antes de aceptar tools
    await asyncio.sleep(2)

    # Arrancar servidor MCP (stdio — Claude Code se conecta via stdin/stdout)
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        init_opts = InitializationOptions(
            server_name    = "qts-trading",
            server_version = "1.0.0",
            capabilities   = server.get_capabilities(
                notification_options = NotificationOptions(),
                experimental_capabilities = {},
            ),
        )
        await server.run(read_stream, write_stream, init_opts)


if __name__ == "__main__":
    asyncio.run(run_server())
