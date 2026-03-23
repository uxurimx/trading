#!/usr/bin/env python3
"""
main.py — QTS · Quantum Trading System
───────────────────────────────────────
Lanza la ventana nativa GTK4 + libadwaita para GNOME/Fedora.

Alternativa terminal:   python main_terminal.py
"""
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-20s  %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)

try:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gtk, Adw  # noqa: F401
except (ImportError, ValueError):
    print("GTK4 / libadwaita no encontrado.")
    print("Instala con:  sudo dnf install python3-gobject gtk4 libadwaita")
    print("Alternativa:  python main_terminal.py")
    sys.exit(1)

from core.db import initialize_db
from interface.gtk_app import run


def main() -> None:
    try:
        initialize_db()
    except Exception as e:
        # El error más común: otro proceso Python (VS Code / Pylance) tiene el lock.
        # El usuario debe cerrar el editor o esperar unos segundos y reintentar.
        print(f"\n[QTS] ERROR al inicializar la base de datos: {e}")
        if "lock" in str(e).lower() or "conflict" in str(e).lower():
            print("[QTS] Otro proceso tiene el lock de la DB (probablemente VS Code/Pylance).")
            print("[QTS] Cierra VS Code o espera unos segundos y vuelve a arrancar.\n")
        sys.exit(1)
    run()


if __name__ == "__main__":
    main()
