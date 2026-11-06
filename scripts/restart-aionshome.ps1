param([switch]$CheckOnly)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'home-server-task.ps1')
$workspacePath = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$serverPath = Join-Path $workspacePath 'aion-chat'
$pythonPath = Join-Path $workspacePath '.venv\Scripts\python.exe'
$mainPath = Join-Path $serverPath 'main.py'
$stopScript = Join-Path $PSScriptRoot 'stop-aionshome.ps1'
$mutexName = 'Local\AionsHome-Restart-' + ($workspacePath.ToLowerInvariant() -replace '[^a-z0-9]', '_')
$mutex = New-Object System.Threading.Mutex($false, $mutexName)
$locked = $false
$result = 0

try {
    try { $locked = $mutex.WaitOne(0) }
    catch [System.Threading.AbandonedMutexException] { $locked = $true }
    if (-not $locked) {
        Write-Host 'AionsHome is already restarting. This extra click was ignored.'
        exit 0
    }

    if (-not (Test-Path -LiteralPath $pythonPath)) {
        $pythonPath = (Get-Command python.exe -ErrorAction Stop).Source
    }
    if (-not (Test-Path -LiteralPath $mainPath)) { throw "Missing server: $mainPath" }
    & $pythonPath -c 'import sys, uvicorn; sys.exit(0)'
    if ($LASTEXITCODE -ne 0) { throw 'Python environment is not ready. Run the environment installer first.' }

    $stopArguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $stopScript, '-ServersOnly')
    if ($CheckOnly) { $stopArguments += '-DryRun' }
    & powershell.exe @stopArguments
    if ($LASTEXITCODE -ne 0) { throw 'Old services could not be stopped safely. Restart cancelled.' }
    if ($CheckOnly) { exit 0 }

    $logPath = Join-Path $serverPath 'data'
    $null = New-Item -ItemType Directory -Path $logPath -Force
    $outputPath = Join-Path $logPath 'home-restart.stdout.log'
    $errorPath = Join-Path $logPath 'home-restart.stderr.log'
    $task = Get-HomeServerTask
    $server = $null
    if ($task) {
        Start-HomeServerTask $task
    } else {
        $server = Start-Process -FilePath $pythonPath -ArgumentList @('-u', ('"{0}"' -f $mainPath)) -WorkingDirectory $serverPath -WindowStyle Hidden -RedirectStandardOutput $outputPath -RedirectStandardError $errorPath -PassThru
    }
    Write-Host 'Starting AionsHome...'
    $ready = $false
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        Start-Sleep -Seconds 1
        if ($server) {
            $server.Refresh()
            if ($server.HasExited) { throw "Server exited during startup. See $errorPath" }
        }
        try {
            $response = Invoke-WebRequest 'http://127.0.0.1:8080/' -UseBasicParsing -TimeoutSec 2
            if ($response.StatusCode -eq 200) { $ready = $true; break }
        } catch { }
    }
    if (-not $ready) { throw "Startup is taking longer than expected. See $errorPath. You can run restart again." }
    Write-Host 'AionsHome is ready: http://localhost:8080'
    Write-Host "Logs: $outputPath"
} catch {
    Write-Host ("Restart failed: {0}" -f $_.Exception.Message) -ForegroundColor Red
    $result = 1
} finally {
    if ($locked) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
exit $result
