import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.config import Config
from agent.events import EventBus, InteractionGateway
from agent.platform import get_platform
from agent.safety.journal import Journal
from agent.safety.policy import SafetyPolicy, validate_mode
from agent.memory.longterm import LongTermMemory
from agent.tools import build_registry
from agent.tools.base import ToolContext, Risk
from agent.tools.terminal import assess_command


def make_ctx(tmp: Path) -> ToolContext:
    cfg = Config()
    cfg.agent_home = tmp / "agent_home"
    cfg.ensure_dirs()
    bus = EventBus()
    gw = InteractionGateway(bus)
    journal = Journal(cfg.journal_file)
    mem = LongTermMemory(cfg.memory_dir)
    return ToolContext(cfg=cfg, bus=bus, gateway=gw, journal=journal, memory=mem,
                       platform=get_platform(), llm=None, workdir=str(tmp))


class TestPolicy(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agent_safe_"))
        self.reg = build_registry()
        self.ctx = make_ctx(self.tmp)
        self.pol = SafetyPolicy("medium", 10)

    def test_modes(self):
        self.assertEqual(validate_mode("AUTO"), "auto")
        self.assertEqual(validate_mode("bogus"), "confirm")

    def test_risk_levels(self):
        d = self.pol.decide("auto", self.reg.get("fs_mkdir"), {"path": "x"}, self.ctx)
        self.assertFalse(d.needs_confirm)
        d = self.pol.decide("confirm", self.reg.get("fs_write"), {"path": "x", "content": "y"}, self.ctx)
        self.assertTrue(d.needs_confirm)  # medium >= medium
        d = self.pol.decide("auto", self.reg.get("terminal_run"), {"command": "rm -rf /"}, self.ctx)
        self.assertTrue(d.needs_confirm)
        self.assertEqual(d.risk, Risk.CRITICAL)

    def test_step_mode_confirms_everything(self):
        d = self.pol.decide("step", self.reg.get("fs_read"), {"path": "x"}, self.ctx)
        self.assertTrue(d.needs_confirm)

    def test_plan_only(self):
        d = self.pol.decide("plan_only", self.reg.get("terminal_run"), {"command": "rm -rf /"}, self.ctx)
        self.assertFalse(d.needs_confirm)

    def test_bulk_escalation(self):
        d = self.pol.decide("auto", self.reg.get("fs_delete"),
                            {"path": "*.log", "permanent": True}, self.ctx)
        self.assertTrue(d.needs_confirm)
        self.assertGreaterEqual(d.risk, Risk.HIGH)

    def test_terminal_assess(self):
        r, _ = assess_command("ls -la")
        self.assertEqual(r, Risk.NONE)
        r, _ = assess_command("pip install numpy")
        self.assertEqual(r, Risk.MEDIUM)
        r, why = assess_command("rm -rf /")
        self.assertEqual(r, Risk.CRITICAL)
        r, _ = assess_command("sc stop nginx")
        self.assertEqual(r, Risk.HIGH)

    def test_journal_undo_cycle(self):
        import asyncio
        j = self.ctx.journal
        e = j.record("t1", "fs_move", {"src": "a", "dst": "b"}, True,
                     undo={"tool": "fs_move", "args": {"src": "b", "dst": "a"}, "note": "x"})
        self.assertIsNotNone(e["undo"])
        last = j.pop_last_undoable()
        self.assertEqual(last["tool"], "fs_move")
        self.assertIsNone(j.pop_last_undoable())  # больше нет


class TestTerminalTool(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agent_term_"))
        self._cwd = os.getcwd()
        os.chdir(self.tmp)
        self.reg = build_registry()
        self.ctx = make_ctx(self.tmp)

    async def asyncTearDown(self):
        os.chdir(self._cwd)

    async def test_run_and_error(self):
        r = await self.reg.call("terminal_run", {"command": "echo hello && echo err >&2; exit 3",
                                                 "cwd": str(self.tmp)}, self.ctx)
        self.assertFalse(r.ok)
        self.assertEqual(r.data.get("exit_code"), 3)
        self.assertIn("hello", r.data.get("stdout", ""))
        self.assertIn("err", r.data.get("stderr", ""))
        r = await self.reg.call("terminal_run", {"command": "echo ok", "cwd": str(self.tmp)}, self.ctx)
        self.assertTrue(r.ok, r.error)
        self.assertEqual(r.data.get("exit_code"), 0)

    async def test_check_readonly_only(self):
        r = await self.reg.call("terminal_check", {"command": "echo readonly"}, self.ctx)
        self.assertTrue(r.ok, r.error)
        r = await self.reg.call("terminal_check", {"command": "rm -rf /tmp"}, self.ctx)
        self.assertFalse(r.ok)


if __name__ == "__main__":
    unittest.main()
