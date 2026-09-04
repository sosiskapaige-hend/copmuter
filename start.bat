@echo off
chcp 65001 >nul
title AI Computer Agent
cd /d %~dp0

rem Запуск приложения БЕЗ сборки в EXE (нужен только Python 3.10+)
where python >nul 2>nul
if errorlevel 1 (
    echo [ОШИБКА] Python не найден. Установите Python 3.10+ с python.org,
    echo отметив "Add Python to PATH".
    pause
    exit /b 1
)

rem в первый раз докачает небольшие зависимости окна
python -c "import webview" >nul 2>nul
if errorlevel 1 python -m pip install --quiet pywebview mss psutil

python desktop.py
pause
