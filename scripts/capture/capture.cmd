@echo off
setlocal DisableDelayedExpansion
set "CAPTURE_MODE=%~1"
if not defined CAPTURE_MODE set "CAPTURE_MODE=doctor"
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0capture.ps1" -Mode "%CAPTURE_MODE%"
set "CAPTURE_EXIT=%ERRORLEVEL%"
echo Capture exited with code %CAPTURE_EXIT%.
echo Reports are under local\validation\windows-capture in the extracted directory.
pause
exit /b %CAPTURE_EXIT%
