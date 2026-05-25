<#
.SYNOPSIS
    Rebuild the Claude Usage tray widget safely.

.DESCRIPTION
    Wraps the PyInstaller rebuild so it can't repeat the two footguns that
    have bitten us before:

      1. Building from tray_widget.py regenerates ClaudeUsage.spec from
         scratch and drops `datas=[('config.json', '.')]`, so config.json
         stops being bundled. This script ALWAYS builds from the committed
         spec and never passes the .py.

      2. A running ClaudeUsage.exe locks files under dist\, making PyInstaller's
         COLLECT step fail with "Access is denied". This script stops any
         running instance first.

    After building it verifies the bundle actually contains config.json and the
    exe, and fails loudly if not.

.PARAMETER Run
    Relaunch dist\ClaudeUsage\ClaudeUsage.exe after a successful build.

.EXAMPLE
    .\build.ps1
    .\build.ps1 -Run
#>
[CmdletBinding()]
param(
    [switch]$Run
)

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

$spec    = 'ClaudeUsage.spec'
$exePath = 'dist\ClaudeUsage\ClaudeUsage.exe'
$bundledConfig = 'dist\ClaudeUsage\_internal\config.json'

if (-not (Test-Path $spec)) {
    throw "Cannot find $spec in $PSScriptRoot - run this from the repo root."
}

# --- 1. Stop any running instance so it can't lock dist\ -------------------
$running = Get-Process -Name ClaudeUsage -ErrorAction SilentlyContinue
if ($running) {
    Write-Host "Stopping running ClaudeUsage.exe (PID $($running.Id -join ', '))..."
    $running | Stop-Process -Force
    # Wait for the OS to actually release the file handles before building.
    for ($i = 0; $i -lt 20 -and (Get-Process -Name ClaudeUsage -ErrorAction SilentlyContinue); $i++) {
        Start-Sleep -Milliseconds 200
    }
    if (Get-Process -Name ClaudeUsage -ErrorAction SilentlyContinue) {
        throw "ClaudeUsage.exe is still running - cannot rebuild while dist\ is locked."
    }
}

# --- 2. Build from the spec (NEVER from tray_widget.py) --------------------
# PyInstaller writes its progress to stderr; under $ErrorActionPreference=Stop
# Windows PowerShell would treat that as a terminating error even on a clean
# build. Relax error handling around the native call and trust the exit code.
Write-Host "Building from $spec ..."
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
pyinstaller --noconfirm $spec
$buildExit = $LASTEXITCODE
$ErrorActionPreference = $prevEAP
if ($buildExit -ne 0) {
    throw "PyInstaller failed with exit code $buildExit."
}

# --- 3. Verify the bundle -------------------------------------------------
$missing = @()
if (-not (Test-Path $exePath))       { $missing += $exePath }
if (-not (Test-Path $bundledConfig)) { $missing += $bundledConfig }
if ($missing.Count -gt 0) {
    throw "Build produced an incomplete bundle - missing:`n  $($missing -join "`n  ")"
}
Write-Host "OK: $exePath and bundled config.json present." -ForegroundColor Green

# --- 4. Optional relaunch -------------------------------------------------
if ($Run) {
    Write-Host "Launching $exePath ..."
    Start-Process -FilePath (Resolve-Path $exePath)
}

Write-Host "Build complete." -ForegroundColor Green
Write-Host "Note: if you moved/renamed the exe, re-run install_start_menu.ps1 to refresh the Start Menu shortcut."
