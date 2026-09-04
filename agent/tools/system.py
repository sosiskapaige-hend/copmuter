"""Информация о системе: CPU/RAM/диск/сеть, процессы, диагностика «почему тормозит»."""
from __future__ import annotations

import asyncio
import platform
import shutil
import time
from typing import Any

from .base import Tool, ToolResult, ToolContext, Risk, _prop
from .registry import ToolRegistry


def _psutil():
    try:
        import psutil  # noqa: F401
        return psutil
    except ImportError:
        return None


def _run(cmd: list[str], timeout: float = 10.0) -> str:
    import subprocess
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return f"(ошибка: {e})"


def _mem_info() -> str:
    ps = _psutil()
    if ps:
        vm = ps.virtual_memory()
        return (f"RAM: {vm.total/1073741824:.1f} ГБ всего, {vm.available/1073741824:.1f} ГБ свободно "
                f"({vm.percent}%)")
    out = _run(["free", "-h"]).strip().splitlines()
    return "RAM: " + (out[0] if out else "недоступно")


def _cpu_info() -> str:
    ps = _psutil()
    if ps:
        try:
            per = ps.cpu_percent(interval=0.3)
        except Exception:
            per = 0.0
        return f"CPU: {platform.processor() or platform.machine()}, " \
               f"{ps.cpu_count()} ядер(а), загрузка {per:.0f}%"
    n = os_cpu_count()
    return f"CPU: {platform.processor() or platform.machine()}, {n} ядер(а)"


def os_cpu_count() -> int:
    import os
    return os.cpu_count() or 1


def _disk_info() -> str:
    ps = _psutil()
    lines = []
    if ps:
        for part in ps.disk_partitions_all() if hasattr(ps, "disk_partitions_all") else ps.disk_partitions():
            try:
                if part.fstype in ("squashfs", "tmpfs", "iso9660"):
                    continue
                u = ps.disk_usage(part.mountpoint)
                lines.append(f"{part.mountpoint}: {u.total/1073741824:.0f} ГБ, "
                             f"занято {u.percent}% (свободно {u.free/1073741824:.0f} ГБ)")
            except (PermissionError, OSError):
                continue
    else:
        lines = _run(["df", "-h"]).strip().splitlines()[:10]
    return "\n".join(lines) or "диск: недоступно"


def _net_info() -> str:
    out = _run(["ip", "-4", "addr", "show"]).strip()
    if out:
        return "\n".join(out.splitlines()[:12])
    out = _run(["ifconfig"]).strip()
    return "\n".join(out.splitlines()[:12]) or "нет данных о сети"


def register_system_tools(reg: ToolRegistry) -> None:

    @reg.tool("system_info",
              "Сведения о системе: ОС, CPU, RAM, диски, сеть, время. Начало любой диагностики.",
              risk=Risk.NONE, category="system",
              parameters={"type": "object", "properties": {}})
    class SystemInfo(Tool):
        async def execute(self, ctx: ToolContext) -> ToolResult:
            pl = ctx.platform
            lines = [
                f"ОС: {platform.system()} {platform.release()} ({pl.system})",
                f"Python: {platform.python_version()}",
                _cpu_info(),
                _mem_info(),
                f"Диски:\n{_disk_info()}",
                f"Сеть:\n{_net_info()}",
                f"Мониторы: {len(pl.monitors)}",
                f"Дисплей: {'да' if pl.has_display else 'НЕТ (headless)'}",
            ]
            return ToolResult.ok_result("\n".join(lines))

    @reg.tool("disk_usage", "Использование дисков (разделы, занято/свободно).",
              risk=Risk.NONE, category="system",
              parameters={"type": "object", "properties": {}})
    class DiskUsage(Tool):
        async def execute(self, ctx: ToolContext) -> ToolResult:
            return ToolResult.ok_result(_disk_info())

    @reg.tool("process_list",
              "Список процессов (PID, имя, CPU%, RAM). Лимит N.",
              risk=Risk.NONE, category="system",
              parameters={"type": "object", "properties": {
                  "limit": _prop("integer", "Сколько показать (по умолчанию 20)"),
                  "name_filter": _prop("string", "Фильтр по подстроке имени")},
                  "required": []})
    class ProcessList(Tool):
        async def execute(self, ctx: ToolContext, limit: int = 20,
                          name_filter: str = "") -> ToolResult:
            ps = _psutil()
            rows = []
            if ps:
                procs = []
                for p in ps.process_iter(["pid", "name", "cpu_percent", "memory_info"]):
                    try:
                        info = p.info
                        nm = (info.get("name") or "")
                        if name_filter and name_filter.lower() not in nm.lower():
                            continue
                        mem = (info.get("memory_info") or None)
                        mem_mb = mem.rss / 1048576 if mem else 0.0
                        procs.append((info.get("cpu_percent") or 0.0, mem_mb,
                                      info.get("pid"), nm))
                    except (ps.NoSuchProcess, ps.AccessDenied, ps.ZombieProcess):
                        continue
                procs.sort(reverse=True)
                for cpu, mem, pid, nm in procs[:limit]:
                    rows.append(f"PID {pid:<7} {cpu:5.1f}% CPU  {mem:8.1f} MB  {nm}")
            else:
                out = _run(["ps", "aux"]) if ctx.platform.system != "windows" else _run(["tasklist"])
                for line in out.splitlines()[:limit + 1]:
                    if name_filter and name_filter.lower() not in line.lower():
                        continue
                    rows.append(line)
            return ToolResult.ok_result("\n".join(rows) if rows else "(нет процессов)")

    @reg.tool("process_top",
              "Топ процессов по CPU и RAM — для ответа «почему тормозит».",
              risk=Risk.NONE, category="system",
              parameters={"type": "object", "properties": {
                  "limit": _prop("integer", "Сколько (по умолчанию 8)")}, "required": []})
    class ProcessTop(Tool):
        async def execute(self, ctx: ToolContext, limit: int = 8) -> ToolResult:
            ps = _psutil()
            if not ps:
                return ToolResult.ok_result(_run(["ps", "aux", "--sort=-%cpu"])[:2000])
            await asyncio.sleep(0.4)
            procs = []
            for p in ps.process_iter(["pid", "name", "cpu_percent", "memory_percent", "memory_info"]):
                try:
                    i = p.info
                    mem = i.get("memory_info")
                    procs.append(((i.get("cpu_percent") or 0),
                                  (mem.rss / 1048576 if mem else 0),
                                  i.get("pid"), i.get("name") or "",
                                  i.get("memory_percent") or 0))
                except Exception:
                    continue
            by_cpu = sorted(procs, key=lambda x: -x[0])[:limit]
            by_mem = sorted(procs, key=lambda x: -x[1])[:limit]
            out = ["ТОП по CPU:"]
            out += [f"  {c:5.1f}%  PID {pid:<7} {nm}" for c, m, pid, nm, mp in by_cpu]
            out.append("ТОП по RAM:")
            out += [f"  {m:8.1f} MB  PID {pid:<7} {nm}" for c, m, pid, nm, mp in by_mem]
            cpu_total = 0.0
            try:
                cpu_total = ps.cpu_percent(interval=0.2)
            except Exception:
                pass
            vm = ps.virtual_memory()
            out.insert(0, f"CPU {cpu_total:.0f}%, RAM {vm.percent}%")
            analysis = ""
            if cpu_total > 85 or vm.percent > 90:
                top = by_cpu[0] if by_cpu else None
                if top:
                    analysis = (f"\nАнализ: высокая нагрузка. Подозреваемый: "
                                f"{top[3]} (PID {top[2]}). Рекомендация: завершить процесс, если не нужен "
                                f"(process_kill, с подтверждением), или дождаться завершения работы.")
            elif not by_cpu or by_cpu[0][0] < 5:
                analysis = "\nАнализ: нагрузка в норме, тормозов со стороны процессов не видно."
            return ToolResult.ok_result("\n".join(out) + analysis)

    @reg.tool("process_kill",
              "Завершить процесс по PID или имени. ОБРАТИТЕ ВНИМАНИЕ: может потерять несохранённые данные.",
              risk=Risk.HIGH, category="system",
              parameters={"type": "object", "properties": {
                  "pid": _prop("integer", "PID процесса"),
                  "name": _prop("string", "Или имя (первый найденный)"),
                  "force": _prop("boolean", "Принудительно (SIGKILL / taskkill /f)")},
                  "required": []})
    class ProcessKill(Tool):
        def estimate_risk(self, ctx, args):
            name = str(args.get("name", "")).lower()
            protected = {"system", "kernel", "init", "csrss", "wininit", "explorer",
                         "svchost", "services", "ssd", "taskhost", "systemd"}
            if name in protected:
                return Risk.CRITICAL, "системный процесс"
            return Risk.HIGH, "завершение процесса"

        async def execute(self, ctx: ToolContext, pid: int | None = None,
                          name: str = "", force: bool = False) -> ToolResult:
            ps = _psutil()
            if ps:
                target = None
                if pid:
                    target = ps.Process(pid)
                else:
                    for p in ps.process_iter(["pid", "name"]):
                        try:
                            if (p.info.get("name") or "").lower() == name.lower():
                                target = p
                                break
                        except Exception:
                            continue
                if target is None:
                    return ToolResult.fail(f"процесс не найден: pid={pid} name={name}")
                try:
                    if force:
                        target.kill()
                    else:
                        target.terminate()
                    target.wait(timeout=5)
                    return ToolResult.ok_result(f"Процесс завершён: {target.name()} (PID {target.pid})")
                except ps.TimeoutExpired:
                    target.kill()
                    return ToolResult.ok_result(f"Процесс принудительно завершён: {target.name()}")
                except ps.NoSuchProcess:
                    return ToolResult.ok_result("Процесс уже не запущен")
            # без psutil
            cmd = ["taskkill", "/PID", str(pid), "/F" if force else ""] if pid \
                else ["taskkill", "/IM", name, "/F" if force else ""]
            import subprocess
            r = subprocess.run([c for c in cmd if c], capture_output=True, text=True)
            return ToolResult.ok_result(r.stdout or r.stderr) if r.returncode == 0 \
                else ToolResult.fail(r.stderr or f"exit {r.returncode}")

    @reg.tool("diagnose_system",
              "Полная быстрая диагностика «почему компьютер тормозит»: CPU, RAM, диск, топ процессов, "
              "вывод с рекомендациями.",
              risk=Risk.NONE, category="system",
              parameters={"type": "object", "properties": {}})
    class DiagnoseSystem(Tool):
        async def execute(self, ctx: ToolContext) -> ToolResult:
            ps = _psutil()
            lines = ["=== ДИАГНОСТИКА СИСТЕМЫ ==="]
            if ps:
                cpu = ps.cpu_percent(interval=0.5)
                vm = ps.virtual_memory()
                swap = ps.swap_memory()
                lines.append(f"CPU: {cpu:.0f}%")
                lines.append(f"RAM: {vm.percent}% (свободно {vm.available/1073741824:.1f} ГБ)")
                lines.append(f"Swap: {swap.percent}%")
                try:
                    du = ps.disk_usage("/")
                    lines.append(f"Диск /: {du.percent}% занято")
                except Exception:
                    pass
            else:
                lines.append("psutil не установлен — базовые метрики ограничены")
            procs = []
            if ps:
                for p in ps.process_iter(["pid", "name", "cpu_percent", "memory_info"]):
                    try:
                        i = p.info
                        mem = i.get("memory_info")
                        procs.append(((i.get("cpu_percent") or 0),
                                      (mem.rss / 1048576 if mem else 0),
                                      i.get("pid"), i.get("name") or ""))
                    except Exception:
                        continue
            procs.sort(reverse=True)
            lines.append("Топ-5 по CPU+RAM:")
            for c, m, pid, nm in procs[:5]:
                lines.append(f"  {c:5.1f}% CPU  {m:8.1f} MB  PID {pid:<7} {nm}")
            # выводы
            recs = []
            if ps:
                if ps.virtual_memory().percent > 85:
                    recs.append("Нехватка RAM — закройте лишние вкладки/приложения или найдите процесс-утеку (process_top).")
                top_cpu = max((p[0] for p in procs), default=0)
                if top_cpu > 60:
                    culprit = next((p for p in procs if p[0] == top_cpu), None)
                    if culprit:
                        recs.append(f"Процесс «{culprit[3]}» (PID {culprit[2]}) съедает CPU — проверьте, нужен ли он.")
                try:
                    if ps.disk_usage("/").percent > 90:
                        recs.append("Диск почти полон — освободите место (fs_search по большим файлам, fs_delete мусора).")
                except Exception:
                    pass
            if recs:
                lines.append("Рекомендации:")
                lines += [f"  - {r}" for r in recs]
            else:
                lines.append("Вывод: явных причин тормозов не обнаружено (CPU/RAM/диск в норме).")
            return ToolResult.ok_result("\n".join(lines))

    @reg.tool("list_monitors",
              "Все подключённые мониторы: индекс, размеры, расположение, основной ли.",
              risk=Risk.NONE, category="system",
              parameters={"type": "object", "properties": {}})
    class ListMonitors(Tool):
        async def execute(self, ctx: ToolContext) -> ToolResult:
            ms = ctx.platform.monitors or [{"index": 1, "name": "primary", "left": 0, "top": 0,
                                            "width": 1920, "height": 1080, "primary": True}]
            lines = [f"Монитор {m['index']} ({m.get('name','')}): {m['width']}x{m['height']} "
                     f"на ({m['left']},{m['top']})" + (" [основной]" if m.get("primary") else "")
                     for m in ms]
            return ToolResult.ok_result("\n".join(lines), monitors=ms)
