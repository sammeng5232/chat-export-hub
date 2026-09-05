# Rebuild GrokExportMonitor.exe
$ErrorActionPreference = "Stop"
$tools = Split-Path -Parent $MyInvocation.MyCommand.Path
& "$env:APPDATA\Python\Python314\Scripts\pyinstaller.exe" --noconfirm --clean --windowed --onefile `
  --name GrokExportMonitor --distpath "$tools\dist" --workpath "$tools\build" --specpath $tools `
  "$tools\grok_export_monitor.py"
Copy-Item "$tools\dist\GrokExportMonitor.exe" "$tools\GrokExportMonitor.exe" -Force
Write-Host "Built: $tools\GrokExportMonitor.exe"
