@echo off
chcp 65001 >nul
title AI Computer Agent — сборка EXE
cd /d %~dp0

echo ============================================================
echo   AI COMPUTER AGENT — сборка Windows EXE (1 клик)
echo ============================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [ОШИБКА] Python не найден в PATH.
    echo Установите Python 3.10+ с https://www.python.org/downloads/
    echo ВАЖНО: поставьте галочку "Add Python to PATH" при установке.
    echo.
    pause
    exit /b 1
)

python --version
echo.
echo [1/4] Устанавливаю зависимости...
python -m pip install --upgrade pip >nul
python -m pip install pywebview pyinstaller mss psutil pypdf pillow
if errorlevel 1 (
    echo [ОШИБКА] Не удалось установить зависимости (нужен доступ в интернет).
    pause
    exit /b 1
)

echo.
echo [2/4] Устанавливаю управляемый браузер Chromium (Playwright, ~170 МБ)...
python -m pip install playwright >nul
python -m playwright install chromium
if errorlevel 1 (
    echo [ПРЕДУПРЕЖДЕНИЕ] Chromium не установился — браузер-режим будет
    echo использовать системный браузер. Прочий функционал не пострадает.
)

echo.
echo [3/4] Собираю EXE (несколько минут, не закрывайте окно)...
python -m PyInstaller --noconfirm --clean ai_agent.spec
if errorlevel 1 (
    echo [ОШИБКА] Сборка не удалась. Прокрутите вывод выше.
    pause
    exit /b 1
)

echo.
echo [4/4] Готово!
echo ============================================================
echo.
echo   EXE-файл:  dist\AIComputerAgent.exe
echo.
echo   Запуск:    двойной клик по dist\AIComputerAgent.exe
echo   (можно перетащить в меню "Пуск" или на рабочий стол)
echo.
echo   Для переноса на другой ПК достаточно самого EXE-файла.
echo ============================================================
echo.
pause
