# P0.8 (релиз v0.5.1): скачивает framework-dependent .NET 9 runtime
# для bundling рядом с apexcore.exe в Inno Setup installer.
#
# Цель: pythonnet + LibreHardwareMonitorLib работают на машинах без
# предустановленного .NET (LTSC Win10/11, корпоративные образы),
# обходит pythonnet issue #2595 (.NET 8 — типы не экспортируются на
# Python-сторону).
#
# Стратегия:
#   - Скачиваем zip из официального azureedge.net (Microsoft CDN).
#   - Извлекаем `shared/Microsoft.NETCore.App/9.0.x/` в
#     `<repo>/build/dotnet/shared/Microsoft.NETCore.App/9.0.x/`.
#   - Извлекаем `host/fxr/9.0.x/` туда же.
#   - Генерируем `apexcore.runtimeconfig.json`.
#   - На Astra Linux этот скрипт не запускается (там hwmon native).

$ErrorActionPreference = "Stop"
Set-Location -Path (Split-Path -Parent $PSScriptRoot)

$DotnetVersion = "9.0.0"
$DotnetUrl = "https://dotnetcli.azureedge.net/dotnet/Runtime/$DotnetVersion/dotnet-runtime-$DotnetVersion-win-x64.zip"
# Хэш можно зафиксировать после первой выкачки. Здесь оставлен пустым;
# реальную проверку SHA256 рекомендуется добавить перед релизом.
$DotnetExpectedSha256 = ""

$BuildDir = "build/dotnet"
$ZipPath = "build/dotnet9.zip"

if (Test-Path "$BuildDir/shared/Microsoft.NETCore.App/$DotnetVersion") {
    Write-Host "[1/3] .NET $DotnetVersion уже на месте — skip."
} else {
    Write-Host "[1/3] Скачиваем .NET $DotnetVersion runtime"
    New-Item -ItemType Directory -Force -Path "build" | Out-Null
    Invoke-WebRequest -Uri $DotnetUrl -OutFile $ZipPath -UseBasicParsing
    if ($DotnetExpectedSha256) {
        $actual = (Get-FileHash -Path $ZipPath -Algorithm SHA256).Hash.ToLower()
        if ($actual -ne $DotnetExpectedSha256.ToLower()) {
            throw "SHA256 mismatch для $ZipPath (ожидали $DotnetExpectedSha256, получили $actual)"
        }
    }

    Write-Host "[2/3] Извлечение в $BuildDir"
    if (Test-Path $BuildDir) {
        Remove-Item -Recurse -Force $BuildDir
    }
    Expand-Archive -Path $ZipPath -DestinationPath $BuildDir -Force
}

Write-Host "[3/4] Генерация apexcore.runtimeconfig.json"
$Config = @"
{
  "runtimeOptions": {
    "tfm": "net9.0",
    "framework": {
      "name": "Microsoft.NETCore.App",
      "version": "$DotnetVersion"
    },
    "rollForward": "LatestPatch"
  }
}
"@
$ConfigPath = "$BuildDir/apexcore.runtimeconfig.json"
Set-Content -Path $ConfigPath -Value $Config -Encoding utf8

# .NET 9 standalone runtime НЕ содержит System.Threading.AccessControl.dll —
# этот тип (MutexAccessRule) поставляется отдельным NuGet-пакетом. LHM v0.9+
# использует MutexAccessRule в Mutexes.Open() → без этой DLL Computer.Open()
# падает с TypeLoadException на .NET 9, не позволяя сервису apexcore_sensord
# войти в Running. На .NET Framework 4.x тип был в mscorlib/System.dll и
# проблемы не возникало.
$AccessControlSharedDir = "$BuildDir/shared/Microsoft.NETCore.App/$DotnetVersion"
$AccessControlDll = "$AccessControlSharedDir/System.Threading.AccessControl.dll"
if (Test-Path $AccessControlDll) {
    Write-Host "[4/4] System.Threading.AccessControl.dll уже на месте — skip."
} else {
    Write-Host "[4/4] Скачиваем System.Threading.AccessControl (NuGet, для LHM Mutex)"
    $NugetVersion = "9.0.0"
    $NugetUrl = "https://www.nuget.org/api/v2/package/System.Threading.AccessControl/$NugetVersion"
    # PS5.1 Expand-Archive проверяет расширение → .nupkg unsupported.
    # Скачиваем сразу с .zip-расширением (NuGet content — это zip-архив).
    $NupkgPath = "build/system.threading.accesscontrol.zip"
    $NupkgExtract = "build/system.threading.accesscontrol.extract"
    try {
        Invoke-WebRequest -Uri $NugetUrl -OutFile $NupkgPath -UseBasicParsing
        if (Test-Path $NupkgExtract) {
            Remove-Item -Recurse -Force $NupkgExtract
        }
        Expand-Archive -Path $NupkgPath -DestinationPath $NupkgExtract -Force
        # Внутри nupkg: lib/net8.0/System.Threading.AccessControl.dll (применимо к net9.0)
        $sourceDll = Get-ChildItem -Path "$NupkgExtract/lib" -Filter "System.Threading.AccessControl.dll" -Recurse `
            -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match 'net[89]\.\d' } |
            Sort-Object -Property FullName -Descending |
            Select-Object -First 1
        if (-not $sourceDll) {
            throw "Не найден System.Threading.AccessControl.dll внутри nupkg"
        }
        Copy-Item -Path $sourceDll.FullName -Destination $AccessControlDll -Force
        Write-Host "  скопирована $($sourceDll.FullName) → $AccessControlDll"
        # cleanup
        Remove-Item -Recurse -Force $NupkgExtract -ErrorAction SilentlyContinue
        Remove-Item -Force $NupkgPath -ErrorAction SilentlyContinue
    } catch {
        Write-Warning "Не удалось скачать System.Threading.AccessControl: $($_.Exception.Message)"
        Write-Warning "LHM Computer.Open() может упасть с TypeLoadException на frozen sensord."
    }
}

Write-Host "Готово: bundled .NET $DotnetVersion в $BuildDir/, runtimeconfig.json — рядом."
