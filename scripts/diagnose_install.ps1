# Полная диагностика установки apexcore на Windows.
#
# Запуск (новая PowerShell, обычный user, НЕ от админа):
#   pwsh -File diagnose_install.ps1
#   # или
#   powershell.exe -ExecutionPolicy Bypass -File diagnose_install.ps1
#
# Результат: $env:USERPROFILE\apexcore-diag.txt
# Скрипт read-only — ничего не ставит, не удаляет, не меняет реестр/сервисы.

[CmdletBinding()]
param(
    [string]$OutputFile = (Join-Path $env:USERPROFILE 'apexcore-diag.txt')
)

$ErrorActionPreference = 'Continue'
$WarningPreference = 'SilentlyContinue'

$lines = New-Object System.Collections.ArrayList
function Add-Line { param($Text); [void]$lines.Add($Text) }
function Add-Section { param($Title); Add-Line ''; Add-Line ('=' * 70); Add-Line "  $Title"; Add-Line ('=' * 70) }

Add-Line "apexcore install diagnostic"
Add-Line "сгенерировано: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Add-Line "хост: $env:COMPUTERNAME"
Add-Line "user: $env:USERNAME"
Add-Line "Windows: $([System.Environment]::OSVersion.VersionString)"
$isAdmin = ([Security.Principal.WindowsPrincipal]::new(
    [Security.Principal.WindowsIdentity]::GetCurrent())).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
Add-Line "запущен под админом: $isAdmin"

# ---------- 1. CPU/GPU контекст ----------
Add-Section '1. CPU / GPU / motherboard (для понимания контекста)'
try {
    $cpu = Get-CimInstance Win32_Processor -ErrorAction SilentlyContinue | Select-Object -First 1
    Add-Line "CPU: $($cpu.Name)"
    Add-Line "  ядра: $($cpu.NumberOfCores) / потоки: $($cpu.NumberOfLogicalProcessors) / частота: $($cpu.MaxClockSpeed) MHz"
} catch { Add-Line "  Win32_Processor: $($_.Exception.Message)" }
try {
    $gpus = Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue
    foreach ($g in $gpus) { Add-Line "GPU: $($g.Name) (driver $($g.DriverVersion))" }
} catch {}
try {
    $bb = Get-CimInstance Win32_BaseBoard -ErrorAction SilentlyContinue | Select-Object -First 1
    Add-Line "Motherboard: $($bb.Manufacturer) $($bb.Product)"
} catch {}

# ---------- 2. PawnIO MSI ----------
Add-Section '2. PawnIO MSI (драйвер для CPU temp / Vcore / Ppt)'
$pawnioFound = $false

# 2a. winget
Add-Line '— winget list namazso.PawnIO —'
try {
    $w = winget list --id namazso.PawnIO --disable-interactivity 2>&1 | Out-String
    # фильтруем спиннер/прогресс-бары и пустые декоративные строки
    $w.TrimEnd() -split "`r?`n" |
        Where-Object {
            $_ -and
            $_ -notmatch '^\s*[\|\\\/\-]\s*$' -and
            $_ -notmatch '^[\s█▒]+$' -and
            $_ -notmatch '^\s*[\s█▒]+\s+\d+\s*(%|KB|MB)' -and
            $_ -notmatch '^Downloading\s' -and
            $_ -notmatch '^\s+\d+%\s*$'
        } |
        ForEach-Object { Add-Line "  $_" }
    if ($w -match 'PawnIO') { $pawnioFound = $true }
} catch { Add-Line "  winget недоступен: $($_.Exception.Message)" }

# 2b. реестр Uninstall
Add-Line '— реестр HKLM Uninstall —'
$found = $false
foreach ($key in 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
                 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*') {
    Get-ItemProperty $key -ErrorAction SilentlyContinue |
        Where-Object { $_.DisplayName -match 'PawnIO' } |
        ForEach-Object {
            Add-Line "  DisplayName: $($_.DisplayName)"
            Add-Line "  DisplayVersion: $($_.DisplayVersion)"
            Add-Line "  InstallLocation: $($_.InstallLocation)"
            Add-Line "  UninstallString: $($_.UninstallString)"
            $found = $true
            $pawnioFound = $true
        }
}
if (-not $found) { Add-Line '  (не найден в HKLM Uninstall)' }

# 2c. DriverStore — наш install_pawnio_service.ps1 ищет именно тут
Add-Line '— DriverStore (наш скрипт ищет pawnio.inf_amd64_*) —'
$dsDir = 'C:\Windows\System32\DriverStore\FileRepository'
if (Test-Path $dsDir) {
    $found = Get-ChildItem $dsDir -Filter 'pawnio.inf_amd64_*' -Directory -ErrorAction SilentlyContinue
    if ($found) {
        foreach ($d in $found) {
            Add-Line "  $($d.FullName)"
            $sys = Join-Path $d.FullName 'PawnIO.sys'
            if (Test-Path $sys) {
                $sz = (Get-Item $sys).Length
                Add-Line "    └─ PawnIO.sys: $sz байт"
            } else {
                Add-Line '    └─ PawnIO.sys не найден ВНУТРИ папки!'
            }
        }
    } else {
        Add-Line '  (pawnio.inf_amd64_* не найден — это критично если PawnIO «установлен»)'
    }
}

# 2d. C:\Program Files\PawnIO
Add-Line '— C:\Program Files\PawnIO —'
if (Test-Path 'C:\Program Files\PawnIO') {
    Get-ChildItem 'C:\Program Files\PawnIO' -ErrorAction SilentlyContinue |
        ForEach-Object { Add-Line "  $($_.Name) ($($_.Length) байт)" }
} else { Add-Line '  (не существует)' }

# 2e. PawnIO service в SCM
Add-Line '— PawnIO service в SCM —'
$svc = Get-Service -Name PawnIO -ErrorAction SilentlyContinue
if ($svc) {
    Add-Line "  Name: $($svc.Name), Status: $($svc.Status), StartType: $($svc.StartType)"
    Add-Line '— sc.exe qc PawnIO —'
    sc.exe qc PawnIO 2>&1 | ForEach-Object { Add-Line "  $_" }
} else { Add-Line '  PawnIO сервис не найден' }

# ---------- 3. apexcore install path ----------
Add-Section '3. apexcore install location'
$apexcoreInstallDir = $null
foreach ($key in 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
                 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*',
                 'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*') {
    Get-ItemProperty $key -ErrorAction SilentlyContinue |
        Where-Object { $_.DisplayName -match 'ApexCore|apexcore' } |
        ForEach-Object {
            Add-Line "  DisplayName: $($_.DisplayName)"
            Add-Line "  DisplayVersion: $($_.DisplayVersion)"
            Add-Line "  InstallLocation: $($_.InstallLocation)"
            Add-Line "  UninstallString: $($_.UninstallString)"
            if (-not $apexcoreInstallDir) { $apexcoreInstallDir = $_.InstallLocation }
        }
}
if (-not $apexcoreInstallDir) {
    Add-Line '  не найден в реестре — пробую стандартные пути'
    foreach ($p in "$env:LOCALAPPDATA\Programs\ApexCore",
                   'C:\Program Files\ApexCore',
                   'C:\Program Files (x86)\ApexCore') {
        if (Test-Path $p) { $apexcoreInstallDir = $p; Add-Line "  найден: $p"; break }
    }
}
if ($apexcoreInstallDir -and (Test-Path $apexcoreInstallDir)) {
    Add-Line ''
    Add-Line "содержимое $apexcoreInstallDir (верхний уровень):"
    Get-ChildItem $apexcoreInstallDir -ErrorAction SilentlyContinue |
        Select-Object -First 30 |
        ForEach-Object {
            $tag = if ($_.PSIsContainer) { '<DIR>' } else { "$($_.Length) б" }
            Add-Line "  $tag  $($_.Name)"
        }
    foreach ($must in 'apexcore.exe','apexcore-sensord','scripts','dotnet') {
        $p = Join-Path $apexcoreInstallDir $must
        Add-Line "  └─ $must : $(if (Test-Path $p){'есть'} else {'НЕТ'})"
    }
}

# ---------- 4. apexcore_sensord service ----------
Add-Section '4. apexcore_sensord service (с подчёркиванием!)'
$svc = Get-Service -Name apexcore_sensord -ErrorAction SilentlyContinue
if ($svc) {
    Add-Line "  Name: $($svc.Name), Status: $($svc.Status), StartType: $($svc.StartType)"
    Add-Line "  DisplayName: $($svc.DisplayName)"
    Add-Line '— sc.exe qc apexcore_sensord —'
    sc.exe qc apexcore_sensord 2>&1 | ForEach-Object { Add-Line "  $_" }
} else {
    Add-Line '  apexcore_sensord сервис НЕ зарегистрирован'
}
Add-Line ''
Add-Line '— реестр HKLM\SYSTEM\...\Services\apexcore_sensord —'
$svcReg = 'HKLM:\SYSTEM\CurrentControlSet\Services\apexcore_sensord'
if (Test-Path $svcReg) {
    Get-ItemProperty $svcReg -ErrorAction SilentlyContinue |
        Select-Object ImagePath, Start, ObjectName |
        Format-List | Out-String |
        ForEach-Object { ($_ -split "`r?`n") | Where-Object { $_ } | ForEach-Object { Add-Line "  $_" } }
    if (Test-Path "$svcReg\Environment") {
        Add-Line '  Environment:'
        (Get-ItemProperty "$svcReg\Environment" -ErrorAction SilentlyContinue).PSObject.Properties |
            Where-Object { $_.Name -notlike 'PS*' } |
            ForEach-Object { Add-Line "    $($_.Name)=$($_.Value)" }
    }
} else { Add-Line '  ключа нет' }

# Все сервисы с pawn/bench/sensord в имени — на случай других вариантов
Add-Line ''
Add-Line '— все сервисы соответствующие *pawn*|*bench*|*sensord* —'
$all = Get-Service -ErrorAction SilentlyContinue | Where-Object {
    $_.Name -match 'pawn|bench|sensord' -or $_.DisplayName -match 'PawnIO|apexcore'
}
if ($all) {
    $all | ForEach-Object {
        Add-Line "  $($_.Name) | $($_.Status) | $($_.StartType) | $($_.DisplayName)"
    }
} else { Add-Line '  (ничего не найдено)' }

# ---------- 5. sensord.log ----------
Add-Section '5. sensord.log (что писал сервис при старте)'
$sensordLog = Join-Path $env:PROGRAMDATA 'apexcore\sensord.log'
Add-Line "путь: $sensordLog"
if (Test-Path $sensordLog) {
    Add-Line "размер: $((Get-Item $sensordLog).Length) байт"
    Add-Line '— tail 60 —'
    Get-Content $sensordLog -Tail 60 -ErrorAction SilentlyContinue |
        ForEach-Object { Add-Line "  $_" }
} else { Add-Line '  ФАЙЛА НЕТ → сервис никогда не стартовал или упал до открытия лога' }

# Тут же — наши новые логи (если в v11 они есть)
foreach ($extra in 'install_pawnio.log','install_sensord.log') {
    $p = Join-Path $env:PROGRAMDATA "apexcore\$extra"
    Add-Line ''
    Add-Line "доп. лог: $p"
    if (Test-Path $p) {
        Add-Line '— tail 50 —'
        Get-Content $p -Tail 50 -ErrorAction SilentlyContinue | ForEach-Object { Add-Line "  $_" }
    } else { Add-Line '  (не существует)' }
}

# ---------- 6. Inno engine log ----------
Add-Section '6. Inno engine [Run] log из %TEMP%'
$candidates = Get-ChildItem $env:TEMP -Filter 'apexcore-setup-*.log' -File -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 3
if ($candidates) {
    foreach ($f in $candidates) {
        Add-Line ''
        Add-Line "файл: $($f.FullName)"
        Add-Line "дата: $($f.LastWriteTime), размер: $($f.Length) байт"
        Add-Line '— tail 100 —'
        Get-Content $f.FullName -Tail 100 -ErrorAction SilentlyContinue | ForEach-Object { Add-Line "  $_" }
        Add-Line ('-' * 70)
    }
} else {
    Add-Line '  apexcore-setup-*.log не найден в %TEMP% — установщик мог не запуститься, лог удалён, или путь нестандартный'
}

# ---------- 7. Event Log Application ----------
Add-Section '7. Windows Event Log — события про python/apexcore/sensord/Service Control Manager'
try {
    $events = Get-WinEvent -FilterHashtable @{LogName='Application'; StartTime=(Get-Date).AddDays(-2)} -ErrorAction SilentlyContinue |
        Where-Object {
            $_.ProviderName -match 'python|apexcore|sensord|Service Control Manager' -or
            $_.Message -match 'apexcore_sensord|PawnIO'
        } |
        Select-Object -First 15
    if ($events) {
        foreach ($e in $events) {
            Add-Line "  [$($e.LevelDisplayName)] $($e.TimeCreated) — $($e.ProviderName) (id $($e.Id))"
            $msg = ($e.Message -split "`r?`n" | Select-Object -First 3) -join ' | '
            Add-Line "    $msg"
        }
    } else { Add-Line '  (за последние 48 ч нет релевантных событий)' }
} catch { Add-Line "  Get-WinEvent: $($_.Exception.Message)" }

# ---------- 8. Системная безопасность ----------
Add-Section '8. Системная безопасность (что может блокировать PawnIO/WinRing0)'
try {
    $vbs = (Get-CimInstance -Namespace 'root\Microsoft\Windows\DeviceGuard' -ClassName Win32_DeviceGuard -ErrorAction SilentlyContinue)
    if ($vbs) {
        Add-Line "  VBS Available: $($vbs.VirtualizationBasedSecurityStatus)  (0=off, 1=on/configured, 2=running)"
        Add-Line "  HVCI Code Integrity (Memory Integrity): $($vbs.CodeIntegrityPolicyEnforcementStatus)  (0=off, 1=audit, 2=enforced)"
        Add-Line "  HVCI Running services: $($vbs.SecurityServicesRunning -join ',')  (1=Credential Guard, 2=HVCI)"
    }
} catch {}
try {
    $sac = Get-MpPreference -ErrorAction SilentlyContinue
    if ($sac) {
        Add-Line "  Defender RealTime: $($sac.DisableRealtimeMonitoring -eq $false)"
        Add-Line "  Defender TamperProtection: $((Get-MpComputerStatus).IsTamperProtected)"
    }
    $smartApp = Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy' -Name 'VerifiedAndReputablePolicyState' -ErrorAction SilentlyContinue
    if ($smartApp) {
        Add-Line "  Smart App Control: $($smartApp.VerifiedAndReputablePolicyState)  (0=off, 1=on, 2=eval)"
    }
} catch {}

# ---------- 9. apexcore в PATH ----------
Add-Section '9. apexcore в PATH (для CommandNotFoundException)'
$gc = Get-Command apexcore -ErrorAction SilentlyContinue
if ($gc) { Add-Line "  apexcore найден: $($gc.Source)" }
else { Add-Line '  apexcore НЕ найден в текущем $env:Path (нужна новая PowerShell-сессия после установки)' }

$systemPath = [System.Environment]::GetEnvironmentVariable('Path','Machine')
if ($apexcoreInstallDir -and $systemPath -match [Regex]::Escape($apexcoreInstallDir)) {
    Add-Line "  установочный путь $apexcoreInstallDir ЕСТЬ в системном PATH"
} elseif ($apexcoreInstallDir) {
    Add-Line "  установочный путь $apexcoreInstallDir НЕТ в системном PATH"
}

# ---------- 10. Smoke: shared memory snapshot ----------
Add-Section '10. Smoke-тест: чтение Global\benchkit_sensors snapshot'
$apexcoreExe = $null
if ($apexcoreInstallDir) {
    $candidate = Join-Path $apexcoreInstallDir 'apexcore.exe'
    if (Test-Path $candidate) { $apexcoreExe = $candidate }
}
if (-not $apexcoreExe -and $gc) { $apexcoreExe = $gc.Source }
if ($apexcoreExe) {
    Add-Line "запускаю: $apexcoreExe doctor --json"
    try {
        $out = & $apexcoreExe doctor --json 2>&1 | Out-String
        # ограничим вывод, нам интересен факт что что-то выводит
        ($out -split "`r?`n" | Select-Object -First 60) | ForEach-Object { Add-Line "  $_" }
    } catch { Add-Line "  $($_.Exception.Message)" }
} else { Add-Line '  apexcore.exe не найден — пропускаю' }

# ---------- save ----------
Add-Line ''
Add-Line ('=' * 70)
Add-Line "конец отчёта · $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Add-Line ('=' * 70)

$text = $lines -join "`r`n"
$text | Out-File -FilePath $OutputFile -Encoding utf8

Write-Host ''
Write-Host "Отчёт сохранён: $OutputFile" -ForegroundColor Green
Write-Host "Размер: $((Get-Item $OutputFile).Length) байт"
Write-Host ''
Write-Host "Пришли этот файл (или скопируй содержимое в чат)." -ForegroundColor Cyan
