"""Desktop-оболочка: нативное окно через pywebview.

Поднимает Web-дашборд на 127.0.0.1 (свободный порт) и показывает его в
нативном окне (Windows: WebView2, Linux: GTK WebKit, macOS: WKWebView).
Если pywebview не установлен — открывается браузер (приложение продолжает
полностью работать).
"""
from __future__ import annotations

import socket
import sys
from pathlib import Path


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _project_root() -> Path:
    # agent/ui/desktop.py → корень проекта
    return Path(__file__).resolve().parents[2]


def _icon_path() -> str | None:
    for name in ("icon.ico", "icon.png"):
        p = _project_root() / "assets" / name
        if p.exists():
            return str(p)
    return None


def _dpi_aware() -> None:
    """Чёткость на HiDPI-экранах Windows."""
    if sys.platform == "win32":
        try:
            import ctypes
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)  # type: ignore[attr-defined]
            except Exception:
                ctypes.windll.user32.SetProcessDPIAware()  # type: ignore[attr-defined]
        except Exception:
            pass


class _WindowApi:
    """API окна для кнопок интерфейса (свернуть/закрыть)."""

    def __init__(self) -> None:
        self.window = None

    def minimize(self) -> bool:
        if self.window is not None:
            self.window.minimize()
            return True
        return False

    def maximize(self) -> bool:
        if self.window is None:
            return False
        try:
            if self.window.maximized:
                self.window.restore()
            else:
                self.window.maximize()
        except Exception:  # noqa: BLE001
            try:
                self.window.maximize()
            except Exception:  # noqa: BLE001
                return False
        return True

    def close(self) -> bool:
        if self.window is not None:
            self.window.destroy()
            return True
        return False


def run_desktop(rt, headless: bool = False, width: int = 1340, height: int = 860,
                title: str = "Copmuter") -> tuple:
    """Запускает UI. Возвращает (ui, url). В оконном режиме блокирует,
    пока окно открыто."""
    from .web import serve
    port = free_port()
    ui = serve(rt, host="127.0.0.1", port=port)
    url = f"http://127.0.0.1:{port}"
    rt.bus.emit("log", level="info", message=f"UI запущен: {url}")

    if headless:
        return ui, url

    try:
        import webview  # type: ignore
    except ImportError:
        print("pywebview не найден — открываю дашборд в системном браузере.\n"
              "Для нативного окна:  pip install pywebview\n" + url)
        import webbrowser
        webbrowser.open(url)
        return ui, url

    _dpi_aware()
    api = _WindowApi()
    # Прозрачное окно: интерфейс — стекло поверх рабочего стола.
    # Если версия pywebview/платформа не поддерживает — обычное окно.
    window = None
    last_err: Exception | None = None
    attempts = (
        {"transparent": True, "js_api": api},
        {"transparent": False, "js_api": api, "background_color": "#0A1A1F"},
        {"background_color": "#0A1A1F"},
    )
    for opts in attempts:
        try:
            window = webview.create_window(
                title, url, width=width, height=height,
                min_size=(1024, 680), **opts)
            break
        except TypeError as e:
            last_err = e
    if window is None:
        raise last_err  # type: ignore[misc]
    api.window = window
    # debug=False: стабильно; fullscreen=False
    webview.start()  # блокирует до закрытия окна
    return ui, url
