# Download smartmontools NSIS installer (.exe) to build/bundles/.
# Used by installer.iss [Files] to avoid winget at install time.
#
# Graceful fallback strategy:
#   1. SourceForge with hardcoded version (fast, no third-party deps)
#   2. If SourceForge fails => winget download
#   3. If both fail => warning + exit 0 (NOT throw). Installer is not broken;
#      it falls back to runtime winget install (same as before bundling).
#
# Usage: pwsh -File scripts/fetch_smartmontools.ps1
# Idempotent.
#
# Comments are intentionally ASCII-only to avoid Windows PowerShell 5.1
# parser issues with non-BOM UTF-8 files containing Cyrillic.

$ErrorActionPreference = "Stop"
Set-Location -Path (Split-Path -Parent $PSScriptRoot)

$BundleDir = "build/bundles"
$Target = "$BundleDir/smartmontools.exe"
New-Item -ItemType Directory -Force -Path $BundleDir | Out-Null

if (Test-Path $Target) {
    Write-Host "[skip] $Target already exists"
    return
}

# --- Attempt 1: SourceForge with hardcoded version --------------------------
# Version 7.4 was latest stable as of May 2026. Update when newer ships.
$Version = "7.4"
$Url = "https://downloads.sourceforge.net/project/smartmontools/smartmontools/$Version/smartmontools-$Version.win32-setup.exe"

Write-Host "[1/2] SourceForge smartmontools $Version..."
$sourceForgeOk = $false
try {
    Invoke-WebRequest -Uri $Url -OutFile $Target -TimeoutSec 60 -UserAgent "apexcore-build"
    if ((Test-Path $Target) -and (Get-Item $Target).Length -gt 102400) {
        $sourceForgeOk = $true
    } else {
        # Tiny file = likely HTML 404 page from SF
        if (Test-Path $Target) { Remove-Item $Target -Force -ErrorAction SilentlyContinue }
        Write-Warning "SourceForge returned tiny file (likely 404). Trying winget..."
    }
} catch {
    Write-Warning ("SourceForge fetch failed: " + $_.Exception.Message + ". Trying winget...")
}
if ($sourceForgeOk) {
    $sz = [math]::Round((Get-Item $Target).Length / 1MB, 1)
    Write-Host "[OK] $Target ($sz MB, SourceForge)"
    return
}

# --- Attempt 2: winget download ---------------------------------------------
$winget = Get-Command winget -ErrorAction SilentlyContinue
if ($winget) {
    Write-Host "[2/2] winget download smartmontools.smartmontools..."
    try {
        $WingetTmp = "$BundleDir/_winget_smartmontools"
        Remove-Item -Recurse -Force $WingetTmp -ErrorAction SilentlyContinue
        New-Item -ItemType Directory -Force -Path $WingetTmp | Out-Null
        winget download --id smartmontools.smartmontools `
            --download-directory $WingetTmp `
            --accept-source-agreements --accept-package-agreements 2>&1 | Out-Host
        $found = Get-ChildItem -Path $WingetTmp -Recurse -Filter "*smartmontools*.exe" |
                 Sort-Object Length -Descending | Select-Object -First 1
        if ($found -and $found.Length -gt 102400) {
            Move-Item $found.FullName $Target -Force
            Remove-Item -Recurse -Force $WingetTmp -ErrorAction SilentlyContinue
            $sz = [math]::Round((Get-Item $Target).Length / 1MB, 1)
            Write-Host "[OK] $Target ($sz MB, winget)"
            return
        }
        Remove-Item -Recurse -Force $WingetTmp -ErrorAction SilentlyContinue
        Write-Warning "winget download: matching .exe not found."
    } catch {
        Write-Warning ("winget download failed: " + $_.Exception.Message)
    }
} else {
    Write-Warning "winget not in PATH."
}

# --- Final: bundling skipped, installer.iss falls back to runtime winget ----
Write-Warning ""
Write-Warning "smartmontools bundling skipped. Installer will use runtime winget"
Write-Warning "install (works on Win10/11 with internet at install time)."
Write-Warning "For offline scenario, download smartmontools manually from"
Write-Warning "https://sourceforge.net/projects/smartmontools/ and put it at $Target"
# Intentionally NOT throw -- build_windows.ps1 should continue with remaining steps.
exit 0
