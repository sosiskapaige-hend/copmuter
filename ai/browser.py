"""Браузерный путь: Playwright + Chromium для сложных страниц.

Когда хватает прямой ссылки (поиск, YouTube, открытие сайта) — ядро открывает URL
нативно и этот модуль не участвует вообще. Playwright нужен там, где страница
требует взаимодействия: заполнить форму, дождаться SPA, вытащить данные, скачать файл.

Браузер поднимается один раз за сессию и переиспользуется: запуск Chromium на каждое
действие — это секунды, которых в бюджете нет.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

log = logging.getLogger("ai.browser")

DEFAULT_TIMEOUT_MS = 15000


class BrowserError(RuntimeError):
    """Понятная человеку причина: что именно не так с браузером."""


def _load_playwright():
    """Playwright импортируется лениво: без него агент работает, просто без этого пути."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - зависит от окружения
        raise BrowserError(
            "Playwright не установлен: нужен «python -m pip install playwright» "
            "и «python -m playwright install chromium»"
        ) from exc
    return sync_playwright


class BrowserSession:
    """Постоянный Chromium + действия над страницей. Один экземпляр на процесс."""

    def __init__(self, *, headless: bool | None = None, timeout_ms: int = DEFAULT_TIMEOUT_MS,
                 channel: str = "") -> None:
        self.headless = (os.environ.get("AGENT_BROWSER_HEADLESS", "1") != "0"
                         if headless is None else headless)
        self.timeout_ms = timeout_ms
        self.channel = channel or os.environ.get("AGENT_BROWSER_CHANNEL", "")
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self.actions = 0
        self.errors = 0
        self.started_ms = 0.0
        self.last_url = ""

    # ------------------------------------------------------------------ жизненный цикл
    @property
    def running(self) -> bool:
        return self._browser is not None and self._page is not None

    def start(self) -> None:
        if self.running:
            return
        started = time.perf_counter()
        sync_playwright = _load_playwright()
        try:
            self._playwright = sync_playwright().start()
            kwargs: dict[str, Any] = {"headless": self.headless}
            if self.channel:
                kwargs["channel"] = self.channel
            self._browser = self._playwright.chromium.launch(**kwargs)
            self._context = self._browser.new_context(
                locale=os.environ.get("AGENT_BROWSER_LOCALE", "ru-RU"),
                viewport={"width": 1440, "height": 900},
                ignore_https_errors=True,
            )
            self._page = self._context.new_page()
            self._page.set_default_timeout(self.timeout_ms)
        except Exception as exc:                       # noqa: BLE001
            self.close()
            raise BrowserError(f"Chromium не запустился: {exc}\n"
                               "Проверьте «python -m playwright install chromium»") from exc
        self.started_ms = (time.perf_counter() - started) * 1000.0
        log.info("браузер поднят за %.0f мс", self.started_ms)

    def close(self) -> None:
        for closer in (lambda: self._context and self._context.close(),
                       lambda: self._browser and self._browser.close(),
                       lambda: self._playwright and self._playwright.stop()):
            try:
                closer()
            except Exception:                          # noqa: BLE001
                pass
        self._page = self._context = self._browser = self._playwright = None

    # ------------------------------------------------------------------ селекторы
    def _locator(self, selector: str):
        """CSS, «text=…», «role=button[name=…]», обычная строка — всё превращаем в локатор."""
        page = self._page
        if selector.startswith("role="):
            body = selector[len("role=") :]
            name = ""
            role = body
            if "[" in body and body.endswith("]"):
                role, _, rest = body.partition("[")
                if rest.startswith("name="):
                    name = rest[len("name=") : -1]
            return page.get_by_role(role.strip(), name=name.strip()) if name else page.get_by_role(role.strip())
        if selector.startswith("text="):
            return page.get_by_text(selector[len("text=") :])
        if selector.startswith("//") or selector.startswith("xpath="):
            return page.locator(selector if selector.startswith("xpath=") else f"xpath={selector}")
        return page.locator(selector)

    # ------------------------------------------------------------------ действия
    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        """Один батч браузерных действий: {"action":…, …} → результат для мозга."""
        action = str(request.get("action") or "info").lower()
        started = time.perf_counter()
        try:
            if action in ("info", "state"):
                return self._result(action, {"running": self.running, "url": self.last_url,
                                             "started_ms": round(self.started_ms, 1),
                                             "actions": self.actions}, started)
            self.start()
            handler = getattr(self, f"_do_{action}", None)
            if handler is None:
                raise BrowserError(f"неизвестное действие браузера: {action}")
            data = handler(request) or {}
            self.actions += 1
            self.last_url = self._page.url if self._page else self.last_url
            return self._result(action, data, started)
        except BrowserError as exc:
            self.errors += 1
            return {"ok": False, "action": action, "error": str(exc),
                    "ms": (time.perf_counter() - started) * 1000.0}
        except Exception as exc:                       # noqa: BLE001
            self.errors += 1
            return {"ok": False, "action": action,
                    "error": f"браузер: {type(exc).__name__}: {exc}",
                    "ms": (time.perf_counter() - started) * 1000.0}

    def _result(self, action: str, data: dict[str, Any], started: float) -> dict[str, Any]:
        return {"ok": True, "action": action, "data": data,
                "ms": (time.perf_counter() - started) * 1000.0}

    def _do_open(self, request: dict[str, Any]) -> dict[str, Any]:
        url = str(request.get("url") or "").strip()
        if not url:
            raise BrowserError("не передан адрес страницы")
        if "://" not in url:
            url = "https://" + url
        response = self._page.goto(url, wait_until=request.get("wait_until", "domcontentloaded"),
                                   timeout=int(request.get("timeout_ms") or self.timeout_ms))
        return {"url": self._page.url, "title": self._page.title(),
                "status": response.status if response else 0}

    def _do_click(self, request: dict[str, Any]) -> dict[str, Any]:
        selector = str(request.get("selector") or request.get("text") or "")
        if not selector:
            raise BrowserError("не передан элемент для клика")
        locator = self._locator(selector)
        count = locator.count()
        if count == 0:
            raise BrowserError(f"элемент не найден: {selector}")
        target = locator.first if count > 1 else locator
        target.click(timeout=int(request.get("timeout_ms") or self.timeout_ms))
        if request.get("wait_after"):
            self._page.wait_for_timeout(int(request["wait_after"]))
        return {"clicked": selector, "matches": count, "url": self._page.url}

    def _do_fill(self, request: dict[str, Any]) -> dict[str, Any]:
        selector = str(request.get("selector") or "")
        text = str(request.get("text") or "")
        if not selector:
            raise BrowserError("не передан селектор поля")
        field = self._locator(selector).first
        field.fill(text, timeout=int(request.get("timeout_ms") or self.timeout_ms))
        if request.get("enter"):
            field.press("Enter")
            self._page.wait_for_load_state("domcontentloaded")
        return {"filled": selector, "chars": len(text), "url": self._page.url}

    def _do_press(self, request: dict[str, Any]) -> dict[str, Any]:
        keys = str(request.get("keys") or request.get("key") or "")
        if not keys:
            raise BrowserError("не переданы клавиши")
        selector = str(request.get("selector") or "")
        if selector:
            self._locator(selector).first.press(keys)
        else:
            self._page.keyboard.press(keys)
        return {"pressed": keys, "url": self._page.url}

    def _do_text(self, request: dict[str, Any]) -> dict[str, Any]:
        selector = str(request.get("selector") or "body")
        locator = self._locator(selector).first
        text = locator.inner_text(timeout=int(request.get("timeout_ms") or self.timeout_ms))
        limit = int(request.get("limit") or 4000)
        return {"text": text[:limit], "chars": len(text), "url": self._page.url,
                "truncated": len(text) > limit}

    def _do_html(self, request: dict[str, Any]) -> dict[str, Any]:
        limit = int(request.get("limit") or 20000)
        html = self._page.content()
        return {"html": html[:limit], "chars": len(html), "url": self._page.url}

    def _do_links(self, request: dict[str, Any]) -> dict[str, Any]:
        limit = int(request.get("limit") or 50)
        pattern = str(request.get("contains") or "")
        links: list[dict[str, str]] = []
        for anchor in self._page.locator("a[href]").all()[: limit * 4]:
            try:
                href = anchor.get_attribute("href") or ""
                label = (anchor.inner_text() or "").strip()
            except Exception:                          # noqa: BLE001
                continue
            if not href or (pattern and pattern.lower() not in (href + label).lower()):
                continue
            links.append({"text": label[:120], "href": href})
            if len(links) >= limit:
                break
        return {"links": links, "count": len(links), "url": self._page.url}

    def _do_eval(self, request: dict[str, Any]) -> dict[str, Any]:
        script = str(request.get("script") or request.get("js") or "")
        if not script:
            raise BrowserError("не передан JS-скрипт")
        value = self._page.evaluate(script)
        try:
            json.dumps(value)
        except (TypeError, ValueError):
            value = str(value)
        return {"value": value, "url": self._page.url}

    def _do_wait(self, request: dict[str, Any]) -> dict[str, Any]:
        """Ожидание состояния страницы, а не sleep: селектор, текст или загрузка."""
        timeout = int(request.get("timeout_ms") or self.timeout_ms)
        if request.get("selector"):
            self._locator(str(request["selector"])).first.wait_for(state="visible", timeout=timeout)
            return {"waited": f"элемент {request['selector']}", "url": self._page.url}
        if request.get("url_contains"):
            self._page.wait_for_url(f"**{request['url_contains']}**", timeout=timeout)
            return {"waited": f"адрес содержит {request['url_contains']}", "url": self._page.url}
        self._page.wait_for_load_state(str(request.get("state") or "networkidle"), timeout=timeout)
        return {"waited": "загрузка страницы", "url": self._page.url}

    def _do_screenshot(self, request: dict[str, Any]) -> dict[str, Any]:
        path = str(request.get("path") or "")
        full_page = bool(request.get("full_page"))
        png = self._page.screenshot(path=path or None, full_page=full_page)
        return {"bytes": len(png) if png else 0, "path": path, "url": self._page.url}

    def _do_back(self, request: dict[str, Any]) -> dict[str, Any]:
        self._page.go_back(wait_until="domcontentloaded")
        return {"url": self._page.url}

    def _do_download(self, request: dict[str, Any]) -> dict[str, Any]:
        """Скачивание: ждём событие download, сохраняем в указанный каталог."""
        folder = str(request.get("folder") or ".")
        selector = str(request.get("selector") or "")
        with self._page.expect_download(timeout=int(request.get("timeout_ms") or self.timeout_ms)) as info:
            if selector:
                self._locator(selector).first.click()
            else:
                self._page.mouse.click(int(request.get("x") or 0), int(request.get("y") or 0))
        download = info.value
        target = os.path.join(folder, download.suggested_filename or "download")
        download.save_as(target)
        return {"path": target, "name": download.suggested_filename, "url": self._page.url}

    # ------------------------------------------------------------------ пакет
    def run_batch(self, steps: list[dict[str, Any]]) -> dict[str, Any]:
        """Несколько шагов одним заходом: модель не платит задержкой за каждый клик."""
        results: list[dict[str, Any]] = []
        for step in steps:
            result = self.handle(step)
            results.append(result)
            if not result.get("ok"):
                break
        return {"ok": all(r.get("ok") for r in results), "results": results,
                "steps": len(results)}
