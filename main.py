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
    initialize_db()
    run()


if __name__ == "__main__":
    main()
