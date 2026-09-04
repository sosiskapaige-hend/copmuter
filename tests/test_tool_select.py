"""Отбор инструментов под бюджет контекста (agent/agent/tool_select.py)."""
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.tools import build_registry
from agent.agent.tool_select import select_tools, find_tool_names, CORE_TOOLS


WIN = SimpleNamespace(has_display=True, has_playwright=True, system="windows")
HEADLESS = SimpleNamespace(has_display=False, has_playwright=False, system="linux")


class TestToolSelect(unittest.TestCase):
    def setUp(self):
        self.reg = build_registry()
        self.all = self.reg.all()

    def _names(self, goal, plan="", recent=(), pinned=(), platform=WIN, budget=40):
        return [t.name for t in select_tools(goal, plan, recent, pinned, self.all,
                                             platform=platform, budget=budget)]

    def test_core_always_present(self):
        for goal in ("Создай папку Test", "Открой ютуб", "Почему тормозит", "Сверни окна"):
            names = self._names(goal)
            for core in CORE_TOOLS:
                self.assertIn(core, names, f"{core} отсутствует для «{goal}»")
            self.assertLessEqual(len(names), 40)

    def test_browser_task_gets_browser_tools(self):
        names = self._names("Открой сайт github.com и скачай архив")
        for n in ("browser_open", "browser_click", "browser_download", "open_url"):
            self.assertIn(n, names)

    def test_window_task_gets_window_tools(self):
        names = self._names("Сверни все окна кроме Chrome")
        self.assertIn("window_minimize", names)
        self.assertIn("close_all_except", names)

    def test_input_task_gets_input_tools(self):
        names = self._names("Кликни по кнопке Войти и введи пароль")
        self.assertIn("mouse_click", names)
        self.assertIn("keyboard_type", names)

    def test_os_task_gets_os_tools(self):
        names = self._names("Выключи звук и включи wifi")
        self.assertIn("os_settings", names)

    def test_headless_demotes_gui(self):
        names = self._names("Создай папку X", platform=HEADLESS)
        self.assertNotIn("mouse_click", names)
        self.assertNotIn("window_focus", names)
        self.assertIn("fs_mkdir", names)

    def test_pinned_and_recent_included(self):
        names = self._names("Создай папку X", pinned={"git_clone"}, recent=["clip_get"])
        self.assertIn("git_clone", names)
        self.assertIn("clip_get", names)

    def test_plan_mentions_count(self):
        names = self._names("Сделай задачу", plan="1. browser_open github → 2. browser_extract")
        self.assertIn("browser_open", names)
        self.assertIn("browser_extract", names)

    def test_budget_zero_returns_all(self):
        self.assertEqual(len(self._names("x", budget=0)), len(self.all))
        self.assertEqual(len(self._names("x", budget=1000)), len(self.all))

    def test_find_tool_names(self):
        found = find_tool_names("Сначала вызову fs_mkdir, потом fs_list. Не fs_mkdir2.", self.reg.names())
        self.assertEqual(found, {"fs_mkdir", "fs_list"})


if __name__ == "__main__":
    unittest.main()
