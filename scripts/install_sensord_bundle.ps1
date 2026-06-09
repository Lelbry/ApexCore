<#
.SYNOPSIS
    Регистрирует Windows-сервис apexcore_sensord из PyInstaller-бандла.

.DESCRIPTION
    Production-вариант установщика — используется внутри Inno Setup
    инсталлера. Бандл `apexcore-sensord.exe` self-contained (свой
    embedded Python + pywin32 + LHM DLL'и), поэтому регистрация сервиса
    тривиальная:

      1. `apexcore-sensord.exe install` — win32serviceutil узнаёт frozen
         режим (`sys.frozen == True`) и пишет binPath = sys.executable
         в SCM. servicemanager и прочие pywin32-модули внутри EXE.
      2. `sc.exe config start= auto` + `sc.exe start`.

    Никаких системного Python, pywin32_postinstall, PYTHONPATH в реестре —
    в production все эти dev-костыли не нужны.

    Скрипт вызывается:
      * из Inno Setup [Run] после копирования файлов (под admin-сессией
        установщика — UAC разовый при установке apexcore'а);
      * вручную: `& "C:\Program Files\apexcore\scripts\install_sensord_bundle.ps1"`
        (тогда сам триггерит UAC).

.PARAMETER InstallDir
    Каталог установки apexcore (по умолчанию — две папки вверх от скрипта).

.PARAMETER Uninstall
    Снять сервис. Эквивалент `apexcore-sensord.exe remove`.

.EXAMPLE
    # из Inno Setup [Run]:
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "{app}\scripts\install_sensord_bundle.ps1"

.EXAMPLE
    # вручную, под обычным юзером (UAC сам поднимется):
    & 'C:\Program Files\apexcore\scripts\install_sensord_bundle.ps1'
.EXAMPLE
    & 'C:\Program Files\apexcore\scripts\install_sensord_bundle.ps1' -Uninstall

.NOTES
    Этот скрипт ПРЕДНАЗНАЧЕН для production-сборки. Для dev-среды
    (editable install в venv) есть отдельный install_sensord.ps1 — он
    делает все 6 этапов pywin32+venv-танцев.
#>

[CmdletBinding()]
param(
    [string]$InstallDir,
    [switch]$Uninstall,
    # -NoPrompt — для non-interactive Inno [Run] (runhidden). Запрещает Read-Host
    # и не оставляет окно открытым после ошибок.
    [switch]$NoPrompt
)

$ErrorActionPreference = 'Stop'

# Логирование всех действий + ошибок в %PROGRAMDATA%\apexcore\install_sensord.log.
# Под runhidden пользователь не видит окно — лог единственный способ узнать что
# пошло не так. Tee-Object невозможен в PS 5.1 для всего скрипта, поэтому
# оборачиваем Write-Host в собственный логгер.
$LogPath = Join-Path $env:PROGRAMDATA 'apexcore\install_sensord.log'
try {
    $logDir = Split-Path -Parent $LogPath
    if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Force -Path $logDir -ErrorAction SilentlyContinue | Out-Null }
    "`r`n===== install_sensord_bundle.ps1 @ $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') =====" |
        Out-File -FilePath $LogPath -Append -Encoding utf8 -ErrorAction SilentlyContinue
    "InstallDir=$InstallDir Uninstall=$Uninstall NoPrompt=$NoPrompt" |
        Out-File -FilePath $LogPath -Append -Encoding utf8 -ErrorAction SilentlyContinue
} catch { }
function Write-LogMsg {
    param([string]$Msg, [string]$Color = 'White')
    try { $Msg | Out-File -FilePath $LogPath -Append -Encoding utf8 -ErrorAction SilentlyContinue } catch {}
    Write-Host $Msg -ForegroundColor $Color
}
# Глобальный catch — если что-то падает, пишем stack trace в лог
trap {
    $msg = "FATAL: $($_.Exception.Message)`n$($_.ScriptStackTrace)"
    try { $msg | Out-File -FilePath $LogPath -Append -Encoding utf8 -ErrorAction SilentlyContinue } catch {}
    Write-Host $msg -ForegroundColor Red
    exit 99
}

# Вычисляем InstallDir по умолчанию: scripts/<this>.ps1 → ..
if (-not $InstallDir) {
    $InstallDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
}

# Standalone sensord-bundle лежит в {app}\apexcore-sensord\ как самостоятельный
# PyInstaller-output. Inno Setup кладёт туда содержимое dist\apexcore-sensord.
$SensordExe = Join-Path $InstallDir 'apexcore-sensord\apexcore-sensord.exe'
$ServiceName = 'apexcore_sensord'

# ───── Самоэлевейтинг ─────
function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-IsAdministrator)) {
    Write-LogMsg 'Требуется админ — перезапускаюсь через UAC...' Yellow
    $shellExe = if (Get-Command pwsh -ErrorAction SilentlyContinue) { 'pwsh' } else { 'powershell.exe' }
    $argList = @('-ExecutionPolicy', 'Bypass', '-File', $MyInvocation.MyCommand.Path,
                 '-InstallDir', $InstallDir)
    if (-not $NoPrompt) { $argList = @('-NoExit') + $argList }
    if ($Uninstall) { $argList += '-Uninstall' }
    if ($NoPrompt) { $argList += '-NoPrompt' }
    Start-Process -FilePath $shellExe -ArgumentList $argList -Verb RunAs
    exit 0
}

# ─── Helper: bounded service removal с принудительным kill ─────────────────
# sc.exe stop без таймаута ждёт до 30 сек ответа от сервиса. Чтобы Inno
# uninstaller не висел навсегда и мог удалить apexcore-sensord.exe — стоп
# через job-с-таймаутом + kill PID + sc.exe delete. Также явный
# taskkill всех apexcore-sensord.exe процессов на всякий случай (если
# сервис уже unregister'ен но процесс остался в памяти).
function Remove-ServiceForced {
    param([string]$Name)
    $svc = Get-Service -Name $Name -ErrorAction SilentlyContinue
    if ($svc) {
        Write-LogMsg "  $Name : status=$($svc.Status), сношу..." DarkGray
        $job = Start-Job -ScriptBlock { & sc.exe stop $using:Name } 2>&1
        $null = Wait-Job -Job $job -Timeout 5
        Remove-Job -Job $job -Force -ErrorAction SilentlyContinue
        $svcWmi = Get-CimInstance -ClassName Win32_Service -Filter "Name='$Name'" -ErrorAction SilentlyContinue
        if ($svcWmi -and $svcWmi.ProcessId -gt 0) {
            try {
                Stop-Process -Id $svcWmi.ProcessId -Force -ErrorAction Stop
                Write-LogMsg "  killed PID $($svcWmi.ProcessId)" DarkGray
                Start-Sleep -Milliseconds 500
            } catch {
                Write-LogMsg "  kill PID $($svcWmi.ProcessId) не удался: $($_.Exception.Message)" DarkGray
            }
        }
        $job = Start-Job -ScriptBlock { & sc.exe delete $using:Name } 2>&1
        $null = Wait-Job -Job $job -Timeout 5
        Remove-Job -Job $job -Force -ErrorAction SilentlyContinue
        $still = Get-Service -Name $Name -ErrorAction SilentlyContinue
        if ($still) {
            Write-LogMsg "  ⚠ сервис $Name всё ещё в SCM — возможно нужен reboot" Yellow
        } else {
            Write-LogMsg "  ✓ $Name удалён" DarkGray
        }
    }
    # Дополнительно: kill любые orphan-процессы apexcore-sensord.exe
    # (бывают если сервис был unregistered но процесс остался). Это
    # критично для Inno uninstaller — без kill он не может удалить
    # apexcore-sensord.exe и папка остаётся «в использовании».
    $orphans = Get-Process apexcore-sensord -ErrorAction SilentlyContinue
    if ($orphans) {
        foreach ($p in $orphans) {
            try {
                Stop-Process -Id $p.Id -Force -ErrorAction Stop
                Write-LogMsg "  killed orphan apexcore-sensord.exe PID $($p.Id)" DarkGray
            } catch {
                Write-LogMsg "  kill orphan PID $($p.Id) не удался: $($_.Exception.Message)" Yellow
            }
        }
        Start-Sleep -Seconds 1  # дать Windows release file handles
    }
}

# ───── Uninstall ─────
if ($Uninstall) {
    Write-LogMsg '=== Удаление сервиса apexcore_sensord ===' Cyan
    # 1. Уничтожаем текущий apexcore_sensord (если есть) с гарантированным
    #    kill PID, чтобы файлы освободились до того как Inno начнёт удалять.
    Remove-ServiceForced -Name $ServiceName
    # 2. Также legacy benchkit_sensord — если v0.8.x ещё установлен рядом.
    if (Get-Service -Name 'benchkit_sensord' -ErrorAction SilentlyContinue) {
        Remove-ServiceForced -Name 'benchkit_sensord'
    }
    Write-LogMsg 'OK' Green
    exit 0
}

# ───── Install ─────
Write-LogMsg '=== Регистрация apexcore_sensord (production-бандл) ===' Cyan
Write-LogMsg "InstallDir: $InstallDir" DarkGray
Write-LogMsg "Sensord:    $SensordExe" DarkGray

if (-not (Test-Path $SensordExe)) {
    Write-LogMsg "✗ Не найден $SensordExe" Red
    Write-LogMsg '  Установщик не положил apexcore-sensord.exe — проверь сборку.' Yellow
    exit 1
}

# Backward-compat (upgrade с v0.8.x): старый сервис назывался
# benchkit_sensord. Если он зарегистрирован — сносим через тот же helper
# что и в Uninstall ветке. Удалить в v0.10.0.
if (Get-Service -Name 'benchkit_sensord' -ErrorAction SilentlyContinue) {
    Write-LogMsg 'Найден legacy-сервис benchkit_sensord (v0.8.x → v0.9.0 upgrade)' Yellow
    Remove-ServiceForced -Name 'benchkit_sensord'
}

# Если предыдущая установка apexcore_sensord осталась — переустанавливаем.
if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
    Write-LogMsg "Найден существующий $ServiceName — сношу для переустановки"
    Remove-ServiceForced -Name $ServiceName
}

Write-LogMsg '[1/3] Регистрирую через apexcore-sensord.exe install...'
# Захватываем stdout И stderr через cmd-обёртку — не через PS-pipe — чтобы
# ErrorRecord-конверсия PowerShell не вешала trap. cmd /c направляет всё
# в один файл, без участия PS-engine.
$installLog = Join-Path $env:PROGRAMDATA 'apexcore\install_sensord_step1.log'
$installExit = 0
try {
    & cmd.exe /c "`"$SensordExe`" install > `"$installLog`" 2>&1"
    $installExit = $LASTEXITCODE
} catch {
    Write-LogMsg "ошибка запуска cmd: $($_.Exception.Message)" Red
    $installExit = -1
}
if (Test-Path $installLog) {
    Get-Content -LiteralPath $installLog -ErrorAction SilentlyContinue |
        ForEach-Object { Write-LogMsg "  $_" }
}
if ($installExit -ne 0) {
    Write-LogMsg "⚠ apexcore-sensord.exe install вернул код $installExit" Yellow
    Write-LogMsg "  apexcore-sensord.exe: $SensordExe" Yellow
    Write-LogMsg "  путь {app}: $InstallDir" Yellow
    Write-LogMsg "  диагностика: apexcore-sensord.exe selftest" Yellow
    # Не блокируем installer — sensord опционален, repair-drivers починит позже
    exit 0
}
Write-LogMsg '      OK' Green

Write-LogMsg "[2/3] Переключаю на Automatic..."
& sc.exe config $ServiceName start= auto | Out-Null
Write-LogMsg '      OK' Green

Write-LogMsg "[3/3] Стартую и проверяю..."
# Не блокируем engine на старте сервиса. sc.exe start без -wait возвращается
# когда SCM принял запрос (START_PENDING); SCM сам ждёт 30 с пока процесс
# войдёт в RUNNING. Если sensord падает на старте — сервис останется
# Stopped и без жёстких блокировок [Run] вернётся быстро.
& sc.exe start $ServiceName 2>&1 | ForEach-Object { Write-LogMsg "  $_" }
# Bounded wait — max 12 секунд, поллим каждую секунду. Если за это время
# сервис не пришёл в Running — логируем и выходим БЕЗ exit 1, чтобы Inno
# engine не вешался. Sensord-проблема — отдельный класс проблем, его лечит
# `apexcore repair-drivers` и `apexcore-sensord.exe selftest`.
$running = $false
for ($i = 0; $i -lt 12; $i++) {
    Start-Sleep -Seconds 1
    $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($svc -and $svc.Status -eq 'Running') { $running = $true; break }
    if (-not $svc) { break }  # сервис вообще пропал
}
if (-not $running) {
    $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    Write-LogMsg "⚠ Сервис не дошёл до Running за 12 сек (status=$($svc.Status))" Yellow
    Write-LogMsg "  Это НЕ блокирует установку apexcore. CPU temp будет недоступна без" Yellow
    Write-LogMsg "  sensord — проверь причину через:" Yellow
    Write-LogMsg "    apexcore-sensord.exe selftest" Yellow
    Write-LogMsg "    Get-Content $env:PROGRAMDATA\apexcore\sensord-boot.log" Yellow
    Write-LogMsg "    Get-Content $env:PROGRAMDATA\apexcore\sensord.log -Tail 50" Yellow
    Write-LogMsg "  Затем: apexcore repair-drivers" Yellow
    $bootlog = Join-Path $env:PROGRAMDATA 'apexcore\sensord-boot.log'
    if (Test-Path $bootlog) {
        Write-LogMsg 'sensord-boot.log tail (последние 15 строк):' Yellow
        Get-Content -LiteralPath $bootlog -Tail 15 -ErrorAction SilentlyContinue |
            ForEach-Object { Write-LogMsg "    $_" }
    }
    $logPath = Join-Path $env:PROGRAMDATA 'apexcore\sensord.log'
    if (Test-Path $logPath) {
        Write-LogMsg 'sensord.log tail:' Yellow
        Get-Content -LiteralPath $logPath -Tail 25 -ErrorAction SilentlyContinue |
            ForEach-Object { Write-LogMsg "    $_" }
    }
    # Намеренно exit 0 — installer должен завершиться УСПЕШНО. Sensord
    # это опциональная фича, без неё apexcore работает (только CPU temp
    # без admin не подтянет).
    exit 0
}
Write-LogMsg '      OK (Status=Running)' Green
Write-LogMsg ''
Write-LogMsg 'Готово! apexcore будет работать без UAC при каждом запуске.' Green
