import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.config import Config
from agent.events import EventBus, InteractionGateway
from agent.llm.mock import MockLLM
from agent.memory.longterm import LongTermMemory
from agent.memory.session import SessionStore
from agent.platform import get_platform
from agent.safety.journal import Journal
from agent.safety.policy import SafetyPolicy
from agent.agent.core import Agent
from agent.agent.prompts import build_context
from agent.tools import build_registry
from agent.tools.base import ToolContext


def build_agent(tmp: Path):
    cfg = Config()
    cfg.agent_home = tmp / "agent_home"
    cfg.llm.provider = "mock"
    cfg.safety.mode = "auto"
    cfg.ensure_dirs()
    bus = EventBus()
    gw = InteractionGateway(bus)
    journal = Journal(cfg.journal_file)
    sessions = SessionStore(cfg.state_dir)
    mem = LongTermMemory(cfg.memory_dir)
    policy = SafetyPolicy("critical", 10)  # в тестах — минимум подтверждений
    llm = MockLLM()
    reg = build_registry()
    agent = Agent(cfg, llm, reg, bus, gw, journal, sessions, mem, policy,
                  get_platform(), workdir=str(tmp))
    return agent, bus, sessions, journal, mem, reg


class TestAgentLoop(unittest.IsolatedAsyncioTestCase):
    def _tmp(self, prefix: str) -> Path:
        tmp = Path(tempfile.mkdtemp(prefix=prefix))
        os.chdir(tmp)
        return tmp

    async def test_end_to_end_create_project(self):
        tmp = self._tmp("agent_e2e_")
        agent, bus, sessions, journal, mem, reg = build_agent(tmp)
        events = []
        bus.subscribe(lambda e: events.append(e.type))
        st = await agent.run_task("Создай папку MyApp с проектом", mode="auto")
        self.assertEqual(st.status, "done", st.summary)
        self.assertGreaterEqual(st.progress, 100 - 1)
        self.assertTrue((tmp / "MyApp").is_dir())
        self.assertTrue((tmp / "MyApp" / "main.py").is_file())
        self.assertIn("plan", events)
        self.assertIn("tool_call", events)
        self.assertIn("observation", events)
        self.assertIn("task_done", events)
        # journal записал действия
        self.assertGreater(len(journal.entries), 3)
        self.assertTrue(any(e.get("undo") for e in journal.entries))

    async def test_plan_only_mode(self):
        tmp = self._tmp("agent_plan_")
        agent, bus, sessions, journal, mem, reg = build_agent(tmp)
        st = await agent.run_task("Создай папку PlanTest", mode="plan_only")
        self.assertEqual(st.status, "done")
        self.assertTrue(st.summary.startswith("План"))
        self.assertFalse((tmp / "PlanTest").exists())

    async def test_error_recovery_and_stop(self):
        tmp = self._tmp("agent_err_")
        agent, bus, sessions, journal, mem, reg = build_agent(tmp)

        # ломаем инструмент: fs_mkdir падает
        real = reg.get("fs_mkdir").execute

        async def broken(ctx, **kw):
            from agent.tools.base import ToolResult
            return ToolResult.fail("permission denied (симуляция)")

        reg.get("fs_mkdir").execute = broken
        st = await agent.run_task("Создай папку BrokenDir", mode="auto")
        # mock повторяет mkdir несколько раз, затем цикл упирается в лимит
        # самоисправление: агент не должен зависнуть; статус — done или failed
        self.assertIn(st.status, ("done", "failed", "cancelled"))
        reg.get("fs_mkdir").execute = real

    async def test_stop_control(self):
        tmp = self._tmp("agent_stop_")
        agent, bus, sessions, journal, mem, reg = build_agent(tmp)

        # делаем slow-инструмент, чтобы успеть остановить
        import time as _t
        real = reg.get("wait").execute

        async def slow(ctx, **kw):
            await asyncio.sleep(5)
            from agent.tools.base import ToolResult
            return ToolResult.ok_result("ок")

        reg.get("wait").execute = slow
        task = asyncio.create_task(agent.run_task("Следи за процессами и подожди", mode="auto"))
        await asyncio.sleep(0.4)
        running = agent.list_running()
        self.assertEqual(len(running), 1)
        agent.stop_task(running[0])
        st = await asyncio.wait_for(task, timeout=10)
        self.assertEqual(st.status, "cancelled")
        reg.get("wait").execute = real

    async def test_memory_context(self):
        tmp = self._tmp("agent_mem_")
        agent, bus, sessions, journal, mem, reg = build_agent(tmp)
        mem.add("preference", "проекты храню в ~/Projects")
        st = await agent.run_task("Создай папку MemTest", mode="auto")
        self.assertEqual(st.status, "done")

    async def test_undo_last(self):
        tmp = self._tmp("agent_undo_")
        agent, bus, sessions, journal, mem, reg = build_agent(tmp)
        st = await agent.run_task("Создай папку UndoTest", mode="auto")
        self.assertEqual(st.status, "done")
        self.assertTrue((tmp / "UndoTest").is_dir())
        r = await reg.call("undo_last", {"steps": 3}, agent._ctx)
        self.assertTrue(r.ok, r.error)
        # undo последних 3 записей: main.py, .gitignore, README удалены
        self.assertFalse((tmp / "UndoTest/main.py").exists())
        self.assertFalse((tmp / "UndoTest/.gitignore").exists())

    def test_context_is_truncated(self):
        long_done = "x" * 12000
        long_tools = "- tool: " + ("desc " * 500)
        ctx = build_context("platform", "memory", long_tools, "plan", long_done)
        self.assertLess(len(ctx), 20000)
        self.assertIn("усечено", ctx)


class TestSchedulerExpr(unittest.TestCase):
    def test_parse(self):
        from agent.tasks.scheduler import parse_expr
        self.assertEqual(parse_expr("daily 09:00"), {"kind": "daily", "hour": 9, "minute": 0})
        self.assertEqual(parse_expr("hourly"), {"kind": "hourly"})
        self.assertEqual(parse_expr("every 30m")["seconds"], 1800)
        w = parse_expr("weekly mon,wed 10:30")
        self.assertEqual(w["days"], [0, 2])
        with self.assertRaises(ValueError):
            parse_expr("каждый день в 9")


class TestTriggerManager(unittest.IsolatedAsyncioTestCase):
    async def test_folder_trigger(self):
        from agent.tasks.triggers import TriggerManager
        tmp = Path(tempfile.mkdtemp(prefix="agent_trig_"))
        watch = tmp / "Downloads"
        watch.mkdir()
        fired = []

        async def on_fire(t, f):
            fired.append(str(f))

        tm = TriggerManager(tmp / "state", on_fire, poll=0.2)
        tm.add(str(watch), "*.pdf", "сложи {file}")
        await tm.start()
        (watch / "doc.pdf").write_text("x")
        for _ in range(50):
            await asyncio.sleep(0.1)
            if fired:
                break
        await tm.stop()
        self.assertEqual(len(fired), 1)
        self.assertTrue(fired[0].endswith("doc.pdf"))


class TestSessionResume(unittest.IsolatedAsyncioTestCase):
    async def test_checkpoint_saved(self):
        tmp = Path(tempfile.mkdtemp(prefix="agent_res_"))
        orig = os.getcwd()
        os.chdir(tmp)
        try:
            await self._go(tmp)
        finally:
            os.chdir(orig)

    async def _go(self, tmp: Path):
        agent, bus, sessions, journal, mem, reg = build_agent(tmp)
        st = await agent.run_task("Создай папку ResumeTest", mode="auto")
        tid = st.task_id
        loaded = sessions.load(tid)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.goal, st.goal)
        self.assertGreater(len(loaded.steps), 0)


if __name__ == "__main__":
    unittest.main()
