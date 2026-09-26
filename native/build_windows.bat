@echo off
setlocal EnableExtensions

rem Builds AgentRuntime.dll, host, scenarios, and tests with MSVC x64.
set "CONFIG=Release"
if /i "%~1"=="debug" set "CONFIG=Debug"
set "ROOT=%~dp0"
pushd "%ROOT%"

where cl.exe >nul 2>nul
if not errorlevel 1 if /i "%VSCMD_ARG_TGT_ARCH%"=="x64" goto :compiler_ready

rem Locate MSVC automatically when launched outside a Developer Command Prompt.
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
if not exist "%VSWHERE%" goto :compiler_missing
set "VCVARS="
for /f "usebackq tokens=*" %%I in (`"%VSWHERE%" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`) do set "VCVARS=%%I\VC\Auxiliary\Build\vcvars64.bat"
if not defined VCVARS goto :compiler_missing
if not exist "%VCVARS%" goto :compiler_missing
call "%VCVARS%" >nul
if errorlevel 1 goto :compiler_missing
where cl.exe >nul 2>nul
if errorlevel 1 goto :compiler_missing

:compiler_ready
if not exist build mkdir build
pushd build
for %%D in (core host scenarios tests) do (
    if not exist "obj\%%D" mkdir "obj\%%D"
    if errorlevel 1 goto :compile_failed
)

set CORE=..\src\core_intent.cpp ..\src\core_intent_parse.cpp ..\src\core_registry.cpp ..\src\core_optimizer.cpp ..\src\core_batch.cpp ..\src\core_router.cpp ..\src\core_runtime.cpp ..\src\core_execute.cpp ..\src\core_util.cpp ..\src\core_wait.cpp ..\src\core_tool_call.cpp ..\src\core_png.cpp ..\src\core_agent_loop.cpp ..\src\platform_win32.cpp ..\src\ipc_win32.cpp ..\src\abi.cpp
set FLAGS=/nologo /std:c++20 /EHsc /utf-8 /W3 /MP /I..\include /DWIN32_LEAN_AND_MEAN /DNOMINMAX
if /i "%CONFIG%"=="Release" set FLAGS=%FLAGS% /O2 /GL /MT /DNDEBUG
if /i "%CONFIG%"=="Debug" set FLAGS=%FLAGS% /Od /Zi /MTd
set LIBS=user32.lib gdi32.lib shell32.lib ole32.lib oleaut32.lib advapi32.lib shlwapi.lib psapi.lib dxgi.lib d3d11.lib uuid.lib version.lib winmm.lib ws2_32.lib

echo === AgentRuntime.dll ===
cl %FLAGS% /LD %CORE% /Fe:AgentRuntime.dll /Foobj\core\ /Fdobj\core\compiler.pdb /link /INCREMENTAL:NO %LIBS%
if errorlevel 1 goto :compile_failed

echo === agent_host.exe ===
cl %FLAGS% ..\src\host_main.cpp %CORE% /Fe:agent_host.exe /Foobj\host\ /Fdobj\host\compiler.pdb /link %LIBS%
if errorlevel 1 goto :compile_failed

echo === agent_scenarios.exe ===
cl %FLAGS% /I..\tests ..\tests\test_scenarios.cpp %CORE% /Fe:agent_scenarios.exe /Foobj\scenarios\ /Fdobj\scenarios\compiler.pdb /link %LIBS%
if errorlevel 1 goto :compile_failed

echo === agent_tests.exe ===
cl %FLAGS% ..\tests\test_core.cpp %CORE% /Fe:agent_tests.exe /Foobj\tests\ /Fdobj\tests\compiler.pdb /link %LIBS%
if errorlevel 1 goto :compile_failed

popd
echo Native build completed: %ROOT%build
popd
exit /b 0

:compile_failed
popd
popd
echo [ERROR] Native C++ compilation failed.
exit /b 1

:compiler_missing
popd
echo [ERROR] MSVC x64 compiler was not found.
echo         Install Visual Studio 2022 Build Tools with "Desktop development with C++".
echo         Then run this file again; vcvars64.bat will be loaded automatically.
exit /b 1
