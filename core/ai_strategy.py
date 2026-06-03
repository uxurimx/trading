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
from core.logger import strategy_logger

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

  ⚠ OVEREXTENSION (trend_score > 85):
  Cuando la tendencia está sobreextendida (trend_score > 85), el precio puede revertir bruscamente.
  En estos casos exige CVD = 5/5 en la dirección de la tendencia para confirmar que AÚN hay fuerza.
  Si CVD < 5/5 con trend_score > 85 → rechazar ese candidato (riesgo de agotamiento).

═══ CVD — definición cuantitativa (REGLA ESTRICTA) ═══
  CVD=X/5bull significa que X de las últimas 5 velas tuvieron delta positivo.
  • LONG alineado:  CVD ≥ 4/5 bull  (presión compradora clara)
  • SHORT alineado: CVD ≤ 1/5 bull  (presión vendedora clara = ≥ 4/5 bear)
  ▶ No basta con mayoría simple — se exige presión CLARA (4/5 mínimo).
  ✗ CVD=3/5 bull con dir=LONG → NO ALINEADO (insuficiente).
  ✗ CVD=2/5 bull con dir=SHORT → NO ALINEADO (insuficiente).

═══ SL / TP — cálculo ═══
  Fees round-trip = 0.11% × entry (ya provistos como rt_fees en cada candidato).
  R:R neto base requerido = {min_rr}

  ⚠ FILTRO DE REVERSIÓN cerca de S/R:
  Si el precio está dentro de 1×ATR de la Resistencia (R) en un LONG,
  o dentro de 1×ATR del Soporte (S) en un SHORT → exige R:R neto ≥ 3.0.
  El precio en S/R adverso indica riesgo elevado de rebote.

  Orden de preferencia para SL/TP:
    1. Usa el nivel S (soporte) o R (resistencia) más cercano del candidato.
    2. Si no hay S/R útil: SL = 1.5×ATR, TP = 4.0×ATR desde entry.
       (Con ATR ≥ 0.4% esto garantiza R:R ≥ {min_rr} después de fees.)

  ▶ Siempre verifica la fórmula R:R antes de responder.
  ▶ Si el nivel S/R más cercano no da R:R neto requerido, usa 4×ATR como TP.

═══ PROCESO obligatorio ═══
  1. Ordena los candidatos por score (mayor primero).
  2. Para cada uno (empezando por el top):
     a. Confirma dirección vs tendencia. Si trend_score > 85, aplica regla overextension.
     b. Verifica CVD con la regla estricta (4/5 mínimo).
     c. Verifica si el precio está cerca de S/R adverso (aplica R:R ≥ 3.0 si aplica).
     d. Calcula SL y TP (usa S/R o ATR×multiplicador).
     e. Calcula R:R neto. Si ≥ umbral requerido → TRADE. Detén el análisis.
  3. Solo retorna NO_TRADE si TODOS los candidatos fallan estos criterios.

DINÁMICA DE VOLUMEN:
- La 'Velocidad de Cinta' (Tape Speed) indica urgencia. Solo considera trades con Tape Speed > 0.5.
- Ignora monedas con bajo volumen o ruido donde el ATR sea puramente por falta de liquidez.

FORMATO DE RESPUESTA: solo JSON válido, sin texto, sin markdown.

Si hay trade:
{{"action":"TRADE","symbol":"SOLUSDT","side":"Buy","entry":145.50,"sl":143.80,"tp":150.90,"confidence":79,"reasoning":"SOL score=73, ALCISTA 68%, CVD=4/5 bull (alineado LONG), EMA↑. SL=1.5×ATR(1.13)=143.80, TP en resistencia 150.90. rt_fees=0.16. R:R=(5.40-0.16)/(1.70+0.16)=2.82. Precio NO cerca de R adverso."}}

Si ninguno califica:
{{"action":"NO_TRADE","reasoning":"Candidato A: CVD=3/5 bull en LONG (insuficiente, exige 4/5). Candidato B: trend_score=88 overextended + CVD=4/5 (exige 5/5). Candidato C: R:R neto=1.8 insuficiente."}}
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
    _MIN_VOL_24H = 5_000_000.0   # $5M USDT — mínimo de liquidez para ejecución segura de SL
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
        # Filtro de liquidez: usar turnover_24h (USDT) si disponible,
        # fallback a volume_24h × price (base coin × precio = USDT aproximado)
        tk = ms.ticker
        vol_usdt = tk.turnover_24h if tk.turnover_24h > 0 else tk.volume_24h * price
        if vol_usdt < _MIN_VOL_24H:
            continue
        atr_pct = tech.atr_15m / price * 100
        if atr_pct < settings.ai_min_atr_pct:
            continue  # ATR demasiado pequeño — fees comerían todo el profit
        candidates.append((opp.score, sym))
    candidates.sort(reverse=True)
    top = candidates[:settings.ai_top_symbols]

    # Contexto de temporalidad para el modelo
    _SPEED_CONTEXT = {
        "nano":     ("nano — scalping extremo",   "1m/3m",   "30-120s",  "0.5-1.5×ATR"),
        "scalp":    ("scalp — operativa rápida",  "1m/15m",  "2-15min",  "1-2×ATR"),
        "fast":     ("fast — intradía corto",     "5m/30m",  "15-60min", "1.5-2.5×ATR"),
        "standard": ("standard — intradía normal","15m/1h",  "1-4h",     "2-4×ATR"),
    }
    spd = settings.speed_level
    sp_desc, sp_tfs, sp_dur, sp_tp = _SPEED_CONTEXT.get(spd, _SPEED_CONTEXT["standard"])
    speed_ctx = (
        f"=== MODO DE OPERACIÓN: {sp_desc.upper()} ===\n"
        f"Temporalidad: {sp_tfs}  |  Duración esperada: {sp_dur}  |  TP objetivo: {sp_tp}\n"
        f"Criterio: entradas precisas en {sp_tfs}, SL/TP ajustados a este horizonte temporal.\n"
    )

    if not top:
        return (
            speed_ctx
            + f"=== SIN CANDIDATOS VÁLIDOS ===\n"
            f"(score ≥ {settings.ai_min_score} Y ATR ≥ {settings.ai_min_atr_pct}% Y R:R ≥ {settings.min_rr})\n"
            "Mercado en baja volatilidad — esperar condiciones mejores."
        )

    lines = [speed_ctx + f"=== TOP {len(top)} CANDIDATOS (score ≥ {settings.ai_min_score}, ATR ≥ {settings.ai_min_atr_pct}%) ==="]

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
            cvd_str = f"CVD={bull}/5bull({bear}/5bear) {ms.cvd_momentum}"
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
    avail = bal.available_balance
    if avail <= 0:
        avail = max(0.0, bal.total_equity - bal.used_margin) or bal.wallet_balance
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
        self.last_scan_reason: str = ""   # razón del último "sin propuesta" (para UI)

    def is_ready(self) -> bool:
        provider = getattr(settings, "ai_provider", "openai")
        if provider == "claude":
            import shutil
            return shutil.which("claude") is not None
        if provider == "openai":
            return bool(getattr(settings, "openai_api_key", ""))
        if provider == "ollama":
            return bool(getattr(settings, "ollama_host", ""))
        if provider == "compatible":
            return bool(getattr(settings, "ai_compat_url", "")) and bool(getattr(settings, "ai_compat_model", ""))
        return False

    def provider_label(self) -> str:
        provider = getattr(settings, "ai_provider", "openai")
        if provider == "claude":
            return "Claude(claude-sonnet-4-6)"
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
    ) -> Optional[Tuple[str, "OrderRequest", dict]]:
        import asyncio
        if getattr(settings, "ai_provider", "openai") != "claude":
            try:
                import openai as _openai  # noqa: F401
            except ImportError:
                log.error("openai no instalado — ejecutar: pip install openai")
                return None

        if not self.is_ready():
            log.warning("AI Strategy: proveedor '%s' no configurado",
                        getattr(settings, "ai_provider", "openai"))
            return None

        with strategy_logger.context() as trace_id:
            self._last_call_ts = time.monotonic()

            # Contar candidatos que pasan TODOS los filtros (score + ATR + volumen 24h)
            _MIN_VOL_24H = 5_000_000.0
            n_candidates = 0
            n_low_score = n_low_vol = n_low_atr = n_no_data = 0
            top_all: list = []   # (score, sym) para todos con datos

            self.last_scan_reason = ""
            for s in symbols:
                opp  = opps.get(s)
                tech = techs.get(s)
                ms   = states.get(s)
                if not opp or not tech or not ms or ms.ticker.last_price <= 0:
                    n_no_data += 1
                    continue
                top_all.append((opp.score, s, tech, ms))
                if opp.score < settings.ai_min_score:
                    n_low_score += 1
                    continue
                tk = ms.ticker
                vol_usdt = tk.turnover_24h if tk.turnover_24h > 0 else tk.volume_24h * tk.last_price
                if vol_usdt < _MIN_VOL_24H:
                    n_low_vol += 1
                    continue
                if tech.atr_15m / ms.ticker.last_price * 100 < settings.ai_min_atr_pct:
                    n_low_atr += 1
                    continue
                n_candidates += 1

            if n_candidates == 0:
                top_all.sort(reverse=True)
                top_info = ""
                if top_all:
                    ts, sym, tech, ms = top_all[0]
                    price = ms.ticker.last_price
                    atr_pct = tech.atr_15m / price * 100 if price > 0 else 0
                    tk = ms.ticker
                    vol_usdt = tk.turnover_24h if tk.turnover_24h > 0 else tk.volume_24h * price
                    vol_m = vol_usdt / 1_000_000
                    _regime = getattr(ms, "regime", "")
                    regime = _regime.value if hasattr(_regime, "value") else str(_regime)
                    top_info = (
                        f" | top={sym.replace('USDT', '')} score={ts}"
                        f" ATR={atr_pct:.2f}% vol=${vol_m:.1f}M {regime}"
                    )
                reason = (
                    f"sin candidatos (score<{settings.ai_min_score}:{n_low_score}"
                    f" vol<$5M:{n_low_vol}"
                    f" ATR<{settings.ai_min_atr_pct}%:{n_low_atr}"
                    f" sin_datos:{n_no_data}){top_info}"
                )
                self.last_scan_reason = reason
                log.info("AI Strategy: sin propuesta — %s", reason)
                return None, None, {}

            market_snapshot  = _build_market_snapshot(symbols, states, opps, techs)
            account_snapshot = _build_account_snapshot(account, active_trades)

            # Log del contexto enviado a la IA
            strategy_logger.info("ANALYSIS_START", "Iniciando análisis de mercado con IA", {
                "n_candidates": n_candidates,
                "n_symbols": len(symbols),
                "goal_usd": goal_usd,
                "leverage": leverage
            })

            user_prompt = (
                f"{account_snapshot}\n\n"
                f"{market_snapshot}\n\n"
                "=== INSTRUCCIONES ===\n"
                f"Goal por trade: ${goal_usd:.2f} USD  |  Leverage: {leverage}x\n"
                "Fees ya incluidas en rt_fees de cada candidato (0.11% round-trip).\n\n"
                "Evalúa candidatos de mayor a menor score.\n"
                "CVD LONG alineado: ≥ 4/5 bull.  CVD SHORT alineado: ≤ 1/5 bull (= ≥ 4/5 bear).\n"
                "Si trend_score > 85: exige CVD = 5/5 en la dirección (overextension guard).\n"
                "Si precio dentro de 1×ATR de S/R adverso: exige R:R neto ≥ 3.0.\n"
                "Para SL/TP: usa niveles S/R si están disponibles; si no, usa RefSL y RefTP del candidato.\n"
                f"Si RefTP no da R:R requerido, busca el nivel S/R más lejano que sí lo dé.\n"
                "Un score ≥ 60 con dirección coherente y CVD ≥ 4/5 ES suficiente.\n"
                "Responde SOLO con el JSON."
            )

            log.info(
                "AI Strategy: consultando %s — %d candidatos de %d símbolos",
                self.provider_label(), n_candidates, len(symbols),
            )
            t0 = time.monotonic()

            # ── Claude CLI provider ───────────────────────────────────────────
            if getattr(settings, "ai_provider", "openai") == "claude":
                return await self._call_claude_cli(
                    user_prompt, t0, symbols, opps, techs, executor, leverage,
                )

            try:
                client, model, use_json_fmt = self._make_client_and_model()
            except Exception as e:
                log.error("AI Strategy: error al crear cliente: %s", e)
                strategy_logger.error("CLIENT_ERROR", f"Error al crear cliente IA: {e}")
                return None

            json_reminder = "" if use_json_fmt else "\nIMPORTANTE: responde ÚNICAMENTE con el JSON, sin ningún texto adicional."
            user_prompt = user_prompt + json_reminder

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
                usage   = response.usage
                token_info = {
                    "model":  model,
                    "prompt": usage.prompt_tokens,
                    "comp":   usage.completion_tokens
                } if usage else {}

                log.info("AI Strategy: respuesta en %.1fs (%d chars) | Tokens: %s", 
                         elapsed, len(raw), usage.total_tokens if usage else "?")
                
                strategy_logger.info("RAW_RESPONSE", "Respuesta recibida del LLM", {
                    "elapsed_s": elapsed,
                    "raw_content": raw,
                    "tokens": token_info
                })

                # --- [NUEVO] CONTROL DE OBSOLESCENCIA ---
                if elapsed > settings.ai_max_latency_s:
                    log.warning("AI Strategy: descartando propuesta por latencia alta (%.1fs > %.1fs). Precio desactualizado.", elapsed, settings.ai_max_latency_s)
                    strategy_logger.warning("LATENCY_REJECT", f"Latencia excesiva: {elapsed:.1f}s", {"max_allowed": settings.ai_max_latency_s})
                    return None

            except asyncio.TimeoutError:
                log.error("AI Strategy: timeout (90s) con %s", self.provider_label())
                strategy_logger.error("TIMEOUT", f"Timeout de 90s con {self.provider_label()}")
                return None
            except Exception as e:
                log.error("AI Strategy: error con %s: %s", self.provider_label(), e)
                strategy_logger.error("LLM_ERROR", f"Error de comunicación con LLM: {e}")
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
                strategy_logger.error("PARSE_ERROR", f"Error al parsear JSON: {e}", {"raw": raw})
                return None

            action    = data.get("action", "NO_TRADE")
            reasoning = data.get("reasoning", "Sin razonamiento.")

            if action != "TRADE":
                log.info("AI Strategy: NO_TRADE — %s", reasoning[:200])
                strategy_logger.info("NO_TRADE", "La IA decidió no operar", {"reasoning": reasoning})
                self.last_scan_reason = f"NO_TRADE: {reasoning[:150]}"
                return None, None, token_info

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
                strategy_logger.error("VALIDATION_ERROR", f"Valores numéricos inválidos: {e}", {"data": data})
                return None

            if not symbol or side not in ("Buy", "Sell") or entry <= 0 or sl <= 0 or tp <= 0:
                log.error("AI Strategy: campos obligatorios faltantes: %s", data)
                strategy_logger.error("VALIDATION_ERROR", "Campos obligatorios faltantes", {"data": data})
                return None

            # Normalizar símbolo: solo se admiten pares USDT de futuros perpetuos.
            # Corregir typos comunes antes de buscar en la lista.
            if not symbol.endswith("USDT"):
                # Intento 1: typo *SDT → *USDT (ej. UAISDT → UAIUSDT)
                if symbol.endswith("SDT"):
                    symbol = symbol[:-3] + "USDT"
                # Intento 2: símbolo sin sufijo (ej. "UAI" → "UAIUSDT")
                elif not any(symbol.endswith(s) for s in ("BTC", "ETH", "BNB")):
                    symbol = symbol + "USDT"
                else:
                    # El AI devolvió un par no-USDT (ej. UAIBTC) — rechazar
                    log.error("AI Strategy: símbolo '%s' no es par USDT — ignorando", symbol)
                    strategy_logger.warning("SYMBOL_ERROR",
                                            f"Símbolo {symbol} no es par USDT perpetuo",
                                            {"raw_symbol": symbol})
                    return None

            if symbol not in symbols:
                log.error("AI Strategy: símbolo '%s' no está en la lista monitoreada", symbol)
                strategy_logger.warning("SYMBOL_ERROR", f"Símbolo {symbol} no monitoreado")
                return None

            # ── Validar dirección coherente ─────────────────────────────────────
            opp = opps.get(symbol)
            if opp and opp.trend_score >= 60:
                if opp.trend_direction == "ALCISTA" and side == "Sell":
                    log.warning("AI Strategy: propuesta SHORT en tendencia ALCISTA %d%% — rechazando", opp.trend_score)
                    strategy_logger.warning("TREND_REJECT", "Propuesta contra tendencia fuerte", {"trend": opp.trend_direction, "side": side})
                    return None
                if opp.trend_direction == "BAJISTA" and side == "Buy":
                    log.warning("AI Strategy: propuesta LONG en tendencia BAJISTA %d%% — rechazando", opp.trend_score)
                    strategy_logger.warning("TREND_REJECT", "Propuesta contra tendencia fuerte", {"trend": opp.trend_direction, "side": side})
                    return None

            # ── Validar geometría SL/TP vs side ────────────────────────────────
            if side == "Buy":
                if sl >= entry:
                    log.error("AI Strategy: LONG pero SL(%.5g) >= entry(%.5g)", sl, entry)
                    strategy_logger.error("GEOMETRY_ERROR", "SL >= Entry en LONG", {"sl": sl, "entry": entry})
                    return None
                if tp <= entry:
                    log.error("AI Strategy: LONG pero TP(%.5g) <= entry(%.5g)", tp, entry)
                    strategy_logger.error("GEOMETRY_ERROR", "TP <= Entry en LONG", {"tp": tp, "entry": entry})
                    return None
            else:
                if sl <= entry:
                    log.error("AI Strategy: SHORT pero SL(%.5g) <= entry(%.5g)", sl, entry)
                    strategy_logger.error("GEOMETRY_ERROR", "SL <= Entry en SHORT", {"sl": sl, "entry": entry})
                    return None
                if tp >= entry:
                    log.error("AI Strategy: SHORT pero TP(%.5g) >= entry(%.5g)", tp, entry)
                    strategy_logger.error("GEOMETRY_ERROR", "TP >= Entry en SHORT", {"tp": tp, "entry": entry})
                    return None

            # ── Validar R:R neto ≥ settings.min_rr ────────────────────────────────────────
            sl_dist = abs(entry - sl)
            tp_dist = abs(tp    - entry)
            rt_fees = entry * TAKER_FEE_RATE * 2
            net_tp  = tp_dist - rt_fees
            net_sl  = sl_dist + rt_fees

            if net_sl <= 0 or net_tp <= 0:
                log.error("AI Strategy: distancias inválidas")
                strategy_logger.error("RR_ERROR", "Diferencia de precio insuficiente para cubrir fees")
                return None

            rr = net_tp / net_sl
            if rr < settings.min_rr:
                log.warning("AI: %s rechazado por R:R insuficiente (%.2f < %.1f)", 
                            symbol, rr, settings.min_rr)
                strategy_logger.warning("RR_REJECT", f"R:R insuficiente: {rr:.2f}", {"min_required": settings.min_rr})
                return None

            # ── Sizing ─────────────────────────────────────────────────────────
            # Slippage buffer del 15%: reduce qty para que el riesgo real (con slippage)
            # no supere el planeado. Equivale a calcular el tamaño como si el riesgo
            # fuera 15% mayor al esperado, absorbiendo movimientos de precio al ejecutar SL.
            _SLIPPAGE_BUFFER = 1.15
            net_tp_unit = tp_dist - rt_fees
            qty = goal_usd / (net_tp_unit * _SLIPPAGE_BUFFER)

            # Calcular balance disponible real (mismo fallback que _run_scan)
            _bal   = account.balance
            _avail = _bal.available_balance
            if _avail <= 0:
                _avail = max(0.0, _bal.total_equity - _bal.used_margin) or _bal.wallet_balance

            # CAP: no usar más del 90% del disponible × apalancamiento
            if _avail > 0:
                max_notional = _avail * leverage * 0.90
                max_qty_bal  = max_notional / entry if entry > 0 else qty
                if qty > max_qty_bal:
                    log.warning(
                        "AI Sizing: qty %.2f → %.2f (cap por balance $%.2f × %dx)",
                        qty, max_qty_bal, _avail, leverage,
                    )
                    qty = max_qty_bal

            qty = executor.round_qty(symbol, qty)
            if qty <= 0:
                log.warning("AI Strategy: qty=0 tras redondear para %s (balance $%.2f)", symbol, _avail)
                strategy_logger.warning("SIZE_ERROR", "Cantidad 0 — balance insuficiente para el mínimo",
                                        {"available": _avail, "leverage": leverage})
                return None

            ok, reason = executor.validate_order(symbol, qty, entry)
            if not ok:
                log.warning("AI Strategy: orden inválida %s qty=%s: %s", symbol, qty, reason)
                strategy_logger.warning("VALIDATION_REJECT", f"Filtros de Bybit: {reason}")
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
                trace_id     = trace_id,
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

            strategy_logger.info("PROPOSAL_READY", f"Propuesta generada para {symbol}", {
                "symbol": symbol,
                "side": side,
                "entry": entry,
                "sl": sl,
                "tp": tp,
                "rr": rr,
                "qty": qty,
                "risk_usd": risk_usd,
                "reasoning": reasoning
            })

            return symbol, req, token_info

    # ── Claude CLI ────────────────────────────────────────────────────────────

    async def _call_claude_cli(
        self,
        user_prompt: str,
        t0: float,
        symbols: list,
        opps: dict,
        techs: dict,
        executor,
        leverage: int,
    ):
        import asyncio
        system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(min_rr=settings.min_rr)
        full_prompt   = f"{system_prompt}\n\n{user_prompt}"

        try:
            proc = await asyncio.create_subprocess_exec(
                "claude", "--print", "--output-format", "text",
                "--model", "claude-sonnet-4-6",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(full_prompt.encode()),
                timeout=90.0,
            )
            raw     = stdout.decode().strip()
            elapsed = time.monotonic() - t0

            strategy_logger.info("RAW_RESPONSE", "Respuesta recibida de Claude CLI", {
                "elapsed_s": round(elapsed, 2),
                "raw_content": raw[:500],
            })

            if elapsed > settings.ai_max_latency_s:
                log.warning("AI Strategy: descartando por latencia alta (%.1fs)", elapsed)
                return None, None, {}

        except asyncio.TimeoutError:
            log.error("AI Strategy: timeout (90s) con Claude CLI")
            strategy_logger.error("TIMEOUT", "Timeout 90s con Claude CLI")
            return None, None, {}
        except Exception as e:
            log.error("AI Strategy: error Claude CLI: %s", e)
            strategy_logger.error("LLM_ERROR", f"Error Claude CLI: {e}")
            return None, None, {}

        # Parsear JSON — mismo extractor robusto
        try:
            raw = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL).strip()
            json_match = re.search(r'\{.*\}', raw, flags=re.DOTALL)
            data = json.loads(json_match.group(0) if json_match else raw)
        except json.JSONDecodeError as e:
            log.error("AI Strategy (Claude): JSON inválido: %s\n%s", e, raw[:300])
            strategy_logger.error("PARSE_ERROR", f"JSON inválido de Claude: {e}", {"raw": raw[:300]})
            return None, None, {}

        token_info = {"model": "claude-sonnet-4-6"}
        action    = data.get("action", "NO_TRADE")
        reasoning = data.get("reasoning", "Sin razonamiento.")

        # Guardar decisión en PostgreSQL (SKIP también se guarda)
        try:
            from core.trading_db import decision_save
            _active_sid = getattr(settings, "_active_trading_session_id", None)
            if _active_sid:
                decision_save(
                    session_id=_active_sid,
                    symbol=data.get("symbol", symbols[0] if symbols else ""),
                    decision_type="ENTER" if action == "TRADE" else "SKIP",
                    action=action,
                    reasoning=reasoning,
                    confidence=int(data.get("confidence", 0) or 0),
                    signals_json={"raw": data},
                    executed=action == "TRADE",
                    latency_ms=int(elapsed * 1000),
                )
        except Exception as db_err:
            log.warning("trading_db: no se pudo guardar decisión: %s", db_err)

        if action != "TRADE":
            log.info("AI Strategy (Claude): NO_TRADE — %s", reasoning[:200])
            strategy_logger.info("NO_TRADE", "Claude decidió no operar", {"reasoning": reasoning})
            self.last_scan_reason = f"NO_TRADE: {reasoning[:150]}"
            return None, None, token_info

        # Extraer campos — mismo flujo que OpenAI
        symbol = str(data.get("symbol", "")).strip().upper()
        side   = str(data.get("side",   "")).strip()
        try:
            entry = float(data.get("entry", 0) or 0)
            sl    = float(data.get("sl",    0) or 0)
            tp    = float(data.get("tp",    0) or 0)
            conf  = int(data.get("confidence", 70) or 70)
        except (TypeError, ValueError) as e:
            log.error("AI Strategy (Claude): valores numéricos inválidos: %s", e)
            return None, None, {}

        if not symbol or side not in ("Buy", "Sell") or entry <= 0 or sl <= 0 or tp <= 0:
            log.error("AI Strategy (Claude): campos obligatorios faltantes: %s", data)
            return None, None, {}

        if not symbol.endswith("USDT"):
            symbol = symbol + "USDT"
        if symbol not in symbols:
            log.error("AI Strategy (Claude): símbolo '%s' no monitoreado", symbol)
            return None, None, {}

        # Mismas validaciones de dirección y geometría que OpenAI
        opp = opps.get(symbol)
        if opp and opp.trend_score >= 60:
            if opp.trend_direction == "ALCISTA" and side == "Sell":
                log.warning("AI Strategy (Claude): SHORT contra tendencia ALCISTA — rechazando")
                return None, None, token_info
            if opp.trend_direction == "BAJISTA" and side == "Buy":
                log.warning("AI Strategy (Claude): LONG contra tendencia BAJISTA — rechazando")
                return None, None, token_info

        if side == "Buy"  and not (sl < entry < tp):
            log.error("AI Strategy (Claude): geometría LONG inválida sl=%.6f entry=%.6f tp=%.6f", sl, entry, tp)
            return None, None, {}
        if side == "Sell" and not (tp < entry < sl):
            log.error("AI Strategy (Claude): geometría SHORT inválida tp=%.6f entry=%.6f sl=%.6f", tp, entry, sl)
            return None, None, {}

        # Calcular qty y OrderRequest reutilizando la misma lógica
        from core.order_model import OrderRequest
        tech      = techs.get(symbol)
        ms        = None
        balance   = executor.paper_balance if getattr(settings, "paper_trading", False) else 0
        risk_pct  = getattr(settings, "max_daily_loss_pct", 1.5) / getattr(settings, "max_trades_per_day", 50)
        risk_usd  = balance * min(risk_pct / 100, 0.02)
        sl_dist   = abs(entry - sl)
        qty       = round(risk_usd / sl_dist, 2) if sl_dist > 0 else 1.0
        qty       = max(qty, 1.0)

        sl_pct    = sl_dist / entry * 100
        tp_dist   = abs(tp - entry)
        rr        = tp_dist / sl_dist if sl_dist > 0 else 0
        fees_rt   = entry * TAKER_FEE_RATE * 2
        net_rr    = (tp_dist - fees_rt) / (sl_dist + fees_rt) if (sl_dist + fees_rt) > 0 else 0

        if net_rr < settings.min_rr:
            log.warning("AI Strategy (Claude): R:R neto %.2f < mínimo %.2f", net_rr, settings.min_rr)
            return None, None, token_info

        req = OrderRequest(
            symbol=symbol, side=side, entry_price=entry,
            sl_price=sl, tp_price=tp, qty=qty,
            leverage=leverage, reasoning=reasoning,
            confidence=conf, strategy_tag="claude_agent",
        )

        strategy_logger.info("PROPOSAL_READY", f"Claude propone {symbol}", {
            "symbol": symbol, "side": side, "entry": entry,
            "sl": sl, "tp": tp, "rr": round(rr, 2), "qty": qty,
            "confidence": conf, "reasoning": reasoning,
        })

        return symbol, req, token_info


# ── Singleton ──────────────────────────────────────────────────────────────────
ai_agent = AIStrategyAgent()