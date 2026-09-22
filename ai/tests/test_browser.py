"""Тесты браузерного пути: Playwright подменяется, чтобы проверять нашу логику.

Настоящий Chromium здесь не запускается (в CI его может не быть), поэтому проверяем
то, что принадлежит нам: разбор действий, селекторы, ожидания, честные ошибки,
переиспользование браузера и поведение пакета шагов.
"""

from __future__ import annotations

import sys
import types
import unittest

from ai.browser import BrowserError, BrowserSession


class FakeLocator:
    def __init__(self, page, selector: str) -> None:
        self.page = page
        self.selector = selector
        self.first = self

    def count(self) -> int:
        return self.page.matches.get(self.selector, 1)

    def click(self, **kwargs) -> None:
        self.page.actions.append(("click", self.selector))
        if self.page.matches.get(self.selector, 1) == 0:
            raise RuntimeError("element not found")

    def fill(self, text: str, **kwargs) -> None:
        self.page.actions.append(("fill", self.selector, text))

    def press(self, keys: str) -> None:
        self.page.actions.append(("press", self.selector, keys))

    def inner_text(self, **kwargs) -> str:
        return self.page.text_map.get(self.selector, f"текст {self.selector}")

    def get_attribute(self, name: str) -> str:
        return f"https://example.com/{self.selector}"

    def wait_for(self, **kwargs) -> None:
        self.page.actions.append(("wait", self.selector))

    def all(self):
        return [FakeLocator(self.page, f"link{i}") for i in range(3)]


class FakePage:
    def __init__(self) -> None:
        self.url = "about:blank"
        self.actions: list[tuple] = []
        self.matches: dict[str, int] = {}
        self.text_map: dict[str, str] = {"#results": "Котики: 10 видео"}
        self.screenshots: list[str] = []

    # локаторы
    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, selector)

    def get_by_text(self, text: str) -> FakeLocator:
        return FakeLocator(self, f"text={text}")

    def get_by_role(self, role: str, name: str = "") -> FakeLocator:
        return FakeLocator(self, f"role={role}[name={name}]" if name else f"role={role}")

    # действия
    def goto(self, url: str, **kwargs):
        self.url = url if "://" in url else "https://" + url
        self.actions.append(("goto", self.url))
        return types.SimpleNamespace(status=200)

    def title(self) -> str:
        return "YouTube"

    def wait_for_timeout(self, ms: int) -> None:
        self.actions.append(("wait_ms", ms))

    def wait_for_load_state(self, state: str, **kwargs) -> None:
        self.actions.append(("load_state", state))

    def wait_for_url(self, pattern: str, **kwargs) -> None:
        self.actions.append(("wait_url", pattern))

    def content(self) -> str:
        return "<html>страница</html>"

    def evaluate(self, script: str):
        self.actions.append(("eval", script))
        return {"ok": True}

    def screenshot(self, path=None, full_page=False) -> bytes:
        self.screenshots.append(path or "")
        return b"PNG" * 10

    def go_back(self, **kwargs) -> None:
        self.actions.append(("back",))

    def set_default_timeout(self, ms: int) -> None:
        self.actions.append(("timeout", ms))

    @property
    def keyboard(self):
        return self

    def press(self, keys: str) -> None:
        self.actions.append(("keyboard_press", keys))

    @property
    def mouse(self):
        return self

    def click(self, x: int, y: int) -> None:
        self.actions.append(("mouse_click", x, y))

    def expect_download(self, **kwargs):
        page = self

        class _Ctx:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            @property
            def value(self):
                return types.SimpleNamespace(suggested_filename="file.pdf", save_as=page._save)

        return _Ctx()

    def _save(self, target: str) -> None:
        self.actions.append(("save", target))


class FakeBrowser:
    def __init__(self) -> None:
        self.launches = 0

    def new_context(self, **kwargs):
        return types.SimpleNamespace(new_page=lambda: FakePage(), close=lambda: None)

    def close(self) -> None:
        pass


class FakeDriver:
    """Подмена sync_playwright(): считаем запуски Chromium."""

    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.launches = 0

    @property
    def chromium(self):
        return self

    def launch(self, **kwargs):
        self.launches += 1
        browser = types.SimpleNamespace()
        browser.new_context = lambda **kw: types.SimpleNamespace(
            new_page=lambda: self.page, close=lambda: None)
        browser.close = lambda: None
        return browser

    def start(self):
        return self

    def stop(self) -> None:
        pass


class TestBrowserSession(unittest.TestCase):
    """Проверяем наш код с подменённым Playwright: сам Chromium в CI не нужен."""

    def setUp(self) -> None:
        self.page = FakePage()
        self.driver = FakeDriver(self.page)
        self._saved = {k: v for k, v in sys.modules.items() if k.startswith("playwright")}
        module = types.ModuleType("playwright.sync_api")
        module.sync_playwright = lambda: self.driver      # type: ignore[attr-defined]
        parent = types.ModuleType("playwright")
        parent.sync_api = module                          # type: ignore[attr-defined]
        sys.modules["playwright"] = parent
        sys.modules["playwright.sync_api"] = module
        self.session = BrowserSession(headless=True)

    def tearDown(self) -> None:
        for key in ("playwright", "playwright.sync_api"):
            sys.modules.pop(key, None)
        sys.modules.update(self._saved)

    def test_open_and_text(self) -> None:
        self.assertTrue(self.session.handle({"action": "open", "url": "youtube.com"})["ok"])
        self.assertEqual(self.page.url, "https://youtube.com")
        result = self.session.handle({"action": "text", "selector": "#results"})
        self.assertTrue(result["ok"])
        self.assertIn("Котики", result["data"]["text"])

    def test_click_missing_element_is_honest_error(self) -> None:
        self.page.matches["#nope"] = 0
        result = self.session.handle({"action": "click", "selector": "#nope"})
        self.assertFalse(result["ok"])
        self.assertIn("элемент не найден", result["error"])

    def test_selectors_text_and_role(self) -> None:
        self.assertTrue(self.session.handle({"action": "click", "selector": "text=Войти"})["ok"])
        self.assertTrue(self.session.handle({"action": "click",
                                             "selector": "role=button[name=Отправить]"})["ok"])
        clicks = [a for a in self.page.actions if a[0] == "click"]
        self.assertEqual(clicks[0][1], "text=Войти")
        self.assertIn("role=button", clicks[1][1])

    def test_fill_and_enter(self) -> None:
        result = self.session.handle({"action": "fill", "selector": "#q", "text": "котики",
                                      "enter": True})
        self.assertTrue(result["ok"])
        self.assertIn(("fill", "#q", "котики"), self.page.actions)
        self.assertIn(("press", "#q", "Enter"), self.page.actions)

    def test_wait_uses_state_not_sleep(self) -> None:
        self.assertTrue(self.session.handle({"action": "wait", "selector": "#ready"})["ok"])
        self.assertTrue(self.session.handle({"action": "wait",
                                             "url_contains": "results"})["ok"])
        waits = [a for a in self.page.actions if a[0] in ("wait", "wait_url")]
        self.assertEqual(len(waits), 2)

    def test_links_filters(self) -> None:
        result = self.session.handle({"action": "links", "contains": "example"})
        self.assertTrue(result["ok"])
        self.assertGreaterEqual(result["data"]["count"], 1)

    def test_unknown_action(self) -> None:
        result = self.session.handle({"action": "телепорт"})
        self.assertFalse(result["ok"])
        self.assertIn("неизвестное действие", result["error"])
        result2 = self.session.handle({"action": "click"})
        self.assertFalse(result2["ok"])
        self.assertIn("не передан элемент", result2["error"])

    def test_batch_stops_on_error(self) -> None:
        self.page.matches["#bad"] = 0
        outcome = self.session.run_batch([
            {"action": "open", "url": "https://ya.ru"},
            {"action": "click", "selector": "#bad"},
            {"action": "text"},
        ])
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["steps"], 2)      # третий шаг не выполняется

    def test_info_before_start(self) -> None:
        fresh = BrowserSession(headless=True)
        info = fresh.handle({"action": "info"})
        self.assertTrue(info["ok"])
        self.assertFalse(info["data"]["running"])

    def test_missing_playwright_is_explained(self) -> None:
        """Без Playwright пользователь получает инструкцию, а не трейсбек."""
        session = BrowserSession(headless=True)
        saved = {k: v for k, v in sys.modules.items() if k.startswith("playwright")}
        for key in saved:
            del sys.modules[key]
        try:
            sys.modules["playwright"] = None            # type: ignore[assignment]
            sys.modules["playwright.sync_api"] = None   # type: ignore[assignment]
            with self.assertRaises(BrowserError) as ctx:
                session.start()
            self.assertIn("Playwright не установлен", str(ctx.exception))
        finally:
            for key in ("playwright", "playwright.sync_api"):
                sys.modules.pop(key, None)
            sys.modules.update(saved)

    def test_browser_is_reused(self) -> None:
        """Второе действие не поднимает новый браузер: запуск — один раз за сессию."""
        self.session.handle({"action": "open", "url": "a.com"})
        self.session.handle({"action": "text"})
        self.session.handle({"action": "links"})
        self.assertEqual(self.driver.launches, 1)
        self.assertTrue(self.session.running)
        self.session.close()
        self.assertFalse(self.session.running)

    def test_start_failure_is_explained(self) -> None:
        class BrokenDriver(FakeDriver):
            def launch(self, **kwargs):
                raise RuntimeError("Executable doesn't exist at .../chrome")

        self.driver = BrokenDriver(self.page)
        session = BrowserSession(headless=True)
        result = session.handle({"action": "open", "url": "example.com"})
        self.assertFalse(result["ok"])
        self.assertIn("Chromium не запустился", result["error"])
        self.assertIn("playwright install", result["error"])


class TestWorkerBrowserHandler(unittest.TestCase):
    def test_browser_request_reaches_session(self) -> None:
        from ai.memory import Memory
        from ai.worker import AiWorker
        from ai.config import WorkerConfig

        import tempfile

        tmp = tempfile.mkdtemp(prefix="ai_browser_test_")
        cfg = WorkerConfig(state_dir=tmp)
        worker = AiWorker(cfg, memory=Memory(cfg.db_path))
        calls: list[dict] = []

        class FakeSession(BrowserSession):
            def handle(self, request):            # type: ignore[override]
                calls.append(request)
                return {"ok": True, "action": request.get("action", "open"),
                        "data": {"url": request.get("url", "")}, "ms": 1.0}

        worker.browser = FakeSession(headless=True)   # type: ignore[assignment]
        reply = worker.handle({"id": 3, "type": "browser", "action": "open",
                               "url": "https://example.com"})
        worker.close()
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["kind"], "browser")
        self.assertEqual(reply["data"]["url"], "https://example.com")
        self.assertEqual(calls[0]["action"], "open")

    def test_browser_error_becomes_honest_reply(self) -> None:
        from ai.memory import Memory
        from ai.worker import AiWorker
        from ai.config import WorkerConfig

        import tempfile

        tmp = tempfile.mkdtemp(prefix="ai_browser_test_")
        cfg = WorkerConfig(state_dir=tmp)
        worker = AiWorker(cfg, memory=Memory(cfg.db_path))

        class BrokenSession(BrowserSession):
            def handle(self, request):            # type: ignore[override]
                return {"ok": False, "action": "open", "error": "Chromium не запустился"}

        worker.browser = BrokenSession(headless=True)   # type: ignore[assignment]
        reply = worker.handle({"id": 4, "type": "browser", "action": "open",
                               "url": "https://example.com"})
        worker.close()
        self.assertFalse(reply["ok"])
        self.assertIn("Chromium", reply["error"])


if __name__ == "__main__":
    unittest.main()
