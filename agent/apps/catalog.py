"""Каталог известных приложений, системных папок и страниц настроек Windows.

Это «стартовые знания» реестра приложений (ТЗ §8): для каждой программы —
русские/английские алиасы, типичные пути .exe, URI-протоколы, идентификаторы
Store-приложений, имена процессов для проверки запуска.

Автообнаружение (agent/apps/discovery.py) дополняет каталог тем, что реально
установлено на машине: реестр, Start Menu, PATH, App Paths, Store. Каталог
нужен, чтобы даже на чистой системе «открой Discord» находил Discord, а
«открой настройки дисплея» сразу открывал нужную страницу настроек.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AppSpec:
    key: str
    display_name: str
    aliases: tuple[str, ...] = ()
    kind: str = "app"            # app | folder | settings | shell | power | store
    # Windows
    exe: tuple[str, ...] = ()            # имена процессов/exe (для проверки и App Paths)
    win_paths: tuple[str, ...] = ()      # кандидаты (поддерживается * и %VAR%)
    protocols: tuple[str, ...] = ()      # URI-схемы
    appids: tuple[str, ...] = ()         # Store/UWP: shell:AppsFolder\<AppID>
    shell: str = ""                      # готовая shell-команда (ms-settings:, explorer.exe shell:...)
    args: tuple[str, ...] = ()           # постоянные аргументы
    # Linux / macOS
    linux_bins: tuple[str, ...] = ()
    linux_desktop: tuple[str, ...] = ()
    mac_app: str = ""
    mac_bins: tuple[str, ...] = ()
    # служебное
    discover: bool = True        # участвует ли в автообнаружении
    in_start_menu: tuple[str, ...] = ()  # как выглядит в меню «Пуск» (для поиска ярлыков)


def A(key: str, display: str, *aliases: str, **kw) -> AppSpec:
    return AppSpec(key=key, display_name=display, aliases=tuple(aliases), **kw)


# --------------------------------------------------------------------------
#  Приложения (пользовательские)
# --------------------------------------------------------------------------
APPS: list[AppSpec] = [
    # --- браузеры ---
    A("browser", "Браузер по умолчанию", "браузер", "браузер по умолчанию", "интернет",
      "зайди в интернет", "выйти в интернет", "веб", "browser", "default browser",
      "веб браузер", "веб-браузер", kind="app"),
    A("chrome", "Google Chrome", "хром", "гугл хром", "chrome", "google chrome", "гугл",
      exe=("chrome.exe",),
      win_paths=(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
                 r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
                 r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
      protocols=("http",),
      linux_bins=("google-chrome", "google-chrome-stable"), linux_desktop=("google-chrome.desktop",),
      mac_app="Google Chrome", in_start_menu=("Google Chrome",)),
    A("firefox", "Mozilla Firefox", "фаерфокс", "файрфокс", "мозилла", "mozila", "firefox",
      exe=("firefox.exe",),
      win_paths=(r"%ProgramFiles%\Mozilla Firefox\firefox.exe",
                 r"%ProgramFiles(x86)%\Mozilla Firefox\firefox.exe"),
      linux_bins=("firefox",), linux_desktop=("firefox.desktop",), mac_app="Firefox",
      in_start_menu=("Firefox",)),
    A("edge", "Microsoft Edge", "эдж", "край", "edge", "microsoft edge", "мс эдж",
      exe=("msedge.exe",),
      win_paths=(r"%ProgramFiles(x86)%\Microsoft Edge\Application\msedge.exe",
                 r"%ProgramFiles%\Microsoft Edge\Application\msedge.exe"),
      linux_bins=("microsoft-edge", "microsoft-edge-stable"),
      linux_desktop=("microsoft-edge.desktop",), mac_app="Microsoft Edge"),
    A("brave", "Brave", "брейв", "brave", "brave browser", exe=("brave.exe",),
      win_paths=(r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe",
                 r"%ProgramFiles(x86)%\BraveSoftware\Brave-Browser\Application\brave.exe"),
      linux_bins=("brave-browser",), linux_desktop=("brave-browser.desktop",),
      mac_app="Brave Browser"),
    A("opera", "Opera", "опера", "opera", exe=("opera.exe", "launcher.exe"),
      win_paths=(r"%LocalAppData%\Programs\Opera\launcher.exe",
                 r"%AppData%\Opera Software\Opera Stable\opera.exe"),
      linux_bins=("opera",), linux_desktop=("opera.desktop",), mac_app="Opera"),
    A("yandex-browser", "Яндекс Браузер", "яндекс браузер", "яндекс", "yandex browser",
      exe=("browser.exe",),
      win_paths=(r"%LocalAppData%\Yandex\YandexBrowser\Application\browser.exe",
                 r"%ProgramFiles%\Yandex\YandexBrowser\Application\browser.exe"),
      linux_bins=("yandex-browser", "browser"), mac_app="Yandex"),
    # --- мессенджеры и связь ---
    A("telegram", "Telegram", "телеграм", "телега", "телегам", "тг", "телегаа", "telegram",
      "телеграмм", "телега desktop", "тг десктоп",
      exe=("telegram.exe",),
      win_paths=(r"%AppData%\Telegram Desktop\Telegram.exe",
                 r"%LocalAppData%\Programs\Telegram Desktop\Telegram.exe",
                 r"%ProgramFiles%\Telegram Desktop\Telegram.exe"),
      protocols=("tg",), appids=("TelegramMessengerLLP.TelegramDesktop_t4vj0pshhgkwm!Telegram.Desktop",),
      linux_bins=("telegram-desktop", "telegram"), linux_desktop=("telegram-desktop.desktop",),
      mac_app="Telegram", in_start_menu=("Telegram",)),
    A("whatsapp", "WhatsApp", "ватсап", "вотсап", "вацап", "whatsapp", "вотс ап",
      exe=("whatsapp.exe",),
      win_paths=(r"%LocalAppData%\WhatsApp\WhatsApp.exe",
                 r"%ProgramFiles%\WindowsApps\*WhatsApp*.exe"),
      protocols=("whatsapp",), appids=("5319275A.WhatsAppDesktop_cv1g1gvanyjgm!WhatsAppDesktop",),
      mac_app="WhatsApp"),
    A("discord", "Discord", "дискорд", "дис", "discord", "диска", "дисорд", "дискордик",
      exe=("discord.exe", "update.exe"),
      win_paths=(r"%LocalAppData%\Discord\Update.exe",
                 r"%LocalAppData%\Discord\app-*\Discord.exe"),
      protocols=("discord",),
      linux_bins=("discord",), linux_desktop=("discord.desktop",), mac_app="Discord",
      in_start_menu=("Discord",)),
    A("slack", "Slack", "слак", "slack", exe=("slack.exe",),
      win_paths=(r"%LocalAppData%\slack\slack.exe", r"%LocalAppData%\slack\app-*\slack.exe"),
      protocols=("slack",), linux_bins=("slack",), linux_desktop=("slack.desktop",), mac_app="Slack"),
    A("teams", "Microsoft Teams", "тимс", "teams", "microsoft teams", "майкрософт тимс",
      exe=("teams.exe", "ms-teams.exe"),
      win_paths=(r"%LocalAppData%\Microsoft\WindowsApps\ms-teams.exe",
                 r"%LocalAppData%\Microsoft\Teams\current\Teams.exe"),
      protocols=("msteams",), appids=("MSTeams_8wekyb3d8bbwe!MSTeams", "MicrosoftTeams_8wekyb3d8bbwe!MicrosoftTeams"),),
    A("zoom", "Zoom", "зум", "zoom", exe=("zoom.exe",),
      win_paths=(r"%AppData%\Zoom\bin\Zoom.exe", r"%ProgramFiles%\Zoom\bin\Zoom.exe"),
      protocols=("zoommtg",), linux_bins=("zoom",), linux_desktop=("zoom.desktop",), mac_app="zoom.us"),
    A("skype", "Skype", "скайп", "skype", exe=("skype.exe",),
      win_paths=(r"%ProgramFiles%\Microsoft\Skype for Desktop\Skype.exe",
                 r"%LocalAppData%\Microsoft\WindowsApps\skype.exe"),
      protocols=("skype",), appids=("Microsoft.SkypeApp_kzf8qxf38zg5c!App",)),
    A("viber", "Viber", "вайбер", "вайбар", "viber", exe=("viber.exe",),
      win_paths=(r"%LocalAppData%\Viber\Viber.exe",), protocols=("viber",)),
    A("signal", "Signal", "сигнал", "signal", exe=("signal.exe",),
      win_paths=(r"%LocalAppData%\Programs\signal-desktop\Signal.exe",), protocols=("sgnl",),
      linux_bins=("signal-desktop",), linux_desktop=("signal-desktop.desktop",), mac_app="Signal"),
    # --- разработка ---
    A("vscode", "Visual Studio Code", "vs code", "вс код", "вскод", "vscode", "код",
      "вижуал студио код", "визуал студио код", "visual studio code", "студия код", "вс",
      exe=("code.exe",),
      win_paths=(r"%LocalAppData%\Programs\Microsoft VS Code\Code.exe",
                 r"%ProgramFiles%\Microsoft VS Code\Code.exe",
                 r"%ProgramFiles(x86)%\Microsoft VS Code\Code.exe"),
      protocols=("vscode",),
      linux_bins=("code", "code-insiders", "codium"),
      linux_desktop=("code.desktop", "visual-studio-code.desktop"),
      mac_app="Visual Studio Code", mac_bins=("code",),
      in_start_menu=("Visual Studio Code", "VS Code")),
    A("cursor", "Cursor", "курсор", "cursor", exe=("cursor.exe",),
      win_paths=(r"%LocalAppData%\Programs\cursor\Cursor.exe",
                 r"%ProgramFiles%\Cursor\Cursor.exe"),
      protocols=("cursor",), linux_bins=("cursor",), mac_app="Cursor"),
    A("python", "Python", "питон", "пайтон", "python", exe=("python.exe", "pythonw.exe"),
      win_paths=(r"%LocalAppData%\Programs\Python\Python*\python.exe",
                 r"%ProgramFiles%\Python*\python.exe", r"%LocalAppData%\Microsoft\WindowsApps\python.exe"),
      linux_bins=("python3", "python"), mac_bins=("python3",)),
    A("pycharm", "PyCharm", "пайчарм", "pycharm", exe=("pycharm64.exe", "pycharm.exe"),
      win_paths=(r"%LocalAppData%\Programs\PyCharm*\bin\pycharm64.exe",
                 r"%ProgramFiles%\JetBrains\PyCharm*\bin\pycharm64.exe",
                 r"%LocalAppData%\JetBrains\Toolbox\apps\PyCharm*\*\bin\pycharm64.exe"),
      linux_bins=("pycharm", "pycharm-professional"), mac_app="PyCharm"),
    A("intellij", "IntelliJ IDEA", "интеледжей", "idea", "intellij", "интеллиджей",
      exe=("idea64.exe",),
      win_paths=(r"%LocalAppData%\Programs\IntelliJ IDEA*\bin\idea64.exe",
                 r"%ProgramFiles%\JetBrains\IntelliJ IDEA*\bin\idea64.exe"),
      linux_bins=("idea",), mac_app="IntelliJ IDEA"),
    A("android-studio", "Android Studio", "андроид студио", "android studio",
      exe=("studio64.exe",),
      win_paths=(r"%ProgramFiles%\Android\Android Studio\bin\studio64.exe",
                 r"%LocalAppData%\Programs\Android Studio\bin\studio64.exe"),
      linux_bins=("android-studio",), mac_app="Android Studio"),
    A("sublime", "Sublime Text", "саблайм", "sublime", "sublime text", exe=("sublime_text.exe",),
      win_paths=(r"%ProgramFiles%\Sublime Text\sublime_text.exe",
                 r"%ProgramFiles%\Sublime Text 3\sublime_text.exe"),
      linux_bins=("subl",), mac_app="Sublime Text"),
    A("notepadpp", "Notepad++", "нотпад плюс", "notepad++", "нотпад++", exe=("notepad++.exe",),
      win_paths=(r"%ProgramFiles%\Notepad++\notepad++.exe",
                 r"%ProgramFiles(x86)%\Notepad++\notepad++.exe"),
      linux_bins=("notepadqq",), in_start_menu=("Notepad++",)),
    A("github-desktop", "GitHub Desktop", "гитхаб десктоп", "github desktop",
      exe=("githubdesktop.exe",),
      win_paths=(r"%LocalAppData%\GitHubDesktop\GitHubDesktop.exe",),
      protocols=("x-github-client",), mac_app="GitHub Desktop"),
    A("docker", "Docker Desktop", "докер", "docker", "docker desktop",
      exe=("docker desktop.exe",),
      win_paths=(r"%ProgramFiles%\Docker\Docker\Docker Desktop.exe",),
      linux_bins=("docker",), mac_app="Docker"),
    A("postman", "Postman", "постман", "postman", exe=("postman.exe",),
      win_paths=(r"%LocalAppData%\Postman\Postman.exe",), linux_bins=("postman",),
      mac_app="Postman"),
    # --- медиа, игры, творчество ---
    A("spotify", "Spotify", "спотифай", "споти", "spotify", exe=("spotify.exe",),
      win_paths=(r"%AppData%\Spotify\Spotify.exe", r"%ProgramFiles%\WindowsApps\*Spotify*.exe"),
      protocols=("spotify",), appids=("SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify",),
      linux_bins=("spotify",), linux_desktop=("spotify.desktop",), mac_app="Spotify",
      in_start_menu=("Spotify",)),
    A("vlc", "VLC", "влс", "vlc", "vlc player", exe=("vlc.exe",),
      win_paths=(r"%ProgramFiles%\VideoLAN\VLC\vlc.exe",
                 r"%ProgramFiles(x86)%\VideoLAN\VLC\vlc.exe"),
      linux_bins=("vlc",), linux_desktop=("vlc.desktop",), mac_app="VLC"),
    A("obs", "OBS Studio", "обс", "obs", "obs studio", exe=("obs64.exe", "obs32.exe"),
      win_paths=(r"%ProgramFiles%\obs-studio\bin\64bit\obs64.exe",
                 r"%ProgramFiles(x86)%\obs-studio\bin\32bit\obs32.exe"),
      linux_bins=("obs",), linux_desktop=("com.obsproject.Studio.desktop",), mac_app="OBS"),
    A("steam", "Steam", "стим", "steam", exe=("steam.exe",),
      win_paths=(r"%ProgramFiles(x86)%\Steam\steam.exe", r"%ProgramFiles%\Steam\steam.exe"),
      protocols=("steam",), linux_bins=("steam",), linux_desktop=("steam.desktop",), mac_app="Steam",
      in_start_menu=("Steam",)),
    A("epicgames", "Epic Games Launcher", "эпик", "эпик геймс", "epic games", "epic",
      exe=("epicgameslauncher.exe",),
      win_paths=(r"%ProgramFiles(x86)%\Epic Games\Launcher\Portal\Binaries\Win32\EpicGamesLauncher.exe",),
      protocols=("com.epicgames.launcher",)),
    A("battlenet", "Battle.net", "батл нет", "battle.net", "батлнет",
      exe=("battle.net.exe",),
      win_paths=(r"%ProgramFiles(x86)%\Battle.net\Battle.net Launcher.exe",
                 r"%ProgramFiles%\Battle.net\Battle.net Launcher.exe")),
    A("discord-canary", "Discord Canary", "дискорд канарейка", exe=("discordcanary.exe",),
      win_paths=(r"%LocalAppData%\DiscordCanary\Update.exe",), discover=False),
    A("audacity", "Audacity", "аудасити", "audacity", exe=("audacity.exe",),
      win_paths=(r"%ProgramFiles%\Audacity\audacity.exe",), linux_bins=("audacity",)),
    A("qbittorrent", "qBittorrent", "кубит", "qbittorrent", "торрент", "torrent",
      exe=("qbittorrent.exe",),
      win_paths=(r"%ProgramFiles%\qBittorrent\qbittorrent.exe",
                 r"%ProgramFiles(x86)%\qBittorrent\qbittorrent.exe"),
      linux_bins=("qbittorrent",)),
    # --- документы и тексты ---
    A("word", "Microsoft Word", "ворд", "word", "ms word", "майкрософт ворд", exe=("winword.exe",),
      win_paths=(r"%ProgramFiles%\Microsoft Office\root\Office16\WINWORD.EXE",
                 r"%ProgramFiles(x86)%\Microsoft Office\root\Office16\WINWORD.EXE",
                 r"%ProgramFiles%\Microsoft Office\Office16\WINWORD.EXE",
                 r"%ProgramFiles%\Microsoft Office\Office15\WINWORD.EXE"),
      linux_bins=("libreoffice",), mac_app="Microsoft Word", in_start_menu=("Word",)),
    A("excel", "Microsoft Excel", "эксель", "excel", "мс эксель", exe=("excel.exe",),
      win_paths=(r"%ProgramFiles%\Microsoft Office\root\Office16\EXCEL.EXE",
                 r"%ProgramFiles(x86)%\Microsoft Office\root\Office16\EXCEL.EXE",
                 r"%ProgramFiles%\Microsoft Office\Office16\EXCEL.EXE"),
      linux_bins=("libreoffice",), mac_app="Microsoft Excel", in_start_menu=("Excel",)),
    A("powerpoint", "Microsoft PowerPoint", "поверпоинт", "поинт", "powerpoint", "ppt",
      exe=("powerpnt.exe",),
      win_paths=(r"%ProgramFiles%\Microsoft Office\root\Office16\POWERPNT.EXE",
                 r"%ProgramFiles(x86)%\Microsoft Office\root\Office16\POWERPNT.EXE"),
      mac_app="Microsoft PowerPoint"),
    A("outlook", "Microsoft Outlook", "аутлук", "outlook", "почта", exe=("outlook.exe",),
      win_paths=(r"%ProgramFiles%\Microsoft Office\root\Office16\OUTLOOK.EXE",
                 r"%ProgramFiles(x86)%\Microsoft Office\root\Office16\OUTLOOK.EXE"),
      protocols=("mailto", "outlook",), mac_app="Microsoft Outlook"),
    A("onenote", "Microsoft OneNote", "ваннот", "onenote", "уаннот", exe=("onenote.exe",),
      win_paths=(r"%ProgramFiles%\Microsoft Office\root\Office16\ONENOTE.EXE",
                 r"%ProgramFiles%\Microsoft Office\Office16\ONENOTE.EXE"),
      protocols=("onenote",), appids=("Microsoft.Office.OneNote_8wekyb3d8bbwe!microsoft.onenoteim",)),
    A("acrobat", "Adobe Acrobat Reader", "акробат", "adobe reader", "acrobat", "пдф",
      exe=("acrobat.exe",),
      win_paths=(r"%ProgramFiles%\Adobe\Acrobat DC\Acrobat\Acrobat.exe",
                 r"%ProgramFiles%\Adobe\Acrobat Reader DC\Reader\AcroRd32.exe"),
      linux_bins=("evince", "okular"), mac_app="Adobe Acrobat Reader"),
    A("notion", "Notion", "ноушен", "notion", exe=("notion.exe",),
      win_paths=(r"%LocalAppData%\Programs\Notion\Notion.exe",), protocols=("notion",),
      mac_app="Notion"),
    A("obsidian", "Obsidian", "обсидиан", "obsidian", exe=("obsidian.exe",),
      win_paths=(r"%LocalAppData%\Obsidian\Obsidian.exe",
                 r"%ProgramFiles%\Obsidian\Obsidian.exe"),
      protocols=("obsidian",), linux_bins=("obsidian",), mac_app="Obsidian"),
    A("7zip", "7-Zip", "7зип", "7zip", "7-zip", exe=("7zfm.exe",),
      win_paths=(r"%ProgramFiles%\7-Zip\7zFM.exe", r"%ProgramFiles(x86)%\7-Zip\7zFM.exe")),
    A("winrar", "WinRAR", "винрар", "winrar", exe=("winrar.exe",),
      win_paths=(r"%ProgramFiles%\WinRAR\WinRAR.exe", r"%ProgramFiles(x86)%\WinRAR\WinRAR.exe")),
    # --- графика ---
    A("gimp", "GIMP", "гимп", "gimp", exe=("gimp-2.10.exe", "gimp.exe"),
      win_paths=(r"%ProgramFiles%\GIMP 2\bin\gimp-2.10.exe",
                 r"%ProgramFiles%\GIMP 3\bin\gimp-3.0.exe"),
      linux_bins=("gimp",), linux_desktop=("gimp.desktop",), mac_app="GIMP"),
    A("inkscape", "Inkscape", "инкскейп", "inkscape", exe=("inkscape.exe",),
      win_paths=(r"%ProgramFiles%\Inkscape\bin\inkscape.exe",), linux_bins=("inkscape",),
      mac_app="Inkscape"),
    A("photoshop", "Adobe Photoshop", "фотошоп", "photoshop", "adobe photoshop",
      exe=("photoshop.exe",),
      win_paths=(r"%ProgramFiles%\Adobe\Adobe Photoshop *\Photoshop.exe",),
      mac_app="Adobe Photoshop"),
    A("blender", "Blender", "блендер", "blender", exe=("blender.exe",),
      win_paths=(r"%ProgramFiles%\Blender Foundation\Blender *\blender.exe",),
      linux_bins=("blender",), mac_app="Blender"),
    A("figma", "Figma", "фигма", "figma", exe=("figma.exe",),
      win_paths=(r"%LocalAppData%\Figma\Figma.exe",), mac_app="Figma"),
    A("sharex", "ShareX", "шеар икс", "sharex", exe=("sharex.exe",),
      win_paths=(r"%ProgramFiles%\ShareX\ShareX.exe",)),
]


# --------------------------------------------------------------------------
#  Системные инструменты Windows (прямые пути: самый быстрый способ)
# --------------------------------------------------------------------------
SYSTEM_TOOLS: list[AppSpec] = [
    A("explorer", "Проводник", "проводник", "explorer", "файлы", "мои файлы", "file explorer",
      "этот компьютер", "мой компьютер", "this pc",
      exe=("explorer.exe",), win_paths=(r"%windir%\explorer.exe",),
      linux_bins=("nautilus", "dolphin", "thunar", "nemo", "pcmanfm", "xdg-open"),
      mac_app="Finder"),
    A("taskmgr", "Диспетчер задач", "диспетчер задач", "таск менеджер", "task manager", "taskmgr",
      exe=("taskmgr.exe",), win_paths=(r"%windir%\System32\Taskmgr.exe",),
      linux_bins=("gnome-system-monitor", "ksysguard"), mac_app="Activity Monitor",
      in_start_menu=("Диспетчер задач", "Task Manager")),
    A("terminal", "Терминал", "терминал", "консоль", "командная строка", "command prompt", "cmd",
      "windows terminal", "повершелл", "powershell", "шелл",
      exe=("windowsterminal.exe", "wt.exe", "cmd.exe", "powershell.exe"),
      win_paths=(r"%LocalAppData%\Microsoft\WindowsApps\wt.exe",
                 r"%windir%\System32\cmd.exe",
                 r"%windir%\System32\WindowsPowerShell\v1.0\powershell.exe"),
      linux_bins=("gnome-terminal", "konsole", "xterm", "alacritty"),
      linux_desktop=("org.gnome.Terminal.desktop",), mac_app="Terminal"),
    A("powershell", "PowerShell", "powershell", "повершелл", "пс",
      exe=("powershell.exe", "pwsh.exe"),
      win_paths=(r"%windir%\System32\WindowsPowerShell\v1.0\powershell.exe",
                 r"%ProgramFiles%\PowerShell\7\pwsh.exe"), linux_bins=("pwsh",)),
    A("settings", "Параметры Windows", "настройки", "параметры", "настройки windows", "settings",
      "системные настройки", "конфигурация", exe=("systemsettings.exe",),
      protocols=("ms-settings",), shell="ms-settings:",
      linux_bins=("gnome-control-center", "systemsettings"), mac_app="System Settings",
      in_start_menu=("Параметры",)),
    A("controlpanel", "Панель управления", "панель управления", "control panel", "контрол панель",
      exe=("control.exe",), shell="control", linux_bins=("systemsettings",)),
    A("regedit", "Редактор реестра", "реестр", "regedit", "редактор реестра",
      exe=("regedit.exe",), win_paths=(r"%windir%\regedit.exe",)),
    A("services", "Службы", "службы", "services", "службы windows", exe=("services.exe",),
      shell="services.msc"),
    A("devicemgr", "Диспетчер устройств", "диспетчер устройств", "device manager",
      shell="devmgmt.msc"),
    A("diskmgmt", "Управление дисками", "управление дисками", "disk management",
      shell="diskmgmt.msc"),
    A("eventvwr", "Просмотр событий", "просмотр событий", "журнал событий", "event viewer",
      shell="eventvwr.msc"),
    A("msinfo", "Сведения о системе", "сведения о системе", "msinfo", shell="msinfo32"),
    A("resmon", "Монитор ресурсов", "монитор ресурсов", "resource monitor", shell="resmon"),
    A("cleanmgr", "Очистка диска", "очистка диска", "disk cleanup", shell="cleanmgr"),
    A("calc", "Калькулятор", "калькулятор", "calculator", "calc", exe=("calculatorapp.exe", "calc.exe"),
      win_paths=(r"%windir%\System32\calc.exe",),
      appids=("Microsoft.WindowsCalculator_8wekyb3d8bbwe!App",),
      linux_bins=("gnome-calculator", "kcalc"), mac_app="Calculator"),
    A("notepad", "Блокнот", "блокнот", "notepad", exe=("notepad.exe",),
      win_paths=(r"%windir%\notepad.exe", r"%LocalAppData%\Microsoft\WindowsApps\notepad.exe"),
      linux_bins=("gedit", "kate", "mousepad"), mac_app="TextEdit",
      in_start_menu=("Блокнот", "Notepad")),
    A("paint", "Paint", "паинт", "пейнт", "paint", exe=("mspaint.exe",),
      win_paths=(r"%windir%\System32\mspaint.exe",), linux_bins=("gimp",), mac_app="Preview"),
    A("snipping", "Ножницы", "ножницы", "скриншот тул", "snipping tool", "screenshot tool",
      exe=("snippingtool.exe",),
      win_paths=(r"%windir%\System32\SnippingTool.exe",
                 r"%LocalAppData%\Microsoft\WindowsApps\SnippingTool.exe"),
      protocols=("ms-screenclip", "ms-screensketch")),
    A("charmap", "Таблица символов", "таблица символов", "charmap", shell="charmap"),
    A("magnifier", "Экранная лупа", "лупа", "magnifier", shell="magnify"),
    A("osk", "Экранная клавиатура", "экранная клавиатура", "on-screen keyboard", shell="osk"),
    A("store", "Microsoft Store", "магазин", "стор", "microsoft store", "store",
      protocols=("ms-windows-store",)),
    A("xbox", "Xbox", "иксбокс", "xbox", protocols=("xbox",),
      appids=("Microsoft.GamingApp_8wekyb3d8bbwe!Microsoft.Xbox.App",)),
    A("camera", "Камера", "камера", "camera",
      appids=("Microsoft.WindowsCamera_8wekyb3d8bbwe!App",),
      linux_bins=("cheese",)),
    A("photos", "Фотографии", "фотографии", "photos", "галерея",
      appids=("Microsoft.Windows.Photos_8wekyb3d8bbwe!App",)),
    A("music", "Музыка", "музыка", "groove music", appids=("Microsoft.ZuneMusic_8wekyb3d8bbwe!Microsoft.ZuneMusic",)),
]


# --------------------------------------------------------------------------
#  Страницы настроек Windows (URI ms-settings:) — открываются мгновенно
# --------------------------------------------------------------------------
SETTINGS_PAGES: list[AppSpec] = [
    A("settings-display", "Настройки: экран", "настройки дисплея", "настройки экрана",
      "разрешение экрана", "масштаб", "яркость настройки", "монитор настройки",
      shell="ms-settings:display", kind="settings", discover=False),
    A("settings-sound", "Настройки: звук", "настройки звука", "звук настройки", "звуковые устройства",
      shell="ms-settings:sound", kind="settings", discover=False),
    A("settings-bluetooth", "Настройки: Bluetooth", "настройки блютуз", "bluetooth настройки",
      shell="ms-settings:bluetooth", kind="settings", discover=False),
    A("settings-network", "Настройки: сеть", "настройки сети", "сетевые настройки",
      shell="ms-settings:network", kind="settings", discover=False),
    A("settings-wifi", "Настройки: Wi-Fi", "настройки вайфай", "настройки wi-fi", "настройки вай-фая",
      shell="ms-settings:network-wifi", kind="settings", discover=False),
    A("settings-apps", "Настройки: приложения", "установленные приложения", "удаление программ",
      "программы и компоненты", shell="ms-settings:appsfeatures", kind="settings", discover=False),
    A("settings-update", "Настройки: обновление", "центр обновления", "обновление windows",
      "виндовс апдейт", shell="ms-settings:windowsupdate", kind="settings", discover=False),
    A("settings-power", "Настройки: питание", "настройки питания", "спящий режим настройки",
      shell="ms-settings:powersleep", kind="settings", discover=False),
    A("settings-printers", "Настройки: принтеры", "настройки принтера", "принтеры и сканеры",
      shell="ms-settings:printers", kind="settings", discover=False),
    A("settings-mouse", "Настройки: мышь", "настройки мыши", "скорость курсора",
      shell="ms-settings:mousetouchpad", kind="settings", discover=False),
    A("settings-keyboard", "Настройки: клавиатура", "настройки клавиатуры", "раскладка клавиатуры",
      shell="ms-settings:keyboard", kind="settings", discover=False),
    A("settings-datetime", "Настройки: время и язык", "настройки времени", "дата и время",
      "часовой пояс", shell="ms-settings:dateandtime", kind="settings", discover=False),
    A("settings-language", "Настройки: язык", "язык системы", "языковые настройки",
      shell="ms-settings:regionlanguage", kind="settings", discover=False),
    A("settings-personalization", "Настройки: персонализация", "персонализация", "обои настройки",
      "тема оформления", "смена темы", shell="ms-settings:personalization-background",
      kind="settings", discover=False),
    A("settings-default-apps", "Настройки: приложения по умолчанию", "приложения по умолчанию",
      "браузер по умолчанию настройки", shell="ms-settings:defaultapps",
      kind="settings", discover=False),
    A("settings-startup", "Настройки: автозагрузка", "автозагрузка", "программы в автозагрузке",
      shell="ms-settings:startupapps", kind="settings", discover=False),
    A("settings-storage", "Настройки: хранилище", "хранилище", "место на диске настройки",
      shell="ms-settings:storagesense", kind="settings", discover=False),
    A("settings-privacy", "Настройки: конфиденциальность", "конфиденциальность", "приватность",
      shell="ms-settings:privacy", kind="settings", discover=False),
    A("settings-accounts", "Настройки: учётные записи", "учётные записи", "аккаунты",
      shell="ms-settings:yourinfo", kind="settings", discover=False),
    A("settings-taskbar", "Настройки: панель задач", "настройки панели задач", "панель задач",
      shell="ms-settings:taskbar", kind="settings", discover=False),
    A("settings-clipboard", "Настройки: буфер обмена", "журнал буфера обмена",
      shell="ms-settings:clipboard", kind="settings", discover=False),
]


# --------------------------------------------------------------------------
#  Системные папки и действия
# --------------------------------------------------------------------------
FOLDERS: list[AppSpec] = [
    A("downloads", "Загрузки", "загрузки", "папка загрузки", "downloads", "мои загрузки",
      kind="folder", discover=False,
      win_paths=(r"%USERPROFILE%\Downloads",), shell="shell:Downloads",
      linux_bins=("xdg-open",), mac_app="Downloads"),
    A("desktop", "Рабочий стол", "рабочий стол", "десктоп", "desktop", kind="folder",
      discover=False, win_paths=(r"%USERPROFILE%\Desktop",), shell="shell:Desktop"),
    A("documents", "Документы", "документы", "мои документы", "documents", kind="folder",
      discover=False, win_paths=(r"%USERPROFILE%\Documents",), shell="shell:Personal"),
    A("pictures", "Изображения", "изображения", "картинки папка", "pictures", kind="folder",
      discover=False, win_paths=(r"%USERPROFILE%\Pictures",), shell="shell:My Pictures"),
    A("music-folder", "Музыка (папка)", "папка музыки", "музыка папка", kind="folder",
      discover=False, win_paths=(r"%USERPROFILE%\Music",), shell="shell:My Music"),
    A("videos", "Видео", "видео папка", "videos", kind="folder", discover=False,
      win_paths=(r"%USERPROFILE%\Videos",), shell="shell:My Video"),
    A("recyclebin", "Корзина", "корзина", "recycle bin", "trash", kind="folder", discover=False,
      shell="shell:RecycleBinFolder"),
    A("temp", "Временные файлы", "временная папка", "temp", kind="folder", discover=False,
      win_paths=(r"%TEMP%",)),
    A("home", "Домашняя папка", "домашняя папка", "домашний каталог", "home", kind="folder",
      discover=False, win_paths=(r"%USERPROFILE%",)),
    A("projects", "Проекты", "папка проектов", "projects", kind="folder", discover=False,
      win_paths=(r"%USERPROFILE%\source\repos", r"%USERPROFILE%\Projects",
                 r"%USERPROFILE%\Documents\Projects")),
]

POWER_ACTIONS: list[AppSpec] = [
    A("shutdown", "Выключение", "выключи компьютер", "выключить компьютер", "выключи пк",
      "shutdown", "выключение", kind="power", discover=False),
    A("restart", "Перезагрузка", "перезагрузи компьютер", "перезагрузить компьютер",
      "перезагрузка", "reboot", "restart", kind="power", discover=False),
    A("lock", "Блокировка", "заблокируй компьютер", "заблокировать компьютер", "блокировка",
      "lock", kind="power", discover=False),
    A("sleep", "Спящий режим", "спящий режим", "усыпи компьютер", "сон", "sleep", kind="power",
      discover=False),
    A("logoff", "Выход из системы", "выйди из системы", "заверши сеанс", "log off", "logoff",
      kind="power", discover=False),
    A("monitor-off", "Выключить монитор", "выключи монитор", "погаси экран", "monitor off",
      kind="power", discover=False),
    A("show-desktop", "Показать рабочий стол", "покажи рабочий стол", "сверни все окна",
      "show desktop", kind="power", discover=False),
]


ALL_SPECS: list[AppSpec] = APPS + SYSTEM_TOOLS + SETTINGS_PAGES + FOLDERS + POWER_ACTIONS

BY_KEY: dict[str, AppSpec] = {s.key: s for s in ALL_SPECS}


def builtin_aliases() -> dict[str, tuple[str, ...]]:
    """{ключ: алиасы} — для AliasResolver."""
    return {s.key: (s.display_name,) + s.aliases for s in ALL_SPECS}


def spec(key: str) -> AppSpec | None:
    return BY_KEY.get(key)


def specs_for_discovery() -> list[AppSpec]:
    return [s for s in ALL_SPECS if s.discover]
