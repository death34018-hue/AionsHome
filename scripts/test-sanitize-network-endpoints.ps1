$ErrorActionPreference = 'Stop'

$repositoryRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).ProviderPath
$sanitizer = Join-Path $repositoryRoot 'scripts\sanitize-network-endpoints.ps1'

function New-SanitizerFixture([string]$Root) {
    $android = Join-Path $Root 'AionApp\app\src\main\java\com\aion\chat'
    $python = Join-Path $Root 'aion-chat'
    [void](New-Item -ItemType Directory -Force -Path $android)
    [void](New-Item -ItemType Directory -Force -Path (Join-Path $android '..\..\..\test\java\com\aion\chat'))
    [void](New-Item -ItemType Directory -Force -Path (Join-Path $python 'routes'))

    @'
class LauncherActivity {
    static final String URL_HOME = "http://192.168.23.45:8000";
    static final String URL_OUTDOOR = "http://100.99.88.77:8000";
}
'@ | Set-Content -Encoding UTF8 -LiteralPath (Join-Path $android 'LauncherActivity.java')
    @'
class ConnectionEndpoint {
    static final String CLOUDFLARE_HOST = "private.example.test";
    static final String LEGACY_CLOUDFLARE_WS_HOST = "legacy-private.example.test";
}
'@ | Set-Content -Encoding UTF8 -LiteralPath (Join-Path $android 'ConnectionEndpoint.java')
}

# Keep fixture paths short enough for Windows PowerShell 5.1.
$sandbox = Join-Path $repositoryRoot ('.tmp\sanitize-network-' + [guid]::NewGuid().ToString('N').Substring(0, 12))
try {
    $safeRoot = Join-Path $sandbox 'safe'
    New-SanitizerFixture $safeRoot
    [void](New-Item -ItemType Directory -Force -Path (Join-Path $safeRoot 'scripts'))
    Copy-Item -LiteralPath $sanitizer -Destination (Join-Path $safeRoot 'scripts\sanitize-network-endpoints.ps1')
    Set-Content -Encoding UTF8 -LiteralPath (Join-Path $safeRoot 'aion-chat\security_access.py') -Value 'CGNAT = "100.64.0.0/10"'
    Set-Content -Encoding UTF8 -LiteralPath (Join-Path $safeRoot 'aion-chat\routes\security_access_report.py') -Value 'CGNAT = "100.64.0.0/10"'
    Set-Content -Encoding UTF8 -LiteralPath (Join-Path $safeRoot 'aion-chat\test_security_access.py') -Value 'fixture = "100.64.0.42"'
    Set-Content -Encoding UTF8 -LiteralPath (Join-Path $safeRoot 'aion-chat\test_homecoming_crypto.py') -Value 'fixture = "https://100.64.0.10/v1"'
    Set-Content -Encoding UTF8 -LiteralPath (Join-Path $safeRoot 'scripts\test-network-endpoints.ps1') -Value '$fixtures = @("100.64.0.10", "100.64.0.42", "100.70.80.90")'

    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $sanitizer -Root $safeRoot | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw 'Standards and test-only CGNAT fixtures were incorrectly rejected.'
    }

    $unsafeRoot = Join-Path $sandbox 'unsafe'
    New-SanitizerFixture $unsafeRoot
    [void](New-Item -ItemType Directory -Force -Path (Join-Path $unsafeRoot 'scripts'))
    Copy-Item -LiteralPath $sanitizer -Destination (Join-Path $unsafeRoot 'scripts\sanitize-network-endpoints.ps1')
    Set-Content -Encoding UTF8 -LiteralPath (Join-Path $unsafeRoot 'aion-chat\runtime_config.py') -Value 'host = "100.70.80.90"'
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $sanitizer -Root $unsafeRoot | Out-Null
    if ($LASTEXITCODE -eq 0) {
        throw 'An unexpected production CGNAT address was not rejected.'
    }

    Write-Output 'PASS: test fixtures are allowed while production CGNAT addresses remain blocked.'
}
finally {
    $sandbox = [IO.Path]::GetFullPath($sandbox)
    $safePrefix = [IO.Path]::GetFullPath((Join-Path $repositoryRoot '.tmp')).TrimEnd('\') + '\sanitize-network-'
    if (-not $sandbox.StartsWith($safePrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Fixture cleanup path escaped the repository temporary directory.'
    }
    if (Test-Path -LiteralPath $sandbox) {
        Remove-Item -Recurse -Force -LiteralPath $sandbox
    }
}
