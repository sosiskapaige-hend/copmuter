"""Браузер как полноценная среда.

Преимущество над «чистой» GUI-имитацией: DOM-доступ — надёжнее и быстрее.
Агент получает: вкладки, навигацию, клик по селектору, ввод, извлечение
текста/таблиц, загрузки, PDF, скриншоты страницы, выполнение JS.

Бэкенд: Playwright (Chromium). Если не установлен — инструменты возвращают
понятное сообщение, и агент может открыть системный браузер (launch_app)
и работать через GUI-инструменты.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

from .base import Tool, ToolResult, ToolContext, Risk, _prop
from .registry import ToolRegistry


class _BrowserState:
    """Один browser + несколько страниц (вкладок)."""

    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx
        self._pw = None
        self._browser = None
        self._pages: dict[int, Any] = {}
        self._next_tab = 1
        self.downloads_dir = Path(ctx.cfg.downloads_dir)
        self.downloads_dir.mkdir(parents=True, exist_ok=True)

    async def _ensure(self):
        if self._browser is not None:
            return None
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return "playwright не установлен: pip install playwright && playwright install chromium"
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=not self.ctx.platform.has_display)
        return None

    async def close(self):
        try:
            if self._browser:
                await self._browser.close()
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        self._browser = None
        self._pages = {}

    def tab(self, i: int | None = None):
        if not self._pages:
            return None
        if i is None:
            return next(iter(self._pages.values()))
        return self._pages.get(i)


_states: dict[int, _BrowserState] = {}


def _state(ctx: ToolContext) -> _BrowserState:
    key = id(ctx)
    if key not in _states:
        _states[key] = _BrowserState(ctx)
    return _states[key]


def register_browser_tools(reg: ToolRegistry) -> None:

    @reg.tool("browser_open",
              "Открыть страницу в управляемом браузере (новая вкладка). Возвращает номер вкладки.",
              risk=Risk.NONE, category="browser",
              parameters={"type": "object", "properties": {
                  "url": _prop("string", "URL (напр. https://example.com)"),
                  "tab": _prop("integer", "Или открыть в существующей вкладке N")},
                  "required": ["url"]})
    class BrowserOpen(Tool):
        async def execute(self, ctx: ToolContext, url: str, tab: int | None = None) -> ToolResult:
            st = _state(ctx)
            err = await st._ensure()
            if err:
                return ToolResult.fail(err + " | Альтернатива: launch_app(chrome) + GUI-инструменты")
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            if tab and tab in st._pages:
                page = st._pages[tab]
                await page.goto(url, timeout=60000)
                return ToolResult.ok_result(f"Вкладка {tab} → {url}", tab=tab, title=await page.title())
            ctx0 = await st._browser.new_context(accept_downloads=True)
            page = await ctx0.new_page()
            await page.goto(url, timeout=60000)
            st._pages[st._next_tab] = page
            n = st._next_tab
            st._next_tab += 1
            return ToolResult.ok_result(f"Вкладка {n} открыта: {url} — «{await page.title()}»",
                                        tab=n, title=await page.title())

    @reg.tool("browser_navigate", "Перейти по URL в активной (или указанной) вкладке.",
              risk=Risk.NONE, category="browser",
              parameters={"type": "object", "properties": {
                  "url": _prop("string", "URL"), "tab": _prop("integer", "Вкладка (по умолчанию активная)")},
                  "required": ["url"]})
    class BrowserNavigate(Tool):
        async def execute(self, ctx: ToolContext, url: str, tab: int | None = None) -> ToolResult:
            st = _state(ctx)
            err = await st._ensure()
            if err:
                return ToolResult.fail(err)
            page = st.tab(tab)
            if page is None:
                return ToolResult.fail("нет вкладок — сначала browser_open")
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            await page.goto(url, timeout=60000)
            return ToolResult.ok_result(f"Переход: {url} — «{await page.title()}»")

    @reg.tool("browser_click",
              "Нажать элемент по CSS-селектору (надёжнее, чем координаты). "
              "Поддерживает текст-селектор: 'text=Войти', 'role=button[name=Login]'.",
              risk=Risk.NONE, category="browser",
              parameters={"type": "object", "properties": {
                  "selector": _prop("string", "CSS/text/role-селектор"),
                  "tab": _prop("integer", "Вкладка")}, "required": ["selector"]})
    class BrowserClick(Tool):
        async def execute(self, ctx: ToolContext, selector: str, tab: int | None = None) -> ToolResult:
            st = _state(ctx)
            page = st.tab(tab)
            if page is None:
                return ToolResult.fail("нет вкладок")
            try:
                await page.click(selector, timeout=15000)
                return ToolResult.ok_result(f"Нажато: {selector}")
            except Exception as e:
                return ToolResult.fail(f"элемент не найден/не кликабелен: {selector}. "
                                       f"Сначала browser_extract, чтобы увидеть структуру. Детали: {str(e)[:200]}")

    @reg.tool("browser_type", "Ввести текст в поле по селектору (input, textarea, [contenteditable]).",
              risk=Risk.NONE, category="browser",
              parameters={"type": "object", "properties": {
                  "selector": _prop("string", "CSS-селектор поля"),
                  "text": _prop("string", "Текст"),
                  "clear": _prop("boolean", "Очистить поле перед вводом (по умолчанию true)"),
                  "tab": _prop("integer", "Вкладка")}, "required": ["selector", "text"]})
    class BrowserType(Tool):
        async def execute(self, ctx: ToolContext, selector: str, text: str,
                          clear: bool = True, tab: int | None = None) -> ToolResult:
            st = _state(ctx)
            page = st.tab(tab)
            if page is None:
                return ToolResult.fail("нет вкладок")
            try:
                if clear:
                    await page.fill(selector, text, timeout=15000)
                else:
                    await page.press(selector, "End", timeout=15000)
                    await page.type(selector, text)
                return ToolResult.ok_result(f"Введено в {selector}: {len(text)} символов")
            except Exception as e:
                return ToolResult.fail(f"поле не найдено: {selector}. {str(e)[:200]}")

    @reg.tool("browser_extract",
              "Извлечь содержимое страницы: видимый текст, ссылки, формы, таблицы, "
              "или произвольный HTML через JS. Основной инструмент «чтения» веба.",
              risk=Risk.NONE, category="browser",
              parameters={"type": "object", "properties": {
                  "what": _prop("string", "text | links | forms | tables | html | title (по умолчанию text)"),
                  "selector": _prop("string", "Ограничить селектором (опционально)"),
                  "js": _prop("string", "Или JS-выражение, возвращающее JSON-совместимое значение"),
                  "tab": _prop("integer", "Вкладка")}, "required": []})
    class BrowserExtract(Tool):
        async def execute(self, ctx: ToolContext, what: str = "text", selector: str = "",
                          js: str = "", tab: int | None = None) -> ToolResult:
            st = _state(ctx)
            page = st.tab(tab)
            if page is None:
                return ToolResult.fail("нет вкладок")
            try:
                if js:
                    val = await page.evaluate(js)
                    return ToolResult.ok_result(str(val)[:8000])
                scope = page.locator(selector).first if selector else page
                if what == "title":
                    return ToolResult.ok_result(await page.title())
                if what == "links":
                    links = await page.eval_on_selector_all(
                        "a[href]", "els => els.slice(0, 100).map(a => a.href + ' | ' + (a.textContent||'').trim().slice(0,80))")
                    return ToolResult.ok_result("\n".join(links) or "(ссылок нет)")
                if what == "forms":
                    forms = await page.eval_on_selector_all(
                        "input, textarea, select, button",
                        "els => els.slice(0, 80).map(e => e.tagName + '#' + (e.id || '') + "
                        "'[name=' + (e.name || '') + '] type=' + (e.type || '') + ' ph=' + (e.placeholder || '')"
                        ").join('\\n')")
                    return ToolResult.ok_result(forms or "(форм нет)")
                if what == "tables":
                    tables = await page.eval_on_selector_all(
                        "table",
                        "ts => ts.slice(0,10).map(t => Array.from(t.querySelectorAll('tr'))"
                        ".slice(0,50).map(r => Array.from(r.querySelectorAll('td,th'))"
                        ".map(c => (c.textContent||'').trim()).join(' | ')).join('\\n')).join('\\n---\\n')")
                    return ToolResult.ok_result(tables or "(таблиц нет)")
                if what == "html":
                    html = await (scope if selector else page.locator("body")).inner_html()
                    return ToolResult.ok_result(html[:15000])
                text = await scope.inner_text()
                return ToolResult.ok_result(text[:15000])
            except Exception as e:
                return ToolResult.fail(f"извлечение не удалось: {str(e)[:200]}")

    @reg.tool("browser_tabs", "Список вкладок (номер, URL, заголовок).", risk=Risk.NONE,
              category="browser",
              parameters={"type": "object", "properties": {}})
    class BrowserTabs(Tool):
        async def execute(self, ctx: ToolContext) -> ToolResult:
            st = _state(ctx)
            if not st._pages:
                return ToolResult.ok_result("(вкладок нет)")
            lines = []
            for n, page in st._pages.items():
                lines.append(f"вкладка {n}: {page.url} — «{await page.title()}»")
            return ToolResult.ok_result("\n".join(lines))

    @reg.tool("browser_tab_close", "Закрыть вкладку.", risk=Risk.NONE, category="browser",
              parameters={"type": "object", "properties": {
                  "tab": _prop("integer", "Номер вкладки (по умолчанию активная)")}, "required": []})
    class BrowserTabClose(Tool):
        async def execute(self, ctx: ToolContext, tab: int | None = None) -> ToolResult:
            st = _state(ctx)
            page = st.tab(tab)
            if page is None:
                return ToolResult.fail("вкладка не найдена")
            n = tab or next(iter(st._pages))
            await page.close()
            st._pages.pop(n, None)
            return ToolResult.ok_result(f"Вкладка {n} закрыта")

    @reg.tool("browser_download",
              "Скачать файл по ссылке (или кнопке). Сохраняет в папку загрузок агента.",
              risk=Risk.MEDIUM, category="browser",
              parameters={"type": "object", "properties": {
                  "url": _prop("string", "Прямая ссылка на файл"),
                  "as": _prop("string", "Имя файла (опционально)")}, "required": ["url"]})
    class BrowserDownload(Tool):
        async def execute(self, ctx: ToolContext, url: str, as_name: str = "") -> ToolResult:
            st = _state(ctx)
            err = await st._ensure()
            if err:
                return ToolResult.fail(err)
            page = st.tab()
            try:
                if page is None:
                    ctx0 = await st._browser.new_context(accept_downloads=True)
                    page = await ctx0.new_page()
                async with page.expect_download(timeout=120000) as dl_info:
                    await page.goto(url, timeout=120000) if url else await page.click("body")
                dl = await dl_info.value
                name = as_name or dl.suggested_filename
                target = st.downloads_dir / name
                await dl.save_as(str(target))
                return ToolResult.ok_result(f"Скачано: {target} ({target.stat().st_size} Б)", path=str(target),
                                            undo={"tool": "fs_delete", "args": {"path": str(target), "permanent": True},
                                                  "note": f"удалить {name}"})
            except Exception as e:
                return ToolResult.fail(f"скачивание не удалось: {str(e)[:200]}")

    @reg.tool("browser_upload",
              "Загрузить файл на страницу в input[type=file].",
              risk=Risk.MEDIUM, category="browser",
              parameters={"type": "object", "properties": {
                  "selector": _prop("string", "Селектор input[type=file]"),
                  "file": _prop("string", "Путь к файлу")}, "required": ["selector", "file"]})
    class BrowserUpload(Tool):
        async def execute(self, ctx: ToolContext, selector: str, file: str) -> ToolResult:
            st = _state(ctx)
            page = st.tab()
            if page is None:
                return ToolResult.fail("нет вкладок")
            p = os.path.expanduser(file)
            if not os.path.exists(p):
                return ToolResult.fail(f"файл не найден: {p}")
            try:
                await page.set_input_files(selector, p, timeout=15000)
                return ToolResult.ok_result(f"Загружен файл: {os.path.basename(p)}")
            except Exception as e:
                return ToolResult.fail(f"загрузка не удалась: {str(e)[:200]}")

    @reg.tool("browser_wait",
              "Дождаться элемента/текста/времени (динамические страницы, SPA).",
              risk=Risk.NONE, category="browser",
              parameters={"type": "object", "properties": {
                  "selector": _prop("string", "Дождаться появления селектора"),
                  "text": _prop("string", "Или появления текста"),
                  "seconds": _prop("number", "Или просто подождать N секунд"),
                  "timeout": _prop("number", "Таймаут сек (по умолчанию 20)")}, "required": []})
    class BrowserWait(Tool):
        async def execute(self, ctx: ToolContext, selector: str = "", text: str = "",
                          seconds: float | None = None, timeout: float = 20) -> ToolResult:
            st = _state(ctx)
            page = st.tab()
            if page is None:
                return ToolResult.fail("нет вкладок")
            try:
                if seconds:
                    await asyncio.sleep(min(seconds, 60))
                    return ToolResult.ok_result(f"Подождено {seconds}с")
                if selector:
                    await page.wait_for_selector(selector, timeout=timeout * 1000)
                    return ToolResult.ok_result(f"Элемент появился: {selector}")
                if text:
                    await page.wait_for_function(
                        f"document.body.innerText.includes({json.dumps(text)})", timeout=timeout * 1000)
                    return ToolResult.ok_result(f"Текст появился: {text}")
                return ToolResult.fail("укажите selector, text или seconds")
            except Exception as e:
                return ToolResult.fail(f"ожидание не завершено: {str(e)[:150]}")

    @reg.tool("browser_screenshot", "Скриншот текущей вкладки (PNG).",
              risk=Risk.NONE, category="browser",
              parameters={"type": "object", "properties": {
                  "tab": _prop("integer", "Вкладка"), "full_page": _prop("boolean", "Вся страница целиком")},
                  "required": []})
    class BrowserScreenshot(Tool):
        async def execute(self, ctx: ToolContext, tab: int | None = None,
                          full_page: bool = False) -> ToolResult:
            st = _state(ctx)
            page = st.tab(tab)
            if page is None:
                return ToolResult.fail("нет вкладок")
            out = Path(ctx.cfg.state_dir / "screens") / f"browser_{int(time.time()*1000)}.png"
            out.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(out), full_page=full_page)
            ctx.set_screenshot_cache(str(out))
            ctx.bus.emit("screen", path=str(out), monitor="browser")
            return ToolResult.ok_result(f"Скриншот вкладки: {out}", path=str(out))

    @reg.tool("browser_pdf", "Сохранить страницу в PDF.", risk=Risk.LOW, category="browser",
              parameters={"type": "object", "properties": {
                  "out": _prop("string", "Путь к .pdf (по умолчанию в downloads)")}, "required": []})
    class BrowserPdf(Tool):
        async def execute(self, ctx: ToolContext, out: str = "") -> ToolResult:
            st = _state(ctx)
            page = st.tab()
            if page is None:
                return ToolResult.fail("нет вкладок")
            target = os.path.expanduser(out) if out else str(st.downloads_dir / "page.pdf")
            await page.pdf(path=target)
            return ToolResult.ok_result(f"PDF сохранён: {target}", path=target)

    @reg.tool("browser_close", "Закрыть управляемый браузер (все вкладки).",
              risk=Risk.LOW, category="browser",
              parameters={"type": "object", "properties": {}})
    class BrowserClose(Tool):
        async def execute(self, ctx: ToolContext) -> ToolResult:
            st = _state(ctx)
            await st.close()
            _states.pop(id(ctx), None)
            return ToolResult.ok_result("Браузер закрыт")
