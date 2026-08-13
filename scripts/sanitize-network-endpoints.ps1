param(
    [Parameter(Mandatory = $true)]
    [string]$Root
)

$ErrorActionPreference = 'Stop'
$rootPath = [IO.Path]::GetFullPath($Root)
$launcherPath = Join-Path $rootPath 'AionApp\app\src\main\java\com\aion\chat\LauncherActivity.java'
$endpointPath = Join-Path $rootPath 'AionApp\app\src\main\java\com\aion\chat\ConnectionEndpoint.java'

if (-not (Test-Path -LiteralPath $launcherPath)) {
    throw "Missing endpoint source: $launcherPath"
}
if (-not (Test-Path -LiteralPath $endpointPath)) {
    throw "Missing endpoint source: $endpointPath"
}

$launcher = [IO.File]::ReadAllText($launcherPath)
$endpoint = [IO.File]::ReadAllText($endpointPath)

function Read-MatchValue([string]$Text, [string]$Pattern, [string]$Label) {
    $match = [regex]::Match($Text, $Pattern)
    if (-not $match.Success) { throw "Cannot locate $Label in Android source" }
    return $match.Groups[1].Value
}

$homeHost = Read-MatchValue $launcher 'URL_HOME\s*=\s*"http://([^/:"]+)' 'home host'
$outdoorHost = Read-MatchValue $launcher 'URL_OUTDOOR\s*=\s*"http://([^/:"]+)' 'outdoor host'
$cloudflareHost = Read-MatchValue $endpoint 'CLOUDFLARE_HOST\s*=\s*"([^"]+)' 'Cloudflare host'
$legacyCloudflareHost = Read-MatchValue $endpoint 'LEGACY_CLOUDFLARE_WS_HOST\s*=\s*"([^"]+)' 'legacy Cloudflare host'

$replacements = [ordered]@{}
$replacements[$homeHost] = '192.168.1.100'
$replacements[$outdoorHost] = '100.64.0.1'
$replacements[$cloudflareHost] = 'chat.example.com'
$replacements[$legacyCloudflareHost] = 'legacy-ws.example.com'

$sourceRoot = Join-Path $rootPath 'AionApp\app\src'
$utf8NoBom = New-Object Text.UTF8Encoding($false)
Get-ChildItem -LiteralPath $sourceRoot -Recurse -File | Where-Object {
    $_.Extension -in '.java', '.kt', '.xml', '.json', '.properties'
} | ForEach-Object {
    $content = [IO.File]::ReadAllText($_.FullName)
    $updated = $content
    foreach ($oldValue in $replacements.Keys) {
        if ($oldValue -and $oldValue -ne $replacements[$oldValue]) {
            $updated = $updated.Replace($oldValue, $replacements[$oldValue])
        }
    }
    if ($updated -ne $content) {
        [IO.File]::WriteAllText($_.FullName, $updated, $utf8NoBom)
        Write-Host "  sanitized: $($_.FullName.Substring($rootPath.Length + 1))"
    }
}

$excludedSegments = @(
    '\.git\', '\.gradle\', '\.venv\', '\node_modules\', '\vendor\',
    '\cache\', '\build\', '\dist\', '\__pycache__\'
)
$textExtensions = @(
    '.java', '.kt', '.xml', '.json', '.jsonl', '.properties', '.gradle',
    '.md', '.txt', '.bat', '.cmd', '.ps1', '.py', '.js', '.css', '.html',
    '.yml', '.yaml', '.toml', '.ini', '.cfg', '.env'
)
$selfPath = Join-Path $rootPath 'scripts\sanitize-network-endpoints.ps1'
$remaining = New-Object Collections.Generic.List[string]
Get-ChildItem -LiteralPath $rootPath -Recurse -File -ErrorAction SilentlyContinue | Where-Object {
    $path = $_.FullName
    $_.Extension -in $textExtensions -and
    $path -ne $selfPath -and
    -not ($excludedSegments | Where-Object { $path.Contains($_) })
} | ForEach-Object {
    $path = $_.FullName
    $relativePath = $path.Substring($rootPath.Length + 1)
    $isTestFile = (
        $relativePath -match '(^|\\)(test|tests)\\' -or
        $_.BaseName -match '^(test_|.*_test$)'
    )
    try {
        $content = [IO.File]::ReadAllText($path)
        foreach ($oldValue in $replacements.Keys) {
            if ($oldValue -and $oldValue -ne $replacements[$oldValue] -and $content.Contains($oldValue)) {
                $remaining.Add("${relativePath}: $oldValue")
            }
        }
        foreach ($match in [regex]::Matches($content, '(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)')) {
            $parts = $match.Value.Split('.') | ForEach-Object { [int]$_ }
            if (($parts | Where-Object { $_ -gt 255 }).Count -gt 0) { continue }
            $isCgnat = $parts[0] -eq 100 -and $parts[1] -ge 64 -and $parts[1] -le 127
            $isCgnatNetwork = (
                $match.Value -eq '100.64.0.0' -and
                $content.Substring($match.Index) -match '^100\.64\.0\.0/10(?!\d)'
            )
            if ($isCgnat -and -not $isTestFile -and -not $isCgnatNetwork -and $match.Value -ne '100.64.0.1') {
                $remaining.Add("${relativePath}: $($match.Value)")
            }
        }
    } catch {
        # Ignore unreadable/binary files; build artifacts are deleted by the caller.
    }
}

if ($remaining.Count -gt 0) {
    Write-Host ''
    Write-Host 'Personal network values still remain:' -ForegroundColor Red
    $remaining | Sort-Object -Unique | ForEach-Object { Write-Host "  $_" -ForegroundColor Red }
    exit 2
}

Write-Host '  network endpoint sanitization verified'
