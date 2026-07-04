# Скачивает последний релизный MSI namazso/PawnIO в build/bundles/.
# Используется installer.iss [Files] чтобы избежать winget при установке.
#
# Запуск: pwsh -File new-app/scripts/fetch_pawnio.ps1
# Идемпотентен: пропускает если уже скачан и хэш совпадает.

$ErrorActionPreference = "Stop"
Set-Location -Path (Split-Path -Parent $PSScriptRoot)

$BundleDir = "build/bundles"
$Target = "$BundleDir/PawnIO_setup.exe"
New-Item -ItemType Directory -Force -Path $BundleDir | Out-Null

if (Test-Path $Target) {
    Write-Host "[skip] $Target уже существует ($(((Get-Item $Target).Length / 1MB).ToString('0.0')) МБ)"
    return
}

# Релизы PawnIO распространяются из репозитория namazso/PawnIO.Setup
# (там лежит NSIS-инсталлер). Сам namazso/PawnIO без публичных релизов.
Write-Host "[1/2] Запрашиваю latest release у namazso/PawnIO.Setup..."
try {
    $apiUrl = "https://api.github.com/repos/namazso/PawnIO.Setup/releases/latest"
    $headers = @{ 'User-Agent' = 'apexcore-build' }
    $rel = Invoke-RestMethod -Uri $apiUrl -Headers $headers -TimeoutSec 30
} catch {
    Write-Host "[!] GitHub API недоступен: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "    Скачайте PawnIO_setup.exe вручную с" -ForegroundColor Yellow
    Write-Host "    https://github.com/namazso/PawnIO.Setup/releases/latest" -ForegroundColor Yellow
    Write-Host "    и положите в $Target" -ForegroundColor Yellow
    throw
}

$asset = $rel.assets | Where-Object { $_.name -match '^PawnIO.*\.exe$' } | Select-Object -First 1
if (-not $asset) {
    throw "В последнем релизе PawnIO.Setup .exe не найден. Проверьте https://github.com/namazso/PawnIO.Setup/releases вручную."
}

Write-Host "[2/2] Скачиваю $($asset.name) v$($rel.tag_name) ($([math]::Round($asset.size / 1MB, 1)) МБ)..."
Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $Target -TimeoutSec 120
Write-Host "[OK] $Target"
