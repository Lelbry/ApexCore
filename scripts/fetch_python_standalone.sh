#!/usr/bin/env bash
# Скачивает relocatable CPython (python-build-standalone) для бандла в .deb.
#
# Зачем: системный venv в .deb (python3 -m venv) был НЕ переносим —
#   (1) shebang консольных скриптов указывал на build-staging путь
#       (debian/apexcore/...), которого на чужой машине нет → «bad interpreter»;
#   (2) bin/python3 → /usr/bin/python3 → жёсткая привязка к системному Python 3.11.
# Standalone-сборка самодостаточна (свой интерпретатор + stdlib) и релоцируема:
# распаковывается в /opt/apexcore/.venv и работает на любой Astra/Debian без
# системного python вообще.
#
# Источник: github.com/astral-sh/python-build-standalone, вариант install_only
# (релоцируемый, gnu/glibc). Кешируется — повторный запуск ничего не качает.
#
# Override через env: APEXCORE_PYSTANDALONE_VER / _TAG / _URL.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$ROOT/packaging/astra/python-standalone"

PY_VER="${APEXCORE_PYSTANDALONE_VER:-3.11.9}"
PBS_TAG="${APEXCORE_PYSTANDALONE_TAG:-20240814}"
ASSET="cpython-${PY_VER}+${PBS_TAG}-x86_64-unknown-linux-gnu-install_only.tar.gz"
URL="${APEXCORE_PYSTANDALONE_URL:-https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}/${ASSET}}"

if [ -x "$DEST/python/bin/python3" ]; then
    echo "[fetch_python_standalone] уже есть: $DEST/python ($("$DEST/python/bin/python3" --version 2>&1))"
    exit 0
fi

mkdir -p "$DEST"
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

echo "[fetch_python_standalone] download: $URL"
if command -v curl >/dev/null 2>&1; then
    curl -fSL --retry 3 "$URL" -o "$tmp"
elif command -v wget >/dev/null 2>&1; then
    wget -O "$tmp" "$URL"
else
    echo "[!] нужен curl или wget для загрузки CPython standalone" >&2
    exit 1
fi

# install_only-архив распаковывается в каталог ./python/
rm -rf "$DEST/python"
tar -xzf "$tmp" -C "$DEST"

if [ ! -x "$DEST/python/bin/python3" ]; then
    echo "[!] после распаковки нет $DEST/python/bin/python3 — проверьте URL/версию" >&2
    exit 1
fi
echo "[fetch_python_standalone] OK → $DEST/python ($("$DEST/python/bin/python3" --version 2>&1))"
