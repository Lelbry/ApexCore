# Sync version — единый источник истины: pyproject.toml `[project] version`.
#
# Пишет три артефакта:
#   build/version.txt   — простой текст для всех сборок (Astra dch и т.п.)
#   build/version.iss   — Inno Setup `#define MyAppVersion "X.X.X"`
#   $env:APEXCORE_VERSION — переменная окружения текущего процесса PowerShell
#
# Запуск: pwsh -File scripts/sync_version.ps1
# Используется в scripts/build_windows.ps1 шаг [0/10] и scripts/build_astra.sh [3/6].

$ErrorActionPreference = "Stop"
Set-Location -Path (Split-Path -Parent $PSScriptRoot)

$PyToml = "pyproject.toml"
if (-not (Test-Path $PyToml)) {
    throw "pyproject.toml не найден: $PyToml"
}

# Python ≥ 3.11 умеет tomllib из коробки. Однострочник, чтобы избежать
# проблем с цитированием PowerShell 5.1 vs здесь-документов.
$pyExpr = 'import tomllib, pathlib; print(tomllib.loads(pathlib.Path(''pyproject.toml'').read_bytes().decode())[''project''][''version''])'
$Version = & python -c $pyExpr
if ($LASTEXITCODE -ne 0) { throw "python failed to read pyproject.toml" }
$Version = $Version.Trim()
if (-not $Version) { throw "version пустой" }

Write-Host "[version] = $Version"

$BuildDir = "build"
New-Item -ItemType Directory -Force -Path $BuildDir | Out-Null

Set-Content -Path "$BuildDir/version.txt" -Value $Version -Encoding utf8
Set-Content -Path "$BuildDir/version.iss" -Value "#define MyAppVersion `"$Version`"" -Encoding utf8

# Установим для текущего PS-процесса и parent окружения (если запускается из build_windows.ps1).
$env:APEXCORE_VERSION = $Version

Write-Host "  build/version.txt + build/version.iss обновлены"
Write-Host "  `$env:APEXCORE_VERSION = $Version"
