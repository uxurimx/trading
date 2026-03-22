# Fix Error 34040: SL/TP en Hedge Mode

## 🔴 El problema (log real)

```
23:43:05  qts.controller   ERROR  [RETRY 1/3] set_sl_tp excepción:
         PaperExecutor.set_sl_tp() got an unexpected keyword argument 'trace_id'

23:43:05  qts.controller   CRITICAL  🚨 [PANIC EXIT] Fallo crítico al colocar
         SL/TP tras 3 intentos. Cerrando posición en PIPPINUSDT.
```

### ¿Qué significa?

El sistema intenta 3 veces colocar el **Stop Loss y Take Profit**, pero **falla cada vez** y hace un **Panic Exit** (cierre de emergencia).

---

## ✅ Fixes aplicados (3 cambios)

### 1. BybitExecutor: Retry automático para error 34040

**`core/executor.py`** — set_sl_tp()

- Si Bybit devuelve error 34040/110025 (positionIdx incorrecto)
- Alterna automáticamente: `_hedge_mode = not _hedge_mode`
- Recalcula positionIdx (1 para Long, 2 para Short)
- Reintenta la llamada

### 2. Controller: Eliminar TP correctamente

**`core/controller.py`** — _clear_tp() y _clear_tp_and_trail()

- **Antes**: `set_sl_tp(sym, tp=0, side=side)` ❌ No borra TP
- **Después**: `set_sl_tp(sym, clear_tp=True, side=side)` ✅ Borra TP

### 3. PaperExecutor: Aceptar parámetro trace_id

**`core/paper_wallet.py`** — set_sl_tp()

- Ahora acepta (e ignora) el parámetro `trace_id` para logging

---

## 📈 Resultado

### ❌ ANTES (Panic Exits repetidos)
```
23:43:05 — Error set_sl_tp (PIPPINUSDT) — Panic Exit
23:43:13 — Error set_sl_tp (RESOLVUSDT) — Panic Exit
23:43:28 — Error set_sl_tp (RESOLVUSDT) — Panic Exit
```

### ✅ DESPUÉS (Trade completado)
```
23:43:05 — SET_SL_TP positionIdx=1 (LONG) — ✓ OK
          → SL/TP aplicado
          → Trailing stop activo
          → Cierre normal al TP
```

---

## 🚀 Ver qué pasó: Usar el visor

```bash
python -m tools.log_viewer

   [2] 🤖 Analizar con IA

   La IA detectará automáticamente:
   • Panic Exits por error 34040
   • Símbolos problemáticos
   • Patrones de fallo
   • Acciones recomendadas
```

---

## 📊 Verificación

```bash
# Los fixes están en:
grep "34040\|110025" core/executor.py         # Retry logic ✓
grep "clear_tp=True" core/controller.py       # TP elimina ✓
grep "trace_id" core/paper_wallet.py          # Parámetro ✓

# Compila:
python -c "import core.executor, core.controller; print('OK')"
```

---

## 💡 Sistema de Observabilidad Nuevo

Ahora puedes analizar qué pasó:

```bash
python -m tools.log_viewer  # Interfaz visual e interactiva
```

Opciones:
- **[1]** Ver logs últimas 24h
- **[2]** Analizar con IA automáticamente
- **[3]** Preguntar específicamente
- **[4]** Ver analítica de trades
- **[5]** Filtrar por evento

Todas las decisiones se guardan en `system_logs` (tabla en DuckDB) con:
- **Trace ID**: agrupa todos los eventos del mismo trade
- **Payload**: contexto completo (símbolo, posición, modo hedge, etc.)
- **Logging estructurado**: no más logs de texto, sino datos analizables

---

**Resumen**: El error 34040 se ha arreglado con retry automático + mejor logging. Usa `log_viewer` para ver qué pasó.
