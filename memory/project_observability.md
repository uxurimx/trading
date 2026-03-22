---
name: Sistema de observabilidad de QTS
description: Log analítico estructurado en DB con agente analista IA
type: project
---

El sistema de observabilidad está implementado:
- `core/logger.py`: StructuredLogger con trace IDs, cola asíncrona → DuckDB. Instancias: strategy_logger, executor_logger, controller_logger, risk_logger, system_logger.
- `core/db.py`: tabla system_logs con índices (trace_id, event, level, ts, component). Secuencia separada logs_id_seq. Funciones: get_logs_for_analyst(), get_trade_analytics().
- `core/log_analyst.py`: LogAnalystAgent que usa el mismo proveedor LLM configurado. Métodos: analyze(hours) → AnalysisReport, ask(question, hours) → str.

**Why:** Evolución de logs de texto plano hacia "mina de datos" para que una IA analice patrones de trades SL, latencia IA→fill, símbolos problemáticos.
**How to apply:** `await log_analyst.analyze(24)` da un reporte con findings. `await log_analyst.ask("pregunta")` responde preguntas específicas.
