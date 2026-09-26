// Окно: своя шапка, настоящее стекло, отпечаток сборки и диалог подтверждения.
//
// Низкоуровневых вызовов Win32 здесь нет и быть не должно: за систему отвечает
// AgentRuntime.dll, за рассуждения — Python-воркер. Стекло оформляется штатным
// SystemBackdrop из Windows App SDK, то есть средствами платформы, а не самоделкой.
using System;
using System.IO;
using Copmuter.Agent.Models;
using Copmuter.Agent.Services;
using Copmuter.Agent.ViewModels;
using Microsoft.UI.Composition.SystemBackdrops;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Media.Imaging;
using Windows.System;

namespace Copmuter.Agent;

public sealed partial class MainWindow : Window
{
    public MainWindow()
    {
        // ViewModel готовим до InitializeComponent: x:Bind читает его при загрузке XAML.
        ViewModel = new ChatViewModel(new AgentSettings());
        InitializeComponent();
        ConfigureGlassWindow();
        LoadBrandAssets();
        Closed += (_, _) => ViewModel.Shutdown();
        ViewModel.ConfirmationRequested += OnConfirmationRequestedAsync;
        ViewModel.Start();
    }

    public ChatViewModel ViewModel { get; }

    /// <summary>
    /// Стеклянное окно: своя шапка вместо системной и настоящий SystemBackdrop ОС.
    /// Если окружение стекло не поддерживает (сервер/старая сборка), окно остаётся
    /// тёмным — приложение обязано запуститься в любом случае.
    /// </summary>
    private void ConfigureGlassWindow()
    {
        try
        {
            ExtendsContentIntoTitleBar = true;
            SetTitleBar(TitleBarDragRegion);
            StyleCaptionButtons();
            Root.Loaded += (_, _) => UpdateCaptionInset();
            AppWindow.Changed += (_, _) => UpdateCaptionInset();
        }
        catch (Exception)
        {
            // Системная шапка тоже рабочий вариант.
        }

        try
        {
            // Mica — сдержанная материя Windows 11: обои угадываются под тёмной
            // подложкой, но не спорят с содержимым. Акрил — более «стеклянный»
            // вариант, доступный с Windows 10 1809.
            if (MicaController.IsSupported())
            {
                SystemBackdrop = new MicaBackdrop();
                return;
            }

            if (DesktopAcrylicController.IsSupported())
            {
                SystemBackdrop = new DesktopAcrylicBackdrop();
                return;
            }
        }
        catch (Exception)
        {
            // Ниже — сплошной фон.
        }

        Root.Background = new SolidColorBrush(Windows.UI.Color.FromArgb(255, 0x0B, 0x1A, 0x20));
    }

    /// <summary>Кнопки окна в тон шапке: без системной заливки, светлые глифы.</summary>
    private void StyleCaptionButtons()
    {
        var bar = AppWindow.TitleBar;
        bar.ButtonBackgroundColor = Microsoft.UI.Colors.Transparent;
        bar.ButtonInactiveBackgroundColor = Microsoft.UI.Colors.Transparent;
        bar.ButtonForegroundColor = Windows.UI.Color.FromArgb(255, 0xF2, 0xFA, 0xF8);
        bar.ButtonInactiveForegroundColor = Windows.UI.Color.FromArgb(0x5C, 0xE2, 0xF4, 0xF1);
        bar.ButtonHoverForegroundColor = Microsoft.UI.Colors.White;
        bar.ButtonHoverBackgroundColor = Windows.UI.Color.FromArgb(0x24, 0xFF, 0xFF, 0xFF);
        bar.ButtonPressedForegroundColor = Microsoft.UI.Colors.White;
        bar.ButtonPressedBackgroundColor = Windows.UI.Color.FromArgb(0x38, 0xFF, 0xFF, 0xFF);
    }

    /// <summary>Держим место под системные кнопки окна, иначе они лягут на переключатели.</summary>
    private void UpdateCaptionInset()
    {
        try
        {
            var scale = Root.XamlRoot?.RasterizationScale ?? 1.0;
            var inset = AppWindow.TitleBar.RightInset / scale;
            CaptionSpacer.Width = inset > 0 ? inset : 140;
        }
        catch (Exception)
        {
            CaptionSpacer.Width = 140;
        }
    }

    /// <summary>Иконка приложения и отпечаток сборки — из файлов рядом с exe.</summary>
    private void LoadBrandAssets()
    {
        BuildStamp.Text = BuildInfo.Display;
        try
        {
            var png = Path.Combine(AppContext.BaseDirectory, "assets", "icon.png");
            if (File.Exists(png))
            {
                BrandLogo.Source = new BitmapImage(new Uri(png));
            }

            var ico = Path.Combine(AppContext.BaseDirectory, "assets", "icon.ico");
            if (File.Exists(ico))
            {
                AppWindow.SetIcon(ico);
            }
        }
        catch (Exception)
        {
            // Логотип не критичен: остаётся бирюзовый бейдж.
        }
    }

    private async void OnSendClick(object sender, RoutedEventArgs e) => await ViewModel.SendAsync();

    /// <summary>Готовая подсказка подставляет текст в поле ввода — и всё.</summary>
    private void OnSuggestionClick(object sender, RoutedEventArgs e)
    {
        if (sender is Button { Tag: string text })
        {
            ViewModel.Input = text;
            InputBox.Focus(FocusState.Programmatic);
        }
    }

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
