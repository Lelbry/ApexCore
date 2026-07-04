<#
.SYNOPSIS
    Dev-launcher: запускает apexcore с админ-правами в активированном .venv.

.DESCRIPTION
    Без прав администратора kernel-driver WinRing0 не регистрируется и
    LibreHardwareMonitorLib не отдаёт CPU-температуру (issue #20 / #17).
    Этот скрипт сам элевейтит окно через UAC, активирует .venv в корне
    репозитория, переходит в new-app/ и запускает apexcore с переданными
    аргументами. Окно остаётся открытым (-NoExit) для повторных вызовов.

    Без аргументов — открывает интерактивное меню apexcore.
    С аргументами — пробрасывает их в apexcore как есть.

.EXAMPLE
    .\dev.ps1
    Открывает интерактивное меню в админ-окне.

.EXAMPLE
    .\dev.ps1 doctor
    apexcore doctor — диагностика сенсоров (должна показать CPU-температуру).

.EXAMPLE
    .\dev.ps1 micro run --preset standard
    Прогон микробенча с пресетом standard.
#>

[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ApexcoreArgs
)

$ErrorActionPreference = 'Stop'

$scriptPath = $MyInvocation.MyCommand.Path

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-IsAdministrator)) {
    Write-Host "Перезапуск через UAC (нужен админ для WinRing0/LHM CPU-температуры)..." -ForegroundColor Yellow
    $shellExe = if (Get-Command pwsh -ErrorAction SilentlyContinue) { 'pwsh' } else { 'powershell.exe' }
    $argList = @('-NoExit', '-ExecutionPolicy', 'Bypass', '-File', $scriptPath)
    if ($ApexcoreArgs) {
        $argList += $ApexcoreArgs
    }
    Start-Process -FilePath $shellExe -ArgumentList $argList -Verb RunAs
    exit 0
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$newAppDir = Join-Path $repoRoot 'new-app'
$venvActivate = Join-Path $repoRoot '.venv\Scripts\Activate.ps1'

# Worktree-fallback: если в текущем репо-корне нет .venv (типичный случай
# для git worktree, где dev держит один общий venv в основном репо) —
# пытаемся найти основной репо.
#
# Стратегия 1 (надёжная, без зависимости от git/PATH): если репо лежит
# по пути `<main>\.claude\worktrees\<name>`, то <main> ровно три уровня
# вверх от worktree-корня. Smart App Control / UAC обычно ломают
# git-в-PATH для admin-shell, так что path-based проверка — основная.
#
# Стратегия 2 (fallback): спросить git rev-parse --git-common-dir. Может
# не сработать в admin-окне после UAC если git не на PATH у LocalSystem.
if (-not (Test-Path -LiteralPath $venvActivate)) {
    $candidateMains = @()
    # Стратегия 1: путь содержит `\.claude\worktrees\`.
    if ($repoRoot -match '^(?<main>.+?)\\\.claude\\worktrees\\') {
        $candidateMains += $matches['main']
    }
    # Стратегия 2: git rev-parse, если git доступен.
    try {
        $gitCommonDir = & git -C $repoRoot rev-parse --git-common-dir 2>$null
        if ($LASTEXITCODE -eq 0 -and $gitCommonDir) {
            $gitCommonAbs = if ([System.IO.Path]::IsPathRooted($gitCommonDir)) {
                $gitCommonDir
            } else {
                Join-Path $repoRoot $gitCommonDir
            }
            $candidateMains += (Resolve-Path (Join-Path $gitCommonAbs '..')).Path
        }
    } catch {
        # git не найден — пропускаем стратегию 2.
    }
    foreach ($mainRepoRoot in $candidateMains) {
        $fallbackActivate = Join-Path $mainRepoRoot '.venv\Scripts\Activate.ps1'
        if ((Test-Path -LiteralPath $fallbackActivate) -and ($mainRepoRoot -ne $repoRoot)) {
            Write-Host "Worktree обнаружен; использую .venv основного репо: $mainRepoRoot" -ForegroundColor DarkYellow
            $venvActivate = $fallbackActivate
            break
        }
    }
}

if (-not (Test-Path -LiteralPath $venvActivate)) {
    Write-Host "Не найден .venv по пути $venvActivate" -ForegroundColor Red
    Write-Host "Создайте окружение и установите apexcore в dev-режиме:" -ForegroundColor Yellow
    Write-Host "  cd $repoRoot"
    Write-Host '  python -m venv .venv'
    Write-Host '  .\.venv\Scripts\Activate.ps1'
    Write-Host '  pip install -e ".\new-app[dev]"'
    exit 1
}

Write-Host "Админ-режим: активирую .venv ($venvActivate)..." -ForegroundColor Cyan
. $venvActivate

Set-Location -LiteralPath $newAppDir

# Запускаем через `python -m apexcore`, а не `apexcore.exe`. Причина —
# Windows Smart App Control блокирует pip-сгенерированные wrapper-exe
# (`apexcore.exe`, `pip.exe`) как неподписанные, но сам подписанный
# `python.exe` (Python.org) пропускает. `__main__.py` в модуле даёт
# тот же entry point, что и wrapper-exe.
$pythonExe = Join-Path (Split-Path -Parent $venvActivate) 'python.exe'

if ($ApexcoreArgs -and $ApexcoreArgs.Count -gt 0) {
    Write-Host "Запуск: python -m apexcore $($ApexcoreArgs -join ' ')" -ForegroundColor Green
    & $pythonExe -m apexcore @ApexcoreArgs
} else {
    Write-Host "Запуск: python -m apexcore (меню)" -ForegroundColor Green
    & $pythonExe -m apexcore
}
