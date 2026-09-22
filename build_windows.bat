@echo off
rem Полная сборка Copmuter: ядро (C++/MSVC) + оболочка (C#/WinUI 3).
setlocal
cd /d %~dp0

echo == Ядро: AgentRuntime.dll, agent_host.exe, agent_tests.exe
call native\build_windows.bat || (echo [!] Ядро не собралось & exit /b 1)

echo.
echo == Оболочка: Copmuter.exe (WinUI 3)
dotnet build shell\Copmuter.Agent\Copmuter.Agent.csproj -c Release || (echo [!] Оболочка не собралась & exit /b 1)

echo.
echo Готово.
echo   Ядро без UI:  native\build\agent_host.exe "открой телегу"
echo   Приложение:   start.bat
endlocal
