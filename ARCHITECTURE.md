# Архитектура AI Computer Agent

## Ключевые решения

### 1. Ядро на stdlib — «железо» опционально

Весь агентный цикл (план → действие → наблюдение → проверка), LLM-клиент,
система событий, очередь, расписание, триггеры, Web UI работают **без единой
внешней зависимости** (Python ≥ 3.10). Железные бэкенды (mss, pyautogui,
playwright, psutil, tesseract) подключаются через `platform.detect()`: если
библиотека/дисплей недоступны, инструмент возвращает **наблюдение**
(«headless: используйте прямые средства») — и агент сам выбирает другой путь.
Так система одинаково живёт на вашем ПК, на сервере и в CI, а деградация —
плавная, без падений.

### 2. LLM — только 6 методов, любой провайдер

```python
plan(goal, context, tools)        → Plan
next_action(goal, ctx, history, tools_schema) → Decision(tool_call|final|ask)
assess(goal, done, last_error)    → Assessment(progress, achieved, remaining)
answer(messages)                  → str
describe_image(b64, prompt)       → str      (vision)
```

- `OpenAICompatibleLLM` — stdlib `urllib`, работает с любым
  `/v1/chat/completions` (OpenAI, Groq, OpenRouter, DeepSeek, Ollama, LM
  Studio). Поддержка: function calling, vision (base64), JSON-режим; для
  моделей без tool calling — JSON-протокол в тексте.
- `MockLLM` — детерминированные сценарии (regex по цели) для офлайн-демо и
  тестов: реально вызывает инструменты через тот же цикл.

### 3. Инструменты = единое пространство

```python
class Tool:  # name, description, JSON-schema, risk, is_gui
    async def execute(ctx, **args) -> ToolResult   # ok/output/data/error
    def estimate_risk(ctx, args) -> (Risk, reason) # динамическая оценка
```

`ToolResult.data["undo"] = {tool, args, note}` — инструмент сам знает, как
отменить себя. Реестр сериализует GUI-инструменты через общий lock (мышь
одна!), несерийные — параллельно. Schema инструментов автоматически
формируется для function calling и «простой список» для промптов.

**Расширение** = новый класс Tool + `@reg.tool(...)` + строка в
`build_registry()`. Ничего больше менять не нужно: LLM увидит инструмент в
схеме, safety оценит риск, journal зафиксирует undo, UI покажет вызов.

### 4. Агентный цикл (`agent/agent/core.py`)

```
run_task(goal, mode):
  1. plan = llm.plan(...)                     [T_PLAN в UI]
     если план.asks_user → ask_user → реплан
  2. if mode == plan_only → вернуть план
  3. loop (до max_iterations):
     пауза/стоп из Control (event)
     decision = llm.next_action(...)          [T_THOUGHT]
     ask?   → gateway.ask (UI modal/CLI) → ответ в контекст
     final? → assess: achieved? → DONE : «остановись, цель не достигнута» → продолжить
     tool_call:
        safety.decide(mode, tool, args) → risk, needs_confirm
        needs_confirm → gateway.confirm (timeout → отказ → «выбери другой путь»)
        result = registry.call(...)          [T_TOOL_CALL, T_OBSERVATION]
        journal.record(undo=result.data.undo)
        error? → счётчик повторов; ≥3 → «смени стратегию», перепланирование
        finish_task? → DONE
     каждые N шагов: assess → [T_PROGRESS], adjust_plan? → replan,
                    achieved? → DONE
```

Важные свойства:

- **Проверка результата обязательна**: завершение принимается только после
  `assess.achieved` (или 2-й попытки LLM завершиться без успеха).
- **Антизацикливание**: счётчик одинаковых (tool, args) ошибок; повтор ≥3 →
  принудительная смена подхода + перепланирование (лимит перепланов).
- **Контекст сжатый**: plan + последние 15 шагов + сводки; история LLM-сообщений
  ограничена окном (40).
- **Checkpoint** после каждого шага → resume после перезапуска
  (`sessions/{task_id}.json`).
- **Control**: pause/stop/resume через `asyncio.Event` — реакция в следующий
  виток цикла.

### 5. Безопасность (`safety/`)

- `Risk`: none/low/medium/high/critical. Базовый риск инструмента +
  `estimate_risk(args)`: например, `terminal_run` сканирует команду паттернами
  (mkfs/format/reg delete/drop table/curl|sh → до CRITICAL); `fs_delete` по
  glob и спискам — массовая операция → HIGH; `process_kill` системного — CRITICAL.
- Режимы: `auto` (подтверждение CRITICAL + массовых), `confirm` (от MEDIUM),
  `step` (всё), `observe` (CRITICAL), `plan_only` (ничего не выполняется).
- Подтверждение — **блокирующее ожидание** в UI (модальное окно/CLI-вопрос)
  с таймаутом; таймаут = отказ; отказ возвращается агенту как наблюдение
  «пользователь отказал, выбери другой путь» — агент адаптируется.
- `terminal_check` — отдельный инструмент с жёстким allow-list (read-only).
- **Undo**: журнал `journal.json` с undo-планами; `undo_last(n)` прогоняет их
  обратно через тот же реестр (без обхода safety — undo-инструменты low-risk).
- Корзина: удалённое уходит в `~/.ai-computer-agent/trash/` с временнoй
  меткой; запись — с бэкапом в `state/backups/`.

### 6. Взаимодействие с пользователем (`events.py`)

Одна шина событий (`EventBus`) — единый контракт для всех UI:
`plan, thought, tool_call, observation, error, progress, confirm_request,
user_ask, task_done, task_failed, task_state, log, screen`.

`InteractionGateway` — pause-механика: агент вызывает `await gateway.ask/confirm`
и **создаёт Future в своём event loop**; UI (Web modal / CLI input / API
`POST /api/confirm`) резолвит его из любого потока через
`loop.call_soon_threadsafe`/`run_coroutine_threadsafe`. Таймаут — отказ.

### 7. Web UI без фреймворков — чат

`http.server.ThreadingHTTPServer` + SSE (`/api/events`) + REST. Один
`index.html` (inline CSS/JS, без CDN — работает офлайн): чат-интерфейс
(сайдбар с чатами: клик/переименование/удаление/поиск), два режима ответа
(Чат — стриминг токенов; Агент — агентный цикл с «активностями» прямо в
чате), голос (Web Speech API + серверный Whisper), вложения (загрузка,
vision-картинки в контекст, текстовые файлы inline), markdown с
код-блоками, модальные подтверждения агента. Всё «операторское» (память,
расписание, триггеры, журнал, undo, режимы риска) — в ⚙ Настройках.
Runtime держит один asyncio-луп в фоновом потоке; HTTP-хендлеры перекидывают
в него корутины (`run_coroutine_threadsafe`) — потокобезопасно.

**Чат-подсистема (`chats.py`)**: `ChatStore` — JSON-файл на чат в
`AGENT_HOME/chats/`; `ChatService` — генерация: чат-режим в отдельном потоке
(стриминг через `llm.chat_stream`, события `chat_delta` с `pos`-дедупликацией
при пересоединении SSE), агент-режим — корутина в лупе (события агента
маркируются `task_id` и транслируются в `chat_activity` нужного чата).
Вложения лежат в `AGENT_HOME/uploads/`, отдаются через `/api/files/<name>`.

### 8. Задачи во времени (`tasks/`)

- `TaskManager` — очередь + N воркеров; каждая задача = `TaskState` на диске
  (resume), события `task_state` в UI.
- `Scheduler` — выражения `daily HH:MM | weekly <dow> HH:MM | hourly |
  every N m/h/d`; тик каждые 30с; срабатывание = новая задача в очереди.
- `TriggerManager` — наблюдение папок: polling 3с (или watchdog), базовая
  линия «уже виденных» файлов, cooldown против лавины; новый файл = задача
  с `{file}`-подстановкой.
- `BackgroundRunner` — произвольные async-задачи с событиями `bg_done`.

### 9. Экран и зрение

`screen_capture` (mss → PNG в `state/screens/`, событие `screen` в UI;
headless → синтетический PNG через собственный PNG-кодек на zlib — конвейер
«снимок → VLM → решение» работает всегда) → `screen_describe` (VLM: окна,
кнопки, поля, ошибки, позиции в %) / `ocr_image` (tesseract). GUI-цикл
агента: `screen_capture → screen_describe → mouse_* → screen_capture → проверка`.

### 10. Память

- `SessionStore` — состояния задач (checkpoint, история, resume).
- `LongTermMemory` — предпочтения/факты в JSON, keyword-поиск; релевантные
  пункты вставляются в системный контекст каждой задачи; UI: add/remove/list
  (прозрачность + управление).

## Поток данных (пример)

```
User: «Найди PDF за месяц и разложи по папкам»
  → planner: [поиск, группировка, разложение, проверка]
  → terminal/llm: fs_search(root, ext=pdf, max_age_days=30)
      [tool_call] [observation: 43 файла]
  → fs_organize(dir=Downloads, exts=pdf, by=month)
      [confirm: HIGH, «43 файла»] → пользователь: OK
      [observation: разложено 43, undo сохранён]
  → assess: progress 80%, remaining «проверить»
  → fs_list(Downloads/2026-08 ...) → проверено
  → finish_task: «Разложены 43 PDF по 3 папкам: ...»
  → [task_done] (UI: зелёная строка, прогресс 100%, история обновлена)
```

## Точки расширения

1. Новый инструмент — `agent/tools/mymod.py` + регистрация.
2. Новый LLM-провайдер — реализация 6 методов `LLM`.
3. Новый UI — подписка на `EventBus` + вызовы `runtime.*`
   (примеры: web, cli).
4. Новый тип триггера (процесс, порт, входящий файл в почте) — класс с
   `on_fire` в `tasks/`.
5. Персистентные «навыки» (скрипты успешных сценариев) — слой над журналом.
