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
    try:
        initialize_db()
    except Exception as e:
        print(f"\n[QTS] ERROR al inicializar la base de datos: {e}")
        if "lock" in str(e).lower() or "conflict" in str(e).lower():
            print("[QTS] Otro proceso tiene el lock de la DB (probablemente VS Code/Pylance).")
            print("[QTS] Cierra VS Code o espera unos segundos y vuelve a arrancar.\n")
        import sys; sys.exit(1)
    try:
        from tools.changelog_server import start_background as _start_changelog
        _start_changelog(open_browser=False)
    except Exception:
        pass  # changelog server es opcional, no debe bloquear el sistema
    _load_symbols()
    app = TradingApp()
    app.run()


if __name__ == "__main__":
    main()
