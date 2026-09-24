$ErrorActionPreference = "Stop"
$tools = Split-Path -Parent $MyInvocation.MyCommand.Path
$desktop = [Environment]::GetFolderPath('Desktop')
$projectExe = Join-Path $tools 'ChatExportHub.exe'
$shortcutPath = Join-Path $desktop 'Chat Export Hub.lnk'
$diyShortcutPath = Join-Path $desktop 'Apps\DIY\Chat Export Hub.lnk'

$ico = Join-Path $tools 'assets\chat_export_hub.ico'
if (-not (Test-Path -LiteralPath $ico)) {
    throw "Icon missing: $ico"
}

& "$env:APPDATA\Python\Python314\Scripts\pyinstaller.exe" --noconfirm --clean --windowed --onefile `
  --name ChatExportHub --icon $ico --distpath "$tools\dist" --workpath "$tools\build" --specpath $tools `
  --hidden-import chat_export_agents --hidden-import chat_export_i18n --hidden-import chat_export_search_index `
  --add-data "$tools\chat_export_agents.py;." --add-data "$tools\chat_export_i18n.py;." `
  --add-data "$tools\chat_export_search_index.py;." `
  --add-data "$ico;." `
  "$tools\chat_export_hub.py"

Copy-Item "$tools\dist\ChatExportHub.exe" $projectExe -Force

# Do not place a full .exe on the Desktop — only a shortcut
$desktopExe = Join-Path $desktop 'ChatExportHub.exe'
if (Test-Path -LiteralPath $desktopExe) {
    Remove-Item -LiteralPath $desktopExe -Force -ErrorAction SilentlyContinue
}

# Re-sign project exe
& powershell -NoProfile -ExecutionPolicy Bypass -File "$tools\create_codesign_cert_and_sign.ps1" -Targets @($projectExe)

# Refresh Desktop + DIY shortcuts (never dump the full exe on Desktop)
$wsh = New-Object -ComObject WScript.Shell
$icoFile = Join-Path $tools 'assets\chat_export_hub_v2.ico'
if (-not (Test-Path $icoFile)) { $icoFile = Join-Path $tools 'assets\chat_export_hub.ico' }
foreach ($link in @($shortcutPath, $diyShortcutPath)) {
    $linkDir = Split-Path -Parent $link
    if (-not (Test-Path -LiteralPath $linkDir)) {
        New-Item -ItemType Directory -Path $linkDir -Force | Out-Null
    }
    $sc = $wsh.CreateShortcut($link)
    $sc.TargetPath = $projectExe
    $sc.WorkingDirectory = $tools
    $sc.WindowStyle = 1
    $sc.Description = 'Chat Export Hub — multi-agent live chat export monitor'
    $sc.IconLocation = "$icoFile,0"
    $sc.Save()
}
[System.Runtime.InteropServices.Marshal]::ReleaseComObject($wsh) | Out-Null

Write-Host "Built + signed: $projectExe"
Write-Host "Shortcut:       $shortcutPath"
Write-Host "DIY shortcut:   $diyShortcutPath"

