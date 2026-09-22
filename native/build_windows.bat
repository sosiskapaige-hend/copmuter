@echo off
rem ============================================================================
rem  Сборка нативного ядра (Windows): AgentRuntime.dll + agent_host.exe
rem  Требуется MSVC (Build Tools или Visual Studio) с окружением vcvars64.
rem
rem    native\build_windows.bat            — Release x64
rem    native\build_windows.bat debug      — Debug
rem ============================================================================
setlocal enabledelayedexpansion

set CONFIG=Release
if /i "%~1"=="debug" set CONFIG=Debug

set ROOT=%~dp0
pushd "%ROOT%"

where cl.exe >nul 2>nul
if errorlevel 1 (
  echo [!] cl.exe не в PATH. Запустите "vcvars64.bat" или Developer Command Prompt.
  popd & exit /b 1
)

if not exist build mkdir build
pushd build

set CORE=..\src\core_intent.cpp ..\src\core_intent_parse.cpp ..\src\core_registry.cpp ^
 ..\src\core_optimizer.cpp ..\src\core_batch.cpp ..\src\core_router.cpp ..\src\core_runtime.cpp ^
 ..\src\core_execute.cpp ..\src\core_util.cpp ..\src\core_wait.cpp ..\src\core_tool_call.cpp ..\src\core_png.cpp ^
 ..\src\core_agent_loop.cpp ..\src\platform_win32.cpp ..\src\ipc_win32.cpp ..\src\abi.cpp

set FLAGS=/nologo /std:c++20 /EHsc /W3 /MP /I..\include /DWIN32_LEAN_AND_MEAN /DNOMINMAX
if /i "%CONFIG%"=="Release" (set FLAGS=%FLAGS% /O2 /GL /DNDEBUG) else (set FLAGS=%FLAGS% /Od /Zi)
set LIBS=user32.lib gdi32.lib shell32.lib ole32.lib oleaut32.lib advapi32.lib shlwapi.lib ^
 psapi.lib dxgi.lib d3d11.lib uuid.lib version.lib winmm.lib

echo == AgentRuntime.dll (ядро, постоянный процесс внутри приложения)
cl %FLAGS% /LD %CORE% /Fe:AgentRuntime.dll /Fo:core_ /link /INCREMENTAL:NO %LIBS% /DEF:..\AgentRuntime.def
if errorlevel 1 (popd & popd & exit /b 1)

echo == agent_host.exe (отладочный запуск ядра без UI)
cl %FLAGS% ..\src\host_main.cpp %CORE% /Fe:agent_host.exe /Fo:host_ /link %LIBS%
if errorlevel 1 (popd & popd & exit /b 1)

echo == agent_scenarios.exe (обязательные сценарии ТЗ)
cl %FLAGS% /I..\tests ..\tests\test_scenarios.cpp %CORE% /Fe:agent_scenarios.exe /Fo:scen_ /link %LIBS%
if errorlevel 1 (popd & popd & exit /b 1)

echo == agent_tests.exe (тесты ядра на моках)
cl %FLAGS% ..\tests\test_core.cpp %CORE% /Fe:agent_tests.exe /Fo:test_ /link %LIBS%
if errorlevel 1 (popd & popd & exit /b 1)

popd
echo.
echo Готово: %ROOT%build\AgentRuntime.dll, agent_host.exe, agent_tests.exe, agent_scenarios.exe
echo Проверка:  build\agent_host.exe "открой телегу"
echo Тесты:     build\agent_tests.exe
popd
endlocal
