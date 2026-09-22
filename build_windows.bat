@echo off
setlocal
cd /d "%~dp0"

rem Keep batch files ASCII-only. Non-ASCII text can break cmd.exe block parsing.
echo === Copmuter Windows build ===

where dotnet.exe >nul 2>nul
if errorlevel 1 (
    echo [ERROR] .NET 8 SDK was not found in PATH.
    echo         Download it from https://dotnet.microsoft.com/download/dotnet/8.0
    exit /b 1
)

echo.
echo [1/2] Building native C++ runtime...
call "%~dp0native\build_windows.bat"
if errorlevel 1 exit /b 1

echo.
echo [2/2] Building WinUI application...
dotnet build "%~dp0shell\Copmuter.Agent\Copmuter.Agent.csproj" -c Release -p:Platform=x64 -p:AppendPlatformToOutputPath=false
if errorlevel 1 (
    echo [ERROR] WinUI build failed.
    exit /b 1
)

echo.
echo Build completed successfully.
echo Application: shell\Copmuter.Agent\bin\Release\net8.0-windows10.0.19041.0\Copmuter.exe
exit /b 0
