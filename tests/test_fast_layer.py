"""Тесты нового слоя: движок намерений, быстрый путь, ожидания, зрение, ввод.

Проверяем ровно то, что обещает ТЗ: простые команды выполняются без модели,
результат проверяется, способы имеют запасные варианты, а подсистемы умеют
работать headless (без GUI и без внешних библиотек).
"""
import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.apps.aliases import AliasResolver, normalize, similarity
from agent.apps.catalog import BY_KEY, builtin_aliases
from agent.apps.launcher import AppLauncher, FakeLaunchEnv
from agent.apps.registry import AppRegistry
from agent.config import Config
from agent.events import EventBus, InteractionGateway
from agent.fast.batch import Action, ActionQueue
from agent.fast.executor import FastExecutor
from agent.fast.intent import Intent, IntentEngine, IntentMemory
from agent.fast.layer import FastLayer, _clean_code, _slug
from agent.fast.optimizer import ExecutionOptimizer
from agent.fast.router import AGENT, CHAT, DIRECT, VISION, FastRouter
from agent.input.controller import InputController
from agent.memory.longterm import LongTermMemory
from agent.performance.logger import RunLog
from agent.performance.metrics import Metrics
from agent.platform import get_platform
from agent.safety.journal import Journal
from agent.safety.policy import SafetyPolicy
from agent.system.state import ComputerState
from agent.system.wait import WaitManager
from agent.tools import build_registry
from agent.tools.base import ToolContext
from agent.vision.coordinates import CoordinateMapper
from agent.vision.elements import VisionProcessor
from agent.vision.screenshot import ScreenshotManager


class AutoGateway:
    """Шлюз подтверждений для тестов: сразу одобряет и запоминает запросы."""

    def __init__(self):
        self.asked: list[str] = []

    async def confirm(self, description: str, payload=None, timeout: float = 0.0):
        self.asked.append(description)
        return True, ""

    async def ask(self, question: str, timeout: float = 0.0):
        self.asked.append(question)
        return "да"


def make_ctx(tmp: Path, services: dict | None = None) -> ToolContext:
    cfg = Config()
    cfg.agent_home = tmp / "agent_home"
    cfg.ensure_dirs()
    bus = EventBus()
    return ToolContext(cfg=cfg, bus=bus, gateway=InteractionGateway(bus),
                       journal=Journal(cfg.journal_file),
                       memory=LongTermMemory(cfg.memory_dir),
                       platform=get_platform(), llm=None, workdir=str(tmp),
                       services=services or {})


class BaseCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agent_fast_"))
        self._cwd = os.getcwd()
        os.chdir(self.tmp)
        self.reg = build_registry()
        self.apps = AppRegistry(self.tmp / "apps.json")
        self.metrics = Metrics(None)
        self.log = RunLog(None)
        self.state = ComputerState(get_platform(), cache=None, workdir=str(self.tmp))
        self.wait = WaitManager(state=self.state, platform=get_platform())
        self.optimizer = ExecutionOptimizer(metrics=self.metrics, log=self.log)
        self.inputs = InputController(platform=get_platform(),
                                      virtual_path=self.tmp / "input.jsonl")
        self.screens = ScreenshotManager(cfg=None, state=self.state, log=self.log,
                                        metrics=self.metrics)
        self.vision = VisionProcessor(None, self.screens, state=self.state,
                                      log=self.log, metrics=self.metrics)
        self.policy = SafetyPolicy()
        self.gateway = AutoGateway()
        self.ctx = make_ctx(self.tmp, services={"state": self.state, "wait": self.wait,
                                                "inputs": self.inputs, "apps": self.apps,
                                                "metrics": self.metrics, "log": self.log,
                                                "screens": self.screens,
                                                "vision": self.vision})
        self.executor = FastExecutor(cfg=Config(), registry=self.reg, metrics=self.metrics,
                                     log=self.log, optimizer=self.optimizer, wait=self.wait,
                                     state=self.state, screens=self.screens, vision=self.vision,
                                     inputs=self.inputs, apps=self.apps, policy=self.policy,
                                     gateway=self.gateway, bus=self.ctx.bus)
        cfg = Config()
        cfg.agent_home = self.tmp / "agent_home"
        self.layer = FastLayer(cfg=cfg, registry=self.reg, llm=None, bus=self.ctx.bus,
                               metrics=self.metrics, log=self.log, apps=self.apps,
                               state=self.state, wait=self.wait, inputs=self.inputs,
                               screens=self.screens, vision=self.vision,
                               optimizer=self.optimizer, policy=self.policy,
                               gateway=self.gateway, platform=get_platform(),
                               workdir=str(self.tmp))
        self.layer.executor.policy = self.policy
        self.layer.executor.gateway = self.gateway
        # подтверждения в тестах не должны ждать пользователя
        self.ctx.cfg.agent.ask_user_timeout = 1.0
        self.engine = IntentEngine(registry=self.apps, memory=IntentMemory(None))

    async def asyncTearDown(self):
        os.chdir(self._cwd)


class TestIntentEngine(BaseCase):
    async def test_launch_aliases(self):
        for phrase, key in [("открой телегу", "telegram"), ("запусти дискорд", "discord"),
                            ("открой проводник", "explorer"), ("открой вс код", "vscode"),
                            ("запусти браузер", None)]:
            intent = self.engine.parse(phrase)
            self.assertEqual(intent.name, "launch_app", phrase)
            if key:
                self.assertEqual(intent.slots.get("app_key"), key, phrase)

    async def test_search_queries_are_clean(self):
        cases = {"найди видео про котиков на ютубе": ("youtube_search", "котиков"),
                 "погугли 123": ("web_search", "123"),
                 "найди в интернете погоду в Москве": ("web_search", "погоду в Москве")}
        for phrase, (name, query) in cases.items():
            intent = self.engine.parse(phrase)
            self.assertEqual(intent.name, name, phrase)
            self.assertEqual(intent.slots.get("query"), query, phrase)

    async def test_file_intents_and_filler(self):
        intent = self.engine.parse("создай папку WebTest через веб-интерфейс")
        self.assertEqual(intent.name, "create_folder")
        self.assertEqual(intent.slots.get("target"), "WebTest")
        intent = self.engine.parse("удали папку 123 с рабочего стола")
        self.assertEqual(intent.name, "delete_path")
        self.assertTrue(intent.slots.get("path", "").endswith("123"))

    async def test_compound_and_fallback(self):
        intent = self.engine.parse("открой Discord и Telegram")
        self.assertEqual(intent.name, "compound")
        self.assertEqual(len(intent.parts), 2)
        self.assertEqual(self.engine.parse("сделай сайт для магазина").name, "code_task")
        complex_intent = self.engine.parse("проанализируй документы и сделай отчёт")
        self.assertEqual(complex_intent.name, "agent_task")

    async def test_memory_learns_phrase(self):
        engine = IntentEngine(registry=self.apps, memory=IntentMemory(self.tmp / "mem.json"))
        first = engine.parse("вруби мой плеер")
        engine.memory.remember("вруби мой плеер",
                               Intent("launch_app", {"app_key": "spotify",
                                                     "app_display": "Spotify"}, 0.9))
        second = engine.parse("вруби мой плеер")
        self.assertNotEqual(first.name, "launch_app")   # до обучения — не знали
        self.assertEqual(second.name, "launch_app")
        self.assertEqual(second.slots.get("app_key"), "spotify")
        self.assertEqual(second.source, "memory")


class TestRouter(BaseCase):
    async def test_direct_vs_agent(self):
        layer = FastLayer(registry=self.reg, apps=self.apps, policy=self.policy,
                          vision=self.vision, screens=self.screens)
        self.assertEqual(layer.route("создай папку RouterA").kind, DIRECT)
        self.assertEqual(layer.route("сколько будет 2+2").kind, CHAT)
        self.assertEqual(layer.route("проанализируй документы и сделай отчёт").kind, AGENT)

    async def test_vision_route_needs_vision(self):
        router = FastRouter(engine=self.engine, executor=self.executor)
        route = router.route("нажми на кнопку Отправить")
        self.assertIn(route.kind, (VISION, AGENT))
        # без подсистем зрения честно уходим в агентный цикл
        self.executor.vision = None
        self.assertEqual(router.route("нажми на кнопку Отправить").kind, AGENT)

    async def test_preview_marks_confirmation(self):
        layer = FastLayer(registry=self.reg, apps=self.apps, policy=self.policy)
        self.assertTrue(layer.preview("выключи компьютер")["needs_confirm"])
        self.assertFalse(layer.preview("создай папку SafeDir")["needs_confirm"])


class TestFastExecution(BaseCase):
    async def test_create_folder_and_file_then_delete(self):
        res = await self.executor.execute(_intent(self.engine, "создай папку PrefA"),
                                          self.ctx, mode="auto")
        self.assertTrue(res.ok, res.error)
        self.assertTrue((self.tmp / "PrefA").is_dir())
        res = await self.executor.execute(
            _intent(self.engine, "создай файл note.txt с текстом привет"), self.ctx, mode="auto")
        self.assertTrue(res.ok, res.error)
        self.assertEqual((self.tmp / "note.txt").read_text(encoding="utf-8"), "привет")
        res = await self.executor.execute(_intent(self.engine, "удали папку PrefA"), self.ctx,
                                          mode="auto")
        self.assertTrue(res.ok, res.error)
        self.assertFalse((self.tmp / "PrefA").exists())

    async def test_compound_with_code_part_is_not_fake_success(self):
        # «сделай папку и напиши калькулятор» — инструменты делают своё,
        # но без модели код не сгенерировать: это не «выполнено».
        comp = Intent("compound", {"count": 2}, 0.9, "тест", parts=[
            Intent("create_folder", {"path": str(self.tmp / "CompoundDir")}, 0.9, "тест"),
            Intent("code_task", {"language": "python", "what": "калькулятор"}, 0.8, "тест")])
        res = await self.executor.execute(comp, self.ctx, mode="auto")
        self.assertTrue((self.tmp / "CompoundDir").is_dir())   # первая часть выполнена
        self.assertFalse(res.ok)
        self.assertTrue(res.needs_llm_text)
        self.assertTrue(res.needs_agent)

    async def test_layer_handles_compound_with_code(self):
        res = await self.layer.run("открой VS Code и напиши калькулятор на Python",
                                   self.ctx, mode="auto")
        self.assertIsNotNone(res)       # маршрут — быстрый путь, а не агентный цикл
        self.assertEqual(res.intent_name, "compound")
        self.assertTrue(any("code" in str(a) for a in res.actions))

    async def test_launch_uses_registry_chain(self):
        env = FakeLaunchEnv(platform="windows", existing={"C:/Apps/Telegram/telegram.exe"})
        self.apps.set_path("telegram", "C:/Apps/Telegram/telegram.exe", plat="windows")
        launcher = AppLauncher(self.apps, env=env, metrics=self.metrics, log=self.log,
                               optimizer=self.optimizer, wait=self.wait, cfg=None)
        self.executor.launcher = launcher
        res = await self.executor.execute(_intent(self.engine, "открой телегу"), self.ctx,
                                          mode="auto")
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.data.get("method"), "cached_executable")
        self.assertTrue(self.apps.find("телегу").ok)      # алиас выучен

    async def test_failed_action_reports_honestly(self):
        intent = _intent(self.engine, "закрой chrome")
        res = await self.executor.execute(intent, self.ctx, mode="auto")
        self.assertFalse(res.ok)          # процесса нет — задача не «выполнена»
        self.assertTrue(res.text or res.error)

    async def test_dangerous_action_asks_user(self):
        # в режиме step подтверждение обязательно даже для безопасных вещей
        intent = _intent(self.engine, "закрой chrome")
        await self.executor.execute(intent, self.ctx, mode="step")
        self.assertTrue(self.gateway.asked)
        self.assertTrue(any("chrome" in q.lower() or "Chrome" in q for q in self.gateway.asked))


class TestBatchAndWaits(BaseCase):
    async def test_action_queue_runs_without_llm(self):
        q = ActionQueue(max_parallel=3, log=self.log, metrics=self.metrics)
        q.add(Action("fs_mkdir", {"path": str(self.tmp / "batchA")}, verify="file_exists"))
        q.add(Action("fs_write", {"path": str(self.tmp / "batchA" / "a.txt"),
                                  "content": "1"}, depends_on=[0], verify="file_exists"))
        q.add(Action("fs_write", {"path": str(self.tmp / "batchA" / "b.txt"),
                                  "content": "2"}, depends_on=[0], verify="soft"))

        async def call(a):
            return await self.reg.call(a.tool, a.args, self.ctx)

        async def verify(a):
            if a.verify == "file_exists":
                ok = Path(str(a.args.get("path"))).exists()
                return ok, "файл на месте" if ok else "файла нет"
            return None, ""

        result = await q.run(call, verify=verify)
        self.assertTrue(result.ok, result.error)
        self.assertTrue((self.tmp / "batchA" / "a.txt").is_file())
        self.assertEqual(result.actions[0].status, "success")

    async def test_wait_manager_conditions(self):
        target = self.tmp / "late.txt"

        async def create_later():
            await asyncio.sleep(0.15)
            target.write_text("ok", encoding="utf-8")

        asyncio.ensure_future(create_later())
        res = await self.wait.wait_file_exists(str(target), timeout=3.0)
        self.assertTrue(res.ok)
        self.assertLess(res.elapsed_ms, 3000)
        gone = await self.wait.wait_file_gone(str(self.tmp / "never.txt"), timeout=0.3)
        self.assertTrue(gone.ok)

    async def test_wait_condition_times_out(self):
        res = await self.wait.wait_condition(lambda: False, timeout=0.2)
        self.assertFalse(res.ok)


class TestSubsystems(BaseCase):
    async def test_optimizer_learns_methods(self):
        methods = ["cached_executable", "registered_app", "protocol", "shell"]
        self.assertEqual(self.optimizer.rank_methods("launch_app", methods)[0], "cached_executable")
        for _ in range(3):
            self.optimizer.note("launch_app", "protocol", True, 30)
        self.assertEqual(self.optimizer.rank_methods("launch_app", methods)[0], "protocol")
        self.optimizer.note("launch_app", "protocol", False, 5000)
        self.assertNotEqual(self.optimizer.rank_methods("launch_app", methods)[0], "protocol")

    async def test_app_registry_and_aliases(self):
        self.assertGreaterEqual(len(self.apps.all()), 40)
        for phrase, key in [("телега", "telegram"), ("тг", "telegram"), ("хром", "chrome"),
                            ("вс коде", "vscode"), ("photoshop", "photoshop")]:
            res = self.apps.find(phrase)
            self.assertTrue(res.ok, phrase)
            self.assertEqual(res.record.key, key, phrase)
        self.apps.add_aliases("spotify", ["музыкалка"])
        self.assertEqual(self.apps.find("музыкалку").record.key, "spotify")

    async def test_similarity_helper(self):
        self.assertGreaterEqual(similarity("телеграм", "телеграмм"), 0.9)
        self.assertEqual(normalize("Ёлка,  Тест!"), "елка тест")

    async def test_screenshot_pipeline_headless(self):
        shot = await self.screens.capture(tag="test")
        self.assertTrue(Path(shot.path).is_file())
        self.assertGreater(shot.width, 0)
        diff = self.screens.difference(shot, shot)
        self.assertAlmostEqual(diff, 0.0, places=6)

    async def test_coordinate_mapper(self):
        shot = type("S", (), {"width": 1000, "height": 500, "origin_x": 100, "origin_y": 50,
                              "scale": 1.0, "dpi_scale": 1.0})()
        mapper = CoordinateMapper(shot, monitors=None, dpi_scale=1.0)
        centre = mapper.normalized_to_screen(500, 500)         # Qwen-VL: 0..1000
        cx, cy = (centre.x, centre.y) if hasattr(centre, "x") else tuple(centre)
        self.assertAlmostEqual(cx, 600, delta=3)
        self.assertAlmostEqual(cy, 300, delta=3)
        unit = mapper.normalized_to_screen(0.5, 0.5, mode="0-1")
        ux, uy = (unit.x, unit.y) if hasattr(unit, "x") else tuple(unit)
        self.assertAlmostEqual(ux, 600, delta=3)
        self.assertAlmostEqual(uy, 300, delta=3)
        point = mapper.image_to_screen(10, 20)
        px, py = (point.x, point.y) if hasattr(point, "x") else tuple(point)
        self.assertEqual((px, py), (110, 70))

    async def test_input_controller_sequence(self):
        await self.inputs.type_text("привет")
        res = await self.inputs.run_sequence([{"action": "click", "x": 10, "y": 10},
                                              ("hotkey", "ctrl+s"), ("type", "текст")])
        self.assertTrue(res.ok, res.message)
        written = (self.tmp / "input.jsonl").read_text(encoding="utf-8").strip().splitlines()
        self.assertGreaterEqual(len(written), 3)

    async def test_metrics_and_log(self):
        self.metrics.task_start("direct", "тест", t0=time.perf_counter() - 0.05)
        self.metrics.tool_call("fs_mkdir", 12.0, True)
        self.metrics.task_end(ok=True, note="готово")
        summary = self.metrics.summary(top=5)
        self.assertEqual(summary["ttc"]["success_rate"], 1.0)
        self.assertGreaterEqual(summary["ttc"]["avg_ms"], 40)
        self.assertIn("fs_mkdir", summary["tools"])
        self.log.event("test", ok=True, ms=1.2)
        self.assertTrue(self.log.tail(1))


class TestVisionProcessor(BaseCase):
    async def test_analyze_without_model_reports_honestly(self):
        shot = await self.screens.capture(tag="vision")
        text, data = await self.vision.analyze(shot, prompt="опиши экран")
        # модели нет — не выдумываем результат, просто нет текста
        self.assertEqual(text, "")
        self.assertIsNone(data)

    async def test_extract_json_tolerates_fences(self):
        from agent.vision.elements import extract_json
        data = extract_json('```json\n{"elements": [{"label": "OK", "x": 10}]}\n```')
        self.assertEqual(data["elements"][0]["label"], "OK")


class TestPluginsAndDiscovery(BaseCase):
    async def test_plugin_sdk_registers_tool(self):
        from agent.plugins import load_plugins
        plug = self.tmp / "plugins"
        plug.mkdir(parents=True, exist_ok=True)
        plugin_src = "\n".join([
            "from agent.tools.base import Risk, Tool, ToolResult",
            "",
            "",
            "class Hello(Tool):",
            "    name = 'hello_plugin'",
            "    description = 'тестовый плагин'",
            "    risk = Risk.NONE",
            "",
            "    async def execute(self, ctx, **kw):",
            "        return ToolResult.ok_result('привет')",
            "",
            "",
            "TOOLS = [Hello()]",
        ])
        (plug / "hello.py").write_text(plugin_src, encoding="utf-8")
        (plug / "_skip.py").write_text("raise RuntimeError('не должен загружаться')\n",
                                       encoding="utf-8")
        reg = build_registry(plugins=False)
        summary = load_plugins(reg, dirs=[plug])
        self.assertIn("hello_plugin", reg.names())
        self.assertIn("_skip.py", summary["skipped"])
        self.assertFalse(summary["errors"], summary["errors"])
        res = await reg.call("hello_plugin", {}, self.ctx)
        self.assertTrue(res.ok)
        self.assertEqual(res.output, "привет")

    async def test_broken_plugin_does_not_break_registry(self):
        from agent.plugins import load_plugins
        plug = self.tmp / "plugins2"
        plug.mkdir(parents=True, exist_ok=True)
        (plug / "broken.py").write_text("this is not python\n", encoding="utf-8")
        reg = build_registry(plugins=False)
        before = len(reg.names())
        summary = load_plugins(reg, dirs=[plug])
        self.assertEqual(len(reg.names()), before)
        self.assertTrue(summary["errors"])

    async def test_registry_discovery_roundtrip(self):
        from agent.apps.discovery import AppDiscovery
        from agent.performance.cache import CacheHub
        hub = CacheHub(self.tmp / "cache")
        apps = AppRegistry(self.tmp / "apps.json")
        discovery = AppDiscovery(cache=hub, log=self.log)
        summary = discovery.run(apps, quick=True)
        self.assertIn("found", summary)
        apps.save()
        again = AppRegistry(self.tmp / "apps.json")
        self.assertEqual(sorted(a.key for a in again.all()),
                         sorted(a.key for a in apps.all()))
        self.assertGreaterEqual(len(again.all()), len(apps.installed()))


class TestLayerHelpers(BaseCase):
    async def test_clean_code_strips_fences(self):
        self.assertEqual(_clean_code("```python\nprint(1)\n```"), "print(1)")
        self.assertEqual(_clean_code("print(2)"), "print(2)")

    async def test_slug_is_latin(self):
        self.assertEqual(_slug("Калькулятор на Python"), "kalkulyator_na_python")

    async def test_catalog_has_expected_apps(self):
        for key in ("telegram", "discord", "vscode", "chrome", "explorer", "settings"):
            self.assertIn(key, BY_KEY, key)
        self.assertGreater(len(builtin_aliases()), 50)

    async def test_alias_resolver_direct(self):
        resolver = AliasResolver(None, builtin_aliases())
        key, score, why = resolver.resolve("телега")
        self.assertEqual(key, "telegram")
        self.assertGreater(score, 0.9)
        self.assertTrue(why)


def _intent(engine: IntentEngine, phrase: str):
    intent = engine.parse(phrase)
    assert intent.name != "agent_task", f"фраза не разобрана: {phrase}"
    return intent


if __name__ == "__main__":
    unittest.main()
