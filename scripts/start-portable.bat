@echo off
setlocal
cd /d "%~dp0"
if not exist "Copmuter.exe" (
    echo [ERROR] Extract the entire ZIP before running start.bat.
    pause
    exit /b 1
)
start "" "%~dp0Copmuter.exe"
