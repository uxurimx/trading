"""
main_terminal.py — QTS · Quantum Trading System
────────────────────────────────────────────────
Punto de entrada TUI. Inicializa la base de datos y arranca el dashboard.

Uso:
    python main_terminal.py
"""
from core.db import initialize_db, save_monitored_symbols, load_monitored_symbols
from core.config import settings
from core.executor import BybitExecutor
from interface.terminal import TradingApp


def _load_symbols() -> None:
    """Carga el universo de símbolos: Bybit → DB → .env."""
    import logging
    _l = logging.getLogger("qts.startup")
    bl = settings.blacklist_set

    if settings.auto_load_symbols:
        fetched = BybitExecutor.fetch_top_usdt_symbols_sync(
            limit   = settings.max_symbols,
            testnet = settings.bybit_testnet,
        )
        if fetched:
            filtered = [(sym, vol) for sym, vol in fetched if sym not in bl]
            save_monitored_symbols(filtered)
            settings.symbols = ",".join(sym for sym, _ in filtered)
            _l.info("Símbolos cargados desde Bybit: %d pares", len(filtered))
            return

        cached = [s for s in load_monitored_symbols() if s not in bl]
        if cached:
            settings.symbols = ",".join(cached)
            _l.warning("Bybit no disponible — cache DB: %d pares", len(cached))
            return

        _l.warning("Sin Bybit ni cache DB — usando .env")


def main() -> None:
    initialize_db()
    _load_symbols()
    app = TradingApp()
    app.run()


if __name__ == "__main__":
    main()
