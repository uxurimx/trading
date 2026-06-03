QTS — Reporte de Estado y Plan de Crecimiento                                                                          
                  
  ▎ TL;DR: Tu sistema ya es ~80% production-ready. Le faltan capas de contexto (qué es cada par, qué mueve cada sector) y
  ▎  de abstracción visual (mapa de constelaciones, no sólo tabla). Lo "fácil + seguro + exponencial" es contradictorio 
  ▎ si se promete a la vez, pero se aproxima con automatización + risk fortress + composición disciplinada. Abajo el     
  ▎ reporte completo, el menú nuevo que pides, y el plan de ejecución por fases.

  ---
  PARTE 1 — Diagnóstico del sistema actual
                                                                                                                         
  Lo que ya tienes (no necesita rehacerse)
                                                                                                                         
  ┌──────────────────────────────────────────┬─────────────┬─────────────────────────────────────────────────────────┐
  │                   Capa                   │   Estado    │                       Comentario                        │   
  ├──────────────────────────────────────────┼─────────────┼─────────────────────────────────────────────────────────┤   
  │ Ingesta de mercado (streams/)            │ ✅          │ WS público+privado de Bybit, REST fallback,             │
  │                                          │ Production  │ CVD/OI/liquidations en tiempo real.                     │   
  ├──────────────────────────────────────────┼─────────────┼─────────────────────────────────────────────────────────┤   
  │ Motor de señales (core/absorption,        │ ✅          │ Score 0-100 multi-factor, Fibonacci-weighted multi-TF  │
  │ regime, trend, liquidity)                 │ Production  │ (1m→6h).                                               │   
  ├───────────────────────────────────────────┼─────────────┼────────────────────────────────────────────────────────┤
  │ Ejecutor Bybit (core/executor.py, 766L)   │ ✅          │ Auto-corrige hedge/one-way positionIdx, reduce-only,   │   
  │                                           │ Production  │ modify SL/TP.                                          │   
  ├───────────────────────────────────────────┼─────────────┼────────────────────────────────────────────────────────┤
  │ Strategy engine (core/strategy.py, 682L)  │ ✅          │ ATR adaptativo, 4 speed levels                         │   
  │                                           │ Production  │ (nano/scalp/fast/standard).                            │   
  ├───────────────────────────────────────────┼─────────────┼────────────────────────────────────────────────────────┤
  │ AI Strategy Agent (core/ai_strategy.py)   │ ✅          │ OpenAI/Ollama/Compatible, throttle 60s, R:R ≥2.0 neto  │   
  │                                           │ Funcional   │ de fees.                                               │   
  ├───────────────────────────────────────────┼─────────────┼────────────────────────────────────────────────────────┤
  │ Risk Fortress (core/risk.py)              │ ✅          │ Circuit breaker diario -2%, alertas margen 60/80%.     │   
  │                                           │ Production  │                                                        │   
  ├───────────────────────────────────────────┼─────────────┼────────────────────────────────────────────────────────┤
  │ Session Manager / TSAA (core/session.py)  │ ✅          │ Sesiones acotadas: target PnL, drawdown, tiempo, API   │   
  │                                           │ Production  │ cost cap. Único en su categoría.                       │   
  ├───────────────────────────────────────────┼─────────────┼────────────────────────────────────────────────────────┤
  │ Web dashboard (FastAPI + WS)              │ ✅          │ 90+ endpoints; el modo MAIN/mentor en progreso         │   
  │                                           │ Funcional   │ (cambios sin commitear).                               │   
  ├───────────────────────────────────────────┼─────────────┼────────────────────────────────────────────────────────┤
  │ MCP server (mcp_server.py)                │ ✅          │ 8 tools que Claude puede invocar — el verdadero        │   
  │                                           │ Production  │ superpoder.                                            │   
  ├───────────────────────────────────────────┼─────────────┼────────────────────────────────────────────────────────┤
  │ DuckDB (storage/trading.duckdb)           │ ✅          │ 7 tablas, índices, trace_id en logs.                   │   
  │                                           │ Funcional   │                                                        │   
  └───────────────────────────────────────────┴─────────────┴────────────────────────────────────────────────────────┘
                                                                                                                         
  Brechas reales (lo que sí hay que construir)                                                                           
   
  1. No hay "qué es" detrás del símbolo. PEPEUSDT y RNDRUSDT se ven igual en tu tabla. Falta tagging por                 
  sector/narrativa.
  2. No hay menú por estado. Tienes top-N por score, pero no "rompiendo 24h high con CVD positivo" o "durmientes a punto 
  de despertar".                                                                                                         
  3. Liquidaciones se reciben pero no influyen en scoring. Es un edge que estás desperdiciando.
  4. No hay correlaciones cruzadas. No sabes que JTO se mueve cuando SOL hace un 3%.                                     
  5. log_analyst.py está como esqueleto. El agente que aprende de tus propias pérdidas no se ejecuta solo.               
  6. Sin recuperación de estado del controller si se reinicia con posiciones abiertas.                                   
  7. Sin estado PENDING para órdenes (sólo OPEN/CLOSED).                                                                 
  8. Paper wallet sin terminar — no puedes hacer backtest realista sin tocar capital.                                    
                                                                                                                         
  ---                                                                                                                    
  PARTE 2 — Cómo lo hacen los grandes (Bloomberg, Citadel, Jane Street)                                                  
                                                                                                                         
  Una Terminal Bloomberg cuesta ~$30k/año y tú ya tienes el 60% de su valor. Lo que ellos hacen y tú no:
                                                                                                                         
  a) Contexto enciclopédico instantáneo                                                                                  
                                                                                                                         
  Bloomberg te da, para cualquier ticker: descripción, sector, peers, cadena de suministro, contratos relevantes,        
  noticias en tiempo real con sentiment, eventos próximos (earnings, halvings, unlocks).
  → Tu equivalente cripto: CoinGecko/CoinMarketCap API + DefiLlama + Token Unlocks + Messari. Es gratis o casi gratis.   
                                                                                                                         
  b) Cross-asset y cross-sector heatmaps                                                                                 
                                                                                                                         
  Una sola vista: oro vs DXY vs S&P vs crude vs BTC. Ves la rotación en segundos.                                        
  → Tu equivalente: matriz de correlaciones rodando 30d entre todos tus símbolos, coloreada.
                                                                                                                         
  c) News-flow filtrado por relevancia para posiciones                                                                   
                                                                                                                         
  "Tienes long SOL, salió que Visa firma con Solana → +impact alto". Bloomberg lo conecta automáticamente.               
  → Tu equivalente: RSS de Coindesk/The Block + LLM que clasifique impacto por símbolo de tu watchlist.
                                                                                                                         
  d) Order Flow real (DOM completo, niveles de iceberg)                                                                  
                                                                                                                         
  Tú tienes orderbook L1+L2 y CVD. Bloomberg agrega flujos institucionales (TRACE para bonos, NYSE OpenBook). En cripto, 
  Coinglass + Hyblock son el equivalente: liquidation heatmap, long/short ratio, large trades.
  → Tu equivalente: ya rastreas liquidaciones, sólo te falta explotarlas en scoring.                                     
                                                                                                                         
  e) Backtesting y "what-if" en línea
                                                                                                                         
  Bloomberg PORT, BAck. Puedes pintar curvas hipotéticas.                                                                
  → Tu equivalente: paper_wallet.py + DuckDB con histórico → simulación de estrategias sin riesgo.
                                                                                                                         
  f) Mentor de risk en cada confirmación                                                                                 
                                                                                                                         
  Cuando un trader institucional dispara un trade, el sistema le muestra: drawdown si pierde, VaR de la cartera,         
  correlación con lo que ya tiene, recomendación de tamaño Kelly-ajustado.
  → Tu modo MAIN/mentor va exactamente por aquí. Falta el cálculo de Kelly y el VaR del portfolio.                       
                                                                                                                         
  ---                                                                                                                    
  PARTE 3 — El nuevo menú: "Constelaciones de Pares"                                                                     
                                                                                                                         
  Imagínalo como un mapa estelar: cada símbolo es una estrella, su brillo es el momentum, su color es el régimen, y se
  agrupan en constelaciones según su estado.                                                                             
   
  Constelaciones propuestas (filtros)                                                                                    
                  
  ┌──────────────────┬───────────────────────────────────────────────────┬───────────────────────────────────────────┐   
  │   Constelación   │                      Filtro                       │              Por qué importa              │
  ├───────────────────────┼────────────────────────────────────────────────┼─────────────────────────────────────────┤   
  │ 🔥 Breakout           │ precio > 24h high · vol > 1.5× media · CVD     │ Momentum confirmado, alta probabilidad  │
  │                       │ ≥4/5 bull                                      │ continuación                            │
  ├───────────────────────┼────────────────────────────────────────────────┼─────────────────────────────────────────┤   
  │ ❄️  Colapso            │ precio < 24h low · CVD ≤1/5 bull · OI subiendo │ Pánico controlado; short o esperar      │
  │                       │                                                │ mean-reversion                          │   
  ├───────────────────────┼────────────────────────────────────────────────┼─────────────────────────────────────────┤
  │ 🚀 Impulso            │ %chg 1h > 2σ · vol creciente · trend ≥0.6      │ Movimiento joven, entrada antes de FOMO │   
  │                       │                                                │  masivo                                 │   
  ├───────────────────────┼───────────────────────────────────────────────┼──────────────────────────────────────────┤
  │ 🌱 Acumulación        │ régimen=RANGING · CVD positivo persistente ·  │ Smart money cargando antes del move      │   
  │                       │ OB imbalance bid                              │                                          │
  ├───────────────────────┼───────────────────────────────────────────────┼──────────────────────────────────────────┤   
  │ 💧 Distribución       │ régimen=RANGING · CVD negativo · OB imbalance │ Salida silenciosa; evitar long,          │
  │                       │  ask                                          │ considerar short                         │   
  ├───────────────────────┼───────────────────────────────────────────────┼──────────────────────────────────────────┤
  │ 🎢 Volátiles          │ ATR > 2× media 30d                            │ Scalp friendly, peligroso para posición  │   
  ├───────────────────────┼───────────────────────────────────────────────┼──────────────────────────────────────────┤   
  │ 🛌 Durmientes         │ ATR < 0.5× media 30d · vol bajo · sin         │ Coiled spring; próximo movimiento será   │
  │                       │ breakout en 7d                                │ grande                                   │   
  ├───────────────────────┼───────────────────────────────────────────────┼──────────────────────────────────────────┤
  │ ✨ Recién listados    │ listing < 30d                                 │ Volatilidad extrema, oportunidad pero    │   
  │                       │                                               │ alta varianza                            │   
  ├───────────────────────┼───────────────────────────────────────────────┼──────────────────────────────────────────┤
  │ 🧲 Magnetos de        │ $liq 24h > $5M · OI/MarketCap > 5%            │ Predispuestos a long/short squeeze       │   
  │ liquidación           │                                               │                                          │   
  ├───────────────────────┼───────────────────────────────────────────────┼──────────────────────────────────────────┤
  │ 🌊 Funding extremo    │ abs(funding 8h) > 0.05%                       │ Crowded trade; contrarian setup          │   
  ├───────────────────────┼───────────────────────────────────────────────┼──────────────────────────────────────────┤   
  │ 📐 Desacoplados       │ correlación 30d con BTC < 0.3                 │ Diversificación real, no falsa           │
  ├───────────────────────┼───────────────────────────────────────────────┼──────────────────────────────────────────┤   
  │ 👑 Líderes de sector  │ top market cap o vol en su categoría          │ Termómetro de narrativa                  │
  ├───────────────────────┼───────────────────────────────────────────────┼──────────────────────────────────────────┤   
  │ 🐺 Lobos solitarios   │ par moviéndose contra su sector               │ Catalizador idiosincrático (hack,        │
  │                       │                                               │ listing, asociación)                     │   
  └───────────────────────┴───────────────────────────────────────────────┴──────────────────────────────────────────┘
                                                                                                                         
  Implementación práctica

  Un nuevo endpoint:                                                                                                     
  GET /api/constellation/{tag}?limit=20&style=medium
                                                                                                                         
  Y en la UI un selector tipo "tabs estelares" arriba del board principal. Cada constelación recalculada cada 30s (puedes
   reutilizar el scan loop existente).                                                                                   
                                                                                                                         
  Esfuerzo: ~2 días de trabajo. La data ya la tienes; falta sólo el filtrado + endpoint + UI.                            
                  
  ---                                                                                                                    
  PARTE 4 — "Tejido Narrativo": qué hay detrás de cada par
                                                                                                                         
  Tu pregunta del azúcar/dulces es exactamente el problema de rotación sectorial. Trasladada a cripto:
                                                                                                                         
  ▎ "Si sube SOL → ¿qué tokens del ecosistema Solana también suben?"                                                     
  ▎ "Si Bitcoin entra en bull → ¿qué L2 de Bitcoin (STX, ORDI, SATS) se mueven más?"                                     
  ▎ "Si OpenAI lanza algo → ¿qué tokens AI (FET, TAO, RNDR, AGIX) bombean?"                                              
                                                                                                                         
  Módulo propuesto: core/narrative.py                                                                                    
                                                                                                                         
  Fuente de datos: CoinGecko API gratis (/coins/{id} te da categories).                                                  
   
  Estructura sugerida:                                                                                                   
  # core/narrative.py
  @dataclass         
  class SymbolContext:
      symbol: str                                                                                                        
      name: str                    # "Solana"
      sector: str                  # "Layer 1"                                                                           
      narratives: list[str]        # ["AI", "RWA", "Meme"]
      ecosystem: str               # "Solana", "Ethereum", "Bitcoin", "Native"                                           
      sector_peers: list[str]      # otros L1 grandes                                                                    
      ecosystem_children: list[str] # tokens del ecosistema                                                              
      leader_correlation: float    # correlación 30d con el líder                                                        
      description_short: str       # 1 línea humana                                                                      
                                                                                                                         
  Vista nueva: "El por qué del par"                                                                                      
                                                                                                                         
  Cuando seleccionas SOLUSDT, debajo de las gauges, un panel:                                                            
                                                                                                                         
  SOLANA — Layer 1 público de alto throughput                                                                            
  Sector: Layer 1 (peers: ETH, AVAX, SUI, APT)                                                                           
  Narrativas activas: DePIN, Memecoins, DEX
  Ecosistema (correlación 30d con SOL):                                                                                  
    JTO 0.78  ·  JUP 0.71  ·  RAY 0.65  ·  BONK 0.58  ·  WIF 0.61                                                        
  Si SOL +3% → mediana del ecosistema: +4.2%   (β=1.4)                                                                   
  Próximos catalizadores: Firedancer testnet (Jun 2026)                                                                  
  Unlocks próximos: 18M SOL en 23 días (~$3.6B notional)                                                                 
                                                                                                                         
  El "azúcar → dulces" en versión cripto                                                                                 
                                                                                                                         
  Cuando detectes que SOL rompe un nivel clave, el sistema te debe alertar de inmediato sobre todos los pares del        
  ecosistema con alta correlación, ordenados por β, para que entres en el token "dulce" antes de que el resto del mercado
   lo conecte.                                                                                                           
                  
  Esto es edge real e institucional. Es lo que hacen los flujos macro: ver el commodity primario y rotar al derivado de  
  segundo orden.
                                                                                                                         
  Aplicaciones concretas en cripto:                                                                                      
  - BTC rompe → STX, ORDI, SATS, RUNECOIN reaccionan minutos después.
  - ETH sube → LDO, RPL, EIGEN, ENA (restaking & LSDs) se aceleran.                                                      
  - AI hype global → TAO, FET, RNDR, AKT, AGIX correlacionan.      
  - Bull en memecoins → SHIB, PEPE, FLOKI, BONK, WIF rotan.                                                              
                                                                                                                         
  Esfuerzo: 3-4 días (fetch CoinGecko + cache + cálculo de β + UI panel).                                                
                                                                                                                         
  ---                                                                                                                    
  PARTE 5 — Herramientas para "crecer dinero" (honestamente)                                                             
                                                                                                                         
  Voy a ser directo: "fácil + seguro + exponencial" simultáneo no existe. Es como pedir comida deliciosa, gratis y
  saludable a la vez. Pero puedes acercarte mucho con esta combinación:                                                  
                  
  a) Kelly Fraccional (el tamaño correcto de la apuesta)                                                                 
                  
  Tu sistema ya calcula R:R y win-rate. Falta cerrar el loop:                                                            
                  
  fracción óptima = (W × R - L) / R                                                                                      
  fracción real recomendada = fracción óptima × 0.25  (¼-Kelly, anti-volatilidad)                                        
                                                                                                                         
  Si tu win-rate histórico es 55% y R:R medio es 2.0 → Kelly full = 32%. ¼-Kelly = 8% del equity por trade.              
  → Esto es lo que NO debe ser configurable a ojo; debe calcularse desde tu propio trade_journal.                        
                                                                                                                         
  b) Composición disciplinada (la regla 50/30/20)                                                                        
                                                                                                                         
  Cada vez que la sesión cierra en verde:                                                                                
  - 50% se reinvierte (aumenta el equity de trading)
  - 30% sale a cold storage (BTC/ETH, intocable, snowball largo plazo)                                                   
  - 20% explora algo nuevo (par nuevo, estrategia nueva, herramienta nueva)
                                                                                                                         
  Efecto exponencial real: con 50% reinvertido y sesiones de +2% semanal, doblas capital en ~36 semanas. Sin esta regla, 
  gastas las ganancias y trabajas en círculo.                                                                            
                                                                                                                         
  c) Piramidación inteligente                                                                                            
                  
  Cuando un trade va +1R, añadir 50% de la posición original con SL en breakeven. Si va +2R, otro 25%. Asimétrico:       
  pérdidas pequeñas, ganancias grandes. Tu sistema ya tiene la mecánica de breakeven; sólo falta el "scale-in".
                                                                                                                         
  d) Anti-fragilidad por seguros

  Una vez al mes, 0.5% del equity en calls/puts BTC out-of-the-money en Deribit. Si todo va bien, los pierdes (cost of   
  doing business). Si hay flash crash o pump descomunal, financian un año.
                                                                                                                         
  e) Yield base   

  Mientras esperas señales, el cash en USDT puede ganar 5-8% APY en Aave/Compound o en cuentas Bybit Earn. Sobre $10k →  
  $500-800/año "gratis" sólo por no dejarlo dormido. Cuidado: smart contract risk; usar sólo blue-chip protocols.
                                                                                                                         
  f) Multi-cuenta / Multi-strategy                                                                                       
   
  Una cuenta para tu sistema actual (alta frecuencia), otra para "swing positions" basadas en macro (más lentas, más     
  tamaño, menos trades). No mezcles. Cada estrategia con su propio capital y riesgo. Diversificación de estrategias > 
  diversificación de assets.                                                                                             
                  
  ---
  PARTE 6 — Razonamiento geométrico, topológico y disruptivo
                                                                                                                         
  Geometría: el espacio de fases del mercado
                                                                                                                         
  Imagina cada par como un punto en un espacio n-dimensional:
  - Eje X: momentum (1h % change)                                                                                        
  - Eje Y: régimen (–1 ranging, 0 trending, +1 volatile)                                                                 
  - Eje Z: CVD strength                                 
  - Color: ATR percentil                                                                                                 
  - Tamaño: liquidaciones 24h
                                                                                                                         
  En este espacio, los trades ganadores históricos forman clusters (zonas de buena pesca). Calcula las coordenadas de    
  cada trade que cerraste en +R y verás regiones del espacio que son consistentemente rentables y otras que son trampas. 
                                                                                                                         
  → Acción concreta: entrena un clasificador simple (o pídeselo al LLM en cada análisis) que diga: "este setup está en   
  zona pesca-buena (similar a 47 trades ganados) o zona trampa (similar a 23 perdidos)". Tu log_analyst.py muerto en agua
   es exactamente esto.                                                                                                  
                  
  Topología: la forma del mercado cambia, no sólo su valor                                                               
   
  El mercado no es una línea, es una superficie elástica que se deforma. Cuando el funding rate global sube agresivamente
   y la dominancia BTC baja simultáneamente, el "terreno" cambia: lo que antes era una colina rentable, ahora es un
  valle. Tus speed_levels nano/scalp/fast/standard ya capturan algo de esto, pero a nivel de timeframe, no a nivel de    
  régimen global de mercado.

  → Acción concreta: un módulo core/market_weather.py que clasifica el estado global del mercado cada 5 minutos: RISK_ON 
  / RISK_OFF / SQUEEZE_LONG / SQUEEZE_SHORT / DEAD. Tus estrategias adaptan agresividad según el clima.
                                                                                                                         
  Expansión / disrupción / abundancia

  - Expansión: Tu sistema hoy trades 1 par a la vez (mayormente). Con constellations + narrative puede operar narrativas 
  enteras — entrar long en 3-5 tokens del mismo cluster cuando el líder rompe, con risk distribuido.
  - Disrupción: El próximo salto no es "mejor señal", es un nuevo loop: detectar narrativa → entrar en líder → escalar al
   ecosistema → tomar profit cuando los rezagados FOMO. Bloomberg no hace esto en cripto. Hay edge real.                 
  - Abundancia: Cada par es una oportunidad; con 100 símbolos cargados, siempre hay algo en breakout. La escasez no está
  en oportunidades — está en atención y disciplina. Tu UI debe filtrar agresivamente para protegerlas.                   
                  
  ---                                                                                                                    
  PARTE 7 — Cómo lo usaría yo (caso de uso completo de un día)
                                                              
  07:00 — Abro QTS. Veo "Clima de mercado": RISK_ON (BTC dom bajando, funding equilibrado, vol BTC media). Decisión:
  agresividad estándar, no nano.                                                                                         
   
  07:05 — Reviso constelación 🔥 Breakout. 3 candidatos: TAOUSDT, FETUSDT, RNDRUSDT. Todos sector AI. El "Tejido         
  Narrativo" me confirma: rotación a AI en marcha (los 3 corren juntos con β >1.2 vs líder TAO).
                                                                                                                         
  07:08 — Selecciono TAOUSDT (líder, mayor liquidez). Modo MAIN: régimen TRENDING_UP, score 82, CVD 5/5, RSI 64 (no      
  sobrecomprado todavía). Botón "SUGERIR SL/TP" estilo MEDIO. Sistema me da SL en swing low local (–1.8%), TP en nivel de
   liquidación grande (+4.2%). R:R = 2.3.                                                                                
                  
  07:10 — Kelly fraccional del sistema me sugiere 7% del equity. Confirmo entrada. Mentor mode me alerta: "Tienes 2      
  posiciones AI activas ya. Correlación 0.81. Considera reducir tamaño 30%". Bajo a 5%.
                                                                                                                         
  07:12 — Posición OPEN. Strip vertical activo. El sistema añade automáticamente alerta: "Si FET o RNDR rompen su 1h high
   → notificar (rotación confirmada)".
                                                                                                                         
  07:35 — Posición en +1R. Sistema mueve SL a breakeven (automático). Piramidación opcional dispara: "¿Añadir 50% con SL 
  en BE? S/N". Sí.
                                                                                                                         
  08:15 — Posición en +2.3R. TP hit. Sesión cierra esta operación con +$430. Regla 50/30/20 activa: $215 reinvierten,    
  $130 va a comprar BTC spot automáticamente (vía API), $86 quedan en "exploración".
                                                                                                                         
  Tarde — log_analyst corre: "He visto 12 trades AI esta semana. Win rate 67%, R:R medio 1.9. Sigue funcionando. Pero tus
   entradas SHORT en regímenes RANGING tienen win rate 31%. Sugerencia: filtrar SHORT sólo en TRENDING_DOWN. Aplicar?".
  Acepto.                                                                                                                
                  
  Domingo — El sistema mismo me manda un Slack/email: "Semana cerrada +6.3%, drawdown máx 2.1%. Sesión más rentable:     
  Martes mañana AI rotation. Sesión más débil: viernes noche memes (–1.4%, sugerencia desactivar fines de semana en
  memes)."                                                                                                               
                  
  Esto es delegación de inteligencia, no de control. Tú decides la dirección estratégica; el sistema ejecuta y aprende.  
   
  ---                                                                                                                    
  PARTE 8 — Roadmap concreto (qué construir, en qué orden)
                                                                                                                         
  Fase 0 (esta semana) — Cerrar lo abierto
                                                                                                                         
  1. Revisar y commitear los cambios de web/* (modo MAIN/mentor).                                                        
  2. Probar el endpoint /api/suggest-levels con 5 símbolos de distinta liquidez.
  3. Hacer un commit limpio con mensaje claro: "feat: mentor mode con sugerencia SL/TP en pre-trade".                    
                                                                                                                         
  Fase 1 (1-2 semanas) — Contexto narrativo                                                                              
                                                                                                                         
  1. core/narrative.py con fetch CoinGecko + cache DuckDB (24h TTL).                                                     
  2. Panel "El por qué del par" en modo MAIN.
  3. Tabla symbol_correlations recalculada cada hora con ventana 30d.                                                    
                                                                                                                         
  Fase 2 (1-2 semanas) — Constelaciones                                                                                  
                                                                                                                         
  1. Filtros en core/scanner.py para los 13 tags propuestos.                                                             
  2. Endpoint GET /api/constellation/{tag}.
  3. UI: barra de tabs estelar arriba del board principal.                                                               
  4. Sonido/notification opcional al detectar nueva entrada en una constelación marcada como "favorita".                 
                                                                                                                         
  Fase 3 (2-3 semanas) — Inteligencia que aprende                                                                        
                                                                                                                         
  1. Resucitar core/log_analyst.py: corre cada 6h, analiza trade_journal completa, escribe insights en una tabla         
  insights.       
  2. Modo MAIN muestra "Insights relevantes para este setup" basados en trades pasados similares.                        
  3. Kelly fraccional dinámico calculado desde win_rate/RR reales del journal.                                           
                                                                                                                         
  Fase 4 (2-3 semanas) — Risk de portfolio (no por trade)                                                                
                                                                                                                         
  1. Cálculo VaR del portfolio actual.                                                                                   
  2. Alerta de correlación cruzada al abrir nueva posición.
  3. Regla 50/30/20 automatizada con cron que mueve a Bybit Spot al cierre de sesión verde.                              
                                                                                                                         
  Fase 5 (cuando todo lo anterior esté sólido) — Disrupciones                                                            
                                                                                                                         
  1. core/market_weather.py (régimen global).                                                                            
  2. Trading multi-cuenta: una cuenta hi-freq, otra swing macro.
  3. Integración Coinglass/Hyblock para liquidation heatmap.                                                             
  4. News-flow LLM filtrado por watchlist.                                                                               
  5. Paper wallet completo → backtesting honesto de cada nueva estrategia.                                               
                                                                                                                         
  ---             
  La metáfora final                                                                                                      
                                                                                                                         
  Tu sistema hoy es un velero solo bien construido. Llega lejos pero requiere mucha atención.
                                                                                                                         
  Lo que falta es:
  1. Mapa estelar (constelaciones) → ves todo el océano de un vistazo.                                                   
  2. Brújula de narrativas (tejido) → sabes hacia dónde sopla cada viento.                                               
  3. Cuaderno de bitácora que aprende (log_analyst real) → cada travesía te enseña.
  4. Reglas de capitanía (Kelly + 50/30/20) → repartes el cargamento sin perder el barco entero en una tormenta.         
                                                                                                                         
  Con esto pasa de ser un velero a ser una flota mercante con escolta IA: muchas rutas en paralelo, cada una con su      
  escudo, y el conjunto se hace más fuerte y más grande cada viaje.                                                      
                                                                                                                         
  ---                                                                                                                    
  ¿Por dónde quieres empezar? Mi recomendación: Fase 0 + Fase 1 (commitear el modo MAIN actual + añadir narrative.py con
  tags de CoinGecko). Es la base sobre la que todo lo demás se monta y se ve en ~10 días de trabajo.   
