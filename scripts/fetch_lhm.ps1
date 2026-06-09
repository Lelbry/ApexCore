# Скачать LibreHardwareMonitor зависимости в src/apexcore/infrastructure/sensors/lib/.
# Запускается scripts/build_windows.ps1 перед PyInstaller.
#
# Что внутри:
#   * LibreHardwareMonitorLib.dll — основной модуль чтения сенсоров (MPL-2.0)
#   * HidSharp.dll, BlackSharp.Core.dll, DiskInfoToolkit.dll, RAMSPDToolkit-NDD.dll —
#     зависимости LHM-lib (датчики материнки, дисков, SPD)
#   * Microsoft.Win32.TaskScheduler.dll, Microsoft.Bcl.*, System.* — netstandard
#     polyfill'ы для запуска под .NET Framework 4.8
#
# Что НЕ копируем:
#   * LibreHardwareMonitor.exe, OxyPlot.*, Aga.Controls — это GUI-обёртка LHM, нам не нужна
#   * WinRing0x64.sys — встроен в LibreHardwareMonitorLib.dll как resource, lib сама
#     извлекает его и регистрирует kernel-сервис WinRing0_1_2_0 при первом admin-старте.
#     Поэтому apexcore-installer (PrivilegesRequired=admin) триггерит установку драйвера
#     один раз через postinstall-вызов "apexcore info"; дальнейшие запуски — без UAC.
#   * .NET 8 self-contained — отказались, .NET Framework 4.8 идёт со всеми Win10+ из
#     коробки, а LHM-lib именно под net472 собран. Ставить .NET 8 рядом смысла нет.
#
# Запуск:
#   pwsh -File scripts/fetch_lhm.ps1
#   pwsh -File scripts/fetch_lhm.ps1 -Force            # перевыкачать

param(
    [switch]$Force
)

$ErrorActionPreference = "Stop"
Set-Location -Path (Split-Path -Parent $PSScriptRoot)

# ────────── зафиксированная версия ──────────

# LHM v0.9.6 (2026-02-14), последняя стабильная.
# digest опубликован GitHub Releases API (gh release view --json assets).
$LhmVersion = "0.9.6"
$LhmZipUrl = "https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases/download/v$LhmVersion/LibreHardwareMonitor.zip"
$LhmZipSha256 = "086d9f1b5a99e643edc2cfaaac16051685b551e4c5ac0b32a57c58c0e529c001"

$LibDir = "src/apexcore/infrastructure/sensors/lib"
$TempDir = "build/lhm_fetch"

# Файлы, которые мы НЕ хотим тащить в инсталлер (GUI-часть LHM и debug-артефакты).
$ExcludeFiles = @(
    "LibreHardwareMonitor.exe",
    "LibreHardwareMonitor.exe.config",
    "Aga.Controls.dll",
    "OxyPlot.dll",
    "OxyPlot.WindowsForms.dll"
)

function Test-LhmAlreadyFetched {
    return Test-Path "$LibDir/LibreHardwareMonitorLib.dll"
}

if ((Test-LhmAlreadyFetched) -and -not $Force) {
    Write-Host "[1/1] LHM-lib уже на месте, пропускаю"
    Write-Host "Готово."
    exit 0
}

Write-Host "[1/1] LibreHardwareMonitor v$LhmVersion"
New-Item -ItemType Directory -Force -Path $LibDir, $TempDir | Out-Null
$zipPath = "$TempDir/lhm.zip"

Write-Host "  download $LhmZipUrl"
Invoke-WebRequest -Uri $LhmZipUrl -OutFile $zipPath -UseBasicParsing

$actual = (Get-FileHash -Path $zipPath -Algorithm SHA256).Hash.ToLower()
if ($actual -ne $LhmZipSha256.ToLower()) {
    throw "SHA256 mismatch для $zipPath (ожидали $LhmZipSha256, получили $actual)"
}

$extractDir = "$TempDir/lhm"
Remove-Item -Recurse -Force $extractDir -ErrorAction SilentlyContinue
Expand-Archive -Path $zipPath -DestinationPath $extractDir -Force

# Чистим целевую папку от старых артефактов (включая остатки прошлых версий LHM).
Get-ChildItem -Path $LibDir -File | Where-Object {
    $_.Extension -in @(".dll", ".sys", ".pdb")
} | Remove-Item -Force

# Копируем только верхне-уровневые .dll (исключая GUI-обёртку), без .pdb/.xml/resources/.
$copied = 0
Get-ChildItem -Path $extractDir -File -Filter "*.dll" | Where-Object {
    $ExcludeFiles -notcontains $_.Name
} | ForEach-Object {
    Copy-Item $_.FullName $LibDir -Force
    $copied++
}

if (-not (Test-Path "$LibDir/LibreHardwareMonitorLib.dll")) {
    throw "LibreHardwareMonitorLib.dll не оказалась в $LibDir после копирования"
}

Write-Host "  → $LibDir ($copied файлов)"
Write-Host "Готово."
