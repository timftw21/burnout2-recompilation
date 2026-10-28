@echo off
setlocal
set "B2_POWERSHELL=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if exist "%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe" set "B2_POWERSHELL=%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe"
start "" "%B2_POWERSHELL%" -NoProfile -STA -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0cmake\build.ps1"
