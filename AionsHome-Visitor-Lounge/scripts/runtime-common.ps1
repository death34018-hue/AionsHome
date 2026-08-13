Set-StrictMode -Version Latest

function Get-LoungeProjectRoot {
    $root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
    if ([IO.Path]::GetFileName($root) -cne 'AionsHome-Visitor-Lounge') {
        throw "Refusing to operate outside the AionsHome-Visitor-Lounge project."
    }
    return $root
}

function Get-LoungeRuntimeDirectory {
    param([Parameter(Mandatory = $true)][string]$ProjectRoot)
    return [IO.Path]::GetFullPath((Join-Path $ProjectRoot '.runtime'))
}

function Test-LoungeHealthEndpoint {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [Parameter(Mandatory = $true)][ValidateSet('visitor', 'admin')]
        [string]$ExpectedService
    )
    try {
        $response = Invoke-WebRequest `
            -Uri $Uri `
            -Method Get `
            -UseBasicParsing `
            -TimeoutSec 2 `
            -ErrorAction Stop
        if ([int]$response.StatusCode -ne 200) { return $false }
        $contentType = [string]$response.Headers['Content-Type']
        if ($contentType -notmatch '^\s*application/json(?:\s*;|\s*$)') {
            return $false
        }
        $payload = $response.Content | ConvertFrom-Json -ErrorAction Stop
        return [string]$payload.status -ceq 'ok' -and
            [string]$payload.service -ceq $ExpectedService
    } catch {
        return $false
    }
}

function Import-LoungeEnvironment {
    param([Parameter(Mandatory = $true)][string]$ProjectRoot)
    $environmentFile = Join-Path $ProjectRoot '.env'
    if (-not (Test-Path -LiteralPath $environmentFile -PathType Leaf)) {
        throw "Missing required .env file: $environmentFile"
    }
    $loaded = @{}
    foreach ($rawLine in [IO.File]::ReadAllLines($environmentFile)) {
        $line = $rawLine.Trim()
        if (-not $line -or $line.StartsWith('#')) { continue }
        if ($line -notmatch '^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
            throw "Invalid .env assignment."
        }
        $name = $Matches[1]
        $value = $Matches[2].Trim()
        if ($value.Length -ge 2 -and (
            ($value.StartsWith('"') -and $value.EndsWith('"')) -or
            ($value.StartsWith("'") -and $value.EndsWith("'"))
        )) {
            $value = $value.Substring(1, $value.Length - 2)
        } else {
            $value = ($value -replace '\s+#.*$', '').Trim()
        }
        if ($value.Contains("`r") -or $value.Contains("`n")) {
            throw "Invalid multiline .env value."
        }
        $loaded[$name] = $value
    }
    foreach ($required in @(
        'VISITOR_LOUNGE_KEY_PEPPER',
        'VISITOR_LOUNGE_MASTER_KEY',
        'VISITOR_LOUNGE_SESSION_SECRET'
    )) {
        if (-not $loaded.ContainsKey($required) -or [string]::IsNullOrWhiteSpace($loaded[$required])) {
            throw "Missing required environment key: $required"
        }
    }
    foreach ($name in $loaded.Keys) {
        [Environment]::SetEnvironmentVariable($name, $loaded[$name], 'Process')
    }
    return $loaded
}

function Read-LoungePid {
    param([Parameter(Mandatory = $true)][string]$PidFile)
    $raw = [IO.File]::ReadAllText($PidFile).Trim()
    $parsed = 0
    if (-not [int]::TryParse($raw, [ref]$parsed) -or $parsed -le 0) {
        throw "Unsafe PID file: $PidFile"
    }
    return $parsed
}

function Get-LoungeProcessInfo {
    param([Parameter(Mandatory = $true)][int]$ProcessId)
    return Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
}

function ConvertFrom-LoungeCommandLine {
    param([Parameter(Mandatory = $true)][string]$CommandLine)
    if (-not ('VisitorLounge.NativeCommandLine' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

namespace VisitorLounge {
    public static class NativeCommandLine {
        [DllImport("shell32.dll", SetLastError = true)]
        private static extern IntPtr CommandLineToArgvW(
            [MarshalAs(UnmanagedType.LPWStr)] string commandLine,
            out int argumentCount
        );

        [DllImport("kernel32.dll")]
        private static extern IntPtr LocalFree(IntPtr memory);

        public static string[] Split(string commandLine) {
            int count;
            IntPtr arguments = CommandLineToArgvW(commandLine, out count);
            if (arguments == IntPtr.Zero) {
                throw new InvalidOperationException("Command line tokenization failed.");
            }
            try {
                string[] result = new string[count];
                for (int index = 0; index < count; index++) {
                    IntPtr value = Marshal.ReadIntPtr(arguments, index * IntPtr.Size);
                    result[index] = Marshal.PtrToStringUni(value);
                }
                return result;
            } finally {
                LocalFree(arguments);
            }
        }
    }
}
'@
    }
    return [VisitorLounge.NativeCommandLine]::Split($CommandLine)
}

function Resolve-LoungeExistingPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    $resolved = (Resolve-Path -LiteralPath $Path -ErrorAction Stop).ProviderPath
    return [IO.Path]::GetFullPath($resolved).TrimEnd('\', '/')
}

function Test-LoungeCanonicalArguments {
    param(
        [Parameter(Mandatory = $true)][string[]]$Tokens,
        [Parameter(Mandatory = $true)][string[]]$ExpectedTokens,
        [int[]]$PathTokenIndexes = @()
    )
    if ($Tokens.Count -ne $ExpectedTokens.Count) { return $false }
    for ($index = 0; $index -lt $Tokens.Count; $index++) {
        if ($PathTokenIndexes -contains $index) {
            try {
                if ((Resolve-LoungeExistingPath -Path $Tokens[$index]) -ine
                    (Resolve-LoungeExistingPath -Path $ExpectedTokens[$index])) {
                    return $false
                }
            } catch {
                return $false
            }
        } elseif ($Tokens[$index] -cne $ExpectedTokens[$index]) {
            return $false
        }
    }
    return $true
}

function Get-LoungeVenvBasePython {
    param([Parameter(Mandatory = $true)][string]$ProjectRoot)
    $configuration = Join-Path $ProjectRoot '.venv\pyvenv.cfg'
    $seenConfigurations = @{}
    $maximumDepth = 8
    for ($depth = 0; $depth -lt $maximumDepth; $depth++) {
        if (-not (Test-Path -LiteralPath $configuration -PathType Leaf)) {
            throw 'Missing venv configuration in Python executable chain.'
        }
        $configuration = Resolve-LoungeExistingPath -Path $configuration
        if ($seenConfigurations.ContainsKey($configuration)) {
            throw 'Python venv executable chain contains a cycle.'
        }
        $seenConfigurations[$configuration] = $true

        $executableValues = @()
        foreach ($line in [IO.File]::ReadAllLines($configuration)) {
            if ($line -match '^\s*executable\s*=\s*(.+?)\s*$') {
                $executableValues += $Matches[1]
            }
        }
        if ($executableValues.Count -ne 1) {
            throw 'Venv must declare exactly one base executable.'
        }
        $declaredExecutable = $executableValues[0]
        if (-not [IO.Path]::IsPathRooted($declaredExecutable)) {
            throw 'Venv base executable must use an absolute path.'
        }
        $basePython = Resolve-LoungeExistingPath -Path $declaredExecutable
        if ([IO.Path]::GetFileName($basePython) -ine 'python.exe') {
            throw 'Venv base executable must be python.exe.'
        }

        $scriptsDirectory = [IO.Path]::GetDirectoryName($basePython)
        if ([IO.Path]::GetFileName($scriptsDirectory) -ine 'Scripts') {
            return $basePython
        }
        $candidateRoot = [IO.Path]::GetDirectoryName($scriptsDirectory)
        $candidateConfiguration = Join-Path $candidateRoot 'pyvenv.cfg'
        if (-not (Test-Path -LiteralPath $candidateConfiguration -PathType Leaf)) {
            return $basePython
        }
        $configuration = $candidateConfiguration
    }
    throw "Python venv executable chain exceeds $maximumDepth configurations."
}

function Test-LoungePythonRuntimeIdentity {
    param(
        [Parameter(Mandatory = $true)]$Process,
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [Parameter(Mandatory = $true)][string]$ExpectedPython,
        [Parameter(Mandatory = $true)][string[]]$ExpectedTokens
    )
    try {
        $tokens = @(ConvertFrom-LoungeCommandLine -CommandLine ([string]$Process.CommandLine))
        if (-not (Test-LoungeCanonicalArguments `
            -Tokens $tokens `
            -ExpectedTokens $ExpectedTokens `
            -PathTokenIndexes @(0, 10)
        )) {
            return $false
        }
        $executable = Resolve-LoungeExistingPath -Path ([string]$Process.ExecutablePath)
        if ($executable -ieq $ExpectedPython) { return $true }
        $basePython = Get-LoungeVenvBasePython -ProjectRoot $ProjectRoot
        if ($executable -ine $basePython) { return $false }
        $parentId = [int]$Process.ParentProcessId
        if ($parentId -le 0) { return $false }
        $parent = Get-LoungeProcessInfo -ProcessId $parentId
        if ($null -eq $parent) { return $false }
        $parentExecutable = Resolve-LoungeExistingPath -Path ([string]$parent.ExecutablePath)
        $parentTokens = @(
            ConvertFrom-LoungeCommandLine -CommandLine ([string]$parent.CommandLine)
        )
        return $parentExecutable -ieq $ExpectedPython -and
            (Test-LoungeCanonicalArguments `
                -Tokens $parentTokens `
                -ExpectedTokens $ExpectedTokens `
                -PathTokenIndexes @(0, 10))
    } catch {
        return $false
    }
}

function Find-LoungePythonServiceProcess {
    param(
        [Parameter(Mandatory = $true)][int]$WrapperProcessId,
        [Parameter(Mandatory = $true)][ValidateSet('visitor', 'admin')][string]$Role,
        [Parameter(Mandatory = $true)][string]$ProjectRoot
    )
    $wrapper = Get-LoungeProcessInfo -ProcessId $WrapperProcessId
    if ($null -eq $wrapper -or
        -not (Test-LoungeProcessIdentity `
            -Process $wrapper `
            -Role $Role `
            -ProjectRoot $ProjectRoot
        )) {
        throw "Invalid $Role venv wrapper process."
    }
    $root = Resolve-LoungeExistingPath -Path $ProjectRoot
    $expectedPython = Resolve-LoungeExistingPath `
        -Path (Join-Path $root '.venv\Scripts\python.exe')
    $basePython = Get-LoungeVenvBasePython -ProjectRoot $root
    if ($basePython -ieq $expectedPython) { return $wrapper }

    $deadline = [DateTime]::UtcNow.AddSeconds(5)
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($null -eq (Get-LoungeProcessInfo -ProcessId $WrapperProcessId)) {
            throw "$Role venv wrapper exited before its worker was verified."
        }
        $matches = @(
            Get-CimInstance Win32_Process `
                -Filter "ParentProcessId = $WrapperProcessId" `
                -ErrorAction SilentlyContinue |
                Where-Object {
                    Test-LoungeProcessIdentity `
                        -Process $_ `
                        -Role $Role `
                        -ProjectRoot $root
                }
        )
        if ($matches.Count -eq 1) { return $matches[0] }
        if ($matches.Count -gt 1) {
            throw "Multiple verified $Role workers were found."
        }
        Start-Sleep -Milliseconds 50
    }
    throw "$Role venv worker did not appear before the deadline."
}

function Sync-LoungeServicePidFromLauncher {
    param(
        [Parameter(Mandatory = $true)][ValidateSet('visitor', 'admin')][string]$Role,
        [Parameter(Mandatory = $true)][string]$ProjectRoot
    )
    $runtimeDirectory = Get-LoungeRuntimeDirectory -ProjectRoot $ProjectRoot
    $servicePidFile = Join-Path $runtimeDirectory "$Role.pid"
    if (Test-Path -LiteralPath $servicePidFile -PathType Leaf) { return }
    $launcherPidFile = Join-Path $runtimeDirectory "$Role.launcher.pid"
    if (-not (Test-Path -LiteralPath $launcherPidFile -PathType Leaf)) { return }
    $launcherPid = Read-LoungePid -PidFile $launcherPidFile
    if ($null -eq (Get-LoungeProcessInfo -ProcessId $launcherPid)) { return }
    $serviceProcess = Find-LoungePythonServiceProcess `
        -WrapperProcessId $launcherPid `
        -Role $Role `
        -ProjectRoot $ProjectRoot
    Write-LoungePid `
        -PidFile $servicePidFile `
        -ProcessId ([int]$serviceProcess.ProcessId)
}

function Test-LoungeProcessIdentity {
    param(
        [Parameter(Mandatory = $true)]$Process,
        [Parameter(Mandatory = $true)][ValidateSet('visitor', 'admin', 'supervisor')][string]$Role,
        [Parameter(Mandatory = $true)][string]$ProjectRoot
    )
    $commandLine = [string]$Process.CommandLine
    if ([string]::IsNullOrWhiteSpace($commandLine)) { return $false }
    try {
        $tokens = @(ConvertFrom-LoungeCommandLine -CommandLine $commandLine)
        $root = Resolve-LoungeExistingPath -Path $ProjectRoot
    } catch {
        return $false
    }
    switch ($Role) {
        'visitor' {
            $expectedPython = Join-Path $root '.venv\Scripts\python.exe'
            try { $expectedPython = Resolve-LoungeExistingPath -Path $expectedPython }
            catch { return $false }
            $expectedTokens = @(
                $expectedPython, '-m', 'uvicorn',
                'visitor_lounge.visitor_app:build_visitor_app', '--factory',
                '--host', '127.0.0.1', '--port', '8001', '--app-dir', $root
            )
            return Test-LoungePythonRuntimeIdentity `
                -Process $Process `
                -ProjectRoot $root `
                -ExpectedPython $expectedPython `
                -ExpectedTokens $expectedTokens
        }
        'admin' {
            $expectedPython = Join-Path $root '.venv\Scripts\python.exe'
            try { $expectedPython = Resolve-LoungeExistingPath -Path $expectedPython }
            catch { return $false }
            $expectedTokens = @(
                $expectedPython, '-m', 'uvicorn',
                'visitor_lounge.admin_app:build_admin_app', '--factory',
                '--host', '127.0.0.1', '--port', '8002', '--app-dir', $root
            )
            return Test-LoungePythonRuntimeIdentity `
                -Process $Process `
                -ProjectRoot $root `
                -ExpectedPython $expectedPython `
                -ExpectedTokens $expectedTokens
        }
        'supervisor' {
            try {
                $executable = Resolve-LoungeExistingPath -Path ([string]$Process.ExecutablePath)
            } catch {
                return $false
            }
            $trustedPowerShell = @()
            foreach ($candidate in @(
                (Join-Path $PSHOME 'powershell.exe'),
                (Join-Path $PSHOME 'pwsh.exe'),
                (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe')
            )) {
                if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                    $trustedPowerShell += Resolve-LoungeExistingPath -Path $candidate
                }
            }
            $expectedScript = Join-Path $root 'scripts\supervisor.ps1'
            $expectedTokens = @(
                $executable, '-NoProfile', '-ExecutionPolicy', 'Bypass',
                '-File', $expectedScript, '-ProjectRoot', $root,
                '-LoungeIdentity', 'visitor_lounge'
            )
            return @($trustedPowerShell | Where-Object { $_ -ieq $executable }).Count -gt 0 -and
                (Test-LoungeCanonicalArguments `
                    -Tokens $tokens `
                    -ExpectedTokens $expectedTokens `
                    -PathTokenIndexes @(0, 5, 7))
        }
    }
    return $false
}

function Stop-VerifiedLoungeProcessTree {
    param(
        [Parameter(Mandatory = $true)][int]$ProcessId,
        [Parameter(Mandatory = $true)][ValidateSet('visitor', 'admin', 'supervisor')][string]$Role,
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [switch]$SingleProcess
    )
    $process = Get-LoungeProcessInfo -ProcessId $ProcessId
    if ($null -eq $process) { return }
    if (-not (Test-LoungeProcessIdentity -Process $process -Role $Role -ProjectRoot $ProjectRoot)) {
        throw "Refusing to stop unverified $Role PID $ProcessId."
    }
    $taskkillArguments = @('/PID', [string]$ProcessId, '/F')
    if ($Role -ne 'supervisor' -and -not $SingleProcess) {
        $taskkillArguments += '/T'
    }
    & "$env:SystemRoot\System32\taskkill.exe" @taskkillArguments *> $null
    if ($LASTEXITCODE -ne 0 -and $null -ne (Get-LoungeProcessInfo -ProcessId $ProcessId)) {
        throw "Failed to stop verified $Role PID $ProcessId."
    }
    $deadline = [DateTime]::UtcNow.AddSeconds(5)
    while ($null -ne (Get-LoungeProcessInfo -ProcessId $ProcessId)) {
        if ([DateTime]::UtcNow -ge $deadline) {
            throw "Verified $Role PID $ProcessId did not exit before the deadline."
        }
        Start-Sleep -Milliseconds 50
    }
}

function Write-LoungePid {
    param(
        [Parameter(Mandatory = $true)][string]$PidFile,
        [Parameter(Mandatory = $true)][int]$ProcessId
    )
    [IO.File]::WriteAllText($PidFile, [string]$ProcessId, [Text.Encoding]::ASCII)
}

function ConvertTo-LoungeQuotedArgument {
    param([Parameter(Mandatory = $true)][string]$Value)
    return '"' + $Value.Replace('"', '\"') + '"'
}
