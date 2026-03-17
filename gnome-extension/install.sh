#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# install.sh  —  Instala la extensión QTS en GNOME Shell
#
# Uso:
#   cd ~/Projects/trading/gnome-extension
#   bash install.sh
#
# Requiere GNOME Shell 45+ (Fedora 39+, Ubuntu 23.10+).
# ──────────────────────────────────────────────────────────────────────────────

set -euo pipefail

UUID="qts-indicator@qts.trading"
DEST="$HOME/.local/share/gnome-shell/extensions/$UUID"
SRC="$(cd "$(dirname "$0")" && pwd)"

echo "==> Instalando extensión QTS en: $DEST"

# Crear directorio destino
mkdir -p "$DEST"

# Copiar archivos
cp "$SRC/metadata.json"    "$DEST/metadata.json"
cp "$SRC/extension.js"     "$DEST/extension.js"
cp "$SRC/stylesheet.css"   "$DEST/stylesheet.css"

echo "==> Archivos copiados."

# Habilitar la extensión
echo "==> Habilitando extensión..."
if gnome-extensions enable "$UUID" 2>/dev/null; then
    echo "==> Extensión habilitada."
else
    echo "NOTA: No se pudo habilitar automáticamente."
    echo "      Usa: gnome-extensions enable $UUID"
    echo "      O ejecuta desde la misma sesión de escritorio."
fi

echo ""
echo "══════════════════════════════════════════════════════════════"
echo "  INSTALADO: $UUID"
echo ""
echo "  Si el panel no aparece inmediatamente, recarga GNOME Shell:"
echo "    Alt+F2 → r → Enter"
echo "  (En Wayland usa: gnome-shell --replace & )"
echo ""
echo "  Para desinstalar:"
echo "    gnome-extensions disable $UUID"
echo "    rm -rf $DEST"
echo "══════════════════════════════════════════════════════════════"
