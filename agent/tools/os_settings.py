"""Управление настройками ОС: звук, микрофон, дисплей, Bluetooth, Wi-Fi,
уведомления, автозагрузка, службы, яркость.

Реализовано шаблонами команд per-OS. Критичные изменения — только через
подтверждение (risk HIGH/CRITICAL в estimate_risk).
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess

from .base import Tool, ToolResult, ToolContext, Risk, _prop
from .registry import ToolRegistry


async def _run_cmd(cmd: list[str], timeout: float = 30) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
        return proc.returncode or 0, (out.decode("utf-8", "replace") + err.decode("utf-8", "replace")).strip()
    except FileNotFoundError:
        return 127, f"команда не найдена: {cmd[0]}"
    except asyncio.TimeoutError:
        return 124, f"таймаут: {' '.join(cmd)}"
    except Exception as e:  # noqa: BLE001
        return 1, str(e)


class OsSettingsBase:
    """Базовая реализация по платформе."""

    def _sys(self, ctx: ToolContext) -> str:
        return ctx.platform.system

    # ---------- звук ----------
    def volume_get(self, ctx) -> tuple[int, str]:
        if self._sys(ctx) == "windows":
            # PowerShell-чтение COM-объекта — сложный путь; используем ключи как fallback
            return 0, "Windows: для чтения уровня используйте powershell (Get-Volume не встроен). Попробуйте set."
        if self._sys(ctx) == "macos":
            return _run_cmd_sync(["osascript", "-e", "output volume of (get volume settings)"])
        r = subprocess.run(["pactl", "get-sink-volume", "@DEFAULT_SINK@"],
                           capture_output=True, text=True)
        if r.returncode == 0:
            m = re.search(r"(\d+)%", r.stdout)
            if m:
                return 0, m.group(1) + "%"
        r = subprocess.run(["amixer", "sget", "Master"], capture_output=True, text=True)
        m = re.search(r"\[(\d+)%\]", r.stdout)
        if m:
            return 0, m.group(1) + "%"
        return 1, "нет pactl/amixer"

    def volume_set(self, ctx, level: int) -> tuple[int, str]:
        level = max(0, min(100, int(level)))
        if self._sys(ctx) == "windows":
            # через отправку Media Volume Keys не точно; используем PowerShell COM
            ps = (
                "Add-Type -TypeDefinition 'using System;using System.Runtime.InteropServices;"
                "[DllImport(\"user32.dll\")]public static extern void keybd_event(byte b,byte s,uint e,uint i);';"
            )
            return 1, "Windows: точная установка уровня требует nircmd/COM — используйте клавиши (hotkey) или утилиту. Логика: уровень " + str(level)
        if self._sys(ctx) == "macos":
            return _run_cmd_sync(["osascript", "-e", f"set volume output volume {level}"])
        r = subprocess.run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{level}%"],
                           capture_output=True, text=True)
        if r.returncode == 0:
            return 0, f"громкость {level}%"
        r = subprocess.run(["amixer", "sset", "Master", f"{level}%"], capture_output=True, text=True)
        return (0, f"громкость {level}%") if r.returncode == 0 else (1, r.stderr.strip() or "amixer не сработал")

    def mute(self, ctx, on: bool) -> tuple[int, str]:
        if self._sys(ctx) == "macos":
            return _run_cmd_sync(["osascript", "-e", f"set volume muted {'true' if on else 'false'}"])
        r = subprocess.run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "1" if on else "0"],
                           capture_output=True, text=True)
        if r.returncode == 0:
            return 0, f"mute {'вкл' if on else 'выкл'}"
        r = subprocess.run(["amixer", "sset", "Master", "mute" if on else "unmute"],
                           capture_output=True, text=True)
        return (0, f"mute {'вкл' if on else 'выкл'}") if r.returncode == 0 else (1, r.stderr.strip())

    def mic_get(self, ctx) -> tuple[int, str]:
        if self._sys(ctx) == "linux":
            r = subprocess.run(["pactl", "list", "sources", "--short"], capture_output=True, text=True)
            lines = [l for l in r.stdout.splitlines() if l.strip()]
            return 0, "\n".join(lines[:20]) or "(источников нет)"
        return 1, "Linux: pactl; Windows: powershell Get-ComObject или настройки (launch_app settings)"

    # ---------- сеть ----------
    def wifi(self, ctx, action: str) -> tuple[int, str]:
        if action == "status":
            if self._sys(ctx) == "linux":
                r = subprocess.run(["ip", "addr", "show"], capture_output=True, text=True)
                m = re.search(r"inet\s+([\d.]+)", r.stdout)
                return 0, (f"IP: {m.group(1)}" if m else "адрес не найден") + "\n" + \
                    "\n".join(r.stdout.splitlines()[:10])
            if self._sys(ctx) == "windows":
                r = subprocess.run(["ipconfig"], capture_output=True, text=True)
                return 0, r.stdout[:1500]
            return 1, "неизвестная платформа"
        if action in ("on", "off"):
            if self._sys(ctx) == "linux":
                nm = ["nmcli", "radio", "wifi", action]
                return _run_cmd_sync(nm)
            if self._sys(ctx) == "windows":
                # имя адаптера ищем
                r = subprocess.run(["netsh", "interface", "show", "interfaces"],
                                   capture_output=True, text=True)
                m = re.search(r"^\s*\d+\s+\w+\s+(\S+?)\s+Wi-?Fi", r.stdout, re.M | re.I)
                name = m.group(1) if m else "Wi-Fi"
                state = "enable" if action == "on" else "disable"
                return _run_cmd_sync(["netsh", "interface", "set", "interface",
                                      f"name={name}", "admin", state])
        return 1, f"неизвестное действие: {action}"

    def bluetooth(self, ctx, action: str) -> tuple[int, str]:
        if self._sys(ctx) == "linux":
            return _run_cmd_sync(["bluetoothctl", action])
        if self._sys(ctx) == "windows":
            if action == "status":
                r = subprocess.run(["powershell", "-NoProfile", "-Command",
                                    "Get-PnpDevice -Class Bluetooth -ErrorAction SilentlyContinue | "
                                    "Select-Object Status,FriendlyName | Format-Table -AutoSize"],
                                   capture_output=True, text=True)
                return 0, r.stdout[:1000] or "устройства Bluetooth не найдены"
            return 1, "Windows: переключение через ms-settings:bluetooth (open_path 'ms-settings:bluetooth')"
        return 1, "не поддерживается на этой платформе"

    def brightness(self, ctx, level: int | None) -> tuple[int, str]:
        if self._sys(ctx) == "linux":
            files = ["/sys/class/backlight/" + d + "/brightness"
                     for d in os.listdir("/sys/class/backlight") if os.path.isdir(f"/sys/class/backlight/{d}")]
            if not files:
                r = subprocess.run(["brightnessctl"], capture_output=True, text=True)
                return 0, r.stdout or "яркость: недоступна"
            f = files[0]
            if level is None:
                return 0, f.read_text().strip()
            mx = int(open(f.replace("brightness", "max_brightness")).read().strip())
            open(f, "w").write(str(int(mx * level / 100)))
            return 0, f"яркость {level}%"
        if self._sys(ctx) == "windows":
            if level is None:
                return 0, "Windows: используйте клавиши яркости или ms-settings:display"
            return 1, "Windows: точное значение через ms-settings:display (открою настройки)"
        return 1, "недоступно"

    def list_startup(self, ctx) -> tuple[int, str]:
        if self._sys(ctx) == "windows":
            r = subprocess.run(["powershell", "-NoProfile", "-Command",
                                "Get-CimInstance Win32_StartupCommand | Select Name,Command | Format-Table -AutoSize"],
                               capture_output=True, text=True)
            return 0, r.stdout[:2000]
        if self._sys(ctx) == "linux":
            r = subprocess.run(["systemctl", "list-unit-files", "--state=enabled", "--type=service"],
                               capture_output=True, text=True)
            return 0, r.stdout[:2000]
        return 1, "недоступно"

    def list_services(self, ctx, state: str) -> tuple[int, str]:
        if self._sys(ctx) == "linux":
            if state:
                return _run_cmd_sync(["systemctl", "list-units", f"--state={state}", "--type=service", "--no-pager"])
            return _run_cmd_sync(["systemctl", "list-units", "--type=service", "--no-pager"])
        if self._sys(ctx) == "windows":
            args = ["powershell", "-NoProfile", "-Command", "Get-Service | Format-Table -AutoSize"]
            r = subprocess.run(args, capture_output=True, text=True)
            return 0, r.stdout[:3000]
        return 1, "недоступно"

    def service_set(self, ctx, name: str, action: str) -> tuple[int, str]:
        if self._sys(ctx) == "linux":
            return _run_cmd_sync(["systemctl", action, name])
        if self._sys(ctx) == "windows":
            act = {"start": "start", "stop": "stop", "restart": "restart"}.get(action, action)
            return _run_cmd_sync(["sc", act, name])
        return 1, "недоступно"


def _run_cmd_sync(cmd: list[str], timeout: float = 30) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or r.stderr or "").strip()
    except FileNotFoundError:
        return 127, f"не найдено: {cmd[0]}"
    except Exception as e:  # noqa: BLE001
        return 1, str(e)


def register_os_settings_tools(reg: ToolRegistry) -> None:
    impl = OsSettingsBase()

    @reg.tool("os_settings",
              "Управление настройками ОС: volume (get/set/mute), wifi (status/on/off), "
              "bluetooth (status/on/off), brightness, mic, startup (список автозагрузки), "
              "services (список/старт/стоп службы). Критичные изменения — с подтверждением.",
              risk=Risk.MEDIUM, category="os",
              parameters={"type": "object", "properties": {
                  "area": _prop("string",
                                "volume | wifi | bluetooth | brightness | mic | startup | services"),
                  "action": _prop("string", "get | set | on | off | list | start | stop | restart | status"),
                  "value": _prop("string", "Значение (уровень 0-100, имя службы)"),
                  "target": _prop("string", "Цель (имя службы/адаптера)")},
                  "required": ["area", "action"]})
    class OsSettings(Tool):
        def estimate_risk(self, ctx, args):
            area = str(args.get("area", ""))
            action = str(args.get("action", "")).lower()
            if area == "wifi" and action in ("on", "off"):
                return Risk.HIGH, "включение/выключение Wi-Fi"
            if area == "bluetooth" and action in ("on", "off"):
                return Risk.MEDIUM, "Bluetooth on/off"
            if area == "services" and action in ("start", "stop", "restart"):
                return Risk.HIGH, f"управление службой {args.get('target','?')}"
            if area == "startup" and action != "list":
                return Risk.HIGH, "изменение автозагрузки"
            return Risk.NONE, "" if action in ("get", "list", "status") else "настройка"

        async def execute(self, ctx: ToolContext, area: str, action: str,
                          value: str = "", target: str = "") -> ToolResult:
            a = action.lower()
            try:
                if area == "volume":
                    if a == "get":
                        code, out = impl.volume_get(ctx)
                    elif a in ("set", "volume"):
                        code, out = impl.volume_set(ctx, int(value or 50))
                    elif a in ("on", "off", "mute", "unmute"):
                        code, out = impl.mute(ctx, a in ("on", "mute"))
                    else:
                        code, out = 1, f"неизвестное действие: {a}"
                elif area == "wifi":
                    code, out = impl.wifi(ctx, a if a in ("on", "off") else "status")
                elif area == "bluetooth":
                    code, out = impl.bluetooth(ctx, a if a in ("on", "off") else "status")
                elif area == "brightness":
                    lvl = int(value) if value else None
                    code, out = impl.brightness(ctx, lvl)
                elif area == "mic":
                    code, out = impl.mic_get(ctx)
                elif area == "startup":
                    if a == "list":
                        code, out = impl.list_startup(ctx)
                    else:
                        code, out = 1, "автозагрузка: список доступен; изменение — через системные настройки (открою)"
                elif area == "services":
                    if a == "list" or a == "status":
                        code, out = impl.list_services(ctx, "")
                    elif a in ("start", "stop", "restart") and target:
                        code, out = impl.service_set(ctx, target, a)
                    else:
                        code, out = 1, "нужно: action=list|start|stop|restart и target=имя службы"
                else:
                    code, out = 1, f"неизвестная область: {area}"
            except Exception as e:  # noqa: BLE001
                code, out = 1, str(e)
            if code == 0:
                return ToolResult.ok_result(out or "выполнено")
            return ToolResult.fail(out or f"exit {code}")

    @reg.tool("os_notifications", "Настройки уведомлений (список/состояние) — информационно.",
              risk=Risk.NONE, category="os",
              parameters={"type": "object", "properties": {}})
    class OsNotifications(Tool):
        async def execute(self, ctx: ToolContext) -> ToolResult:
            if ctx.platform.system == "windows":
                r = subprocess.run(["powershell", "-NoProfile", "-Command",
                                    "(Get-ItemProperty 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Notifications').GetKeys 2>$null"],
                                   capture_output=True, text=True)
                return ToolResult.ok_result(r.stdout[:1500] or "список приложений с уведомлениями (чтение)")
            r = subprocess.run(["gsettings", "list-recursively", "org.gnome.desktop.notifications"],
                               capture_output=True, text=True)
            return ToolResult.ok_result(r.stdout[:1500] or "настройки уведомлений")
