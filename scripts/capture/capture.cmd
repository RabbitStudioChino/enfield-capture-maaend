@echo off
setlocal DisableDelayedExpansion
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0capture.ps1" %*
set "CAPTURE_EXIT=%ERRORLEVEL%"
echo Capture exited with code %CAPTURE_EXIT%.
echo Reports are under local\validation\windows-capture in the extracted directory.
pause
exit /b %CAPTURE_EXIT%
