<#
.SYNOPSIS
    Регистрирует Windows-сервис apexcore_sensord (постоянный, start=auto).

.DESCRIPTION
    apexcore_sensord — Python-сервис, который держит LibreHardwareMonitor
    с PawnIO открытыми всю свою жизнь и каждые 250 мс публикует snapshot
    всех сенсоров в Global shared memory `Global\apexcore_sensors`.
    apexcore-клиенты (без admin) читают snapshot через shm_adapter —
    UAC при каждом запуске apexcore больше не нужен.

    Сценарий установки — много шагов из-за известных подводных камней
    pywin32 + venv на Windows. Скрипт делает их все автоматически:

      1. Проверяет наличие LibreHardwareMonitorLib.dll в src/.../sensors/lib;
         если нет — запускает fetch_lhm.ps1.
      2. Устанавливает pywin32 в СИСТЕМНЫЙ Python (тот, что лежит в
         %LOCALAPPDATA%\Programs\Python\Python311 или указанный явно).
         Это нужно потому что pythonservice.exe инициализирует Python
         через системный prefix и НЕ подхватывает venv pywin32. С pywin32
         только в venv сервис падает на «No module named 'servicemanager'».
      3. Запускает pywin32_postinstall.py -install чтобы скопировать
         pythoncom*.dll/pywintypes*.dll в C:\Windows\System32.
      4. Регистрирует сервис через системный Python:
            python -m apexcore.services.sensord install
         binPath получает системный pythonservice.exe.
      5. Прописывает PYTHONPATH в реестре сервиса (Environment), указывая
         на наш src и на venv\Lib\site-packages для 3rd-party зависимостей.
      6. Переключает на Automatic и стартует.

    Требования:
      * Windows 10/11.
      * PawnIO установлен через MSI (https://pawnio.eu) ИЛИ через
         install_pawnio_service.ps1; без него сервис стартует, но snapshot
         будет пустым (graceful degrade).
      * Системный Python 3.11 (по умолчанию ищется в
         %LOCALAPPDATA%\Programs\Python\Python311). Если он в другом
         месте — передай -SystemPythonExe.
      * apexcore установлен editable в .venv (по умолчанию `.venv` в корне репозитория).
         Это даёт нам путь к 3rd-party зависимостям (pythonnet, pydantic,
         numpy и т.д.) — сервис подхватывает их через PYTHONPATH.

.PARAMETER SystemPythonExe
    Путь к python.exe в СИСТЕМНОМ Python (вне venv). По умолчанию
    %LOCALAPPDATA%\Programs\Python\Python311\python.exe.

.PARAMETER VenvPath
    Путь к корню .venv, где лежат 3rd-party зависимости apexcore'а.
    По умолчанию `.venv` в корне репозитория.

.PARAMETER WorktreeSrc
    Путь к src/ apexcore'а (editable install). По умолчанию вычисляется
    относительно расположения этого скрипта.

.PARAMETER Uninstall
    Снять сервис. Эквивалент `python -m apexcore.services.sensord remove`.

.EXAMPLE
    .\scripts\install_sensord.ps1
.EXAMPLE
    .\scripts\install_sensord.ps1 -Uninstall
.EXAMPLE
    .\scripts\install_sensord.ps1 -SystemPythonExe 'C:\Python311\python.exe'

.NOTES
    Сервис работает под LocalSystem — этого достаточно для CreateFile(\\.\PawnIO)
    и для создания Global mapping'а с явным SDDL
    `D:P(A;;GR;;;WD)(A;;GA;;;SY)(A;;GA;;;BA)`.

    После установки клиенты apexcore (любые non-admin процессы) видят сенсоры
    через `apexcore.services.shm_adapter.read_shm_snapshot()` — там 108+
    значений (temperatures, voltages, power, fans, clocks, tjmax).

    Лог: `%PROGRAMDATA%\apexcore\sensord.log` (rotated 512KB x 3).
#>

[CmdletBinding()]
param(
    [string]$SystemPythonExe,
    [string]$VenvPath,
    [string]$WorktreeSrc,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'

# ───── Вычисляем дефолтные пути ─────
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$newAppDir = Split-Path -Parent $scriptDir

if (-not $SystemPythonExe) {
    $SystemPythonExe = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe'
}
if (-not $VenvPath) {
    $VenvPath = (Join-Path $newAppDir '.venv')
}
if (-not $WorktreeSrc) {
    $WorktreeSrc = (Resolve-Path (Join-Path $newAppDir 'src')).Path
}

# ───── Самоэлевейтинг через UAC ─────
function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-IsAdministrator)) {
    Write-Host 'Требуется админ — перезапускаюсь через UAC...' -ForegroundColor Yellow
    Write-Host "  System Python: $SystemPythonExe" -ForegroundColor DarkGray
    Write-Host "  Venv:          $VenvPath" -ForegroundColor DarkGray
    Write-Host "  Worktree src:  $WorktreeSrc" -ForegroundColor DarkGray
    $shellExe = if (Get-Command pwsh -ErrorAction SilentlyContinue) { 'pwsh' } else { 'powershell.exe' }
    $argList = @(
        '-NoExit', '-ExecutionPolicy', 'Bypass', '-File', $MyInvocation.MyCommand.Path,
        '-SystemPythonExe', $SystemPythonExe,
        '-VenvPath', $VenvPath,
        '-WorktreeSrc', $WorktreeSrc
    )
    if ($Uninstall) { $argList += '-Uninstall' }
    Start-Process -FilePath $shellExe -ArgumentList $argList -Verb RunAs
    exit 0
}

# ───── Константы ─────
$ServiceName = 'apexcore_sensord'

# ───── Uninstall-ветка ─────
if ($Uninstall) {
    Write-Host '=== Удаление сервиса apexcore_sensord ===' -ForegroundColor Cyan
    $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if (-not $svc) {
        Write-Host 'Сервис не зарегистрирован — нечего удалять.' -ForegroundColor Yellow
    } else {
        if ($svc.Status -eq 'Running') {
            Write-Host "  останавливаю $ServiceName..."
            & sc.exe stop $ServiceName | Out-Null
            Start-Sleep -Seconds 1
        }
        Write-Host "  удаляю сервис $ServiceName..."
        # Системный Python не видит apexcore без явного PYTHONPATH — без
        # него `python -m apexcore.services.sensord remove` падает с
        # ModuleNotFoundError, и приходится fallback'ом дёргать sc.exe.
        # Ставим PYTHONPATH временно ради чистого вывода.
        if (Test-Path $SystemPythonExe) {
            $env:PYTHONPATH = "$WorktreeSrc;$(Join-Path $VenvPath 'Lib\site-packages')"
            & $SystemPythonExe -m apexcore.services.sensord remove
            Remove-Item Env:\PYTHONPATH -ErrorAction SilentlyContinue
        }
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path $SystemPythonExe)) {
            & sc.exe delete $ServiceName | Out-Null
        }
        Write-Host '  OK' -ForegroundColor Green
    }
    Write-Host ''
    Write-Host '[Enter] чтобы закрыть окно...' -ForegroundColor DarkGray
    [void](Read-Host)
    exit 0
}

# ───── Install-ветка ─────
Write-Host '=== Установка сервиса apexcore_sensord ===' -ForegroundColor Cyan
Write-Host "System Python: $SystemPythonExe" -ForegroundColor DarkGray
Write-Host "Venv:          $VenvPath" -ForegroundColor DarkGray
Write-Host "Worktree src:  $WorktreeSrc" -ForegroundColor DarkGray

# 1. LibreHardwareMonitorLib.dll должен быть в lib/.
Write-Host '[1/6] Проверяю LibreHardwareMonitorLib.dll в src/.../sensors/lib...'
$libDir = Join-Path $WorktreeSrc 'apexcore\infrastructure\sensors\lib'
$lhmDll = Join-Path $libDir 'LibreHardwareMonitorLib.dll'
if (-not (Test-Path $lhmDll)) {
    Write-Host "      $lhmDll отсутствует — запускаю fetch_lhm.ps1..." -ForegroundColor Yellow
    $fetchLhm = Join-Path $scriptDir 'fetch_lhm.ps1'
    if (Test-Path $fetchLhm) {
        & $fetchLhm
        if (-not (Test-Path $lhmDll)) {
            Write-Host "✗ fetch_lhm.ps1 не положил DLL в $libDir" -ForegroundColor Red
            exit 1
        }
    } else {
        Write-Host "✗ fetch_lhm.ps1 не найден ($fetchLhm). Положите LibreHardwareMonitorLib.dll руками." -ForegroundColor Red
        exit 1
    }
}
Write-Host '      OK' -ForegroundColor Green

# 2. Системный Python — проверка наличия и установка pywin32 туда.
Write-Host '[2/6] Проверяю системный Python + pywin32...'
if (-not (Test-Path $SystemPythonExe)) {
    Write-Host "✗ Системный Python не найден: $SystemPythonExe" -ForegroundColor Red
    Write-Host '  Установи Python 3.11 от python.org или передай -SystemPythonExe.' -ForegroundColor Yellow
    exit 1
}
$probe = & $SystemPythonExe -c "import sys; print(sys.version_info[:2])"
Write-Host "      version: $probe"
$smProbe = & $SystemPythonExe -c "import importlib.util; print('OK' if importlib.util.find_spec('servicemanager') else 'MISSING')"
if ($smProbe -ne 'OK') {
    Write-Host '      pywin32 (servicemanager) не найден — ставлю pip install pywin32...'
    & $SystemPythonExe -m pip install pywin32 --quiet
    if ($LASTEXITCODE -ne 0) { Write-Host '✗ pip install pywin32 упал' -ForegroundColor Red; exit 1 }
} else {
    Write-Host '      pywin32 уже установлен' -ForegroundColor Green
}

# 3. pywin32_postinstall — копирует pythoncom*.dll/pywintypes*.dll в System32.
Write-Host '[3/6] Запускаю pywin32_postinstall (копирует DLL в System32)...'
if (-not (Test-Path 'C:\Windows\System32\pythoncom311.dll')) {
    $postInstall = & $SystemPythonExe -c "import os, sys; print(os.path.join(os.path.dirname(sys.executable), 'Scripts', 'pywin32_postinstall.py'))"
    if (Test-Path $postInstall) {
        & $SystemPythonExe $postInstall -install
    } else {
        & $SystemPythonExe -m pywin32_postinstall -install
    }
    if (-not (Test-Path 'C:\Windows\System32\pythoncom311.dll')) {
        Write-Host '✗ pywin32_postinstall не положил pythoncom311.dll в System32' -ForegroundColor Red
        exit 1
    }
}
Write-Host '      OK (pythoncom311.dll и pywintypes311.dll в System32)' -ForegroundColor Green

# 4. Регистрация сервиса через системный Python (binPath будет системный pythonservice.exe).
Write-Host "[4/6] Регистрирую $ServiceName через системный Python..."
$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "      обнаружен предыдущий сервис ($($existing.Status), $($existing.StartType)) — сношу..."
    if ($existing.Status -eq 'Running') {
        & sc.exe stop $ServiceName | Out-Null
        Start-Sleep -Seconds 1
    }
    & sc.exe delete $ServiceName | Out-Null
    Start-Sleep -Seconds 1
}
$env:PYTHONPATH = "$WorktreeSrc;$(Join-Path $VenvPath 'Lib\site-packages')"
& $SystemPythonExe -m apexcore.services.sensord install
if ($LASTEXITCODE -ne 0) {
    Write-Host "✗ install упал (код $LASTEXITCODE)" -ForegroundColor Red
    exit 1
}
Write-Host '      OK' -ForegroundColor Green

# 5. PYTHONPATH в реестре сервиса (Environment). Без этого pythonservice.exe
# не находит apexcore (системный Python не знает про editable install в venv).
Write-Host "[5/6] Прописываю PYTHONPATH в реестр HKLM\...\Services\$ServiceName..."
$svcPath = "HKLM:\SYSTEM\CurrentControlSet\Services\$ServiceName"
$venvSP = Join-Path $VenvPath 'Lib\site-packages'
$paths = @(
    $WorktreeSrc,
    $venvSP
) -join ';'
New-ItemProperty -Path $svcPath -Name Environment -PropertyType MultiString -Value @("PYTHONPATH=$paths") -Force | Out-Null
Write-Host '      OK' -ForegroundColor Green

# 6. Старт и проверка.
Write-Host "[6/6] Переключаю $ServiceName на Automatic и запускаю..."
& sc.exe config $ServiceName start= auto | Out-Null
& sc.exe start $ServiceName | Out-Null
Start-Sleep -Seconds 6
$svc = Get-Service -Name $ServiceName
if ($svc.Status -ne 'Running') {
    Write-Host "✗ Сервис не Running: $($svc.Status)" -ForegroundColor Red
    Write-Host 'Лог сервиса:' -ForegroundColor Yellow
    $logPath = Join-Path $env:PROGRAMDATA 'apexcore\sensord.log'
    if (Test-Path $logPath) { Get-Content -LiteralPath $logPath -Tail 30 }
    Write-Host ''
    Write-Host 'Application event log:' -ForegroundColor Yellow
    Get-EventLog -LogName Application -Newest 5 -EntryType Error |
        Where-Object { $_.Source -match 'python|apexcore' } |
        Format-List TimeGenerated, EventID, Source, Message
    exit 1
}
Write-Host '      OK (Status=Running)' -ForegroundColor Green
Write-Host ''
Write-Host 'Готово! apexcore_sensord зарегистрирован и работает.' -ForegroundColor Green
Write-Host ''
Write-Host 'Проверь из НЕ-admin окна:' -ForegroundColor Cyan
Write-Host '  python -c "from apexcore.services.shm_adapter import read_shm_snapshot;' -NoNewline
Write-Host ' s=read_shm_snapshot(); print(len(s.values) if s else 0, ''keys'')"'
Write-Host ''
Write-Host 'Лог сервиса: %PROGRAMDATA%\apexcore\sensord.log' -ForegroundColor DarkGray
Write-Host 'Откат:       .\scripts\install_sensord.ps1 -Uninstall' -ForegroundColor DarkGray
Write-Host ''
Write-Host '[Enter] чтобы закрыть окно...' -ForegroundColor DarkGray
[void](Read-Host)
