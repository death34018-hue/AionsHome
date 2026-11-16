param([string]$Serial = '')

$ErrorActionPreference = 'Stop'
$adbPath = (Get-Command adb -ErrorAction Stop).Source
$deviceArgs = @()
if ($Serial) { $deviceArgs = @('-s', $Serial) }

function Read-Adb {
    param([string[]]$Arguments)
    $result = & $adbPath @deviceArgs @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) { throw ($result -join "`n") }
    return ($result -join "`n")
}

if ((Read-Adb @('get-state')).Trim() -ne 'device') {
    throw 'Connect the phone and accept its USB debugging prompt first.'
}

$report = [ordered]@{
    captured_at = (Get-Date).ToString('o')
    app = @()
    preferences = [ordered]@{}
    service = @()
    audio = @()
    webviews = @()
    logs = @()
    errors = @()
}
$package = 'com.aion.chat'

try {
    $report.app = @((Read-Adb @('shell', 'dumpsys', 'package', $package)) -split "`n" |
        Where-Object { $_ -match 'versionCode=|versionName=|lastUpdateTime=' })
    [xml]$prefs = Read-Adb @('exec-out', 'run-as', $package, 'cat', 'shared_prefs/aion_prefs.xml')
    foreach ($entry in $prefs.map.ChildNodes) {
        $key = $entry.GetAttribute('name')
        if ($key -notmatch '^background_tts_') { continue }
        $value = if ($entry.HasAttribute('value')) { $entry.GetAttribute('value') } else { $entry.InnerText }
        if ($key -eq 'background_tts_active_at') {
            $value = [BitConverter]::Int64BitsToDouble([long]$value)
        }
        $report.preferences[$key] = $value
    }
} catch { $report.errors += "Preferences: $($_.Exception.Message)" }

try {
    $report.service = @((Read-Adb @('shell', 'dumpsys', 'activity', 'services', $package)) -split "`n" |
        Where-Object { $_ -match 'ServiceRecord|isForeground=|foregroundId=|startRequested=' })
    $report.audio = @((Read-Adb @('shell', 'dumpsys', 'audio')) -split "`n" |
        Select-String -Pattern 'STREAM_MUSIC:|mMode=|mRingerMode' -Context 0,6 |
        ForEach-Object { $_.Line; $_.Context.PostContext })
} catch { $report.errors += "Service/audio: $($_.Exception.Message)" }

try {
    $report.logs = @((Read-Adb @('logcat', '-d', '-v', 'threadtime', '-t', '6000',
        'AionPush:D', 'AionBackgroundTts:V', 'AionWebView:I', 'chromium:W',
        'MediaPlayer:W', 'AndroidRuntime:E', '*:S')) -split "`n" |
        Where-Object { $_ -match 'tts|foreground=|OPEN|FAIL|CLOSED|Network|MediaPlayer|Exception|Error|FATAL|onDestroy|onCreate' } |
        Select-Object -Last 600)
} catch { $report.errors += "Logs: $($_.Exception.Message)" }

# Read only playback state from the existing WebView, without refreshing it.
$expression = @'
(() => {
  function inspect(w) {
    const read = code => { try { return w.eval(code); } catch (_) { return null; } };
    try {
      return {
        path: w.location.pathname,
        hidden: w.document.hidden,
        nativeForeground: w.AionTtsAudio?.isForeground?.() ?? null,
        nativeSpeaking: w.AionAmbientSound?.isTtsPlaying?.() ?? null,
        private: read(`typeof ttsEnabled === 'undefined' ? null : {
          enabled: ttsEnabled, voice: ttsVoiceId, activeAt: ttsPlaybackActiveAt,
          clientId: _clientId, socket: typeof ws === 'undefined' ? null : ws?.readyState,
          playing: ttsPlaying, order: ttsPlayOrder, paused: ttsAudio.paused,
          queues: Object.fromEntries(Object.entries(ttsChunkQueues).map(([id,q]) =>
            [id, {next:q.nextPlay, finished:q.finished, chunks:Object.keys(q.chunks)}]))
        }`),
        room: read(`typeof crTtsEnabled === 'undefined' ? null : {
          enabled: crTtsEnabled, voice: crCurrentTTSVoice(), activeAt: crTtsPlaybackActiveAt,
          clientId: crAmbientClientId, socket: crWs?.readyState,
          playing: _ttsEngine.playing, order: _ttsEngine.playOrder, paused: _ttsEngine.audio.paused,
          queues: Object.fromEntries(Object.entries(_ttsEngine.chunkQueues).map(([id,q]) =>
            [id, {next:q.nextPlay, finished:q.finished, chunks:Object.keys(q.chunks)}]))
        }`)
      };
    } catch (_) { return { inaccessible: true }; }
  }
  return [inspect(window), ...Array.from(document.querySelectorAll('iframe'), f => inspect(f.contentWindow))];
})()
'@

$forwardPort = $null
try {
    $appPid = (Read-Adb @('shell', 'pidof', $package)).Trim().Split(' ')[0]
    if ($appPid) {
        $forwardPort = (Read-Adb @('forward', 'tcp:0', "localabstract:webview_devtools_remote_$appPid")).Trim()
        $pages = Invoke-RestMethod -Uri "http://127.0.0.1:$forwardPort/json" -TimeoutSec 5
        foreach ($page in $pages) {
            if (-not $page.webSocketDebuggerUrl) { continue }
            $socket = [System.Net.WebSockets.ClientWebSocket]::new()
            $timeout = [System.Threading.CancellationTokenSource]::new(8000)
            try {
                $socket.ConnectAsync([uri]$page.webSocketDebuggerUrl, $timeout.Token).GetAwaiter().GetResult()
                $request = @{ id = 1; method = 'Runtime.evaluate'; params = @{
                    expression = $expression; returnByValue = $true
                }} | ConvertTo-Json -Depth 5 -Compress
                $bytes = [Text.Encoding]::UTF8.GetBytes($request)
                $socket.SendAsync([ArraySegment[byte]]::new($bytes),
                    [System.Net.WebSockets.WebSocketMessageType]::Text, $true, $timeout.Token).GetAwaiter().GetResult()
                do {
                    $body = [IO.MemoryStream]::new()
                    try {
                        do {
                            $buffer = [byte[]]::new(8192)
                            $received = $socket.ReceiveAsync([ArraySegment[byte]]::new($buffer), $timeout.Token).GetAwaiter().GetResult()
                            if ($received.MessageType -eq [System.Net.WebSockets.WebSocketMessageType]::Close) {
                                throw 'WebView debugger closed the connection.'
                            }
                            $body.Write($buffer, 0, $received.Count)
                        } until ($received.EndOfMessage)
                        $reply = [Text.Encoding]::UTF8.GetString($body.ToArray()) | ConvertFrom-Json
                    } finally { $body.Dispose() }
                } until ($reply.id -eq 1)
                if ($reply.error -or $reply.result.exceptionDetails) { throw 'WebView playback state was unavailable.' }
                $report.webviews += ,$reply.result.result.value
            } catch { $report.errors += "WebView: $($_.Exception.Message)" }
            finally { $socket.Dispose(); $timeout.Dispose() }
        }
    }
} catch { $report.errors += "WebView connection: $($_.Exception.Message)" }
finally {
    if ($forwardPort) { & $adbPath @deviceArgs forward --remove "tcp:$forwardPort" 2>&1 | Out-Null }
}

$outputDirectory = Join-Path (Split-Path -Parent $PSScriptRoot) 'build'
New-Item -ItemType Directory -Force -Path $outputDirectory | Out-Null
$outputPath = Join-Path $outputDirectory 'tts-diagnostics.json'
[IO.File]::WriteAllText($outputPath, ($report | ConvertTo-Json -Depth 14), [Text.UTF8Encoding]::new($false))
Write-Host "Saved diagnostics: $outputPath"
