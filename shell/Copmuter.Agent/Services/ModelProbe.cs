// Состояние модели: спрашиваем у локального сервера (LM Studio/Ollama) список моделей.
//
// Нужно, чтобы индикатор в шапке говорил правду: «модель на связи» вместо
// декоративной зелёной точки. Никаких сторонних пакетов — обычный HttpClient.
using System;
using System.Net.Http;
using System.Net.Http.Json;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;

namespace Copmuter.Agent.Services;

public sealed record ModelStatus(bool Online, string Title, string Detail);

public static class ModelProbe
{
    private static readonly HttpClient Http = new() { Timeout = TimeSpan.FromSeconds(4) };

    /// <summary>Опрашивает /v1/models по адресу сервера из настроек.</summary>
    public static async Task<ModelStatus> CheckAsync(string endpoint, CancellationToken token = default)
    {
        var url = (endpoint ?? "").Trim();
        if (url.Length == 0)
        {
            return new ModelStatus(false, "модель не задана", "укажите адрес сервера в параметрах");
        }

        var root = url.TrimEnd('/');
        if (root.EndsWith("/v1", StringComparison.OrdinalIgnoreCase))
        {
            root = root[..^3];
        }

        try
        {
            using var doc = await Http.GetFromJsonAsync<JsonDocument>($"{root}/v1/models", token);
            if (doc is null || !doc.RootElement.TryGetProperty("data", out var data) ||
                data.ValueKind != JsonValueKind.Array || data.GetArrayLength() == 0)
                return new ModelStatus(false, "Нет загруженных моделей", "Загрузите модель в LM Studio.");
            var first = data[0].TryGetProperty("id", out var id) ? id.GetString() : null;
            return new ModelStatus(true, first ?? "API отвечает", $"Сервер: {root}");
        }
        catch (Exception ex)
        {
            return new ModelStatus(false, "модель не отвечает", Shorten(ex.Message));
        }
    }

    private static string Shorten(string message) =>
        message.Length <= 60 ? message : message[..57] + "…";
}
