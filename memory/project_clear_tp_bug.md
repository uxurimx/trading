---
name: clear_tp bug en controller
description: _clear_tp y _clear_tp_and_trail pasaban tp=0 en vez de clear_tp=True
type: project
---

En controller.py, _clear_tp() y _clear_tp_and_trail() llamaban set_sl_tp(tp=0) que NO borra el TP (la función solo agrega takeProfit al body si tp>0). El fix correcto es clear_tp=True.

**Why:** Bug silencioso: el trailing stop no eliminaba el TP de Bybit, causando cierres prematuros.
**How to apply:** Siempre usar clear_tp=True para eliminar TP activo. tp=0 significa "no modificar TP".
