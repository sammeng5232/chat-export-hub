[CmdletBinding()]
param(
    [switch]$Once,
    [ValidateRange(1, 3600)]
    [double]$IntervalSeconds = 30
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$homeDir = [Environment]::GetFolderPath('UserProfile')
$toolDir = Split-Path -Parent $PSCommandPath
$exporter = Join-Path $toolDir 'export_grok_chats_live.py'
$rtk = Join-Path $homeDir 'bin\rtk.exe'
$grokHome = Join-Path $homeDir '.grok'
$grokOutput = Join-Path $homeDir 'grok_chat_live_exports'
$logPath = Join-Path $grokOutput 'watcher.log'
$statusPath = Join-Path $grokOutput 'watcher.status.json'
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)

function Add-WatcherLog {
    param([Parameter(Mandatory)][string]$Message)
    if ((Test-Path -LiteralPath $logPath) -and (Get-Item -LiteralPath $logPath).Length -ge 10MB) {
        for ($index = 4; $index -ge 1; $index--) {
            $older = "$logPath.$index"
            $newer = "$logPath.$($index + 1)"
            if (Test-Path -LiteralPath $older) {
                Move-Item -LiteralPath $older -Destination $newer -Force
            }
        }
        Move-Item -LiteralPath $logPath -Destination "$logPath.1" -Force
    }
    $line = '{0} {1}{2}' -f ([DateTimeOffset]::Now.ToString('o')), $Message, [Environment]::NewLine
    [System.IO.File]::AppendAllText($logPath, $line, $utf8NoBom)
}

function Write-WatcherStatus {
    param(
        [Parameter(Mandatory)][string]$State,
        [int]$ExitCode = 0,
        [string]$Message = ''
    )
    $payload = [ordered]@{
        updated_at = [DateTimeOffset]::Now.ToString('o')
        state = $State
        pid = $PID
        exit_code = $ExitCode
        message = $Message
        exporter = $exporter
        grok_source = $grokHome
        grok_output = $grokOutput
        interval_seconds = $IntervalSeconds
        once = [bool]$Once
    }
    $tmpPath = "$statusPath.tmp"
    [System.IO.File]::WriteAllText($tmpPath, ($payload | ConvertTo-Json -Depth 4), $utf8NoBom)
    Move-Item -LiteralPath $tmpPath -Destination $statusPath -Force
}

[void](New-Item -ItemType Directory -Path $grokOutput -Force)

if (-not (Test-Path -LiteralPath $exporter -PathType Leaf)) {
    throw "Grok chat exporter not found: $exporter"
}

$python = $null
foreach ($candidate in @(
        'C:\Program Files\PyManager\python.exe',
        (Get-Command python -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source)
    )) {
    if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
        $python = $candidate
        break
    }
}
if (-not $python) {
    throw 'Python executable not found'
}

$mutex = [System.Threading.Mutex]::new($false, 'Local\GrokChatExportWatcher')
$hasMutex = $false
$exitCode = 0

try {
    $hasMutex = $mutex.WaitOne(0)
    if (-not $hasMutex) {
        Add-WatcherLog 'start skipped: another watcher already owns the mutex'
        exit 0
    }

    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'

    $exportArgs = @(
        $exporter,
        '--grok-home', $grokHome,
        '--output-dir', $grokOutput,
        '--interval', ([string]::Format([Globalization.CultureInfo]::InvariantCulture, '{0}', $IntervalSeconds))
    )
    if ($Once) {
        $exportArgs += '--once'
    }

    $runner = $python
    $runnerArgs = $exportArgs
    if (Test-Path -LiteralPath $rtk -PathType Leaf) {
        $runner = $rtk
        $runnerArgs = @('python') + $exportArgs
    }

    Add-WatcherLog "watcher starting: $runner $($runnerArgs -join ' ')"
    Write-WatcherStatus -State 'running'
    & $runner @runnerArgs 2>&1 | ForEach-Object {
        $outputLine = [string]$_
        Add-WatcherLog $outputLine
        if ($outputLine -match 'grok tracked=') {
            Write-WatcherStatus -State 'running' -Message $outputLine
        }
    }
    $exitCode = if ($null -eq $LASTEXITCODE) { 0 } else { [int]$LASTEXITCODE }
    if ($exitCode -eq 0) {
        Add-WatcherLog 'watcher exited normally'
        Write-WatcherStatus -State 'completed' -ExitCode 0
    }
    else {
        Add-WatcherLog "watcher exited with code $exitCode"
        Write-WatcherStatus -State 'failed' -ExitCode $exitCode -Message 'Python exporter returned a non-zero exit code.'
    }
}
catch {
    $exitCode = if ($exitCode -ne 0) { $exitCode } else { 1 }
    $message = $_.Exception.Message
    try {
        Add-WatcherLog "watcher failed: $message"
        Write-WatcherStatus -State 'failed' -ExitCode $exitCode -Message $message
    }
    catch {
    }
}
finally {
    if ($hasMutex) {
        $mutex.ReleaseMutex()
    }
    $mutex.Dispose()
}

exit $exitCode
