param([switch]$ValidateOnly)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'runtime-common.ps1')

$projectRoot = Get-LoungeProjectRoot
$runtimeDirectory = Get-LoungeRuntimeDirectory -ProjectRoot $projectRoot
$logsDirectory = Join-Path $projectRoot 'logs'
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$supervisorScript = Join-Path $PSScriptRoot 'supervisor.ps1'

[void](Import-LoungeEnvironment -ProjectRoot $projectRoot)
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Missing independent lounge virtual environment: $python"
}
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'config\visitor-lounge.toml') -PathType Leaf)) {
    throw "Missing visitor lounge configuration."
}
[void](New-Item -ItemType Directory -Path $runtimeDirectory -Force)
[void](New-Item -ItemType Directory -Path $logsDirectory -Force)

foreach ($record in @('visitor', 'visitor.launcher', 'admin', 'admin.launcher', 'supervisor')) {
    $role = $record.Split('.')[0]
    $pidFile = Join-Path $runtimeDirectory "$record.pid"
    if (-not (Test-Path -LiteralPath $pidFile -PathType Leaf)) { continue }
    $recordedPid = Read-LoungePid -PidFile $pidFile
    if ($null -ne (Get-LoungeProcessInfo -ProcessId $recordedPid)) {
        throw "Recorded $role PID $recordedPid is still live; refusing duplicate start."
    }
    Remove-Item -LiteralPath $pidFile
}

if ($ValidateOnly) {
    Write-Output 'Visitor Lounge validation passed.'
    exit 0
}

$startupTimeoutSeconds = 10
$configuredTimeout = [Environment]::GetEnvironmentVariable(
    'VISITOR_LOUNGE_STARTUP_TIMEOUT_SECONDS',
    'Process'
)
if (-not [string]::IsNullOrWhiteSpace($configuredTimeout)) {
    $parsedTimeout = 0
    if (-not [int]::TryParse($configuredTimeout, [ref]$parsedTimeout) -or
        $parsedTimeout -lt 1 -or $parsedTimeout -gt 300) {
        throw 'VISITOR_LOUNGE_STARTUP_TIMEOUT_SECONDS must be an integer from 1 to 300.'
    }
    $startupTimeoutSeconds = $parsedTimeout
}

function Test-LoungeListenerOwner {
    param(
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][int]$ProcessId
    )
    if ($null -ne (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue)) {
        try {
            $listeners = @(Get-NetTCPConnection `
                -State Listen `
                -LocalAddress '127.0.0.1' `
                -LocalPort $Port `
                -ErrorAction Stop)
            return $listeners.Count -eq 1 -and
                [int]$listeners[0].OwningProcess -eq $ProcessId
        } catch {
        }
    }
    $netstat = & "$env:SystemRoot\System32\netstat.exe" -ano -p tcp 2>$null
    if ($LASTEXITCODE -ne 0) { return $false }
    $matchingOwners = @()
    $pattern = '^\s*TCP\s+127\.0\.0\.1:' + $Port +
        '\s+\S+\s+LISTENING\s+(\d+)\s*$'
    foreach ($line in $netstat) {
        if ([string]$line -match $pattern) {
            $matchingOwners += [int]$Matches[1]
        }
    }
    return $matchingOwners.Count -eq 1 -and $matchingOwners[0] -eq $ProcessId
}

$powerShell = Join-Path $PSHOME 'powershell.exe'
$arguments = @(
    '-NoProfile',
    '-ExecutionPolicy', 'Bypass',
    '-File', (ConvertTo-LoungeQuotedArgument $supervisorScript),
    '-ProjectRoot', (ConvertTo-LoungeQuotedArgument $projectRoot),
    '-LoungeIdentity', 'visitor_lounge'
)
$supervisor = Start-Process -FilePath $powerShell `
    -ArgumentList $arguments `
    -WorkingDirectory $projectRoot `
    -WindowStyle Hidden `
    -PassThru
Write-LoungePid -PidFile (Join-Path $runtimeDirectory 'supervisor.pid') -ProcessId $supervisor.Id

$deadline = [DateTime]::UtcNow.AddSeconds($startupTimeoutSeconds)
$ready = $false
try {
    while ([DateTime]::UtcNow -lt $deadline) {
        $supervisor.Refresh()
        if ($supervisor.HasExited) {
            throw 'Visitor Lounge supervisor exited before startup completed.'
        }

        $processesReady = $true
        $recordedPids = @{}
        foreach ($role in @('visitor', 'admin')) {
            $pidFile = Join-Path $runtimeDirectory "$role.pid"
            if (-not (Test-Path -LiteralPath $pidFile -PathType Leaf)) {
                $processesReady = $false
                continue
            }
            $recordedPid = Read-LoungePid -PidFile $pidFile
            $process = Get-LoungeProcessInfo -ProcessId $recordedPid
            if ($null -eq $process) {
                $processesReady = $false
                continue
            }
            if (-not (Test-LoungeProcessIdentity `
                -Process $process `
                -Role $role `
                -ProjectRoot $projectRoot
            )) {
                throw "Refusing unverified $role PID $recordedPid during startup."
            }
            $recordedPids[$role] = $recordedPid
        }
        if ($processesReady -and
            (Test-LoungeListenerOwner `
                -Port 8001 `
                -ProcessId $recordedPids['visitor']) -and
            (Test-LoungeListenerOwner `
                -Port 8002 `
                -ProcessId $recordedPids['admin']) -and
            (Test-LoungeHealthEndpoint `
                -Uri 'http://127.0.0.1:8001/healthz' `
                -ExpectedService 'visitor') -and
            (Test-LoungeHealthEndpoint `
                -Uri 'http://127.0.0.1:8002/healthz' `
                -ExpectedService 'admin')) {
            $ready = $true
            break
        }
        Start-Sleep -Milliseconds 100
    }
} finally {
    if (-not $ready) {
        $rollbackFailures = [Collections.Generic.List[string]]::new()
        foreach ($role in @('visitor', 'admin')) {
            try {
                Sync-LoungeServicePidFromLauncher `
                    -Role $role `
                    -ProjectRoot $projectRoot
            } catch {
                $rollbackFailures.Add($_.Exception.Message)
            }
        }
        foreach ($record in @('visitor', 'visitor.launcher', 'admin', 'admin.launcher', 'supervisor')) {
            $role = $record.Split('.')[0]
            $pidFile = Join-Path $runtimeDirectory "$record.pid"
            if (-not (Test-Path -LiteralPath $pidFile -PathType Leaf)) { continue }
            $recordedPid = $null
            try {
                $recordedPid = Read-LoungePid -PidFile $pidFile
                $process = Get-LoungeProcessInfo -ProcessId $recordedPid
                if ($null -ne $process) {
                    Stop-VerifiedLoungeProcessTree `
                        -ProcessId $recordedPid `
                        -Role $role `
                        -ProjectRoot $projectRoot `
                        -SingleProcess:($record.EndsWith('.launcher'))
                }
                Remove-Item -LiteralPath $pidFile
            } catch {
                if ($null -ne $recordedPid -and
                    $null -eq (Get-LoungeProcessInfo -ProcessId $recordedPid)) {
                    Remove-Item -LiteralPath $pidFile -ErrorAction SilentlyContinue
                } else {
                    $rollbackFailures.Add($_.Exception.Message)
                }
            }
        }
        if ($rollbackFailures.Count -gt 0) {
            throw "Partial startup rollback failed: $($rollbackFailures -join '; ')"
        }
    }
}
if ($ready) {
    Write-Output 'Visitor Lounge started on 127.0.0.1:8001; admin on 127.0.0.1:8002.'
    exit 0
}
throw "Visitor Lounge did not pass both isolated health checks before the startup deadline."
