"""Git-инструменты: clone, status, log, diff, commit, branch. Безопасность:
clone/commit — LOW-MEDIUM; push — MEDIUM (может опубликовать)."""
from __future__ import annotations

import asyncio
import os
import subprocess

from .base import Tool, ToolResult, ToolContext, Risk, _prop
from .registry import ToolRegistry


async def _git(args: list[str], cwd: str, timeout: int = 180) -> ToolResult:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args, cwd=cwd or None,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout)
        out = out_b.decode("utf-8", "replace").strip()
        err = err_b.decode("utf-8", "replace").strip()
        code = proc.returncode or 0
        text = out or err or f"(пусто, exit={code})"
        return (ToolResult.ok_result(text, exit_code=code) if code == 0
                else ToolResult.fail(text, exit_code=code))
    except FileNotFoundError:
        return ToolResult.fail("git не установлен")
    except asyncio.TimeoutError:
        return ToolResult.fail(f"git {args[0]}: таймаут {timeout}с")


def register_git_tools(reg: ToolRegistry) -> None:

    @reg.tool("git_clone", "Клонировать репозиторий в папку.", risk=Risk.LOW, category="git",
              parameters={"type": "object", "properties": {
                  "url": _prop("string", "URL репозитория"),
                  "dest": _prop("string", "Папка (по умолчанию — имя репо)"),
                  "branch": _prop("string", "Ветка")}, "required": ["url"]})
    class GitClone(Tool):
        async def execute(self, ctx: ToolContext, url: str, dest: str = "",
                          branch: str = "") -> ToolResult:
            args = ["clone"]
            if branch:
                args += ["-b", branch]
            args += [url]
            if dest:
                args.append(dest)
            return await _git(args, ctx.workdir)

    @reg.tool("git_status", "Статус репозитория.", risk=Risk.NONE, category="git",
              parameters={"type": "object", "properties": {
                  "cwd": _prop("string", "Папка репозитория")}, "required": []})
    class GitStatus(Tool):
        async def execute(self, ctx: ToolContext, cwd: str = "") -> ToolResult:
            return await _git(["status", "--short", "-b"], cwd or ctx.workdir)

    @reg.tool("git_log", "История коммитов.", risk=Risk.NONE, category="git",
              parameters={"type": "object", "properties": {
                  "cwd": _prop("string", "Папка"), "n": _prop("integer", "Сколько (по умолчанию 10)")},
                  "required": []})
    class GitLog(Tool):
        async def execute(self, ctx: ToolContext, cwd: str = "", n: int = 10) -> ToolResult:
            return await _git(["log", "--oneline", f"-{max(1, min(n, 100))}", "--stat"],
                              cwd or ctx.workdir)

    @reg.tool("git_diff", "Изменения (working tree или против коммита).", risk=Risk.NONE,
              category="git",
              parameters={"type": "object", "properties": {
                  "cwd": _prop("string", "Папка"), "target": _prop("string", "Напр. HEAD~1")},
                  "required": []})
    class GitDiff(Tool):
        async def execute(self, ctx: ToolContext, cwd: str = "", target: str = "") -> ToolResult:
            args = ["diff"]
            if target:
                args.append(target)
            return await _git(args, cwd or ctx.workdir)

    @reg.tool("git_commit", "Коммит (staged или с -a).", risk=Risk.LOW, category="git",
              parameters={"type": "object", "properties": {
                  "cwd": _prop("string", "Папка"), "message": _prop("string", "Сообщение"),
                  "all": _prop("boolean", "git add -A перед коммитом")},
                  "required": ["cwd", "message"]})
    class GitCommit(Tool):
        async def execute(self, ctx: ToolContext, cwd: str, message: str,
                          all: bool = False) -> ToolResult:
            if all:
                await _git(["add", "-A"], cwd)
            return await _git(["commit", "-m", message], cwd)

    @reg.tool("git_branch", "Список/создание/переключение веток.", risk=Risk.LOW, category="git",
              parameters={"type": "object", "properties": {
                  "cwd": _prop("string", "Папка"), "name": _prop("string", "Имя ветки (создать/переключить)"),
                  "action": _prop("string", "list | create | checkout (по умолчанию list)")},
                  "required": ["cwd"]})
    class GitBranch(Tool):
        async def execute(self, ctx: ToolContext, cwd: str, name: str = "",
                          action: str = "list") -> ToolResult:
            if action == "create" and name:
                return await _git(["checkout", "-b", name], cwd)
            if action == "checkout" and name:
                return await _git(["checkout", name], cwd)
            return await _git(["branch", "-a"], cwd)

    @reg.tool("git_push", "Отправить коммиты в удалённый репозиторий.", risk=Risk.MEDIUM,
              category="git",
              parameters={"type": "object", "properties": {
                  "cwd": _prop("string", "Папка"), "remote": _prop("string", "По умолчанию origin"),
                  "branch": _prop("string", "Ветка (по умолчанию текущая)")},
                  "required": ["cwd"]})
    class GitPush(Tool):
        async def execute(self, ctx: ToolContext, cwd: str, remote: str = "origin",
                          branch: str = "") -> ToolResult:
            args = ["push"]
            if branch:
                args += [remote, branch]
            return await _git(args, cwd)
