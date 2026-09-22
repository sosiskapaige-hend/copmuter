"""Быстрый браузерный слой (ТЗ §18).

Главное отличие от «водить мышкой по адресной строке»: агент формирует ПРЯМУЮ
ссылку на результаты поиска и открывает её.

    «найди видео про котиков» → https://www.youtube.com/results?search_query=котики
    «поищи 123»               → https://www.google.com/search?q=123
    «открой youtube»          → https://www.youtube.com

Это один вызов `open_url` без единого клика, ввода и обращения к модели.
Playwright/CDP подключается только тогда, когда нужен DOM (взять список
результатов, прочитать страницу, кликнуть по ссылке) — и только если установлен.
"""
from __future__ import annotations

import html
import os
import re
import shutil
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path

from .base import Risk, Tool, ToolContext, ToolResult

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

ENGINES = {
    "google": "https://www.google.com/search?q={q}",
    "yandex": "https://yandex.ru/search/?text={q}",
    "bing": "https://www.bing.com/search?q={q}",
    "duckduckgo": "https://duckduckgo.com/?q={q}",
    "youtube": "https://www.youtube.com/results?search_query={q}",
}

SHORTCUTS = {
    "youtube": "https://www.youtube.com", "ютуб": "https://www.youtube.com",
    "ютюб": "https://www.youtube.com", "google": "https://www.google.com",
    "гугл": "https://www.google.com", "yandex": "https://yandex.ru",
    "яндекс": "https://yandex.ru", "github": "https://github.com",
    "гитхаб": "https://github.com", "vk": "https://vk.com", "вк": "https://vk.com",
    "wikipedia": "https://ru.wikipedia.org", "википедия": "https://ru.wikipedia.org",
    "mail": "https://mail.google.com", "почта": "https://mail.google.com",
    "twitch": "https://www.twitch.tv", "reddit": "https://www.reddit.com",
    "steam": "https://store.steampowered.com", "avito": "https://www.avito.ru",
    "авито": "https://www.avito.ru", "ozon": "https://www.ozon.ru",
    "озон": "https://www.ozon.ru", "kinopoisk": "https://www.kinopoisk.ru",
    "кинопоиск": "https://www.kinopoisk.ru",
}


def _prop(t: str, desc: str) -> dict:
    return {"type": t, "description": desc}


def search_url(query: str, engine: str = "google") -> str:
    """Прямая ссылка на результаты поиска."""
    q = urllib.parse.quote_plus((query or "").strip())
    tpl = ENGINES.get((engine or "google").lower(), ENGINES["google"])
    return tpl.format(q=q)


def normalize_target(text: str) -> str:
    """«ютуб» → https://www.youtube.com, «youtube.com» → https://youtube.com."""
    t = (text or "").strip().strip("«»\"'")
    if not t:
        return ""
    low = t.lower()
    if low in SHORTCUTS:
        return SHORTCUTS[low]
    for key, url in SHORTCUTS.items():
        if low.startswith(key + "."):
            rest = t[len(key):]
            return f"https://{key}{rest}" if not rest.startswith(".") else f"https://{key}{rest}"
    if t.startswith(("http://", "https://", "file://")):
        return t
    if re.match(r"^[\w\-]+\.[a-zA-Zа-я]{2,}(/\S*)?$", t):
        return "https://" + t
    return t


async def _open_direct(ctx: ToolContext, url: str) -> ToolResult:
    """Открыть ссылку системно (shell → браузер), без адресной строки."""
    launcher = ctx.service("launcher")
    if launcher is not None:
        try:
            res = await launcher.open_url(ctx, url)
            if getattr(res, "ok", False):
                return ToolResult.ok_result(f"Открыл: {url}", url=url, method=res.method,
                                            pid=getattr(res, "pid", 0))
        except Exception:
            pass
    reg_tools = ctx.service("registry")
    if reg_tools is not None:
        r = await reg_tools.call("open_url", {"url": url}, ctx)
        if r.ok:
            return ToolResult.ok_result(r.output or f"Открыл: {url}", url=url, method="open_url")
    try:
        if os.name == "nt":
            os.startfile(url)                              # type: ignore[attr-defined]
        elif shutil.which("xdg-open"):
            subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        elif shutil.which("open"):
            subprocess.Popen(["open", url], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        else:
            import webbrowser
            if not webbrowser.open(url):
                return ToolResult.fail("не нашёл браузер для открытия ссылки")
        return ToolResult.ok_result(f"Открыл: {url}", url=url, method="native_shell")
    except Exception as exc:                               # noqa: BLE001
        return ToolResult.fail(f"не удалось открыть ссылку: {exc}")


def _fetch(url: str, timeout: float = 12.0, limit: int = 200_000) -> tuple[bool, str, str]:
    """Скачать страницу без браузера (быстро, без Playwright)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                  "Accept-Language": "ru,en;q=0.8"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(limit)
            charset = resp.headers.get_content_charset() or "utf-8"
            return True, raw.decode(charset, "replace"), ""
    except Exception as exc:                               # noqa: BLE001
        return False, "", f"{type(exc).__name__}: {exc}"


def html_to_text(markup: str, limit: int = 6000) -> str:
    body = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", markup or "")
    body = re.sub(r"(?is)<br\s*/?>|</p>|</div>|</li>|</h[1-6]>", "\n", body)
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    body = html.unescape(body)
    body = re.sub(r"[ \t\u00a0]+", " ", body)
    body = re.sub(r"\n\s*\n\s*\n+", "\n\n", body)
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    return "\n".join(lines)[:limit]


def register_browser_agent_tools(reg) -> None:
    @reg.tool("open_browser",
              "Открыть браузер по умолчанию (без адресной строки). Можно сразу с "
              "сайтом: open_browser(target='youtube.com').",
              risk=Risk.LOW, category="browser",
              parameters={"type": "object", "properties": {
                  "target": _prop("string", "Сайт или ссылка (необязательно)"),
                  "browser": _prop("string", "Конкретный браузер (необязательно)")},
                  "required": []})
    class OpenBrowser(Tool):
        async def execute(self, ctx: ToolContext, target: str = "", browser: str = "") -> ToolResult:
            if target:
                url = normalize_target(target)
                res = await _open_direct(ctx, url)
                if res.ok:
                    return res
            launcher = ctx.service("launcher")
            if launcher is not None:
                res = await launcher.open_url(ctx, "about:blank" if os.name != "nt" else "")
                if getattr(res, "ok", False):
                    return ToolResult.ok_result(f"Открыл браузер ({res.method}).", method=res.method)
            reg_tools = ctx.service("registry")
            if reg_tools is not None:
                r = await reg_tools.call("open_url", {"url": "", "browser": browser}, ctx)
                if r.ok:
                    return ToolResult.ok_result(r.output, method="open_url")
            return ToolResult.fail("не удалось открыть браузер")

    @reg.tool("search_google",
              "Найти в интернете через прямую ссылку поиска (без ввода в адресную "
              "строку). С extract=true вернёт первые результаты текстом.",
              risk=Risk.NONE, category="browser",
              parameters={"type": "object", "properties": {
                  "query": _prop("string", "Поисковый запрос"),
                  "engine": _prop("string", "google | yandex | bing | duckduckgo"),
                  "open": _prop("boolean", "Открыть результаты в браузере (по умолчанию да)"),
                  "extract": _prop("boolean", "Вернуть список результатов")},
                  "required": ["query"]})
    class SearchGoogle(Tool):
        async def execute(self, ctx: ToolContext, query: str, engine: str = "google",
                          open: bool = True, extract: bool = False) -> ToolResult:
            if not (query or "").strip():
                return ToolResult.fail("пустой запрос")
            url = search_url(query, engine)
            out_lines = [f"Ищу «{query}» — {url}"]
            data: dict = {"url": url, "query": query, "engine": engine, "method": "direct_url"}
            if open:
                res = await _open_direct(ctx, url)
                out_lines.append(res.output if res.ok else f"браузер: {res.error}")
                if not res.ok:
                    return ToolResult(ok=False, output="\n".join(out_lines), error=res.error, data=data)
            if extract:
                results = _extract_results(url)
                data["results"] = results
                if results:
                    out_lines.append("Результаты:")
                    out_lines += [f"{i}. {r['title']} — {r['url']}" for i, r in enumerate(results[:8], 1)]
                else:
                    out_lines.append("(не удалось получить список результатов без браузера)")
            return ToolResult.ok_result("\n".join(out_lines), **data)

    @reg.tool("search_youtube",
              "Найти видео на YouTube через прямую ссылку results?search_query=…",
              risk=Risk.NONE, category="browser",
              parameters={"type": "object", "properties": {
                  "query": _prop("string", "Что искать"),
                  "open": _prop("boolean", "Открыть в браузере (по умолчанию да)"),
                  "extract": _prop("boolean", "Вернуть список видео")},
                  "required": ["query"]})
    class SearchYouTube(Tool):
        async def execute(self, ctx: ToolContext, query: str, open: bool = True,
                          extract: bool = False) -> ToolResult:
            if not (query or "").strip():
                return ToolResult.fail("пустой запрос")
            url = search_url(query, "youtube")
            lines = [f"Открываю YouTube: {url}"]
            data: dict = {"url": url, "query": query, "engine": "youtube", "method": "direct_url"}
            if open:
                res = await _open_direct(ctx, url)
                if not res.ok:
                    return ToolResult(ok=False, output="\n".join(lines + [res.error]),
                                      error=res.error, data=data)
                lines.append(f"Открыл ({res.method}).")
            if extract:
                items = _extract_youtube(url)
                data["videos"] = items
                if items:
                    lines.append("Видео:")
                    lines += [f"{i}. {v}" for i, v in enumerate(items[:8], 1)]
            return ToolResult.ok_result("\n".join(lines), **data)

    @reg.tool("open_search_result",
              "Открыть результат из уже открытого поиска: по номеру (1..N) или по "
              "тексту ссылки. Работает через DOM, если доступен Playwright.",
              risk=Risk.LOW, category="browser",
              parameters={"type": "object", "properties": {
                  "index": _prop("integer", "Номер результата (с 1)"),
                  "title": _prop("string", "Часть текста ссылки/заголовка"),
                  "url": _prop("string", "Если известна ссылка — открыть сразу")},
                  "required": []})
    class OpenSearchResult(Tool):
        async def execute(self, ctx: ToolContext, index: int = 0, title: str = "",
                          url: str = "") -> ToolResult:
            if url:
                return await _open_direct(ctx, normalize_target(url))
            reg_tools = ctx.service("registry")
            if reg_tools is None:
                return ToolResult.fail("нужен доступ к инструментам браузера")
            picked = ""
            if title:
                r = await reg_tools.call("browser_extract", {"selector": "a", "limit": 60}, ctx)
                if r.ok and isinstance(r.data, dict):
                    items = r.data.get("items") or r.data.get("links") or []
                    for it in items:
                        text = str(it.get("text") or it.get("title") or "")
                        href = str(it.get("href") or it.get("url") or "")
                        if title.lower() in text.lower() and href.startswith("http"):
                            picked = href
                            break
            elif index:
                r = await reg_tools.call("browser_extract", {"selector": "a", "limit": 60}, ctx)
                if r.ok and isinstance(r.data, dict):
                    links = [str(it.get("href") or it.get("url") or "")
                             for it in (r.data.get("items") or r.data.get("links") or [])]
                    links = [l for l in links if l.startswith("http")]
                    if 1 <= index <= len(links):
                        picked = links[index - 1]
            if not picked:
                return ToolResult.fail("не нашёл подходящий результат "
                                       "(возможно, браузер не управляется Playwright)")
            return await _open_direct(ctx, picked)

    @reg.tool("read_page",
              "Прочитать страницу текстом. Без браузера — быстрая загрузка и очистка "
              "HTML; с браузером — рендер JS.",
              risk=Risk.NONE, category="browser",
              parameters={"type": "object", "properties": {
                  "url": _prop("string", "Ссылка (обязательно для статического чтения)"),
                  "use_browser": _prop("boolean", "Использовать открытый браузер (DOM)"),
                  "limit": _prop("integer", "Максимум символов")},
                  "required": []})
    class ReadPage(Tool):
        async def execute(self, ctx: ToolContext, url: str = "", use_browser: bool = False,
                          limit: int = 6000) -> ToolResult:
            if use_browser:
                reg_tools = ctx.service("registry")
                if reg_tools is not None:
                    r = await reg_tools.call("browser_extract", {"selector": "body", "limit": 5}, ctx)
                    if r.ok:
                        return ToolResult.ok_result(r.output[:limit], method="browser_dom")
            if not url:
                page = ctx.service("browser_url")
                url = str(page or "")
            if not url:
                return ToolResult.fail("нужен url (или включите use_browser)")
            ok, markup, err = _fetch(normalize_target(url))
            if not ok:
                return ToolResult.fail(f"не удалось загрузить страницу: {err}")
            text = html_to_text(markup, limit)
            return ToolResult.ok_result(f"Страница {url}:\n{text}",
                                        url=url, chars=len(text), method="urllib")

    @reg.tool("browser_scroll",
              "Прокрутить активную страницу (down/up/top/bottom или на N экранов).",
              risk=Risk.NONE, category="browser", is_gui=True,
              parameters={"type": "object", "properties": {
                  "direction": _prop("string", "down | up | top | bottom"),
                  "amount": _prop("integer", "Сколько экранов прокрутить")},
                  "required": []})
    class BrowserScroll(Tool):
        async def execute(self, ctx: ToolContext, direction: str = "down",
                          amount: int = 3) -> ToolResult:
            d = (direction or "down").lower()
            steps = max(1, min(int(amount or 3), 30))
            keys = {"down": "pagedown", "up": "pageup", "top": "ctrl+home",
                    "bottom": "ctrl+end"}.get(d, "pagedown")
            inputs = ctx.service("inputs")
            if inputs is None:
                reg_tools = ctx.service("registry")
                if reg_tools is None:
                    return ToolResult.fail("нет подсистемы ввода")
                r = await reg_tools.call("keyboard_hotkey", {"keys": keys}, ctx)
                return ToolResult.ok_result(r.output if r.ok else "прокрутил", method="keyboard_hotkey") \
                    if r.ok else ToolResult.fail(r.error)
            for _ in range(steps):
                await inputs.press_key(keys.split("+")[-1] if "+" not in keys else keys)
            return ToolResult.ok_result(f"Прокрутил {d} ×{steps}.", method="sendinput")


def _extract_results(url: str, limit: int = 8) -> list[dict]:
    """Простые результаты поиска из HTML (без браузера)."""
    ok, markup, _err = _fetch(url)
    if not ok:
        return []
    out: list[dict] = []
    for m in re.finditer(r'<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>', markup, re.S):
        href, inner = m.group(1), html_to_text(m.group(2), 200).strip()
        if not inner or "google." in href or "youtube.com/results" in href:
            continue
        if any(href.startswith(p) for p in ("https://www.google.", "https://yandex.",
                                            "https://duckduckgo.")):
            continue
        if any(r["url"] == href for r in out):
            continue
        out.append({"title": inner[:140], "url": href})
        if len(out) >= limit:
            break
    return out


def _extract_youtube(url: str, limit: int = 8) -> list[str]:
    ok, markup, _err = _fetch(url)
    if not ok:
        return []
    titles = re.findall(r'"title":\{"runs":\[\{"text":"(.*?)"\}\]', markup)
    if not titles:
        titles = re.findall(r'<a[^>]+id="video-title"[^>]*title="([^"]+)"', markup)
    seen: list[str] = []
    for t in titles:
        t = html.unescape(t).strip()
        if t and t not in seen and t.lower() != "youtube":
            seen.append(t)
        if len(seen) >= limit:
            break
    return seen


__all__ = ["register_browser_agent_tools", "search_url", "normalize_target",
           "html_to_text", "ENGINES"]
