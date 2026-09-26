using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using Copmuter.Agent.Models;
using Copmuter.Agent.Services;

static void Check(bool condition, string name)
{
    if (!condition) throw new Exception(name);
    Console.WriteLine("PASS " + name);
}

var chat = new Chat { Title = "Задача с кириллицей" };
var notifications = new List<string>();
chat.PropertyChanged += (_, e) => notifications.Add(e.PropertyName!);
chat.Messages.Add(new ChatMessage { Role = MessageRole.User, Text = "Открой Telegram", Time = "12:34:56" });
chat.Touch();
Check(notifications.Contains("Subtitle") && notifications.Contains("Time"), "history metadata notifies bindings");
Check(chat.Time == "12:34", "history timestamp");
var workspace = new WorkspaceData { Chats = new() { chat }, GlassOpacity = 0.86 };
workspace.Settings.ModelEndpoint = "http://127.0.0.1:4321/v1";
var json = JsonSerializer.Serialize(workspace);
var restored = JsonSerializer.Deserialize<WorkspaceData>(json)!;
Check(restored.Chats.Count == 1 && restored.Chats[0].Messages.Count == 1, "messages survive save/load");
Check(restored.Chats[0].Messages[0].Text == "Открой Telegram", "UTF-8 content survives save/load");
Check(restored.Settings.ModelEndpoint == workspace.Settings.ModelEndpoint && restored.GlassOpacity == 0.86,
    "connection and glass settings survive save/load");
Check(!json.Contains("PythonPath") && !json.Contains("RepoRoot"), "relocatable executable paths are not persisted");
var notified = false;
chat.Messages[0].PropertyChanged += (_, e) => notified = e.PropertyName == "Text";
chat.Messages[0].Text = "Изменено";
Check(notified, "message updates notify bindings");

var directory = Path.Combine(Path.GetTempPath(), "copmuter-shell-tests-" + Guid.NewGuid().ToString("N"));
Directory.CreateDirectory(directory);
try
{
    Check(LogTail.Read(directory).Contains("ещё не создан"), "missing journal is explained");
    File.WriteAllLines(LogTail.Path(directory), Enumerable.Range(0, 500).Select(i => "строка " + i));
    var lines = LogTail.Read(directory, 100).Split(Environment.NewLine);
    Check(lines.Length == 100 && lines[0] == "строка 400" && lines[^1] == "строка 499", "journal returns bounded tail");
    using (var writer = new FileStream(LogTail.Path(directory), FileMode.Open, FileAccess.Write, FileShare.ReadWrite))
        Check(LogTail.Read(directory, 2).Contains("строка 499"), "journal can be read while native writer is open");
}
finally { Directory.Delete(directory, true); }

// A local HTTP server proves the status is derived from responses, not a green placeholder.
foreach (var (body, online) in new[]
{
    ("{\"data\":[{\"id\":\"local-model\"}]}", true),
    ("{\"data\":[]}", false),
    ("{\"wrong\":true}", false),
    ("not-json", false),
})
{
    var listener = new TcpListener(IPAddress.Loopback, 0);
    listener.Start();
    var port = ((IPEndPoint)listener.LocalEndpoint).Port;
    var server = Task.Run(async () =>
    {
        using var client = await listener.AcceptTcpClientAsync();
        using var stream = client.GetStream();
        var buffer = new byte[4096];
        await stream.ReadAsync(buffer);
        var bytes = Encoding.UTF8.GetBytes(body);
        var headers = Encoding.ASCII.GetBytes($"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {bytes.Length}\r\nConnection: close\r\n\r\n");
        await stream.WriteAsync(headers);
        await stream.WriteAsync(bytes);
    });
    try
    {
        var status = await ModelProbe.CheckAsync($"http://127.0.0.1:{port}/v1");
        await server.WaitAsync(TimeSpan.FromSeconds(5));
        Check(status.Online == online, "model status: " + body);
    }
    finally { listener.Stop(); }
}
Console.WriteLine("Shell model/service tests passed.");
