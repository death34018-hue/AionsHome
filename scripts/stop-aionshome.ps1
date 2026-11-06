param(
    [switch]$DryRun,
    [switch]$ServersOnly
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot 'home-server-task.ps1')
$workspacePath = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$workspacePattern = [regex]::Escape($workspacePath)

$targetPorts = @(8080, 8011, 18788)
$directPatterns = @(
    'uvicorn\s+main:app',
    '(?:^|\s)-u\s+main\.py(?:\s|$)',
    'aion-chat[\\/]+main\.py',
    '-m\s+qq_social\.worker'
)
if (-not $ServersOnly) {
    $directPatterns += @('desktop_pet\.py', '--app=http://localhost:8080/wallpaper')
}

function Add-Target {
    param(
        [hashtable]$Set,
        [int]$Id,
        [string]$Reason
    )
    if ($Id -le 0 -or $protected.ContainsKey($Id)) { return }
    if (-not $Set.ContainsKey($Id)) {
        $Set[$Id] = New-Object System.Collections.Generic.List[string]
    }
    if (-not $Set[$Id].Contains($Reason)) {
        $Set[$Id].Add($Reason)
    }
}

$processes = @(Get-CimInstance Win32_Process)
$byPid = @{}
$childrenByParent = @{}
foreach ($p in $processes) {
    $byPid[[int]$p.ProcessId] = $p
    $ppid = 0
    if ($null -ne $p.ParentProcessId) { $ppid = [int]$p.ParentProcessId }
    if (-not $childrenByParent.ContainsKey($ppid)) {
        $childrenByParent[$ppid] = New-Object System.Collections.Generic.List[int]
    }
    $childrenByParent[$ppid].Add([int]$p.ProcessId)
}

$targets = @{}
# Never close the restart command itself, or the terminal that invoked it.
$protected = @{}
$ancestorId = $PID
while ($ancestorId -gt 0 -and -not $protected.ContainsKey($ancestorId)) {
    $protected[$ancestorId] = $true
    if (-not $byPid.ContainsKey($ancestorId)) { break }
    $ancestorId = [int]$byPid[$ancestorId].ParentProcessId
}

function Test-WorkspaceProcess {
    param($Process)
    $seen = @{}
    while ($null -ne $Process -and -not $seen.ContainsKey([int]$Process.ProcessId)) {
        $seen[[int]$Process.ProcessId] = $true
        if ($Process.CommandLine -match $workspacePattern -or $Process.ExecutablePath -like "$workspacePath\*") { return $true }
        $Process = $byPid[[int]$Process.ParentProcessId]
    }
    return $false
}

foreach ($p in $processes) {
    if ($protected.ContainsKey([int]$p.ProcessId)) { continue }
    $cmd = ""
    if ($null -ne $p.CommandLine) { $cmd = [string]$p.CommandLine }
    if (-not (Test-WorkspaceProcess $p)) { continue }
    # Only dedicated /c launchers are disposable; never kill an interactive shell.
    if ($p.Name -eq 'cmd.exe' -and $cmd -match '(?i)\s/c\s' -and
        $cmd -match '(?:一键启动|重启小家)\.bat') {
        Add-Target $targets ([int]$p.ProcessId) 'old home launcher window'
        continue
    }
    if ($p.Name -notmatch '^(?:pythonw?|uvicorn|chrome|msedge)(?:\d+(?:\.\d+)*)?\.exe$') { continue }
    foreach ($pattern in $directPatterns) {
        if ($cmd -match $pattern) {
            Add-Target $targets ([int]$p.ProcessId) "command matches '$pattern'"
            break
        }
    }
}

foreach ($conn in @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $targetPorts -contains $_.LocalPort })) {
    $owner = $byPid[[int]$conn.OwningProcess]
    if ($null -eq $owner -or -not (Test-WorkspaceProcess $owner) -or $owner.Name -notmatch '^(?:pythonw?|uvicorn).*\.exe$') {
        throw "Port $($conn.LocalPort) belongs to another application (PID $($conn.OwningProcess)). Nothing was stopped."
    }
    Add-Target $targets ([int]$conn.OwningProcess) "listening on port $($conn.LocalPort)"
}

$queue = New-Object System.Collections.Generic.Queue[int]
foreach ($id in @($targets.Keys)) {
    $queue.Enqueue([int]$id)
}
while ($queue.Count -gt 0) {
    $id = $queue.Dequeue()
    if (-not $childrenByParent.ContainsKey($id)) { continue }
    foreach ($childId in $childrenByParent[$id]) {
        if ($targets.ContainsKey([int]$childId) -or $protected.ContainsKey([int]$childId)) { continue }
        Add-Target $targets ([int]$childId) "child of target $id"
        $queue.Enqueue([int]$childId)
    }
}

function Get-Depth {
    param([int]$Id)
    $depth = 0
    $seen = @{}
    while ($byPid.ContainsKey($Id)) {
        if ($seen.ContainsKey($Id)) { break }
        $seen[$Id] = $true
        $parent = 0
        if ($null -ne $byPid[$Id].ParentProcessId) { $parent = [int]$byPid[$Id].ParentProcessId }
        if (-not $targets.ContainsKey($parent)) { break }
        $depth += 1
        $Id = $parent
    }
    return $depth
}

$ordered = @($targets.Keys | ForEach-Object {
    $id = [int]$_
    [PSCustomObject]@{
        Id = $id
        Depth = Get-Depth $id
        Process = $byPid[$id]
        Reason = ($targets[$id] -join "; ")
    }
} | Sort-Object Depth, Id)

# Only pause recovery after verifying the port owners, even if the server already exited.
if (-not $DryRun) {
    $homeTask = Get-HomeServerTask
    if ($homeTask) { Suspend-HomeServerTask $homeTask }
}
if (-not $ordered -or $ordered.Count -eq 0) {
    Write-Host "No AionsHome processes found."
    exit 0
}

Write-Host "Targets:"
foreach ($item in $ordered) {
    $cmd = ""
    if ($null -ne $item.Process.CommandLine) { $cmd = [string]$item.Process.CommandLine }
    if ($cmd.Length -gt 160) { $cmd = $cmd.Substring(0, 160) + "..." }
    Write-Host ("  PID {0} {1} - {2}" -f $item.Id, $item.Process.Name, $item.Reason)
    if ($cmd) { Write-Host ("    {0}" -f $cmd) }
}

if ($DryRun) {
    Write-Host ""
    Write-Host "Dry run only. Nothing was stopped."
    exit 0
}

Write-Host ""
Write-Host "Stopping..."
# Stop launchers/supervisors first, then their captured children, so they cannot respawn.
foreach ($item in $ordered) {
    try {
        Stop-Process -Id $item.Id -Force -ErrorAction Stop
        Write-Host ("  stopped PID {0}" -f $item.Id)
    } catch {
        Write-Host ("  skipped PID {0}: {1}" -f $item.Id, $_.Exception.Message)
    }
}

Start-Sleep -Seconds 1

$alive = @($ordered | Where-Object { Get-Process -Id $_.Id -ErrorAction SilentlyContinue })
if ($alive.Count -gt 0) {
    throw "Some home processes could not be stopped: $($alive.Id -join ', '). No new server will be started."
}

$remaining = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $targetPorts -contains $_.LocalPort })
if ($remaining.Count -gt 0) {
    Write-Host ""
    Write-Host "Warning: these ports are still listening:"
    foreach ($conn in $remaining) {
        Write-Host ("  {0}:{1} PID {2}" -f $conn.LocalAddress, $conn.LocalPort, $conn.OwningProcess)
    }
    exit 1
} else {
    Write-Host ""
    Write-Host "AionsHome backend ports 8080/8011/18788 are clear."
}
exit 0
