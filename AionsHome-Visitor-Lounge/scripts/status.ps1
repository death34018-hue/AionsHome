$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'runtime-common.ps1')

$projectRoot = Get-LoungeProjectRoot
$runtimeDirectory = Get-LoungeRuntimeDirectory -ProjectRoot $projectRoot
$unsafe = $false
foreach ($record in @('visitor', 'visitor.launcher', 'admin', 'admin.launcher', 'supervisor')) {
    $role = $record.Split('.')[0]
    $label = $record.Replace('.', ' ')
    $pidFile = Join-Path $runtimeDirectory "$record.pid"
    if (-not (Test-Path -LiteralPath $pidFile -PathType Leaf)) {
        Write-Output "$label`: stopped"
        continue
    }
    try {
        $recordedPid = Read-LoungePid -PidFile $pidFile
        $process = Get-LoungeProcessInfo -ProcessId $recordedPid
        if ($null -eq $process) {
            Write-Output "$label`: stale PID $recordedPid"
        } elseif (Test-LoungeProcessIdentity -Process $process -Role $role -ProjectRoot $projectRoot) {
            Write-Output "$label`: running (PID $recordedPid)"
        } else {
            $unsafe = $true
            Write-Output "$label`: unsafe PID record (not touched)"
        }
    } catch {
        $unsafe = $true
        Write-Output "$label`: unsafe PID file (not touched)"
    }
}
if ($unsafe) { exit 2 }
