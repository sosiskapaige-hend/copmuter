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
        var workspace = WorkspaceStore.Load(out var loadError);
        ViewModel = new ChatViewModel(workspace);
        InitializeComponent();
        Root.DataContext = ViewModel;
        ConfigureGlassWindow();
        LoadBrandAssets();
        AppWindow.Resize(new Windows.Graphics.SizeInt32(1280, 860));
        ViewModel.ConfirmationRequested += OnConfirmationRequestedAsync;
        Root.Loaded += async (_, _) =>
        {
            foreach (ComboBoxItem item in SafetyModeBox.Items)
                if ((string)item.Tag == ViewModel.Settings.SafetyMode) SafetyModeBox.SelectedItem = item;
            UpdateNavigation();
            ObserveMessages();
            await ViewModel.StartAsync();
            if (loadError.Length > 0) ViewModel.ReportError(loadError);
        };
        AppWindow.Closing += async (_, e) =>
        {
            if (_allowClose) return;
            e.Cancel = true;
            if (_closing) return;
            _closing = true;
            _confirmation?.Hide();
            Root.IsHitTestVisible = false;
            await ViewModel.ShutdownAsync();
            _allowClose = true;
            Close();
        };
        ViewModel.PropertyChanged += (_, e) =>
        {
            if (e.PropertyName == nameof(ViewModel.ActiveChat)) ObserveMessages();
            if (e.PropertyName == nameof(ViewModel.Tab)) UpdateNavigation();
        };
        var newChat = new KeyboardAccelerator { Key = VirtualKey.N, Modifiers = VirtualKeyModifiers.Control };
        newChat.Invoked += (_, e) => { ViewModel.NewChat(); InputBox.Focus(FocusState.Programmatic); e.Handled = true; };
        Root.KeyboardAccelerators.Add(newChat);
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
            // Desktop acrylic samples windows behind us; Mica samples wallpaper only.
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
            CaptionSpacer.Width = new GridLength(inset > 0 ? inset : 140);
        }
        catch (Exception)
        {
            CaptionSpacer.Width = new GridLength(140);
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

    private ContentDialog? _confirmation;
    private bool _allowClose, _closing;
    private System.Collections.ObjectModel.ObservableCollection<ChatMessage>? _observedMessages;

    private async void OnConfirmationRequestedAsync(object? sender, string question)
    {
        if (_closing || _confirmation is not null) { ViewModel.AnswerConfirmation(false); return; }
        var approved = false;
        try
        {
            _confirmation = new ContentDialog
            {
                Title = "Разрешить действие?", Content = question,
                PrimaryButtonText = "Разрешить", CloseButtonText = "Отмена",
                DefaultButton = ContentDialogButton.Close, XamlRoot = Root.XamlRoot,
                RequestedTheme = ElementTheme.Dark,
            };
            approved = await _confirmation.ShowAsync() == ContentDialogResult.Primary;
        }
        catch (Exception ex) { ViewModel.ReportError("Диалог не открылся: " + ex.Message); }
        finally { _confirmation = null; ViewModel.AnswerConfirmation(approved); }
    }

    private void OnStopClick(object sender, RoutedEventArgs e)
    {
        _confirmation?.Hide();
        ViewModel.Stop();
    }

    private async void OnInputKeyDown(object sender, KeyRoutedEventArgs e)
    {
        var shift = Microsoft.UI.Input.InputKeyboardSource.GetKeyStateForCurrentThread(VirtualKey.Shift);
        if (e.Key == VirtualKey.Enter && (shift & Windows.UI.Core.CoreVirtualKeyStates.Down) == 0)
        {
            e.Handled = true;
            await ViewModel.SendAsync();
        }
    }

    private void OnNewChatClick(object sender, RoutedEventArgs e)
    {
        ViewModel.NewChat(); InputBox.Focus(FocusState.Programmatic);
    }
    private void OnNavigate(object sender, RoutedEventArgs e)
    {
        if (sender is Button { Tag: string tab } && Enum.TryParse<AppTab>(tab, out var value)) ViewModel.Tab = value;
    }
    private void UpdateNavigation()
    {
        foreach (var button in new[] { ChatNav, JournalNav, DiagnosticsNav, SettingsNav })
            button.Background = (string)button.Tag == ViewModel.Tab.ToString()
                ? (Brush)Application.Current.Resources["AccentSoftBrush"] : new SolidColorBrush(Microsoft.UI.Colors.Transparent);
    }
    private void OnChatClicked(object sender, ItemClickEventArgs e)
    {
        if (e.ClickedItem is Chat chat) { ViewModel.ActiveChat = chat; ViewModel.Tab = AppTab.Chat; }
    }
    private async void OnDeleteChatClick(object sender, RoutedEventArgs e)
    {
        if (_confirmation is not null) return;
        var chat = ViewModel.ActiveChat;
        var dialog = new ContentDialog
        {
            Title = "Удалить задачу?", Content = "Переписка будет удалена с этого компьютера. Выполненные действия не откатываются.",
            PrimaryButtonText = "Удалить", CloseButtonText = "Отмена", DefaultButton = ContentDialogButton.Close,
            XamlRoot = Root.XamlRoot, RequestedTheme = ElementTheme.Dark,
        };
        try
        {
            // Do not compete with the runtime's safety dialog.
            if (ViewModel.IsBusy) { ViewModel.ReportError("Сначала дождитесь завершения задачи."); return; }
            _confirmation = dialog;
            if (await dialog.ShowAsync() == ContentDialogResult.Primary && ReferenceEquals(chat, ViewModel.ActiveChat))
                ViewModel.DeleteActiveChat();
        }
        finally { _confirmation = null; }
    }
    private async void OnCheckModelClick(object sender, RoutedEventArgs e) => await ViewModel.RefreshModelAsync();
    private async void OnApplySettingsClick(object sender, RoutedEventArgs e) => await ViewModel.ApplySettingsAsync();
    private async void OnRefreshDiagnosticsClick(object sender, RoutedEventArgs e) => await ViewModel.RefreshDiagnosticsAsync();
    private async void OnRefreshJournalClick(object sender, RoutedEventArgs e) => await ViewModel.RefreshJournalAsync();
    private async void OnOpenStateFolderClick(object sender, RoutedEventArgs e)
    {
        try
        {
            Directory.CreateDirectory(ViewModel.Settings.StateDir);
            var folder = await Windows.Storage.StorageFolder.GetFolderFromPathAsync(ViewModel.Settings.StateDir);
            if (!await Launcher.LaunchFolderAsync(folder)) ViewModel.ReportError("Windows не смогла открыть папку.");
        }
        catch (Exception ex) { ViewModel.ReportError("Не удалось открыть папку: " + ex.Message); }
    }
    private void OnSafetyModeChanged(object sender, SelectionChangedEventArgs e)
    {
        if (SafetyModeBox.SelectedItem is ComboBoxItem { Tag: string mode }) ViewModel.Settings.SafetyMode = mode;
    }
    private void OnRootSizeChanged(object sender, SizeChangedEventArgs e)
    {
        // The same UI collapses to icon navigation on smaller windows.
        if (SidebarColumn is null || HistoryPanel is null) return;
        var compact = e.NewSize.Width < 980;
        SidebarColumn.Width = new GridLength(compact ? 72 : 248);
        foreach (var label in new[] { NewChatLabel, ChatNavLabel, JournalNavLabel, DiagnosticsNavLabel, SettingsNavLabel })
            label.Visibility = compact ? Visibility.Collapsed : Visibility.Visible;
        HistoryPanel.Visibility = compact ? Visibility.Collapsed : Visibility.Visible;
    }
    private void ObserveMessages()
    {
        if (_observedMessages is not null) _observedMessages.CollectionChanged -= OnMessagesChanged;
        _observedMessages = ViewModel.ActiveChat.Messages;
        _observedMessages.CollectionChanged += OnMessagesChanged;
    }
    private void OnMessagesChanged(object? sender, System.Collections.Specialized.NotifyCollectionChangedEventArgs e)
    {
        if (MessageScroll.ScrollableHeight - MessageScroll.VerticalOffset > 100) return;
        DispatcherQueue.TryEnqueue(() =>
        {
            MessageScroll.UpdateLayout();
            MessageScroll.ChangeView(null, MessageScroll.ScrollableHeight, null, true);
        });
    }
}
