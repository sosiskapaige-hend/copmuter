// Логика окна: ввод → нативное ядро → поток событий → чат и статусы.
//
// Здесь нет ни одного низкоуровневого вызова: за Win32/SendInput/реестр/файлы отвечает
// AgentRuntime.dll, за рассуждения — Python-воркер. Оболочка показывает, что происходит.
using System;
using System.Collections.Concurrent;
using System.Collections.ObjectModel;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using Copmuter.Agent.Models;
using Copmuter.Agent.Services;

namespace Copmuter.Agent.ViewModels;

public sealed class ChatViewModel : ObservableObject
{
    private readonly NativeRuntime _runtime = new();
    private readonly WorkerProcess _worker;
    private readonly ConcurrentQueue<RuntimeEvent> _pending = new();
    private readonly Timer _uiPump;
    private string _input = "";
    private string _status = "Готов";
    private string _metrics = "";
    private bool _busy;

    public ChatViewModel(AgentSettings settings)
    {
        Settings = settings;
        _worker = new WorkerProcess(new WorkerOptions
        {
            PythonPath = settings.PythonPath,
            RepoRoot = settings.RepoRoot,
            SocketPath = settings.SocketPath,
            StateDir = settings.StateDir,
        });
        _worker.LogReceived += (_, line) => Status = line;
        _runtime.EventReceived += (_, ev) => _pending.Enqueue(ev);
        // События копятся в очереди и разбираются в UI-потоке: никаких кросспоточных правок.
        _uiPump = new Timer(_ => Drain(), null, 60, 60);
    }

    public AgentSettings Settings { get; }
    public ObservableCollection<ChatMessage> Messages { get; } = new();

    public string Input
    {
        get => _input;
        set => SetProperty(ref _input, value);
    }

    public string Status
    {
        get => _status;
        private set => SetProperty(ref _status, value);
    }

    public string Metrics
    {
        get => _metrics;
        private set => SetProperty(ref _metrics, value);
    }

    public bool IsBusy
    {
        get => _busy;
        private set => SetProperty(ref _busy, value);
    }

    public void Start()
    {
        try
        {
            _worker.Start();
            _runtime.Start(Settings);
            Status = "Ядро и мозг готовы";
            RefreshMetrics();
        }
        catch (Exception ex)
        {
            Add("ошибка запуска: " + ex.Message, MessageRole.Error);
        }
    }

    public async Task SendAsync()
    {
        var text = Input.Trim();
        if (text.Length == 0 || IsBusy)
        {
            return;
        }
        Input = "";
        Messages.Add(new ChatMessage { Role = MessageRole.User, Text = text });
        IsBusy = true;
        Status = "Выполняю";
        var planOnly = Settings.PlanOnly;
        try
        {
            await Task.Run(() =>
            {
                if (planOnly)
                {
                    var plan = _runtime.Preview(text);
                    Add(plan, MessageRole.Agent);
                    return;
                }
                var outcome = _runtime.Execute(text);
                if (outcome.NeedsLlm && !string.IsNullOrEmpty(Settings.SocketPath))
                {
                    // Сложная задача: тот же вызов, но уже с агентом (план → инструменты → проверка).
                    var task = _runtime.RunTask(text);
                    Report(task.Message.Length > 0 ? task.Message : Describe(outcome));
                    return;
                }
                Report(Describe(outcome));
            });
        }
        catch (Exception ex)
        {
            Add("сбой: " + ex.Message, MessageRole.Error);
        }
        finally
        {
            IsBusy = false;
            Status = "Готов";
            RefreshMetrics();
        }
    }

    public void Stop()
    {
        _runtime.Cancel();
        Status = "Останавливаю";
    }

    public void Shutdown()
    {
        _uiPump.Dispose();
        _runtime.Dispose();
        _worker.Dispose();
    }

    private static string Describe(NativeOutcome outcome)
    {
        if (!outcome.Handled)
        {
            return outcome.NeedsLlm ? "нужна модель" : "не понял команду";
        }
        return outcome.Message;
    }

    private void Report(string message)
    {
        Add(message, MessageRole.Agent);
    }

    private void Drain()
    {
        while (_pending.TryDequeue(out var ev))
        {
            if (ev.Kind == "tool_call" || ev.Kind == "observation")
            {
                // Инструменты показываем строкой, а не отдельными пузырями: чат не должен
                // превращаться в лог.
                Status = ev.Message;
                continue;
            }
            if (ev.Kind == "thought")
            {
                continue;   // рассуждения модели пользователю не показываем (ТЗ)
            }
            if (ev.Kind == "task_done")
            {
                Status = ev.Message;
                continue;
            }
            if (!string.IsNullOrEmpty(ev.Message))
            {
                Add(ev.Message, ev.Status == "failed" ? MessageRole.Error : MessageRole.Status);
            }
        }
        RefreshMetrics();
    }

    private void Add(string text, MessageRole role)
    {
        if (string.IsNullOrWhiteSpace(text))
        {
            return;
        }
        Messages.Add(new ChatMessage { Role = role, Text = text });
    }

    private void RefreshMetrics()
    {
        try
        {
            var json = _runtime.MetricsJson();
            if (json.Length > 2)
            {
                Metrics = json.Length > 160 ? json.Substring(0, 160) + "…" : json;
            }
        }
        catch (Exception)
        {
            // метрики не критичны для работы
        }
    }
}
