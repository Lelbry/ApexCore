# Контролируемая деградация Windows 11 для валидации apexcore.
#
# Параметры:
#   -MaxCpu 50          — ограничить максимальную загрузку CPU процентами через powercfg.
#   -Affinity 0x3       — запускать apexcore с маской аффинити (только указанные ядра).
#   -Noise              — запустить фоновую CPU-нагрузку (PowerShell-цикл) на половине ядер.
#   -Reset              — снять ограничения и убить фоновый шум.
#
# Запуск с правами администратора.

param(
    [int]$MaxCpu = 0,
    [string]$Affinity = "",
    [switch]$Noise,
    [switch]$Reset
)

if ($Reset) {
    Write-Host "Снимаю ограничения активной схемы питания"
    powercfg /SETACVALUEINDEX SCHEME_CURRENT SUB_PROCESSOR PROCTHROTTLEMAX 100 | Out-Null
    powercfg /SETDCVALUEINDEX SCHEME_CURRENT SUB_PROCESSOR PROCTHROTTLEMAX 100 | Out-Null
    powercfg /SETACTIVE SCHEME_CURRENT | Out-Null

    Write-Host "Останавливаю фоновый шум (Get-Process -Name apexcore_noise)"
    Get-Process -Name "apexcore_noise" -ErrorAction SilentlyContinue | Stop-Process -Force
    return
}

if ($MaxCpu -gt 0) {
    Write-Host "Ограничиваю PROCTHROTTLEMAX до $MaxCpu%"
    powercfg /SETACVALUEINDEX SCHEME_CURRENT SUB_PROCESSOR PROCTHROTTLEMAX $MaxCpu | Out-Null
    powercfg /SETDCVALUEINDEX SCHEME_CURRENT SUB_PROCESSOR PROCTHROTTLEMAX $MaxCpu | Out-Null
    powercfg /SETACTIVE SCHEME_CURRENT | Out-Null
}

if ($Affinity -ne "") {
    Write-Host "Для запуска с аффинити используйте:"
    Write-Host "  start /AFFINITY $Affinity apexcore bench run --profile balanced"
}

if ($Noise) {
    $coreCount = [Math]::Max(1, [int]([Environment]::ProcessorCount / 2))
    Write-Host "Запускаю $coreCount фоновых нагрузочных потоков (Process: apexcore_noise)"
    for ($i = 0; $i -lt $coreCount; $i++) {
        Start-Process -FilePath "powershell" -ArgumentList @(
            "-WindowStyle", "Hidden",
            "-NoProfile",
            "-Command", "while ($true) { 1..1000000 | ForEach-Object { [math]::Sqrt($_) } | Out-Null }"
        ) -WindowStyle Hidden | Out-Null
    }
}

Write-Host "Готово."
