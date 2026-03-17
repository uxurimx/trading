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

MIN_SCORE       = 55      # opp.score mínimo para proponer
MIN_RR          = 2.0     # R:R mínimo requerido
DEFAULT_LEVERAGE = 5      # apalancamiento por defecto (configurable)
ATR_SL_MULT     = 1.5     # SL = entry ± ATR × 1.5
ATR_TP_MULT     = 3.0     # TP = entry ± ATR × 3.0 → R:R ≈ 2.0
MAX_RISK_PCT    = 1.5     # % del equity máximo por trade
MAX_MARGIN_PCT  = 35.0    # % del equity disponible que puede ir a margen
PROPOSAL_TTL    = 60      # segundos antes de que una propuesta expire


# ─── Validación ───────────────────────────────────────────────────────────────

class ValidationError(Exception):
    pass


# ─── Resultado de análisis sin posición ──────────────────────────────────────

def _atr_levels(
    side:     str,
    entry:    float,
    atr:      float,
    support:  float,
    resistance: float,
) -> Tuple[float, float]:
    """
    Calcula (sl, tp) usando ATR y soporte/resistencia.
    Para LONG: SL por debajo del soporte (o 1.5x ATR si está muy lejos).
               TP en resistencia o 3x ATR, lo que dé mejor R:R.
    Para SHORT: inverso.
    """
    if atr <= 0:
        return 0.0, 0.0

    if side == "Buy":
        sl_atr = entry - atr * ATR_SL_MULT
        # Si soporte está más cerca que el SL por ATR, usar el soporte como guía
        if support > 0 and support < entry and support > sl_atr:
            sl = support * 0.998  # ligeramente por debajo del soporte
        else:
            sl = sl_atr

        tp_atr = entry + atr * ATR_TP_MULT
        # Si hay resistencia y está más cerca que el TP por ATR → usar resistencia
        if resistance > 0 and resistance > entry and resistance < tp_atr:
            tp_from_res = resistance * 0.999
            # Si la resistencia aún da R:R aceptable, úsala
            sl_dist = entry - sl
            tp_dist_res = tp_from_res - entry
            if sl_dist > 0 and tp_dist_res / sl_dist >= MIN_RR:
                tp = tp_from_res
            else:
                tp = tp_atr
        else:
            tp = tp_atr

    else:  # Sell
        sl_atr = entry + atr * ATR_SL_MULT
        if resistance > 0 and resistance > entry and resistance < sl_atr:
            sl = resistance * 1.002
        else:
            sl = sl_atr

        tp_atr = entry - atr * ATR_TP_MULT
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
    tp_dist = abs(tp - entry)    # ganancia por contrato si TP se cumple
    sl_dist = abs(sl - entry)    # pérdida por contrato si SL se activa

    if tp_dist <= 0 or sl_dist <= 0:
        return 0.0, 0.0, 0.0, "distancias TP/SL inválidas"

    # ── Qty para alcanzar el goal ──────────────────────────────────────────
    qty_for_goal = goal_usd / tp_dist

    # ── Qty limitada por pérdida máxima aceptada ───────────────────────────
    if max_loss_usd > 0:
        # El usuario dijo "no quiero perder más de $X"
        qty_for_loss = max_loss_usd / sl_dist
    else:
        # Fallback: usar % del equity
        qty_for_loss = (equity * MAX_RISK_PCT / 100) / sl_dist

    # ── Qty limitada por margen disponible ────────────────────────────────
    max_margin    = equity * MAX_MARGIN_PCT / 100
    qty_for_margin = (max_margin * leverage) / entry  # notional = margin × lev

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

    # Calcular margen y riesgo real
    notional = qty * entry
    margin   = notional / leverage
    risk_usd = qty * sl_dist

    return qty, risk_usd, margin, ""


# ─── Strategy Engine ─────────────────────────────────────────────────────────

class StrategyEngine:
    """
    Evalúa señales y genera propuestas de órdenes.
    Solo propone — nunca ejecuta. El TradeController decide si ejecutar.
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
        max_loss_usd: float = 0.0,   # 0 = usar % del equity por defecto
    ) -> Optional[OrderRequest]:
        """
        Genera un OrderRequest para el símbolo dado.
        Retorna None si no hay setup válido.
        """
        # ── Filtros mínimos ────────────────────────────────────────────────
        if opp.score < settings.min_scan_score:
            return None

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

        # ── Niveles SL / TP ────────────────────────────────────────────────
        sl, tp = _atr_levels(
            side, entry, tech.atr_15m, tech.support, tech.resistance
        )
        if sl <= 0 or tp <= 0:
            return None

        rr = _compute_rr(side, entry, sl, tp)
        if rr < MIN_RR:
            return None

        # ── Tamaño de posición ─────────────────────────────────────────────
        qty, risk_usd, margin, err = _size_for_goal(
            goal_usd, max_loss_usd, entry, tp, sl, equity, leverage, executor, symbol
        )
        if qty <= 0:
            log.debug("propose(%s) sizing failed: %s", symbol, err)
            return None

        notional = qty * entry

        # ── Razones para la UI ─────────────────────────────────────────────
        reasons: List[str] = []
        if opp.regime.regime == "RANGING":
            reasons.append("régimen RANGO — ideal para absorción")
        elif opp.regime.regime == "ACCUMULATION":
            reasons.append("acumulación detectada")
        if tech.at_ema200:
            reasons.append("precio en EMA200 1h — soporte clave")
        if tech.ema15m_bull and side == "Buy":
            reasons.append("EMAs 15m alcistas alineadas")
        elif not tech.ema15m_bull and side == "Sell":
            reasons.append("EMAs 15m bajistas alineadas")
        reasons.extend(opp.reasons[:2])

        log.info(
            "Proposal: %s %s x%s @ %s  SL=%s TP=%s R:R=%.1f score=%d",
            side, symbol, qty, entry, sl, tp, rr, opp.score,
        )

        return OrderRequest(
            symbol      = symbol,
            side        = side,
            qty         = qty,
            order_type  = "Market",
            entry_price = entry,
            sl_price    = sl,
            tp_price    = tp,
            goal_usd    = goal_usd,
            risk_usd    = risk_usd,
            rr_ratio    = round(rr, 2),
            opp_score   = opp.score,
            notional    = round(notional, 2),
            margin      = round(margin, 2),
            leverage    = leverage,
            reasons     = reasons[:3],
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
            if opp.score <= best_score:
                continue   # no mejora el mejor encontrado hasta ahora

            proposal = self.propose(
                sym, state, opp, tech, account, goal_usd,
                executor, leverage, max_loss_usd
            )
            if proposal:
                best_score  = opp.score
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
