---
name: project_phase1_scope
description: Phase 1 scope — Market Intelligence additions confirmed by user
type: project
---

Phase 1 añade inteligencia de mercado real sobre el dashboard base de Phase 0.

**User confirmó:** usa más futuros (linear) pero quiere también spot para calcular basis.

**Entregables Phase 1:**
- Spot stream (precio spot para basis futures-spot)
- Liquidaciones en tiempo real (Bybit `liquidation.{symbol}`)
- CVD por vela (sparkline de últimas 12 velas de 1 min)
- Basis futures-spot (precio + %)
- OI velocity (tasa de cambio del Open Interest)
- Funding countdown (tiempo al próximo funding)
- Tape expandido: trades + liquidaciones separadas

**Why:** El user opera por absorción — estos datos son los precursores directos de esa señal.
**How to apply:** Phase 2 (detector de absorción) usará estos datos como inputs.
