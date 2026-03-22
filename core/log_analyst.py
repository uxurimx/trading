"""
core/log_analyst.py
────────────────────
LogAnalystAgent — Analizador específico de problemas del trading.

NO es un chatbot genérico. Es un detective que:
  1. Identifica patrones específicos en los logs (typos, desincronización, loops)
  2. Correlaciona eventos (propuesta → ejecución → cierre)
  3. Propone fixes CONCRETOS con código listo para copiar/pegar
  4. Sugiere mejoras automatizadas

Problemas que detecta:
  • Typos en símbolos (UAISDT vs UAIUSDT)
  • Desincronización paper/real
  • Trades atrapadas en loop
  • positionIdx mal detectado (error 34040)
  • Latencias anómalas
  • Patrones de cierre forzado (bybit_close)
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import List, Optional

from core.config import settings
from core.db import get_connection, get_logs_for_analyst, get_trade_analytics
from core.logger import system_logger

log = logging.getLogger("qts.log_analyst")


# ─── Hallazgos específicos ─────────────────────────────────────────────────────

@dataclass
class Finding:
    """Un hallazgo actionable con fix específico."""
    severity:    str          # CRITICAL | WARNING | INFO
    problem:     str          # qué está pasando
    root_cause:  str          # por qué
    solution:    str          # qué hacer
    code_fix:    str = ""     # código para pegar
    affected:    List[str]    = field(default_factory=list)  # símbolos/eventos afectados
    frequency:   int          = 0  # cuántas veces ocurrió


@dataclass
class AnalysisReport:
    """Resultado del análisis."""
    generated_at:   int = field(default_factory=lambda: int(time.time()))
    findings:       List[Finding] = field(default_factory=list)
    automated_fixes: List[tuple] = field(default_factory=list)  # (file, old, new)
    summary:        str = ""

    @property
    def has_critical(self) -> bool:
        return any(f.severity == "CRITICAL" for f in self.findings)

    def to_text(self) -> str:
        """Formatea el reporte de forma legible."""
        lines = [
            "\n" + "="*80,
            "🔬 ANÁLISIS AUTOMÁTICO DE LOGS DEL TRADING",
            "="*80 + "\n",
        ]

        if self.summary:
            lines.append(f"📊 RESUMEN:\n{self.summary}\n")

        for finding in self.findings:
            icon = "🔴" if finding.severity == "CRITICAL" else "🟡" if finding.severity == "WARNING" else "🔵"
            lines.append(f"\n{icon} {finding.severity}: {finding.problem}")
            lines.append(f"   Causa: {finding.root_cause}")
            lines.append(f"   Solución: {finding.solution}")
            if finding.affected:
                lines.append(f"   Afectado: {', '.join(finding.affected)}")
            if finding.frequency > 1:
                lines.append(f"   Ocurrencias: {finding.frequency}x")
            if finding.code_fix:
                lines.append(f"\n   💾 FIX (copiar/pegar):\n{self._indent(finding.code_fix, 7)}")

        if self.automated_fixes:
            lines.append("\n" + "-"*80)
            lines.append("🤖 FIXES AUTOMÁTICOS LISTOS PARA APLICAR:\n")
            for i, (file, old, new) in enumerate(self.automated_fixes, 1):
                lines.append(f"{i}. {file}")
                lines.append(f"   OLD: {old[:60]}...")
                lines.append(f"   NEW: {new[:60]}...")

        lines.append("\n" + "="*80)
        return "\n".join(lines)

    @staticmethod
    def _indent(text: str, spaces: int) -> str:
        return "\n".join(" "*spaces + line for line in text.split("\n"))


# ─── Analizadores específicos ──────────────────────────────────────────────────

class SymbolTypoDetector:
    """Detecta typos en símbolos (UAISDT vs UAIUSDT)."""

    @staticmethod
    def analyze(logs: list) -> Optional[Finding]:
        """Busca propuestas con símbolos que no existen."""
        typo_events = [
            log for log in logs
            if log["event"] == "SYMBOL_ERROR" or
               (log["event"] == "PROPOSAL_READY" and "símbolo" in log["message"].lower())
        ]

        typo_map = {}
        for log in logs:
            if "símbolo" in log["message"].lower() and "no monitoreado" in log["message"].lower():
                # Extraer símbolo de "símbolo 'UAISDT' no monitoreado"
                match = re.search(r"'(\w+)'", log["message"])
                if match:
                    typo = match.group(1)
                    typo_map[typo] = typo_map.get(typo, 0) + 1

        if not typo_map:
            return None

        typos = list(typo_map.items())
        worst_typo, count = max(typos, key=lambda x: x[1])

        # Detectar patrón: UAISDT → UAIUSDT
        corrected = worst_typo.replace("SDT", "USDT") if "SDT" in worst_typo else worst_typo

        if corrected != worst_typo:
            fix_code = f"""
# En core/ai_strategy.py, línea ~450:
# ANTES:
if symbol not in symbols:
    log.error("AI Strategy: símbolo '%s' no monitoreado", symbol)
    return None

# DESPUÉS:
if symbol not in symbols:
    # Auto-fix: normalizar SDT → USDT
    if symbol.endswith("SDT"):
        symbol = symbol[:-3] + "USDT"
    if symbol not in symbols:
        log.error("AI Strategy: símbolo '%s' no monitoreado", symbol)
        return None
"""
            return Finding(
                severity="WARNING",
                problem=f"IA genera símbolo con typo: '{worst_typo}' (debería ser '{corrected}')",
                root_cause="Modelo LLM a veces inventa caracteres (UAISDT en lugar de UAIUSDT)",
                solution=f"Auto-normalizar: SDT → USDT",
                code_fix=fix_code,
                affected=[worst_typo],
                frequency=count,
            )
        return None


class PaperRealMismatchDetector:
    """Detecta desincronización entre paper_trading y Bybit real."""

    @staticmethod
    def analyze(logs: list) -> Optional[Finding]:
        """Busca: [PAPER] Opened pero luego 'no detectado en Bybit'."""
        mismatches = []

        for i, log in enumerate(logs):
            # Buscar "[PAPER] Opened X"
            if "[PAPER] Opened" in log["message"]:
                symbol = re.search(r"Opened \w+ (\w+)", log["message"])
                if symbol:
                    sym = symbol.group(1)
                    # Buscar "no detectado en Bybit" para el mismo símbolo después
                    for j in range(i, min(i+20, len(logs))):
                        if sym in logs[j]["message"] and "no detectado en Bybit" in logs[j]["message"]:
                            mismatches.append(sym)
                            break

        if not mismatches:
            return None

        count = len(mismatches)
        fix_code = """
# En core/controller.py, línea ~760:
# PROBLEMA: En paper trading, no debe verificar en Bybit

# ANTES:
if pos_key not in account.positions:
    # Caso especial: Bybit tarda tiempo...

# DESPUÉS:
# En paper trading, no verificar en Bybit (confía en state local)
from core.config import settings
if settings.paper_trading:
    # Paper trading: estado local es fuente de verdad
    return

if pos_key not in account.positions:
    # Real trading: Bybit tarda tiempo...
"""
        return Finding(
            severity="CRITICAL",
            problem="Desincronización paper/real: abre posición pero luego la marca como cerrada",
            root_cause="En paper_trading, el controller intenta validar en Bybit (que no existe)",
            solution="En paper mode, confiar ciegamente en el estado local, NO validar en Bybit",
            code_fix=fix_code,
            affected=mismatches,
            frequency=count,
        )


class TradeLoopDetector:
    """Detecta trades atrapadas en loop (mismo símbolo, múltiples intentos)."""

    @staticmethod
    def analyze(logs: list) -> Optional[Finding]:
        """Busca: propuesta X 3+ veces para el mismo símbolo con posición abierta."""
        loop_symbols = {}

        for log in logs:
            if "AI propuesta lista" in log["message"] or "PROPOSAL_READY" in log["event"]:
                # Extraer símbolo
                match = re.search(r"(\w{6,10}USDT)", log["message"])
                if match:
                    sym = match.group(1)
                    # Verificar si hay "ya hay posición abierta" después
                    loop_symbols[sym] = loop_symbols.get(sym, 0) + 1

        # Filtrar solo los que se repiten 3+ veces
        loops = {sym: count for sym, count in loop_symbols.items() if count >= 3}

        if not loops:
            return None

        worst_sym, count = max(loops.items(), key=lambda x: x[1])

        fix_code = f"""
# En core/controller.py, línea ~550:
# PROBLEMA: IA sugiere entrar en {worst_sym} pero ya hay posición

# AÑADIR LÓGICA DE DEDUPLICACIÓN:
if req.symbol in self._active:
    # Ya hay posición abierta, ignorar propuesta
    log.info("Propuesta ignorada: %s ya abierto", req.symbol)
    return

# O MEJOR: En ai_strategy.py, filtrar símbolos con posición abierta
active_symbols = [t.symbol for t in active_trades if t.is_active]
candidates = [s for s in candidates if s not in active_symbols]
"""
        return Finding(
            severity="WARNING",
            problem=f"Trade atrapada en LOOP: IA intenta entrar en {worst_sym} {count}+ veces",
            root_cause="No filtra símbolos con posición abierta antes de proponer",
            solution="Excluir de propuestas los símbolos con trade activa",
            code_fix=fix_code,
            affected=[worst_sym],
            frequency=count,
        )


class PositionIdxDetector:
    """Detecta errores de positionIdx (34040, 110025)."""

    @staticmethod
    def analyze(logs: list) -> Optional[Finding]:
        """Busca errores 34040/110025 en set_sl_tp."""
        pos_idx_errors = []

        for log in logs:
            if "34040" in str(log.get("payload", {}).get("retCode", "")) or \
               "110025" in str(log.get("payload", {}).get("retCode", "")):
                pos_idx_errors.append(log)

        if not pos_idx_errors:
            return None

        return Finding(
            severity="CRITICAL",
            problem=f"Error {pos_idx_errors[0]['payload'].get('retCode')} en set_sl_tp",
            root_cause="positionIdx incorrecto para modo Hedge/One-way",
            solution="Ya está implementado el auto-retry con flip. Verificar que detect_position_mode() se ejecuta en startup.",
            code_fix="# El fix ya está en executor.py:set_sl_tp() línea ~407\n# Auto-retry con flip _hedge_mode si error 34040/110025",
            affected=list(set(log["payload"].get("symbol") for log in pos_idx_errors if "symbol" in log["payload"])),
            frequency=len(pos_idx_errors),
        )


class LatencyDetector:
    """Detecta latencias anómalas (IA → fill muy lento)."""

    @staticmethod
    def analyze(logs: list) -> Optional[Finding]:
        """Busca traces con latencia IA→fill > 5s."""
        traces_with_latency = {}

        for log in logs:
            trace_id = log.get("trace_id")
            if not trace_id:
                continue

            if "ANALYSIS_START" in log["event"]:
                if trace_id not in traces_with_latency:
                    traces_with_latency[trace_id] = {"start": log["ts"]}
            elif "ORDER_SUCCESS" in log["event"]:
                if trace_id in traces_with_latency:
                    traces_with_latency[trace_id]["end"] = log["ts"]

        slow_traces = {
            tid: (data["end"] - data["start"]) / 1000
            for tid, data in traces_with_latency.items()
            if "end" in data and (data["end"] - data["start"]) / 1000 > 5
        }

        if not slow_traces:
            return None

        avg_latency = sum(slow_traces.values()) / len(slow_traces)

        return Finding(
            severity="WARNING",
            problem=f"Latencia alta: IA→fill toma {avg_latency:.1f}s (esperado <3s)",
            root_cause="Probablemente reintentos de set_sl_tp o conexión lenta a OpenAI",
            solution="Revisar velocidad de red, aumentar timeouts, o reducir ai_max_latency_s",
            code_fix=f"""
# En .env:
AI_MAX_LATENCY_S=10  # Aumentar timeout de {settings.ai_max_latency_s}s a 10s
""",
            frequency=len(slow_traces),
        )


# ─── Agente principal ──────────────────────────────────────────────────────────

class LogAnalystAgent:
    """Detective de trading que analiza logs y sugiere fixes específicos."""

    def __init__(self):
        self._detectors = [
            SymbolTypoDetector,
            PaperRealMismatchDetector,
            TradeLoopDetector,
            PositionIdxDetector,
            LatencyDetector,
        ]

    def analyze_local(self, logs: list, hours: int = 24) -> AnalysisReport:
        """
        Análisis LOCAL sin LLM (mucho más rápido y confiable).
        Ejecuta todos los detectores específicos del trading.
        """
        report = AnalysisReport()

        if not logs:
            report.summary = "No hay logs de error en el período."
            return report

        # Ejecutar detectores
        for detector_class in self._detectors:
            detector = detector_class()
            finding = detector.analyze(logs)
            if finding:
                report.findings.append(finding)

        # Ordenar por severidad
        severity_order = {"CRITICAL": 0, "WARNING": 1, "INFO": 2}
        report.findings.sort(key=lambda f: severity_order.get(f.severity, 99))

        # Generar resumen
        if report.findings:
            critical = sum(1 for f in report.findings if f.severity == "CRITICAL")
            warning = sum(1 for f in report.findings if f.severity == "WARNING")
            report.summary = f"🔴 {critical} críticos | 🟡 {warning} warnings\n"
            report.summary += "Problemas específicos detectados y fixes listos para aplicar."
        else:
            report.summary = "✅ No se detectaron problemas conocidos en los logs."

        return report

    async def analyze_with_llm(self, logs: list, hours: int = 24) -> AnalysisReport:
        """
        Análisis con LLM para casos complejos (correlaciones no obvias).
        SOLO si local no encuentras suficientes hallazgos.
        """
        # Por ahora, solo hacer análisis local (mucho más útil)
        return self.analyze_local(logs, hours)

    async def analyze(self, hours: int = 24) -> AnalysisReport:
        """Main entry point: obtener logs y analizar."""
        logs = get_logs_for_analyst(hours=hours, levels=("WARNING", "ERROR", "CRITICAL"), limit=500)
        return self.analyze_local(logs, hours)


# ── Singleton ──────────────────────────────────────────────────────────────────
log_analyst = LogAnalystAgent()
