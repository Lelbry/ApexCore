#!/usr/bin/env bash
# Brand asset pipeline (Astra Linux / Debian).
#
# Из packaging/branding/source/apex-logo.png генерирует все производные
# в build/branding/. Требует ImageMagick (`magick` или `convert`) в PATH.
#
# Идемпотентен: пересобирает только если source новее target.
#
# Запуск: bash new-app/scripts/build_branding.sh
#
# Используется в scripts/build_astra.sh (шаг [1/6]).

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

SOURCE="packaging/branding/source/apex-logo.png"
BUILD_DIR="build/branding"

if [[ ! -f "$SOURCE" ]]; then
    echo "[!] Source не найден: $SOURCE" >&2
    echo "    Положите PNG 512×512 RGBA в эту папку." >&2
    exit 1
fi

# ImageMagick: пробуем `magick`, потом старый `convert`
MAGICK=""
if command -v magick >/dev/null 2>&1; then
    MAGICK="magick"
elif command -v convert >/dev/null 2>&1; then
    MAGICK="convert"
else
    echo "[!] ImageMagick не найден в PATH." >&2
    echo "    Установите: sudo apt install imagemagick" >&2
    exit 2
fi

mkdir -p "$BUILD_DIR"

newer_than() {
    # $1 = target, $2 = source. Возвращает 0 если target отсутствует или
    # source новее target.
    [[ ! -f "$1" ]] && return 0
    [[ "$2" -nt "$1" ]] && return 0
    return 1
}

SIZES=(256 128 80 64 52 48 32)
for size in "${SIZES[@]}"; do
    OUT="$BUILD_DIR/apex-logo-$size.png"
    if newer_than "$OUT" "$SOURCE"; then
        echo "  → $OUT (${size}×${size})"
        $MAGICK "$SOURCE" -resize "${size}x${size}" -strip "$OUT"
    fi
done

# Multi-resolution ICO (для совместимости — Astra .deb он не требует, но
# Windows installer.iss и apexcore.spec ссылаются на тот же файл)
ICO="$BUILD_DIR/apex-logo.ico"
if newer_than "$ICO" "$SOURCE"; then
    echo "  → $ICO (multi-resolution 16/32/48/256)"
    $MAGICK "$SOURCE" -define icon:auto-resize='256,48,32,16' "$ICO"
fi

echo ""
echo "[OK] Branding assets готовы в $BUILD_DIR/"
ls -la "$BUILD_DIR/"
