// Чат: список сообщений плюс подпись для сайдбара.
using System.Collections.ObjectModel;
using System.ComponentModel;

namespace Copmuter.Agent.Models;

public sealed class Chat : INotifyPropertyChanged
{
    private string _title = "Новый чат";

    public ObservableCollection<ChatMessage> Messages { get; set; } = new();

    public string Title
    {
        get => _title;
        set
        {
            if (_title != value)
            {
                _title = value;
                PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(Title)));
            }
        }
    }

    /// <summary>Подпись строки: «агент · 3 шага», «пусто» и т.п.</summary>
    public string Subtitle =>
        Messages.Count == 0 ? "пусто" : $"сообщений: {Messages.Count}";

    public string Time => Messages.Count == 0 ? "" : Messages[^1].Time.Substring(0, 5);

    public void Touch()
    {
        PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(Subtitle)));
        PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(Time)));
    }

    public event PropertyChangedEventHandler? PropertyChanged;
}
