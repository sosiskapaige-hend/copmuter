// Конвертеры для привязок: цвет и выравнивание зависят от роли сообщения.
using System;
using Microsoft.UI;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Data;
using Microsoft.UI.Xaml.Media;
using Copmuter.Agent.Models;

namespace Copmuter.Agent.Converters;

public sealed class MessageBrushConverter : IValueConverter
{
    private static readonly SolidColorBrush User = new(Windows.UI.Color.FromArgb(255, 0x2B, 0x57, 0x97));
    private static readonly SolidColorBrush Agent = new(Windows.UI.Color.FromArgb(255, 0x25, 0x28, 0x2E));
    private static readonly SolidColorBrush Status = new(Windows.UI.Color.FromArgb(255, 0x1E, 0x2A, 0x22));
    private static readonly SolidColorBrush Error = new(Windows.UI.Color.FromArgb(255, 0x4A, 0x1F, 0x1F));

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
