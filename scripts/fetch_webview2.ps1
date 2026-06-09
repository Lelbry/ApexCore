# Downloads the Microsoft Edge WebView2 Evergreen Bootstrapper (~2 MB) into
# build/bundles/. Bundled into the WebView2 bootstrapper (Bootstrapper.csproj
# Content); if the target machine lacks the WebView2 Runtime, MainWindow runs
# this setup silently before loading the wizard UI (clean Win10 / LTSC / N).
#
# Run: pwsh -File scripts/fetch_webview2.ps1
# Idempotent: skips if already downloaded.

$ErrorActionPreference = "Stop"
Set-Location -Path (Split-Path -Parent $PSScriptRoot)

$BundleDir = "build/bundles"
$Target = "$BundleDir/MicrosoftEdgeWebview2Setup.exe"
New-Item -ItemType Directory -Force -Path $BundleDir | Out-Null

if (Test-Path $Target) {
    Write-Host "[skip] $Target already exists ($(((Get-Item $Target).Length / 1KB).ToString('0')) KB)"
    return
}

# Official Microsoft Evergreen Bootstrapper (stable fwlink, ~2 MB online setup).
$url = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
Write-Host "[1/1] Downloading WebView2 Evergreen Bootstrapper..."
Invoke-WebRequest -Uri $url -OutFile $Target -TimeoutSec 120 -UseBasicParsing
Write-Host "[OK] $Target ($(((Get-Item $Target).Length / 1KB).ToString('0')) KB)"
