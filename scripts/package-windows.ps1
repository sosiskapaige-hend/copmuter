# Run from the repository root, after self-contained dotnet publish to publish/.
$ErrorActionPreference = 'Stop'
$publish = Join-Path $PWD 'publish'
$pythonVersion = '3.11.9' # Last 3.11 bugfix release with official Windows binaries.
$archive = Join-Path $env:RUNNER_TEMP 'python-embed.zip'
Invoke-WebRequest "https://www.python.org/ftp/python/$pythonVersion/python-$pythonVersion-embed-amd64.zip" -OutFile $archive
Expand-Archive $archive (Join-Path $publish 'python') -Force
# Embedded Python ignores PATH/PYTHONPATH. Resolve ai from the distribution root,
# not from the caller's current directory. No pip/site-packages are required.
@('python311.zip', '.', '..') | Set-Content (Join-Path $publish 'python/python311._pth') -Encoding ascii
Copy-Item ai (Join-Path $publish 'ai') -Recurse
Get-ChildItem (Join-Path $publish 'ai') -Directory -Recurse -Filter __pycache__ | Remove-Item -Recurse -Force
Copy-Item scripts/start-portable.bat (Join-Path $publish 'start.bat')
Copy-Item docs/windows-portable.md (Join-Path $publish 'README.md')
# WinUI and .NET native components can require the VC runtime even though our
# own core uses /MT. Ship the redistributable x64 CRT app-local, not an installer.
$vswhere = "${env:ProgramFiles(x86)}/Microsoft Visual Studio/Installer/vswhere.exe"
$vs = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
$crt = Get-ChildItem "$vs/VC/Redist/MSVC/*/x64/Microsoft.VC143.CRT" -Directory |
    Sort-Object FullName -Descending | Select-Object -First 1
if (!$crt) { throw 'MSVC x64 redistributable CRT directory not found' }
Copy-Item "$($crt.FullName)/*.dll" $publish -Force

$required = @('Copmuter.exe', 'Copmuter.dll', 'Copmuter.runtimeconfig.json',
    'AgentRuntime.dll', 'Microsoft.UI.Xaml.dll', 'Microsoft.WindowsAppRuntime.dll',
    'resources.pri', 'coreclr.dll', 'hostfxr.dll', 'hostpolicy.dll',
    'vcruntime140.dll', 'vcruntime140_1.dll', 'msvcp140.dll',
    'python/python.exe', 'python/python311.dll', 'python/python311.zip',
    'python/python311._pth', 'python/LICENSE.txt', 'ai/main.py',
    'ai/pipe_win.py', 'ai/security/__init__.py', 'ai/tools/__init__.py', 'start.bat')
foreach ($file in $required) {
    if (!(Test-Path (Join-Path $publish $file))) { throw "Missing runtime file: $file" }
}
$runtimeConfig = Get-Content "$publish/Copmuter.runtimeconfig.json" -Raw | ConvertFrom-Json
if ($runtimeConfig.runtimeOptions.framework -or $runtimeConfig.runtimeOptions.frameworks) {
    throw 'Publish is framework-dependent, not self-contained'
}
if ((Get-FileHash "$publish/AgentRuntime.dll").Hash -ne (Get-FileHash 'native/build/AgentRuntime.dll').Hash) {
    throw 'Published native DLL differs from native build output'
}
New-Item artifacts -ItemType Directory -Force | Out-Null
Compress-Archive "$publish/*" 'artifacts/Copmuter-win-x64.zip' -Force
# Validate the actual deliverable after extraction, including relocation/spaces.
$unpacked = Join-Path $env:RUNNER_TEMP 'Copmuter portable smoke'
Expand-Archive 'artifacts/Copmuter-win-x64.zip' $unpacked -Force
foreach ($file in $required) {
    if (!(Test-Path (Join-Path $unpacked $file))) { throw "Missing ZIP entry: $file" }
}
& "$unpacked/python/python.exe" -m ai.main --health --state-dir "$env:RUNNER_TEMP/copmuter-health"
if ($LASTEXITCODE -ne 0) { throw 'Bundled Python health check failed' }
& "$unpacked/python/python.exe" -c "import ctypes; ctypes.CDLL(r'$unpacked/AgentRuntime.dll')"
if ($LASTEXITCODE -ne 0) { throw 'Native DLL load failed' }
# Run outside the checkout, using the extracted app and its bundled worker.
$app = Start-Process "$unpacked/Copmuter.exe" -WorkingDirectory $env:RUNNER_TEMP -PassThru
try {
    Start-Sleep -Seconds 15
    if ($app.HasExited) { throw "Desktop startup failed: exit $($app.ExitCode)" }
    $worker = Get-CimInstance Win32_Process | Where-Object {
        $_.ParentProcessId -eq $app.Id -and $_.Name -eq 'python.exe' -and $_.ExecutablePath -eq "$unpacked\python\python.exe"
    }
    if (!$worker) { throw 'Desktop did not start the bundled AI worker' }
} finally {
    if (!$app.HasExited) { & taskkill /PID $app.Id /T /F | Out-Null }
}
