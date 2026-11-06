$ErrorActionPreference = 'Stop'

$repositoryRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).ProviderPath
$sourceBatch = Get-ChildItem -File -LiteralPath $repositoryRoot -Filter '*.bat' |
    Where-Object { Select-String -Quiet -SimpleMatch 'sanitize-network-endpoints.ps1' -LiteralPath $_.FullName } |
    Select-Object -First 1 -ExpandProperty FullName
if (-not $sourceBatch) {
    throw 'Personal-data cleanup batch was not found.'
}
$sandbox = Join-Path ([IO.Path]::GetTempPath()) ("aionshome-clean-personal-data-" + [guid]::NewGuid().ToString('N'))

try {
    [void](New-Item -ItemType Directory -Path $sandbox)
    Copy-Item -LiteralPath $sourceBatch -Destination (Join-Path $sandbox 'cleanup.bat')

    $directories = @(
        'scripts',
        'aion-chat\data',
        'AionApp',
        'AionsHome-Visitor-Lounge\data',
        'AionsHome-Visitor-Lounge\logs',
        'AionsHome-Visitor-Lounge\.runtime',
        'AionsHome-Visitor-Lounge\.rollback-quarantine',
        'AionsHome-Visitor-Lounge\.codex-home',
        'AionsHome-Visitor-Lounge\.pytest_cache',
        'AionsHome-Visitor-Lounge\.venv',
        'AionsHome-Visitor-Lounge\static\avatars'
    )
    foreach ($relativePath in $directories) {
        [void](New-Item -ItemType Directory -Force -Path (Join-Path $sandbox $relativePath))
    }

    @'
param([string]$Root)
exit 0
'@ | Set-Content -Encoding UTF8 -LiteralPath (Join-Path $sandbox 'scripts\sanitize-network-endpoints.ps1')

    $sensitiveFiles = @(
        'aion-chat\data\lounge_friends.json',
        'aion-chat\data\lounge-context-bridge.key',
        'aion-chat\server.stdout.log',
        'aion-chat\server.stderr.log',
        'AionApp\local.properties',
        'AionsHome-Visitor-Lounge\.env',
        'AionsHome-Visitor-Lounge\data\visitor-lounge.sqlite3',
        'AionsHome-Visitor-Lounge\logs\visitor.stdout.log',
        'AionsHome-Visitor-Lounge\.runtime\visitor.pid',
        'AionsHome-Visitor-Lounge\.rollback-quarantine\old.env',
        'AionsHome-Visitor-Lounge\.codex-home\auth.json',
        'AionsHome-Visitor-Lounge\.pytest_cache\nodeids',
        'AionsHome-Visitor-Lounge\.venv\pyvenv.cfg'
    )
    foreach ($relativePath in $sensitiveFiles) {
        Set-Content -Encoding UTF8 -LiteralPath (Join-Path $sandbox $relativePath) -Value 'private-fixture'
    }

    Set-Content -Encoding UTF8 -LiteralPath (Join-Path $sandbox 'AionsHome-Visitor-Lounge\static\avatars\host.jpg') -Value 'avatar-fixture'
    Set-Content -Encoding UTF8 -LiteralPath (Join-Path $sandbox 'unrelated.keep') -Value 'keep-fixture'
    $batchPath = Join-Path $sandbox 'cleanup.bat'
    $startInfo = New-Object Diagnostics.ProcessStartInfo
    $startInfo.FileName = $env:ComSpec
    $startInfo.Arguments = '/d /c call cleanup.bat'
    $startInfo.WorkingDirectory = $sandbox
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardInput = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $startInfo
    [void]$process.Start()
    $process.StandardInput.WriteLine('Y')
    $process.StandardInput.WriteLine('')
    $process.StandardInput.Close()
    $batchOutput = @($process.StandardOutput.ReadToEnd(), $process.StandardError.ReadToEnd())
    $process.WaitForExit()
    if ($process.ExitCode -ne 0) {
        throw "Cleanup batch exited with code $($process.ExitCode)`n$($batchOutput -join [Environment]::NewLine)"
    }

    foreach ($relativePath in $sensitiveFiles) {
        if (Test-Path -LiteralPath (Join-Path $sandbox $relativePath)) {
            throw "Sensitive fixture was not removed: $relativePath`n$($batchOutput -join [Environment]::NewLine)"
        }
    }
    foreach ($relativePath in @(
        'AionsHome-Visitor-Lounge\static\avatars\host.jpg',
        'unrelated.keep',
        'aion-chat\data\activity_logs\.gitkeep',
        'aion-chat\data\chats\.gitkeep',
        'aion-chat\data\tts_cache\.gitkeep',
        'aion-chat\data\uploads\.gitkeep'
    )) {
        if (-not (Test-Path -LiteralPath (Join-Path $sandbox $relativePath) -PathType Leaf)) {
            throw "Unrelated fixture was removed: $relativePath"
        }
    }

    Write-Output 'PASS: Visitor Lounge personal data is removed and the avatar is preserved.'
}
finally {
    if (Test-Path -LiteralPath $sandbox) {
        Remove-Item -Recurse -Force -LiteralPath $sandbox
    }
}
