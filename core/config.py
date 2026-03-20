from __future__ import annotations

from typing import Any, Dict, List

from pydantic_settings import BaseSettings, SettingsConfigDict


# ─── Niveles de velocidad ──────────────────────────────────────────────────────
# Define qué klines y timeframes usa cada modo.
#   fast      = kline principal para ATR/EMAs (base de la señal)
#   slow      = kline de contexto (EMA50/200, RSI lento)
#   tf_label  = etiqueta mostrada en UI

SPEED_CONFIGS: Dict[str, Dict[str, Any]] = {
    "nano": {
        "fast": "1",   "fast_limit": 100,
        "slow": "3",   "slow_limit": 100,
        "tf_label": "1m",
        "label": "NANO",
        "desc": "Trades <90s · SL/TP ultrajustado",
        "be_hold_s": 2,       # 2 s para confirmar breakeven
        "atr_sl_mult": 0.4,   # SL ultra-corto (R:R 2.5)
        "atr_tp_mult": 1.0,   # TP mínimo
    },
    "scalp": {
        "fast": "1",   "fast_limit": 100,
        "slow": "5",   "slow_limit": 100,
        "tf_label": "1m",
        "label": "SCALP",
        "desc": "Trades 1–5 min · ATR 1m",
        "be_hold_s": 5,    # 5 s para confirmar breakeven
    },
    "fast": {
        "fast": "5",   "fast_limit": 80,
        "slow": "15",  "slow_limit": 80,
        "tf_label": "5m",
        "label": "FAST",
        "desc": "Trades 5–20 min · ATR 5m",
        "be_hold_s": 10,   # 10 s
    },
    "standard": {
        "fast": "15",  "fast_limit": 80,
        "slow": "60",  "slow_limit": 220,
        "tf_label": "15m",
        "label": "STANDARD",
        "desc": "Trades 20–120 min · ATR 15m",
        "be_hold_s": 30,   # 30 s (valor original)
    },
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Bybit
    bybit_api_key: str = ""
    bybit_api_secret: str = ""
    bybit_testnet: bool = False

    # ── AI Strategy Agent ──────────────────────────────────────────────────────
    # Cuando ai_strategy_mode=True, el agente IA genera las estrategias en tiempo real.
    ai_strategy_mode:   bool = False
    ai_provider:        str  = "openai"   # "openai" | "ollama" | "compatible"

    # OpenAI (proveedor por defecto)
    openai_api_key:     str  = ""
    openai_model:       str  = "gpt-4o"   # "gpt-4o" | "gpt-4o-mini" | "o3-mini"

    # Ollama — LLM local
    ollama_host:        str  = "http://localhost:11434"
    ollama_model:       str  = "llama3.2"

    # Compatible OpenAI (Groq, Together, Mistral, Perplexity, etc.)
    ai_compat_url:      str  = ""   # ej: https://api.groq.com/openai/v1
    ai_compat_key:      str  = ""
    ai_compat_model:    str  = ""   # ej: llama-3.1-8b-instant

    # Agente IA — parámetros internos
    ai_min_interval_s: int   = 60    # segundos mínimos entre llamadas
    ai_top_symbols:    int   = 3     # cuántos candidatos enviar al agente
    ai_min_score:      int   = 70    # score mínimo para ser candidato
    ai_min_atr_pct:    float = 0.40  # ATR mínimo requerido (%)
    ai_max_latency_s:  float = 45.0  # latencia máxima aceptable para la propuesta

    # Mercado — carga dinámica desde Bybit o fallback CSV manual
    # Si auto_load_symbols=True, se reemplaza al iniciar con los top-N por volumen.
    symbols: str = "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT"   # fallback mínimo
    default_symbol: str = "BTCUSDT"

    # Carga dinámica de símbolos desde Bybit
    auto_load_symbols: bool = True    # descarga top-N por volumen 24h al iniciar
    max_symbols:       int  = 100     # cuántos pares monitorear (top por volumen)

    # Blacklist — pares excluidos del scan (manual + auto)
    symbol_blacklist:       str  = ""    # CSV de pares excluidos, ej: XRPUSDT,BONKUSDT
    auto_blacklist_enabled: bool = True  # excluir par tras N pérdidas seguidas
    auto_blacklist_losses:  int  = 3     # número de pérdidas consecutivas para excluir

    # WebSocket
    ws_reconnect_delay: float = 5.0

    # Base de datos
    db_path: str = "storage/trading.duckdb"

    # Inteligencia de mercado (Fase 1)
    candle_interval: int = 60   # segundos por vela de CVD (60 = 1 min)

    # Risk Management (Fase 5)
    max_daily_loss_pct:      float = 2.0
    max_trades_per_day:      int   = 10
    circuit_breaker_enabled: bool  = True

    # Protección activa (breakeven / trailing)
    breakeven_pct:    float = 40.0   # % del TP distance para activar breakeven
    profit_lock_pct:  float = 60.0   # % para profit lock
    trailing_pct:     float = 70.0   # % para trailing stop
    be_hold_time_s:   int   = 30     # segundos que el precio debe mantenerse en BE

    # Estrategia
    min_scan_score:   int   = 70     # score mínimo para generar propuesta
    min_rr:           float = 2.5    # R:R mínimo aceptado en pre-flight (subido de 1.3)
    scan_interval_s:  int   = 30     # segundos entre scans automáticos
    speed_level:      str   = "standard"  # "nano" | "scalp" | "fast" | "standard"

    # ── Salidas de protección (Fase 1) ──────────────────────────────────────
    # Weak Exit: cierra si el setup se debilita antes de producir ganancia
    weak_exit_enabled:       bool  = True
    weak_exit_window_s:      int   = 120   # solo activo en los primeros N segundos del trade
    weak_exit_min_score:     int   = 4     # puntuación de debilidad (0-6) para activar salida
    weak_exit_min_elapsed_s: int   = 30    # segundos mínimos antes de poder disparar (evita t=0)
    weak_exit_min_sl_pct:    float = 8.0   # precio debe haber avanzado X% hacia SL antes de disparar

    # Time Stop: cierra si no hay progreso suficiente en N segundos
    time_stop_enabled:  bool  = True
    time_stop_window_s: int   = 300    # ventana de tiempo (5 min; 180s era demasiado corto)
    time_stop_min_pct:  float = 15.0   # % mínimo de avance hacia TP requerido

    # Cooldown por símbolo+dirección tras weak_exit o time_stop
    symbol_cooldown_s: int = 300   # segundos sin re-entrar mismo símbolo/dirección

    # Partial Lock: escalón intermedio que asegura ganancia real antes del profit lock
    partial_lock_enabled: bool  = True
    partial_lock_at_pct:  float = 50.0  # % de progreso al TP para activar
    partial_lock_frac:    float = 0.55  # SL = entry ± sl_dist × frac (bloquea 55% del riesgo)

    # Flag interno para evitar guardar durante la carga inicial de Pydantic
    _initialized: bool = False

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Forzamos inicialización manual del flag privado en Pydantic v2
        object.__setattr__(self, "_initialized", True)

    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if getattr(self, "_initialized", False) and not name.startswith("_"):
            self.save()

    def save(self) -> None:
        """Guarda la configuración actual en el archivo .env preservando comentarios."""
        import os
        env_path = ".env"
        # Exportar datos (campos privados se excluyen por defecto en Pydantic v2)
        current_data = self.model_dump()
        
        lines = []
        if os.path.exists(env_path):
            try:
                with open(env_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
            except: pass
        
        new_lines = []
        seen_keys = set()
        
        # 1. Actualizar líneas existentes y preservar comentarios
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                new_lines.append(line)
                continue
            
            if "=" in stripped:
                key = stripped.split("=")[0].upper()
                field_name = key.lower()
                if field_name in current_data:
                    val = current_data[field_name]
                    if isinstance(val, bool): val = str(val).lower()
                    new_lines.append(f"{key}={val}\n")
                    seen_keys.add(field_name)
                else:
                    new_lines.append(line)
            else:
                new_lines.append(line)
        
        # 2. Añadir campos nuevos que no estaban en el .env original
        for field, val in current_data.items():
            if field not in seen_keys:
                key = field.upper()
                if isinstance(val, bool): val = str(val).lower()
                new_lines.append(f"{key}={val}\n")
        
        try:
            # Escribir de forma atómica si es posible (simple write aquí)
            with open(env_path, "w", encoding="utf-8") as f:
                f.writelines(new_lines)
        except Exception as e:
            # Usar print o logging directo para evitar ciclos
            print(f"Settings: error al persistir .env: {e}")

    @property
    def speed_cfg(self) -> Dict[str, Any]:
        return SPEED_CONFIGS.get(self.speed_level, SPEED_CONFIGS["standard"])

    @property
    def fast_kline(self) -> str:
        return self.speed_cfg["fast"]

    @property
    def slow_kline(self) -> str:
        return self.speed_cfg["slow"]

    @property
    def effective_be_hold_s(self) -> int:
        """Segundos mínimos en umbral de BE antes de moverlo. Adapta por speed level."""
        return self.speed_cfg.get("be_hold_s", self.be_hold_time_s)

    # Filtro horario (UTC) — no operar fuera de este rango
    trading_hours_enabled: bool = False   # desactivado por defecto
    trading_hours_start:   int  = 7       # hora UTC de inicio (7 = 07:00 UTC)
    trading_hours_end:     int  = 23      # hora UTC de fin   (23 = 23:00 UTC)

    # Paper trading
    paper_trading:  bool  = False
    paper_balance:  float = 10_000.0

    @property
    def symbol_list(self) -> List[str]:
        """Lista de símbolos parseada desde el CSV (excluye blacklist)."""
        bl = self.blacklist_set
        return [s.strip().upper() for s in self.symbols.split(",")
                if s.strip() and s.strip().upper() not in bl]

    @property
    def blacklist_set(self) -> set:
        """Conjunto de símbolos en la blacklist (para lookup O(1))."""
        return {s.strip().upper() for s in self.symbol_blacklist.split(",") if s.strip()}


settings = Settings()
