#!/usr/bin/env bash
# Сборка self-contained .deb пакета ApexCore для Astra Linux 1.8+.
#
# Требования (на build-машине):
#   apt install -y debhelper python3 python3-venv python3-pip imagemagick \
#                  build-essential devscripts
#
# Pipeline (см. plan-файл):
#   [1/6]  Brand assets (build_branding.sh)
#   [2/6]  Версия из pyproject.toml (sync_version.sh)
#   [3/6]  pip download wheels (offline-install заготовка)
#   [4/6]  Обновить debian/changelog (dch)
#   [5/6]  dpkg-buildpackage -us -uc -b
#   [6/6]  Перенос .deb в dist/
#
# Запуск: bash new-app/scripts/build_astra.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

ASTRA_DIR="$ROOT/packaging/astra"
WHEELS_DIR="$ASTRA_DIR/wheels"
DIST_DIR="$ROOT/dist"

# Sanity check инструментов
for tool in python3 dpkg-buildpackage debhelper magick imagemagick; do
    # debhelper и imagemagick проверяем через apt-метаданные
    case "$tool" in
        debhelper)
            dpkg -l debhelper 2>/dev/null | grep -q '^ii' || \
                { echo "[!] Установите debhelper: sudo apt install debhelper" >&2; exit 1; }
            ;;
        imagemagick)
            command -v magick >/dev/null 2>&1 || command -v convert >/dev/null 2>&1 || \
                { echo "[!] Установите imagemagick: sudo apt install imagemagick" >&2; exit 1; }
            ;;
        magick)
            : # покрыто выше через imagemagick
            ;;
        *)
            command -v "$tool" >/dev/null 2>&1 || \
                { echo "[!] Не найден: $tool" >&2; exit 1; }
            ;;
    esac
done

echo "[1/6] Branding assets..."
bash scripts/build_branding.sh

echo "[2/6] Версия из pyproject.toml..."
# shellcheck disable=SC1091
source scripts/sync_version.sh
VERSION="${APEXCORE_VERSION:-0.0.0}"

# Копируем build/branding в packaging/astra/build/branding (rules его берёт оттуда)
mkdir -p "$ASTRA_DIR/build"
rm -rf "$ASTRA_DIR/build/branding"
cp -r "$ROOT/build/branding" "$ASTRA_DIR/build/branding"

echo "[3/6] Wheels offline (~120-200 МБ)..."
mkdir -p "$WHEELS_DIR"
rm -rf "$WHEELS_DIR"/*

# Список зависимостей из pyproject.toml + extras [fast, webui]
REQS="$(python3 - <<'EOF'
import tomllib, pathlib
data = tomllib.loads(pathlib.Path("pyproject.toml").read_bytes().decode())
deps = list(data["project"]["dependencies"])
deps += data["project"]["optional-dependencies"].get("fast", [])
deps += data["project"]["optional-dependencies"].get("webui", [])
print('\n'.join(deps))
EOF
)"

echo "$REQS" | python3 -m pip download \
    -d "$WHEELS_DIR" \
    --only-binary=:all: \
    --platform manylinux2014_x86_64 \
    --python-version 3.11 \
    -r /dev/stdin

# Сам apexcore как wheel (build из src/).
# ВАЖНО: setuptools кеширует build/lib/ между сборками и не обновляет
# изменённые файлы надёжно (особенно после `git reset --hard`, который
# сбрасывает рабочее дерево, но build/lib — untracked и переживает reset).
# Из-за этого wheel молча уезжал со СТАРЫМ server.py: новые файлы
# добавлялись, а изменённые — нет (см. docs/Astra/problems_fixes.md #21).
# Чистим build artefacts + pip-cache перед сборкой, чтобы wheel всегда
# собирался из актуального src/.
rm -rf "$ROOT/build/lib" "$ROOT/build/bdist."* "$ROOT/src"/*.egg-info
python3 -m pip wheel . --no-deps --no-cache-dir --wheel-dir "$WHEELS_DIR"

echo "[3b/6] CPython standalone (relocatable) для бандла..."
# Релоцируемый CPython вместо venv от системного python — пакет переносим
# на любую машину (см. debian/rules). rules берёт его из
# packaging/astra/python-standalone/python.
bash scripts/fetch_python_standalone.sh

echo "[4/6] debian/changelog..."
# Если в pyproject новая версия — добавляем запись через dch.
cd "$ASTRA_DIR"
if ! head -1 debian/changelog | grep -q "($VERSION)"; then
    if command -v dch >/dev/null 2>&1; then
        DEBEMAIL="${DEBEMAIL:-noreply@example.local}" \
        DEBFULLNAME="${DEBFULLNAME:-lelbry}" \
            dch -v "$VERSION" "release $VERSION"
    else
        echo "[!] dch (devscripts) не установлен — пропускаю авто-обновление changelog." >&2
    fi
fi

echo "[5/6] dpkg-buildpackage..."
dpkg-buildpackage -us -uc -b

echo "[6/6] Перенос .deb в dist/..."
mkdir -p "$DIST_DIR"
mv "$ROOT/packaging/"*.deb "$DIST_DIR/" 2>/dev/null || true
mv "$ROOT"/*.deb "$DIST_DIR/" 2>/dev/null || true

echo ""
echo "[OK] Сборка завершена. Результаты:"
ls -la "$DIST_DIR"/apexcore_*.deb 2>/dev/null || echo "  (не найдено)"

echo ""
echo "Установка:"
echo "  sudo apt install ./dist/apexcore_${VERSION}_amd64.deb"
echo ""
echo "Тест в Docker:"
echo "  docker run --rm -it -v \$PWD/dist:/dist astralinuxteam/orel:1.8 bash"
echo "  > apt update && apt install -y /dist/apexcore_${VERSION}_amd64.deb"
echo "  > apexcore setup --no-browser"
