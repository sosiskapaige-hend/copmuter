// Чтение хвоста журнала ядра: state_dir\agent_debug.log.
//
// Журнал пишет нативное ядро (agent_host --debug / настройка DebugLog). Оболочка
// только показывает последние строки — это честный просмотр, а не своя логика.
using System;
using System.Collections.Generic;
using System.IO;

namespace Copmuter.Agent.Services;

public static class LogTail
{
    public const string FileName = "agent_debug.log";

    public static string Path(string stateDir) =>
        string.IsNullOrWhiteSpace(stateDir) ? "" : System.IO.Path.Combine(stateDir, FileName);

    /// <summary>Последние <paramref name="lines"/> строк файла (или пояснение, почему пусто).</summary>
    public static string Read(string stateDir, int lines = 40)
    {
        var path = Path(stateDir);
        if (path.Length == 0 || !File.Exists(path))
        {
            return $"Журнал ещё не создан.\nВключите «Журнал отладки» в параметрах — ядро начнёт писать сюда:\n{path}";
        }

        try
        {
            var tail = new Queue<string>(lines);
            using var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
            var truncated = stream.Length > 256 * 1024;
            if (truncated) stream.Seek(-256 * 1024, SeekOrigin.End);
            using var reader = new StreamReader(stream);
            if (truncated) reader.ReadLine(); // Skip a possibly partial UTF-8 line.
            string? line;
            while ((line = reader.ReadLine()) is not null)
            {
                if (tail.Count == lines)
                {
                    tail.Dequeue();
                }
                tail.Enqueue(line);
            }
            return string.Join(Environment.NewLine, tail);
        }
        catch (Exception ex)
        {
            return $"Не удалось прочитать журнал: {ex.Message}";
        }
    }
}
