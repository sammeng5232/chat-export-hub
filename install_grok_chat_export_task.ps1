[CmdletBinding()]
param(
    [string]$TaskName = 'Grok Chat Export Watcher'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$toolDir = Split-Path -Parent $PSCommandPath
$exporter = Join-Path $toolDir 'export_grok_chats_live.py'
if (-not (Test-Path -LiteralPath $exporter -PathType Leaf)) {
    throw "Grok chat exporter not found: $exporter"
}

$homeDir = [Environment]::GetFolderPath('UserProfile')
$pythonw = 'C:\Program Files\PyManager\pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonw -PathType Leaf)) {
    throw "No-console Python launcher not found: $pythonw"
}

$grokHome = Join-Path $homeDir '.grok'
$grokOutput = Join-Path $homeDir 'grok_chat_live_exports'
$logPath = Join-Path $grokOutput 'watcher.log'
$statusPath = Join-Path $grokOutput 'watcher.status.json'

[void](New-Item -ItemType Directory -Path $grokOutput -Force)

$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$userName = $identity.Name
$actionArguments = '"{0}" --grok-home "{1}" --output-dir "{2}" --interval 30 --quiet --log-file "{3}" --status-file "{4}"' -f `
    $exporter, $grokHome, $grokOutput, $logPath, $statusPath
$action = New-ScheduledTaskAction -Execute $pythonw -Argument $actionArguments

$triggers = @(
    (New-ScheduledTaskTrigger -AtLogOn -User $userName),
    (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes 5) `
        -RepetitionDuration (New-TimeSpan -Days 3650))
)

$principal = New-ScheduledTaskPrincipal -UserId $userName -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -Compatibility Win8

$task = New-ScheduledTask -Action $action -Trigger $triggers -Principal $principal -Settings $settings `
    -Description 'Continuously exports local Grok CLI session chat_history.jsonl files to ~/grok_chat_live_exports every 30s; the five-minute trigger revives the watcher if it stops.'

[void](Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force)
Start-ScheduledTask -TaskName $TaskName

Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State, Author, Description
Write-Host "Output directory: $grokOutput"
Write-Host "Log: $logPath"
Write-Host "Status: $statusPath"
