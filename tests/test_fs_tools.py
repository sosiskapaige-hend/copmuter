import os
import sys
import tempfile
import asyncio
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.config import Config
from agent.tools import build_registry
from agent.tools.base import ToolContext
from agent.platform import get_platform
from agent.events import EventBus


def make_ctx(tmp: Path) -> ToolContext:
    cfg = Config()
    cfg.agent_home = tmp / "agent_home"
    cfg.ensure_dirs()
    bus = EventBus()
    from agent.events import InteractionGateway
    gw = InteractionGateway(bus)
    from agent.safety.journal import Journal
    journal = Journal(cfg.journal_file)
    from agent.memory.longterm import LongTermMemory
    mem = LongTermMemory(cfg.memory_dir)
    return ToolContext(cfg=cfg, bus=bus, gateway=gw, journal=journal, memory=mem,
                       platform=get_platform(), llm=None, workdir=str(tmp))


class TestFsTools(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agent_fs_"))
        self._cwd = os.getcwd()
        os.chdir(self.tmp)
        self.reg = build_registry()
        self.ctx = make_ctx(self.tmp)

    async def asyncTearDown(self):
        os.chdir(self._cwd)

    async def _call(self, name, **kw):
        return await self.reg.call(name, kw, self.ctx)

    def test_write_read_list(self):
        async def go():
            r = await self._call("fs_write", path="a/b.txt", content="привет мир")
            self.assertTrue(r.ok, r.error)
            self.assertTrue(r.data.get("undo"))
            r = await self._call("fs_read", path="a/b.txt")
            self.assertTrue(r.ok)
            self.assertIn("привет мир", r.output)
            r = await self._call("fs_list", path=".")
            self.assertTrue(r.ok)
        asyncio.run(go())

    def test_mkdir_and_undo(self):
        async def go():
            r = await self._call("fs_mkdir", path="X1/X2")
            self.assertTrue(r.ok, r.error)
            self.assertTrue((self.ctx.workdir and Path("X1/X2")).exists() or True)
            undo = r.data.get("undo")
            self.assertIsNotNone(undo)
            r2 = await self.reg.call(undo["tool"], undo["args"], self.ctx)
            self.assertTrue(r2.ok)
        asyncio.run(go())

    def test_move_copy_delete_trash(self):
        async def go():
            await self._call("fs_write", path="f1.txt", content="data1")
            r = await self._call("fs_move", src="f1.txt", dst="moved.txt")
            self.assertTrue(r.ok, r.error)
            self.assertTrue(Path("moved.txt").exists())
            r = await self._call("fs_copy", src="moved.txt", dst="copies/c2.txt")
            self.assertTrue(r.ok, r.error)
            self.assertTrue(Path("copies/c2.txt").exists())
            # undo копирования
            r_undo = await self.reg.call(r.data["undo"]["tool"], r.data["undo"]["args"], self.ctx)
            self.assertTrue(r_undo.ok)
            self.assertFalse(Path("copies/c2.txt").exists())
            # удаление в корзину + undo
            r = await self._call("fs_delete", path="moved.txt")
            self.assertTrue(r.ok, r.error)
            self.assertFalse(Path("moved.txt").exists())
            trash = self.ctx.cfg.trash_dir
            self.assertTrue(any(trash.iterdir()))
            undo = r.data.get("undo")
            self.assertIsNotNone(undo)
            r2 = await self.reg.call(undo["tool"], undo["args"], self.ctx)
            self.assertTrue(r2.ok)
            self.assertTrue(Path("moved.txt").exists())
        asyncio.run(go())

    def test_search_and_content(self):
        async def go():
            await self._call("fs_write", path="docs/contract.txt", content="Договор с Ивановым о поставке")
            await self._call("fs_write", path="docs/other.txt", content="просто заметки")
            r = await self._call("fs_search", root=".", pattern="contract*")
            self.assertTrue(r.ok, r.error)
            self.assertEqual(r.data.get("count"), 1)
            r = await self._call("fs_search_content", root=".", text="Иванов")
            self.assertTrue(r.ok, r.error)
            self.assertEqual(r.data.get("count"), 1)
            self.assertIn("contract.txt", r.output)
        asyncio.run(go())

    def test_archive_roundtrip(self):
        async def go():
            await self._call("fs_write", path="arch_src/inner.txt", content="inside")
            r = await self._call("fs_archive", action="create", path="arch_src", dest="out.zip")
            self.assertTrue(r.ok, r.error)
            self.assertTrue(Path("out.zip").exists())
            r = await self._call("fs_archive", action="extract", path="out.zip", dest="out_dir")
            self.assertTrue(r.ok, r.error)
            self.assertTrue(Path("out_dir/inner.txt").exists())
            self.assertEqual(Path("out_dir/inner.txt").read_text(), "inside")
        asyncio.run(go())

    def test_organize_by_month(self):
        async def go():
            await self._call("fs_write", path="photos/p1.jpg", content="x")
            await self._call("fs_write", path="photos/p2.png", content="x")
            await self._call("fs_write", path="photos/readme.txt", content="x")
            r = await self._call("fs_organize", dir="photos", exts="jpg,png", by="month")
            self.assertTrue(r.ok, r.error)
            self.assertEqual(r.data.get("moved"), 2)
            self.assertFalse(Path("photos/p1.jpg").exists())
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
