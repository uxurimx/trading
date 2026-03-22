# Sistema de Observabilidad de QTS

## 📊 ¿Qué es?

El sistema de observabilidad captura **cada evento relevante del trading** de forma estructurada en la base de datos:
- Análisis de la IA
- Ejecución de órdenes
- Errors y reintentos
- Ciclo de vida del trade

En lugar de logs de texto plano en consola, ahora **toda la información se guarda en `system_logs`** para que una **IA analista** pueda descubrir patrones.

---

## 🔧 Cómo usarlo (Para el usuario)

### 1️⃣ Ver logs en tiempo real (Interfaz visual)

```bash
python -m tools.log_viewer
```

Esto abre un **menú interactivo** donde puedes:
- Ver logs recientes (últimas 24h)
- Ejecutar análisis automático con IA
- Hacer preguntas libres al analista
- Ver analítica de trades
- Filtrar eventos por tipo

### 2️⃣ Ejecutar análisis automático

Dentro del visor, presiona **[2]** para iniciar análisis. La IA revisará los últimos logs y te mostrará:

```
╔════════════════════════════════════════════════════════════════╗
║  🤖 ANÁLISIS DE OBSERVABILIDAD (24h)                          ║
╚════════════════════════════════════════════════════════════════╝

Resumen ejecutivo:
  Se detectaron 2 Panic Exits por error 34040 en set_sl_tp.
  El parámetro positionIdx no se calculaba correctamente.
  Latencia IA→ejecución: 2.9s (normal).

┌─ HALLAZGOS ───────────────────────────────────────────────────┐
│
│ 🔴 [ERRORS] Panic Exit por SL/TP fallido
│    2 eventos en 2 horas (RESOLVUSDT).
│    Error: positionIdx incompatible con modo Hedge/One-way.
│    ▶ SOLUCIÓN: Verificar que detect_position_mode() se ejecuta
│              en startup. El fix ya está en executor.py (retry).
│
│ 🔴 [ERRORS] clear_tp no elimina TP de Bybit
│    Trailing stop no activaba porque tp=0 no borra TP.
│    ▶ SOLUCIÓN: Cambiar tp=0 → clear_tp=True (YA HECHO).
│
│ 🟡 [PERFORMANCE] Latencia post-fill
│    Promedio 3.2s para set_sl_tp (esperado 1-2s).
│    Probablemente por reintentos en detección hedge mode.
│
└───────────────────────────────────────────────────────────────┘
```

### 3️⃣ Hacer preguntas libres

Presiona **[3]** para formulaciones personalizadas:

```
Tu pregunta: ¿Cuáles son los símbolos más problemáticos?

Respuesta:
RESOLVUSDT y PIPPINUSDT generaron 5 Panic Exits combinados.
Ambas con error 34040 (positionIdx). El patrón sugiere que
la detección de modo hedge no es robustaen trades muy rápidos.
```

---

## 🐛 Error 34040: Solución aplicada

### ¿Qué era el problema?

El log mostraba:
```
23:43:05  qts.controller   ERROR  [RETRY 1/3] set_sl_tp excepción:
         PaperExecutor.set_sl_tp() got an unexpected keyword argument 'trace_id'
```

**Causa**: `PaperExecutor.set_sl_tp()` no tenía el parámetro `trace_id` que acababa de añadir a `BybitExecutor`.

### ✅ Soluciones aplicadas

| Problema | Fix | Dónde |
|----------|-----|-------|
| **Error 34040** en Bybit | Retry automático con flip `hedge_mode` | `core/executor.py:set_sl_tp()` |
| **TP no se borra** | Cambiar `tp=0` → `clear_tp=True` | `core/controller.py:_clear_tp()` |
| **PaperExecutor incompatible** | Añadir parámetro `trace_id` | `core/paper_wallet.py:set_sl_tp()` |
| **Logs no registrados** | Parámetro `trace_id` en executor_logger | `core/executor.py` |

### 📋 Checklist de aplicación

```
✅ core/executor.py       — set_sl_tp() con retry para 34040/110025
✅ core/controller.py     — _clear_tp() y _clear_tp_and_trail() usan clear_tp=True
✅ core/paper_wallet.py   — set_sl_tp() acepta trace_id
✅ core/logger.py         — controller_logger + from_trade()
✅ core/db.py             — logs_id_seq, índices, get_logs_for_analyst()
✅ core/log_analyst.py    — LogAnalystAgent para análisis automático
✅ tools/log_viewer.py    — Interfaz visual e interactiva
```

---

## 🏗️ Arquitectura interna

### Flujo de datos

```
Evento en EXECUTOR
    ↓
executor_logger.info/error()
    ↓
StructuredLogger.log()
    ↓
enqueue_system_log()  (queue asíncrona)
    ↓
_log_worker (thread daemon)
    ↓
DuckDB: system_logs (trace_id, ts, level, component, event, message, payload)
    ↓
get_logs_for_analyst() → comprimido por tipo de evento
    ↓
LogAnalystAgent.analyze() → LLM
    ↓
Hallazgos JSON → report.to_text()
```

### Trace ID: correlación completa del trade

Cada trade tiene un **trace_id único de 8 caracteres**:

```
[a1b2c3d4]  IA Strategy: ANALYSIS_START  → "BTCUSDT score=82"
[a1b2c3d4]  IA Strategy: RAW_RESPONSE     → modelo responde
[a1b2c3d4]  IA Strategy: PROPOSAL_READY   → OrderRequest listo
[a1b2c3d4]  EXECUTOR:    ORDER_SENT       → enviada a Bybit
[a1b2c3d4]  EXECUTOR:    ORDER_SUCCESS    → fill confirmado
[a1b2c3d4]  EXECUTOR:    SET_SL_TP_SEND   → intento 1 de SL/TP
[a1b2c3d4]  EXECUTOR:    SET_SL_TP_OK     → SL/TP aplicado
[a1b2c3d4]  CONTROLLER:  TRADE_FINALIZED  → cierre con PnL=$+45.50
```

Con el **trace_id**, la IA "lee la historia completa" de una operación de principio a fin.

---

## 🤖 Agente Analista: Capacidades

El `LogAnalystAgent` puede responder:

### 1. Preguntas automáticas
```python
await log_analyst.analyze(hours=24)
```
Devuelve: Hallazgos de problemas críticos, patrones negativos, oportunidades.

### 2. Preguntas libres
```python
await log_analyst.ask("¿Por qué fallan las órdenes en PIPPINUSDT?", hours=48)
```
Devuelve: Análisis específico del símbolo con contexto.

### 3. Datos agregados
```python
from core.db import get_trade_analytics
analytics = get_trade_analytics(hours=24)
# → close_reasons, error_symbols, avg_latency, sl_trades
```

---

## 📈 Evolución: De logs de texto → mina de datos

| Antes (❌) | Ahora (✅) |
|-----------|----------|
| `log.info("Order sent")` | `executor_logger.info("ORDER_SENT", "...", {"symbol": "BTC", "qty": 1.5, ...})` |
| Logs en consola solo | Logs en DB + correlación con trace_id |
| Análisis manual | IA analista automática |
| "¿Qué pasó?" no responde | "¿Patrones en SL?" → IA responde con datos |

---

## 🔍 Ejemplos de uso

### Ejemplo 1: Detectar error recurrente

```bash
$ python -m tools.log_viewer

   [2] 🤖 Analizar con IA

   ⏳ Consultando IA...

   🔴 [ERRORS] Error 34040 en set_sl_tp
      Ocurrió 3 veces en 1 hora
      Símbolo: PIPPINUSDT, RESOLVUSDT
      ▶ CAUSA: positionIdx=0 enviado a cuenta Hedge Mode
      ▶ SOLUCIÓN: Reintentar con flip (YA IMPLEMENTADO)
```

### Ejemplo 2: Pregunta sobre latencia

```bash
   [3] 💬 Pregunta libre

   Tu pregunta: ¿Cuánto tiempo tarda de propuesta IA a ejecución?

   ⏳ Consultando IA...

   Latencia promedio: 3.1s
   - Análisis IA: 2.9s
   - Envío orden: 0.2s
   - Total dentro de lo normal para gpt-4o-mini
```

### Ejemplo 3: Analítica de trades

```bash
   [4] 📊 Analítica de trades

   Razones de cierre:
     • sl_hit              15x  Avg: -$12.50
     • tp_reached           8x  Avg: +$34.20
     • weak_exit           12x  Avg: -$5.30
     • breakeven_exit       3x  Avg: +$0.50

   Símbolos con errores:
     • PIPPINUSDT          5 errores (34040)
     • RESOLVUSDT          3 errores (34040)
```

---

## 📝 Para desarrolladores

### Añadir un nuevo log estructurado

```python
from core.logger import executor_logger, controller_logger

# En executor.py:
with executor_logger.context(trace_id) as tid:
    executor_logger.info("CUSTOM_EVENT", "Descripción", {
        "symbol": symbol,
        "value": my_value,
        "status": "processing",
    })

# En controller.py:
with controller_logger.from_trade(trade):
    controller_logger.warning("WEAK_SIGNAL", "Setup débil detectado", {
        "weakness_score": 5,
        "reason": "RSI divergence",
    })
```

### Consultar logs para análisis manual

```python
from core.db import get_logs_for_analyst, get_trade_analytics

# Logs de error de las últimas 48h
logs = get_logs_for_analyst(hours=48, levels=("ERROR", "CRITICAL"))

# Analítica agregada
analytics = get_trade_analytics(hours=48)
for reason in analytics["close_reasons"]:
    print(f"{reason['reason']}: {reason['count']}x")
```

---

## ⚡ Performance

- **Queue asíncrona**: Los logs **NO bloquean** la ejecución del trading
- **Worker daemon**: Thread separado procesa la cola
- **Índices en DB**: Consultas rápidas (trace_id, event, level, ts, component)
- **Compresión de eventos**: El analista agrupa por tipo para economizar tokens

---

## 🚀 Próximos pasos

1. **Ejecuta el visor**: `python -m tools.log_viewer`
2. **Analiza últimas 24h**: Presiona `[2]`
3. **Revisa hallazgos**: El agente mostrará qué arreglar
4. **Mejora continua**: Cada análisis alimenta mejores decisiones

---

## 📞 Soporte

- **Logs estructurados**: `core/logger.py`
- **Agente analista**: `core/log_analyst.py`
- **Visor interactivo**: `tools/log_viewer.py`
- **Esquema DB**: `core/db.py` (tabla `system_logs`)
