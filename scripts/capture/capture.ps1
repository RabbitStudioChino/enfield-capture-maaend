[CmdletBinding(PositionalBinding=$false)]
param(
    [Parameter(Position=0)][ValidateSet('doctor', 'probe', 'start', 'auto', 'serve', 'regions', 'list', 'status', 'pause', 'stop', 'resume', 'logs', 'export')][string]$Mode = 'serve',
    [string]$Workspace,
    [Parameter(ValueFromRemainingArguments=$true)][string[]]$CaptureArgs = @()
)
$ErrorActionPreference = 'Stop'
$script:CaptureScriptPath = $PSCommandPath
$script:WorkspaceWasProvided = $PSBoundParameters.ContainsKey('Workspace')
$script:PythonVersion = '3.12.10'
$script:PythonPackageUrl = 'https://api.nuget.org/v3-flatcontainer/python/3.12.10/python.3.12.10.nupkg'
$script:PythonPackageHash = '0eb85c2dfccccf1b17352de4c397f69194035b7d37149eacc16f1147d93de3b8'

function Assert-WorkspacePath([string]$Path) {
    $full = [IO.Path]::GetFullPath($Path)
    $prefix = $script:WorkspaceRoot.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    if ($full -ne $script:WorkspaceRoot -and -not $full.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Path escapes workspace: $full"
    }
    $current = $full
    while ($current.Length -ge $script:WorkspaceRoot.Length) {
        $item = Get-Item -LiteralPath $current -Force -ErrorAction SilentlyContinue
        if ($item -and ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw "Linked workspace path is forbidden: $current"
        }
        if ($current -eq $script:WorkspaceRoot) { break }
        $current = [IO.Path]::GetDirectoryName($current)
    }
}

function New-LocalDirectory([string]$Path) {
    Assert-WorkspacePath $Path
    [void][IO.Directory]::CreateDirectory($Path)
}

function Write-BootstrapReport {
    $temporary = $script:ReportPath + '.partial'
    Assert-WorkspacePath $temporary
    [IO.File]::WriteAllText($temporary, (($script:Report | ConvertTo-Json -Depth 8) + "`n"), [Text.UTF8Encoding]::new($false))
    if ([IO.File]::Exists($script:ReportPath)) {
        [IO.File]::Replace($temporary, $script:ReportPath, [NullString]::Value)
    } else {
        [IO.File]::Move($temporary, $script:ReportPath)
    }
}

function Write-SetupLog([string]$Message) {
    Write-Host $Message
    [IO.File]::AppendAllText($script:LogPath, ([DateTime]::UtcNow.ToString('o') + ' ' + $Message + "`n"))
}

function Get-PythonPackage([string]$PackagePath) {
    Assert-WorkspacePath $PackagePath
    if ([IO.File]::Exists($PackagePath)) {
        if ((Get-FileHash -LiteralPath $PackagePath -Algorithm SHA256).Hash.ToLowerInvariant() -eq $script:PythonPackageHash) {
            Write-SetupLog 'Using the verified local CPython package.'
            return
        }
        throw "Cached Python package SHA-256 mismatch. Remove this file and run again: $PackagePath"
    }
    $partial = $PackagePath + '.' + [guid]::NewGuid().ToString('N') + '.partial'
    Assert-WorkspacePath $partial
    Write-SetupLog "Downloading official CPython $script:PythonVersion x64 (up to 120 seconds)."
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $script:PythonPackageUrl -OutFile $partial -UseBasicParsing -TimeoutSec 120 -MaximumRedirection 5
        if ((Get-FileHash -LiteralPath $partial -Algorithm SHA256).Hash.ToLowerInvariant() -ne $script:PythonPackageHash) {
            throw 'Downloaded Python package SHA-256 mismatch; it will not be executed.'
        }
        [IO.File]::Move($partial, $PackagePath)
    } finally {
        if ([IO.File]::Exists($partial)) { [IO.File]::Delete($partial) }
    }
}

function Expand-PythonPackage([string]$PackagePath, [string]$Destination) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    New-LocalDirectory $Destination
    $prefix = [IO.Path]::GetFullPath($Destination).TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    $archive = [IO.Compression.ZipFile]::OpenRead($PackagePath)
    try {
        foreach ($entry in $archive.Entries) {
            $name = $entry.FullName
            if (-not $name.StartsWith('tools/', [StringComparison]::Ordinal)) { continue }
            $parts = $name.TrimEnd('/').Split('/')
            if ($name.Contains('\') -or $name.Contains(':') -or ($parts | Where-Object { $_ -eq '.' -or $_ -eq '..' -or $_ -eq '' })) {
                throw "Unsafe Python package entry: $name"
            }
            if ((($entry.ExternalAttributes -shr 16) -band 0xF000) -eq 0xA000 -or ($entry.ExternalAttributes -band 0x400)) {
                throw "Linked Python package entry: $name"
            }
            $target = [IO.Path]::GetFullPath((Join-Path $Destination $name))
            if (-not $target.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
                throw "Python package entry escapes extraction directory: $name"
            }
            Assert-WorkspacePath $target
            if ($name.EndsWith('/')) {
                New-LocalDirectory $target
                continue
            }
            New-LocalDirectory ([IO.Path]::GetDirectoryName($target))
            $inputStream = $entry.Open()
            try {
                $outputStream = [IO.File]::Open($target, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
                try { $inputStream.CopyTo($outputStream) } finally { $outputStream.Dispose() }
            } finally { $inputStream.Dispose() }
        }
    } finally { $archive.Dispose() }
}

function Test-PythonRuntime([string]$Python) {
    Assert-WorkspacePath $Python
    if (-not [IO.File]::Exists($Python)) { return $false }
    $stdout = Join-Path $script:RunPath 'python-check.stdout.log'
    $stderr = Join-Path $script:RunPath 'python-check.stderr.log'
    $code = '"import sys, struct, venv, ensurepip; assert sys.version_info[:3] == (3,12,10) and struct.calcsize(''P'') == 8; print(sys.version)"'
    $process = Start-Process -FilePath $Python -ArgumentList @('-I', '-B', '-c', $code) -NoNewWindow -PassThru -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    try {
        $null = $process.Handle
        if (-not $process.WaitForExit(15000)) {
            $process.Kill()
            [void]$process.WaitForExit(2000)
            throw 'Local CPython validation exceeded 15 seconds.'
        }
        return $process.ExitCode -eq 0
    } finally { $process.Dispose() }
}

function Assert-RuntimeTree([string]$Runtime) {
    $pending = [Collections.Generic.Stack[string]]::new()
    $pending.Push($Runtime)
    while ($pending.Count -gt 0) {
        foreach ($entry in [IO.Directory]::EnumerateFileSystemEntries($pending.Pop())) {
            $attributes = [IO.File]::GetAttributes($entry)
            if ($attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Linked runtime entry is forbidden: $entry" }
            if ($attributes -band [IO.FileAttributes]::Directory) { $pending.Push($entry) }
        }
    }
}

function Invoke-CaptureBootstrap {
    $lock = $null
    $exitCode = 1
    $script:Report = $null
    try {
        if ([string]::IsNullOrWhiteSpace($script:CaptureScriptPath) -or -not [IO.Path]::IsPathRooted($script:CaptureScriptPath)) {
            throw 'Capture script path is unavailable. Run capture.cmd or invoke capture.ps1 with -File.'
        }
        $scriptDirectory = [IO.Path]::GetDirectoryName($script:CaptureScriptPath)
        if (-not $script:WorkspaceWasProvided) {
            $Workspace = Join-Path $scriptDirectory '..\..'
        }
        if ([string]::IsNullOrWhiteSpace($Workspace)) { throw 'Workspace directory must not be empty.' }
        $script:WorkspaceRoot = [IO.Path]::GetFullPath($Workspace).TrimEnd([IO.Path]::DirectorySeparatorChar)
        Assert-WorkspacePath $script:WorkspaceRoot
        if (-not [IO.Directory]::Exists($script:WorkspaceRoot)) { throw 'Workspace directory does not exist.' }
        $results = Join-Path $script:WorkspaceRoot 'local/validation/windows-capture'
        New-LocalDirectory $results
        $script:RunPath = Join-Path $results ('python-bootstrap-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ') + '-' + [guid]::NewGuid().ToString('N'))
        New-LocalDirectory $script:RunPath
        $script:ReportPath = Join-Path $script:RunPath 'report.json'
        $script:LogPath = Join-Path $script:RunPath 'install.log'
        $script:Report = [ordered]@{schema_version='capture.probe.bootstrap/1.0'; status='running'; stage='runtime_setup'; started_utc=[DateTime]::UtcNow.ToString('o'); python_version=$script:PythonVersion; package_url=$script:PythonPackageUrl; package_sha256=$script:PythonPackageHash; errors=@(); child_exit_code=$null}
        Write-BootstrapReport
        Write-SetupLog "Python setup report: $script:ReportPath"
        $temporary = Join-Path $script:WorkspaceRoot 'local/tmp'
        $runtimeRoot = Join-Path $script:WorkspaceRoot 'local/runtime'
        $cache = Join-Path $script:WorkspaceRoot 'local/cache/python'
        foreach ($directory in @($temporary, $runtimeRoot, $cache)) { New-LocalDirectory $directory }
        $env:TMP = $temporary
        $env:TEMP = $temporary
        $env:TMPDIR = $temporary
        $env:PYTHONDONTWRITEBYTECODE = '1'
        $env:PYTHONUTF8 = '1'
        $lockPath = Join-Path $runtimeRoot 'capture-bootstrap.lock'
        Assert-WorkspacePath $lockPath
        $needsSetup = $Mode -in @('doctor', 'probe', 'start', 'auto', 'serve', 'regions', 'list')
        if ($needsSetup) {
            $lock = [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
        }
        $runtime = Join-Path $runtimeRoot 'python-3.12.10'
        $python = Join-Path $runtime 'tools/python.exe'
        if (-not $needsSetup -and -not [IO.File]::Exists($python)) {
            throw 'Capture environment is missing. Run capture.cmd doctor or capture.cmd start first.'
        }
        Assert-WorkspacePath $runtime
        if (-not [IO.Directory]::Exists($runtime)) {
            $package = Join-Path $cache 'python.3.12.10.nupkg'
            Get-PythonPackage $package
            $staging = Join-Path $runtimeRoot ('python-3.12.10.partial-' + [guid]::NewGuid().ToString('N'))
            Write-SetupLog 'Extracting the verified package into the project runtime directory.'
            Expand-PythonPackage $package $staging
            if (-not (Test-PythonRuntime (Join-Path $staging 'tools/python.exe'))) { throw 'Extracted CPython 3.12.10 x64 validation failed.' }
            [IO.File]::WriteAllText((Join-Path $staging 'package.sha256'), $script:PythonPackageHash)
            [IO.Directory]::Move($staging, $runtime)
        }
        $marker = Join-Path $runtime 'package.sha256'
        Assert-WorkspacePath $marker
        if (-not [IO.File]::Exists($marker) -or [IO.File]::ReadAllText($marker).Trim() -ne $script:PythonPackageHash) {
            throw "Existing runtime has no matching package marker. Use a fresh extraction directory: $runtime"
        }
        Assert-RuntimeTree $runtime
        if (-not (Test-PythonRuntime $python)) { throw 'Project CPython is not a working 3.12.10 x64 runtime.' }
        $script:Report.stage = 'setup_and_capture'
        $script:Report.python = $python
        Write-BootstrapReport
        Write-SetupLog 'Project CPython is ready. Starting the existing dependency setup and capture launcher.'
        $bootstrap = Join-Path $scriptDirectory 'bootstrap.py'
        if ($needsSetup) {
            & $python -B $bootstrap --setup-only --workspace $script:WorkspaceRoot | Out-Host
            if ($LASTEXITCODE -ne 0) {
                $script:Report.child_exit_code = $LASTEXITCODE
                $script:Report.status = 'failed'
                return $LASTEXITCODE
            }
        }
        if ($lock) {
            $lock.Dispose()
            $lock = $null
        }
        & $python -B $bootstrap --launch-only --command $Mode --workspace $script:WorkspaceRoot -- @CaptureArgs | Out-Host
        $code = $LASTEXITCODE
        $script:Report.child_exit_code = $code
        $script:Report.status = if ($code -eq 0) { 'complete' } else { 'failed' }
        $exitCode = $code
    } catch {
        $message = $_.Exception.Message
        Write-Host "Capture startup failed: $message" -ForegroundColor Red
        if ($script:Report) {
            $script:Report.status = 'failed'
            $script:Report.errors += $message
            Write-SetupLog $message
        }
        $exitCode = 1
    } finally {
        if ($lock) { $lock.Dispose() }
        if ($script:Report) {
            if ($script:Report.status -eq 'running') {
                $script:Report.status = 'interrupted'
                $script:Report.errors += 'Startup interrupted before completion.'
                $exitCode = 130
            }
            $script:Report.finished_utc = [DateTime]::UtcNow.ToString('o')
            try { Write-BootstrapReport } catch {
                [Console]::Error.WriteLine("$($script:Report.status): cannot finalize startup report: $($_.Exception.Message); errors=$($script:Report.errors)")
                $exitCode = 1
            }
        }
    }
    return $exitCode
}

if ($MyInvocation.InvocationName -ne '.') { exit (Invoke-CaptureBootstrap) }
