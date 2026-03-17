from __future__ import annotations

from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


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
        "DOTUSDT,NEARUSDT,ATOMUSDT,FTMUSDT,INJUSDT"
    )
    default_symbol: str = "XRPUSDT"

    # WebSocket
    ws_reconnect_delay: float = 5.0

    # Base de datos
    db_path: str = "storage/trading.duckdb"

    # Inteligencia de mercado (Fase 1)
    candle_interval: int = 60   # segundos por vela de CVD (60 = 1 min)

    # Risk Management (Fase 5)
    max_daily_loss_pct: float = 2.0
    max_trades_per_day: int = 10

    @property
    def symbol_list(self) -> List[str]:
        """Lista de símbolos parseada desde el CSV."""
        return [s.strip().upper() for s in self.symbols.split(",") if s.strip()]


settings = Settings()
