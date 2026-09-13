[CmdletBinding()]
param(
    [string]$TaskName = 'Claude Code Chat Export Splitter'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$toolDir = Split-Path -Parent $PSCommandPath
$splitter = Join-Path $toolDir 'split_claude_code_exports.py'
if (-not (Test-Path -LiteralPath $splitter -PathType Leaf)) {
    throw "Claude Code splitter not found: $splitter"
}

$homeDir = [Environment]::GetFolderPath('UserProfile')
$pythonw = 'C:\Program Files\PyManager\pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonw -PathType Leaf)) {
    throw "No-console Python launcher not found: $pythonw"
}

$cliOutput = Join-Path $homeDir 'claude_code_chat_live_exports'
$guiOutput = Join-Path $homeDir 'claude_code_gui_chat_live_exports'
$logPath = Join-Path $cliOutput 'watcher.log'
$statusPath = Join-Path $cliOutput 'watcher.status.json'

[void](New-Item -ItemType Directory -Path $cliOutput -Force)
[void](New-Item -ItemType Directory -Path $guiOutput -Force)

$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$userName = $identity.Name
$actionArguments = '"{0}" --role split --interval 15 --quiet --log-file "{1}" --status-file "{2}"' -f `
    $splitter, $logPath, $statusPath
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
    -Description 'Splits the combined Claude Code exports into the CLI agent (terminal + VS Code sessions) and the GUI agent (Claude desktop app sessions) every 15s; the five-minute trigger revives the splitter if it stops.'

[void](Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force)
Start-ScheduledTask -TaskName $TaskName

Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State, Author, Description
Write-Host "CLI output directory: $cliOutput"
Write-Host "GUI output directory: $guiOutput"
Write-Host "Log: $logPath"
Write-Host "Status: $statusPath"
