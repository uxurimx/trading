---
name: Bybit Hedge Mode positionIdx fix
description: Error 34040 en set_sl_tp por positionIdx incorrecto en Hedge Mode
type: project
---

set_sl_tp en executor.py devuelve error 34040 ("not modified") cuando la cuenta está en Hedge Mode y se envía positionIdx=0. El fix agrega retry automático con flip de `_hedge_mode` (igual que place_market_bracket con error 110025).

**Why:** Log real mostró 3 reintentos fallidos → Panic Exit en PIPPINUSDT SHORT por no poder aplicar SL/TP.
**How to apply:** Si aparece error 34040 u 110025 en set_sl_tp, el executor ahora lo maneja solo. Si persiste, verificar manualmente que `detect_position_mode()` se llama en startup.
