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
from web.sentiment import compute_pilot
from web.eta_estimator import compute_eta
from web.level_tracker import tracker as _level_tracker
from web.energy_tracker import compute_energy

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

# Runtime stats por pos_key: peaks, MFE/MAE, sample_count, signals_at_open.
# Se alimenta tick a tick durante el snapshot loop y se archiva al cerrarse la
# posición (antes del forget) en closed_trade_analysis.
_pos_runtime:  dict = {}     # pos_key → dict con peaks/pnl/signals
_pos_last:     dict = {}     # pos_key → último snapshot info (para archivar al cierre)

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


def _archive_closed_trade(pos_key: str) -> None:
    """Persiste el snapshot completo del trade al detectarse su cierre.
    Captura zonas+histograma del tracker antes del forget y dispara el
    análisis IA estilo mentor en background."""
    rt   = _pos_runtime.get(pos_key)
    last = _pos_last.get(pos_key, {})
    if not rt:
        return
    zsnap = _zone_tracker().final_snapshot(pos_key) or {}
    opened_ms = int(rt.get("opened_at_ms", 0))
    closed_ms = int(time.time() * 1000)
    duration_s = max(0, (closed_ms - opened_ms) // 1000) if opened_ms > 0 else 0
    trade_id = uuid.uuid4().hex[:12]

    record = {
        "id":           trade_id,
        "pos_key":      pos_key,
        "symbol":       last.get("symbol", pos_key.split("_")[0]),
        "side":         last.get("side", pos_key.split("_")[-1] if "_" in pos_key else ""),
        "direction":    last.get("direction", ""),
        "opened_at":    opened_ms,
        "closed_at":    closed_ms,
        "duration_s":   duration_s,
        "entry_price":  rt.get("entry_price", 0),
        "last_mark":    last.get("last_mark", 0),
        "sl_price":     last.get("sl") or rt.get("sl_initial", 0),
        "tp_price":     last.get("tp") or rt.get("tp_initial", 0),
        "qty":          last.get("qty") or rt.get("qty_initial", 0),
        "leverage":     rt.get("leverage", 1),
        "max_mark":     rt.get("max_mark", 0),
        "min_mark":     rt.get("min_mark", 0),
        "max_pnl":      rt.get("max_pnl", 0),
        "min_pnl":      rt.get("min_pnl", 0),
        "last_pnl":     last.get("last_pnl", 0),
        "sample_count": rt.get("samples", 0),
        "zones":        zsnap,
        "signals_open": rt.get("signals_open", {}),
        "signals_close": last.get("signals_close", {}),
    }
    try:
        from core.db import save_closed_trade_analysis
        save_closed_trade_analysis(record)
    except Exception as e:
        log.error("save_closed_trade_analysis fallo: %s", e)
        return

    # Disparar análisis mentor IA en background (no bloquea el snapshot loop).
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_run_mentor_analysis(trade_id, record))
    except RuntimeError:
        log.debug("no event loop running — IA mentor diferido")


def _build_mentor_prompt(rec: dict) -> str:
    """Construye un prompt rico, con datos reales del seguimiento del trade."""
    sym       = rec.get("symbol", "?")
    direction = rec.get("direction") or ("LONG" if rec.get("side") == "Buy" else "SHORT")
    lev       = rec.get("leverage", 1)
    entry     = rec.get("entry_price", 0) or rec.get("avg_entry", 0)
    exit_p    = rec.get("exit_price") or rec.get("last_mark", 0)
    sl, tp    = rec.get("sl_price", 0), rec.get("tp_price", 0)
    qty       = rec.get("qty", 0)
    dur_s     = int(rec.get("duration_s", 0))
    dur_fmt   = format_elapsed(dur_s)
    pnl       = rec.get("bybit_pnl") or rec.get("last_pnl", 0)
    fees      = rec.get("total_fees", 0)
    mfe       = rec.get("max_pnl", 0)
    mae       = rec.get("min_pnl", 0)
    max_mark  = rec.get("max_mark", 0)
    min_mark  = rec.get("min_mark", 0)
    samples   = rec.get("sample_count", 0)

    # Distribución por zonas
    zones    = rec.get("zones") or {}
    z_list   = zones.get("zones") or []
    life_s   = zones.get("wall_clock_elapsed_s") or zones.get("total_seconds") or dur_s
    cur_zone = zones.get("current_zone", "?")

    zones_text_lines = []
    for z in sorted(z_list, key=lambda x: -x.get("seconds", 0))[:6]:
        zones_text_lines.append(
            f"  · {z.get('label','?')}: {format_elapsed(z.get('seconds',0))} "
            f"({z.get('pct_of_life',0):.0f}% · racha máx {format_elapsed(z.get('max_streak',0))} · "
            f"{z.get('visits',0)} visitas)"
        )
    zones_text = "\n".join(zones_text_lines) or "  (sin datos de zona)"

    # Histograma fino — buckets donde más estancado (top 5)
    hist        = zones.get("histogram") or []
    hist_buckets= zones.get("hist_buckets") or len(hist) or 40
    dwell_text  = "  (sin datos)"
    if hist and any(h > 0 for h in hist):
        idx_sorted = sorted(range(len(hist)), key=lambda i: -hist[i])[:5]
        parts = []
        for i in idx_sorted:
            if hist[i] <= 0: continue
            # Bucket i mapea linealmente entre SL→TP
            if sl > 0 and tp > 0:
                price = sl + (tp - sl) * (i + 0.5) / hist_buckets
            else:
                price = 0
            pct_in_zone = 100.0 * (i + 0.5) / hist_buckets
            parts.append(f"  · @{price:.4f} (~{pct_in_zone:.0f}% del rango SL→TP): {format_elapsed(hist[i])}")
        dwell_text = "\n".join(parts)

    sig_o = rec.get("signals_open") or {}
    sig_c = rec.get("signals_close") or {}

    # Movimiento del precio durante la vida del trade
    if entry > 0:
        upside   = (max_mark - entry) / entry * 100 if max_mark > 0 else 0
        downside = (min_mark - entry) / entry * 100 if min_mark > 0 else 0
    else:
        upside = downside = 0

    # Diagnósticos pre-computados (heurística simple para guiar al modelo)
    diagnostics = []
    if dur_s > 4 * 3600:
        diagnostics.append(f"Duración alta ({dur_fmt}) — riesgo de fatiga de tesis o funding negativo.")
    if mfe > 0 and pnl < mfe * 0.5:
        diagnostics.append(
            f"MFE no capturado: llegó a +${mfe:.2f} pero cerró con ${pnl:.2f} "
            f"(reciste {((mfe-pnl)/max(mfe,1e-6))*100:.0f}% del peak)."
        )
    if mae < 0 and abs(mae) > abs(pnl) and pnl >= 0:
        diagnostics.append(f"Aguantó drawdown profundo (mae=${mae:.2f}) antes de salir verde.")
    if cur_zone in ("below_sl", "sl_entry") and pnl < 0:
        diagnostics.append("Cerró en zona perdedora (cerca de SL o entre SL y entry).")
    if direction == "LONG" and exit_p > 0 and entry > 0 and exit_p < entry and max_mark > entry * 1.005:
        diagnostics.append("LONG verde durante el trade pero cerró por debajo de entry — devolvió ganancia.")
    if not diagnostics:
        diagnostics.append("Sin alertas heurísticas obvias.")
    diag_text = "\n".join(f"  - {d}" for d in diagnostics)

    return f"""Eres un mentor experto de trading de futuros perpetuos (Bybit, USDT-M). Analiza este trade REAL con datos detallados de seguimiento minuto a minuto. Tu objetivo es enseñar al trader a no repetir errores y a repetir aciertos. Sé directo, específico, accionable. Nada de generalidades.

═══ TRADE ═══
Par: {sym}   Dirección: {direction} {lev}x   Cantidad: {qty}
Entrada: {entry:.6f}   Salida: {exit_p:.6f}
SL: {sl:.6f}   TP: {tp:.6f}
Duración: {dur_fmt} ({dur_s}s · {samples} muestras de seguimiento)
PnL final: ${pnl:.4f}   Fees: ${fees:.4f}

═══ EXTREMOS DURANTE LA VIDA DEL TRADE ═══
Mark máximo: {max_mark:.6f} ({upside:+.3f}% vs entry)
Mark mínimo: {min_mark:.6f} ({downside:+.3f}% vs entry)
MFE (peak PnL ganador):  ${mfe:.4f}
MAE (peor PnL no realizado): ${mae:.4f}

═══ TIEMPO POR ZONAS (cronotopología SL→TP) ═══
Vida total observada: {format_elapsed(life_s)}   Zona al cierre: {cur_zone}
Distribución (top zonas):
{zones_text}

═══ DWELL FINO (top 5 buckets donde el precio se quedó más tiempo) ═══
{dwell_text}

═══ SEÑALES AL ABRIR vs CERRAR ═══
Apertura: opp_score={sig_o.get('opp_score',0)}  absorción={sig_o.get('ab_side','?')}  tendencia={sig_o.get('trend_dir','?')}  régimen={sig_o.get('regime','?')}  rsi={sig_o.get('rsi',0)}
Cierre:   opp_score={sig_c.get('opp_score',0)}  absorción={sig_c.get('ab_side','?')}  tendencia={sig_c.get('trend_dir','?')}  régimen={sig_c.get('regime','?')}  rsi={sig_c.get('rsi',0)}

═══ DIAGNÓSTICOS PRE-COMPUTADOS ═══
{diag_text}

Responde SOLO con este JSON (sin markdown):
{{
  "veredicto": "win|loss|breakeven",
  "score": 1-10,
  "resumen": "1-2 oraciones precisas con lo más importante",
  "fortalezas": ["frase corta accionable", "..."],
  "debilidades": ["frase corta con causa concreta", "..."],
  "momentos_clave": [
    {{"cuando": "ej. 'minuto 0-10' o 'tras tocar +1R'", "que_paso": "...", "que_hacer_distinto": "..."}}
  ],
  "leccion_principal": "la única regla que el trader debe interiorizar de este trade",
  "proximo_trade": ["sugerencia concreta 1", "sugerencia concreta 2", "..."],
  "patron_recurrente": "si detectas un patrón de comportamiento típico del trader (o null)",
  "alertas": ["riesgo específico a vigilar la próxima vez", "..."]
}}"""


async def _run_mentor_analysis(trade_id: str, record: dict) -> None:
    """Llama al LLM con prompt mentor y persiste el resultado en DB."""
    try:
        # Enriquecer con datos de Bybit closed-pnl (fees, exit_price reales)
        await _enrich_with_bybit_closed_pnl(trade_id, record)

        from core.ai_strategy import AIStrategyAgent
        agent = AIStrategyAgent()
        client, _model_default, use_json = agent._make_client_and_model()
        # Mentor task = barato + rápido. Override del modelo de estrategia.
        mentor_model = getattr(settings, "ai_mentor_model", None) or "gpt-4o-mini"

        prompt = _build_mentor_prompt(record)
        kwargs: dict = {
            "model": mentor_model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.4,
            "max_tokens": 900,
        }
        if use_json:
            kwargs["response_format"] = {"type": "json_object"}

        resp = await client.chat.completions.create(**kwargs)
        raw  = resp.choices[0].message.content.strip()

        import json as _json
        try:
            analysis = _json.loads(raw)
        except Exception:
            import re
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            analysis = _json.loads(m.group()) if m else {"resumen": raw}

        from core.db import update_closed_trade_ai
        update_closed_trade_ai(trade_id, analysis, mentor_model)
        log.warning("Mentor IA ✓ %s (%s) score=%s",
                    record.get("symbol"), trade_id, analysis.get("score", "?"))
    except Exception as e:
        log.error("_run_mentor_analysis %s: %s", trade_id, e)


async def _enrich_with_bybit_closed_pnl(trade_id: str, record: dict) -> None:
    """Consulta closed-pnl de Bybit para el símbolo y enriquece el registro
    con fees reales y exit_price. Coincide por ventana temporal."""
    try:
        sym       = record.get("symbol", "")
        opened_ms = int(record.get("opened_at", 0))
        closed_ms = int(record.get("closed_at", 0))
        if not sym or not opened_ms:
            return
        data = await _exec._get("/v5/position/closed-pnl", {
            "category": "linear",
            "symbol":   sym,
            "limit":    "20",
        })
        items = data.get("result", {}).get("list", []) or []
        # Match: posiciones cuya updatedTime está cerca de closed_ms (±5 min)
        best = None
        best_dt = 10 * 60 * 1000
        for x in items:
            up = int(x.get("updatedTime") or 0)
            dt = abs(up - closed_ms)
            if dt < best_dt:
                best, best_dt = x, dt
        if not best:
            return
        pnl       = float(best.get("closedPnl") or 0)
        cum_entry = float(best.get("cumEntryValue") or 0)
        cum_exit  = float(best.get("cumExitValue") or 0)
        fees      = abs(cum_entry - cum_exit - pnl) if (cum_entry or cum_exit) else 0.0
        avg_entry = float(best.get("avgEntryPrice") or record.get("entry_price", 0))
        avg_exit  = float(best.get("avgExitPrice")  or record.get("last_mark", 0))
        # Tomar opened_at REAL de Bybit (createdTime de la closed-pnl) si nuestro
        # registro no lo tenía o difiere
        bybit_open = int(best.get("createdTime") or 0)
        if bybit_open > 0:
            record["opened_at"] = bybit_open
            record["duration_s"] = max(0, (closed_ms - bybit_open) // 1000)

        record["bybit_pnl"]  = pnl
        record["total_fees"] = fees
        record["exit_price"] = avg_exit
        record["avg_entry"]  = avg_entry

        from core.db import update_closed_trade_bybit_fields
        update_closed_trade_bybit_fields(trade_id, pnl, fees, avg_exit, avg_entry)
        # También corregir opened_at/duration si cambió
        if bybit_open > 0:
            try:
                con = get_conn_quick()
                con.execute(
                    "UPDATE closed_trade_analysis SET opened_at = ?, duration_s = ? WHERE id = ?",
                    (bybit_open, record["duration_s"], trade_id),
                )
                con.close()
            except Exception as e:
                log.debug("update opened_at en archive: %s", e)
    except Exception as e:
        log.debug("_enrich_with_bybit_closed_pnl: %s", e)


def get_conn_quick():
    from core.db import get_connection
    return get_connection()


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

                # Touch counter — barato (O(tracks) por símbolo)
                if live_mark > 0:
                    try:
                        _level_tracker.touch_tick(sym, live_mark)
                    except Exception:
                        pass

                # Duración: confiar en createdTime de Bybit cuando es razonable.
                # Sobrevive reinicios del server porque createdTime viene del exchange.
                # Fallback: _pos_first_seen (server-side) solo si Bybit no da timestamp válido.
                pos_key       = f"{sym}_{pos.side}"
                bybit_elapsed = max(0.0, time.time() - pos.created_time / 1000) if pos.created_time > 0 else 0.0
                if 0 < bybit_elapsed < 30 * 24 * 3600:
                    elapsed_override = bybit_elapsed
                else:
                    elapsed_override = time.time() - _pos_first_seen.setdefault(pos_key, time.time())

                metrics = calc_position_metrics(pos, live_mark=live_mark,
                                                elapsed_override=elapsed_override)

                # Sampler de zona (cronotopología): clasifica el mark y acumula tiempo.
                # Corre dentro del snapshot loop (1 Hz) — suficiente para zonas con
                # duración de minutos/horas; el sampler dedicado complementa a 0.5 Hz.
                opened_hint = pos.created_time / 1000.0 if pos.created_time > 0 else None
                _zone_tracker().sample(pos_key, metrics["geometry"],
                                       live_mark or pos.entry_price,
                                       opened_at_hint=opened_hint)
                zones_summary = _zone_tracker().summary(pos_key)

                sig = _signals.get(sym, {})
                opp = sig.get("opp")
                ab  = sig.get("absorption")
                tr  = sig.get("trend")
                rg  = sig.get("regime")

                # Runtime stats: peaks, MFE/MAE, snapshot de señales al abrir.
                # Estos datos se archivan al detectar el cierre de la posición.
                cur_mark = live_mark or pos.entry_price
                cur_pnl  = float(metrics.get("net_pnl_now") or 0.0)
                rt = _pos_runtime.get(pos_key)
                if rt is None:
                    rt = {
                        "opened_at_ms": int(pos.created_time) if pos.created_time > 0 else int(time.time() * 1000),
                        "entry_price": pos.entry_price,
                        "sl_initial":  pos.stop_loss,
                        "tp_initial":  pos.take_profit,
                        "leverage":    float(pos.leverage),
                        "qty_initial": pos.size,
                        "max_mark":    cur_mark,
                        "min_mark":    cur_mark,
                        "max_pnl":     cur_pnl,
                        "min_pnl":     cur_pnl,
                        "samples":     0,
                        "signals_open": {
                            "opp_score": opp.score if opp else 0,
                            "ab_side":   ab.side   if ab  else "NEUTRAL",
                            "trend_dir": tr.direction if tr else "NEUTRAL",
                            "regime":    rg.regime if rg else "UNKNOWN",
                            "atr":       round(sig.get("atr", 0), 6),
                            "rsi":       round(sig.get("rsi", 50), 1),
                        },
                    }
                    _pos_runtime[pos_key] = rt
                else:
                    if cur_mark > rt["max_mark"]: rt["max_mark"] = cur_mark
                    if cur_mark < rt["min_mark"] or rt["min_mark"] == 0: rt["min_mark"] = cur_mark
                    if cur_pnl > rt["max_pnl"]: rt["max_pnl"] = cur_pnl
                    if cur_pnl < rt["min_pnl"]: rt["min_pnl"] = cur_pnl
                rt["samples"] += 1

                # Snapshot ligero para archivar si la posición desaparece.
                _pos_last[pos_key] = {
                    "symbol":    sym,
                    "side":      pos.side,
                    "direction": "LONG" if pos.is_long else "SHORT",
                    "last_mark": cur_mark,
                    "last_pnl":  cur_pnl,
                    "qty":       pos.size,
                    "sl":        pos.stop_loss,
                    "tp":        pos.take_profit,
                    "signals_close": {
                        "opp_score": opp.score if opp else 0,
                        "ab_side":   ab.side   if ab  else "NEUTRAL",
                        "trend_dir": tr.direction if tr else "NEUTRAL",
                        "regime":    rg.regime if rg else "UNKNOWN",
                        "atr":       round(sig.get("atr", 0), 6),
                        "rsi":       round(sig.get("rsi", 50), 1),
                    },
                }

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

        # Detectar cierre: posición que estaba siendo trackeada y desaparece.
        # Antes de olvidarla, archivar el snapshot completo en DB para análisis.
        closed_keys = [k for k in list(_pos_runtime) if k not in active_keys]
        for k in closed_keys:
            try:
                _archive_closed_trade(k)
            except Exception as e:
                log.error("archive_closed_trade %s: %s", k, e)
            finally:
                _pos_runtime.pop(k, None)
                _pos_last.pop(k, None)

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
        from core.db import initialize_db
        initialize_db()
    except Exception as e:
        log.warning("initialize_db: %s", e)

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


@app.get("/api/closed-trades")
async def api_closed_trades(limit: int = 100):
    """Archivo enriquecido de trades cerrados con contexto completo
    (zonas, histograma, peaks, MFE/MAE, señales al abrir y cerrar)."""
    try:
        from core.db import get_closed_trade_analyses
        items = get_closed_trade_analyses(limit=min(int(limit), 500))
        for it in items:
            it["duration_fmt"] = format_elapsed(it.get("duration_s", 0))
            entry = it.get("entry_price", 0)
            qty   = it.get("qty", 0)
            if entry > 0 and qty > 0:
                dirn = 1 if (it.get("direction") == "LONG" or it.get("side") == "Buy") else -1
                it["mfe_usd"] = round((it["max_mark"] - entry) * qty * dirn, 4) if dirn == 1 else round((entry - it["min_mark"]) * qty, 4)
                it["mae_usd"] = round((it["min_mark"] - entry) * qty * dirn, 4) if dirn == 1 else round((entry - it["max_mark"]) * qty, 4)
            else:
                it["mfe_usd"] = it["mae_usd"] = 0.0
        return JSONResponse({"trades": items})
    except Exception as e:
        log.error("api_closed_trades: %s", e)
        return JSONResponse({"trades": [], "error": str(e)})


@app.get("/api/history")
async def api_history(limit: int = 50):
    """Historial de trades cerrados — fusiona closed-pnl de Bybit con el archivo
    enriquecido (closed_trade_analysis) que sí trae fechas reales, MFE/MAE,
    zonas, señales al abrir/cerrar y análisis IA mentor cuando ya se generó.
    """
    try:
        # 1) Archivo enriquecido del dashboard (fuente de verdad para dates+IA)
        from core.db import get_closed_trade_analyses
        archive = get_closed_trade_analyses(limit=max(limit, 200))
        # Index por (symbol, closed_at_minuto) para matching tolerante al jitter
        arch_by_key: dict = {}
        for a in archive:
            key = (a.get("symbol", ""), (int(a.get("closed_at", 0)) // 60000))
            arch_by_key[key] = a

        data = await _exec._get("/v5/position/closed-pnl", {
            "category": "linear",
            "limit":    str(min(limit, 200)),
        })
        items = data.get("result", {}).get("list", [])
        history = []
        consumed_archives = set()
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

            entry_h = {
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
            }

            # Match con archivo enriquecido del dashboard (ventana ±2 min)
            ck = close_ts // 60000
            matched = None
            for delta in (0, -1, 1, -2, 2):
                cand = arch_by_key.get((sym, ck + delta))
                if cand and cand["id"] not in consumed_archives:
                    matched = cand
                    consumed_archives.add(cand["id"])
                    break

            if matched:
                # Dashboard archive es la fuente de verdad para tiempos+contexto
                entry_h["id"]            = matched["id"]
                entry_h["open_ts"]       = matched["opened_at"] or open_ts
                entry_h["close_ts"]      = matched["closed_at"] or close_ts
                entry_h["duration_s"]    = matched["duration_s"] or duration_s
                entry_h["duration_fmt"]  = format_elapsed(entry_h["duration_s"])
                entry_h["max_mark"]      = matched["max_mark"]
                entry_h["min_mark"]      = matched["min_mark"]
                entry_h["max_pnl"]       = matched["max_pnl"]
                entry_h["min_pnl"]       = matched["min_pnl"]
                entry_h["sample_count"]  = matched["sample_count"]
                entry_h["sl_price"]      = matched["sl_price"]
                entry_h["tp_price"]      = matched["tp_price"]
                entry_h["zones"]         = matched["zones"]
                entry_h["signals_open"]  = matched["signals_open"]
                entry_h["signals_close"] = matched["signals_close"]
                entry_h["ai_analysis"]   = matched["ai_analysis"]
                entry_h["ai_model"]      = matched["ai_model"]
                entry_h["ai_generated_at"] = matched["ai_generated_at"]
            history.append(entry_h)

        # Trades del archivo que NO matchearon (no aparecen aún en closed-pnl o
        # son demasiado nuevos) — los agregamos arriba del listado.
        extras = []
        for a in archive:
            if a["id"] in consumed_archives:
                continue
            extras.append({
                "id":          a["id"],
                "symbol":      a["symbol"].replace("USDT", ""),
                "full_sym":    a["symbol"],
                "side":        a["side"],
                "direction":   a["direction"] or ("LONG" if a["side"] == "Buy" else "SHORT"),
                "qty":         a["qty"],
                "entry_price": a["avg_entry"] or a["entry_price"],
                "exit_price":  a["exit_price"] or a["last_mark"],
                "closed_pnl":  a["bybit_pnl"] or a["last_pnl"],
                "total_fees":  a["total_fees"],
                "leverage":    int(a["leverage"] or 1),
                "open_ts":     a["opened_at"],
                "close_ts":    a["closed_at"],
                "duration_s":  a["duration_s"],
                "duration_fmt": format_elapsed(a["duration_s"]),
                "max_mark":    a["max_mark"],
                "min_mark":    a["min_mark"],
                "max_pnl":     a["max_pnl"],
                "min_pnl":     a["min_pnl"],
                "sample_count": a["sample_count"],
                "sl_price":    a["sl_price"],
                "tp_price":    a["tp_price"],
                "zones":       a["zones"],
                "signals_open": a["signals_open"],
                "signals_close": a["signals_close"],
                "ai_analysis": a["ai_analysis"],
                "ai_model":    a["ai_model"],
                "ai_generated_at": a["ai_generated_at"],
                "from_archive": True,
            })
        # Mezclar: archivos huérfanos arriba, resto por close_ts desc
        history = sorted(extras + history, key=lambda h: -(h.get("close_ts") or 0))
        return JSONResponse({"history": history})
    except Exception as e:
        log.error("api_history: %s", e)
        return JSONResponse({"history": [], "error": str(e)})


@app.post("/api/mentor-analysis/{trade_id}")
async def api_mentor_analysis(trade_id: str):
    """Re-genera (o genera por primera vez) el análisis mentor IA para un trade
    archivado. Devuelve el análisis completo."""
    try:
        from core.db import get_closed_trade_by_id
        rec = get_closed_trade_by_id(trade_id)
        if not rec:
            return JSONResponse({"ok": False, "error": "trade no encontrado"}, status_code=404)
        await _run_mentor_analysis(trade_id, rec)
        # Recargar con resultado IA
        rec2 = get_closed_trade_by_id(trade_id) or rec
        return JSONResponse({"ok": True, "analysis": rec2.get("ai_analysis"),
                             "model": rec2.get("ai_model"),
                             "generated_at": rec2.get("ai_generated_at")})
    except Exception as e:
        log.error("api_mentor_analysis: %s", e)
        return JSONResponse({"ok": False, "error": str(e)})


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

    try:
        _level_tracker.record(sym, payload.get("levels") or [])
    except Exception:
        pass

    return JSONResponse(payload)


@app.get("/api/energy/{symbol}")
async def api_energy(
    symbol: str,
    view_min: float = 0.0,
    view_max: float = 0.0,
):
    """Energía instantánea + dirección por nivel estructural + vitalidad global."""
    sym = symbol.upper()
    if not sym.endswith("USDT"):
        sym += "USDT"
    ms = _market.states.get(sym)
    if not ms:
        return JSONResponse({"error": "symbol not streaming"}, status_code=404)

    price = ms.ticker.last_price or ms.orderbook.mid_price
    if view_min <= 0 or view_max <= 0 or view_max <= view_min:
        if price > 0:
            view_min = price * 0.98
            view_max = price * 1.02

    try:
        lmap = _liq_an.analyze(ms)
        levels = []
        for lv in lmap.levels:
            if view_min and (lv.price < view_min or lv.price > view_max):
                continue
            levels.append({"price": lv.price, "type": lv.level_type})
    except Exception:
        levels = []

    payload = compute_energy(ms, levels, view_min, view_max)
    if payload is None:
        return JSONResponse({"error": "no data"}, status_code=503)
    return JSONResponse(payload)


@app.get("/api/level_trails/{symbol}")
async def api_level_trails(
    symbol: str,
    view_min: float = 0.0,
    view_max: float = 0.0,
):
    """Trayectoria reciente de niveles estructurales dentro del viewport."""
    sym = symbol.upper()
    if not sym.endswith("USDT"):
        sym += "USDT"
    ms = _market.states.get(sym)
    if not ms:
        return JSONResponse({"trails": [], "error": "symbol not streaming"}, status_code=404)
    price = ms.ticker.last_price or ms.orderbook.mid_price
    if view_min <= 0 or view_max <= 0 or view_max <= view_min:
        if price <= 0:
            return JSONResponse({"trails": []})
        view_min = price * 0.98
        view_max = price * 1.02
    trails = _level_tracker.get_trails(sym, view_min, view_max)
    return JSONResponse({
        "symbol":   sym,
        "view_min": view_min,
        "view_max": view_max,
        "ts":       int(time.time() * 1000),
        "trails":   trails,
    })


@app.get("/api/eta/{symbol}")
async def api_eta(
    symbol: str,
    sl: float = 0.0,
    entry: float = 0.0,
    be: float = 0.0,
    tp: float = 0.0,
    is_long: bool = True,
    milestones: str = "",   # "25:79000,50:80000,75:81000"
):
    """Proyecta ETA a SL/BE/TP/milestones desde el precio actual."""
    sym = symbol.upper()
    if not sym.endswith("USDT"):
        sym += "USDT"
    ms = _market.states.get(sym)
    if not ms:
        return JSONResponse({"error": "symbol not streaming"}, status_code=404)

    ms_list = []
    for tok in (milestones or "").split(","):
        tok = tok.strip()
        if not tok or ":" not in tok:
            continue
        try:
            pct_s, px_s = tok.split(":", 1)
            ms_list.append({"pct": int(float(pct_s)), "price": float(px_s)})
        except Exception:
            continue

    geom = {
        "sl": sl, "entry": entry, "be": be, "tp": tp,
        "is_long": bool(is_long), "milestones": ms_list,
    }
    sig    = _signals.get(sym, {})
    rg     = sig.get("regime")
    regime = rg.regime if rg else "UNKNOWN"
    payload = compute_eta(ms, geom, regime=regime)
    if payload is None:
        return JSONResponse({"error": "no data"}, status_code=503)
    return JSONResponse(payload)


@app.get("/api/pilot/{symbol}")
async def api_pilot(symbol: str):
    """Instrumentos del piloto: presión, velocidad y tipo de carretera."""
    sym = symbol.upper()
    if not sym.endswith("USDT"):
        sym += "USDT"
    ms = _market.states.get(sym)
    if not ms:
        return JSONResponse({"error": "symbol not streaming"}, status_code=404)
    payload = compute_pilot(ms, _signals.get(sym))
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


@app.get("/api/suggest-levels/{symbol}")
async def api_suggest_levels(
    symbol: str,
    dir: str = "Buy",
    style: str = "medium",     # fast | medium | slow
):
    """Sugiere SL/TP basados en HVN/LVN, S/R, clusters de liquidación y ATR."""
    sym = symbol.upper()
    if not sym.endswith("USDT"):
        sym += "USDT"
    ms = _market.states.get(sym)
    if not ms:
        return JSONResponse({"error": "symbol not streaming"}, status_code=404)

    price = _mark_prices.get(sym) or ms.ticker.last_price or ms.orderbook.mid_price
    if price <= 0:
        return JSONResponse({"error": "no price"}, status_code=503)

    sig    = _signals.get(sym, {})
    atr    = float(sig.get("atr", 0) or 0)
    rsi    = float(sig.get("rsi", 50) or 50)
    regime = sig.get("regime")
    regime_name = regime.regime if regime else "UNKNOWN"
    is_long = (dir.lower() in ("buy", "long"))

    # ATR multipliers por estilo
    style = (style or "medium").lower()
    sl_mult = {"fast": 0.6, "medium": 1.2, "slow": 2.0}.get(style, 1.2)
    tp_mult = {"fast": 1.5, "medium": 3.0, "slow": 5.0}.get(style, 3.0)
    rr_min  = {"fast": 1.5, "medium": 2.0, "slow": 2.5}.get(style, 2.0)

    # Viewport amplio para capturar niveles relevantes (±3%)
    view_lo = price * 0.97
    view_hi = price * 1.03
    payload = build_liquidity_map(ms, _account.state.open_orders, view_lo, view_hi)
    if payload is None:
        return JSONResponse({"error": "no liquidity data"}, status_code=503)

    levels = payload.get("levels") or []
    liqs   = payload.get("liqs") or []

    # ── Clusters de liquidaciones (suma notional por bucket de 0.1%) ────────
    liq_clusters: list[dict] = []
    bucket_pct = 0.001
    bk: dict[float, float] = {}
    for lq in liqs:
        p = float(lq.get("price", 0))
        n = float(lq.get("notional", 0))
        if p <= 0 or n <= 0:
            continue
        key = round(p / (price * bucket_pct)) * (price * bucket_pct)
        bk[key] = bk.get(key, 0.0) + n
    for p, n in bk.items():
        if n >= 5000:   # cluster significativo
            liq_clusters.append({"price": round(p, 6), "notional": round(n, 0)})
    liq_clusters.sort(key=lambda x: x["price"])

    # ── Candidatos para SL (en contra) y TP (a favor) ───────────────────────
    if is_long:
        below = sorted([lv for lv in levels if lv["price"] < price], key=lambda lv: price - lv["price"])
        above = sorted([lv for lv in levels if lv["price"] > price], key=lambda lv: lv["price"] - price)
        sl_side, tp_side = below, above
    else:
        above = sorted([lv for lv in levels if lv["price"] > price], key=lambda lv: lv["price"] - price)
        below = sorted([lv for lv in levels if lv["price"] < price], key=lambda lv: price - lv["price"])
        sl_side, tp_side = above, below

    def pick_sl(candidates: list[dict]) -> tuple[float, str]:
        """Primer HVN/EQ relevante en contra, con buffer del 0.1%."""
        for lv in candidates:
            if lv["type"] in ("HVN", "EQ", "ROUND") and lv.get("strength", 0) >= 0.3:
                buffer_dir = -1 if is_long else 1
                px = lv["price"] * (1 + buffer_dir * 0.001)
                return px, f"{lv['type']} a {lv['price']:.4f} (fuerza {lv['strength']:.2f})"
        return 0.0, ""

    def pick_tp(candidates: list[dict]) -> tuple[float, str]:
        """HVN/EQ a favor, o liq cluster si hay; preferir el que respete rr_min."""
        for lv in candidates:
            if lv["type"] in ("HVN", "EQ", "ROUND") and lv.get("strength", 0) >= 0.25:
                return lv["price"], f"{lv['type']} a {lv['price']:.4f} (fuerza {lv['strength']:.2f})"
        return 0.0, ""

    sl_struct, sl_reason = pick_sl(sl_side)
    tp_struct, tp_reason = pick_tp(tp_side)

    # ── Fallback ATR si no hay estructura ───────────────────────────────────
    sl_atr = price - sl_mult * atr if is_long else price + sl_mult * atr
    tp_atr = price + tp_mult * atr if is_long else price - tp_mult * atr

    reasoning: list[str] = []
    if sl_struct > 0:
        sl_price = sl_struct
        reasoning.append(f"SL detrás de {sl_reason}")
    elif atr > 0:
        sl_price = sl_atr
        reasoning.append(f"SL por ATR×{sl_mult:.1f} (sin estructura cercana)")
    else:
        sl_pct_fallback = {"fast": 0.5, "medium": 1.0, "slow": 1.8}.get(style, 1.0) / 100
        sl_price = price * (1 - sl_pct_fallback) if is_long else price * (1 + sl_pct_fallback)
        reasoning.append(f"SL por % fijo {sl_pct_fallback*100:.1f}% (sin ATR ni niveles)")

    if tp_struct > 0:
        tp_price = tp_struct
        reasoning.append(f"TP en {tp_reason}")
    elif atr > 0:
        tp_price = tp_atr
        reasoning.append(f"TP por ATR×{tp_mult:.1f} (sin estructura cercana)")
    else:
        tp_pct_fallback = {"fast": 1.0, "medium": 2.0, "slow": 4.0}.get(style, 2.0) / 100
        tp_price = price * (1 + tp_pct_fallback) if is_long else price * (1 - tp_pct_fallback)
        reasoning.append(f"TP por % fijo {tp_pct_fallback*100:.1f}%")

    # ── Validar R:R y estirar TP si hace falta ──────────────────────────────
    risk   = abs(price - sl_price)
    reward = abs(tp_price - price)
    rr = (reward / risk) if risk > 0 else 0.0
    if rr < rr_min and risk > 0:
        # Reposicionar TP para alcanzar rr_min mínimo
        need = risk * rr_min
        tp_price = price + need if is_long else price - need
        reward   = need
        rr       = rr_min
        reasoning.append(f"TP extendido para asegurar R:R ≥ {rr_min}")

    sl_pct = abs(sl_price - price) / price * 100
    tp_pct = abs(tp_price - price) / price * 100

    # ── Estimación de "velocidad" del setup ─────────────────────────────────
    # Combina ATR%, régimen, densidad de liqs recientes y RSI extremos
    atr_pct = (atr / price * 100) if price > 0 else 0
    recent_liqs = sum(1 for lq in liqs if lq.get("age_s", 999) < 60)
    speed_score = 0
    if atr_pct >= 0.5: speed_score += 2
    elif atr_pct >= 0.3: speed_score += 1
    if regime_name in ("VOLATILE", "TRENDING_UP", "TRENDING_DOWN"): speed_score += 2
    elif regime_name == "RANGING": speed_score -= 1
    if recent_liqs >= 3: speed_score += 1
    if rsi <= 25 or rsi >= 75: speed_score += 1

    if speed_score >= 4:
        speed = "RÁPIDO"
        eta_min = max(2, int(60 * tp_pct / max(0.05, atr_pct)))
    elif speed_score >= 2:
        speed = "MEDIO"
        eta_min = max(10, int(120 * tp_pct / max(0.05, atr_pct))) if atr_pct > 0 else 60
    else:
        speed = "LENTO"
        eta_min = max(30, int(240 * tp_pct / max(0.05, atr_pct))) if atr_pct > 0 else 180

    # Cap razonable
    eta_min = min(eta_min, 60 * 24)

    reasoning.insert(0, f"Régimen {regime_name} · ATR {atr_pct:.2f}% · RSI {rsi:.0f}")
    if liq_clusters:
        nearest = min(liq_clusters, key=lambda c: abs(c["price"] - price))
        side = "arriba" if nearest["price"] > price else "abajo"
        reasoning.append(f"Cluster de liqs {side} a {nearest['price']:.4f} (~${nearest['notional']:,.0f})")

    return JSONResponse({
        "symbol":     sym,
        "mark":       round(price, 6),
        "direction":  "LONG" if is_long else "SHORT",
        "style":      style,
        "sl_price":   round(sl_price, 6),
        "tp_price":   round(tp_price, 6),
        "sl_pct":     round(sl_pct, 3),
        "tp_pct":     round(tp_pct, 3),
        "rr":         round(rr, 2),
        "speed":      speed,
        "eta_min":    eta_min,
        "atr":        round(atr, 6),
        "atr_pct":    round(atr_pct, 3),
        "regime":     regime_name,
        "rsi":        round(rsi, 1),
        "reasoning":  reasoning,
        "liq_clusters": liq_clusters[:5],
        "levels_used": {
            "sl": sl_reason or None,
            "tp": tp_reason or None,
        },
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


@app.post("/api/partial-close/{symbol}/{side}")
async def api_partial_close(symbol: str, side: str, pct: float = 50.0):
    """Cierra una fracción (pct ∈ (0, 100]) de la posición a mercado."""
    sym = symbol.upper()
    pct = max(1.0, min(100.0, float(pct)))
    pos = next(
        (p for p in _account.state.open_positions() if p.symbol == sym and p.side == side),
        None,
    )
    if not pos:
        return JSONResponse({"success": False, "error": "Posición no encontrada"})
    try:
        info = await _exec.load_instrument_info(sym)
        step = float(info.qty_step)
        raw  = pos.size * (pct / 100.0)
        # Redondear al step más cercano, asegurar mínimo
        qty  = max(float(info.min_qty), round(round(raw / step) * step, 8))
        # Si pct=100, usa qty total exacta para no dejar polvo
        if pct >= 100.0:
            qty = pos.size
        close_side = "Sell" if pos.is_long else "Buy"
        body = {
            "category":    "linear",
            "symbol":      sym,
            "side":        close_side,
            "orderType":   "Market",
            "qty":         str(qty),
            "timeInForce": "IOC",
            "reduceOnly":  True,
            "positionIdx": _exec._pos_idx(side),
        }
        data = await _exec._post("/v5/order/create", body)
        if data.get("retCode") == 0:
            log.warning("Partial close: %s %s %.1f%% (qty=%s)", sym, side, pct, qty)
            asyncio.create_task(_account_refresh_loop_once())
            return JSONResponse({"success": True, "qty": qty, "order_id": data["result"].get("orderId", "")})
        return JSONResponse({"success": False, "error": data.get("retMsg", "error")})
    except Exception as e:
        log.error("api_partial_close: %s", e)
        return JSONResponse({"success": False, "error": str(e)})


@app.post("/api/quick-trade")
async def api_quick_trade(req: Request):
    """Trade rápido a mercado: side + size_usdt + (sl_pct, tp_pct opcionales).
    Calcula SL/TP automáticamente desde el mark si no se proveen.
    """
    try:
        body      = await req.json()
        symbol    = str(body.get("symbol", "")).upper()
        if symbol and not symbol.endswith("USDT"):
            symbol += "USDT"
        side      = str(body.get("side", "Buy"))
        size_usdt = float(body.get("size_usdt", 0))
        sl_pct    = float(body.get("sl_pct", 1.0))     # % distancia desde mark
        tp_pct    = float(body.get("tp_pct", 2.0))
        leverage  = int(body.get("leverage", 10))
        if not symbol or size_usdt <= 0:
            return JSONResponse({"success": False, "error": "Parámetros incompletos"})

        mark = _mark_prices.get(symbol) or 0.0
        ms   = _market.states.get(symbol)
        if not mark and ms and ms.connected:
            mark = ms.ticker.last_price
        if mark <= 0:
            return JSONResponse({"success": False, "error": "Mark price no disponible"})

        is_buy = side == "Buy"
        sl = mark * (1 - sl_pct / 100.0) if is_buy else mark * (1 + sl_pct / 100.0)
        tp = mark * (1 + tp_pct / 100.0) if is_buy else mark * (1 - tp_pct / 100.0)

        info    = await _exec.load_instrument_info(symbol)
        step    = float(info.qty_step)
        raw_qty = size_usdt / mark
        qty     = max(float(info.min_qty), round(round(raw_qty / step) * step, 8))

        order = OrderRequest(
            symbol     = symbol,
            side       = side,
            qty        = qty,
            order_type = "Market",
            price      = 0.0,
            sl_price   = sl,
            tp_price   = tp,
            entry_price= mark,
            leverage   = leverage,
            trace_id   = str(uuid.uuid4())[:8],
            strategy_tag = "quick_web",
        )
        result = await _exec.place_market_bracket(order)
        return JSONResponse({
            "success":  result.success,
            "order_id": result.order_id,
            "qty":      qty,
            "sl":       sl,
            "tp":       tp,
            "error":    result.error_msg,
        })
    except Exception as e:
        log.error("api_quick_trade: %s", e)
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
