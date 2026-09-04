"""Контекст рабочей сессии: цель, выполненные шаги, наблюдения, сводки.

Сохраняет окно для LLM (сжатое) и позволяет возобновить задачу после
перезапуска (checkpoint).
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class StepRecord:
    index: int
    plan_title: str
    tool: str
    args: dict
    ok: bool
    output: str
    error: str = ""
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {"index": self.index, "plan_title": self.plan_title, "tool": self.tool,
                "args": self.args, "ok": self.ok, "output": self.output[:800],
                "error": self.error[:400], "ts": self.ts}


@dataclass
class TaskState:
    task_id: str
    goal: str
    mode: str = "auto"
    status: str = "pending"      # pending|planning|running|paused|waiting_user|done|failed|cancelled
    plan: list[dict] = field(default_factory=list)
    steps: list[StepRecord] = field(default_factory=list)
    progress: int = 0
    iteration: int = 0
    last_error: str = ""
    summary: str = ""
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    extra: dict = field(default_factory=dict)

    def touch(self) -> None:
        self.updated = time.time()

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id, "goal": self.goal, "mode": self.mode,
            "status": self.status, "plan": self.plan, "steps": [s.to_dict() for s in self.steps],
            "progress": self.progress, "iteration": self.iteration,
            "last_error": self.last_error, "summary": self.summary,
            "created": self.created, "updated": self.updated, "extra": self.extra,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TaskState":
        st = cls(task_id=d.get("task_id", uuid.uuid4().hex[:10]), goal=d.get("goal", ""),
                 mode=d.get("mode", "auto"), status=d.get("status", "pending"),
                 plan=d.get("plan", []), progress=d.get("progress", 0),
                 iteration=d.get("iteration", 0), last_error=d.get("last_error", ""),
                 summary=d.get("summary", ""))
        st.steps = [StepRecord(index=s.get("index", i), plan_title=s.get("plan_title", ""),
                               tool=s.get("tool", ""), args=s.get("args", {}),
                               ok=s.get("ok", True), output=s.get("output", ""),
                               error=s.get("error", "")) for i, s in enumerate(d.get("steps", []))]
        return st


class SessionStore:
    """Хранение состояний задач на диске (для resume)."""

    def __init__(self, state_dir: Path) -> None:
        self.dir = Path(state_dir) / "tasks"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.active: dict[str, TaskState] = {}

    def path(self, task_id: str) -> Path:
        return self.dir / f"{task_id}.json"

    def save(self, st: TaskState) -> None:
        try:
            st.touch()
            self.path(st.task_id).write_text(json.dumps(st.to_dict(), ensure_ascii=False, indent=1),
                                             encoding="utf-8")
            self.active[st.task_id] = st
        except OSError:
            pass

    def load(self, task_id: str) -> TaskState | None:
        p = self.path(task_id)
        if not p.exists():
            return None
        try:
            st = TaskState.from_dict(json.loads(p.read_text(encoding="utf-8")))
            self.active[st.task_id] = st
            return st
        except (json.JSONDecodeError, OSError):
            return None

    def list(self) -> list[TaskState]:
        out = []
        for p in sorted(self.dir.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                out.append(TaskState.from_dict(d))
            except (json.JSONDecodeError, OSError):
                continue
        return out[:100]

    def history_brief(self, n: int = 30) -> list[dict]:
        return [{"task_id": s.task_id, "goal": s.goal[:100], "status": s.status,
                 "progress": s.progress, "summary": s.summary[:200],
                 "updated": s.updated} for s in self.list()[:n]]
