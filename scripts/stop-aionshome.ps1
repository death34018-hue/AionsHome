param(
    [switch]$DryRun
)

$ErrorActionPreference = "SilentlyContinue"

$targetPorts = @(8080, 8011)
$directPatterns = @(
    'uvicorn\s+main:app',
    'python(?:\.exe)?\s+-u\s+main\.py',
    'aion-chat[\\/]+main\.py',
    'aion-chat[\\/]+mcp_servers[\\/]+home_assistant_server\.py',
    'desktop_pet\.py',
    '一键启动\.bat',
    '启动桌宠\.bat',
    '--app=http://localhost:8080/wallpaper'
)

function Add-Target {
    param(
        [hashtable]$Set,
        [int]$Id,
        [string]$Reason
    )
    if ($Id -le 0 -or $Id -eq $PID) { return }
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

foreach ($conn in @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $targetPorts -contains $_.LocalPort })) {
    Add-Target $targets ([int]$conn.OwningProcess) "listening on port $($conn.LocalPort)"
}

foreach ($p in $processes) {
    $cmd = ""
    if ($null -ne $p.CommandLine) { $cmd = [string]$p.CommandLine }
    foreach ($pattern in $directPatterns) {
        if ($cmd -match $pattern) {
            Add-Target $targets ([int]$p.ProcessId) "command matches '$pattern'"
            break
        }
    }
}

$queue = New-Object System.Collections.Generic.Queue[int]
foreach ($id in @($targets.Keys)) {
    $queue.Enqueue([int]$id)
}
while ($queue.Count -gt 0) {
    $id = $queue.Dequeue()
    if (-not $childrenByParent.ContainsKey($id)) { continue }
    foreach ($childId in $childrenByParent[$id]) {
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
} | Sort-Object Depth, Id -Descending)

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
foreach ($item in $ordered) {
    try {
        Stop-Process -Id $item.Id -Force -ErrorAction Stop
        Write-Host ("  stopped PID {0}" -f $item.Id)
    } catch {
        Write-Host ("  skipped PID {0}: {1}" -f $item.Id, $_.Exception.Message)
    }
}

Start-Sleep -Seconds 1

$remaining = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $targetPorts -contains $_.LocalPort })
if ($remaining.Count -gt 0) {
    Write-Host ""
    Write-Host "Warning: these ports are still listening:"
    foreach ($conn in $remaining) {
        Write-Host ("  {0}:{1} PID {2}" -f $conn.LocalAddress, $conn.LocalPort, $conn.OwningProcess)
    }
} else {
    Write-Host ""
    Write-Host "AionsHome backend ports 8080/8011 are clear."
}
