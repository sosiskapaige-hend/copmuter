using System;
using System.Collections.Generic;
using System.IO;
using System.Text.Json;
using Copmuter.Agent.Models;

namespace Copmuter.Agent.Services;

// UI state stays separate from the native runtime's database. No secrets are stored.
public sealed class WorkspaceData
{
    public AgentSettings Settings { get; set; } = new();
    public double GlassOpacity { get; set; } = 0.86;
    public List<Chat> Chats { get; set; } = new();
}

public static class WorkspaceStore
{
    private static string FilePath => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Copmuter", "workspace.json");

    public static WorkspaceData Load(out string error)
    {
        error = "";
        try
        {
            return File.Exists(FilePath)
                ? JsonSerializer.Deserialize<WorkspaceData>(File.ReadAllText(FilePath)) ?? new()
                : new();
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or JsonException)
        {
            error = "Не удалось прочитать сохранённые чаты: " + ex.Message;
            return new();
        }
    }

    public static void Save(WorkspaceData data)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(FilePath)!);
        var temporary = FilePath + ".tmp";
        File.WriteAllText(temporary, JsonSerializer.Serialize(data));
        File.Move(temporary, FilePath, overwrite: true);
    }
}
