"""Терминал: выполнение команд с захватом stdout/stderr/exit code,
таймаутами и динамической оценкой опасности команды.
"""
from __future__ import annotations

import asyncio
import os
import re
import shlex
from typing import Any

from .base import Tool, ToolResult, ToolContext, Risk, _prop
from .registry import ToolRegistry

# Паттерны, повышающие риск команды
_RISKY = [
    (re.compile(r"\b(rm\s+(-[a-z]*r[a-z]*\s+)?(/|~|\$HOME)\s*$)", re.I), Risk.CRITICAL, "удаление корня/дома"),
    (re.compile(r"\b(mkfs|fdisk|diskpart|format)\b", re.I), Risk.CRITICAL, "работа с дисками"),
    (re.compile(r"\b(shutdown|reboot|poweroff|halt)\b", re.I), Risk.CRITICAL, "выключение системы"),
    (re.compile(r"\breg\s+(delete|add)\b", re.I), Risk.HIGH, "изменение реестра"),
    (re.compile(r"\b(sc\s+(stop|delete)|net\s+stop)\b", re.I), Risk.HIGH, "остановка службы"),
    (re.compile(r"\b(rmdir\s+/s|rd\s+/s)\b", re.I), Risk.HIGH, "рекурсивное удаление (win)"),
    (re.compile(r"\bdel\s+(-[a-z]*s[a-z]*|/s)\b", re.I), Risk.HIGH, "массовое удаление (win)"),
    (re.compile(r"\b(drop\s+(table|database)|truncate\s+table)\b", re.I), Risk.HIGH, "разрушение БД"),
    (re.compile(r"\b(curl|wget)\b.*\|\s*(ba)?sh", re.I), Risk.HIGH, "запуск удалённого скрипта"),
    (re.compile(r"\b(chmod\s+[0-7]*777|chmod\s+-R)\b", re.I), Risk.MEDIUM, "массовые права"),
    (re.compile(r"\b(pip\s+install|npm\s+i(nstall)?|apt(-get)?\s+install|winget\s+install|brew\s+install)\b", re.I),
     Risk.MEDIUM, "установка пакетов"),
    (re.compile(r">\s*\S+"), Risk.MEDIUM, "запись в файл редиректом"),
]

_SAFE_READONLY = re.compile(
    r"^(ls|dir|cat|type|echo|pwd|cd|which|where|uname|whoami|date|df|du|free|top|ps|grep|find|head|tail|"
    r"git\s+(status|log|diff|branch|remote|show)|python3?|--version|-h|-V)\b")


def assess_command(cmd: str) -> tuple[Risk, str]:
    if _SAFE_READONLY.match(cmd.strip()):
        return Risk.NONE, ""
    best = Risk.NONE
    reason = ""
    for rx, risk, why in _RISKY:
        if rx.search(cmd):
            if risk > best:
                best, reason = risk, why
    if best == Risk.NONE and any(op in cmd for op in (">", ">>", "mv ", "cp ", "install", "sudo")):
        best, reason = Risk.MEDIUM, "модифицирующая команда"
    return best, reason


def register_terminal_tools(reg: ToolRegistry) -> None:

    @reg.tool("terminal_run",
              "Выполнить команду в терминале (bash на Linux/macOS, cmd на Windows). "
              "Возвращает stdout, stderr и exit code. Для опасных команд потребуется подтверждение.",
              risk=Risk.MEDIUM, category="terminal",
              parameters={"type": "object", "properties": {
                  "command": _prop("string", "Команда"),
                  "cwd": _prop("string", "Рабочая директория"),
                  "timeout": _prop("integer", "Таймаут секунд (по умолчанию 120)"),
                  "env": _prop("object", "Дополнительные переменные окружения")},
                  "required": ["command"]})
    class TerminalRun(Tool):
        def estimate_risk(self, ctx, args):
            risk, why = assess_command(str(args.get("command", "")))
            return risk, why

        async def execute(self, ctx: ToolContext, command: str, cwd: str = "",
                          timeout: int = 120, env: dict | None = None) -> ToolResult:
            workdir = os.path.expanduser(cwd) if cwd else os.getcwd()
            if not os.path.isdir(workdir):
                return ToolResult.fail(f"рабочая папка не найдена: {workdir}")
            full_env = dict(os.environ)
            if env:
                full_env.update({str(k): str(v) for k, v in env.items()})
            if ctx.platform.system == "windows":
                proc_cmd = command
                shell = True
            else:
                proc_cmd = command
                shell = True
            try:
                proc = await asyncio.create_subprocess_shell(
                    proc_cmd, cwd=workdir, env=full_env, shell=shell,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                try:
                    out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
                    return ToolResult.fail(f"таймаут ({timeout}с): {command[:120]}", timed_out=True)
                out = out_b.decode("utf-8", "replace")
                err = err_b.decode("utf-8", "replace")
                code = proc.returncode or 0
                parts = [f"exit code: {code}"]
                if out.strip():
                    parts.append(f"STDOUT:\n{out[-4000:]}")
                if err.strip():
                    parts.append(f"STDERR:\n{err[-2000:]}")
                text = "\n".join(parts)
                if code != 0:
                    return ToolResult.fail(text, exit_code=code, stdout=out, stderr=err)
                return ToolResult.ok_result(text, exit_code=code, stdout=out, stderr=err)
            except Exception as e:  # noqa: BLE001
                return ToolResult.fail(f"не удалось запустить: {e}")

    @reg.tool("terminal_check",
              "Быстрая проверка: существует ли файл/папка, установлена ли программа "
              "(версия), порт лишний. ТОЛЬКО read-only команды (жёсткий allow-list).",
              risk=Risk.NONE, category="terminal",
              parameters={"type": "object", "properties": {
                  "command": _prop("string", "Команда проверки (read-only)")},
                  "required": ["command"]})
    class TerminalCheck(Tool):
        _ALLOW = {"ls", "dir", "cat", "type", "echo", "pwd", "whoami", "uname", "date",
                  "df", "du", "free", "ps", "grep", "head", "tail", "which", "where",
                  "test", "stat", "file", "id", "printenv"}
        _GIT_RO = {"status", "log", "diff", "branch", "remote", "show", "rev-parse",
                   "describe", "ls-files"}

        @classmethod
        def _is_readonly(cls, command: str) -> tuple[bool, str]:
            if any(op in command for op in (">", "|", "&", ";", "`", "$(", "rm", "del ",
                                            "mv ", "cp ", "mkfs", "format", "sc ",
                                            "shutdown", "reboot", "kill", "install")):
                return False, "содержит опасные операторы/ключевые слова"
            parts = command.strip().split()
            if not parts:
                return False, "пустая команда"
            head = parts[0]
            if head in cls._ALLOW:
                return True, ""
            if head == "git" and len(parts) > 1 and parts[1] in cls._GIT_RO:
                return True, ""
            return False, f"команда '{head}' не в allow-list"

        async def execute(self, ctx: ToolContext, command: str) -> ToolResult:
            ok_ro, why = self._is_readonly(command)
            if not ok_ro:
                return ToolResult.fail(f"terminal_check только для read-only команд: {why}. "
                                       f"Для остальных — terminal_run (с подтверждением).")
            try:
                proc = await asyncio.create_subprocess_shell(
                    command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                out_b, err_b = await asyncio.wait_for(proc.communicate(), 30)
                out = out_b.decode("utf-8", "replace").strip()
                err = err_b.decode("utf-8", "replace").strip()
                code = proc.returncode or 0
                text = out or err or f"(пусто) exit={code}"
                return ToolResult.ok_result(text, exit_code=code)
            except Exception as e:  # noqa: BLE001
                return ToolResult.fail(f"не удалось: {e}")
