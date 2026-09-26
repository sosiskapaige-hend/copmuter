// Конвертеры для привязок: подложка, обводка, скругление и выравнивание зависят
// от роли сообщения. Цвета — токены дизайна из App.xaml: бирюза для пользователя,
// нейтральное стекло для агента, красная подсветка для ошибок.
using System;
using Microsoft.UI;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Data;
using Microsoft.UI.Xaml.Media;
using Copmuter.Agent.Models;

namespace Copmuter.Agent.Converters;

public sealed class MessageBrushConverter : IValueConverter
{
    private static readonly SolidColorBrush User = new(Windows.UI.Color.FromArgb(0x33, 0x35, 0xDF, 0xCC));
    private static readonly SolidColorBrush Agent = new(Windows.UI.Color.FromArgb(0xD9, 0x0D, 0x1E, 0x25));
    private static readonly SolidColorBrush Status = new(Windows.UI.Color.FromArgb(0x0A, 0xFF, 0xFF, 0xFF));
    private static readonly SolidColorBrush Error = new(Windows.UI.Color.FromArgb(0x1F, 0xFF, 0x7A, 0x8A));

    public object Convert(object value, Type targetType, object parameter, string language) =>
        value switch
        {
            MessageRole.User => User,
            MessageRole.Status => Status,
            MessageRole.Error => Error,
            _ => Agent,
        };

    public object ConvertBack(object value, Type targetType, object parameter, string language) =>
        throw new NotSupportedException();
}

public sealed class MessageEdgeConverter : IValueConverter
{
    private static readonly SolidColorBrush User = new(Windows.UI.Color.FromArgb(0x5C, 0x35, 0xDF, 0xCC));
    private static readonly SolidColorBrush Agent = new(Windows.UI.Color.FromArgb(0x24, 0xFF, 0xFF, 0xFF));
    private static readonly SolidColorBrush Status = new(Windows.UI.Color.FromArgb(0x12, 0xFF, 0xFF, 0xFF));
    private static readonly SolidColorBrush Error = new(Windows.UI.Color.FromArgb(0x66, 0xFF, 0x7A, 0x8A));

    public object Convert(object value, Type targetType, object parameter, string language) =>
        value switch
        {
            MessageRole.User => User,
            MessageRole.Status => Status,
            MessageRole.Error => Error,
            _ => Agent,
        };

    public object ConvertBack(object value, Type targetType, object parameter, string language) =>
        throw new NotSupportedException();
}

/// <summary>Скругление «хвостом» к говорящему: у пользователя — верхний правый угол.</summary>
public sealed class MessageCornerConverter : IValueConverter
{
    private static readonly CornerRadius User = new(16, 6, 16, 16);
    private static readonly CornerRadius Agent = new(6, 16, 16, 16);
    private static readonly CornerRadius Neutral = new(12, 12, 12, 12);

    public object Convert(object value, Type targetType, object parameter, string language) =>
        value switch
        {
            MessageRole.User => User,
            MessageRole.Status => Neutral,
            MessageRole.Error => Agent,
            _ => Agent,
        };

    public object ConvertBack(object value, Type targetType, object parameter, string language) =>
        throw new NotSupportedException();
}

public sealed class MessageAlignConverter : IValueConverter
{
    public object Convert(object value, Type targetType, object parameter, string language) =>
        value is MessageRole.User ? HorizontalAlignment.Right : HorizontalAlignment.Left;

    public object ConvertBack(object value, Type targetType, object parameter, string language) =>
        throw new NotSupportedException();
}

public sealed class InvertBoolConverter : IValueConverter
{
    public object Convert(object value, Type targetType, object parameter, string language) =>
        value is not true;

    public object ConvertBack(object value, Type targetType, object parameter, string language) =>
        value is not true;
}

public sealed class BoolToVisibilityConverter : IValueConverter
{
    public object Convert(object value, Type targetType, object parameter, string language) =>
        value is true ? Visibility.Visible : Visibility.Collapsed;

    public object ConvertBack(object value, Type targetType, object parameter, string language) =>
        value is Visibility.Visible;
}
