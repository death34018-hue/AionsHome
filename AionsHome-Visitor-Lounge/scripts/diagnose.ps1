$ErrorActionPreference = 'Continue'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'runtime-common.ps1')

$projectRoot = Get-LoungeProjectRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$database = Join-Path $projectRoot 'data\visitor-lounge.sqlite3'
$problems = 0

function Test-LoungeExternalCommand {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    $exitCode = $null
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Stop'
        $global:LASTEXITCODE = $null
        & $Executable @Arguments *> $null
        if ($null -ne $global:LASTEXITCODE) {
            $exitCode = [int]$global:LASTEXITCODE
        }
    } catch {
        $exitCode = $null
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    return $null -ne $exitCode -and $exitCode -eq 0
}

try {
    $environment = Import-LoungeEnvironment -ProjectRoot $projectRoot
    Write-Output 'config secrets: present (values hidden)'
} catch {
    $problems += 1
    Write-Output 'config secrets: invalid'
}

$settingsProbe = @(
    '-c',
    'from pathlib import Path; import sys; from visitor_lounge.settings import Settings; Settings.load(Path(sys.argv[1]))',
    $projectRoot
)
if ((Test-Path -LiteralPath $python -PathType Leaf) -and
    (Test-LoungeExternalCommand -Executable $python -Arguments $settingsProbe)) {
    Write-Output 'configuration: valid'
} else {
    $problems += 1
    Write-Output 'configuration: invalid'
}

if ((Test-Path -LiteralPath $database -PathType Leaf) -and (Test-Path -LiteralPath $python -PathType Leaf)) {
    $databaseProbe = @(
        '-c',
        "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'; c.close()",
        $database
    )
    if (Test-LoungeExternalCommand -Executable $python -Arguments $databaseProbe) {
        Write-Output 'database: integrity ok'
    }
    else { $problems += 1; Write-Output 'database: integrity check failed' }
} elseif (Test-Path -LiteralPath $database -PathType Leaf) {
    $problems += 1
    Write-Output 'database: venv unavailable for integrity check'
} else {
    Write-Output 'database: not created yet'
}

foreach ($port in @(8001, 8002)) {
    $listener = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
    if ($null -eq $listener) { Write-Output "port $port`: available" }
    else { Write-Output "port $port`: listening" }
}

$sharedCodexProbe = @(
    '-c',
    'from pathlib import Path; import sys; from visitor_lounge.shared_codex_runtime import SharedCodexRuntime; r=Path(sys.argv[1]); SharedCodexRuntime().resolve(lounge_root=r, model=sys.argv[2], instructions_file=r/sys.argv[3]/sys.argv[4], developer_instructions=sys.argv[5])',
    $projectRoot,
    'gpt-5.6-sol',
    'config',
    'codex_base.md',
    'diagnostic-only'
)
if (-not (Test-Path -LiteralPath $python -PathType Leaf) -or
    -not (Test-LoungeExternalCommand -Executable $python -Arguments $sharedCodexProbe)) {
    $problems += 1
    Write-Output 'Codex runtime: shared AionsHome profile unavailable'
} else {
    Write-Output 'Codex runtime: shared AionsHome profile available'
}

$drive = Get-PSDrive -Name ([IO.Path]::GetPathRoot($projectRoot).Substring(0, 1))
Write-Output ("disk free: {0:N1} GiB" -f ($drive.Free / 1GB))

foreach ($endpoint in @(
    @{ Uri = 'http://127.0.0.1:8001/healthz'; Service = 'visitor' },
    @{ Uri = 'http://127.0.0.1:8002/healthz'; Service = 'admin' }
)) {
    if (Test-LoungeHealthEndpoint `
        -Uri $endpoint.Uri `
        -ExpectedService $endpoint.Service
    ) {
        Write-Output "$($endpoint.Uri): healthy"
    } else {
        $problems += 1
        Write-Output "$($endpoint.Uri): unhealthy"
    }
}
if ($problems -gt 0) { exit 1 }
