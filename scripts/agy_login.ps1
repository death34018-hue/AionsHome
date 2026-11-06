$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path $PSScriptRoot -Parent
$taskLoginDirectory = Join-Path $taskRoot 'aion-chat\data\antigravity-login'
New-Item -ItemType Directory -Path $taskLoginDirectory -Force | Out-Null
Set-Location -LiteralPath $taskLoginDirectory
$taskAgy = Join-Path $env:LOCALAPPDATA 'agy\bin\agy.exe'
Write-Host 'Antigravity official account sign-in' -ForegroundColor Cyan
Write-Host 'This is a dedicated login folder, not your chat data folder.'
Write-Host 'If already signed in: type /logout in agy, then sign in with your alternate Google account.'
Write-Host 'Choose the alternate account explicitly in the browser. Do not send any chat prompt here.'
Write-Host 'After sign-in, use /exit and tell Codex that sign-in is complete.'
& $taskAgy
