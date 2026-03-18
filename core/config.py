from __future__ import annotations

from typing import Any, Dict, List

from pydantic_settings import BaseSettings, SettingsConfigDict


# ─── Niveles de velocidad ──────────────────────────────────────────────────────
# Define qué klines y timeframes usa cada modo.
#   fast      = kline principal para ATR/EMAs (base de la señal)
#   slow      = kline de contexto (EMA50/200, RSI lento)
#   tf_label  = etiqueta mostrada en UI

SPEED_CONFIGS: Dict[str, Dict[str, Any]] = {
    "scalp": {
        "fast": "1",   "fast_limit": 100,
        "slow": "5",   "slow_limit": 100,
        "tf_label": "1m",
        "label": "SCALP",
        "desc": "Trades 1–5 min · ATR 1m",
    },
    "fast": {
        "fast": "5",   "fast_limit": 80,
        "slow": "15",  "slow_limit": 80,
        "tf_label": "5m",
        "label": "FAST",
        "desc": "Trades 5–20 min · ATR 5m",
    },
    "standard": {
        "fast": "15",  "fast_limit": 80,
        "slow": "60",  "slow_limit": 220,
        "tf_label": "15m",
        "label": "STANDARD",
        "desc": "Trades 20–120 min · ATR 15m",
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

    # Mercado — str para evitar que pydantic-settings intente parsear como JSON
    symbols: str = (
        "XRPUSDT,SOLUSDT,BTCUSDT,ETHUSDT,XLMUSDT,"
        "DOGEUSDT,ADAUSDT,LTCUSDT,AVAXUSDT,LINKUSDT,"
        "DOTUSDT,NEARUSDT,ATOMUSDT,FTMUSDT,INJUSDT,"
        "BNBUSDT,TRXUSDT,SUIUSDT,APTUSDT,ARBUSDT,"
        "OPUSDT,MATICUSDT,UNIUSDT,SEIUSDT,TIAUSDT,"
        "WLDUSDT,FETUSDT,RENDERUSDT,STXUSDT,RUNEUSDT,"
        "AAVEUSDT,CRVUSDT,GMXUSDT,JUPUSDT,PYTHUSDT,"
        "WIFUSDT,BONKUSDT,PEPEUSDT,FLOKIUSDT,LDOUSDT,"
        "EIGENUSDT,ENAUSDT,REZUSDT,SAGAUSDT,ALTUSDT"
    )
    default_symbol: str = "XRPUSDT"

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
    min_scan_score:   int   = 55     # score mínimo para generar propuesta
    scan_interval_s:  int   = 30     # segundos entre scans automáticos
    speed_level:      str   = "standard"  # "scalp" | "fast" | "standard"

    @property
    def speed_cfg(self) -> Dict[str, Any]:
        return SPEED_CONFIGS.get(self.speed_level, SPEED_CONFIGS["standard"])

    @property
    def fast_kline(self) -> str:
        return self.speed_cfg["fast"]

    @property
    def slow_kline(self) -> str:
        return self.speed_cfg["slow"]

    # Paper trading
    paper_trading:  bool  = False
    paper_balance:  float = 10_000.0

    @property
    def symbol_list(self) -> List[str]:
        """Lista de símbolos parseada desde el CSV."""
        return [s.strip().upper() for s in self.symbols.split(",") if s.strip()]


settings = Settings()
