$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'runtime-common.ps1')

$projectRoot = Get-LoungeProjectRoot
$runtimeDirectory = Get-LoungeRuntimeDirectory -ProjectRoot $projectRoot
if (-not (Test-Path -LiteralPath $runtimeDirectory -PathType Container)) {
    Write-Output 'Visitor Lounge is stopped.'
    exit 0
}

$failures = [Collections.Generic.List[string]]::new()
foreach ($role in @('visitor', 'admin')) {
    try {
        Sync-LoungeServicePidFromLauncher `
            -Role $role `
            -ProjectRoot $projectRoot
    } catch {
        $failures.Add($_.Exception.Message)
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
            $failures.Add($_.Exception.Message)
        }
    }
}

if ($failures.Count -gt 0) {
    foreach ($failure in $failures) { Write-Error $failure }
    exit 1
}
Write-Output 'Visitor Lounge stopped.'
