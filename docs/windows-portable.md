# Copmuter: Windows x64 portable build

Download **Copmuter-win-x64** from the successful GitHub Actions run. Extract
`Copmuter-win-x64.zip` completely into a writable folder; do not run inside the ZIP.
Start `Copmuter.exe` or `start.bat`. Keep all DLLs and subdirectories together.
Windows 10 1809 or newer / Windows 11 x64 is required.

The archive contains the WinUI 3 shell, AgentRuntime.dll, self-contained .NET 8
and Windows App SDK 1.5 runtime, app-local Visual C++ runtime, CPython 3.11.9
embedded x64 (including its license), and the complete `ai/` worker package.
No Visual Studio, MSVC, .NET SDK/runtime or system Python installation is needed.
State/logs are created under `%LOCALAPPDATA%\Copmuter`, not shipped from the runner.
Worker defaults live in `ai/config.py`; it does not consume the repository's
`config.json` (that file belongs to the separate legacy Python application).

For AI tasks, install/run LM Studio separately, load Qwen3-VL-8B-Instruct and enable
its local server at `http://127.0.0.1:1234/v1`. Neither LM Studio nor model weights
are included. `AGENT_LLM_URL`, `AGENT_LLM_MODEL`, `AGENT_LLM_KEY` can override defaults.
The core/worker can start without the model; model-dependent tasks cannot succeed.
The optional Playwright/Chromium browser automation backend is not bundled;
its existing graceful-unavailable behavior is unchanged. Native browser launching
and desktop control do not require it. The legacy optional OCR/voice/web UI stack
in `requirements-optional.txt`, npm packages and `ai_agent.spec` is not used by this
WinUI application and is not silently installed into the artifact.

## Building and checks

`.github/workflows/build-windows.yml`: manual dispatch or push to main on
windows-2022. Runner-only tools: VS 2022 MSVC x64 + Windows SDK, .NET 8 SDK,
Python 3.11.9, PowerShell. NuGet restores the pinned Windows App SDK and Windows
SDK BuildTools from the csproj. Publishing performs restore with the same RID
and self-contained properties as the build.

`native/build_windows.bat` builds all four native targets in Release, with separate
object/PDB directories, C++20, UTF-8 and static MSVC runtime. The workflow checks
DLL exports, runs AI unit tests, verifies runtime files and .NET configuration,
then extracts the ZIP to a relocated path containing spaces and checks Python
health, native DLL loading and desktop/worker process startup without LM Studio.
This is a startup smoke test, not a full interactive UI or real-model acceptance
suite; the Windows runner itself has development runtimes installed.

The root `start.bat` remains the development launcher. The ZIP gets a separate
portable launcher that never builds anything. Native test/host EXEs are compiled
but are not needed to run the desktop app and are not shipped.
