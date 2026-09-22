// Надзор за Python-мозгом: оболочка поднимает воркер один раз при старте и держит его
// живым. Никаких «python на каждую команду» — канал Named Pipes живёт всю сессию.
using System;
using System.Diagnostics;
using System.IO;

namespace Copmuter.Agent.Services;

public sealed class WorkerOptions
{
    public string PythonPath { get; set; } = "python";
    public string RepoRoot { get; set; } = AppContext.BaseDirectory;
    public string SocketPath { get; set; } = @"\\.\pipe\agent_ai_v1";
    public string StateDir { get; set; } = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Copmuter");
    public string LogPath { get; set; } = "";
}

/// <summary>Воркер (Python + Qwen3-VL через LM Studio). Живёт рядом с оболочкой.</summary>
public sealed class WorkerProcess : IDisposable
{
    private readonly WorkerOptions _options;
    private Process? _process;
    private int _restarts;

    public event EventHandler<string>? LogReceived;

    public WorkerProcess(WorkerOptions options)
    {
        _options = options;
        if (string.IsNullOrWhiteSpace(_options.LogPath))
        {
            _options.LogPath = Path.Combine(_options.StateDir, "logs", "ai_worker.log");
        }
    }

    public bool IsRunning => _process is { HasExited: false };

    public void Start()
    {
        if (IsRunning)
        {
            return;
        }
        Directory.CreateDirectory(_options.StateDir);
        Directory.CreateDirectory(Path.GetDirectoryName(_options.LogPath)!);

        var info = new ProcessStartInfo
        {
            FileName = _options.PythonPath,
            Arguments = "-m ai.main --serve",
            WorkingDirectory = _options.RepoRoot,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = System.Text.Encoding.UTF8,
            StandardErrorEncoding = System.Text.Encoding.UTF8,
        };
        info.Environment["AGENT_AI_SOCKET"] = _options.SocketPath;
        info.Environment["AGENT_STATE_DIR"] = _options.StateDir;
        info.Environment["PYTHONIOENCODING"] = "utf-8";
        info.Environment["PYTHONUNBUFFERED"] = "1";

        _process = new Process { StartInfo = info, EnableRaisingEvents = true };
        _process.OutputDataReceived += (_, e) => Publish(e.Data);
        _process.ErrorDataReceived += (_, e) => Publish(e.Data);
        _process.Exited += (_, _) => OnExited();
        _process.Start();
        _process.BeginOutputReadLine();
        _process.BeginErrorReadLine();
        Publish($"воркер запущен: {_options.PythonPath} -m ai.main --serve");
    }

    private void OnExited()
    {
        Publish("воркер остановился");
        // Один честный перезапуск: мозг не должен «умирать молча» посреди сессии.
        if (_restarts++ < 1)
        {
            Publish("перезапускаю воркер");
            Start();
        }
    }

    private void Publish(string? line)
    {
        if (string.IsNullOrWhiteSpace(line))
        {
            return;
        }
        LogReceived?.Invoke(this, line);
        try
        {
            using var writer = new StreamWriter(_options.LogPath, append: true,
                                                System.Text.Encoding.UTF8);
            writer.WriteLine($"{DateTime.Now:HH:mm:ss.fff} {line}");
        }
        catch (IOException)
        {
            // лог не должен ронять процесс
        }
    }

    /// <summary>Проверка готовности канала: воркер отвечает на health.</summary>
    public bool WaitReady(TimeSpan timeout)
    {
        var deadline = DateTime.UtcNow + timeout;
        while (DateTime.UtcNow < deadline)
        {
            if (!IsRunning)
            {
                return false;
            }
            try
            {
                using var probe = new System.IO.Pipes.NamedPipeClientStream(
                    ".", _options.SocketPath.Replace(@"\\.\pipe\", ""), PipeDirection.InOut);
                probe.Connect(150);
                return true;
            }
            catch (Exception)
            {
                System.Threading.Thread.Sleep(120);
            }
        }
        return false;
    }

    public void Dispose()
    {
        try
        {
            if (IsRunning && _process is not null)
            {
                _process.Kill(entireProcessTree: true);
                _process.WaitForExit(2000);
            }
        }
        catch (InvalidOperationException)
        {
        }
        _process?.Dispose();
        _process = null;
    }
}
