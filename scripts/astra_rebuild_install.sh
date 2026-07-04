#!/usr/bin/env bash
# Astra Linux: full rebuild + reinstall цикл для итерационной разработки.
#
# Делает за один заклинатель то, что вручную набирается 6+ команд:
#   1. git fetch + reset --hard <ref> (по умолчанию origin/dev)
#   2. kill running apexcore webui (иначе .deb upgrade молча оставит старый код в памяти)
#   3. clean debian/ build artefacts
#   4. bash scripts/build_astra.sh
#   5. cp .deb → /tmp/ (workaround для _apt sandbox warning, см. problems_fixes #7)
#   6. sudo apt install --reinstall
#   7. sanity: версия пакета + 1 строка из установленного .py (доказательство что код приехал)
#
# Использование:
#   bash new-app/scripts/astra_rebuild_install.sh                  # ← origin/dev (typical dev loop)
#   bash new-app/scripts/astra_rebuild_install.sh v0.8.7           # ← конкретный тег
#   bash new-app/scripts/astra_rebuild_install.sh abc1234          # ← конкретный коммит
#   bash new-app/scripts/astra_rebuild_install.sh --no-reset       # ← без git pull (текущий рабочий код)
#
# Этот скрипт НЕ запускает apexcore doctor / webui — это контроль пользователя
# (запуск может быть от обычного юзера, от sudo, с разными портами и т.п.).

set -euo pipefail

REF="${1:-origin/dev}"
DO_RESET=1
if [ "$REF" = "--no-reset" ]; then
    DO_RESET=0
    REF=""
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

# Цветной префикс для шагов, но без exit-кодов от echo.
step() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }

if [ "$DO_RESET" -eq 1 ]; then
    step "[1/7] git fetch origin --tags + reset --hard $REF"
    git fetch origin --tags --force
    git reset --hard "$REF"
    git log --oneline -3
else
    step "[1/7] git: пропуск (--no-reset)"
fi

step "[2/7] kill running apexcore (webui/wizard) — чтобы reinstall не оставил старый код в памяти"
if pgrep -f "apexcore (webui|setup)" >/dev/null 2>&1; then
    pkill -f "apexcore (webui|setup)" || true
    sleep 1
    echo "  → завершены"
else
    echo "  → нет активных процессов"
fi

step "[3/7] clean debian/ artefacts"
ASTRA_DEBIAN="$ROOT/new-app/packaging/astra/debian"
if [ -d "$ASTRA_DEBIAN" ]; then
    sudo rm -rf \
        "$ASTRA_DEBIAN/apexcore" \
        "$ASTRA_DEBIAN/.debhelper" \
        2>/dev/null || true
    sudo rm -f \
        "$ASTRA_DEBIAN/files" \
        "$ASTRA_DEBIAN"/*.substvars \
        2>/dev/null || true
    echo "  → очищено"
else
    echo "  → $ASTRA_DEBIAN не существует — пропускаю"
fi

step "[4/7] bash new-app/scripts/build_astra.sh"
export DEB_BUILD_OPTIONS="nostrip nodwz noddebs"
bash new-app/scripts/build_astra.sh

# Версия из pyproject.toml — для подбора правильного имени .deb.
VERSION="$(python3 - <<'EOF'
import tomllib, pathlib
print(tomllib.loads(pathlib.Path("new-app/pyproject.toml").read_bytes().decode())["project"]["version"])
EOF
)"
DEB_PATH="$ROOT/new-app/dist/apexcore_${VERSION}_amd64.deb"
if [ ! -f "$DEB_PATH" ]; then
    echo "  [!] .deb не найден: $DEB_PATH" >&2
    ls -la "$ROOT/new-app/dist/" 2>/dev/null || true
    exit 1
fi

step "[5/7] cp .deb → /tmp/ (избегаем _apt sandbox warning)"
sudo cp "$DEB_PATH" /tmp/
echo "  → /tmp/$(basename "$DEB_PATH")"

step "[6/7] sudo apt install --reinstall"
sudo apt install --reinstall -y "/tmp/$(basename "$DEB_PATH")"

step "[7/7] sanity: версия + проверка свежего кода в установленном пакете"
dpkg -l apexcore | tail -1
INSTALLED_PY="/opt/apexcore/.venv/lib/python3.11/site-packages/apexcore/infrastructure/adapters/linux.py"
if [ -f "$INSTALLED_PY" ]; then
    if grep -q "_HWMON_CPU_CHIPS" "$INSTALLED_PY"; then
        echo "  ✓ _HWMON_CPU_CHIPS присутствует в установленном linux.py (CPU/GPU/disk pipeline актуален)"
    else
        echo "  [!] _HWMON_CPU_CHIPS ОТСУТСТВУЕТ в установленном linux.py — проверьте сборку!" >&2
    fi
else
    echo "  [!] $INSTALLED_PY не найден" >&2
fi
STATIC_INDEX="/opt/apexcore/.venv/lib/python3.11/site-packages/apexcore/interfaces/webui/static/index.html"
if [ -f "$STATIC_INDEX" ]; then
    echo "  ✓ WebUI static на месте (Static UI not bundled — НЕ будет)"
else
    echo "  [!] WebUI static отсутствует — 'apexcore webui' покажет «Static UI not bundled»" >&2
fi
# Generic freshness check: установленный server.py должен быть БАЙТ-в-БАЙТ
# равен src/. Ловит stale build/lib (см. problems_fixes #21): _HWMON_CPU_CHIPS
# выше — старый маркер и НЕ доказывает что свежий код приехал.
SRC_SERVER="$ROOT/new-app/src/apexcore/interfaces/webui/server.py"
INST_SERVER="/opt/apexcore/.venv/lib/python3.11/site-packages/apexcore/interfaces/webui/server.py"
if [ -f "$SRC_SERVER" ] && [ -f "$INST_SERVER" ]; then
    if cmp -s "$SRC_SERVER" "$INST_SERVER"; then
        echo "  ✓ установленный server.py идентичен src/ (свежий код приехал)"
    else
        echo "  [!] установленный server.py ОТЛИЧАЕТСЯ от src/ — wheel собрался из stale build/lib? См. problems_fixes #21" >&2
    fi
fi

printf '\n\033[1;32mГотово.\033[0m Следующие шаги (вручную):\n'
printf '  apexcore doctor                # диагностика сенсоров\n'
printf '  apexcore info                  # capability-строка\n'
printf '  apexcore webui --port 8765 &   # WebUI tour\n'
printf '  apexcore micro run --preset fast    # короткий scoring v2 прогон (~30 сек)\n'
