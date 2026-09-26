// Сообщение чата: роль определяет вид пузыря, время — подпись под ним.
using System;
using System.ComponentModel;
using System.Runtime.CompilerServices;

namespace Copmuter.Agent.Models;

public enum MessageRole
{
    User,
    Agent,
    Status,
    Error,
}

public sealed class ChatMessage : INotifyPropertyChanged
{
    private string _text = "";

    public MessageRole Role { get; init; }

    public string Text
    {
        get => _text;
        set
        {
            if (_text != value)
            {
                _text = value;
                PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(Text)));
            }
        }
    }

    /// <summary>Короткая служебная метка: каким путём получен ответ.</summary>
    public string Detail { get; set; } = "";

    public string Time { get; init; } = DateTime.Now.ToString("HH:mm:ss");

    public string Speaker => Role switch
    {
        MessageRole.User => "Вы", MessageRole.Error => "Не удалось выполнить",
        MessageRole.Status => "Ход выполнения", _ => "Copmuter",
    };

    public bool IsUser => Role == MessageRole.User;

    public event PropertyChangedEventHandler? PropertyChanged;
}
