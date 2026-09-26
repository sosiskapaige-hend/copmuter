# Run in Windows PowerShell (STA). Exercise the extracted WinUI app, not a mock.
param([Parameter(Mandatory=$true)][int]$AppProcessId,
      [Parameter(Mandatory=$true)][string]$OutputDirectory)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes, System.Drawing
New-Item $OutputDirectory -ItemType Directory -Force | Out-Null
$deadline = (Get-Date).AddSeconds(25)
do {
    $window = [System.Windows.Automation.AutomationElement]::RootElement.FindFirst(
        [System.Windows.Automation.TreeScope]::Children,
        [System.Windows.Automation.PropertyCondition]::new(
            [System.Windows.Automation.AutomationElement]::ProcessIdProperty, $AppProcessId))
    if (!$window) { Start-Sleep -Milliseconds 250 }
} until ($window -or (Get-Date) -gt $deadline)
if (!$window) { throw 'WinUI window is not exposed to UI Automation' }

function Find-Control([string]$Id) {
    $control = $window.FindFirst([System.Windows.Automation.TreeScope]::Descendants,
        [System.Windows.Automation.PropertyCondition]::new(
            [System.Windows.Automation.AutomationElement]::AutomationIdProperty, $Id))
    if (!$control) { throw "Missing control: $Id" }
    return $control
}
function Invoke-Control([string]$Id) {
    $control = Find-Control $Id
    if (!$control.Current.IsEnabled) { throw "Disabled control: $Id" }
    $control.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
    Start-Sleep -Milliseconds 250
}
function Capture-Window([string]$Name) {
    $r = $window.Current.BoundingRectangle
    $bitmap = [System.Drawing.Bitmap]::new([int]$r.Width, [int]$r.Height)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    try {
        $graphics.CopyFromScreen([int]$r.X, [int]$r.Y, 0, 0, $bitmap.Size)
        $bitmap.Save((Join-Path $OutputDirectory "$Name.png"), [System.Drawing.Imaging.ImageFormat]::Png)
    } finally { $graphics.Dispose(); $bitmap.Dispose() }
}

Invoke-Control 'NewChat'
Capture-Window '01-home'
$input = Find-Control 'TaskInput'
$input.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).SetValue('создай папку CopmuterUiSmoke')
$plan = Find-Control 'PlanOnly'
$toggle = $plan.GetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern)
if ($toggle.Current.ToggleState -ne [System.Windows.Automation.ToggleState]::On) { $toggle.Toggle() }
$deadline = (Get-Date).AddSeconds(20)
while (!(Find-Control 'Send').Current.IsEnabled -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 250 }
Invoke-Control 'Send'
$deadline = (Get-Date).AddSeconds(10)
while ((Find-Control 'Stop').Current.IsEnabled -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 100 }
if ((Find-Control 'Stop').Current.IsEnabled) { throw 'Plan command did not finish / UI blocked' }
$texts = $window.FindAll([System.Windows.Automation.TreeScope]::Descendants,
    [System.Windows.Automation.PropertyCondition]::new(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Text))
$names = @($texts | ForEach-Object { $_.Current.Name })
if (!($names | Where-Object { $_ -match 'Подтверждение|Потребуется подтверждение|Маршрут|Инструмент|Намерение' })) {
    $names | Out-File (Join-Path $OutputDirectory 'ui-text.txt') -Encoding utf8
    throw 'No native preview result found in chat'
}
Capture-Window '02-plan'
Invoke-Control 'Settings'
$slider = Find-Control 'GlassDensity'
$slider.GetCurrentPattern([System.Windows.Automation.RangeValuePattern]::Pattern).SetValue(0.9)
Capture-Window '03-settings'
Invoke-Control 'Diagnostics'
Invoke-Control 'RefreshDiagnostics'
Start-Sleep -Milliseconds 500
Invoke-Control 'Journal'
Invoke-Control 'RefreshJournal'
'PASS: new task, input, plan-only, send, settings, density, diagnostics, journal' |
    Out-File (Join-Path $OutputDirectory 'ui-smoke.txt') -Encoding utf8
