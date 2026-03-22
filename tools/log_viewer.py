#!/usr/bin/env python3
"""
tools/log_viewer.py
────────────────────
Visor interactivo de logs estructurados + Agente Analista IA.

Uso:
    python -m tools.log_viewer

Opciones:
    [1] Ver logs recientes (últimas 24h)
    [2] Analizar con IA (hallazgos automáticos)
    [3] Pregunta libre al analista
    [4] Filtrar por evento específico
    [5] Salir
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.db import get_logs_for_analyst, get_connection, get_trade_analytics
from core.log_analyst import log_analyst, AnalysisReport, Finding


def _colorize(text: str, code: int) -> str:
    """Coloriza texto con códigos ANSI."""
    return f"\033[{code}m{text}\033[0m"


def _bold(text: str) -> str:
    return _colorize(text, 1)


def _red(text: str) -> str:
    return _colorize(text, 31)


def _yellow(text: str) -> str:
    return _colorize(text, 33)


def _green(text: str) -> str:
    return _colorize(text, 32)


def _cyan(text: str) -> str:
    return _colorize(text, 36)


def _gray(text: str) -> str:
    return _colorize(text, 90)


def print_header(title: str) -> None:
    """Imprime un encabezado visual."""
    print(f"\n{_bold(_cyan('═' * 80))}")
    print(f"{_bold(title)}")
    print(f"{_bold(_cyan('═' * 80))}\n")


def print_log_entry(log: dict, idx: int) -> None:
    """Imprime un log estructurado de forma legible."""
    ts_dt = datetime.fromtimestamp(log["ts"] / 1000)
    ts_str = ts_dt.strftime("%H:%M:%S")

    # Color según level
    if log["level"] == "CRITICAL":
        level_str = _red(f"[{log['level']}]")
    elif log["level"] == "ERROR":
        level_str = _red(f"[{log['level']}]")
    elif log["level"] == "WARNING":
        level_str = _yellow(f"[{log['level']}]")
    else:
        level_str = _green(f"[{log['level']}]")

    trace_str = _gray(f"({log['trace_id']})" if log["trace_id"] else "")

    print(f"{idx:3}. {ts_str} {level_str} {log['component']:12} {log['event']:20} {trace_str}")
    print(f"     {_cyan(log['message'][:70])}")

    # Payload (solo campos interesantes)
    if log["payload"]:
        payload_items = []
        for k in ["symbol", "retCode", "retMsg", "reason", "side", "hedge_mode",
                  "positionIdx", "sl", "tp", "qty", "elapsed_s", "attempt", "rr"]:
            if k in log["payload"]:
                v = log["payload"][k]
                if isinstance(v, float):
                    v = f"{v:.4f}"
                payload_items.append(f"{k}={v}")
        if payload_items:
            print(f"     {_gray(' | '.join(payload_items))}")
    print()


def show_recent_logs(hours: int = 24) -> None:
    """Muestra los logs de las últimas N horas."""
    print_header(f"📋 Últimas {hours}h de logs (WARNING/ERROR/CRITICAL)")

    logs = get_logs_for_analyst(hours=hours, limit=100)

    if not logs:
        print(_yellow("   No hay logs de error en el período."))
        return

    print(f"   Total: {len(logs)} eventos\n")

    # Contar por tipo
    by_event = {}
    for log in logs:
        key = f"{log['component']}.{log['event']}"
        by_event[key] = by_event.get(key, 0) + 1

    print(_cyan("   Eventos más frecuentes:"))
    for event, count in sorted(by_event.items(), key=lambda x: -x[1])[:10]:
        print(f"     • {event}: {count}x")
    print()

    # Mostrar últimos logs
    print(_cyan("   Logs recientes:"))
    for idx, log in enumerate(logs[:30], 1):
        print_log_entry(log, idx)

    if len(logs) > 30:
        print(_gray(f"   ... y {len(logs) - 30} más"))


async def analyze_with_ai(hours: int = 24) -> None:
    """Ejecuta el agente analista y muestra hallazgos."""
    print_header(f"🤖 Analizando sistema (últimas {hours}h)")

    if not log_analyst.is_ready():
        print(_red("❌ Error: Proveedor LLM no configurado."))
        print("   Verifica en .env:")
        print("     OPENAI_API_KEY=... o")
        print("     OLLAMA_HOST=http://localhost:11434, OLLAMA_MODEL=llama3.2")
        return

    print("   ⏳ Consultando IA (esto toma unos segundos)...\n")

    report = await log_analyst.analyze(hours=hours)

    if report.error:
        print(_red(f"❌ Error en análisis: {report.error}"))
        return

    print(report.to_text())

    # Preguntas sugeridas
    if report.findings:
        print(_cyan("\n   💡 Preguntas sugeridas:"))
        print("     • ¿Qué patrones tienen los trades perdidos?")
        print("     • ¿Cuál es la latencia IA → ejecución?")
        print("     • ¿Hay símbolos con errores recurrentes?")


async def ask_analyst() -> None:
    """Pregunta libre al analista."""
    print_header("💬 Pregunta al Analista")

    if not log_analyst.is_ready():
        print(_red("❌ Error: Proveedor LLM no configurado."))
        return

    try:
        hours = int(input(f"   {_cyan('Período (horas) [24]: ')}") or "24")
    except ValueError:
        hours = 24

    question = input(f"   {_cyan('Tu pregunta: ')}")

    if not question.strip():
        print(_yellow("   Pregunta vacía."))
        return

    print(f"\n   ⏳ Consultando IA...\n")
    answer = await log_analyst.ask(question, hours=hours)
    print(f"   {_green('✓ Respuesta:')}\n   {answer}\n")


def filter_by_event() -> None:
    """Filtra logs por evento específico."""
    print_header("🔍 Filtrar por Evento")

    con = get_connection()
    events = con.execute(
        "SELECT DISTINCT event, COUNT(*) as n FROM system_logs "
        "WHERE level IN ('WARNING','ERROR','CRITICAL') "
        "GROUP BY event ORDER BY n DESC LIMIT 20"
    ).fetchall()
    con.close()

    if not events:
        print(_yellow("   No hay eventos de error."))
        return

    print(_cyan("   Eventos más frecuentes:\n"))
    for idx, (event, count) in enumerate(events, 1):
        print(f"   [{idx}] {event:30} ({count}x)")

    try:
        choice = int(input(f"\n   {_cyan('Selecciona [1-20]: ')}"))-1
        if 0 <= choice < len(events):
            event_name = events[choice][0]
            con = get_connection()
            logs = con.execute(
                "SELECT trace_id, ts, level, component, message, payload "
                "FROM system_logs WHERE event = ? "
                "ORDER BY ts DESC LIMIT 20", (event_name,)
            ).fetchall()
            con.close()

            print_header(f"🔍 Evento: {event_name}")
            for idx, log in enumerate(logs, 1):
                log_dict = {
                    "trace_id": log[0],
                    "ts": log[1],
                    "level": log[2],
                    "component": log[3],
                    "message": log[4],
                    "event": event_name,
                    "payload": {},
                }
                print_log_entry(log_dict, idx)
    except (ValueError, IndexError):
        print(_yellow("   Selección inválida."))


def show_trade_analytics() -> None:
    """Muestra analítica rápida de trades."""
    print_header("📊 Analítica de Trades (últimas 48h)")

    analytics = get_trade_analytics(hours=48)

    if not analytics.get("close_reasons"):
        print(_yellow("   No hay datos."))
        return

    print(_cyan("   Razones de cierre:\n"))
    for reason in analytics["close_reasons"][:10]:
        avg_pnl = reason["avg_pnl"]
        pnl_str = _green(f"+${avg_pnl:.2f}") if avg_pnl > 0 else _red(f"${avg_pnl:.2f}")
        print(f"     • {reason['reason']:20} {reason['count']:3}x  Avg: {pnl_str}")

    print(_cyan("\n   Símbolos con errores:\n"))
    for sym_err in analytics.get("error_symbols", [])[:5]:
        print(f"     • {sym_err['symbol']:12} {sym_err['error_count']} errores")

    latency = analytics.get("avg_latency_ai_to_fill_s", 0)
    if latency > 0:
        print(_cyan(f"\n   Latencia IA → Fill: {latency:.2f}s"))


async def main() -> None:
    """Loop principal interactivo."""
    print("\n" + _bold(_cyan("╔═══════════════════════════════════════════════════════════════╗")))
    print(_bold(_cyan("║  QTS LOG VIEWER & ANALYST                                         ║")))
    print(_bold(_cyan("╚═══════════════════════════════════════════════════════════════╝\n")))

    while True:
        print(_cyan("┌─ MENÚ ─────────────────────────────────────────────────────────────┐"))
        print(_cyan("│"))
        print(_cyan("│  [1] 📋 Ver logs recientes (últimas 24h)"))
        print(_cyan("│  [2] 🤖 Analizar con IA (hallazgos automáticos)"))
        print(_cyan("│  [3] 💬 Pregunta libre al analista"))
        print(_cyan("│  [4] 📊 Analítica de trades"))
        print(_cyan("│  [5] 🔍 Filtrar por evento"))
        print(_cyan("│  [0] 🚪 Salir"))
        print(_cyan("│"))
        print(_cyan("└────────────────────────────────────────────────────────────────────┘\n"))

        choice = input(f"{_cyan('Opción: ')}").strip()

        if choice == "1":
            show_recent_logs(24)
        elif choice == "2":
            await analyze_with_ai(24)
        elif choice == "3":
            await ask_analyst()
        elif choice == "4":
            show_trade_analytics()
        elif choice == "5":
            filter_by_event()
        elif choice == "0":
            print(_green("\n✓ Adiós.\n"))
            break
        else:
            print(_yellow("   Opción inválida.\n"))

        input(f"\n{_gray('Presiona ENTER para continuar...')}")


if __name__ == "__main__":
    asyncio.run(main())
