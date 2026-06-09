#!/usr/bin/env bash
# Sync version — единый источник истины: pyproject.toml `[project] version`.
#
# Пишет:
#   build/version.txt — простой текст
#   build/version.iss — Inno Setup `#define MyAppVersion "X.X.X"`
# Экспортирует APEXCORE_VERSION в окружение (через `export` если source'нуть).
#
# Запуск:
#   bash scripts/sync_version.sh           # пишет файлы
#   source scripts/sync_version.sh         # + экспорт переменной в текущую сессию

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ ! -f pyproject.toml ]]; then
    echo "pyproject.toml не найден в $ROOT" >&2
    exit 1
fi

VERSION="$(python3 - <<'EOF'
import tomllib, pathlib
data = tomllib.loads(pathlib.Path("pyproject.toml").read_bytes().decode())
print(data["project"]["version"])
EOF
)"
if [[ -z "$VERSION" ]]; then
    echo "version пустой" >&2
    exit 2
fi

mkdir -p build
echo "$VERSION" > build/version.txt
printf '#define MyAppVersion "%s"\n' "$VERSION" > build/version.iss

export APEXCORE_VERSION="$VERSION"

echo "[version] = $VERSION"
echo "  build/version.txt + build/version.iss обновлены"
