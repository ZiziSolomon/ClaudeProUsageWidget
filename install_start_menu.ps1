# Installs a Claude Usage shortcut into the current-user Start Menu.
#
# After running, the widget shows up in Start under "Claude Usage" and can
# be right-clicked → Pin to Start. Launching it starts the tray app (and the
# JSONL watcher / HTTP server it embeds).
#
# Run from an ordinary PowerShell prompt:
#   powershell -ExecutionPolicy Bypass -File .\install_start_menu.ps1

$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$exePath    = Join-Path $projectDir "dist\ClaudeUsage\ClaudeUsage.exe"
$iconPath   = Join-Path $projectDir "claude_usage.ico"

# We point the shortcut at the PyInstaller-built ClaudeUsage.exe rather
# than pythonw.exe, because Windows 11's "Other system tray icons" list
# reads FileDescription from the running EXE - pythonw.exe shows up as
# "python", whereas ClaudeUsage.exe shows up as "Claude Usage".
if (-not (Test-Path $exePath)) {
    throw "ClaudeUsage.exe not built yet. Run: pyinstaller --noconfirm --windowed --name ClaudeUsage --icon claude_usage.ico --version-file version_info.txt tray_widget.py"
}

$startMenu  = [Environment]::GetFolderPath('Programs')
$shortcut   = Join-Path $startMenu "Claude Usage.lnk"

$wsh = New-Object -ComObject WScript.Shell
$lnk = $wsh.CreateShortcut($shortcut)
$lnk.TargetPath       = $exePath
$lnk.WorkingDirectory = $projectDir
$lnk.IconLocation     = "$iconPath,0"
$lnk.Description      = "Claude session usage tray widget"
$lnk.WindowStyle      = 7
$lnk.Save()

Write-Host "Installed shortcut: $shortcut"
Write-Host "Open Start, search 'Claude Usage', right-click -> Pin to Start."
