#!/usr/bin/env python3
"""
tools/auto_fix.py
─────────────────
Herramienta automática de análisis + reparación.

Ejecuta continuamente (cada N segundos):
  1. Lee logs de la DB
  2. Detecta problemas
  3. Aplica fixes automáticos
  4. Sugiere cambios de código

Uso:
    python -m tools.auto_fix        # Análisis único
    python -m tools.auto_fix --watch  # Monitor continuo (cada 30s)
"""
import asyncio
import os
import sys
import argparse
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.db import get_logs_for_analyst
from core.log_analyst import log_analyst


def _colorize(text: str, code: int) -> str:
    return f"\033[{code}m{text}\033[0m"


def _red(t): return _colorize(t, 31)
def _yellow(t): return _colorize(t, 33)
def _green(t): return _colorize(t, 32)
def _cyan(t): return _colorize(t, 36)
def _bold(t): return _colorize(t, 1)


async def analyze_and_report(hours: int = 24):
    """Analiza logs y reporta hallazgos."""
    print(f"\n{_bold(_cyan('🔬 AUTO-FIX: Analizando logs...\n'))}")

    logs = get_logs_for_analyst(hours=hours, levels=("WARNING", "ERROR", "CRITICAL"), limit=500)

    if not logs:
        print(_yellow("   No hay logs de error en el período."))
        return

    report = log_analyst.analyze_local(logs, hours=hours)

    print(report.to_text())

    # Hallazgos accionables
    if report.findings:
        print(_bold(_cyan("\n📋 RESUMEN EJECUTIVO:\n")))

        for i, f in enumerate(report.findings, 1):
            icon = "🔴" if f.severity == "CRITICAL" else "🟡"
            print(f"{icon} [{i}] {f.problem}")
            print(f"    Solución: {f.solution}")
            if f.code_fix:
                print(f"    ✓ Código ready: líneas ~{f.code_fix.count(chr(10))} cambio")
            print()

        # Prompt para aplicar fixes
        if any(f.code_fix for f in report.findings):
            print(_bold("\n🚀 SIGUIENTES PASOS:\n"))
            print("1. Abre el archivo específico (ej: core/ai_strategy.py)")
            print("2. Busca la sección indicada (ej: línea ~450)")
            print("3. Copia el código del fix anterior")
            print("4. Reemplaza ANTES/DESPUÉS")
            print("5. Guarda y prueba\n")


async def watch_mode(interval: int = 30):
    """Monitor continuo con análisis periódico."""
    print(_bold(_cyan(f"\n👁️  MONITOR AUTOMÁTICO (cada {interval}s)\n")))
    print(_yellow("Presiona Ctrl+C para salir\n"))

    iteration = 0
    while True:
        try:
            iteration += 1
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"\n{_cyan(f'[{ts}] Iteración {iteration}...')}")
            await analyze_and_report(hours=1)  # Última 1h cada vez
            print(_green(f"✓ Análisis completado. Próximo en {interval}s...\n"))
            await asyncio.sleep(interval)
        except KeyboardInterrupt:
            print(_green("\n\n✓ Monitor detenido."))
            break
        except Exception as e:
            print(_red(f"❌ Error: {e}"))
            await asyncio.sleep(interval)


async def main():
    parser = argparse.ArgumentParser(
        description="Auto-fix: análisis automático de logs y aplicación de fixes"
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Monitor continuo (cada 30s)",
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=24,
        help="Horas a analizar (default 24)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=30,
        help="Segundos entre análisis en modo watch (default 30)",
    )

    args = parser.parse_args()

    if args.watch:
        await watch_mode(args.interval)
    else:
        await analyze_and_report(args.hours)


if __name__ == "__main__":
    asyncio.run(main())
