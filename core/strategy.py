"""
core/strategy.py
─────────────────
StrategyEngine — convierte señales de mercado + goal_usd en una propuesta
de OrderRequest lista para enviar al executor.

Lógica:
  1. Evalúa todos los símbolos monitoreados
  2. Selecciona el de mayor opp.score que pase los filtros mínimos
  3. Calcula entry, SL y TP usando ATR + soporte/resistencia
  4. Dimensiona la qty para que, si el TP se cumple, el beneficio = goal_usd
  5. Capa: la qty a que el riesgo no supere risk_pct del equity

Filtros mínimos:
  · opp.score >= MIN_SCORE (55)
  · R:R calculado >= MIN_RR (2.0)
  · notional >= min_notional del instrumento
  · riesgo <= equity × MAX_RISK_PCT (default 1.5%)
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

from core.order_model import OrderRequest, AutoMode
from core.config import settings

if TYPE_CHECKING:
    from streams.market import MarketState
    from streams.account import AccountState
    from core.regime import OpportunitySignal
    from core.technicals import TechSignal
    from core.risk import RiskFortress, RiskStatus
    from core.executor import BybitExecutor

log = logging.getLogger("qts.strategy")


# ─── Parámetros ───────────────────────────────────────────────────────────────

MIN_SCORE       = 70      # opp.score mínimo para proponer (era 55 — subido por análisis de 273 trades)
MIN_RR          = 1.3     # R:R mínimo requerido (era 2.0 — bajado para TP más alcanzable)
DEFAULT_LEVERAGE = 5      # apalancamiento por defecto (configurable)
ATR_SL_MULT     = 1.5     # SL = entry ± ATR × 1.5
ATR_TP_MULT     = 2.0     # TP = entry ± ATR × 2.0 → R:R ≈ 1.3 (era 3.0 — solo 5.5% TP hits)
MAX_RISK_PCT    = 1.5     # % del equity máximo por trade
MAX_MARGIN_PCT  = 35.0    # % del equity disponible que puede ir a margen
PROPOSAL_TTL    = 60      # segundos antes de que una propuesta expire

# ── Modo rápido (fast mode) para objetivos pequeños en activos de precio alto ─
# Cuando el goal en USD es muy pequeño relativo al precio del activo, los
# multiplicadores ATR estándar generan TPs muy lejanos en valor absoluto.
# El modo rápido usa SL y TP más ajustados (mínimo RR viable) para que el trade
# se resuelva en la mitad del recorrido.
# Ejemplo: SOL $130, goal $0.50 → ratio = 0.0038 → fast mode
#          SL: 1.0×ATR en lugar de 2.2×ATR  |  TP: 2.2×ATR en lugar de 5.0×ATR
FAST_GOAL_RATIO  = 0.008   # goal/entry < 0.8% → activar fast mode
FAST_MODE_SL_MULT = 1.0    # SL justo → más expuesto al ruido, pero trade más rápido
FAST_MODE_TP_MULT = 1.5    # TP mínimo viable con RR=1.5 (era 2.2 — demasiado lejos)

# ── Velocity boost para objetivos pequeños ────────────────────────────────────
# Cuando goal_usd < VELOCITY_GOAL_MAX, se añaden hasta +10 pts de score
# a símbolos con ATR% en rango 0.5%-3% (sweet spot para resolución rápida).
VELOCITY_GOAL_MAX = 2.0    # por debajo de este goal activar velocity scoring

# ── Costes de transacción Bybit (perpetuos) ───────────────────────────────────
TAKER_FEE_RATE  = 0.00055  # 0.055% tarifa taker (market orders)
MAKER_FEE_RATE  = 0.00020  # 0.020% tarifa maker (limit orders)
FUNDING_RATE_8H = 0.00010  # 0.010%/8h — estimación conservadora para longs
#   El funding real varía por par y condiciones de mercado.
#   Para shorts puede ser negativo (te pagan), para longs es el coste habitual.


# ─── Validación ───────────────────────────────────────────────────────────────

class ValidationError(Exception):
    pass


# ─── Resultado de análisis sin posición ──────────────────────────────────────

def _adaptive_sl_tp_mult(entry: float, atr: float) -> Tuple[float, float]:
    """
    Ajusta multiplicadores ATR según la volatilidad relativa del par (ATR%).
    Objetivo: SL que no sea víctima del ruido propio del par.

    · >3% de ATR por vela 15m → mercado muy volátil, ATR ya es amplio → sl_mult bajo
    · <0.7%                   → mercado muy quieto → sl_mult alto para evitar ruido
    R:R mínimo siempre respetado.
    """
    if entry <= 0 or atr <= 0:
        return ATR_SL_MULT, ATR_TP_MULT
    atr_pct = atr / entry

    if atr_pct > 0.030:              # >3%: mercado muy volátil
        sl_m, tp_m = 1.2, 2.0       # era (1.2, 2.8) — TP reducido
    elif atr_pct > 0.015:            # 1.5-3%: normal-alto
        sl_m, tp_m = 1.5, 2.5       # era (1.5, 3.0)
    elif atr_pct > 0.007:            # 0.7-1.5%: normal-bajo
        sl_m, tp_m = 1.8, 3.0       # era (1.8, 4.0)
    else:                            # <0.7%: muy quieto (scalping zone)
        sl_m, tp_m = 2.2, 3.5       # era (2.2, 5.0)

    # Garantizar RR >= MIN_RR
    if sl_m > 0 and tp_m / sl_m < MIN_RR:
        tp_m = sl_m * MIN_RR
    return sl_m, tp_m


def _velocity_boost(goal_usd: float, entry: float, atr: float) -> int:
    """
    Puntos adicionales de score para símbolos con ATR% en rango óptimo para
    resolución rápida de objetivos pequeños.
    Solo aplica cuando goal_usd < VELOCITY_GOAL_MAX.
    Máximo +10 pts cuando ATR% ≈ 1.5% (punto ideal).
    """
    if goal_usd >= VELOCITY_GOAL_MAX or entry <= 0 or atr <= 0:
        return 0
    atr_pct = atr / entry
    if atr_pct < 0.005 or atr_pct > 0.030:
        return 0
    # Bell curve: máximo en 1.5%, cae a 0 en los bordes (0.5% y 3%)
    center = 0.015
    width  = 0.010
    boost  = max(0.0, 1.0 - abs(atr_pct - center) / width)
    return int(boost * 10)


def _atr_levels(
    side:       str,
    entry:      float,
    atr:        float,
    support:    float,
    resistance: float,
    fast_mode:  bool = False,
) -> Tuple[float, float]:
    """
    Calcula (sl, tp) usando ATR adaptativo y soporte/resistencia.
    Para LONG: SL por debajo del soporte (o sl_mult × ATR si está muy lejos).
               TP en resistencia o tp_mult × ATR, lo que dé mejor R:R.
    Para SHORT: inverso.
    fast_mode: usa multiplicadores ajustados (1.0x SL / 2.2x TP) para trades
               que se resuelven más rápido (objetivos pequeños en activos de precio alto).
    """
    if atr <= 0:
        return 0.0, 0.0

    # Speed-level override tiene prioridad (p.e. modo NANO usa SL/TP ultra-ajustados)
    _speed = settings.speed_cfg
    if "atr_sl_mult" in _speed and "atr_tp_mult" in _speed:
        sl_mult, tp_mult = _speed["atr_sl_mult"], _speed["atr_tp_mult"]
        # Garantizar RR mínimo
        if sl_mult > 0 and tp_mult / sl_mult < MIN_RR:
            tp_mult = sl_mult * MIN_RR
    elif fast_mode:
        sl_mult, tp_mult = FAST_MODE_SL_MULT, FAST_MODE_TP_MULT
    else:
        sl_mult, tp_mult = _adaptive_sl_tp_mult(entry, atr)

    if side == "Buy":
        sl_atr = entry - atr * sl_mult
        # Si soporte está más cerca que el SL por ATR, usar el soporte como guía
        if support > 0 and support < entry and support > sl_atr:
            sl = support * 0.998  # ligeramente por debajo del soporte
        else:
            sl = sl_atr

        tp_atr = entry + atr * tp_mult
        # Si hay resistencia y está más cerca que el TP por ATR → usar resistencia
        if resistance > 0 and resistance > entry and resistance < tp_atr:
            tp_from_res = resistance * 0.999
            sl_dist = entry - sl
            tp_dist_res = tp_from_res - entry
            if sl_dist > 0 and tp_dist_res / sl_dist >= MIN_RR:
                tp = tp_from_res
            else:
                tp = tp_atr
        else:
            tp = tp_atr

    else:  # Sell
        sl_atr = entry + atr * sl_mult
        if resistance > 0 and resistance > entry and resistance < sl_atr:
            sl = resistance * 1.002
        else:
            sl = sl_atr

        tp_atr = entry - atr * tp_mult
        if support > 0 and support < entry and support > tp_atr:
            tp_from_sup = support * 1.001
            sl_dist = sl - entry
            tp_dist_sup = entry - tp_from_sup
            if sl_dist > 0 and tp_dist_sup / sl_dist >= MIN_RR:
                tp = tp_from_sup
            else:
                tp = tp_atr
        else:
            tp = tp_atr

    return sl, tp


def _compute_rr(side: str, entry: float, sl: float, tp: float) -> float:
    if side == "Buy":
        sl_d = entry - sl
        tp_d = tp - entry
    else:
        sl_d = sl - entry
        tp_d = entry - tp
    if sl_d <= 0 or tp_d <= 0:
        return 0.0
    return tp_d / sl_d


def _size_for_goal(
    goal_usd:     float,
    max_loss_usd: float,     # límite absoluto de pérdida en USD (0 = usar % del equity)
    entry:        float,
    tp:           float,
    sl:           float,
    equity:       float,
    leverage:     int,
    executor:     "BybitExecutor",
    symbol:       str,
) -> Tuple[float, float, float, str]:
    """
    Retorna (qty, risk_usd, margin, error_msg).
    Calcula qty para ganar goal_usd si TP se cumple.
    El binding constraint es: min(qty_for_goal, qty_for_max_loss, qty_for_margin).
    """
    tp_dist = abs(tp - entry)    # ganancia bruta por contrato si TP se cumple
    sl_dist = abs(sl - entry)    # pérdida bruta por contrato si SL se activa

    if tp_dist <= 0 or sl_dist <= 0:
        return 0.0, 0.0, 0.0, "distancias TP/SL inválidas"

    # ── Coste round-trip taker (entrada + salida) por unidad ──────────────
    # Bybit cobra taker fee sobre el notional en cada ejecución.
    # Calculamos el coste en precio por contrato para incluirlo en el sizing.
    rt_fee_per_unit = entry * TAKER_FEE_RATE * 2   # entrada + salida

    # Ganancia neta real por contrato si TP se cumple (bruta − fees)
    net_tp_per_unit = tp_dist - rt_fee_per_unit
    # Pérdida neta real por contrato si SL se activa (bruta + fees)
    net_sl_per_unit = sl_dist + rt_fee_per_unit

    if net_tp_per_unit <= 0:
        return 0.0, 0.0, 0.0, "TP insuficiente para cubrir comisiones"

    # ── Qty para alcanzar el goal NETO de comisiones ──────────────────────
    qty_for_goal = goal_usd / net_tp_per_unit

    # ── Qty limitada por pérdida máxima aceptada (incluyendo fees) ─────────
    if max_loss_usd > 0:
        qty_for_loss = max_loss_usd / net_sl_per_unit
    else:
        qty_for_loss = (equity * MAX_RISK_PCT / 100) / net_sl_per_unit

    # ── Qty limitada por margen disponible ────────────────────────────────
    max_margin    = equity * MAX_MARGIN_PCT / 100
    qty_for_margin = (max_margin * leverage) / entry

    # El binding constraint es el mínimo de los tres
    qty = min(qty_for_goal, qty_for_loss, qty_for_margin)

    # Redondear hacia abajo al step del instrumento
    qty = executor.round_qty(symbol, qty)
    if qty <= 0:
        return 0.0, 0.0, 0.0, "qty = 0 tras redondear (posición demasiado pequeña)"

    # Verificar mínimos del instrumento
    ok, reason = executor.validate_order(symbol, qty, entry)
    if not ok:
        return 0.0, 0.0, 0.0, reason

    # Calcular margen y riesgo real (incluyendo fees en el riesgo reportado)
    notional = qty * entry
    margin   = notional / leverage
    risk_usd = qty * net_sl_per_unit  # pérdida real si SL se activa

    return qty, risk_usd, margin, ""


# ─── Estrategias disponibles ──────────────────────────────────────────────────
#
# ABSORCION  — mercado en rango/acumulación, la liquidez institucional detiene el precio.
#              Dirección: contra la presión dominante (sell-absorption → BUY).
#              Mejor en: RANGING, ACCUMULATION.
#
# TENDENCIA  — mercado con tendencia clara. Entramos en dirección del trend,
#              nunca contra él. SL ajustado (más cerca), TP ampliado (más lejos).
#              Mejor en: TRENDING_UP (sólo LONG), TRENDING_DOWN (sólo SHORT).
#
# MOMENTUM   — impulso muy fuerte y direccional. Señal de absorción muy alta +
#              OI creciente + precio moviéndose. Quick-scalp, SL/TP más ajustados.
#              Mejor en: VOLATILE con abs_pts muy alto (≥35).
#
# El sistema selecciona la estrategia automáticamente; si ninguna aplica, pasa.

# Multiplicadores SL/TP por estrategia  (sl_mult, tp_mult)
_STRATEGY_PARAMS: dict = {
    "absorcion": (None, None),   # usa _adaptive_sl_tp_mult (dinámico por ATR%)
    "tendencia": (1.0,  2.5),    # SL justo, TP amplio para montar el trend
    "momentum":  (1.2,  1.8),    # rápido: SL/TP cortos, trade se resuelve pronto
}


def _select_strategy(opp: "OpportunitySignal", tech: "TechSignal") -> Tuple[str, str, List[str]]:
    """
    Determina qué estrategia usar y si la entrada está permitida.

    Retorna (strategy_tag, block_reason, extra_reasons).
    block_reason == "" significa que la entrada está permitida.
    """
    regime  = opp.regime.regime
    side    = "Buy" if opp.direction == "LONG" else "Sell"
    t_dir   = opp.trend_direction    # "ALCISTA" | "BAJISTA" | "NEUTRAL"
    t_score = opp.trend_score        # 0-100

    # ── Bloqueo de tendencia — REGLA FUNDAMENTAL ──────────────────────────────
    # Si hay una tendencia macro FUERTE (≥60%), nunca entramos contra ella.
    # Esto evita el error de comprar en tendencia bajista y vender en alcista.
    if t_score >= 60:
        if t_dir == "BAJISTA" and side == "Buy":
            return "", f"tendencia BAJISTA {t_score}% — no LONG", []
        if t_dir == "ALCISTA" and side == "Sell":
            return "", f"tendencia ALCISTA {t_score}% — no SHORT", []

    # También bloquear cuando el régimen es trending y la dirección es contraria
    if regime == "TRENDING_DOWN" and side == "Buy":
        return "", "régimen TRENDING_DOWN — no LONG", []
    if regime == "TRENDING_UP"   and side == "Sell":
        return "", "régimen TRENDING_UP — no SHORT", []

    # ── Selección de estrategia ───────────────────────────────────────────────

    # TENDENCIA: mercado con trend claro, operar EN dirección del trend
    if regime in ("TRENDING_UP", "TRENDING_DOWN") or t_score >= 55:
        reasons = [f"tendencia {t_dir} ({t_score}%) — entrada con el trend"]
        if tech.ema15m_bull and side == "Buy":
            reasons.append("EMAs 15m alcistas confirmadas")
        elif not tech.ema15m_bull and side == "Sell":
            reasons.append("EMAs 15m bajistas confirmadas")
        return "tendencia", "", reasons

    # MOMENTUM: señal muy fuerte de absorción + mercado volátil
    if regime == "VOLATILE" and opp.abs_pts >= 35:
        reasons = [f"impulso fuerte absorción ({opp.abs_pts}pts) en mercado volátil"]
        return "momentum", "", reasons

    # ABSORCION: mercado en rango/acumulación (caso por defecto)
    reasons: List[str] = []
    if regime == "RANGING":
        reasons.append("régimen RANGO — absorción en soporte/resistencia")
    elif regime == "ACCUMULATION":
        reasons.append("acumulación institucional detectada")
    if tech.at_ema200:
        reasons.append("precio en EMA200 1h — nivel clave")
    if tech.ema15m_bull and side == "Buy":
        reasons.append("EMAs 15m alcistas alineadas")
    elif not tech.ema15m_bull and side == "Sell":
        reasons.append("EMAs 15m bajistas alineadas")
    return "absorcion", "", reasons


# ─── Strategy Engine ─────────────────────────────────────────────────────────

class StrategyEngine:
    """
    Evalúa señales y genera propuestas usando la estrategia más adecuada al
    régimen de mercado actual. Solo propone — nunca ejecuta.

    Estrategias:
      absorcion  — default, mercado lateral/acumulación
      tendencia  — trend-following cuando hay dirección clara
      momentum   — impulso fuerte, trade rápido en mercado volátil
    """

    def propose(
        self,
        symbol:       str,
        state:        "MarketState",
        opp:          "OpportunitySignal",
        tech:         "TechSignal",
        account:      "AccountState",
        goal_usd:     float,
        executor:     "BybitExecutor",
        leverage:     int   = DEFAULT_LEVERAGE,
        max_loss_usd: float = 0.0,
    ) -> Optional[OrderRequest]:
        """
        Genera un OrderRequest para el símbolo dado usando la estrategia correcta.
        Retorna None si no hay setup válido o si la estrategia bloquea la entrada.
        """
        # ── Filtros mínimos ────────────────────────────────────────────────
        if opp.score < settings.min_scan_score:
            return None

        _MIN_RR = settings.min_rr if hasattr(settings, "min_rr") else MIN_RR

        if not opp.is_actionable:
            return None

        if not tech.has_data or tech.atr_15m <= 0:
            return None

        equity = account.balance.total_equity
        if equity <= 0:
            return None

        tk    = state.ticker
        entry = tk.last_price
        if entry <= 0:
            return None

        side = "Buy" if opp.direction == "LONG" else "Sell"

        # ── Selección de estrategia + gate de tendencia ────────────────────
        strategy_tag, block_reason, strategy_reasons = _select_strategy(opp, tech)
        if block_reason:
            log.debug("propose(%s) bloqueado: %s", symbol, block_reason)
            return None

        # ── Multiplicadores SL/TP según estrategia ─────────────────────────
        fast_mode = (goal_usd / entry < FAST_GOAL_RATIO) if entry > 0 else False
        sl_mult_override, tp_mult_override = _STRATEGY_PARAMS[strategy_tag]

        if sl_mult_override is not None:
            # Estrategia con parámetros fijos (tendencia / momentum)
            atr = tech.atr_15m
            if side == "Buy":
                sl = entry - atr * sl_mult_override
                tp = entry + atr * tp_mult_override
                if tech.support > 0 and tech.support < entry and tech.support > sl:
                    sl = tech.support * 0.998
            else:
                sl = entry + atr * sl_mult_override
                tp = entry - atr * tp_mult_override
                if tech.resistance > 0 and tech.resistance > entry and tech.resistance < sl:
                    sl = tech.resistance * 1.002
        else:
            # Absorción: SL/TP adaptativos por volatilidad
            sl, tp = _atr_levels(
                side, entry, tech.atr_15m, tech.support, tech.resistance,
                fast_mode=fast_mode,
            )

        if sl <= 0 or tp <= 0:
            return None

        rr = _compute_rr(side, entry, sl, tp)
        if rr < _MIN_RR:
            return None

        # ── Tamaño de posición ─────────────────────────────────────────────
        qty, risk_usd, margin, err = _size_for_goal(
            goal_usd, max_loss_usd, entry, tp, sl, equity, leverage, executor, symbol
        )
        if qty <= 0:
            log.debug("propose(%s) sizing failed: %s", symbol, err)
            return None

        notional = qty * entry

        # ── Razones para la UI (estrategia + señales) ─────────────────────
        reasons = strategy_reasons[:]
        reasons.extend(opp.reasons[:2])

        log.info(
            "Proposal [%s]: %s %s x%s @ %s  SL=%s TP=%s R:R=%.1f score=%d",
            strategy_tag, side, symbol, qty, entry, sl, tp, rr, opp.score,
        )

        return OrderRequest(
            symbol       = symbol,
            side         = side,
            qty          = qty,
            order_type   = "Market",
            entry_price  = entry,
            sl_price     = sl,
            tp_price     = tp,
            goal_usd     = goal_usd,
            risk_usd     = risk_usd,
            rr_ratio     = round(rr, 2),
            opp_score    = opp.score,
            notional     = round(notional, 2),
            margin       = round(margin, 2),
            leverage     = leverage,
            reasons      = reasons[:3],
            strategy_tag = strategy_tag,
        )

    def scan_all(
        self,
        symbols:      List[str],
        states:       Dict[str, "MarketState"],
        opps:         Dict[str, "OpportunitySignal"],
        techs:        Dict[str, "TechSignal"],
        account:      "AccountState",
        goal_usd:     float,
        executor:     "BybitExecutor",
        leverage:     int   = DEFAULT_LEVERAGE,
        max_loss_usd: float = 0.0,
    ) -> Optional[Tuple[str, OrderRequest]]:
        """
        Escanea todos los símbolos y retorna (symbol, best_proposal).
        Elige el de mayor opp.score que genere una propuesta válida.
        """
        best_score  = -1
        best_result: Optional[Tuple[str, OrderRequest]] = None

        for sym in symbols:
            state = states.get(sym)
            opp   = opps.get(sym)
            tech  = techs.get(sym)
            if not state or not opp or not tech:
                continue

            # Velocity boost: para objetivos pequeños, preferir símbolos con
            # ATR% en rango óptimo para resolución rápida (0.5%-3%).
            entry = state.ticker.last_price if state.ticker.last_price > 0 else 0
            vboost = _velocity_boost(goal_usd, entry, tech.atr_15m)
            effective_score = opp.score + vboost

            if effective_score <= best_score:
                continue   # no mejora el mejor encontrado hasta ahora

            proposal = self.propose(
                sym, state, opp, tech, account, goal_usd,
                executor, leverage, max_loss_usd
            )
            if proposal:
                best_score  = effective_score
                best_result = (sym, proposal)

        return best_result

    def simulate(
        self,
        equity:       float,
        goal_usd:     float,
        max_loss_usd: float,
        entry:        float,
        atr:          float,
        leverage:     int,
        executor:     "BybitExecutor",
        symbol:       str,
    ) -> dict:
        """
        Calcula lo que el sistema HARÍA con estos parámetros, sin ejecutar nada.
        Retorna un dict con todos los números para mostrar en la UI.
        """
        if atr <= 0 or entry <= 0 or equity <= 0:
            return {"error": "datos de mercado no disponibles aún"}

        sl_dist = atr * ATR_SL_MULT
        tp_dist = atr * ATR_TP_MULT
        sl      = entry - sl_dist   # LONG como ejemplo
        tp      = entry + tp_dist
        rr      = tp_dist / sl_dist if sl_dist > 0 else 0

        # Qty para cada constraint
        qty_goal    = goal_usd / tp_dist          if tp_dist > 0 else 0
        qty_loss    = max_loss_usd / sl_dist      if sl_dist > 0 and max_loss_usd > 0 else qty_goal
        qty_margin  = (equity * MAX_MARGIN_PCT / 100 * leverage) / entry if entry > 0 else 0

        qty_raw     = min(qty_goal, qty_loss, qty_margin)
        qty         = executor.round_qty(symbol, qty_raw)

        if qty <= 0:
            info      = executor.get_info(symbol)
            qty       = info.min_qty
            is_capped = True
        else:
            is_capped = qty < qty_goal * 0.95  # true si algún límite cortó la qty

        notional    = qty * entry
        margin      = notional / leverage
        real_profit = qty * tp_dist
        real_loss   = qty * sl_dist
        binding     = (
            "margen disponible" if qty_raw >= qty_goal and qty_raw >= qty_loss else
            "límite de pérdida" if qty_loss <= qty_goal else
            "goal"
        )

        # Cuánto me falta de equity para lograr el goal exacto
        qty_needed  = goal_usd / tp_dist if tp_dist > 0 else 0
        margin_needed = qty_needed * entry / leverage
        equity_gap  = max(0, margin_needed - equity * MAX_MARGIN_PCT / 100)

        return {
            # Parámetros de la orden
            "symbol":       symbol,
            "entry":        entry,
            "sl":           sl,
            "tp":           tp,
            "qty":          qty,
            "leverage":     leverage,
            "notional":     round(notional, 2),
            "margin":       round(margin, 2),
            "rr":           round(rr, 2),
            # Resultados esperados
            "real_profit":  round(real_profit, 2),   # si TP se cumple
            "real_loss":    round(real_loss, 2),      # si SL se activa
            # Diagnosis
            "goal_requested":  goal_usd,
            "loss_requested":  max_loss_usd,
            "goal_achievable": round(real_profit, 2),
            "is_capped":       is_capped,
            "binding_limit":   binding,
            "equity_gap":      round(equity_gap, 2),  # equity adicional para lograr goal exacto
            # Constraints individuales
            "qty_for_goal":   round(qty_goal, 1),
            "qty_for_loss":   round(qty_loss, 1),
            "qty_for_margin": round(qty_margin, 1),
        }

    def max_achievable_goal(
        self,
        equity:     float,
        entry:      float,
        atr:        float,
        leverage:   int,
        executor:   "BybitExecutor",
        symbol:     str,
    ) -> float:
        """
        Dado el equity actual, calcula el máximo goal_usd realista.
        Útil para ajustar la UI cuando el usuario pide demasiado.
        """
        if equity <= 0 or atr <= 0 or entry <= 0:
            return 0.0

        # Margen máximo = MAX_MARGIN_PCT del equity
        max_margin   = equity * MAX_MARGIN_PCT / 100
        max_notional = max_margin * leverage
        max_qty      = executor.round_qty(symbol, max_notional / entry)

        # Riesgo máximo = MAX_RISK_PCT del equity
        sl_dist = atr * ATR_SL_MULT
        max_qty_risk = equity * MAX_RISK_PCT / 100 / sl_dist if sl_dist > 0 else 0
        qty = min(max_qty, executor.round_qty(symbol, max_qty_risk))

        tp_dist = atr * ATR_TP_MULT
        return round(qty * tp_dist, 2)
