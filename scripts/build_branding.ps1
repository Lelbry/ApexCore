# Brand asset pipeline (Windows).
#
# Из packaging/branding/source/apex-logo.png генерирует все производные
# в build/branding/. Требует ImageMagick (`magick`) в PATH.
#
# Идемпотентен: пересобирает только если source новее target.
#
# Запуск: pwsh -File new-app/scripts/build_branding.ps1
#
# Используется в scripts/build_windows.ps1 (шаг [1/10]).

$ErrorActionPreference = "Stop"
Set-Location -Path (Split-Path -Parent $PSScriptRoot)

$Source = "packaging/branding/source/apex-logo.png"
$BuildDir = "build/branding"

if (-not (Test-Path $Source)) {
    throw "Source не найден: $Source — положите PNG 512x512 RGBA в эту папку."
}

# Проверка ImageMagick
$magick = Get-Command magick -ErrorAction SilentlyContinue
if (-not $magick) {
    Write-Host "[!] ImageMagick не найден в PATH." -ForegroundColor Red
    Write-Host "    Установите: winget install ImageMagick.ImageMagick" -ForegroundColor Yellow
    throw "magick.exe required"
}

New-Item -ItemType Directory -Force -Path $BuildDir | Out-Null

function NewerThan($target, $source) {
    if (-not (Test-Path $target)) { return $true }
    return (Get-Item $source).LastWriteTime -gt (Get-Item $target).LastWriteTime
}

$Sizes = @(256, 128, 80, 64, 52, 48, 32)
foreach ($size in $Sizes) {
    $out = Join-Path $BuildDir "apex-logo-$size.png"
    if (NewerThan $out $Source) {
        Write-Host "  → $out ($size×$size)"
        & magick "$Source" -resize "${size}x${size}" -strip "$out"
    }
}

# Multi-resolution ICO (16, 32, 48, 256)
$Ico = Join-Path $BuildDir "apex-logo.ico"
if (NewerThan $Ico $Source) {
    Write-Host "  → $Ico (multi-resolution 16/32/48/256)"
    & magick "$Source" `
        -define icon:auto-resize="256,48,32,16" `
        "$Ico"
}

Write-Host ""
Write-Host "[OK] Branding assets готовы в $BuildDir/" -ForegroundColor Green
Get-ChildItem $BuildDir | Format-Table Name, Length -AutoSize
