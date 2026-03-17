"""
core/status_writer.py
─────────────────────
Escribe /tmp/qts_status.json cada N ciclos de UI (≈ 2 s).
Leído por la extensión GNOME Shell (Phase 6).

Escritura atómica: escribe en .tmp y hace rename para evitar
que la extensión lea un JSON a medias.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from streams.market import MarketState
    from streams.account import AccountState
    from core.absorption import AbsorptionSignal
    from core.regime import OpportunitySignal
    from core.risk import RiskStatus
    from core.trend import TrendSignal


_STATUS_PATH = Path("/tmp/qts_status.json")
_TMP_PATH    = Path("/tmp/qts_status.tmp")


class StatusWriter:
    """
    Serializa el estado completo de QTS a JSON para la extensión GNOME Shell.
    Llamar .tick() en cada ciclo de UI; escribe solo cada WRITE_EVERY ciclos.
    """

    WRITE_EVERY = 20   # ciclos de 100 ms → cada ~2 segundos

    def __init__(self) -> None:
        self._counter = 0

    def tick(
        self,
        symbol:     str,
        state:      "MarketState",
        sig:        "AbsorptionSignal",
        opp:        "OpportunitySignal",
        risk:       "RiskStatus",
        trend:      "TrendSignal",
        acct_state: "AccountState",
    ) -> None:
        self._counter += 1
        if self._counter < self.WRITE_EVERY:
            return
        self._counter = 0
        try:
            self._write(symbol, state, sig, opp, risk, trend, acct_state)
        except Exception:
            pass  # nunca debe interrumpir el ciclo de UI

    # ── Serialización ─────────────────────────────────────────────────────────

    def _write(
        self,
        symbol:     str,
        state:      "MarketState",
        sig:        "AbsorptionSignal",
        opp:        "OpportunitySignal",
        risk:       "RiskStatus",
        trend:      "TrendSignal",
        acct_state: "AccountState",
    ) -> None:
        tk       = state.ticker
        sym      = symbol.replace("USDT", "").replace("PERP", "")

        data: dict = {
            "ts":         int(time.time()),
            "symbol":     sym,
            "price":      tk.last_price,
            "change_pct": tk.price_change_pct,
            "absorption": {
                "is_signal": sig.is_signal,
                "side":      sig.side,        # "BUY" | "SELL" | "NEUTRAL"
                "score":     sig.score,
            },
            "opportunity": {
                "score":        opp.score,
                "direction":    opp.direction,   # "LONG" | "SHORT" | "NEUTRAL"
                "is_actionable": opp.is_actionable,
            },
            "regime": {
                "label":      opp.regime.label,
                "color_key":  opp.regime.color_key,
                "confidence": opp.regime.confidence,
            },
            "trend": {
                "direction": trend.direction,
                "score":     trend.score,
                "label":     trend.label,
            },
            "risk": {
                "level":          risk.level,
                "daily_pnl_usd":  risk.daily_pnl_usd,
                "daily_pnl_pct":  risk.daily_pnl_pct,
                "margin_pct":     risk.margin_pct,
                "unrealized_usd": risk.unrealized_usd,
                "message":        risk.message,
            },
            "balance": {
                "equity":     acct_state.balance.total_equity,
                "available":  acct_state.balance.available_balance,
                "margin_pct": acct_state.balance.margin_pct,
            },
            "positions": [
                {
                    "symbol":         p.symbol.replace("USDT", ""),
                    "side":           p.side_label,
                    "size":           p.size,
                    "entry_price":    p.entry_price,
                    "mark_price":     p.mark_price,
                    "unrealized_pnl": p.unrealized_pnl,
                    "pnl_pct":        p.pnl_pct,
                    "leverage":       p.leverage,
                    "liq_price":      p.liquidation_price,
                    "stop_loss":      p.stop_loss,
                    "take_profit":    p.take_profit,
                }
                for p in acct_state.open_positions()
            ],
        }

        # Escritura atómica — evita que la extensión lea JSON incompleto
        _TMP_PATH.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        _TMP_PATH.rename(_STATUS_PATH)
