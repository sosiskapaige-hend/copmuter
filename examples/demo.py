"""Демо-сценарий: показывает агентный цикл на mock-LLM (работает офлайн).

Запуск:  python examples/demo.py
"""
import asyncio
import os
import sys
import tempfile
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
from agent.tools import build_registry


def demo(goal: str, mode: str = "auto") -> None:
    tmp = Path(tempfile.mkdtemp(prefix="agent_demo_"))
    os.chdir(tmp)
    cfg = Config()
    cfg.agent_home = tmp / "agent_home"
    cfg.llm.provider = "mock"
    cfg.ensure_dirs()

    bus = EventBus()

    def printer(ev):
        d = ev.data
        if ev.type == "plan":
            print("\n🧭 ПЛАН:")
            for i, s in enumerate(d["steps"], 1):
                print(f"   {i}. {s['title']} — {s.get('detail','')}")
        elif ev.type == "tool_call":
            print(f"   🔧 {d['name']}({d['args']})")
        elif ev.type == "observation":
            mark = "✔" if d["ok"] else "✘"
            print(f"      {mark} {(d.get('output') or d.get('error') or '').splitlines()[0][:100]}")
        elif ev.type == "progress":
            print(f"   📈 {d['progress']}%")
        elif ev.type == "task_done":
            print(f"\n🏁 ГОТОВО: {d['summary']}")
        elif ev.type == "task_failed":
            print(f"\n💥 СБОЙ: {d['error']}")

    bus.subscribe(printer)

    agent = Agent(cfg, MockLLM(), build_registry(), bus, InteractionGateway(bus),
                  Journal(cfg.journal_file), SessionStore(cfg.state_dir),
                  LongTermMemory(cfg.memory_dir), SafetyPolicy("critical", 10),
                  get_platform(), workdir=str(tmp))
    print(f"\n=== ЗАДАЧА: «{goal}» (режим: {mode}) ===")
    st = asyncio.run(agent.run_task(goal, mode=mode))
    print(f"статус: {st.status}, прогресс: {st.progress}%")
    if st.plan:
        print("файлы:", [str(p) for p in tmp.glob("*") if p.is_file()][:5])


if __name__ == "__main__":
    demo("Создай папку DemoProject с main.py и проверь структуру")
    demo("Сделай скриншот экрана и опиши что на нём")
    demo("Проведи диагностику: почему компьютер тормозит?")
    demo("Создай папку PlanOnly", mode="plan_only")
