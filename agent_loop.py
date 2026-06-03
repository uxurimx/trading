#!/usr/bin/env python3
"""
agent_loop.py — Kairos Trading Agent
Claude es el edificio. El resto son andamios.

Paper trading: $1.38 → $5.00
Corre: python agent_loop.py
Log:   tail -f /tmp/kairos-agent.log
"""
import asyncio
import json
import logging
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import urllib.request

import aiohttp

sys.path.insert(0, str(Path(__file__).parent))
from core.trading_db import (
    decision_save, session_close, session_count_trade,
    session_open, session_update, signal_save,
    trade_close, trade_open, token_save, token_stats,
)

# ── Configuración ─────────────────────────────────────────────────────────────
PAPER_BALANCE_START = 1.38
TARGET_BALANCE      = 5.00
RISK_PCT            = 0.02       # 2% del balance por trade (riesgo real, no margen)
LEVERAGE            = 10
SCAN_INTERVAL       = 45         # segundos entre ciclos
SYMBOLS             = ["XRPUSDT", "SOLUSDT", "BTCUSDT", "ETHUSDT", "DOGEUSDT", "BNBUSDT"]
LOG_FILE            = "/tmp/kairos-agent.log"
BASE_URL            = "https://api.bybit.com"
CLAUDE_MODEL        = "claude-sonnet-4-6"
KAIROS_CHAT_EVENT   = "http://localhost:5300/event"

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("kairos")

# ── Estado en memoria (se persiste en PostgreSQL) ─────────────────────────────
state = {
    "session_id":    None,
    "balance":       PAPER_BALANCE_START,
    "open_trade":    None,   # dict: {pg_id, symbol, side, entry, sl, tp, qty, opened_at}
    "cycle":         0,
    "total_calls":   0,
    "skips":         0,
    "wins":          0,
    "losses":        0,
    "last_decisions": [],    # últimas 5 para contexto
}

# ── Notificaciones → kairos-chat ─────────────────────────────────────────────

def notify(event_type: str, **kwargs) -> None:
    """Envía evento a kairos-chat (WebSocket → Android)."""
    try:
        payload = json.dumps({"type": event_type, **kwargs}).encode()
        req = urllib.request.Request(
            KAIROS_CHAT_EVENT,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=2)
    except Exception:
        pass  # kairos-chat puede no estar corriendo, no es crítico


# ── Market data ───────────────────────────────────────────────────────────────

async def fetch_ticker(session: aiohttp.ClientSession, symbol: str) -> dict:
    url = f"{BASE_URL}/v5/market/tickers?category=linear&symbol={symbol}"
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
        d = await r.json()
        items = d.get("result", {}).get("list", [])
        return items[0] if items else {}

async def fetch_klines(session: aiohttp.ClientSession, symbol: str, interval: str, limit: int = 60) -> list:
    url = f"{BASE_URL}/v5/market/kline?category=linear&symbol={symbol}&interval={interval}&limit={limit}"
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
        d = await r.json()
        return d.get("result", {}).get("list", [])

def ema(prices: list, p: int) -> float:
    if len(prices) < p:
        return prices[-1] if prices else 0.0
    k = 2 / (p + 1)
    v = sum(prices[:p]) / p
    for px in prices[p:]:
        v = px * k + v * (1 - k)
    return v

def rsi(prices: list, p: int = 14) -> float:
    if len(prices) < p + 1:
        return 50.0
    g, l = [], []
    for i in range(1, len(prices)):
        d = prices[i] - prices[i - 1]
        g.append(max(0, d))
        l.append(max(0, -d))
    ag = sum(g[-p:]) / p
    al = sum(l[-p:]) / p
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)

def atr_calc(klines: list, p: int = 14) -> float:
    trs = []
    for i in range(min(len(klines) - 1, p * 2)):
        h  = float(klines[i][2])
        lo = float(klines[i][3])
        cp = float(klines[i + 1][4])
        trs.append(max(h - lo, abs(h - cp), abs(lo - cp)))
    return sum(trs[:p]) / p if trs else 0.0

async def build_snapshot(http: aiohttp.ClientSession) -> dict:
    """Construye snapshot de mercado para todos los símbolos."""
    snap = {}
    for sym in SYMBOLS:
        try:
            ticker, k5, k15 = await asyncio.gather(
                fetch_ticker(http, sym),
                fetch_klines(http, sym, "5",  60),
                fetch_klines(http, sym, "15", 60),
            )
            if not ticker or not k15:
                continue

            price   = float(ticker.get("lastPrice", 0) or 0)
            chg24   = float(ticker.get("price24hPcnt", 0) or 0) * 100
            funding = float(ticker.get("fundingRate", 0) or 0) * 100
            vol24   = float(ticker.get("volume24h", 0) or 0) * price

            def closes(kl): return [float(k[4]) for k in reversed(kl)]
            c5  = closes(k5)
            c15 = closes(k15)

            e9_5   = ema(c5,  9)
            e21_5  = ema(c5,  21)
            e9_15  = ema(c15, 9)
            e21_15 = ema(c15, 21)
            r5     = rsi(c5)
            r15    = rsi(c15)
            atr15  = atr_calc(k15)
            atr_pct = atr15 / price * 100 if price > 0 else 0

            vols = [float(k[5]) for k in k5[:20]]
            avg_v = sum(vols[3:]) / max(len(vols[3:]), 1)
            vol_ratio = sum(vols[:3]) / 3 / avg_v if avg_v > 0 else 1.0

            snap[sym] = {
                "price":     round(price, 6),
                "chg24h":    round(chg24, 2),
                "funding":   round(funding, 4),
                "vol24m":    round(vol24 / 1_000_000, 1),
                "rsi_5m":    round(r5, 1),
                "rsi_15m":   round(r15, 1),
                "ema_5m":    "UP" if e9_5 > e21_5 else "DOWN",
                "ema_15m":   "UP" if e9_15 > e21_15 else "DOWN",
                "atr_pct":   round(atr_pct, 3),
                "vol_ratio": round(vol_ratio, 2),
            }
        except Exception as e:
            log.warning("snapshot error %s: %s", sym, e)
    return snap

# ── Llamada a Claude ──────────────────────────────────────────────────────────

def call_claude(prompt: str) -> tuple[str, int, dict]:
    """Llama claude --print stream-json, retorna (texto, latencia_ms, usage)."""
    t0 = time.monotonic()
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_write": 0}
    try:
        proc = subprocess.Popen(
            ["claude", "--print", "--output-format", "stream-json",
             "--verbose", "--model", CLAUDE_MODEL],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True,
        )
        proc.stdin.write(prompt)
        proc.stdin.close()

        full_text = ""
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            t = obj.get("type")
            if t == "assistant":
                msg = obj.get("message", {})
                content = msg.get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            full_text += block["text"]
            elif t == "result":
                u = obj.get("usage", {})
                if u:
                    usage["input_tokens"]  = u.get("input_tokens", 0)
                    usage["output_tokens"] = u.get("output_tokens", 0)
                    usage["cache_read"]    = u.get("cache_read_input_tokens", 0)
                    usage["cache_write"]   = u.get("cache_creation_input_tokens", 0)
                # Usar costo real de Anthropic si está disponible
                cost = obj.get("total_cost_usd")
                if cost is not None:
                    usage["total_cost_usd"] = float(cost)
        proc.wait()
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        return full_text.strip(), elapsed_ms, usage
    except subprocess.TimeoutExpired:
        return "", 90000, usage
    except Exception as e:
        log.error("call_claude error: %s", e)
        return "", 0, usage

def build_prompt(snapshot: dict) -> str:
    sid = state["session_id"]
    bal = state["balance"]
    pnl = bal - PAPER_BALANCE_START
    remaining = TARGET_BALANCE - bal
    risk_usd  = bal * RISK_PCT

    lines = [
        "Eres Kairos, un trader experto en futuros perpetuos de criptomonedas (Bybit).",
        "Modo: PAPER TRADING. Capital inicial: $1.38. Objetivo: $5.00.",
        "",
        f"=== SESIÓN ACTUAL ===",
        f"Balance:    ${bal:.4f}",
        f"PnL:        ${pnl:+.4f}",
        f"Falta:      ${remaining:.4f} para objetivo",
        f"Trades:     {state['wins']}W / {state['losses']}L",
        f"Riesgo/trade: ${risk_usd:.4f} ({RISK_PCT*100:.0f}%)",
        "",
    ]

    # Posición abierta
    ot = state["open_trade"]
    if ot:
        sym   = ot["symbol"]
        price = snapshot.get(sym, {}).get("price", ot["entry"])
        if ot["side"] == "LONG":
            upnl = (price - ot["entry"]) * ot["qty"]
        else:
            upnl = (ot["entry"] - price) * ot["qty"]
        dur = int(time.time() - ot["opened_at"])

        lines += [
            f"=== POSICIÓN ABIERTA ===",
            f"Symbol:  {sym} {ot['side']}",
            f"Entrada: {ot['entry']}  |  Precio actual: {price}",
            f"TP: {ot['tp']}  |  SL: {ot['sl']}",
            f"Qty: {ot['qty']}  |  PnL flotante: ${upnl:+.4f}",
            f"Duración: {dur//60}m{dur%60:02d}s",
            "",
        ]
    else:
        lines += ["=== POSICIÓN: ninguna ===", ""]

    # Snapshot de mercado
    lines.append("=== MERCADO ===")
    for sym, d in snapshot.items():
        lines.append(
            f"{sym:<14} ${d['price']:<10} chg={d['chg24h']:+.2f}%  "
            f"rsi5m={d['rsi_5m']:.0f}  rsi15m={d['rsi_15m']:.0f}  "
            f"ema5m={d['ema_5m']}  ema15m={d['ema_15m']}  "
            f"atr={d['atr_pct']:.2f}%  vol={d['vol_ratio']:.1f}x  "
            f"fund={d['funding']:+.4f}%  vol24=${d['vol24m']:.0f}M"
        )

    # Últimas decisiones (contexto para no repetir)
    if state["last_decisions"]:
        lines += ["", "=== MIS ÚLTIMAS DECISIONES ==="]
        for dec in state["last_decisions"][-5:]:
            lines.append(f"  [{dec['ts']}] {dec['action']} {dec.get('symbol','')} — {dec['reason'][:80]}")

    lines += [
        "",
        "=== TU ROL ===",
        "Eres un trader profesional de futuros perpetuos. Estos datos son tu feed en tiempo real.",
        "Razona desde primeros principios: momentum, estructura de precio, contexto macro del mercado ahora mismo.",
        "El sistema que generó estos datos es una guía, no una ley. Tú decides.",
        "",
        "Contexto de referencia (úsalo si ayuda, ignóralo si el mercado dice otra cosa):",
        f"- Riesgo disponible por trade: ${risk_usd:.4f} (2% del balance)",
        "- Máximo 1 posición simultánea",
        "- En papel: no hay spread real ni slippage, pero opera como si existieran",
        "",
        "Piensa en voz alta dentro de 'razon' — qué ves, por qué entras o no, qué invalida el setup.",
        "No repitas setups que ya fallaron recientemente (ver últimas decisiones arriba).",
        "",
        "Responde SOLO con JSON válido, sin texto antes ni después:",
        "{",
        '  "accion": "ENTER" | "SKIP" | "HOLD" | "EXIT",',
        '  "simbolo": "XRPUSDT",',
        '  "lado": "LONG" | "SHORT",',
        '  "entrada": 1.2190,',
        '  "sl": 1.2050,',
        '  "tp": 1.2480,',
        '  "confianza": 72,',
        '  "tipo": "EXPRESS_SCALP" | "QUICK_SCALP" | "REVERSAL" | "BREAKOUT",',
        '  "razon": "tu razonamiento real"',
        "}",
        "",
        "sl/tp/entrada/lado/simbolo/tipo solo requeridos en ENTER. confianza siempre.",
    ]

    return "\n".join(lines)

# ── Paper trading ─────────────────────────────────────────────────────────────

def paper_open(sym: str, side: str, entry: float, sl: float, tp: float, confidence: int, tipo: str, razon: str) -> None:
    risk_usd = state["balance"] * RISK_PCT
    sl_dist  = abs(entry - sl)
    qty      = round(risk_usd / sl_dist, 4) if sl_dist > 0 else 1.0
    qty      = max(qty, 0.1)
    rr       = abs(tp - entry) / sl_dist if sl_dist > 0 else 0

    pg_id = trade_open(
        session_id=state["session_id"],
        symbol=sym, side=side,
        entry_price=entry, sl_price=sl, tp_price=tp,
        qty=qty, leverage=LEVERAGE,
        opportunity_type=tipo,
        confidence_score=min(confidence // 10, 10),
        risk_usd=risk_usd,
        rr_planned=round(rr, 3),
        paper_mode=True,
        pre_notes=razon,
    )
    decision_save(
        session_id=state["session_id"], trade_id=pg_id,
        symbol=sym, decision_type="ENTER", action=side,
        reasoning=razon, confidence=confidence,
        entry_price=entry, sl_price=sl, tp_price=tp, executed=True,
    )

    state["open_trade"] = {
        "pg_id": pg_id, "symbol": sym, "side": side,
        "entry": entry, "sl": sl, "tp": tp,
        "qty": qty, "opened_at": time.time(),
    }
    log.info("▶ ENTER %s %s @ %.6f  SL=%.6f TP=%.6f  qty=%.4f  risk=$%.4f",
             side, sym, entry, sl, tp, qty, risk_usd)
    notify("ENTER", symbol=sym, side=side, entry=entry, sl=sl, tp=tp,
           risk=round(risk_usd, 4), balance=round(state["balance"], 4),
           reason=razon[:100])

def paper_close(reason: str, current_price: float) -> None:
    ot = state["open_trade"]
    if not ot:
        return
    sym   = ot["symbol"]
    entry = ot["entry"]
    qty   = ot["qty"]
    side  = ot["side"]

    if side == "LONG":
        pnl = (current_price - entry) * qty
        r   = (current_price - entry) / abs(entry - ot["sl"]) if abs(entry - ot["sl"]) > 0 else 0
    else:
        pnl = (entry - current_price) * qty
        r   = (entry - current_price) / abs(ot["sl"] - entry) if abs(ot["sl"] - entry) > 0 else 0

    trade_close(ot["pg_id"], current_price, round(pnl, 6), round(r, 4), reason)
    decision_save(
        session_id=state["session_id"], trade_id=ot["pg_id"],
        symbol=sym, decision_type="EXIT", action="CLOSE",
        reasoning=f"Cerrado: {reason}. PnL={pnl:+.4f} R={r:+.2f}",
        executed=True,
    )

    state["balance"] += pnl
    session_count_trade(state["session_id"], won=pnl > 0)
    session_update(state["session_id"], state["balance"])

    won = pnl > 0
    if won:
        state["wins"] += 1
    else:
        state["losses"] += 1

    event_type = "WIN" if won else "LOSS"
    log.info("%s  %s %s  exit=%.6f  PnL=%+.4f  R=%+.2f  balance=$%.4f",
             "✓" if won else "✗", side, sym, current_price, pnl, r, state["balance"])
    notify(event_type, symbol=sym, side=side,
           entry=round(ot["entry"], 6), exit=round(current_price, 6),
           pnl=round(pnl, 4), r=round(r, 3),
           balance=round(state["balance"], 4),
           reason=reason)

    state["open_trade"] = None

# ── Ciclo principal ───────────────────────────────────────────────────────────

def check_tp_sl(snapshot: dict) -> None:
    """Verifica si TP o SL fue alcanzado sin que Claude necesite decidir."""
    ot = state["open_trade"]
    if not ot:
        return
    sym   = ot["symbol"]
    price = snapshot.get(sym, {}).get("price")
    if not price:
        return

    if ot["side"] == "LONG":
        if price >= ot["tp"]:
            paper_close("TP_HIT", ot["tp"])
            return
        if price <= ot["sl"]:
            paper_close("SL_HIT", ot["sl"])
            return
    else:
        if price <= ot["tp"]:
            paper_close("TP_HIT", ot["tp"])
            return
        if price >= ot["sl"]:
            paper_close("SL_HIT", ot["sl"])
            return

def process_decision(data: dict, snapshot: dict, latency_ms: int) -> None:
    accion = data.get("accion", "SKIP").upper()
    sym    = data.get("simbolo", "")
    razon  = data.get("razon", "")
    conf   = int(data.get("confianza", 50) or 50)

    state["last_decisions"].append({
        "ts":     datetime.now().strftime("%H:%M"),
        "action": accion,
        "symbol": sym,
        "reason": razon,
    })
    if len(state["last_decisions"]) > 10:
        state["last_decisions"].pop(0)

    if accion == "SKIP" or accion == "HOLD":
        state["skips"] += 1
        decision_save(
            session_id=state["session_id"],
            symbol=sym or "MARKET",
            decision_type="SKIP",
            action="HOLD",
            reasoning=razon,
            confidence=conf,
            latency_ms=latency_ms,
        )
        log.info("⏸ %s — %s", accion, razon[:80])

    elif accion == "EXIT" and state["open_trade"]:
        sym_open = state["open_trade"]["symbol"]
        price    = snapshot.get(sym_open, {}).get("price", state["open_trade"]["entry"])
        paper_close(f"CLAUDE_EXIT: {razon[:50]}", price)

    elif accion == "ENTER" and not state["open_trade"]:
        lado    = data.get("lado", "LONG")
        entrada = float(data.get("entrada", 0) or 0)
        sl      = float(data.get("sl", 0) or 0)
        tp      = float(data.get("tp", 0) or 0)
        tipo    = data.get("tipo", "QUICK_SCALP")

        # Validaciones básicas
        if not sym or not entrada or not sl or not tp:
            log.warning("ENTER incompleto — ignorando: %s", data)
            return
        if lado == "LONG"  and not (sl < entrada < tp):
            log.warning("LONG geometría inválida: sl=%.6f entry=%.6f tp=%.6f", sl, entrada, tp)
            return
        if lado == "SHORT" and not (tp < entrada < sl):
            log.warning("SHORT geometría inválida: tp=%.6f entry=%.6f sl=%.6f", tp, entrada, sl)
            return

        # Usar precio actual como entrada real (paper)
        price_now = snapshot.get(sym, {}).get("price", entrada)
        paper_open(sym, lado, price_now, sl, tp, conf, tipo, razon)

    elif accion == "ENTER" and state["open_trade"]:
        log.info("ENTER ignorado — posición ya abierta en %s", state["open_trade"]["symbol"])

async def run_cycle(http: aiohttp.ClientSession) -> None:
    state["cycle"] += 1
    log.info("── Ciclo %d │ balance=$%.4f │ %s",
             state["cycle"], state["balance"],
             "pos=" + state["open_trade"]["symbol"] if state["open_trade"] else "sin posición")

    # 1. Snapshot de mercado
    try:
        snapshot = await build_snapshot(http)
    except Exception as e:
        log.error("build_snapshot error: %s", e)
        return

    if not snapshot:
        log.warning("Snapshot vacío — esperando")
        return

    # Guarda señales del símbolo más relevante (o el de la posición)
    watch_sym = state["open_trade"]["symbol"] if state["open_trade"] else SYMBOLS[0]
    if watch_sym in snapshot:
        d = snapshot[watch_sym]
        signal_save(
            session_id=state["session_id"],
            symbol=watch_sym,
            price=d["price"],
            change_24h=d["chg24h"],
            volume_ratio=d["vol_ratio"],
            rsi_5m=d["rsi_5m"],
            rsi_15m=d["rsi_15m"],
            atr_pct=d["atr_pct"],
            funding_rate=d["funding"],
            opp_score=0,
        )

    # 2. Verificar TP/SL automáticamente
    check_tp_sl(snapshot)

    # 3. Construir prompt y llamar a Claude
    prompt = build_prompt(snapshot)
    state["total_calls"] += 1

    log.info("→ Llamando a Claude (llamada #%d)...", state["total_calls"])
    raw_text, latency_ms, usage = call_claude(prompt)

    if not raw_text:
        log.warning("Claude no respondió — saltando ciclo")
        return

    # Guardar uso de tokens en PostgreSQL
    call_cost = usage.get("total_cost_usd") or token_cost(
        usage["input_tokens"], usage["output_tokens"],
        usage["cache_read"], usage["cache_write"],
    )
    token_save(
        session_id=state["session_id"],
        input_tokens=usage["input_tokens"],
        output_tokens=usage["output_tokens"],
        cache_read=usage["cache_read"],
        cache_write=usage["cache_write"],
        latency_ms=latency_ms,
        cost_override=call_cost,
    )
    log.info("← %.1fs  in=%d out=%d cache_r=%d cache_w=%d  $%.5f",
             latency_ms / 1000,
             usage["input_tokens"], usage["output_tokens"],
             usage["cache_read"], usage["cache_write"], call_cost)

    # 4. Parsear JSON
    try:
        raw_text = raw_text.strip()
        import re
        m = re.search(r'\{.*\}', raw_text, flags=re.DOTALL)
        data = json.loads(m.group(0) if m else raw_text)
    except json.JSONDecodeError as e:
        log.error("JSON inválido de Claude: %s | raw: %s", e, raw_text[:200])
        return

    # 5. Procesar decisión
    process_decision(data, snapshot, latency_ms)

    # 6. Estado de progreso
    pnl = state["balance"] - PAPER_BALANCE_START
    pct = pnl / (TARGET_BALANCE - PAPER_BALANCE_START) * 100
    log.info("   Progreso: $%.4f → $%.4f  PnL=%+.4f  [%.1f%%]  %dW/%dL  calls=%d",
             PAPER_BALANCE_START, state["balance"], pnl, pct,
             state["wins"], state["losses"], state["total_calls"])

    # Proyecciones de costo cada 10 ciclos
    if state["cycle"] % 10 == 0:
        st = token_stats(state["session_id"])
        if st:
            log.info("── TOKENS ── total=$%.4f  /llamada=$%.6f  proj 1h=$%.3f  24h=$%.2f  30d=$%.2f",
                     st["total_cost"], st["cost_per_call"],
                     st["proj_1h"], st["proj_24h"], st["proj_30d"])

    # Objetivo alcanzado
    if state["balance"] >= TARGET_BALANCE:
        log.info("🎯 OBJETIVO ALCANZADO — $%.4f ≥ $%.4f", state["balance"], TARGET_BALANCE)
        session_close(state["session_id"], state["balance"], "Objetivo alcanzado")
        sys.exit(0)

    # Capital destruido — parar
    if state["balance"] < 0.20:
        log.warning("⚠ Balance crítico $%.4f — deteniendo agente", state["balance"])
        session_close(state["session_id"], state["balance"], "Balance crítico")
        sys.exit(1)

# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    log.info("=" * 60)
    log.info("  KAIROS TRADING AGENT")
    log.info("  Paper trading: $%.2f → $%.2f", PAPER_BALANCE_START, TARGET_BALANCE)
    log.info("  Símbolos: %s", ", ".join(SYMBOLS))
    log.info("  Intervalo: %ds  |  Riesgo/trade: %.0f%%", SCAN_INTERVAL, RISK_PCT * 100)
    log.info("=" * 60)

    # Abrir sesión en PostgreSQL
    state["session_id"] = session_open(
        name=f"agent-{datetime.now().strftime('%Y%m%d-%H%M')}",
        initial_balance=PAPER_BALANCE_START,
        target_balance=TARGET_BALANCE,
        mode="PAPER",
        agent="CLAUDE",
        strategy_focus="AUTONOMOUS",
    )
    log.info("Sesión: %s", state["session_id"])

    async with aiohttp.ClientSession() as http:
        while True:
            try:
                await run_cycle(http)
            except KeyboardInterrupt:
                log.info("Detenido por usuario")
                session_close(state["session_id"], state["balance"], "Detenido manualmente")
                break
            except Exception as e:
                log.error("Error en ciclo: %s", e)

            await asyncio.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Agente detenido")
