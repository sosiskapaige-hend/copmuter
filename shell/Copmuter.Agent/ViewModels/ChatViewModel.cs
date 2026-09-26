using System;
using System.Collections.Concurrent;
using System.Collections.ObjectModel;
using System.Linq;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using Copmuter.Agent.Models;
using Copmuter.Agent.Services;
using Microsoft.UI.Dispatching;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Media;

namespace Copmuter.Agent.ViewModels;

public enum AppTab { Chat, Diagnostics, Journal, Settings }

public sealed class ChatViewModel : ObservableObject
{
    private readonly NativeRuntime _runtime = new();
    private WorkerProcess? _worker;
    private readonly ConcurrentQueue<RuntimeEvent> _pending = new();
    private readonly DispatcherQueue _dispatcher = DispatcherQueue.GetForCurrentThread();
    private readonly Timer _uiPump;
    private Task _operation = Task.CompletedTask;
    private Chat? _runningChat;
    private Chat _activeChat;
    private string _input = "", _status = "Запуск…", _search = "";
    private string _metrics = "Нажмите «Обновить», чтобы получить данные ядра.";
    private string _tools = "", _journal = "", _modelTitle = "Модель не проверена", _modelDetail = "";
    private bool _busy, _ready, _closing, _modelOnline, _checkingModel, _executing;
    private double _glassOpacity = 0.86;
    private AppTab _tab;

    public ChatViewModel(WorkspaceData workspace)
    {
        Settings = workspace.Settings ?? new();
        _glassOpacity = Math.Clamp(workspace.GlassOpacity, 0.35, 1);
        foreach (var chat in workspace.Chats ?? new()) Chats.Add(chat);
        if (Chats.Count == 0) Chats.Add(new Chat());
        _activeChat = Chats[0];
        RebuildFilter();
        _runtime.EventReceived += (_, ev) => _pending.Enqueue(ev);
        // Drain events only. Never query the native runtime from this UI timer:
        // its serial ABI mutex may be held by a command awaiting confirmation.
        _uiPump = new Timer(_ => _dispatcher.TryEnqueue(Drain), null, 80, 80);
    }

    public AgentSettings Settings { get; }
    public ObservableCollection<Chat> Chats { get; } = new();
    public ObservableCollection<Chat> FilteredChats { get; } = new();
    public Chat ActiveChat
    {
        get => _activeChat;
        set
        {
            if (value is null || !SetProperty(ref _activeChat, value)) return;
            RaiseChatState();
        }
    }
    public string Input { get => _input; set { SetProperty(ref _input, value ?? ""); Raise(nameof(CanSend)); } }
    public string SearchText { get => _search; set { if (SetProperty(ref _search, value ?? "")) RebuildFilter(); } }
    public string Status { get => _status; private set => SetProperty(ref _status, value); }
    public string Metrics { get => _metrics; private set => SetProperty(ref _metrics, value); }
    public string Tools { get => _tools; private set => SetProperty(ref _tools, value); }
    public string Journal { get => _journal; private set => SetProperty(ref _journal, value); }
    public string ModelTitle { get => _modelTitle; private set => SetProperty(ref _modelTitle, value); }
    public string ModelDetail { get => _modelDetail; private set => SetProperty(ref _modelDetail, value); }
    public Brush ModelDotBrush => new SolidColorBrush(_modelOnline
        ? Windows.UI.Color.FromArgb(255, 0x46, 0xDB, 0xAA)
        : Windows.UI.Color.FromArgb(255, 0x91, 0xA7, 0xAB));
    public bool IsBusy
    {
        get => _busy;
        private set
        {
            SetProperty(ref _busy, value);
            Raise(nameof(CanSend)); Raise(nameof(CanEdit)); Raise(nameof(CanStop));
        }
    }
    public bool CanSend => _ready && !IsBusy && !_closing && !string.IsNullOrWhiteSpace(Input);
    public bool CanEdit => !IsBusy && !_closing;
    public bool CanStop => IsBusy && _executing;
    public double GlassOpacity
    {
        get => _glassOpacity;
        set => SetProperty(ref _glassOpacity, Math.Clamp(value, 0.35, 1));
    }
    public AppTab Tab
    {
        get => _tab;
        set
        {
            if (!SetProperty(ref _tab, value)) return;
            Raise(nameof(ChatPanelVisibility)); Raise(nameof(DiagnosticsPanelVisibility));
            Raise(nameof(JournalPanelVisibility)); Raise(nameof(SettingsPanelVisibility));
            if (value == AppTab.Journal) _ = RefreshJournalAsync();
        }
    }
    public Visibility ChatPanelVisibility => Visible(Tab == AppTab.Chat);
    public Visibility DiagnosticsPanelVisibility => Visible(Tab == AppTab.Diagnostics);
    public Visibility JournalPanelVisibility => Visible(Tab == AppTab.Journal);
    public Visibility SettingsPanelVisibility => Visible(Tab == AppTab.Settings);
    public Visibility WelcomeVisibility => Visible(ActiveChat.Messages.Count == 0);
    public Visibility EmptySearchVisibility => Visible(FilteredChats.Count == 0);
    private static Visibility Visible(bool condition) => condition ? Visibility.Visible : Visibility.Collapsed;
    public event EventHandler<string>? ConfirmationRequested;

    public Task StartAsync() => ApplySettingsAsync();

    public async Task ApplySettingsAsync()
    {
        if (IsBusy || _closing) return;
        if (!Uri.TryCreate(Settings.ModelEndpoint, UriKind.Absolute, out var uri) ||
            (uri.Scheme != "http" && uri.Scheme != "https"))
        {
            Status = "Укажите корректный HTTP(S)-адрес модели.";
            return;
        }
        IsBusy = true;
        _ready = false;
        Status = "Подключаю ядро и воркер…";
        try
        {
            // Snapshot configuration; editable controls are disabled until completion.
            _operation = Task.Run(() =>
            {
                _worker?.Dispose();
                _runtime.Dispose();
                _worker = new WorkerProcess(new WorkerOptions
                {
                    PythonPath = Settings.PythonPath, RepoRoot = Settings.RepoRoot,
                    SocketPath = Settings.SocketPath, StateDir = Settings.StateDir,
                    LlmUrl = Settings.ModelEndpoint, LlmModel = Settings.VisionModel,
                });
                _worker.Start();
                _runtime.Start(Settings);
            });
            await _operation;
            _ready = true;
            Status = "Готов";
            Save();
        }
        catch (Exception ex) { Status = "Ошибка запуска: " + ex.Message; }
        finally { IsBusy = false; }
        if (!_closing) await RefreshModelAsync();
    }

    public async Task RefreshModelAsync()
    {
        if (_checkingModel || _closing) return;
        _checkingModel = true;
        ModelTitle = "Проверяю соединение…";
        try
        {
            var result = await ModelProbe.CheckAsync(Settings.ModelEndpoint);
            _modelOnline = result.Online;
            ModelTitle = result.Title;
            ModelDetail = result.Detail;
            Raise(nameof(ModelDotBrush));
        }
        finally { _checkingModel = false; }
    }

    public async Task RefreshDiagnosticsAsync()
    {
        if (IsBusy || _closing) return;
        IsBusy = true;
        try
        {
            var task = Task.Run(() => (_runtime.MetricsJson(), _runtime.ToolsJson()));
            _operation = task;
            var (metrics, tools) = await task;
            Metrics = Pretty(metrics); Tools = Pretty(tools);
        }
        catch (Exception ex) { Metrics = "Не удалось прочитать диагностику: " + ex.Message; }
        finally { IsBusy = false; }
    }

    public async Task RefreshJournalAsync()
    {
        var stateDir = Settings.StateDir;
        Journal = await Task.Run(() => LogTail.Read(stateDir, 100));
    }

    public void NewChat()
    {
        var chat = new Chat();
        Chats.Insert(0, chat); SearchText = "";
        ActiveChat = chat; RebuildFilter(); Tab = AppTab.Chat;
        Save();
    }

    public void DeleteActiveChat()
    {
        if (ReferenceEquals(_runningChat, ActiveChat))
        {
            Status = "Сначала остановите текущую задачу.";
            return;
        }
        Chats.Remove(ActiveChat);
        if (Chats.Count == 0) Chats.Add(new Chat());
        ActiveChat = Chats[0]; RebuildFilter(); Save();
    }

    public async Task SendAsync()
    {
        if (!CanSend) return;
        var text = Input.Trim();
        var chat = ActiveChat;
        var planOnly = Settings.PlanOnly || Settings.SafetyMode == "plan_only";
        Input = "";
        if (chat.Messages.Count == 0) chat.Title = text.Length > 48 ? text[..47] + "…" : text;
        Add(chat, text, MessageRole.User);
        _runningChat = chat;
        _executing = true;
        IsBusy = true;
        Status = planOnly ? "Готовлю план…" : "Выполняю…";
        try
        {
            // RunTask already contains the deterministic fast path. Do not Execute
            // first and then RunTask: doing so can repeat a partially executed task.
            var task = Task.Run(() => planOnly
                ? (true, _runtime.Preview(text))
                : Describe(_runtime.RunTask(text)));
            _operation = task;
            var (ok, response) = await task;
            Drain();
            Add(chat, response, ok ? MessageRole.Agent : MessageRole.Error);
            Status = ok ? "Готов" : "Задача не выполнена";
        }
        catch (Exception ex)
        {
            Add(chat, ex.Message, MessageRole.Error);
            Status = "Ошибка выполнения";
        }
        finally
        {
            Drain(); _runningChat = null; _executing = false; IsBusy = false; Save();
        }
    }

    public void Stop()
    {
        if (!CanStop) return;
        _runtime.Cancel();
        Status = "Останавливаю…";
    }
    public void AnswerConfirmation(bool approved) => _runtime.AnswerConfirmation(approved && !_closing);

    public async Task ShutdownAsync()
    {
        if (_closing) return;
        _closing = true;
        _uiPump.Dispose();
        _runtime.Cancel();
        Save();
        try { await _operation; } catch (Exception) { }
        await Task.Run(() => { _worker?.Dispose(); _runtime.Dispose(); });
    }

    public void Save()
    {
        try
        {
            WorkspaceStore.Save(new WorkspaceData
            {
                Settings = Settings, GlassOpacity = GlassOpacity, Chats = Chats.ToList(),
            });
        }
        catch (Exception ex) { Status = "Не удалось сохранить: " + ex.Message; }
    }
    public void ReportError(string message) => Status = message;

    private static (bool, string) Describe(NativeOutcome outcome) => (outcome.Ok,
        !string.IsNullOrWhiteSpace(outcome.Message) ? outcome.Message :
        !string.IsNullOrWhiteSpace(outcome.Error) ? outcome.Error :
        outcome.Ok ? "Задача выполнена." : "Ядро не вернуло результат. Откройте диагностику.");

    private static string Pretty(string json)
    {
        try
        {
            using var doc = JsonDocument.Parse(json);
            return JsonSerializer.Serialize(doc.RootElement, new JsonSerializerOptions { WriteIndented = true });
        }
        catch (JsonException) { return json; }
    }
    private void RebuildFilter()
    {
        FilteredChats.Clear();
        foreach (var chat in Chats.Where(c => c.Title.Contains(SearchText.Trim(), StringComparison.OrdinalIgnoreCase)))
            FilteredChats.Add(chat);
        Raise(nameof(EmptySearchVisibility));
    }
    private void Drain()
    {
        while (_pending.TryDequeue(out var ev))
        {
            if (_closing) continue;
            if (ev.Kind == "confirm" && ev.Status == "pending")
            {
                if (ConfirmationRequested is null) AnswerConfirmation(false);
                else ConfirmationRequested.Invoke(this, ev.Message);
            }
            else if (ev.Kind != "thought" && !string.IsNullOrWhiteSpace(ev.Message))
            {
                Status = ev.Message;
                // Actual execution events, not fabricated progress cards.
                if (_runningChat is not null && ev.Kind == "observation")
                    Add(_runningChat, ev.Message, ev.Status == "failed" ? MessageRole.Error : MessageRole.Status);
            }
        }
    }
    private void Add(Chat chat, string text, MessageRole role)
    {
        if (string.IsNullOrWhiteSpace(text)) return;
        chat.Messages.Add(new ChatMessage { Role = role, Text = text });
        chat.Touch(); RaiseChatState(); RebuildFilter();
    }
    private void RaiseChatState() { Raise(nameof(WelcomeVisibility)); }
}
