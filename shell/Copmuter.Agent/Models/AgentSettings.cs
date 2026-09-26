// Настройки оболочки: то, что пользователь меняет в окне.
using System;
using System.ComponentModel;
using System.IO;
using System.Runtime.CompilerServices;

namespace Copmuter.Agent.Models;

public sealed class AgentSettings : INotifyPropertyChanged
{
    private string _stateDir = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Copmuter");
    private string _socketPath = @"\\.\pipe\agent_ai_v1";
    private string _pythonPath = File.Exists(Path.Combine(AppContext.BaseDirectory, "python", "python.exe"))
        ? Path.Combine(AppContext.BaseDirectory, "python", "python.exe") : "python";
    private string _repoRoot = AppContext.BaseDirectory;
    private string _safetyMode = "auto";
    private string _defaultBrowser = "";
    private string _language = "ru";
    private bool _preload = true;
    private bool _planOnly;
    private int _launchTimeoutMs = 12000;
    private int _visionTimeoutMs = 20000;
    private int _maxRetries = 2;
    private int _confirmTimeoutMs = 30000;
    private bool _debugLog;
    private bool _fastRouteDirect = true;
    private string _modelEndpoint = "http://127.0.0.1:1234/v1";
    private string _visionModel = "qwen3-vl-8b-instruct";

    public string StateDir { get => _stateDir; set => Set(ref _stateDir, value); }
    public string SocketPath { get => _socketPath; set => Set(ref _socketPath, value); }
    [System.Text.Json.Serialization.JsonIgnore]
    public string PythonPath { get => _pythonPath; set => Set(ref _pythonPath, value); }
    [System.Text.Json.Serialization.JsonIgnore]
    public string RepoRoot { get => _repoRoot; set => Set(ref _repoRoot, value); }
    /// <summary>full | auto | confirm | step | observe | plan_only</summary>
    public string SafetyMode { get => _safetyMode; set => Set(ref _safetyMode, value); }
    public string DefaultBrowser { get => _defaultBrowser; set => Set(ref _defaultBrowser, value); }
    public string Language { get => _language; set => Set(ref _language, value); }
    public bool Preload { get => _preload; set => Set(ref _preload, value); }
    /// <summary>PLAN ONLY: показывать план и ничего не выполнять.</summary>
    public bool PlanOnly { get => _planOnly; set => Set(ref _planOnly, value); }
    public int LaunchTimeoutMs { get => _launchTimeoutMs; set => Set(ref _launchTimeoutMs, value); }
    public int VisionTimeoutMs { get => _visionTimeoutMs; set => Set(ref _visionTimeoutMs, value); }
    public int MaxRetries { get => _maxRetries; set => Set(ref _maxRetries, value); }
    /// <summary>Сколько ждать ответа на «подтвердите опасное действие» (мс).</summary>
    public int ConfirmTimeoutMs { get => _confirmTimeoutMs; set => Set(ref _confirmTimeoutMs, value); }
    /// <summary>Журнал отладки ядра: state_dir\agent_debug.log (для разбора проблем).</summary>
    public bool DebugLog { get => _debugLog; set => Set(ref _debugLog, value); }

    public bool FastRouteDirect { get => _fastRouteDirect; set => Set(ref _fastRouteDirect, value); }
    public string ModelEndpoint { get => _modelEndpoint; set => Set(ref _modelEndpoint, value); }
    public string VisionModel { get => _visionModel; set => Set(ref _visionModel, value); }

    public event PropertyChangedEventHandler? PropertyChanged;

    private void Set<T>(ref T field, T value, [CallerMemberName] string? name = null)
    {
        if (Equals(field, value))
        {
            return;
        }
        field = value;
        PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));
    }
}
