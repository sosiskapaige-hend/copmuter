using Copmuter.Agent.Models;
using Copmuter.Agent.ViewModels;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;
using Windows.System;

namespace Copmuter.Agent;

public sealed partial class MainWindow : Window
{
    public MainWindow()
    {
        // ViewModel готовим до InitializeComponent: x:Bind читает его при загрузке XAML.
        ViewModel = new ChatViewModel(new AgentSettings());
        InitializeComponent();
        Closed += (_, _) => ViewModel.Shutdown();
        ViewModel.ConfirmationRequested += OnConfirmationRequestedAsync;
        ViewModel.Start();
    }

    public ChatViewModel ViewModel { get; }

    private async void OnSendClick(object sender, RoutedEventArgs e) => await ViewModel.SendAsync();

    // Диалог подтверждения: рантайм держит опасное действие на паузе, ждём ответ.
    private async void OnConfirmationRequestedAsync(object? sender, string question)
    {
        var dialog = new ContentDialog
        {
            Title = "Подтвердите действие",
            Content = question,
            PrimaryButtonText = "Выполнить",
            CloseButtonText = "Отмена",
            DefaultButton = ContentDialogButton.Close,   // Enter не должен удалять
            XamlRoot = Content.XamlRoot,
        };
        var result = await dialog.ShowAsync();
        ViewModel.AnswerConfirmation(result == ContentDialogResult.Primary);
    }

    private void OnStopClick(object sender, RoutedEventArgs e) => ViewModel.Stop();

    private async void OnInputKeyDown(object sender, KeyRoutedEventArgs e)
    {
        if (e.Key == VirtualKey.Enter && !e.KeyStatus.IsMenuKeyDown)
        {
            e.Handled = true;
            await ViewModel.SendAsync();
        }
    }

    private void OnSettingsToggled(object sender, RoutedEventArgs e)
    {
        // Панель настроек показывается привязкой; здесь ничего делать не нужно.
    }
}
