"""
core/ai_strategy.py
────────────────────
AIStrategyAgent — genera propuestas de trading usando un agente de OpenAI.

Fixes vs v1:
  · Solo envía el TOP 12 por score (no 35/100) → modelo más enfocado
  · Prompt corregido: mercado trending = OPERAR EN LA DIRECCIÓN, no rechazar
  · Intervalo mínimo 60 s entre llamadas (evita spam a la API)
  · Formato de contexto más limpio y directo
  · [NUEVO] Filtro de Latencia: descarta trades obsoletos (> 45s)
  · [NUEVO] Extractor JSON robusto (ignora <think> y basura de modelos locales)
"""
from __future__ import annotations

import json
import logging
import time
import re
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
# Los parámetros de IA ahora se manejan vía settings para ser dinámicos en la UI.


# ─── Prompt del sistema (CORREGIDO) ──────────────────────────────────────────

_SYSTEM_PROMPT_TEMPLATE = """\
Eres un trader experto en futuros perpetuos de criptomonedas (Bybit).
Tu trabajo es SELECCIONAR EL MEJOR TRADE del lote de candidatos, no buscar excusas para rechazar.

═══ DIRECCIÓN según tendencia ═══
  • trend ALCISTA (trend_score ≥ 60) → dir=LONG  (Buy)
  • trend BAJISTA (trend_score ≥ 60) → dir=SHORT (Sell)
  • trend NEUTRAL o trend_score < 60 → ambas direcciones válidas
  ▶ Tendencia fuerte = OPORTUNIDAD, NO razón para rechazar.
  ✗ Solo rechaza si la dirección del sistema va CONTRA la tendencia.

═══ CVD — definición cuantitativa ═══
  CVD=X/5bull significa que X de las últimas 5 velas tuvieron delta positivo.
  • LONG alineado:  CVD ≥ 3/5 bull  (mayoría alcista)
  • SHORT alineado: CVD ≤ 2/5 bull  (mayoría bajista = ≥ 3/5 bear)
  • CVD=3/5 bull con dir=LONG → ALINEADO ✓
  • CVD=2/5 bull con dir=SHORT → ALINEADO ✓  (= 3/5 bear)
  ▶ No requieras unanimidad — basta con la mayoría.

═══ SL / TP — cálculo ═══
  Fees round-trip = 0.11% × entry (ya provistos como rt_fees en cada candidato).
  R:R neto = (TP_dist − rt_fees) / (SL_dist + rt_fees) ≥ {min_rr}

  Orden de preferencia para SL/TP:
    1. Usa el nivel S (soporte) o R (resistencia) más cercano del candidato.
    2. Si no hay S/R útil: SL = 1.5×ATR, TP = 4.0×ATR desde entry.
       (Con ATR ≥ 0.4% esto garantiza R:R ≥ {min_rr} después de fees.)

  ▶ Siempre verifica la fórmula R:R antes de responder.
  ▶ Si el nivel S/R más cercano no da R:R neto ≥ {min_rr}, usa 4×ATR como TP.

═══ PROCESO obligatorio ═══
  1. Ordena los candidatos por score (mayor primero).
  2. Para cada uno (empezando por el top):
     a. Confirma dirección vs tendencia.
     b. Verifica CVD con la regla de mayoría.
     c. Calcula SL y TP (usa S/R o ATR×multiplicador).
     d. Calcula R:R neto. Si ≥ {min_rr} → TRADE. Detén el análisis.
  3. Solo retorna NO_TRADE si TODOS los candidatos tienen R:R neto < {min_rr}
     O si la dirección va contra la tendencia fuerte.

═══ RESPUESTA: solo JSON válido, sin texto, sin markdown ═══

Si hay trade:
{{"action":"TRADE","symbol":"SOLUSDT","side":"Buy","entry":145.50,"sl":143.80,"tp":150.90,"confidence":79,"reasoning":"SOL score=73, ALCISTA 68%, CVD=4/5 bull (alineado LONG), EMA↑. SL=1.5×ATR(1.13)=143.80, TP en resistencia 150.90. rt_fees=0.16. R:R=(5.40-0.16)/(1.70+0.16)=5.24/1.86=2.82"}}

Si ninguno califica:
{{"action":"NO_TRADE","reasoning":"Candidato A: dir LONG vs tendencia BAJISTA fuerte. Candidato B: CVD=1/5 bull en LONG (no alineado). Candidato C: R:R neto=1.8 (insuficiente incluso con TP en resistencia R=X)."}}
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
        opp  = opps.get(sym)
        tech = techs.get(sym)
        ms   = states.get(sym)
        if not opp or opp.score < settings.ai_min_score:
            continue
        if not tech or not ms:
            continue
        price = ms.ticker.last_price
        if price <= 0:
            continue
        atr_pct = tech.atr_15m / price * 100
        if atr_pct < settings.ai_min_atr_pct:
            continue  # ATR demasiado pequeño — fees comerían todo el profit
        candidates.append((opp.score, sym))
    candidates.sort(reverse=True)
    top = candidates[:settings.ai_top_symbols]

    if not top:
        return (
            f"=== SIN CANDIDATOS VÁLIDOS ===\n"
            f"(score ≥ {settings.ai_min_score} Y ATR ≥ {settings.ai_min_atr_pct}% Y R:R ≥ {settings.min_rr})\n"
            "Mercado en baja volatilidad — esperar condiciones mejores."
        )

    lines = [f"=== TOP {len(top)} CANDIDATOS (score ≥ {settings.ai_min_score}, ATR ≥ {settings.ai_min_atr_pct}%) ==="]

    for _score, sym in top:
        ms   = states.get(sym)
        opp  = opps.get(sym)
        tech = techs.get(sym)
        if not ms or not opp or not tech:
            continue

        price = ms.ticker.last_price
        atr   = tech.atr_15m
        atr_pct = atr / price * 100

        # CVD — expresado como X/5 bull (mayoría define dirección)
        cvd_candles = list(getattr(ms, "cvd_candles", []))[-5:]
        if cvd_candles:
            bull = sum(1 for c in cvd_candles if c.delta > 0)
            bear = 5 - bull
            cvd_str = f"CVD={bull}/5bull({bear}/5bear)"
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

        # Niveles S/R
        sup_str = f"S={tech.support:.6g}"   if tech.support    > 0 else ""
        res_str = f"R={tech.resistance:.6g}" if tech.resistance > 0 else ""
        sr_str  = "  ".join(filter(None, [sup_str, res_str])) or "S/R=N/D"
        ema_str = "EMA↑" if tech.ema15m_bull else "EMA↓"

        # Referencia SL/TP con multiplicadores que garantizan R:R ≥ 2.0
        # SL=1.5×ATR, TP=4.0×ATR → rr_ref ≥ 2.0 cuando ATR% ≥ 0.4%
        rt_fees = price * TAKER_FEE_RATE * 2
        sl_dist_ref = 1.5 * atr
        tp_dist_ref = 4.0 * atr
        rr_ref = (tp_dist_ref - rt_fees) / (sl_dist_ref + rt_fees) if (sl_dist_ref + rt_fees) > 0 else 0
        if opp.direction == "LONG":
            sl_ref = price - sl_dist_ref
            tp_ref = price + tp_dist_ref
        else:
            sl_ref = price + sl_dist_ref
            tp_ref = price - tp_dist_ref

        lines.append(
            f"\n[{sym.replace('USDT',''):>8}] score={opp.score} dir={opp.direction}"
            f"\n  price={price:.6g}  ATR={atr:.5g}({atr_pct:.2f}%)  rt_fees={rt_fees:.5g}"
            f"\n  trend={opp.trend_direction}({opp.trend_score}%)  régimen={opp.regime.label}"
            f"\n  {cvd_str}{oi_str}  {ema_str}  {sr_str}"
            f"\n  RefSL≈{sl_ref:.6g} RefTP≈{tp_ref:.6g} → R:R_ref={rr_ref:.2f}"
            f"  {'✓ VIABLE' if rr_ref >= settings.min_rr else '⚠ usar S/R para mejorar TP'}"
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
    Agente de IA multi-proveedor para generar propuestas de trading.
    Soporta: OpenAI · Ollama (LLM local) · Compatible OpenAI (Groq, Mistral, etc.)
    Intervalo mínimo de 60 s entre llamadas.
    """

    def __init__(self) -> None:
        self._last_call_ts: float = 0.0

    def is_ready(self) -> bool:
        provider = getattr(settings, "ai_provider", "openai")
        if provider == "openai":
            return bool(getattr(settings, "openai_api_key", ""))
        if provider == "ollama":
            return bool(getattr(settings, "ollama_host", ""))
        if provider == "compatible":
            return bool(getattr(settings, "ai_compat_url", "")) and bool(getattr(settings, "ai_compat_model", ""))
        return False

    def provider_label(self) -> str:
        """Nombre legible del proveedor activo."""
        provider = getattr(settings, "ai_provider", "openai")
        if provider == "ollama":
            model = getattr(settings, "ollama_model", "?")
            return f"Ollama({model})"
        if provider == "compatible":
            model = getattr(settings, "ai_compat_model", "?")
            return f"Compatible({model})"
        return getattr(settings, "openai_model", "gpt-4o")

    def _make_client_and_model(self):
        """
        Retorna (AsyncOpenAI_client, model_name, use_json_format).
        Ollama y compatibles usan la misma interfaz OpenAI con base_url diferente.
        use_json_format=False para Ollama (soporte inconsistente según modelo).
        """
        import openai as _openai
        provider = getattr(settings, "ai_provider", "openai")

        if provider == "ollama":
            host  = getattr(settings, "ollama_host", "http://localhost:11434").rstrip("/")
            model = getattr(settings, "ollama_model", "llama3.2")
            client = _openai.AsyncOpenAI(api_key="ollama", base_url=f"{host}/v1")
            return client, model, False   # sin response_format para Ollama

        if provider == "compatible":
            url   = getattr(settings, "ai_compat_url",   "").rstrip("/")
            key   = getattr(settings, "ai_compat_key",   "") or "none"
            model = getattr(settings, "ai_compat_model", "")
            client = _openai.AsyncOpenAI(api_key=key, base_url=url)
            return client, model, True

        # openai (default)
        model  = getattr(settings, "openai_model", "gpt-4o")
        client = _openai.AsyncOpenAI(api_key=settings.openai_api_key)
        return client, model, True

    def seconds_until_ready(self) -> int:
        elapsed = time.monotonic() - self._last_call_ts
        return max(0, int(settings.ai_min_interval_s - elapsed))

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
            log.warning("AI Strategy: proveedor '%s' no configurado",
                        getattr(settings, "ai_provider", "openai"))
            return None

        self._last_call_ts = time.monotonic()

        n_candidates = sum(
            1 for s in symbols
            if opps.get(s) and opps[s].score >= settings.ai_min_score
        )

        market_snapshot  = _build_market_snapshot(symbols, states, opps, techs)
        account_snapshot = _build_account_snapshot(account, active_trades)

        try:
            client, model, use_json_fmt = self._make_client_and_model()
        except Exception as e:
            log.error("AI Strategy: error al crear cliente: %s", e)
            return None

        # Para Ollama: añadir instrucción JSON al final del prompt de usuario
        json_reminder = "" if use_json_fmt else "\nIMPORTANTE: responde ÚNICAMENTE con el JSON, sin ningún texto adicional. No uses etiquetas <think>."
        user_prompt = (
            f"{account_snapshot}\n\n"
            f"{market_snapshot}\n\n"
            "=== INSTRUCCIONES ===\n"
            f"Goal por trade: ${goal_usd:.2f} USD  |  Leverage: {leverage}x\n"
            "Fees ya incluidas en rt_fees de cada candidato (0.11% round-trip).\n\n"
            "Evalúa candidatos de mayor a menor score.\n"
            "CVD LONG alineado: ≥ 3/5 bull.  CVD SHORT alineado: ≤ 2/5 bull (= ≥ 3/5 bear).\n"
            "Para SL/TP: usa niveles S/R si están disponibles; si no, usa RefSL y RefTP del candidato.\n"
            f"Si RefTP no da R:R ≥ {settings.min_rr}, busca el nivel S/R más lejano que sí lo dé.\n"
            "Un score ≥ 60 con dirección coherente y CVD mayoritariamente alineado ES suficiente.\n"
            f"Responde SOLO con el JSON.{json_reminder}"
        )

        log.info(
            "AI Strategy: consultando %s (%s) — %d candidatos de %d símbolos",
            self.provider_label(), model, n_candidates, len(symbols),
        )
        t0 = time.monotonic()

        create_kwargs: dict = dict(
            model    = model,
            messages = [
                {"role": "system", "content": _SYSTEM_PROMPT_TEMPLATE.format(min_rr=settings.min_rr)},
                {"role": "user",   "content": user_prompt},
            ],
            temperature = 0.15,
            max_tokens  = 1800,
        )
        if use_json_fmt:
            create_kwargs["response_format"] = {"type": "json_object"}

        try:
            response = await asyncio.wait_for(
                client.chat.completions.create(**create_kwargs),
                timeout=90.0,   # Timeout máximo del socket
            )
            elapsed = time.monotonic() - t0
            raw     = response.choices[0].message.content or "{}"
            log.info("AI Strategy: respuesta en %.1fs (%d chars)", elapsed, len(raw))
            
            # --- [NUEVO] CONTROL DE OBSOLESCENCIA ---
            if elapsed > settings.ai_max_latency_s:
                log.warning("AI Strategy: descartando propuesta por latencia alta (%.1fs > %.1fs). Precio desactualizado.", elapsed, settings.ai_max_latency_s)
                return None

        except asyncio.TimeoutError:
            log.error("AI Strategy: timeout (90s) con %s", self.provider_label())
            return None
        except Exception as e:
            log.error("AI Strategy: error con %s: %s", self.provider_label(), e)
            return None

        # ── Parsear [NUEVO: Extractor Regex Robusto] ──────────────────────────
        try:
            raw = raw.strip()
            # Limpiar etiquetas <think> que meten modelos como DeepSeek-R1
            raw = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL).strip()
            
            # Extraer solo lo que parezca un JSON (Ignora texto antes o después)
            json_match = re.search(r'\{.*\}', raw, flags=re.DOTALL)
            if json_match:
                raw_json = json_match.group(0)
            else:
                raw_json = raw # Fallback por si la regex falla

            data = json.loads(raw_json)
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

        # ── Validar R:R neto ≥ settings.min_rr ────────────────────────────────────────
        sl_dist = abs(entry - sl)
        tp_dist = abs(tp    - entry)
        rt_fees = entry * TAKER_FEE_RATE * 2
        net_tp  = tp_dist - rt_fees
        net_sl  = sl_dist + rt_fees

        if net_sl <= 0 or net_tp <= 0:
            log.error("AI Strategy: distancias inválidas")
            return None

        rr = net_tp / net_sl
        if rr < settings.min_rr:
            log.warning("AI: %s rechazado por R:R insuficiente (%.2f < %.1f)", 
                        symbol, rr, settings.min_rr)
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