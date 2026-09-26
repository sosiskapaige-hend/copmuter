// P/Invoke к AgentRuntime.dll — нативное ядро агента (руки).
//
// C# не делает низкоуровневых вызовов (Win32/SendInput/реестр) — всё это внутри
// C++ рантайма. Оболочка только: инициализация, команды, события, метрики.
using Copmuter.Agent.Models;
using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace Copmuter.Agent.Services;

public sealed class NativeOutcome
{
    public bool Handled { get; init; }
    public bool Ok { get; init; }
    public string Route { get; init; } = "";
    public string Action { get; init; } = "";
    public string Message { get; init; } = "";
    public string Error { get; init; } = "";
    public double Ms { get; init; }
    public double RouteUs { get; init; }
    public int Actions { get; init; }
    public bool NeedsLlm { get; init; }
    public string LlmPrompt { get; init; } = "";
    public JsonNode? Data { get; init; }

    public static NativeOutcome Parse(string json)
    {
        try
        {
            using var doc = JsonDocument.Parse(json);
            var root = doc.RootElement;
            return new NativeOutcome
            {
                Handled = root.TryGetProperty("handled", out var h) && h.GetBoolean(),
                Ok = root.TryGetProperty("ok", out var o) && o.GetBoolean(),
                Route = root.TryGetProperty("route", out var r) ? r.GetString() ?? "" : "",
                Action = root.TryGetProperty("action", out var a) ? a.GetString() ?? "" : "",
                Message = root.TryGetProperty("message", out var m) ? m.GetString() ?? "" : "",
                Error = root.TryGetProperty("error", out var e) ? e.GetString() ?? "" : "",
                Ms = root.TryGetProperty("ms", out var ms) ? ms.GetDouble() : 0,
                RouteUs = root.TryGetProperty("route_us", out var ru) ? ru.GetDouble() : 0,
                Actions = root.TryGetProperty("actions", out var ac) ? ac.GetInt32() : 0,
                NeedsLlm = root.TryGetProperty("needs_llm", out var nl) && nl.GetBoolean(),
                LlmPrompt = root.TryGetProperty("llm_prompt", out var lp) ? lp.GetString() ?? "" : "",
                Data = root.TryGetProperty("data", out var d) ? JsonNode.Parse(d.GetRawText()) : null,
            };
        }
        catch (JsonException)
        {
            return new NativeOutcome { Error = "ответ рантайма не разобран", Message = json };
        }
    }
}

public sealed class ToolObservation
{
    public bool Ok { get; init; }
    public string Tool { get; init; } = "";
    public string Output { get; init; } = "";
    public string Error { get; init; } = "";
    public double Ms { get; init; }
}

/// <summary>Событие рантайма для UI (то, что видит пользователь).</summary>
public sealed class RuntimeEvent
{
    public string Kind { get; init; } = "";
    public string Tool { get; init; } = "";
    public string Status { get; init; } = "";
    public string Message { get; init; } = "";
    public double Ms { get; init; }
    public long TsMs { get; init; }

    public static RuntimeEvent Parse(string json)
    {
        using var doc = JsonDocument.Parse(json);
        var root = doc.RootElement;
        return new RuntimeEvent
        {
            Kind = root.TryGetProperty("kind", out var k) ? k.GetString() ?? "" : "",
            Tool = root.TryGetProperty("tool", out var t) ? t.GetString() ?? "" : "",
            Status = root.TryGetProperty("status", out var s) ? s.GetString() ?? "" : "",
            Message = root.TryGetProperty("message", out var m) ? m.GetString() ?? "" : "",
            Ms = root.TryGetProperty("ms", out var ms) ? ms.GetDouble() : 0,
            TsMs = root.TryGetProperty("ts_ms", out var ts) ? ts.GetInt64() : 0,
        };
    }
}

[UnmanagedFunctionPointer(CallingConvention.Cdecl)]
public delegate void AgentEventCallback(IntPtr jsonPtr, IntPtr userData);

/// <summary>Обёртка над AgentRuntime.dll: единственная точка входа в нативное ядро.</summary>
public sealed class NativeRuntime : IDisposable
{
    private const string Dll = "AgentRuntime.dll";

    [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
    private static extern int agent_init([MarshalAs(UnmanagedType.LPUTF8Str)] string configJson);

    [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
    private static extern void agent_shutdown();

    [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
    private static extern IntPtr agent_execute([MarshalAs(UnmanagedType.LPUTF8Str)] string phrase);

    [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
    private static extern IntPtr agent_preview([MarshalAs(UnmanagedType.LPUTF8Str)] string phrase);

    [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
    private static extern IntPtr agent_run_tool([MarshalAs(UnmanagedType.LPUTF8Str)] string tool, [MarshalAs(UnmanagedType.LPUTF8Str)] string argsJson);

    [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
    private static extern IntPtr agent_metrics_json();

    [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
    private static extern IntPtr agent_state_json();

    [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
    private static extern IntPtr agent_tools_json();

    [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
    private static extern void agent_set_event_sink(AgentEventCallback callback, IntPtr userData);

    [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
    private static extern void agent_cancel_current();

    // Ответ на «подтвердите опасное действие»: 1 — да, 0 — нет.
    [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
    private static extern void agent_answer_confirmation(int approved);

    [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
    private static extern void agent_free(IntPtr ptr);

    [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
    private static extern IntPtr agent_run_task([MarshalAs(UnmanagedType.LPUTF8Str)] string task, int maxSteps);

    private AgentEventCallback? _callback;      // держим ссылку: GC не должен собрать делегат
    private bool _initialized;

    public event EventHandler<RuntimeEvent>? EventReceived;

    public bool Initialized => _initialized;

    public void Start(AgentSettings settings)
    {
        if (_initialized)
        {
            return;
        }
        _callback = OnNativeEvent;
        var config = new Dictionary<string, object?>
        {
            ["state_dir"] = settings.StateDir,
            ["safety_mode"] = settings.SafetyMode,
            ["default_browser"] = settings.DefaultBrowser,
            ["language"] = settings.Language,
            ["preload"] = settings.Preload,
            ["fast_path"] = settings.FastRouteDirect,
            ["ai_socket"] = settings.SocketPath,
            ["launch_timeout_ms"] = settings.LaunchTimeoutMs,
            ["vision_timeout_ms"] = settings.VisionTimeoutMs,
            ["max_retries"] = settings.MaxRetries,
            // Рантайм ждёт ответа диалога столько миллисекунд; 0 — не ждать вовсе.
            ["confirm_timeout_ms"] = settings.ConfirmTimeoutMs,
            ["debug_log"] = settings.DebugLog,
        };
        var code = agent_init(JsonSerializer.Serialize(config));
        if (code != 0)
        {
            throw new InvalidOperationException($"agent_init вернул код {code}: {LastError()}");
        }
        agent_set_event_sink(_callback, IntPtr.Zero);
        _initialized = true;
    }

    private void OnNativeEvent(IntPtr jsonPtr, IntPtr userData)
    {
        if (jsonPtr == IntPtr.Zero)
        {
            return;
        }
        var json = Marshal.PtrToStringUTF8(jsonPtr) ?? "";
        try
        {
            EventReceived?.Invoke(this, RuntimeEvent.Parse(json));
        }
        catch (JsonException)
        {
            // повреждённое событие не должно ронять UI-поток
        }
    }

    public NativeOutcome Execute(string phrase)
    {
        EnsureStarted();
        var ptr = agent_execute(phrase);
        try
        {
            return NativeOutcome.Parse(ptr == IntPtr.Zero ? "{}" : Marshal.PtrToStringUTF8(ptr) ?? "{}");
        }
        finally
        {
            if (ptr != IntPtr.Zero)
            {
                agent_free(ptr);
            }
        }
    }

    public NativeOutcome RunTask(string task)
    {
        EnsureStarted();
        // Zero selects the native runtime's configured step limit.
        return NativeOutcome.Parse(ReadAndFree(agent_run_task(task, 0)));
    }

    public string Preview(string phrase)
    {
        EnsureStarted();
        var ptr = agent_preview(phrase);
        try
        {
            return ptr == IntPtr.Zero ? "" : Marshal.PtrToStringUTF8(ptr) ?? "";
        }
        finally
        {
            if (ptr != IntPtr.Zero)
            {
                agent_free(ptr);
            }
        }
    }

    /// <summary>Исполнить один инструмент (вызов из агентного цикла).</summary>
    public ToolObservation RunTool(string tool, string argsJson)
    {
        EnsureStarted();
        var ptr = agent_run_tool(tool, argsJson);
        try
        {
            var json = ptr == IntPtr.Zero ? "{}" : Marshal.PtrToStringUTF8(ptr) ?? "{}";
            using var doc = JsonDocument.Parse(json);
            var root = doc.RootElement;
            return new ToolObservation
            {
                Ok = root.TryGetProperty("ok", out var ok) && ok.GetBoolean(),
                Tool = root.TryGetProperty("tool", out var t) ? t.GetString() ?? tool : tool,
                Output = root.TryGetProperty("output", out var o) ? o.GetString() ?? "" : "",
                Error = root.TryGetProperty("error", out var e) ? e.GetString() ?? "" : "",
                Ms = root.TryGetProperty("ms", out var ms) ? ms.GetDouble() : 0,
            };
        }
        catch (JsonException ex)
        {
            return new ToolObservation { Tool = tool, Error = ex.Message };
        }
        finally
        {
            if (ptr != IntPtr.Zero)
            {
                agent_free(ptr);
            }
        }
    }

    public string MetricsJson() => ReadAndFree(agent_metrics_json());

    public string StateJson() => ReadAndFree(agent_state_json());

    /// <summary>Список инструментов ядра: что доступно в этой сборке.</summary>
    public string ToolsJson() => ReadAndFree(agent_tools_json());

    public void Cancel() => agent_cancel_current();

    /// <summary>Пользователь ответил на диалог подтверждения опасного действия.</summary>
    public void AnswerConfirmation(bool approved)
    {
        if (_initialized)
        {
            agent_answer_confirmation(approved ? 1 : 0);
        }
    }

    private static string ReadAndFree(IntPtr ptr)
    {
        try
        {
            return ptr == IntPtr.Zero ? "{}" : Marshal.PtrToStringUTF8(ptr) ?? "{}";
        }
        finally
        {
            if (ptr != IntPtr.Zero)
            {
                agent_free(ptr);
            }
        }
    }

    private void EnsureStarted()
    {
        if (!_initialized)
        {
            throw new InvalidOperationException("рантайм не инициализирован: вызовите Start()");
        }
    }

    [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
    private static extern IntPtr agent_last_error();

    private static string LastError()
    {
        var ptr = agent_last_error();
        return ptr == IntPtr.Zero ? "" : Marshal.PtrToStringUTF8(ptr) ?? "";
    }

    public void Dispose()
    {
        if (_initialized)
        {
            agent_set_event_sink(null!, IntPtr.Zero);
            agent_shutdown();
            _initialized = false;
        }
        GC.SuppressFinalize(this);
    }
}
