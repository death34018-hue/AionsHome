param(
    [Parameter(Mandatory = $true)][string]$ProjectRoot,
    [Parameter(Mandatory = $true)][string]$LoungeIdentity
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'runtime-common.ps1')

$derivedRoot = Get-LoungeProjectRoot
$projectRoot = [IO.Path]::GetFullPath($ProjectRoot)
if ($projectRoot -ine $derivedRoot -or $LoungeIdentity -cne 'visitor_lounge') {
    throw "Invalid lounge supervisor identity."
}
[void](Import-LoungeEnvironment -ProjectRoot $projectRoot)
$runtimeDirectory = Get-LoungeRuntimeDirectory -ProjectRoot $projectRoot
$logsDirectory = Join-Path $projectRoot 'logs'
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
[void](New-Item -ItemType Directory -Path $runtimeDirectory -Force)
[void](New-Item -ItemType Directory -Path $logsDirectory -Force)

function Start-LoungeUvicorn {
    param(
        [Parameter(Mandatory = $true)][ValidateSet('visitor', 'admin')][string]$Role,
        [Parameter(Mandatory = $true)][string]$Factory,
        [Parameter(Mandatory = $true)][int]$Port
    )
    $arguments = @(
        '-m', 'uvicorn', $Factory, '--factory',
        '--host', '127.0.0.1', '--port', [string]$Port,
        '--app-dir', (ConvertTo-LoungeQuotedArgument $projectRoot)
    )
    $process = Start-Process -FilePath $python `
        -ArgumentList $arguments `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logsDirectory "$Role.stdout.log") `
        -RedirectStandardError (Join-Path $logsDirectory "$Role.stderr.log") `
        -PassThru
    $launcherPidFile = Join-Path $runtimeDirectory "$Role.launcher.pid"
    Write-LoungePid -PidFile $launcherPidFile -ProcessId $process.Id
    try {
        $serviceProcess = Find-LoungePythonServiceProcess `
            -WrapperProcessId $process.Id `
            -Role $Role `
            -ProjectRoot $projectRoot
    } catch {
        if ($null -ne (Get-LoungeProcessInfo -ProcessId $process.Id)) {
            Stop-VerifiedLoungeProcessTree `
                -ProcessId $process.Id `
                -Role $Role `
                -ProjectRoot $projectRoot `
                -SingleProcess
        }
        Remove-Item -LiteralPath $launcherPidFile -ErrorAction SilentlyContinue
        throw
    }
    Write-LoungePid `
        -PidFile (Join-Path $runtimeDirectory "$Role.pid") `
        -ProcessId ([int]$serviceProcess.ProcessId)
    return $process
}

$visitor = $null
$admin = $null
try {
    $visitor = Start-LoungeUvicorn -Role visitor -Factory 'visitor_lounge.visitor_app:build_visitor_app' -Port 8001
    $admin = Start-LoungeUvicorn -Role admin -Factory 'visitor_lounge.admin_app:build_admin_app' -Port 8002
    while (-not $visitor.HasExited -and -not $admin.HasExited) {
        Start-Sleep -Seconds 1
        $visitor.Refresh()
        $admin.Refresh()
    }
} finally {
    $cleanupFailures = [Collections.Generic.List[string]]::new()
    foreach ($record in @('visitor', 'visitor.launcher', 'admin', 'admin.launcher')) {
        $role = $record.Split('.')[0]
        $pidFile = Join-Path $runtimeDirectory "$record.pid"
        if (-not (Test-Path -LiteralPath $pidFile -PathType Leaf)) { continue }
        $recordedPid = $null
        try {
            $recordedPid = Read-LoungePid -PidFile $pidFile
            if ($null -ne (Get-LoungeProcessInfo -ProcessId $recordedPid)) {
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
                $cleanupFailures.Add($_.Exception.Message)
            }
        }
    }
    if ($cleanupFailures.Count -gt 0) {
        throw "Supervisor cleanup failed: $($cleanupFailures -join '; ')"
    }
}
