"""
core/notifier.py
─────────────────
Notificaciones de escritorio GNOME para eventos del sistema de trading.
Usa notify-send (disponible en cualquier sistema GNOME/Linux).
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import time

log = logging.getLogger("qts.notifier")

_HAS_NOTIFY = shutil.which("notify-send") is not None
_DEDUP: dict[str, float] = {}     # title → last sent timestamp
_DEDUP_SECS = 3.0                  # mínimo entre notificaciones con el mismo título


def notify(title: str, body: str = "", urgency: str = "normal") -> None:
    """Envía notificación de escritorio. No bloquea. No falla si notify-send no existe."""
    if not _HAS_NOTIFY:
        return
    now = time.monotonic()
    if now - _DEDUP.get(title, 0) < _DEDUP_SECS:
        return
    _DEDUP[title] = now
    try:
        subprocess.Popen(
            [
                "notify-send",
                "--app-name=QTS Trading",
                f"--urgency={urgency}",
                "--expire-time=8000",
                title,
                body,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        log.debug("notify-send falló: %s", e)


# ── Helpers semánticos ────────────────────────────────────────────────────────

def trade_opened(symbol: str, side: str, entry: float, sl: float, tp: float, goal: float) -> None:
    arrow = "▲ LONG" if side == "Buy" else "▼ SHORT"
    sym   = symbol.replace("USDT", "")
    notify(
        f"⚡ {arrow} {sym} abierto",
        f"Entry {entry:.5g}  SL {sl:.5g}  TP {tp:.5g}  Meta +${goal:.2f}",
    )

def trade_closed(symbol: str, pnl: float, reason: str) -> None:
    sym   = symbol.replace("USDT", "")
    sign  = "+" if pnl >= 0 else ""
    emoji = "✅" if pnl >= 0 else "❌"
    notify(
        f"{emoji} {sym} cerrado  {sign}${pnl:.2f}",
        f"Razón: {reason}",
        urgency="normal" if pnl >= 0 else "critical",
    )

def breakeven_activated(symbol: str, sl: float) -> None:
    sym = symbol.replace("USDT", "")
    notify(f"🛡 {sym} — Breakeven", f"SL movido a entrada {sl:.5g}. No puedes perder.", urgency="low")

def trailing_activated(symbol: str, sl: float) -> None:
    sym = symbol.replace("USDT", "")
    notify(f"📈 {sym} — Trailing activo", f"SL siguiendo el precio → {sl:.5g}", urgency="low")

def proposal_ready(symbol: str, side: str, score: int, goal: float) -> None:
    arrow = "▲ LONG" if side == "Buy" else "▼ SHORT"
    sym   = symbol.replace("USDT", "")
    notify(f"💡 Propuesta: {arrow} {sym}", f"Score {score}/100  Meta +${goal:.2f}  (Confirma en la app)", urgency="low")

def order_failed(symbol: str, error: str) -> None:
    sym = symbol.replace("USDT", "")
    notify(f"⚠ Orden fallida: {sym}", error[:120], urgency="critical")
