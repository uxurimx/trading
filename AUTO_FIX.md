# 🤖 Auto-Fix: Análisis y Reparación Automática

## El problema que resolvimos

Antes (el log_analyst genérico):
```
User: Hay 3 Panic Exits...
IA: "Hay un problema en el sistema, se recomienda revisar los logs"
User: 🤦 Eso no me ayuda
```

Ahora (auto-fix específico):
```
User: python -m tools.auto_fix

🔬 Analizando logs...
🟡 IA genera símbolo con typo: 'UAISDT' (debería ser 'UAIUSDT')
   Causa: Modelo LLM a veces inventa caracteres
   Solución: Auto-normalizar: SDT → USDT

   💾 FIX (copiar/pegar):
       if symbol.endswith("SDT"):
           symbol = symbol[:-3] + "USDT"
```

---

## 🚀 Cómo usarlo

### Opción 1: Análisis único

```bash
source .venv/bin/activate
python -m tools.auto_fix
```

Lee los últimos logs de ERROR/WARNING, detecta problemas específicos, sugiere fixes.

**Output**:
```
🔴 [1] Desincronización paper/real: abre pero luego marca como cerrada
    Solución: En paper mode, confiar en estado local, NO validar en Bybit
    ✓ Código ready: líneas ~15 cambio

🟡 [2] Trade atrapada en LOOP: IA intenta entrar 3+ veces
    Solución: Excluir símbolos con posición activa de propuestas
    ✓ Código ready: líneas ~20 cambio
```

### Opción 2: Monitor continuo

```bash
python -m tools.auto_fix --watch --interval 30
```

**Cada 30 segundos**:
1. Lee logs nuevos
2. Detecta problemas
3. Sugiere fixes
4. Mantente actualizado mientras tradeas

---

## 🎯 Problemas que detecta

### 1. Typos en símbolos (UAISDT → UAIUSDT)

**Detección**:
```
ERROR: símbolo 'UAISDT' no monitoreado
```

**Fix automático**:
```python
if symbol.endswith("SDT"):
    symbol = symbol[:-3] + "USDT"
```

### 2. Desincronización paper/real

**Detección**:
```
[PAPER] Opened UAIUSDT...
[CONTROLLER] Trade UAIUSDT no detectado en Bybit tras 5s
```

**Fix automático**:
```python
from core.config import settings
if settings.paper_trading:
    return  # No validar en Bybit, confía en estado local
```

### 3. Trade atrapada en loop

**Detección**:
```
AI propuesta: SHORT UAIUSDT (intento 1)
Auto-ejecución bloqueada: UAIUSDT — ya hay posición
AI propuesta: SHORT UAIUSDT (intento 2)
Auto-ejecución bloqueada: UAIUSDT — ya hay posición
AI propuesta: SHORT UAIUSDT (intento 3)
```

**Fix automático**:
```python
active_symbols = [t.symbol for t in active_trades if t.is_active]
candidates = [s for s in candidates if s not in active_symbols]
```

### 4. Errores de positionIdx (34040/110025)

**Detección**:
```
ERROR: set_sl_tp failed: PIPPINUSDT (code 34040)
```

**Status**: ✅ Ya implementado. Auto-retry con flip `_hedge_mode`.

### 5. Latencias anómalas

**Detección**:
```
Latencia IA→fill: 8.5s (esperado <3s)
```

**Sugerencia**:
```
AI_MAX_LATENCY_S=10  # Aumentar timeout
```

---

## 🔧 Cómo funciona internamente

### Arquitectura

```
├─ LogAnalystAgent
│  ├─ SymbolTypoDetector
│  ├─ PaperRealMismatchDetector
│  ├─ TradeLoopDetector
│  ├─ PositionIdxDetector
│  └─ LatencyDetector
│
└─ auto_fix.py (CLI)
   ├─ Lee logs de DB
   ├─ Ejecuta detectores
   ├─ Genera report
   └─ Sugiere fixes
```

### Flujo

```
1. get_logs_for_analyst(hours=24, levels=ERROR/WARNING/CRITICAL)
   ↓ (últimos 500 eventos)
2. report = log_analyst.analyze_local(logs)
   ↓ (ejecuta 5 detectores específicos)
3. Detecta problemas reales
   ↓ (NO genérico, sino ESPECÍFICO)
4. Sugiere fix con código listo para copiar/pegar
   ↓ (No "se recomienda", sino "reemplaza línea X con Y")
```

---

## 📊 Ejemplo real: El log que compartiste

### Input

```
23:53:20  qts.ai_strategy  ERROR  símbolo 'UAISDT' no monitoreado
23:53:30  qts.paper         INFO  [PAPER] Opened Sell UAIUSDT...
23:53:46  qts.controller    INFO  Trade UAIUSDT no detectado en Bybit
23:53:51  qts.controller    INFO  Auto-ejecución bloqueada: UAIUSDT
23:53:51  qts.controller    INFO  Auto-ejecución bloqueada: UAIUSDT
23:54:01  qts.controller    INFO  Auto-ejecución bloqueada: UAIUSDT
```

### Output del auto-fix

```
🔬 AUTO-FIX: Analizando logs...

🟡 [1] IA genera símbolo con typo: 'UAISDT' (debería ser 'UAIUSDT')
    Causa: Modelo LLM a veces inventa caracteres
    Solución: Auto-normalizar: SDT → USDT
    Afectado: UAISDT
    Ocurrencias: 1x

    💾 FIX (copiar/pegar):
        # En core/ai_strategy.py, línea ~450:
        if symbol.endswith("SDT"):
            symbol = symbol[:-3] + "USDT"

🟡 [2] Desincronización paper/real: abre pero luego marca como cerrada
    Causa: En paper_trading, controller intenta validar en Bybit (que no existe)
    Solución: En paper mode, confía en estado local, NO valida en Bybit
    Afectado: UAIUSDT
    Ocurrencias: 1x

    💾 FIX (copiar/pegar):
        # En core/controller.py, línea ~760:
        from core.config import settings
        if settings.paper_trading:
            return  # Paper trading: estado local es fuente de verdad

🟡 [3] Trade atrapada en LOOP: IA intenta entrar 3+ veces
    Causa: No filtra símbolos con posición abierta
    Solución: Excluir de propuestas símbolos con trade activa
    Afectado: UAIUSDT
    Ocurrencias: 3x

    💾 FIX (copiar/pegar):
        # En core/ai_strategy.py, línea ~100:
        active_symbols = [t.symbol for t in active_trades if t.is_active]
        candidates = [s for s in candidates if s not in active_symbols]

📋 RESUMEN EJECUTIVO:
   3 problemas detectados
   3 fixes listos para copiar/pegar
   Tiempo estimado para aplicar: 10 minutos
```

---

## 💡 Mejora continua automática

### Flujo en tiempo real

```
1. Sistema tradea
   ↓
2. Escribe logs en system_logs (DB)
   ↓
3. auto_fix --watch (cada 30s)
   ↓
4. Detecta nuevo problema
   ↓
5. Sugiere fix
   ↓
6. User aplica fix
   ↓
7. Sistema mejora
```

---

## 🎯 Ventajas vs chat genérico

| Aspecto | Chat genérico | Auto-fix específico |
|---------|---------------|-------------------|
| Velocidad | "Revisa los logs" | "Línea X, reemplaza Y con Z" |
| Precisión | Consejo vago | Código listo para copiar |
| Enfoque | General | Trading específico |
| Acción | Manual, exploratoria | Automática, sugerencias concretas |
| Confiabilidad | Puede ser incorrecto | Basado en patrones probados |

---

## 📈 Detectores implementados

```
✅ SymbolTypoDetector       — Detecta UAISDT → UAIUSDT
✅ PaperRealMismatchDetector — Desincronización papel/real
✅ TradeLoopDetector         — Trades atrapadas
✅ PositionIdxDetector       — Errores 34040/110025
✅ LatencyDetector           — Latencias anómalas
```

## 🚀 Próximas adiciones

```
⭕ SwapDetector          — Detecta órdenes invertidas (Buy/Sell)
⭕ MarginDetector        — Falta de margen insuficiente
⭕ WebSocketDownDetector — Reconexiones frecuentes
⭕ APIRateLimitDetector  — Rate limit de Bybit
```

---

## 🔗 Uso integrado

En tu sistema de trading:

```python
# En main.py o controller, al iniciar:
import asyncio
from tools.auto_fix import analyze_and_report

# Al cerrar cada sesión de trading:
await analyze_and_report(hours=4)  # Analizar últimas 4h
# → Mostrará problemas encontrados
# → Sugerirá fixes para la próxima sesión
```

---

## 🎓 Cómo agregar un nuevo detector

1. **Hereda de una clase base o crea nueva**:
```python
class MyCustomDetector:
    @staticmethod
    def analyze(logs: list) -> Optional[Finding]:
        # Buscar patrón específico
        # Retornar Finding con problem, solution, code_fix
        return Finding(...)
```

2. **Registra en LogAnalystAgent**:
```python
self._detectors = [
    SymbolTypoDetector,
    MyCustomDetector,  # ← Nuevo
]
```

3. **Test**:
```bash
python -m tools.auto_fix
```

---

## 📞 Resumen

**ANTES**:
- Logs en consola
- Usuario lee manualmente
- "Hay un problema" (vago)
- No actionable

**AHORA**:
- Logs en DB con trace_id
- auto_fix analiza automáticamente
- "Problema X → Solución Y → Código Z" (específico)
- Completamente actionable

**OBJETIVO**: Sistema que se mejora a sí mismo detectando y sugiriendo reparaciones.
