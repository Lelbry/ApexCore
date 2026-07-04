# ApexCore — сборка под Windows (WebView2 bootstrapper + Inno Setup engine).
#
# Архитектура (см. plan):
#   bootstrapper.exe (C#/WPF/WebView2) — финальный UI инсталлера
#   apexcore-engine.exe (Inno Setup /SILENT) — actual file copy + сервисы
#   bootstrapper exec'ит engine, парсит лог, ретранслирует прогресс в HTML wizard.
#
# Требования (build-машина):
#   - Python 3.11+ в PATH
#   - pip install -e ".[dev,fast,windows]" + pyinstaller
#   - .NET 8 SDK (dotnet --version >= 8.0)
#   - Inno Setup 6 (iscc.exe в PATH)
#   - ImageMagick (magick.exe в PATH)
#
# Запуск из корня проекта: pwsh -File new-app/scripts/build_windows.ps1

$ErrorActionPreference = "Stop"
Set-Location -Path (Split-Path -Parent $PSScriptRoot)

Write-Host ""
Write-Host "=== ApexCore · Windows build ===" -ForegroundColor Cyan
Write-Host ""

Write-Host "[0/10] Очистка старой сборки..."
Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "[1/10] Версия из pyproject.toml..."
& (Join-Path $PSScriptRoot "sync_version.ps1")
if ($LASTEXITCODE -ne 0) { throw "sync_version.ps1 упал" }
$Version = (Get-Content build/version.txt -Raw).Trim()
$env:APEXCORE_VERSION = $Version
Write-Host "    APEXCORE_VERSION=$Version" -ForegroundColor Green

Write-Host ""
Write-Host "[2/10] Brand assets (ImageMagick → ICO + PNGs)..."
& (Join-Path $PSScriptRoot "build_branding.ps1")
if ($LASTEXITCODE -ne 0) { throw "build_branding.ps1 упал" }

Write-Host ""
Write-Host "[3/10] Bundle: PawnIO MSI (GitHub release)..."
try {
    & (Join-Path $PSScriptRoot "fetch_pawnio.ps1")
} catch {
    Write-Warning "PawnIO bundling failed: $($_.Exception.Message). Installer будет fallback'ить на winget."
}

Write-Host ""
Write-Host "[4/10] Bundle: smartmontools (SourceForge)..."
try {
    & (Join-Path $PSScriptRoot "fetch_smartmontools.ps1")
} catch {
    Write-Warning "smartmontools bundling failed. Installer будет fallback'ить на winget."
}

Write-Host ""
Write-Host "[4b/10] Bundle: WebView2 Evergreen Bootstrapper (для чистых Win10/LTSC)..."
try {
    & (Join-Path $PSScriptRoot "fetch_webview2.ps1")
} catch {
    Write-Warning "WebView2 bootstrapper bundling failed: $($_.Exception.Message). На машинах без WebView2 Runtime UI инсталлера не откроется."
}

Write-Host ""
Write-Host "[5/10] LibreHardwareMonitor v0.9.6 DLL..."
& (Join-Path $PSScriptRoot "fetch_lhm.ps1")
if ($LASTEXITCODE -ne 0) { throw "fetch_lhm.ps1 упал" }

Write-Host ""
Write-Host "[6/10] .NET 9 framework-dependent runtime для LHM..."
& (Join-Path $PSScriptRoot "fetch_dotnet9.ps1")
if ($LASTEXITCODE -ne 0) { throw "fetch_dotnet9.ps1 упал" }

Write-Host ""
Write-Host "[7/10] PyInstaller: apexcore.exe (CLI)..."
pyinstaller --noconfirm --clean packaging\windows\apexcore.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller (apexcore) упал" }

$DotnetSrc = "build/dotnet"
$DotnetDst = "dist/apexcore/dotnet"
if (Test-Path $DotnetSrc) {
    Write-Host "    Копирую $DotnetSrc → $DotnetDst"
    Copy-Item -Path $DotnetSrc -Destination $DotnetDst -Recurse -Force
} else {
    Write-Warning ".NET 9 runtime не найден — bundling пропущен"
}

Write-Host ""
Write-Host "[8/10] PyInstaller: apexcore-sensord.exe (Windows service)..."
pyinstaller --noconfirm --clean packaging\windows\apexcore-sensord.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller (sensord) упал" }

Write-Host ""
Write-Host "[9/10] Inno Setup engine (silent installer)..."
# Auto-discover iscc.exe: сначала PATH, потом per-machine, потом per-user install
# (winget JRSoftware.InnoSetup ставит в %LOCALAPPDATA%\Programs\Inno Setup 6\).
$iscc = Get-Command iscc -ErrorAction SilentlyContinue
if (-not $iscc) {
    $candidates = @(
        "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        "C:\Program Files\Inno Setup 6\ISCC.exe",
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
        "C:\Program Files (x86)\Inno Setup 5\ISCC.exe"
    )
    foreach ($p in $candidates) {
        if (Test-Path $p) {
            $iscc = $p
            Write-Host "  iscc найден: $p" -ForegroundColor Gray
            break
        }
    }
}
if (-not $iscc) {
    throw "iscc.exe не найден. Установите Inno Setup 6: winget install JRSoftware.InnoSetup"
}
$isccExe = if ($iscc -is [System.Management.Automation.CommandInfo]) { $iscc.Source } else { $iscc }
# Создаём dist/engine/ для engine'а (отличается от dist/installer/ для bootstrapper'а).
New-Item -ItemType Directory -Force -Path "dist/engine" | Out-Null
& $isccExe /Q `
    "/DOutputBaseFilename=apexcore-engine" `
    "/Odist/engine" `
    packaging\windows\installer.iss
if ($LASTEXITCODE -ne 0) { throw "Inno Setup упал" }

# .iss-файл задаёт OutputBaseFilename=apexcore-setup-{#MyAppVersion} — preprocessor
# /D не переопределяет это значение. Bootstrapper.csproj подключает движок
# по строгому имени dist/engine/apexcore-engine.exe — переименовываем.
$engineDst = "dist/engine/apexcore-engine.exe"
$engineSrc = Get-ChildItem -Path "dist/engine","dist","packaging\windows" -Recurse -Include "apexcore-engine*.exe","apexcore-setup-*.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
if ($engineSrc -and $engineSrc.FullName -ne (Resolve-Path $engineDst -ErrorAction SilentlyContinue)) {
    Move-Item $engineSrc.FullName $engineDst -Force
}
if (-not (Test-Path $engineDst)) { throw "engine .exe не найден после iscc — bootstrapper не сможет его встроить" }

Write-Host ""
Write-Host "[10/10] WebView2 bootstrapper (.NET 8 self-contained)..."

# Копируем shared design system в Resources/wwwroot/
$WwwSrc = "src/apexcore/interfaces/webui/static/setup"
$WwwDst = "packaging/windows/bootstrapper/Resources/wwwroot"
Remove-Item -Recurse -Force "$WwwDst/*" -ErrorAction SilentlyContinue -Exclude ".gitkeep"
Write-Host "    Копирую $WwwSrc → $WwwDst"
Copy-Item -Path "$WwwSrc/*" -Destination $WwwDst -Recurse -Force

# Подменяем версию в meta-теге + патчим пути для WebView2-хоста.
# В FastAPI/Astra-режиме wwwroot отдаётся под /static/setup/, в WebView2
# wwwroot мапится в корень виртуального хоста — нужно переписать пути,
# иначе все CSS/JS/assets отдают 404 и в окне белый экран.
# КРИТИЧНО: используем .NET I/O с явным UTF-8 без BOM. Get-Content -Raw
# в PS5.1 на ru-RU читает UTF-8 файл как CP1251 → кириллица в JS
# превращается в моджибаке после Set-Content.
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
function Edit-WebFile($path, [ScriptBlock]$transform) {
    # .NET I/O использует CWD процесса (не PowerShell Set-Location) — резолвим
    # в абсолютный путь, иначе ReadAllText искать будет от E:\Benchmark, а не
    # от new-app/.
    $abs = (Resolve-Path -LiteralPath $path).Path
    $orig = [System.IO.File]::ReadAllText($abs, $utf8NoBom)
    $patched = & $transform $orig
    if ($patched -ne $orig) {
        [System.IO.File]::WriteAllText($abs, $patched, $utf8NoBom)
        return $true
    }
    return $false
}
$indexHtml = Join-Path $WwwDst "index.html"
if (Test-Path $indexHtml) {
    Edit-WebFile $indexHtml {
        param($t)
        # Version-generic: ловим любой meta name="apexcore-version" content="X.Y.Z"
        # (раньше был хардкод на 0.8.6 — при bump'е приходилось править regex).
        # Эта же версия попадает в шапку EULA через state.bridge.version (license.js).
        $t = $t -replace 'name="apexcore-version"\s+content="[^"]*"', "name=`"apexcore-version`" content=`"$Version`""
        $t = $t -replace '"/static/setup/', '"/'
        $t = $t -replace '"/static/assets/', '"/assets/'
        $t
    } | Out-Null
}
Get-ChildItem -Path $WwwDst -Recurse -Include *.js,*.css | ForEach-Object {
    $changed = Edit-WebFile $_.FullName {
        param($t)
        $t -replace '/static/setup/', '/' -replace '/static/assets/', '/assets/'
    }
    if ($changed) { Write-Host "    [path-fix] $($_.Name)" }
}

$dotnet = Get-Command dotnet -ErrorAction SilentlyContinue
if (-not $dotnet) {
    throw ".NET 8 SDK не найден. Установите: https://dotnet.microsoft.com/download/dotnet/8.0"
}
dotnet publish packaging/windows/bootstrapper/Bootstrapper.csproj `
    -c Release -r win-x64 `
    -o dist/installer `
    -p:Version=$Version `
    -p:FileVersion=$Version `
    -p:AssemblyVersion=$Version
if ($LASTEXITCODE -ne 0) { throw "dotnet publish упал" }

# Финальное имя файла
$finalName = "apexcore-setup-$Version.exe"
$source = "dist/installer/apexcore-setup.exe"
if (Test-Path $source) {
    Move-Item $source "dist/installer/$finalName" -Force
}

Write-Host ""
Write-Host "=== Готово ===" -ForegroundColor Green
Write-Host "Инсталлер: dist/installer/$finalName"
Get-ChildItem dist/installer/ | Format-Table Name, Length -AutoSize
