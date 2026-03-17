"""
main.py — QTS · Quantum Trading System
───────────────────────────────────────
Punto de entrada. Inicializa la base de datos y arranca el dashboard.

Uso:
    python main.py
"""
from core.db import initialize_db
from interface.terminal import TradingApp


def main() -> None:
    initialize_db()
    app = TradingApp()
    app.run()


if __name__ == "__main__":
    main()
