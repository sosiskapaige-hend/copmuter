// Отпечаток сборки: workflow кладёт рядом с exe файл build-info.json.
//
// Зачем: ZIP со стороны выглядит одинаково у всех прогонов (в поставку входят одни
// и те же бинарники), поэтому по одному весу архива невозможно понять, какой коммит
// внутри. Теперь SHA виден прямо в строке состояния окна.
using System;
using System.IO;
using System.Text.Json;

namespace Copmuter.Agent.Services;

public static class BuildInfo
{
    public static string Sha { get; private set; } = "local";

    public static string Ref { get; private set; } = "";

    public static string Run { get; private set; } = "";

    /// <summary>Короткая подпись для строки состояния.</summary>
    public static string Display { get; private set; } = "сборка разработчика";

    static BuildInfo() => Load();

    private static void Load()
    {
        try
        {
            var path = Path.Combine(AppContext.BaseDirectory, "build-info.json");
            if (!File.Exists(path))
            {
                return;
            }

            using var doc = JsonDocument.Parse(File.ReadAllText(path));
            var root = doc.RootElement;
            Sha = Read(root, "sha", Sha);
            Ref = Read(root, "ref", Ref);
            Run = Read(root, "run", Run);
            var built = Read(root, "built_utc", "");

            var shortSha = Sha.Length > 7 ? Sha.Substring(0, 7) : Sha;
            var stamp = built.Length >= 16 ? built.Substring(5, 11).Replace('T', ' ') : "";
            Display = stamp.Length > 0 ? $"{shortSha} · {stamp}" : shortSha;
        }
        catch (Exception)
        {
            // Без отпечатка приложение работает как обычно: это диагностика, не функция.
        }
    }

    private static string Read(JsonElement root, string name, string fallback) =>
        root.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.String
            ? value.GetString() ?? fallback
            : fallback;
}
