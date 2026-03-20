"""
core/audit_agent.py
───────────────────
Agente de Auditoría TSAA. Analiza sesiones pasadas y sugiere optimizaciones.
"""
from __future__ import annotations

import json
import logging
import os
from typing import List, Optional

import aiohttp
from core.config import settings
from core.db import get_session_trades

log = logging.getLogger("qts.audit")

_AUDIT_PROMPT = """\
Eres un Senior Quant Auditor especializado en optimización de algoritmos de trading.
Tu objetivo es analizar una Sesión de Trading completada y encontrar patrones de éxito o fallo.

DATOS DE LA SESIÓN:
{session_data}

LISTADO DE TRADES:
{trades_list}

INSTRUCCIONES DE ANÁLISIS:
1. Segmentación por Estrategia: ¿Qué 'strategy_tag' funcionó mejor?
2. Análisis de 'opp_score': ¿Los trades con mayor score fueron realmente mejores?
3. Siderurgia de Activos: ¿Hay símbolos que son "sumideros de fees" (muchos trades, poco PnL)?
4. Optimización de Salidas: Basado en 'close_reason', ¿hay patrones de salidas prematuras?

REGLA DE ORO: No seas genérico. Indica qué variable específica ajustar (ej. "Aumentar ai_min_score a 75" o "Reducir riesgo en SOLUSDT").

FORMATO DE RESPUESTA:
Markdown limpio con secciones:
## 📊 Resumen Ejecutivo
## 🔍 Hallazgos Clave
## 🛠️ Mejoras Sugeridas (Accionables)
"""

class AuditAgent:
    """
    Agente que realiza minería de datos sobre una sesión y consulta a la IA 
    para obtener sugerencias de mejora.
    """

    def __init__(self) -> None:
        self.api_key = settings.openai_api_key
        self.model   = settings.openai_model
        self.url     = "https://api.openai.com/v1/chat/completions"

    async def audit_session(self, session_id: str, session_summary: dict) -> str:
        """
        Ejecuta el proceso de auditoría para una sesión específica.
        """
        trades = get_session_trades(session_id)
        if not trades:
            return "No hay trades suficientes en esta sesión para realizar una auditoría."

        trades_str = json.dumps(trades, indent=2)
        session_str = json.dumps(session_summary, indent=2)

        prompt = _AUDIT_PROMPT.format(
            session_data=session_str,
            trades_list=trades_str
        )

        log.info("[TSAA] Iniciando auditoría IA para sesión %s (%d trades)", session_id, len(trades))

        try:
            async with aiohttp.ClientSession() as session:
                payload = {
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": "Eres un auditor quant experto."},
                        {"role": "user", "content": prompt}
                    ],
                    "temperature": 0.4
                }
                headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
                
                async with session.post(self.url, json=payload, headers=headers) as resp:
                    if resp.status != 200:
                        err = await resp.text()
                        return f"Error en la API de Auditoría: {err}"
                    
                    data = await resp.json()
                    report = data["choices"][0]["message"]["content"]
                    
                    # Guardar reporte en disco
                    os.makedirs("storage/audits", exist_ok=True)
                    filepath = f"storage/audits/audit_{session_id}.md"
                    with open(filepath, "w") as f:
                        f.write(report)
                    log.info("[TSAA] Reporte de auditoría guardado en %s", filepath)
                    
                    return report, filepath

        except Exception as e:
            log.error("AuditAgent falló: %s", e)
            return f"Excepción durante la auditoría: {str(e)}", ""

audit_agent = AuditAgent()
