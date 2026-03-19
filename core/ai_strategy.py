"""
core/ai_strategy.py
────────────────────
AIStrategyAgent — genera propuestas de trading usando un agente de OpenAI.

Fixes vs v1:
  · Solo envía el TOP 12 por score (no 35/100) → modelo más enfocado
  · Prompt corregido: mercado trending = OPERAR EN LA DIRECCIÓN, no rechazar
  · Intervalo mínimo 60 s entre llamadas (evita spam a la API)
  · Formato de contexto más limpio y directo
"""
from __future__ import annotations

import json
import logging
import time
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

from core.config import settings

if TYPE_CHECKING:
    from streams.market import MarketState
    from streams.account import AccountState
    from core.regime import OpportunitySignal
    from core.technicals import TechSignal
    from core.order_model import OrderRequest, TradeRecord
    from core.executor import BybitExecutor

log = logging.getLogger("qts.ai_strategy")

TAKER_FEE_RATE   = 0.00055   # 0.055% por lado (0.11% round-trip)
AI_MIN_INTERVAL  = 60        # segundos mínimos entre llamadas a OpenAI
AI_TOP_SYMBOLS   = 12        # cuántos símbolos enviar al agente (top por score)
AI_MIN_SCORE     = 60        # score mínimo para incluir un símbolo en el análisis


# ─── Prompt del sistema (CORREGIDO) ──────────────────────────────────────────

_SYSTEM_PROMPT = """\
Eres un trader experto en futuros perpetuos de criptomonedas (Bybit).

REGLA FUNDAMENTAL — dirección según tendencia:
  • Tendencia ALCISTA  (trend_score ≥ 60, ALCISTA) → buscar setup LONG  (Buy)
  • Tendencia BAJISTA  (trend_score ≥ 60, BAJISTA) → buscar setup SHORT (Sell)
  • Tendencia NEUTRAL o trend_score < 60 → ambas direcciones permitidas
  ⚠ Un mercado EN TENDENCIA es la MEJOR condición para operar — NO es motivo para rechazar.
  ⚠ Solo rechaza un símbolo si la señal del sistema (direction) va CONTRA la tendencia fuerte.

CRITERIOS DE ENTRADA (todos deben cumplirse):
  1. Score del sistema ≥ 60 (oportunidad detectada por los detectores técnicos)
  2. CVD y EMA alineados con la dirección de entrada
  3. SL = 1.0×ATR a 1.5×ATR desde el entry (el ATR está en los datos)
  4. TP en el próximo nivel de soporte/resistencia que dé R:R neto ≥ 2:1
     — Fórmula R:R neto: (TP_dist − 0.0011×entry) / (SL_dist + 0.0011×entry)
  5. Si no existe nivel S/R claro, usar TP = entry ± 2.5×ATR (long/short)

PROCESO (obligatorio):
  1. Lee los datos de los candidatos
  2. Para el top 3 por score, evalúa: dirección, CVD, EMA, y calcula SL/TP
  3. Elige el mejor o indica NO_TRADE solo si NINGUNO cumple los 5 criterios
  4. Proporciona los precios EXACTOS basados en los datos reales

RESPUESTA: solo JSON válido, sin texto, sin markdown.

Si hay trade:
{
  "action": "TRADE",
  "symbol": "BTCUSDT",
  "side": "Buy",
  "entry": 103500.0,
  "sl": 102900.0,
  "tp": 104700.0,
  "confidence": 78,
  "reasoning": "BTC score=82, tendencia ALCISTA 71%, CVD 4/5 alcistas, EMA↑. Entry en precio actual 103500, SL=1.0×ATR(600)=102900, TP en resistencia 104700 → R:R neto = (1200-114)/(600+114) = 1086/714 = 1.52... ajustando TP a 105800 para R:R=2.1"
}

Si ningún candidato cumple TODOS los criterios:
{
  "action": "NO_TRADE",
  "reasoning": "Motivo específico por cada candidato evaluado."
}
"""


# ─── Snapshot de mercado (solo top candidatos) ────────────────────────────────

def _build_market_snapshot(
    symbols: List[str],
    states:  Dict[str, "MarketState"],
    opps:    Dict[str, "OpportunitySignal"],
    techs:   Dict[str, "TechSignal"],
) -> str:
    """
    Devuelve solo los TOP AI_TOP_SYMBOLS con score ≥ AI_MIN_SCORE.
    Formato denso pero legible para el modelo.
    """
    # Filtrar y rankear
    candidates = []
    for sym in symbols:
        opp = opps.get(sym)
        if opp and opp.score >= AI_MIN_SCORE:
            candidates.append((opp.score, sym))
    candidates.sort(reverse=True)
    top = candidates[:AI_TOP_SYMBOLS]

    if not top:
        return "=== SIN CANDIDATOS CON SCORE SUFICIENTE ===\n(todos < " + str(AI_MIN_SCORE) + ")"

    lines = [f"=== TOP {len(top)} CANDIDATOS (score ≥ {AI_MIN_SCORE}) ==="]

    for _score, sym in top:
        ms   = states.get(sym)
        opp  = opps.get(sym)
        tech = techs.get(sym)
        if not ms or not opp or not tech:
            continue

        price = ms.ticker.last_price
        if price <= 0:
            continue

        # CVD
        cvd_candles = list(getattr(ms, "cvd_candles", []))[-5:]
        if cvd_candles:
            bull = sum(1 for c in cvd_candles if c.delta > 0)
            cvd_str = f"CVD={bull}/5bull"
        else:
            cvd_str = "CVD=N/D"

        # OI
        oi_str = ""
        oi_samples = list(getattr(ms, "oi_samples", []))[-10:]
        if len(oi_samples) >= 2:
            v0 = getattr(oi_samples[0],  "oi", 0)
            v1 = getattr(oi_samples[-1], "oi", 0)
            if v0 > 0:
                oi_pct = (v1 - v0) / v0 * 100
                oi_str = f" OI={oi_pct:+.1f}%"

        # Niveles
        sup_str = f" S={tech.support:.6g}"   if tech.support    > 0 else ""
        res_str = f" R={tech.resistance:.6g}" if tech.resistance > 0 else ""
        ema_str = "EMA↑" if tech.ema15m_bull else "EMA↓"
        atr_pct = tech.atr_15m / price * 100 if price > 0 else 0

        # Suggested SL/TP for reference
        atr = tech.atr_15m
        if opp.direction == "LONG":
            sl_ref = price - 1.2 * atr
            tp_ref = price + 2.5 * atr
        else:
            sl_ref = price + 1.2 * atr
            tp_ref = price - 2.5 * atr
        rt_fees = price * TAKER_FEE_RATE * 2
        sl_d = abs(price - sl_ref)
        tp_d = abs(tp_ref - price)
        rr_ref = (tp_d - rt_fees) / (sl_d + rt_fees) if (sl_d + rt_fees) > 0 else 0

        lines.append(
            f"\n[{sym.replace('USDT',''):>8}] score={opp.score} dir={opp.direction}"
            f"\n  price={price:.6g}  ATR={atr:.5g}({atr_pct:.2f}%)"
            f"\n  régimen={opp.regime.label}  trend={opp.trend_direction}({opp.trend_score}%)"
            f"\n  {cvd_str}{oi_str}  {ema_str}  abs={opp.abs_pts}pts"
            f"\n  {sup_str.strip()}{res_str.strip()}"
            f"\n  → Ref: SL≈{sl_ref:.6g} TP≈{tp_ref:.6g} (R:R ref={rr_ref:.2f})"
        )

    return "\n".join(lines)


def _build_account_snapshot(
    account:       "AccountState",
    active_trades: List["TradeRecord"],
) -> str:
    bal   = account.balance
    avail = getattr(bal, "available", 0.0)
    lines = [
        "=== CUENTA ===",
        f"Equity=${bal.total_equity:.2f}  PnL_diario=${account.daily_pnl:+.2f}  Disponible=${avail:.2f}",
    ]
    if active_trades:
        lines.append(f"Trades_activos={len(active_trades)}: " +
                     ", ".join(f"{t.request.symbol if t.request else '?'}" for t in active_trades))
    else:
        lines.append("Trades_activos=ninguno")
    return "\n".join(lines)


# ─── AIStrategyAgent ──────────────────────────────────────────────────────────

class AIStrategyAgent:
    """
    Agente de IA que genera propuestas de trading usando la API de OpenAI.
    Solo envía los mejores candidatos (top 12 por score) para un análisis enfocado.
    Intervalo mínimo de 60 segundos entre llamadas para no saturar la API.
    """

    def __init__(self) -> None:
        self._last_call_ts: float = 0.0

    def is_ready(self) -> bool:
        return bool(getattr(settings, "openai_api_key", ""))

    def seconds_until_ready(self) -> int:
        elapsed = time.monotonic() - self._last_call_ts
        return max(0, int(AI_MIN_INTERVAL - elapsed))

    async def generate_proposal(
        self,
        symbols:       List[str],
        states:        Dict[str, "MarketState"],
        opps:          Dict[str, "OpportunitySignal"],
        techs:         Dict[str, "TechSignal"],
        account:       "AccountState",
        active_trades: List["TradeRecord"],
        goal_usd:      float,
        executor:      "BybitExecutor",
        leverage:      int,
    ) -> Optional[Tuple[str, "OrderRequest"]]:
        import asyncio
        try:
            import openai as _openai
        except ImportError:
            log.error("openai no instalado — ejecutar: pip install openai")
            return None

        if not self.is_ready():
            log.warning("AI Strategy: sin API key de OpenAI")
            return None

        self._last_call_ts = time.monotonic()

        # Contar cuántos candidatos hay antes de construir el snapshot
        n_candidates = sum(
            1 for s in symbols
            if opps.get(s) and opps[s].score >= AI_MIN_SCORE
        )

        market_snapshot  = _build_market_snapshot(symbols, states, opps, techs)
        account_snapshot = _build_account_snapshot(account, active_trades)
        model            = getattr(settings, "openai_model", "gpt-4o")

        user_prompt = (
            f"{account_snapshot}\n\n"
            f"{market_snapshot}\n\n"
            "=== INSTRUCCIONES ===\n"
            f"Goal por trade: ${goal_usd:.2f} USD  |  Leverage: {leverage}x\n"
            "Fees taker: 0.055%/lado → 0.11% round-trip (ya incluido en la fórmula R:R neto).\n\n"
            "Evalúa los candidatos de mayor a menor score.\n"
            "Para cada uno: verifica dirección vs tendencia, CVD, EMA y calcula SL/TP exactos.\n"
            "Entrega el mejor trade CON precios precisos, o NO_TRADE si ninguno califica.\n"
            "IMPORTANTE: un score ≥ 70 con CVD y EMA alineados ES suficiente para entrar.\n"
            "Responde SOLO con el JSON."
        )

        log.info(
            "AI Strategy: consultando %s — %d candidatos de %d símbolos",
            model, n_candidates, len(symbols),
        )
        t0 = time.monotonic()

        try:
            client   = _openai.AsyncOpenAI(api_key=settings.openai_api_key)
            response = await asyncio.wait_for(
                client.chat.completions.create(
                    model    = model,
                    messages = [
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user",   "content": user_prompt},
                    ],
                    temperature     = 0.15,
                    max_tokens      = 1800,
                    response_format = {"type": "json_object"},
                ),
                timeout=50.0,
            )
            elapsed = time.monotonic() - t0
            raw     = response.choices[0].message.content or "{}"
            log.info("AI Strategy: respuesta en %.1fs (%d chars)", elapsed, len(raw))

        except asyncio.TimeoutError:
            log.error("AI Strategy: timeout (50s)")
            return None
        except Exception as e:
            log.error("AI Strategy: error OpenAI: %s", e)
            return None

        # ── Parsear ───────────────────────────────────────────────────────────
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            log.error("AI Strategy: JSON inválido: %s\n%s", e, raw[:300])
            return None

        action    = data.get("action", "NO_TRADE")
        reasoning = data.get("reasoning", "Sin razonamiento.")

        if action != "TRADE":
            log.info("AI Strategy: NO_TRADE — %s", reasoning[:200])
            return None

        # ── Extraer y validar campos ───────────────────────────────────────────
        symbol = str(data.get("symbol", "")).strip().upper()
        side   = str(data.get("side",   "")).strip()
        try:
            entry = float(data.get("entry", 0) or 0)
            sl    = float(data.get("sl",    0) or 0)
            tp    = float(data.get("tp",    0) or 0)
            conf  = int(data.get("confidence", 70) or 70)
        except (TypeError, ValueError) as e:
            log.error("AI Strategy: valores numéricos inválidos: %s", e)
            return None

        if not symbol or side not in ("Buy", "Sell") or entry <= 0 or sl <= 0 or tp <= 0:
            log.error("AI Strategy: campos obligatorios faltantes: %s", data)
            return None

        # Normalizar símbolo
        if symbol not in symbols:
            sym_usdt = symbol + "USDT"
            if sym_usdt in symbols:
                symbol = sym_usdt
            else:
                log.error("AI Strategy: símbolo '%s' no monitoreado", symbol)
                return None

        # ── Validar dirección coherente ─────────────────────────────────────
        opp = opps.get(symbol)
        if opp and opp.trend_score >= 60:
            if opp.trend_direction == "ALCISTA" and side == "Sell":
                log.warning("AI Strategy: propuesta SHORT en tendencia ALCISTA %d%% — rechazando", opp.trend_score)
                return None
            if opp.trend_direction == "BAJISTA" and side == "Buy":
                log.warning("AI Strategy: propuesta LONG en tendencia BAJISTA %d%% — rechazando", opp.trend_score)
                return None

        # ── Validar geometría SL/TP vs side ────────────────────────────────
        if side == "Buy":
            if sl >= entry:
                log.error("AI Strategy: LONG pero SL(%.5g) >= entry(%.5g)", sl, entry)
                return None
            if tp <= entry:
                log.error("AI Strategy: LONG pero TP(%.5g) <= entry(%.5g)", tp, entry)
                return None
        else:
            if sl <= entry:
                log.error("AI Strategy: SHORT pero SL(%.5g) <= entry(%.5g)", sl, entry)
                return None
            if tp >= entry:
                log.error("AI Strategy: SHORT pero TP(%.5g) >= entry(%.5g)", tp, entry)
                return None

        # ── Validar R:R neto ≥ 2.0 ────────────────────────────────────────
        sl_dist = abs(entry - sl)
        tp_dist = abs(tp    - entry)
        rt_fees = entry * TAKER_FEE_RATE * 2
        net_tp  = tp_dist - rt_fees
        net_sl  = sl_dist + rt_fees

        if net_sl <= 0 or net_tp <= 0:
            log.error("AI Strategy: distancias inválidas")
            return None

        rr = net_tp / net_sl
        if rr < 2.0:
            log.warning(
                "AI Strategy: R:R neto %.2f < 2.0 — rechazando "
                "(entry=%.5g sl=%.5g tp=%.5g)",
                rr, entry, sl, tp,
            )
            return None

        # ── Sizing ─────────────────────────────────────────────────────────
        net_tp_unit = tp_dist - rt_fees
        qty = goal_usd / net_tp_unit
        qty = executor.round_qty(symbol, qty)
        if qty <= 0:
            log.warning("AI Strategy: qty=0 tras redondear para %s", symbol)
            return None

        ok, reason = executor.validate_order(symbol, qty, entry)
        if not ok:
            log.warning("AI Strategy: orden inválida %s qty=%s: %s", symbol, qty, reason)
            return None

        risk_usd = qty * net_sl
        notional  = qty * entry
        margin    = notional / max(1, leverage)

        # ── Construir OrderRequest ─────────────────────────────────────────
        from core.order_model import OrderRequest
        req = OrderRequest(
            symbol       = symbol,
            side         = side,
            qty          = qty,
            order_type   = "Market",
            entry_price  = entry,
            sl_price     = sl,
            tp_price     = tp,
            goal_usd     = goal_usd,
            risk_usd     = round(risk_usd, 2),
            rr_ratio     = round(rr, 2),
            opp_score    = conf,
            notional     = round(notional, 2),
            margin       = round(margin, 2),
            leverage     = leverage,
            reasons      = [
                f"AI Agent ({model})",
                f"Confianza: {conf}%  |  R:R neto: {rr:.2f}:1",
                reasoning[:60] + ("…" if len(reasoning) > 60 else ""),
            ],
            strategy_tag = "ai_agent",
            ai_reasoning = reasoning,
        )

        log.info(
            "AI Strategy: TRADE %s %s  entry=%.5g  SL=%.5g  TP=%.5g  R:R=%.2f  conf=%d%%",
            side, symbol, entry, sl, tp, rr, conf,
        )
        return symbol, req


# ── Singleton ──────────────────────────────────────────────────────────────────
ai_agent = AIStrategyAgent()
