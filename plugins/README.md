# Плагины агента (Plugin/Tool SDK)

Инструменты можно добавлять, **не меняя ядро**: положите `.py`-файл в одну из папок

1. `AGENT_HOME/plugins/` — по умолчанию `~/.ai-computer-agent/plugins` (для пользователя);
2. `<рабочая папка>/plugins/` — эта папка (для проекта);
3. путь из переменной окружения `AGENT_PLUGINS` (несколько — через `:` / `;`).

Файлы, начинающиеся с `_`/`.` или оканчивающиеся на `.disabled`, не загружаются —
так можно держать примеры рядом с рабочими плагинами.

## Как выглядит плагин

```python
from agent.tools.base import Risk, Tool, ToolResult, ToolContext


class WeatherTool(Tool):
    name = "weather"                     # имя, которым модель вызывает инструмент
    description = "Погода в городе"
    risk = Risk.NONE                     # NONE | LOW | MEDIUM | HIGH | CRITICAL
    category = "misc"                    # category влияет на отбор схем в контекст
    parameters = {"type": "object",
                  "properties": {"city": {"type": "string", "description": "Город"}},
                  "required": ["city"]}

    async def execute(self, ctx: ToolContext, city: str) -> ToolResult:
        return ToolResult.ok_result(f"В городе {city} солнечно")


TOOLS = [WeatherTool()]                  # либо def register(reg): reg.register(WeatherTool())
```

## Что доступно внутри инструмента

* `ctx.cfg` — конфигурация; `ctx.bus` — события (их видно в интерфейсе);
  `ctx.gateway` — запросы подтверждения; `ctx.workdir` — рабочая папка;
* подсистемы нового слоя — через `ctx.service("имя")`:
  `apps` (реестр приложений), `launcher` (цепочка запуска), `state` (состояние ПК),
  `wait` (менеджер ожиданий), `inputs` (ввод, AGENT_INPUT_LOCK), `screens` (снимки экрана),
  `vision` (распознавание интерфейса), `optimizer` (выбор способа), `metrics`, `log`,
  `intent` (движок намерений), `router` (быстрый маршрутизатор), `fast` (слой целиком).
* если подсистемы нет (например, в тестах), `ctx.service(...)` вернёт `None` —
  инструмент должен честно сказать «недоступно», а не падать.

## Проверка

Плагины подключаются при старте агента (`build_registry()`), ошибки попадают в лог
(`trace.jsonl`) и в сводку: один сломанный плагин не мешает остальным инструментам.

```python
from agent.tools import build_registry
from agent.plugins import load_plugins

reg = build_registry()
print(load_plugins(reg, dirs=[pathlib.Path("plugins")]))
```
