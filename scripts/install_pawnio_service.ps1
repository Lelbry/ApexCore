<#
.SYNOPSIS
    Постоянно регистрирует kernel-сервис PawnIO (start=auto).

.DESCRIPTION
    LibreHardwareMonitor v0.9+ перешёл с WinRing0 на PawnIO — современный
    user-mode framework для доступа к MSR/PCI с WHQL-подписанным kernel-
    драйвером (`PawnIO.sys`, Microsoft Windows Hardware Compatibility Publisher).

    PawnIO MSI кладёт `PawnIO.sys` в Windows DriverStore, но сервис в SCM
    регистрируется dynamic из `PawnIOLib.dll` и только на время процесса
    LHM. При выходе сервис останавливается и удаляется — следующий запуск
    apexcore БЕЗ админа уже не видит сенсоров.

    Этот скрипт регистрирует сервис `PawnIO` постоянно (`start=auto`):
    * стартует автоматически при загрузке Windows;
    * любой процесс (включая apexcore без admin) видит PawnIO и читает
      CPU-температуру, Vcore, питание, DIMM-температуру;
    * LHM при запуске находит существующий сервис и подключается, не
      создавая своего.

    Требования:
    * PawnIO должен быть установлен через MSI (https://pawnio.eu).
      LHM при первом admin-запуске сам предлагает установку.
    * Запуск под админом — скрипт сам триггерит UAC.

.PARAMETER Uninstall
    Снять постоянный сервис. Сам PawnIO MSI не трогается.

.EXAMPLE
    .\scripts\install_pawnio_service.ps1
.EXAMPLE
    .\scripts\install_pawnio_service.ps1 -Uninstall

.NOTES
    Безопасность: PawnIO даёт user-mode коду доступ к MSR/PCI через
    подписанные AMX-скрипты. На dev-машине это нормально; в production
    можно оставить demand-start (по умолчанию у MSI) и регистрировать
    сервис на каждый прогон через UAC.
#>

[CmdletBinding()]
param(
    [switch]$Uninstall,
    # -NoPrompt — для non-interactive контекста (Inno [Run] runhidden). Без него
    # Read-Host'ы в конце скрипта вешают engine навсегда, потому что hidden-окно
    # ждёт Enter которого нет. Inno [Run] обязан передавать -NoPrompt.
    [switch]$NoPrompt
)

$ErrorActionPreference = 'Stop'

# Логирование в %PROGRAMDATA%\apexcore\install_pawnio.log — единственный способ
# увидеть что произошло когда скрипт работает из Inno [Run] runhidden.
$LogPath = Join-Path $env:PROGRAMDATA 'apexcore\install_pawnio.log'
try {
    $logDir = Split-Path -Parent $LogPath
    if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Force -Path $logDir -ErrorAction SilentlyContinue | Out-Null }
    "`r`n===== install_pawnio_service.ps1 @ $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') =====" |
        Out-File -FilePath $LogPath -Append -Encoding utf8 -ErrorAction SilentlyContinue
    "Uninstall=$Uninstall NoPrompt=$NoPrompt" |
        Out-File -FilePath $LogPath -Append -Encoding utf8 -ErrorAction SilentlyContinue
} catch { }
function Write-LogMsg {
    param([string]$Msg, [string]$Color = 'White')
    try { $Msg | Out-File -FilePath $LogPath -Append -Encoding utf8 -ErrorAction SilentlyContinue } catch {}
    Write-Host $Msg -ForegroundColor $Color
}
trap {
    $m = "FATAL: $($_.Exception.Message)`n$($_.ScriptStackTrace)"
    try { $m | Out-File -FilePath $LogPath -Append -Encoding utf8 -ErrorAction SilentlyContinue } catch {}
    Write-Host $m -ForegroundColor Red
    exit 99
}

# ───── Самоэлевейтинг через UAC ─────
function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-IsAdministrator)) {
    Write-Host 'Требуется админ — перезапускаюсь через UAC...' -ForegroundColor Yellow
    $shellExe = if (Get-Command pwsh -ErrorAction SilentlyContinue) { 'pwsh' } else { 'powershell.exe' }
    $argList = @('-ExecutionPolicy', 'Bypass', '-File', $MyInvocation.MyCommand.Path)
    if (-not $NoPrompt) { $argList = @('-NoExit') + $argList }
    if ($Uninstall) { $argList += '-Uninstall' }
    if ($NoPrompt) { $argList += '-NoPrompt' }
    Start-Process -FilePath $shellExe -ArgumentList $argList -Verb RunAs
    exit 0
}

# ───── Константы ─────
$ServiceName    = 'PawnIO'
$DriverFileName = 'PawnIO.sys'
$DriverDestPath = Join-Path $env:SystemRoot "System32\drivers\$DriverFileName"

# ───── Uninstall-ветка ─────
if ($Uninstall) {
    Write-Host '=== Удаление постоянного сервиса PawnIO ===' -ForegroundColor Cyan
    $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($svc) {
        if ($svc.Status -eq 'Running') {
            Write-Host "  останавливаю $ServiceName..."
            & sc.exe stop $ServiceName | Out-Null
            Start-Sleep -Seconds 1
        }
        Write-Host "  удаляю сервис $ServiceName..."
        & sc.exe delete $ServiceName | Out-Null
    } else {
        Write-Host "  сервис $ServiceName не зарегистрирован — пропускаю"
    }
    if (Test-Path $DriverDestPath) {
        Write-Host "  удаляю файл $DriverDestPath..."
        Remove-Item $DriverDestPath -Force
    }
    Write-Host 'Готово — постоянная регистрация снята.' -ForegroundColor Green
    Write-Host '(Сам PawnIO MSI не тронут — его удалять только через Установку программ.)' -ForegroundColor DarkGray
    Write-Host ''
    if (-not $NoPrompt) {
        Write-Host '[Enter] чтобы закрыть окно...' -ForegroundColor DarkGray
        [void](Read-Host)
    }
    exit 0
}

# ───── Install-ветка ─────
Write-LogMsg '=== Установка постоянного сервиса PawnIO ===' Cyan

# Idempotent fast-path: если PawnIO_setup.exe v2.2.0+ уже зарегистрировал сервис
# с Auto-start + Running, ничего не делаем — переписывание binPath на
# DriverStore-путь только всё ломает (драйвер активен, можно получить лок).
$existing0 = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing0 -and $existing0.StartType -eq 'Automatic' -and $existing0.Status -eq 'Running') {
    Write-LogMsg "PawnIO сервис уже Auto + Running — ничего не делаю (idempotent skip)." Green
    if (-not $NoPrompt) {
        Write-Host '[Enter] чтобы закрыть окно...' -ForegroundColor DarkGray
        [void](Read-Host)
    }
    exit 0
}

# 1. Найти PawnIO.sys в DriverStore (туда его положил PawnIO MSI).
Write-LogMsg "[1/4] Ищу $DriverFileName в Windows DriverStore..."
$repoPath = 'C:\Windows\System32\DriverStore\FileRepository'
$candidates = Get-ChildItem -Path $repoPath -Filter 'pawnio.inf_amd64_*' -Directory -ErrorAction SilentlyContinue
if (-not $candidates) {
    Write-LogMsg '✗ pawnio.inf не найден в DriverStore.' Red
    Write-LogMsg '  Возможно PawnIO_setup.exe не отработал (антивирус, ACL, или не запустился).' Yellow
    Write-LogMsg '  Установи PawnIO вручную: https://pawnio.eu' Yellow
    exit 1
}
# Берём самую новую версию по дате (если несколько копий)
$driverDir = $candidates | Sort-Object LastWriteTime -Descending | Select-Object -First 1
$driverSrc = Join-Path $driverDir.FullName $DriverFileName
if (-not (Test-Path $driverSrc)) {
    Write-LogMsg "✗ $DriverFileName не найден в $($driverDir.FullName)" Red
    exit 1
}
Write-LogMsg "      источник: $driverSrc" DarkGray

# 2. Если сервис уже существует — проверить state и start type.
Write-LogMsg "[2/4] Проверяю существующий сервис $ServiceName..."
$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    Write-LogMsg "      найден: Status=$($existing.Status), Start=$($existing.StartType)" DarkGray
    if ($existing.StartType -ne 'Automatic') {
        Write-LogMsg '      перевожу на Automatic...'
        & sc.exe config $ServiceName start= auto | Out-Null
    }
    if ($existing.Status -ne 'Running') {
        Write-LogMsg '      запускаю...'
        & sc.exe start $ServiceName 2>&1 | ForEach-Object { Write-LogMsg "        $_" }
    }
    Write-LogMsg '      OK' Green
    Write-Host 'Сервис уже на месте. Готово.' -ForegroundColor Green
    Write-Host ''
    if (-not $NoPrompt) {
        Write-Host '[Enter] чтобы закрыть окно...' -ForegroundColor DarkGray
        [void](Read-Host)
    }
    exit 0
}
Write-LogMsg '      не зарегистрирован — создам новый'

# 3. Скопировать драйвер в System32\drivers (стабильный путь, не зависит
#    от хешированного имени папки DriverStore — оно меняется при
#    обновлении PawnIO).
Write-LogMsg "[3/4] Копирую $DriverFileName в $DriverDestPath..."
Copy-Item -LiteralPath $driverSrc -Destination $DriverDestPath -Force
$size = (Get-Item $DriverDestPath).Length
Write-LogMsg "      $size байт" Green

# 4. Зарегистрировать сервис с start=auto и стартануть.
Write-LogMsg "[4/4] Регистрирую и стартую сервис $ServiceName..."
$scOutput = & sc.exe create $ServiceName binPath= "$DriverDestPath" type= kernel start= auto DisplayName= 'PawnIO Driver (apexcore persistent)' 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-LogMsg "✗ sc.exe create упал (код $LASTEXITCODE):" Red
    $scOutput | ForEach-Object { Write-LogMsg "  $_" }
    exit 1
}
$scStart = & sc.exe start $ServiceName 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-LogMsg "✗ sc.exe start упал (код $LASTEXITCODE):" Red
    $scStart | ForEach-Object { Write-LogMsg "  $_" }
    Write-LogMsg ''
    Write-LogMsg 'Возможные причины:' Yellow
    Write-LogMsg '  * Secure Boot блокирует загрузку — но PawnIO.sys подписан WHQL,'
    Write-LogMsg '    такого быть не должно. Проверь через `signtool verify /v ...`'
    Write-LogMsg '  * Существует другой инстанс PawnIO от MSI — попробуй Uninstall'
    Write-LogMsg '    и переустанови PawnIO.'
    exit 1
}
Write-Host '      OK' -ForegroundColor Green
Write-Host ''
Write-Host 'Готово! Сервис PawnIO зарегистрирован постоянно.' -ForegroundColor Green
Write-Host 'После перезагрузки он стартует автоматически.' -ForegroundColor Green
Write-Host ''
Write-Host 'Проверь:' -ForegroundColor Cyan
Write-Host '  apexcore doctor   # CPU должен показать ✓ LibreHardwareMonitor (DTS ядер CPU)'
Write-Host ''
Write-Host 'Откат:' -ForegroundColor DarkGray
Write-Host '  .\scripts\install_pawnio_service.ps1 -Uninstall'
Write-Host ''
if (-not $NoPrompt) {
    Write-Host '[Enter] чтобы закрыть окно...' -ForegroundColor DarkGray
    [void](Read-Host)
}
