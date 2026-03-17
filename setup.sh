#!/bin/bash
set -e

echo "⚡ QTS — Quantum Trading System"
echo "================================"
echo ""

# Verificar Python
if ! command -v python3 &> /dev/null; then
    echo "✗ Python3 no encontrado. Instala con: sudo dnf install python3"
    exit 1
fi

PYTHON_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "✓ Python $PYTHON_VERSION encontrado"

# Verificar GTK4 / PyGObject (requerido para la ventana nativa)
if ! python3 -c "import gi" 2>/dev/null; then
    echo "⚠  PyGObject no encontrado. Instala con:"
    echo "   sudo dnf install python3-gobject python3-gobject-devel"
    exit 1
fi
echo "✓ PyGObject (GTK4) disponible"

# Entorno virtual con acceso a paquetes del sistema (para gi/GTK4)
if [ ! -d ".venv" ]; then
    python3 -m venv --system-site-packages .venv
    echo "✓ Entorno virtual creado (con system-site-packages)"
else
    echo "✓ Entorno virtual existente"
fi

source .venv/bin/activate

# Dependencias
pip install --upgrade pip -q
pip install -r requirements.txt -q
echo "✓ Dependencias instaladas"

# Directorios
mkdir -p storage
echo "✓ Directorios creados"

# .env
if [ ! -f .env ]; then
    cp .env.example .env
    echo ""
    echo "⚠  Archivo .env creado — EDITA con tus API keys antes de continuar:"
    echo "   nano .env"
else
    echo "✓ .env ya configurado"
fi

echo ""
echo "═══════════════════════════════"
echo "Para iniciar el sistema:"
echo ""
echo "  source .venv/bin/activate"
echo "  python main.py"
echo "═══════════════════════════════"
