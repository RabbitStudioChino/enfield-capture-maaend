param(
    [ValidateSet('doctor', 'probe')][string]$Mode = 'doctor',
    [string]$Workspace = (Join-Path $PSScriptRoot '..\..')
)
$ErrorActionPreference = 'Stop'
$env:PYLAUNCHER_ALLOW_INSTALL = ''
$bootstrap = Join-Path $PSScriptRoot 'bootstrap.py'
if (Get-Command py -ErrorAction SilentlyContinue) {
    try { & py -3.12 -c "import sys; assert sys.version_info[:2] == (3,12) and sys.maxsize > 2**32" 2>$null } catch { $LASTEXITCODE = 1 }
    if ($LASTEXITCODE -eq 0) {
        & py -3.12 $bootstrap --command $Mode --workspace $Workspace
        exit $LASTEXITCODE
    }
}
if (Get-Command python -ErrorAction SilentlyContinue) {
    try { & python -c "import sys; assert sys.version_info[:2] == (3,12) and sys.maxsize > 2**32" 2>$null } catch { $LASTEXITCODE = 1 }
    if ($LASTEXITCODE -eq 0) {
        & python $bootstrap --command $Mode --workspace $Workspace
        exit $LASTEXITCODE
    }
}
$path = [IO.Path]::GetFullPath($Workspace)
if ((Get-Item -LiteralPath $path).Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw 'Linked workspace is forbidden'
}
foreach ($part in @('local', 'validation', 'windows-capture')) {
    $path = Join-Path $path $part
    if (Test-Path -LiteralPath $path) {
        if ((Get-Item -LiteralPath $path).Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw 'Linked output directory is forbidden'
        }
    }
    [void][IO.Directory]::CreateDirectory($path)
}
$path = Join-Path $path ('missing-python-' + [guid]::NewGuid().ToString('N'))
[void][IO.Directory]::CreateDirectory($path)
@{schema_version='capture.probe.bootstrap/1.0';status='failed';errors=@('Python 3.12 x64 is required')} |
    ConvertTo-Json | Set-Content -LiteralPath (Join-Path $path 'report.json') -Encoding UTF8
Write-Host "Python 3.12 x64 is required. Setup report: $path"
exit 1
