@echo off
setlocal DisableDelayedExpansion
call "%~dp0scripts\capture\capture.cmd" %*
exit /b %ERRORLEVEL%
