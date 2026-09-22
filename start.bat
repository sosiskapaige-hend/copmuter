@echo off
setlocal
chcp 65001 >nul 2>nul
title Copmuter
cd /d "%~dp0"

rem Keep this file ASCII-only: cmd.exe can corrupt UTF-8 text inside (...) blocks.
set "STATE=%LOCALAPPDATA%\Copmuter"
set "APP=shell\Copmuter.Agent\bin\Release\net8.0-windows10.0.19041.0\Copmuter.exe"

if not exist "native\build\AgentRuntime.dll" goto :build
if not exist "%APP%" goto :build
goto :preflight

:build
echo [1/3] Building Copmuter...
call "%~dp0build_windows.bat"
if errorlevel 1 goto :build_failed

:preflight
echo [2/3] Checking Python and the local model...
where py.exe >nul 2>nul
if not errorlevel 1 (
    py -3 -m ai.main --preflight --state-dir "%STATE%"
) else (
    where python.exe >nul 2>nul
    if errorlevel 1 goto :python_missing
    python -m ai.main --preflight --state-dir "%STATE%"
)
if errorlevel 1 echo [WARNING] LM Studio model is not ready. Simple commands can still work.

echo [3/3] Starting Copmuter...
if not exist "%APP%" goto :app_missing
start "" "%APP%"
exit /b 0

:python_missing
echo [WARNING] Python 3 was not found. Install Python 3.10 or newer and add it to PATH.
goto :launch

:launch
echo [3/3] Starting Copmuter...
if not exist "%APP%" goto :app_missing
start "" "%APP%"
exit /b 0

:build_failed
echo.
echo [ERROR] Build failed. Read the first error above.
pause
exit /b 1

:app_missing
echo [ERROR] Copmuter.exe was not created at:
echo         %APP%
pause
exit /b 1
