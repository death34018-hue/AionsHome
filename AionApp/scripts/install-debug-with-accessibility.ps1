$ErrorActionPreference = 'Stop'

$project = Split-Path -Parent $PSScriptRoot
$javaHome = 'F:\AndroidStudio\jbr'
if (-not $env:JAVA_HOME -and (Test-Path $javaHome)) {
    $env:JAVA_HOME = $javaHome
}

& "$project\gradlew.bat" -p $project assembleDebug
if ($LASTEXITCODE -ne 0) {
    throw 'Debug APK build failed'
}

& adb install -r "$project\app\build\outputs\apk\debug\app-debug.apk"
if ($LASTEXITCODE -ne 0) {
    throw 'APK update install failed'
}

& adb shell pm grant com.aion.chat android.permission.WRITE_SECURE_SETTINGS
if ($LASTEXITCODE -ne 0) {
    throw 'WRITE_SECURE_SETTINGS grant failed'
}

$dump = (& adb shell dumpsys package com.aion.chat) -join "`n"
if ($LASTEXITCODE -ne 0 -or
        $dump -notmatch 'WRITE_SECURE_SETTINGS: granted=true') {
    throw 'WRITE_SECURE_SETTINGS verification failed'
}

Write-Host 'APK updated and WRITE_SECURE_SETTINGS verified.'
