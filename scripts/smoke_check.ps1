# ApexCore portable smoke check (Windows).
#
# Collects a quick portability report from an arbitrary machine: OS, Python,
# `apexcore` version/info/doctor (sensor backends) and the live /api/hardware
# + /api/system endpoints. No admin required, no browser opened.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File smoke_check.ps1
# Result:
#   .\apexcore_smoke_<host>_<timestamp>.txt   <- send this file back.
#
# Optional env:
#   APEXCORE_BIN          full path to apexcore.exe (default: apexcore from PATH)
#   APEXCORE_SMOKE_PORT   temp webui port for the API probe (default 8799)

$ErrorActionPreference = 'Continue'
$ts    = Get-Date -Format 'yyyyMMdd_HHmmss'
$hostN = $env:COMPUTERNAME
$out   = "apexcore_smoke_${hostN}_${ts}.txt"
$port  = if ($env:APEXCORE_SMOKE_PORT) { $env:APEXCORE_SMOKE_PORT } else { '8799' }
$apex  = if ($env:APEXCORE_BIN) { $env:APEXCORE_BIN } else { 'apexcore' }

function Section($t) { return "`n===== $t =====" }
function Run($exe, $argList) {
  try { return (& $exe @argList 2>&1 | Out-String).TrimEnd() }
  catch { return "[failed: $($_.Exception.Message)]" }
}

$r = @()
$r += "ApexCore smoke check"
$r += "timestamp: $ts"
$r += "host:      $hostN"

$r += Section "OS / runtime"
$os = Get-CimInstance Win32_OperatingSystem -ErrorAction SilentlyContinue
if ($os) { $r += "os:     $($os.Caption) $($os.Version)" }
$r += "arch:   $env:PROCESSOR_ARCHITECTURE"
$r += "python: " + (Run 'python' @('-V'))

$r += Section "apexcore --version"
$r += Run $apex @('--version')
$r += Section "apexcore info"
$r += Run $apex @('info')
$r += Section "apexcore doctor (sensor backends)"
$r += Run $apex @('doctor')

$r += Section "live API probe (temp webui on :$port)"
$proc = $null
try {
  $proc = Start-Process -FilePath $apex -ArgumentList @('webui','--port',"$port") -PassThru -WindowStyle Hidden
} catch {
  $r += "[!] could not start webui: $($_.Exception.Message)"
}
Start-Sleep -Seconds 8
try {
  $hw = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/hardware" -TimeoutSec 8
  $r += "--- GET /api/hardware ---"
  $r += ($hw | ConvertTo-Json -Depth 6)
  $sys = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/system" -TimeoutSec 8
  $r += "--- GET /api/system (cpu/ram/os) ---"
  $r += ($sys | Select-Object cpu_model, ram_total_gb, os_name | ConvertTo-Json)
} catch {
  $r += "[!] API probe failed: $($_.Exception.Message)"
} finally {
  if ($proc -and -not $proc.HasExited) {
    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
  }
}

$r -join "`r`n" | Out-File -FilePath $out -Encoding utf8
Write-Host "Done. Please send back: $((Resolve-Path $out).Path)"
