# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller-спецификация: AI Computer Agent → один EXE.

Собирает: ядро агента (stdlib), Web-дашборд (static), иконку,
плюс ВСЕ установленные опциональные модули (mss, psutil, playwright,
Pillow, pypdf, whisper...). То, что не установлено — просто не попадёт
в EXE, и агент деградирует gracefully.
"""
import os

datas = [
    ("agent/ui/web/static", "agent/ui/web/static"),
    ("assets", "assets"),
]
hiddenimports = ["webview"]

# Опциональные модули: включаем, если установлены
OPTIONAL = ["mss", "psutil", "pypdf", "PIL", "pytesseract",
            "faster_whisper", "sounddevice", "playwright", "pyautogui",
            "watchdog", "win32clipboard"]
for mod in OPTIONAL:
    try:
        __import__(mod)
        from PyInstaller.utils.hooks import collect_all
        d, h, b = collect_all(mod)
        datas += d
        hiddenimports += h
        print(f"[spec] + {mod}")
    except ImportError:
        print(f"[spec] - {mod} (не установлен, пропущен)")

a = Analysis(
    ["desktop.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "PyQt5", "PySide2", "pytest",
              "numpy.testing"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="AIComputerAgent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # оконное приложение, без консольного окна
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join("assets", "icon.ico"),
)
