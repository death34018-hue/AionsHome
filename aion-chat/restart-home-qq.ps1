param([switch]$CheckOnly)

# User-run reload helper. Include the old QQ port for upgrades.
$ErrorActionPreference = 'Stop'
$workspacePath = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$pythonPath = Join-Path $workspacePath '.venv\Scripts\python.exe'
$serverPath = Join-Path $workspacePath 'aion-chat'
$targets = @()
$legacyWorkerId = $null
foreach ($servicePort in @(8080, 18788, 8788)) {
    $listener = Get-NetTCPConnection -LocalPort $servicePort -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $listener) { continue }
    $serviceProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
    if ($serviceProcess.CommandLine -notmatch [regex]::Escape($workspacePath) -or
        ($servicePort -eq 8080 -and $serviceProcess.CommandLine -notmatch '(?:main\.py|uvicorn\s+main:app)') -or
        ($servicePort -ne 8080 -and $serviceProcess.CommandLine -notmatch 'qq_social\.worker')) {
        # The legacy port may have been taken by an unrelated app since upgrading.
        if ($servicePort -eq 8788) { continue }
        throw "Port $servicePort belongs to an unexpected process; nothing was stopped."
    }
    $targets += $serviceProcess
    if ($servicePort -eq 8788) { $legacyWorkerId = $serviceProcess.ProcessId }
}
if ($CheckOnly) {
    Write-Host "Verified $($targets.Count) home/QQ service processes. No changes made."
    exit 0
}
# An explicit restart must work even while QQ is replying or its API is unresponsive.
Write-Host 'Reloading home and QQ. Replies in progress may be interrupted; no need to wait for the group to be quiet.'
. (Join-Path $workspacePath 'scripts\home-server-task.ps1')
if (Get-HomeServerTask) {
    if ($legacyWorkerId) { Stop-Process -Id $legacyWorkerId }
    & (Join-Path $workspacePath 'scripts\restart-aionshome.ps1')
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} else {
    foreach ($serviceProcess in $targets) { Stop-Process -Id $serviceProcess.ProcessId }
    $outputPath = Join-Path $serverPath 'data\home-reload.stdout.log'
    $errorPath = Join-Path $serverPath 'data\home-reload.stderr.log'
    Start-Process -FilePath $pythonPath -ArgumentList '-u','main.py' -WorkingDirectory $serverPath -WindowStyle Hidden -RedirectStandardOutput $outputPath -RedirectStandardError $errorPath
}
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    Start-Sleep -Seconds 2
    try {
        if (Test-Path -LiteralPath (Join-Path $serverPath 'data\qq_social\connection.paused')) {
            $null = Invoke-RestMethod 'http://127.0.0.1:8080/api/qq-social/credentials' -TimeoutSec 2
            Write-Host 'Home is ready. QQ retries are paused; open the QQ page and click manual connect.'
            exit 0
        }
        $status = Invoke-RestMethod 'http://127.0.0.1:8080/api/qq-social/status' -TimeoutSec 2
        if ($null -ne $status.settings.bring_home) {
            Write-Host 'Updated services are ready. Refresh the QQ page.'
            exit 0
        }
    } catch { }
}
throw 'Startup needs attention. Check data\home-reload.stderr.log.'
