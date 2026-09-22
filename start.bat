@echo off
chcp 65001 >nul
title Copmuter — локальный агент
cd /d %~dp0

rem Запуск: ядро + Python-мозг + окно WinUI 3.
rem Модель должна быть поднята в LM Studio (Qwen3-VL-8B-Instruct, сервер на :1234).

set STATE=%LOCALAPPDATA%\Copmuter

if not exist "native\build\AgentRuntime.dll" (
    echo [1/3] Собираю ядро...
    call native\build_windows.bat || (echo Ядро не собралось & pause & exit /b 1)
)

if not exist "shell\Copmuter.Agent\bin\Release\net8.0-windows10.0.19041.0\Copmuter.exe" (
    echo [2/3] Собираю оболочку...
    dotnet build shell\Copmuter.Agent\Copmuter.Agent.csproj -c Release || (echo Оболочка не собралась & pause & exit /b 1)
)

echo [3/3] Проверяю мозг и модель...
python -m ai.main --preflight --state-dir "%STATE%"
if errorlevel 1 (
    echo.
    echo [!] Модель не готова. Запустите LM Studio, загрузите Qwen3-VL-8B-Instruct
    echo     и включите сервер на http://127.0.0.1:1234 — затем запустите start.bat снова.
    echo     Простые команды при этом работают и без модели.
    echo.
)

start "" "shell\Copmuter.Agent\bin\Release\net8.0-windows10.0.19041.0\Copmuter.exe"
