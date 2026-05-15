QTS · Plan brutal para la cabina del piloto                                                                            
                                                                                                                         
  He mapeado el sistema. La barra que ya tienes (calculator.py:calc_position_metrics, app.js:buildProgressBar líneas     
  116-244) es un lienzo plano lineal SL→TP. Lo vamos a convertir en un espaciotiempo navegable: un sistema con tres      
  dimensiones acopladas — precio (eje espacial), tiempo (eje histórico), liquidez (curvatura del fondo) — sobre el cual  
  el operador conduce.                                                                                                   
                                                                                                                         
  El marco mental: la barra deja de ser una regla y se vuelve una variedad métrica. Cada zoom es un cambio de carta local
   (topología); cada zona acumula tiempo como una integral (cronotopología); la liquidez deforma la geometría como una   
  masa (gravitación). Tú, piloto, lees el terreno desde un dashboard.                                                    
                  
  ---
  Fase 0 — Refactor conceptual (no opcional)
                                                                                                                         
  Problema: hoy todo lo importante se calcula en calculator.py y se manda cocinado en el snapshot. Para zoom y dashboards
   reactivos eso no escala: el frontend necesita la geometría cruda y poder reproyectarla sin ida-y-vuelta al server.    
                  
  Acción:                                                                                                                
  - web/calculator.py: separar compute_geometry() (entry, sl, tp, be, milestones, mark, qty, dirn — datos invariantes) de
   compute_pnl() (fees, ROI, PnL — derivables en front).                                                                 
  - web/server.py:_build_snapshot (línea 242): emitir un bloque geometry por posición, plano y limpio, además de los
  métricas listas.                                                                                                       
  - Nuevo módulo frontend app.js → /static/lib/scale.js: una función de transformación de coordenadas T(price → bar%)    
  parametrizada por una ventana [priceMin, priceMax]. Hoy la ventana es implícitamente [sl, tp]. Mañana es una variable
  de estado.                                                                                                             
                  
  Este refactor desbloquea las tres fases siguientes con una única abstracción.                                          
                                                                                                                         
  ---
  Fase 1 — Zoom telescópico (transformada de escala)                                                                     
                                                    
  El gesto de zoom no es UI — es una reproyección de la carta local. Definimos zoom en términos del rango visible
  relativo al SL→TP total.                                                                                               
  
  Modelo                                                                                                                 
                  
  ventana = { center: price, span_pct: 100 | 50 | 25 | 12.5 | 6.25 }                                                     
  T(price) = clamp(0, 100, (price - viewMin) / (viewMax - viewMin) * 100)                                                
  viewMin = center - (sl_tp_range * span_pct/2) / 100                                                                    
  viewMax = center + (sl_tp_range * span_pct/2) / 100                                                                    
                                                                                                                         
  Niveles discretos (como temporalidades de TradingView)                                                                 
                  
  ┌───────┬───────┬───────────────────────────────────────────────────────────────────────────────────────────────────┐  
  │ Nivel │ Span  │                                              Mostrar                                              │
  ├───────┼───────┼───────────────────────────────────────────────────────────────────────────────────────────────────┤  
  │ L0    │ 100%  │ SL ↔ TP (vista actual)                                                                            │
  ├───────┼───────┼───────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ L1    │ 50%   │ mitad centrada en mark                                                                            │  
  ├───────┼───────┼───────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ L2    │ 25%   │ zoom intermedio                                                                                   │  
  ├───────┼───────┼───────────────────────────────────────────────────────────────────────────────────────────────────┤  
  │ L3    │ 12.5% │ micro-zona, ticks reales                                                                          │
  ├───────┼───────┼───────────────────────────────────────────────────────────────────────────────────────────────────┤  
  │ L4    │ 6.25% │ "respiración" del precio                                                                          │
  ├───────┼───────┼───────────────────────────────────────────────────────────────────────────────────────────────────┤  
  │ L−1   │ 200%  │ zoom out: muestra el contexto pre-SL y post-TP (donde están las stop hunts y las extensiones,     │
  │       │       │ clave)                                                                                            │  
  └───────┴───────┴───────────────────────────────────────────────────────────────────────────────────────────────────┘
                                                                                                                         
  Comportamiento adaptativo

  - Anclaje inteligente: al hacer zoom, el centro no es siempre mark. Si el mark está cerca de una zona crítica (BE, hito
   50%, SL), el centro se imanta a ese punto. Centrarse en el mark cuando está lejos de todo, centrarse en lo importante
  cuando está cerca.                                                                                                     
  - Auto-zoom contextual: opción "Seguir precio" — si el precio está estacionario >N minutos, sugerir L1/L2
  automáticamente (es exactamente tu problema actual: trade de horas comprimido en 5% de la barra).                      
  - Niveles fuera de ventana: SL/TP/BE/hitos que caen fuera del span actual se renderizan como flechas pegadas al borde
  con su distancia (◀ SL −1.2%). Nunca desaparecen — la información se preserva, solo cambia de forma.                   
  - Lectura cuántica: el zoom es discreto (niveles fijos), no continuo. Esto evita la ansiedad del scroll infinito y
  respeta el principio de escalas significativas (las temporalidades de un chart son discretas por una razón).           
                  
  UI                                                                                                                     
                  
  - Mini control en la cabecera de la barra: [−]  L0  [+] (icono lupa) o teclado + / − / 0 (0 = reset).                  
  - Indicador del span actual debajo de la barra (Vista: 25% · centro: BE).
  - Animación de transición ~250ms con easing cubic-bezier(.4,0,.2,1) — el cambio de carta debe sentirse como acercar una
   cámara, no como un corte.                                                                                             
  - Estado persistido por symbol_direction en localStorage (qts_zoom_BTCUSDT_Buy = 2).                                   
                                                                                                                         
  Archivos        
                                                                                                                         
  - Nuevo: web/static/lib/scale.js (~80 líneas, puro).                                                                   
  - Modificar: web/static/app.js:buildProgressBar (116-244) para consumir T() en vez de leer entry_pct_bar directo del
  backend.                                                                                                               
  - Modificar: web/static/style.css (360-500) para indicadores de "fuera de ventana".
                                                                                                                         
  ---             
  Fase 2 — Cronotopología: el tiempo que el precio cocina en cada zona                                                   
                                                                                                                         
  Esto es brutal y subestimado. El tiempo-en-zona es la integral del precio sobre el espacio, y revela:
                                                                                                                         
  1. Soportes/resistencias verdaderos: una zona donde el precio reposó 4h tiene memoria, no es ruido. El mercado vuelve a
   respirar ahí.                                                                                                         
  2. Compresión vs expansión: mucho tiempo en poca zona = acumulación (resorte cargado). Poco tiempo en mucha zona =     
  impulso (energía liberada).                                                                                            
  3. Calidad del trade: si pasó 6h en entry→BE antes de moverse al 50%, ese trade fue un combate de attrition; si tardó
  8min, fue un sprint.                                                                                                   
                  
  Modelo de datos                                                                                                        
                  
  Nuevo bucle en backend (server.py): por cada (symbol, position_id, zone) muestrear el mark_price cada N segundos (1-5s)
   y acumular tiempo.
                                                                                                                         
  # web/zone_tracker.py (nuevo)
  ZONES = {
      "SL→Entry":   (sl, entry),
      "Entry→BE":   (entry, be),                                                                                         
      "BE→25%":     (be, m25),
      "25%→50%":    (m25, m50),                                                                                          
      "50%→75%":    (m50, m75),
      "75%→TP":     (m75, tp),                                                                                           
      "Above TP":   (tp, ∞),                                                                                             
      "Below SL":   (−∞, sl),
  }                                                                                                                      
  zone_state[pos_id] = {
      zone: { seconds, visits, last_entered_at, max_streak, last_exited_at }                                             
  }                                                                                                                      
                                                                                                                         
  Persistencia: DuckDB (storage/trading.duckdb, ya en uso). Nueva tabla zone_residency:                                  
  trade_id, symbol, zone, entered_at, exited_at, seconds, price_in, price_out
                                                                                                                         
  Esto permite post-mortem por trade (cuántas veces visitó cada zona, cuánto duró cada visita) y agregado histórico      
  (estadísticamente, en este símbolo, la zona BE→25% dura X minutos en promedio).                                        
                                                                                                                         
  Visualización (5 capas, todas dentro de la misma barra)                                                                
                  
  Capa A — Heatmap acumulado sobre la barra                                                                              
  La barra ya tiene un alto de 8px. Aumentar a 22px y dividir en franjas:
  - Franja superior (8px): la barra actual — zonas, fill, marker.                                                        
  - Franja inferior (12px): mapa de calor del tiempo cocinado. Cada zona se pinta con opacidad proporcional al           
  log(seconds_in_zone). Las zonas más visitadas glow sutil.                                                              
                                                                                                                         
  Capa B — Bubbles de duración (sobre cada zona)
  Pequeños círculos sobre cada zona con Σ tiempo. Tamaño del círculo ∝ log(seconds). Hover/tap → tooltip con: visitas,   
  racha máxima, primera/última visita.                                                                                   
                                                                                                                         
  Capa C — Sparkline del precio en el tiempo                                                                             
  Debajo de la barra, mini-chart horizontal de las últimas N velas del trade, con líneas horizontales en cada zona.
  Muestra el "viaje" del precio en el tiempo desde la apertura. Aquí se ve si el precio rebotó, si fue lineal, si dudó.  
                  
  Capa D — Reloj de fases                                                                                                
  A la derecha de la barra, un widget tipo diagrama de Sankey vertical o donut por fases: cada fase del trade (SL→Entry,
  Entry→BE, etc.) ocupa un sector proporcional al tiempo. Lectura instantánea: "este trade vivió 80% en Entry→BE". Es el 
  "dónde gastaste tu vida" del trade.
                                                                                                                         
  Capa E — Anotador semántico
  Una línea pequeña tipo "diario del trade":
                                                                                                                         
  ▎ "Abierto hace 4h 12m · 47% del tiempo en Entry→BE · 3 visitas a BE · max streak en BE→25%: 38m"                      
                                                                                                                         
  Agregación histórica (un módulo aparte, valioso)                                                                       
                  
  Vista nueva en tab Historial: "Mapa de zonas por símbolo". Para cada símbolo, agregar zone_residency sobre los últimos 
  50 trades:
  - Distribución de tiempo por zona → identifica zonas "pegajosas" (verdaderos S/R).                                     
  - Heatmap horario × zona → identifica patrones de hora ("en este símbolo, el rebote de Entry→BE pasa típicamente entre 
  las 14:00-16:00 UTC").                                                                                                
  - Output emergente: soportes/resistencias dinámicos extraídos del comportamiento real de tu cuenta, no del precio      
  abstracto.      
                                                                                                                         
  Archivos        
                                                                                                                         
  - Nuevo: web/zone_tracker.py (state + sampler + persistencia DuckDB).                                                  
  - Nuevo: core/zone_analytics.py (queries agregadas para tab historial).
  - Modificar: web/server.py — bucle nuevo _zone_sampler_loop (cada 2s) + endpoint GET /api/zones/{symbol}.              
  - Modificar: web/static/app.js — capas A-E en buildProgressBar.                                                        
  - Modificar: web/static/style.css — variables para opacidad/glow del heatmap temporal.                                 
                                                                                                                         
  ---                                                                                                                    
  Fase 3 — El planeta de liquidez (curvatura del espaciotiempo)                                                          
                                                               
  La metáfora gravitacional es geométricamente exacta. Las masas de liquidez son pozos en una superficie 2D que deforman
  la trayectoria del precio. Vamos a renderizar esa superficie.                                                          
   
  Datos crudos                                                                                                           
                  
  Ya los tienes (parcialmente):                                                                                          
  - Orderbook profundo: streams/market.py — bids/asks por nivel.
  - Liquidaciones recientes: ya streamed.                                                                                
  - OI (Open Interest): ya tracked.      
  - Tus SL/TP propios: ya en pos.                                                                                        
  - Stop hunts probables: clusters de SL alrededor de soportes/resistencias visibles → derivado de core/liquidity.py.
                                                                                                                         
  Modelo matemático                                                                                                      
                                                                                                                         
  Cada nivel de precio p tiene una masa de liquidez m(p):                                                                
  m(p) = α·orderbook_size(p)
       + β·recent_liquidation_volume(p)                                                                                  
       + γ·suspected_stop_cluster(p)   
       + δ·my_own_orders(p)                                                                                              
   
  La profundidad del pozo en el plano gravitacional:                                                                     
  depth(p) = −log(1 + m(p) / m_ref) · k
                                                                                                                         
  (m_ref es la mediana de liquidez en el rango visible — normalización local, no global; la sensibilidad se adapta al    
  símbolo.)                                                                                                              
                                                                                                                         
  Renderizado (canvas, no SVG)                                                                                           
                  
  Lienzo: una banda extendida debajo de la barra de progreso, ~80px de alto, mismo eje X (precio reproyectado por T() de 
  Fase 1). El eje Y es la "profundidad" del pozo.
                                                                                                                         
  Capa 1 — La tela:                                                                                                      
  Una grilla deformada (líneas curvas) que ondula según depth(p). Líneas horizontales finas (5-7) que se hunden donde hay
   masa. Color: var(--text-sub) con alpha ~0.3. Esto es la tela del espaciotiempo.                                       
                  
  Capa 2 — Las masas (planetas/lunas):                                                                                   
  Círculos en cada cluster de liquidez:
  - Radio ∝ √m(p).                                                                                                       
  - Color por tipo de liquidez:
    - 🔵 azul: SL/stop clusters (peligro, gravedad hostil).                                                              
    - 🟢 verde: bids profundos (soporte, gravedad amiga si vas long).                                                    
    - 🔴 rojo: asks profundos (resistencia).                                                                             
    - 🟡 amarillo pulsante: tus propias órdenes (familiar).                                                              
    - ⚫ negro con halo: zonas de liquidación reciente (cráter).                                                         
  - Glow sutil con box-shadow o blend screen.                                                                            
                                                                                                                         
  Capa 3 — Movimiento:                                                                                                   
  - Expand: cuando una masa crece >X% en N segundos → animación de pulso (escala 1 → 1.15 → 1).                          
  - Shrink: cuando se consume → fade-out lento con un trail.                                                             
  - Drift: el orderbook se mueve; cada update reposiciona suavemente (requestAnimationFrame, no salto).
                                                                                                                         
  Capa 4 — Trayectorias predichas (opcional, fase posterior):                                                            
  Líneas finas que muestran "si el precio cae, la siguiente masa que toca es...". Es el geodésico sobre la tela.         
                                                                                                                         
  Comportamiento                                                                                                         
                                                                                                                         
  - Sincronizado con el zoom: cuando haces zoom en la barra, la tela se reescala. En L4 (6.25%) ves la microestructura — 
  cada bid/ask individual; en L−1 (200%) ves los planetas grandes y el contexto extendido.
  - Click en una masa: abre un mini-panel con detalle (tamaño exacto, fuente, edad, hisstoria — "esta masa ha estado aquí
   2h 13m").                                                                                                             
  - Toggle por tipo: filtros como cajitas de checkbox arriba de la tela ([ ] SLs  [ ] Bids  [ ] Asks  [ ] Liq). Permite
  leer la geometría limpia.                                                                                              
                  
  Archivos                                                                                                               
                  
  - Nuevo: web/liquidity_map.py — agrega orderbook + liquidaciones + stop clusters en una estructura por niveles de      
  precio.
  - Modificar: web/server.py — endpoint GET /api/liquidity/{symbol} + WS canal complementario (no cargar el snapshot de  
  1s con esta data — es voluminosa, va por su propio canal a ~3-5Hz).                                                    
  - Nuevo: web/static/lib/gravity.js — render canvas, anima con requestAnimationFrame.
  - Modificar: web/static/app.js — montar el canvas debajo de la barra en buildPosCardPro.                               
  - Modificar: web/static/style.css — nuevas variables --gravity-bg, --mass-bid, --mass-ask, --mass-stop.                
                                                                                                                         
  ---                                                                                                                    
  Fase 4 — El dashboard del piloto (los instrumentos)                                                                    
                                                                                                                         
  La barra + la tela + el tiempo cocinado es el terreno. Ahora necesitas la cabina. Tres instrumentos, un velocímetro de
  contexto, una alerta direccional.                                                                                      
                  
  Instrumento 1 — Tacómetro del sentimiento (afán / miedo)                                                               
                  
  Qué mide: la presión emocional del flujo. No es el precio — es la agitación.                                           
                  
  Inputs:                                                                                                                
  - Tasa de cancelación de órdenes (orderbook churn).
  - Trades agresivos por unidad de tiempo (market orders consumiendo liquidez).                                          
  - Imbalance bid/ask en el top-of-book.                                       
  - Liquidaciones recientes ponderadas por tamaño.                                                                       
                                                                                                                         
  Output: un valor [0..1] que representa la temperatura emocional.                                                       
                                                                                                                         
  Visual: aguja semicircular tipo speedometer real, escala dividida en:                                                  
  - 0–0.3: 🧊 Frío (mercado dormido)                                                                                     
  - 0.3–0.6: 😐 Normal                                                                                                   
  - 0.6–0.8: 🔥 Caliente
  - 0.8–1.0: 💥 Pánico/Euforia                                                                                           
                              
  Con redline pulsante cuando entra en el rango superior. La aguja oscila con transition: transform .4s ease-out para que
   el movimiento se sienta físico, no escalonado.                                                                        
   
  Instrumento 2 — Velocímetro del precio (velocidad real)                                                                
                  
  Qué mide: velocidad del precio en %/min normalizada al ATR del símbolo.                                                
                  
  Lectura: "este símbolo normalmente se mueve a 0.15%/min; ahora va a 0.42%/min" → 2.8x normal.                          
                  
  Visual: otro speedometer al lado del primero. Escala:                                                                  
  - 0–1x: cruise  
  - 1–2x: activo                                                                                                         
  - 2–4x: caliente
  - 4x+: explosión                                                                                                       
                  
  Alerta de cambio de régimen: cuando el ratio cruza 2x → toast no-bloqueante en la esquina + flash sutil del marker en  
  la barra. Esa es tu oportunidad según la descripción que diste.                                                        
                                                                                                                         
  Instrumento 3 — Tipo de camino                                                                                         
                  
  Qué clasifica: la "superficie" del momento.                                                                            
   
  Inputs:                                                                                                                
  - Profundidad media del orderbook en el rango visible (¿hay líquido o vacío?).
  - Volatilidad realizada vs ATR.                                                                                        
  - Spread bid-ask normalizado.  
                                                                                                                         
  Outputs (5 estados):                                                                                                   
                                                                                                                         
  ┌───────────────┬────────────────────────────────────┬────────────────────────────────────────┐                        
  │    Estado     │            Significado             │          Recomendación visual          │                        
  ├───────────────┼────────────────────────────────────┼────────────────────────────────────────┤
  │ 🛣 Autopista   │ Liquidez profunda, baja vol        │ "Nitro disponible" (apalancamiento ok) │
  ├───────────────┼────────────────────────────────────┼────────────────────────────────────────┤
  │ 🏁 Pista      │ Tendencia limpia, fricción mínima  │ "Acelera"                              │                        
  ├───────────────┼────────────────────────────────────┼────────────────────────────────────────┤                        
  │ 🛤 Carretera   │ Normal                             │ "Crucero"                              │                        
  ├───────────────┼────────────────────────────────────┼────────────────────────────────────────┤                        
  │ 🌳 Terracería │ Baja liquidez, gaps                │ "Despacio"                             │
  ├───────────────┼────────────────────────────────────┼────────────────────────────────────────┤                        
  │ ⛰ Off-road    │ Caos, liquidaciones, spread amplio │ "Detente / cerrar"                     │
  └───────────────┴────────────────────────────────────┴────────────────────────────────────────┘                        
                  
  Visual: badge prominente arriba del dashboard con icono + label. Cambio de estado dispara animación de transición y log
   persistido. Cuando el estado degrada (Pista → Carretera → Terracería → Off-road) la app reduce visualmente el botón de
   "Aumentar tamaño" y enfatiza "Cerrar".                                                                                
                  
  Instrumento 4 — Indicador G (cuando el camino cambia)                                                                  
   
  Cuando los tres instrumentos coinciden (tacómetro > 0.7 ∧ velocímetro > 2x ∧ camino = Autopista/Pista) → alerta de     
  oportunidad:    
  - Banner superior glow verde, ~3s con sonido opcional.                                                                 
  - Texto sintético: "BTC · 138% velocidad · liquidez limpia · ventana 90s probable".                                    
  - Botón directo: "Ver", abre el panel pre-rellenado.                               
                                                                                                                         
  Cuando coincide la combinación opuesta (pánico + velocidad alta + off-road) → alerta roja: "Cerrar discrecional ·      
  entorno hostil".                                                                                                       
                                                                                                                         
  Layout                                                                                                                 
                  
  Los 4 instrumentos viven en un strip horizontal compacto arriba de cada posición o como sección global. En mobile:     
  stack vertical colapsable. Ningún instrumento debe robar más de ~10% de la pantalla — son cabina, no contenido.
                                                                                                                         
  Archivos        

  - Nuevo: core/sentiment.py — calcula los 4 indicadores con ventanas rodantes.                                          
  - Modificar: web/server.py — incluir bloque dashboard_metrics en snapshot.
  - Nuevo: web/static/lib/gauges.js — render SVG de los tacómetros (SVG sí, son geometrías estáticas con una aguja que   
  rota).                                                                                                                 
  - Modificar: web/static/app.js — sección buildPilotDashboard(pos|global).
                                                                                                                         
  ---             
  Topología del producto (cómo todo se conecta)                                                                          
                                               
                      ┌─── DASHBOARD DEL PILOTO ───┐
                      │ tacómetro · velocímetro    │                                                                     
                      │ camino · alerta-G          │                                                                     
                      └────────────┬───────────────┘                                                                     
                                   │ contexto                                                                            
                                   ▼
     ┌─── BARRA DE PROGRESO (zoomable) ────────────────────┐                                                             
     │     SL  ────  entry  ─be─  25  50  75  ────  TP     │                                                             
     │       ●─────────────●────● ▼ ●   ●   ●              │                                                             
     │       └─heatmap del tiempo cocinado por zona ──┘    │                                                             
     │       └─sparkline del viaje histórico del precio ─┘ │                                                             
     └─────────────────────┬───────────────────────────────┘                                                             
                           │ mismo eje X                                                                                 
                           ▼
     ┌─── TELA DE LIQUIDEZ (gravity map) ──────────────────┐                                                             
     │   ◯ ··· ◯ ··· (◉) ··· ◯ ··· ◯  ← masas             │                                                              
     │     ╱──╲    ╱─────╲    ╱──╲     ← curvatura         │                                                             
     └─────────────────────────────────────────────────────┘                                                             
                                                                                                                         
  Un solo eje X compartido (precio, reproyectado por T()), tres planos verticales conectados:                            
  1. Arriba: contexto físico (cómo está el mundo ahora — el dashboard).                                                  
  2. Medio: tu trade (dónde estás, dónde has estado — la barra + tiempo).                                                
  3. Abajo: el terreno (a dónde quieren ir todos los demás — la tela).   
                                                                                                                         
  Es una proyección 3D plegada a 2D, lo que el cerebro puede leer de un vistazo.                                         
                                                                                                                         
  ---                                                                                                                    
  Roadmap por fases (orden recomendado)                                                                                  
                                                                                                                         
  ┌───────────────────────────────────────┬─────────────────┬────────────────┬──────────────────────────┐
  │                 Fase                  │ Tiempo estimado │    Bloquea     │          Valor           │                
  ├───────────────────────────────────────┼─────────────────┼────────────────┼──────────────────────────┤                
  │ 0 — Refactor T() + geometría separada │ 1 sesión        │ Todo           │ Infraestructura          │
  ├───────────────────────────────────────┼─────────────────┼────────────────┼──────────────────────────┤                
  │ 1 — Zoom telescópico                  │ 1-2 sesiones    │ —              │ Resuelve tu dolor hoy    │                
  ├───────────────────────────────────────┼─────────────────┼────────────────┼──────────────────────────┤                
  │ 2 — Cronotopología (tiempo en zonas)  │ 2-3 sesiones    │ —              │ Edge real, datos propios │                
  ├───────────────────────────────────────┼─────────────────┼────────────────┼──────────────────────────┤                
  │ 3 — Tela de liquidez                  │ 3-4 sesiones    │ —              │ El "wow", trading-grade  │
  ├───────────────────────────────────────┼─────────────────┼────────────────┼──────────────────────────┤                
  │ 4 — Dashboard del piloto              │ 2-3 sesiones    │ Fase 3 parcial │ Decisión asistida        │
  └───────────────────────────────────────┴─────────────────┴────────────────┴──────────────────────────┘                
                  
  Recomendación: Fase 0 + Fase 1 + capa A/B/D de Fase 2 en el primer sprint. Eso resuelve tu problema actual y deja la   
  infraestructura para todo lo demás. La tela y el dashboard son el segundo y tercer sprint.
                                                                                                                         
  ---             
  Decisiones de UI/UX (principios rectores)
                                                                                                                         
  1. Densidad sin ruido: cada pixel debe responder a una pregunta operativa. Si no responde una pregunta, fuera.
  2. Animación con propósito: las transiciones (250-400ms, easing físico) refuerzan la causalidad — el zoom acerca, la   
  masa pulsa porque crece, la aguja oscila porque siente. Nada de animaciones decorativas.                               
  3. Información que cambia de forma, no que desaparece: un nivel fuera del zoom se vuelve flecha al borde, no se borra. 
  La memoria perceptual del operador debe estar intacta.                                                                 
  4. Modos opcionales, default sobrio: Pro/Lite ya existe — agregamos Pilot Mode (todo encendido) y Focus Mode (solo
  barra + zoom). Persistir por sesión.                                                                                   
  5. Lectura periférica: los gauges deben ser legibles con visión periférica. Color > forma > texto, en ese orden.
  6. Keyboard-first: + − zoom, T toggle tela, G toggle gauges, Z foco en zona actual. El piloto experto no mueve el      
  ratón.                                                                                                                 
                                                                                                                         
  ---                                                                                                                    
  ¿Arrancamos con Fase 0 + 1 (refactor + zoom) ahora mismo, o prefieres que empiece por la cronotopología que tiene el
  mayor edge analítico?                                                                                                  
   






hecho y explicacion:

1) Progress bar con cronotopología (las dos franjas debajo)
                                                                                                                         
  Franja superior (heatmap) — opacidad ∝ log(tiempo cocinado en esa zona). La zona actual brilla con borde amarillo.
  Franja inferior (stack) — proporción del tiempo total del trade pasado en cada fase.                                   
                                                            
  Casos reales:                                                                                                          
  - Trade BTC LONG, 3 horas en E→BE (60% del stack) → el precio entra, no consolida arriba de BE, oscila contra ti.
  Acción: no es un setup limpio, considera mover SL más cerca o cerrar y reentrar mejor.                                 
  - 30 min BE→25% + 2 horas 25→50% (80% en zona profit) → trade saludable, está cocinando. Acción: trailing del SL al 25%
   ya alcanzado.                                                                                                         
  - Heatmap muestra picos en BE→25% y 50→75%, vacío en 25→50% → ese rango es un LVN (low volume node), el precio lo cruza
   rápido. Insight: si vuelve, no esperes soporte ahí.                                                                   
                                                                                                                         
  2) Strip gravity (canvas con barras y dots)               
                                                                                                                         
  Eje X = precio (mismo viewport que el progress bar; el zoom afecta a los dos).                                         
                                                                                                                         
  Lo que ves:                                                                                                            
  - Barras verdes hacia abajo = bids (compras en el libro). Más gruesas/opacas = más liquidez.
  - Barras rojas hacia arriba = asks (ventas).                                                                           
  - Halo gris de fondo = volumen histórico negociado (volume profile).
  - Líneas verticales con etiquetas: HVN (cyan, alta participación), LVN (ámbar, vacío), EQ_H/EQ_L (rojo/verde, stops    
  acumulados), ROUND (lila, números psicológicos).                                                                       
  - Burbujas con glow = liquidaciones. Verde = shorts liquidados (alza brutal), rojo = longs liquidados (caída). Se      
  desvanecen con la edad (~2 min).                                                                                       
  - Triángulos = tus órdenes pendientes (verde = Buy, rojo = Sell).                                                      
  - Línea blanca punteada vertical = precio actual.                
                                                                                                                         
  Casos reales:                                                                                                          
  - Pared roja gruesa 0.3% arriba del precio → ask wall. Resistencia activa. Probable que el precio rebote o que un whale
   la barra liquidándola. Acción: SL por encima de la pared, no debajo.                                                  
  - Cluster de líneas lilas (ROUND) en $60,000 + EQ_H rojo justo encima → imán psicológico. Cuando el precio se acerca,
  atrae stop hunts. Setup: si vas LONG, no pongas TP exactamente en 60000 — ponlo 0.1% antes.                            
  - Liquidación verde grande en $59,900 con glow fuerte → acaban de barrer shorts. Combustible alcista de corto plazo    
  (segundos a minutos). Acción: trailing rápido o no entrar SHORT en ese tramo.                                      
  - HVN cyan a tu favor entre entry y TP → magnetismo, el precio quiere volver ahí. Buen soporte para mover BE.          
  - LVN ámbar entre mark y TP → vacío de liquidez, el precio cruzará rápido. No esperes que se quede ahí; o llega a TP o
  se devuelve veloz.                                                                                                     
                                                                                                                         
  3) Cabina del piloto (3 gauges)                                                                                        
                                                                                                                         
  Tacómetro de Presión (izquierda)                                                                                       
                                                                                                                         
  Score 0-100 + etiqueta FEAR/GREED/NEUTRAL.                                                                             
                                                            
  Mezcla: imbalance del libro (30%) + pulso CVD (30%) + liquidaciones recientes vs OI (25%) + funding (15%).             
                                                            
  - < 25 NEUTRAL = mercado dormido, sin convicción. No fuerces entradas.                                                 
  - 40-65 GREED/FEAR = sesgo claro, momentum sano.          
  - > 75 = extremo. Cuidado: o es la fase de aceleración final o el punto de reversión.                                  
                                                                                                                         
  Caso real: vas LONG y la aguja sube a 85 GREED → euforia. Probabilidad de pullback alta en 5-15min. Acción: cerrar     
  parcial 50% o mover SL agresivo.                                                                                       
                                                                                                                         
  Caso real 2: estás SHORT, presión 70 FEAR → confirmación, pero si llega a 90+ con liquidaciones rojas masivas en el    
  strip, es capitulación → reverso inminente. Acción: cerrar y voltear o salir.
                                                                                                                         
  Velocímetro (centro)                                      

  % por minuto vs referencia ATR de las últimas 5 velas.                                                                 
  
  - Score 50 = velocidad normal (≈ el rango promedio del símbolo).                                                       
  - 80-100 con color rojo = aceleración violenta. Cualquier setup técnico se rompe.
  - < 20 = mercado quieto, scalping limitado.                                                                            
  
  Caso real: vas a entrar LONG, velocímetro 95 rojo → el precio está corriendo, tu entry probable que se ejecute peor    
  (slippage) y el SL se barra. Acción: espera a que baje a 40-60.
                                                                                                                         
  Caso real 2: posición abierta, velocímetro saltó de 30 a 90 en segundos → news o liquidación en cadena. Acción: revisa 
  el strip gravity para ver dirección (¿liquidaciones verdes o rojas?) y decide trailing/cierre.
                                                                                                                         
  Carretera (derecha)                                       

  Régimen del mercado + leverage sugerido.                                                                               
  
  - ═══ Autopista (TRENDING, 10×) → tendencia clara, momentum unidireccional. Apalancamiento mayor justificado porque las
   velas no se contradicen. Estrategia: trend-following, trailing wide.
  - ∿∿∿ Curvas (RANGING, 3×) → mercado lateral, rebotes en S/R. Apalancamiento medio. Estrategia: comprar abajo, vender  
  arriba; SL fuera del rango.                                                                                            
  - ≈≈≈ Terracería (VOLATILE, 1×) → choppy, mecha arriba y abajo. Apalancamiento mínimo. Estrategia: no operar o tamaño
  muy chico.                                                                                                             
  - ▪▪▪ Atasco (ACCUMULATION, espera) → OI creciendo, precio sin moverse — alguien está cargando. Estrategia: no entres,
  espera el breakout.                                                                                                    
                                                            
  Override defensivo: si estás en Autopista pero el velocímetro pasa 85, el sistema degrada a Terracería automáticamente 
  (autopista a 200 km/h con neblina = peligro).             
                                                                                                                         
  ---                                                       
  Lectura combinada — los 3 instrumentos juntos
                                                                                                                         
  ┌────────────┬──────────┬──────────────────────┬───────────────────────────────────────────────┐
  │  Pressure  │ Velocity │         Road         │                    Lectura                    │                       
  ├────────────┼──────────┼──────────────────────┼───────────────────────────────────────────────┤                       
  │ 30 NEUTRAL │ 20       │ Curvas               │ Mercado dormido. Scalping si acaso.           │
  ├────────────┼──────────┼──────────────────────┼───────────────────────────────────────────────┤                       
  │ 75 GREED   │ 60       │ Autopista            │ Tendencia alcista sana. Mantén LONG, trail.   │                       
  ├────────────┼──────────┼──────────────────────┼───────────────────────────────────────────────┤                       
  │ 90 GREED   │ 95 rojo  │ Autopista→Terracería │ Euforia extrema. Cierra parcial, BE agresivo. │                       
  ├────────────┼──────────┼──────────────────────┼───────────────────────────────────────────────┤                       
  │ 80 FEAR    │ 70       │ Autopista bajista    │ SHORT con convicción.                         │
  ├────────────┼──────────┼──────────────────────┼───────────────────────────────────────────────┤                       
  │ 50 FEAR    │ 90       │ Terracería           │ Pánico errático. Fuera o tamaño mínimo.       │
  ├────────────┼──────────┼──────────────────────┼───────────────────────────────────────────────┤                       
  │ 20 NEUTRAL │ 15       │ Atasco               │ Acumulación. Espera el breakout, no fuerces.  │
  └────────────┴──────────┴──────────────────────┴───────────────────────────────────────────────┘                       
                                                            
  La idea geométrica: la carretera te dice qué tipo de pista; el velocímetro, si vas demasiado rápido para esa pista; el 
  tacómetro, qué tan tensa está la multitud. Tu posición sobre el progress bar es dónde estás en el tramo. El strip
  gravity es el mapa del terreno (dónde hay árboles=stops, dónde hay precipicios=LVN).   
