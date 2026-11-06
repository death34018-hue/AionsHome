# Optional, per-user Windows supervision. Unregistered installations keep their usual launcher.
$homeTaskDirectory = Join-Path (Split-Path -Parent $PSScriptRoot) 'aion-chat'
$homeTaskName = 'AionsHome-Server'

function Get-HomeServerTask {
    $task = Get-ScheduledTask -TaskPath '\' -TaskName $homeTaskName -ErrorAction SilentlyContinue
    if ($task -and $task.Actions.WorkingDirectory -eq $homeTaskDirectory -and
        $task.Actions.Arguments -eq ('-u "{0}\main.py" --scheduled' -f $homeTaskDirectory)) { return $task }
}

function Install-HomeServerTask {
    $existing = Get-ScheduledTask -TaskPath '\' -TaskName $homeTaskName -ErrorAction SilentlyContinue
    if ($existing) {
        if (-not (Get-HomeServerTask)) { throw "Task $homeTaskName already belongs to another installation." }
        return
    }
    $python = Join-Path (Split-Path -Parent $homeTaskDirectory) '.venv\Scripts\pythonw.exe'
    if (-not (Test-Path -LiteralPath $python)) { throw "Missing $python" }
    $action = New-ScheduledTaskAction -Execute $python -Argument ('-u "{0}\main.py" --scheduled' -f $homeTaskDirectory) -WorkingDirectory $homeTaskDirectory
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Priority 6
    $userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
    $null = Register-ScheduledTask -TaskPath '\' -TaskName $homeTaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -ErrorAction Stop
}

function Suspend-HomeServerTask($task) {
    $null = Disable-ScheduledTask -InputObject $task -ErrorAction Stop
    Stop-ScheduledTask -InputObject $task -ErrorAction Stop
}

function Start-HomeServerTask($task) {
    $null = Enable-ScheduledTask -InputObject $task -ErrorAction Stop
    Start-ScheduledTask -InputObject $task -ErrorAction Stop
}
