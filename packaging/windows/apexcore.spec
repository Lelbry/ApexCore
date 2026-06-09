# PyInstaller spec для apexcore (Windows).
# Сборка: pyinstaller packaging/windows/apexcore.spec
# Результат: dist/apexcore/apexcore.exe и /_internal/

# -*- mode: python ; coding: utf-8 -*-

import os
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

datas = []
datas += collect_data_files("apexcore", includes=["**/*.sql", "**/*.yaml", "**/NOTICE.md"])
# WebUI static-bundle: HTML / CSS / JS / SVG / PNG / шрифты.
# Без этого `apexcore webui` отдаёт «Static UI not bundled.» — потому что
# server.py:STATIC_DIR = Path(__file__).parent / "static" не существует в
# frozen-сборке. collect_data_files собирает по wildcard'у — каждый exact
# тип файла должен быть указан явно.
datas += collect_data_files(
    "apexcore.interfaces.webui",
    includes=[
        "static/*.html",
        "static/**/*.html",
        "static/**/*.css",
        "static/**/*.js",
        "static/**/*.svg",
        "static/**/*.png",
        "static/**/*.ico",
        "static/**/*.woff",
        "static/**/*.woff2",
        "static/**/*.ttf",
        "static/**/*.json",
        "static/**/*.txt",
    ],
)

# DLL'и LHM и зависимости идут как binaries (PyInstaller не сжимает,
# подпись/целостность сохраняются). Список собираем динамически — в lib/
# их 24 штуки, перечислять руками нет смысла.
# WinRing0x64.sys в lib/ нет — LHM-lib v0.9.6 несёт его как embedded resource
# и сама извлекает + регистрирует kernel-сервис при первом admin-старте.
binaries = []
_lib_root = os.path.normpath(os.path.join(
    os.path.dirname(SPEC), "..", "..", "src", "apexcore", "infrastructure", "sensors", "lib"
))
if os.path.isdir(_lib_root):
    for _name in os.listdir(_lib_root):
        if _name.endswith(".dll"):
            binaries.append((os.path.join(_lib_root, _name), "apexcore/infrastructure/sensors/lib"))

hiddenimports = []
hiddenimports += collect_submodules("apexcore")
hiddenimports += [
    "scipy._lib.array_api_compat.numpy.fft",
    "scipy.special.cython_special",
    # pythonnet + clr_loader — динамические импорты в lhm.py.
    "clr",
    "clr_loader",
    "pythonnet",
]

block_cipher = None

# Иконка ApexCore для apexcore.exe — генерируется build_branding.ps1
# из packaging/branding/source/apex-logo.png. Встраивается в exe header,
# поэтому Windows Explorer и ярлыки на рабочем столе автоматически
# подхватывают её даже без явного IconFilename в Inno shortcut.
_icon_path = os.path.normpath(os.path.join(
    os.path.dirname(SPEC), "..", "..", "build", "branding", "apex-logo.ico"
))
if not os.path.isfile(_icon_path):
    _icon_path = None  # build_branding.ps1 не запускался — exe без иконки

a = Analysis(
    ["..\\..\\src\\apexcore\\__main__.py"],
    pathex=["..\\..\\src"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["matplotlib", "tkinter", "PyQt5", "PyQt6", "PySide6"],
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
    name="apexcore",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
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
    name="apexcore",
)
