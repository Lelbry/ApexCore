"""Хранилище пользовательских настроек меню (длительности тестов и пр.).

Файл живёт в ``data_dir / menu_settings.yaml`` рядом с БД и YAML-профилями.
Структура минимальная — её можно расширять, не ломая обратную совместимость:
неизвестные ключи игнорируются, отсутствующие — подставляются из default'ов.

Пример файла::

    durations:
      micro: 5.0
      monitor: 10.0
      stress_engine: 15.0
      bench: 30.0
    sampling_rate_sec: 0.5
    threads: 0  # 0 = auto

Зачем отдельно от ``ApexcoreSettings``
--------------------------------------
``ApexcoreSettings`` управляет «системными» путями (БД, лог-уровень,
profiles_path) и читается через pydantic-settings из env. Здесь — чисто
пользовательские предпочтения, которые меняются из меню в рантайме и
сохраняются обратно. Смешивать их в одной модели не стоит: разный
жизненный цикл.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from apexcore.shared.config import load_settings

# ─── Дефолты ────────────────────────────────────────────────────────────────

# Минимальное значение длительности — ниже теряет смысл (warmup съест больше,
# чем сам замер). 0.5 с — нижняя граница для микробенчмарков, 5 с — для стресса.
MIN_DURATION_MICRO = 0.5
MIN_DURATION_MONITOR = 1.0
MIN_DURATION_STRESS = 5.0
MIN_DURATION_BENCH = 5.0
MIN_DURATION_RAM_CACHE = 1.0
MIN_DURATION_SINGLE_MULTI = 1.0
# GPU: compute-фазы (FP32/FP64/VRAM) короче 1 с не дают устойчивого замера
# throughput'а OpenCL-кернела; PCIe ограничен шиной — 0.5 с достаточно.
MIN_DURATION_GPU_COMPUTE = 1.0
MIN_DURATION_GPU_PCIE = 0.5
# GPU-стресс (термостабильность): короче 10 с GPU не успевает прогреться и
# зайти в буст-settle — вердикт по стабильности будет недостоверным.
MIN_DURATION_GPU_STRESS = 10.0

MAX_DURATION = 3600.0  # 1 час — выше уже не имеет смысла для интерактивного тула


@dataclass
class DurationSettings:
    """Длительности по программам тестирования (в секундах)."""

    micro: float = 5.0          # один микробенчмарк CPU
    monitor: float = 10.0       # мониторинг телеметрии
    stress_engine: float = 15.0 # один стресс-движок
    bench: float = 30.0         # каждая фаза bench run
    ram_cache: float = 8.0      # одно из 16 измерений в Ram&Cache-тесте
    single_multi: float = 5.0   # один замер (Single или Multi) в тесте Single/Multi-Core
    gpu_compute: float = 5.0    # каждая compute-фаза GPU (FP32/FP64/VRAM)
    gpu_pcie: float = 2.0       # каждая PCIe-фаза GPU (H2D/D2H)
    gpu_stress: float = 60.0    # GPU-стресс (термостабильность), одна нагрузка


SPARKLINE_STYLES: tuple[str, ...] = ("auto", "unicode", "ascii")
CLI_THEMES: tuple[str, ...] = ("dark", "light")


@dataclass
class MenuSettings:
    """Полный набор пользовательских настроек меню."""

    durations: DurationSettings = field(default_factory=DurationSettings)
    sampling_rate_sec: float = 0.5
    threads: int = 0  # 0 = auto
    sparkline_style: str = "auto"  # "auto" | "unicode" | "ascii"
    webui_host: str = "127.0.0.1"
    webui_port: int = 8765  # default port for `apexcore webui`
    # Тема Rich-консоли (TUI меню + табличный вывод). Сохраняется здесь,
    # чтобы пользователь один раз переключил «Светлая» в настройках и
    # больше не возвращался к --theme флагу или ENV-переменной. ENV и
    # --theme имеют приоритет над этим значением (см. apply_saved_theme).
    cli_theme: str = "dark"  # "dark" | "light"


# ─── Описание программ для UI настроек ──────────────────────────────────────


@dataclass(frozen=True)
class ProgramDescriptor:
    """Описание одной «программы» — единицы, у которой можно поменять длительность.

    Используется только в UI настроек: даёт человеко-читаемое имя, ссылку на
    атрибут ``DurationSettings`` и техническое имя (для отладки/логов).
    """

    label: str          # "Микробенчмарки CPU"
    field: str          # "micro" — атрибут DurationSettings
    technical: str      # "apexcore micro run" — что именно стоит за этим в коде
    min_value: float
    description: str    # одна строка пояснения, что эта программа делает
    # Сколько раз эта длительность повторяется в «полном прогоне» программы.
    # Например для микробенчмарка CPU полный набор = 12 тестов, значит
    # полное время ≈ duration × 12. None / 1 — нет множителя (одиночный тест).
    full_run_count: int = 1
    full_run_unit: str = ""  # пояснение к множителю: «тестов», «измерений», «замера»


PROGRAMS: tuple[ProgramDescriptor, ...] = (
    ProgramDescriptor(
        label="Расширенный тест процессора (на один тест)",
        field="micro",
        technical="apexcore micro run",
        min_value=MIN_DURATION_MICRO,
        description="Длительность одного из 12 тестов CPU (память/FLOPS/IOPS/AES/SHA/фракталы).",
        full_run_count=12,
        full_run_unit="тестов",
    ),
    ProgramDescriptor(
        label="Стресс-тест одного движка",
        field="stress_engine",
        technical="apexcore stress run",
        min_value=MIN_DURATION_STRESS,
        description="Длительность нагрузки одного выбранного стресс-движка.",
    ),
    ProgramDescriptor(
        label="Полный прогон бенчмарка",
        field="bench",
        technical="apexcore bench run",
        min_value=MIN_DURATION_BENCH,
        description="Длительность каждой стресс-фазы внутри полного прогона по профилю.",
    ),
    ProgramDescriptor(
        label="Расширенный тест ОЗУ и кеша (на одно измерение)",
        field="ram_cache",
        technical="apexcore ram-cache run",
        min_value=MIN_DURATION_RAM_CACHE,
        description=(
            "Длительность одного из 16 измерений (4 уровня x 4 операции) в "
            "тесте Ram&Cache. Полный прогон ~= 16 x этого значения."
        ),
        full_run_count=16,
        full_run_unit="измерений",
    ),
    ProgramDescriptor(
        label="Тест Single-Core / Multi-Core (на один замер)",
        field="single_multi",
        technical="CPU advanced, пункт 4",
        min_value=MIN_DURATION_SINGLE_MULTI,
        description=(
            "Длительность одного из двух замеров (Single или Multi) в тесте "
            "одноядерной/многоядерной производительности. Полный тест ~= 2x этого значения."
        ),
        full_run_count=2,
        full_run_unit="замера",
    ),
    ProgramDescriptor(
        label="GPU-бенчмарк: вычислительная фаза (FP32/FP64/VRAM)",
        field="gpu_compute",
        technical="apexcore gpu run",
        min_value=MIN_DURATION_GPU_COMPUTE,
        description=(
            "Длительность одной вычислительной фазы GPU (FP32, FP64 или "
            "пропускная способность VRAM). Таких фаз в полном прогоне три."
        ),
        full_run_count=3,
        full_run_unit="фазы",
    ),
    ProgramDescriptor(
        label="GPU-бенчмарк: фаза PCIe (H2D/D2H)",
        field="gpu_pcie",
        technical="apexcore gpu run",
        min_value=MIN_DURATION_GPU_PCIE,
        description=(
            "Длительность одной фазы копирования по шине PCIe (host→device "
            "или device→host). Таких фаз в полном прогоне две."
        ),
        full_run_count=2,
        full_run_unit="фазы",
    ),
    ProgramDescriptor(
        label="GPU-стресс-тест (термостабильность)",
        field="gpu_stress",
        technical="apexcore gpu stress",
        min_value=MIN_DURATION_GPU_STRESS,
        description=(
            "Длительность длительной FP32-нагрузки на GPU для проверки "
            "термостабильности (нагрев, троттлинг, обвал частоты). "
            "Для честной оценки — минуты."
        ),
    ),
)


def full_run_duration(field: str, value: float) -> tuple[float, int, str] | None:
    """Вернуть (полная длительность, count, unit) для программы или None если множителя нет.

    Например: для ``field='micro'`` и ``value=5.0`` → ``(60.0, 12, 'тестов')``.
    """
    p = get_program(field)
    if p is None or p.full_run_count <= 1:
        return None
    return value * p.full_run_count, p.full_run_count, p.full_run_unit


def get_program(field_name: str) -> ProgramDescriptor | None:
    """Найти описание программы по имени поля (для unit-тестов и UI)."""
    for p in PROGRAMS:
        if p.field == field_name:
            return p
    return None


# ─── I/O ────────────────────────────────────────────────────────────────────


def settings_path() -> Path:
    """Полный путь к файлу настроек меню."""
    return settings_dir() / "menu_settings.yaml"


def settings_dir() -> Path:
    """Каталог, где лежит файл настроек меню (создаётся при необходимости)."""
    base = load_settings().data_dir
    base.mkdir(parents=True, exist_ok=True)
    return base


def open_settings_dir() -> Path:
    """Открыть каталог настроек в системном файловом менеджере.

    Возвращает путь к открытой папке (для UX-сообщений). Используется
    `os.startfile` на Windows, `xdg-open` на Linux, `open` на macOS —
    стандартные кросс-платформенные способы. Любая ошибка пробрасывается
    вверх; вызывающий код решает как её показать пользователю.
    """
    import os as _os
    import subprocess as _sp
    import sys as _sys

    folder = settings_dir()
    if _sys.platform == "win32":
        # os.startfile принимает str/PathLike, открывает в Explorer.
        _os.startfile(str(folder))  # type: ignore[attr-defined]
    elif _sys.platform == "darwin":
        _sp.Popen(["open", str(folder)])
    else:
        # Astra Linux / Ubuntu: xdg-open часть xdg-utils, доступна по дефолту.
        _sp.Popen(["xdg-open", str(folder)])
    return folder


def _coerce_durations(raw: dict[str, Any] | None) -> DurationSettings:
    if not isinstance(raw, dict):
        return DurationSettings()
    defaults = DurationSettings()
    return DurationSettings(
        micro=_safe_float(raw.get("micro"), defaults.micro, MIN_DURATION_MICRO),
        monitor=_safe_float(raw.get("monitor"), defaults.monitor, MIN_DURATION_MONITOR),
        stress_engine=_safe_float(
            raw.get("stress_engine"), defaults.stress_engine, MIN_DURATION_STRESS
        ),
        bench=_safe_float(raw.get("bench"), defaults.bench, MIN_DURATION_BENCH),
        ram_cache=_safe_float(
            raw.get("ram_cache"), defaults.ram_cache, MIN_DURATION_RAM_CACHE
        ),
        single_multi=_safe_float(
            raw.get("single_multi"), defaults.single_multi, MIN_DURATION_SINGLE_MULTI
        ),
        gpu_compute=_safe_float(
            raw.get("gpu_compute"), defaults.gpu_compute, MIN_DURATION_GPU_COMPUTE
        ),
        gpu_pcie=_safe_float(
            raw.get("gpu_pcie"), defaults.gpu_pcie, MIN_DURATION_GPU_PCIE
        ),
        gpu_stress=_safe_float(
            raw.get("gpu_stress"), defaults.gpu_stress, MIN_DURATION_GPU_STRESS
        ),
    )


def _safe_float(value: Any, default: float, minimum: float) -> float:
    """Привести значение к float в допустимом диапазоне; иначе вернуть default."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v < minimum or v > MAX_DURATION:
        return default
    return v


def _safe_int(value: Any, default: int, minimum: int = 0, maximum: int = 1024) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    if v < minimum or v > maximum:
        return default
    return v


def _safe_sparkline_style(value: Any, default: str = "auto") -> str:
    if isinstance(value, str) and value.lower() in SPARKLINE_STYLES:
        return value.lower()
    return default


def _safe_cli_theme(value: Any, default: str = "dark") -> str:
    if isinstance(value, str) and value.lower() in CLI_THEMES:
        return value.lower()
    return default


def apply_saved_theme(theme: str) -> None:
    """Применить тему из ``menu_settings.yaml`` к текущей Rich-консоли.

    Вызывается при старте TUI меню — даёт persistent-светлую без --theme
    флага. Если ENV ``APEXCORE_THEME`` или ``--theme`` уже выставлены (т.е.
    тема уже отличается от 'dark'), сохранённое значение НЕ применяется —
    у явного флага/env-var приоритет.
    """
    from apexcore.interfaces.cli.theme import apply_theme, current_theme

    if current_theme() != "dark":
        # Уже выставлено через --theme или APEXCORE_THEME — не трогаем.
        return
    if theme in CLI_THEMES and theme != "dark":
        apply_theme(theme)


def apply_sparkline_env(style: str) -> None:
    """Прокинуть выбранный sparkline-стиль в ``APEXCORE_SPARKLINE``.

    ``sparkline.py`` читает env, чтобы не тащить за собой импорт меню и не
    делать YAML-I/O при каждом рендере. Вызывается при старте TUI и при
    смене настройки из меню.
    """
    import os as _os

    if style in SPARKLINE_STYLES:
        _os.environ["APEXCORE_SPARKLINE"] = style


# ─── Webui-specific validators ─────────────────────────────────────────────


# Ограничиваем порт диапазоном «безопасных» (>=1024 чтобы не требовать root,
# <=65535 — верхняя граница TCP). 8765 — наш дефолт; пользователь может выбрать
# другой через UI настроек, если 8765 уже занят.
WEBUI_PORT_MIN = 1024
WEBUI_PORT_MAX = 65535


def _safe_port(value: Any, default: int) -> int:
    """Безопасно привести значение к порту в диапазоне [1024, 65535]."""
    return _safe_int(value, default, minimum=WEBUI_PORT_MIN, maximum=WEBUI_PORT_MAX)


# Хост ограничен localhost-вариантами по требованию single-user локального
# приложения. Если пользователь вписал что-то странное — возвращаем default.
_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "0.0.0.0", "::1"}


def _safe_host(value: Any, default: str) -> str:
    """Хост валидируется по белому списку (никаких сторонних IP)."""
    if not isinstance(value, str):
        return default
    v = value.strip().lower()
    return v if v in _ALLOWED_HOSTS else default


def update_webui_port(port: int) -> MenuSettings:
    """Изменить порт Web UI и сохранить. Бросает ValueError при невалидном порте."""
    if not isinstance(port, int) or port < WEBUI_PORT_MIN or port > WEBUI_PORT_MAX:
        raise ValueError(
            f"Порт должен быть целым числом в диапазоне [{WEBUI_PORT_MIN}, {WEBUI_PORT_MAX}]"
        )
    settings = load_menu_settings()
    settings.webui_port = port
    save_menu_settings(settings)
    return settings


def update_webui_host(host: str) -> MenuSettings:
    """Изменить хост Web UI и сохранить. Принимает только белый список."""
    if not isinstance(host, str) or host.strip().lower() not in _ALLOWED_HOSTS:
        raise ValueError(
            f"Хост должен быть одним из {sorted(_ALLOWED_HOSTS)} (single-user localhost-only)"
        )
    settings = load_menu_settings()
    settings.webui_host = host.strip().lower()
    save_menu_settings(settings)
    return settings


def load_menu_settings() -> MenuSettings:
    """Прочитать настройки из YAML или вернуть дефолты, если файла нет."""
    path = settings_path()
    if not path.exists():
        return MenuSettings()
    try:
        with path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError):
        # Битый YAML — молча возвращаем дефолты, чтобы не блокировать запуск меню.
        return MenuSettings()
    if not isinstance(raw, dict):
        return MenuSettings()
    durations = _coerce_durations(raw.get("durations"))
    defaults = MenuSettings()
    return MenuSettings(
        durations=durations,
        sampling_rate_sec=_safe_float(
            raw.get("sampling_rate_sec"),
            defaults.sampling_rate_sec,
            minimum=0.05,
        ),
        threads=_safe_int(raw.get("threads"), defaults.threads),
        sparkline_style=_safe_sparkline_style(
            raw.get("sparkline_style"), defaults.sparkline_style
        ),
        webui_host=_safe_host(raw.get("webui_host"), defaults.webui_host),
        webui_port=_safe_port(raw.get("webui_port"), defaults.webui_port),
        cli_theme=_safe_cli_theme(raw.get("cli_theme"), defaults.cli_theme),
    )


def save_menu_settings(settings: MenuSettings) -> Path:
    """Записать настройки в YAML; вернуть путь к файлу."""
    path = settings_path()
    payload = {
        "durations": asdict(settings.durations),
        "sampling_rate_sec": settings.sampling_rate_sec,
        "threads": settings.threads,
        "sparkline_style": settings.sparkline_style,
        "webui_host": settings.webui_host,
        "webui_port": settings.webui_port,
        "cli_theme": settings.cli_theme,
    }
    with path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(payload, fh, allow_unicode=True, sort_keys=False)
    return path


def reset_to_defaults() -> MenuSettings:
    """Сбросить настройки к дефолтам и сохранить."""
    settings = MenuSettings()
    save_menu_settings(settings)
    return settings


def update_duration(field_name: str, value: float) -> MenuSettings:
    """Изменить длительность одной программы и сохранить.

    Бросает ``ValueError``, если программа неизвестна или значение вне диапазона.
    """
    program = get_program(field_name)
    if program is None:
        raise ValueError(f"неизвестная программа: {field_name!r}")
    if value < program.min_value:
        raise ValueError(
            f"минимум для '{program.label}' — {program.min_value:.1f} с"
        )
    if value > MAX_DURATION:
        raise ValueError(f"максимум — {MAX_DURATION:.0f} с")
    settings = load_menu_settings()
    setattr(settings.durations, field_name, float(value))
    save_menu_settings(settings)
    return settings


__all__ = [
    "CLI_THEMES",
    "MAX_DURATION",
    "MIN_DURATION_BENCH",
    "MIN_DURATION_GPU_COMPUTE",
    "MIN_DURATION_GPU_PCIE",
    "MIN_DURATION_GPU_STRESS",
    "MIN_DURATION_MICRO",
    "MIN_DURATION_MONITOR",
    "MIN_DURATION_RAM_CACHE",
    "MIN_DURATION_SINGLE_MULTI",
    "MIN_DURATION_STRESS",
    "PROGRAMS",
    "SPARKLINE_STYLES",
    "WEBUI_PORT_MAX",
    "WEBUI_PORT_MIN",
    "DurationSettings",
    "MenuSettings",
    "ProgramDescriptor",
    "apply_saved_theme",
    "apply_sparkline_env",
    "full_run_duration",
    "get_program",
    "load_menu_settings",
    "open_settings_dir",
    "reset_to_defaults",
    "save_menu_settings",
    "settings_dir",
    "settings_path",
    "update_duration",
    "update_webui_host",
    "update_webui_port",
]
