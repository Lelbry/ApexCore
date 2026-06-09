# PyInstaller spec для standalone apexcore-sensord.exe (Windows service host).
# Сборка: pyinstaller packaging/windows/apexcore-sensord.spec
# Результат: dist/apexcore-sensord/apexcore-sensord.exe и /_internal/
#
# Зачем отдельный EXE:
# ===================
# Pywin32 service-host (pythonservice.exe) использует системный Python и
# не активирует venv — это вызывает каскад проблем (servicemanager не найден,
# apexcore не в sys.path и т.д.). Standalone PyInstaller-EXE решает это
# радикально: бандл self-contained, binPath сервиса = sensord.exe напрямую,
# никаких внешних Python-зависимостей у конечного пользователя.
#
# Регистрация: `apexcore-sensord.exe install` (win32serviceutil сам поймёт
# что мы frozen и пропишет sys.executable в SCM как binPath).

# -*- mode: python ; coding: utf-8 -*-

import os
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

datas = []
# Только LHM-зависимости — yaml/sql apexcore'у в сервисе не нужны.
datas += collect_data_files("apexcore.infrastructure.sensors", includes=["NOTICE.md"])

# DLL'и LHM (24 шт) — нужны для Computer.Open().
binaries = []
_lib_root = os.path.normpath(os.path.join(
    os.path.dirname(SPEC), "..", "..", "src", "apexcore", "infrastructure", "sensors", "lib"
))
if os.path.isdir(_lib_root):
    for _name in os.listdir(_lib_root):
        if _name.endswith(".dll"):
            binaries.append((os.path.join(_lib_root, _name), "apexcore/infrastructure/sensors/lib"))

hiddenimports = [
    # apexcore-side: только то что реально импортирует сервис.
    "apexcore.services.sensord",
    "apexcore.services.shm_layout",
    "apexcore.services.shm_adapter",
    "apexcore.infrastructure.sensors.lhm",
    # pywin32 — служебная инфраструктура.
    "win32service",
    "win32serviceutil",
    "win32event",
    "win32api",
    "win32con",
    "win32file",
    "win32security",
    "servicemanager",
    "pywintypes",
    "pythoncom",
    # win32timezone — lazy-импортится из win32serviceutil.GetServiceClassString
    # через __import__, PyInstaller-анализ его НЕ видит. Без него
    # `apexcore-sensord.exe install` крашится с ModuleNotFoundError на самом
    # старте регистрации сервиса (зафиксировано на Ryzen 7700X + pywin32 308).
    "win32timezone",
    # pythonnet + clr_loader для LHM.
    "clr",
    "clr_loader",
    "pythonnet",
]
# apexcore submodules — нужны транзитивно через lhm.py
hiddenimports += collect_submodules("apexcore.infrastructure.sensors")
hiddenimports += collect_submodules("apexcore.services")

block_cipher = None

# Иконка ApexCore для apexcore-sensord.exe — таже что и для apexcore.exe.
# Сервис обычно не показывается пользователю, но если он откроет
# services.msc или task manager — увидит правильный логотип.
_icon_path = os.path.normpath(os.path.join(
    os.path.dirname(SPEC), "..", "..", "build", "branding", "apex-logo.ico"
))
if not os.path.isfile(_icon_path):
    _icon_path = None

a = Analysis(
    ["..\\..\\src\\apexcore\\services\\sensord.py"],
    pathex=["..\\..\\src"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Сервис не нуждается в численных библиотеках apexcore'а — гоним
    # лишний вес. Если в будущем потребуется что-то из тяжёлого — добавим.
    excludes=[
        "matplotlib", "tkinter", "PyQt5", "PyQt6", "PySide6",
        "scipy", "numpy.testing", "numba", "psutil",
        # Эти модули нужны только CLI, не сервису.
        "apexcore.interfaces",
        "apexcore.application",
        "apexcore.infrastructure.stress",
        "apexcore.infrastructure.microbench",
        "apexcore.infrastructure.persistence",
        "apexcore.infrastructure.exporters",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="apexcore-sensord",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # console=True — нужен для `debug` и `install` команд + для
    # просмотра ошибок при `Start-Service`. SCM запускает сервис как
    # session 0 process, console-окно ему не показывается пользователю,
    # вреда нет. Но для install/debug command-line режима наличие
    # console обязательно (иначе вывод теряется).
    console=True,
    disable_windowed_traceback=False,
    icon=_icon_path,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="apexcore-sensord",
)
