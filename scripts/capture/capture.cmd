@echo off
setlocal
set "PYLAUNCHER_ALLOW_INSTALL="
set "CAPTURE_SOURCE_ROOT=%~dp0..\.."
set "CAPTURE_MODE=%~1"
if not defined CAPTURE_MODE set "CAPTURE_MODE=doctor"
py -3.12 -c "import sys; assert sys.version_info[:2] == (3,12) and sys.maxsize > 2**32" >nul 2>&1
if not errorlevel 1 (
    py -3.12 "%~dp0bootstrap.py" --command "%CAPTURE_MODE%"
    goto finished
)
python -c "import sys; assert sys.version_info[:2] == (3,12) and sys.maxsize > 2**32" >nul 2>&1
if not errorlevel 1 (
    python "%~dp0bootstrap.py" --command "%CAPTURE_MODE%"
    goto finished
)
echo Python 3.12 x64 is required. Install it, then run this file again.
powershell.exe -NoProfile -Command "$ErrorActionPreference='Stop'; $root=[IO.Path]::GetFullPath($env:CAPTURE_SOURCE_ROOT); if((Get-Item -LiteralPath $root).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Linked workspace is forbidden' }; $p=$root; foreach($part in @('local','validation','windows-capture')) { $p=Join-Path $p $part; if(Test-Path -LiteralPath $p) { if((Get-Item -LiteralPath $p).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Linked output directory is forbidden' } }; [void][IO.Directory]::CreateDirectory($p) }; $p=Join-Path $p ('missing-python-'+[guid]::NewGuid().ToString('N')); [void][IO.Directory]::CreateDirectory($p); @{schema_version='capture.probe.bootstrap/1.0';status='failed';errors=@('Python 3.12 x64 is required')} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $p 'report.json') -Encoding UTF8; Write-Host ('Setup report: '+$p)"
pause
exit /b 1
:finished
set "CAPTURE_EXIT=%ERRORLEVEL%"
echo Capture exited with code %CAPTURE_EXIT%.
pause
exit /b %CAPTURE_EXIT%
