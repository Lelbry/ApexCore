"""GPU-Roofline-калькулятор: архитектурный пик устройства для микробенчмарков.

Спецификация: ``new-app/docs/gpu_benchmark.md`` (методика) и по духу повторяет
CPU-Roofline (:mod:`application.roofline`).

Идея та же, что у CPU: эталоном выступает **архитектурный предел** конкретного
GPU, а не reference-машина. Балл = ``measured / peak`` — доля от теоретического
максимума (форма отношений строится на следующей стадии).

Пик считается по данным OpenCL ``clGetDeviceInfo`` (:class:`domain.gpu.GpuDeviceInfo`):

    fp32_peak_gflops = compute_units × (max_clock_mhz / 1000) ×
                       fp32_flops_per_cu_per_clock(arch) / 1000

Единицы: ``compute_units × GHz × (flops/такт)`` даёт FLOP/с; делённое на 1e9 —
GFLOPS. В формуле ``max_clock_mhz/1000`` = GHz, а деление на ``1000`` в конце —
это перевод (МГц→ГГц) × (нет, см. ниже). Аккуратно: ``CU × (MHz/1000 = GHz) ×
flops`` = CU × 1e9 такт/с × flops = FLOP/с; чтобы получить GFLOPS, делим на 1e9,
но так как GHz уже «×1e9», а нам нужно ×1e9/1e9 — итог просто ``CU × GHz × flops``
в единицах «G». То есть ``/1000`` НЕ нужен, если clock уже в GHz. Здесь clock
приходит в МГц, поэтому: ``CU × (MHz) × flops / 1000`` = GFLOPS. Проверено на
якорях (RTX 4070 Ti, UHD 770) — см. тесты.

    fp64_peak_gflops = fp32_peak_gflops × fp64_ratio(arch)
        → None, если ratio == 0 (нет аппаратного FP64) или device.fp64_supported is False.

    mem_bandwidth_peak_gb_s — из per-model таблицы (шина/тип памяти не
        экспонируются OpenCL). Неизвестная модель → None + note. Для встроенных
        GPU память общая с DRAM → None + note.

Таблица архитектур/моделей — ``data/gpu_arch.yaml`` (пакетные данные,
подключаются через ``importlib.resources``; в pyproject уже покрыты
``package-data`` для ``apexcore.data``).

Env-переопределения (для тестов и неизвестного железа), в духе roofline.py:
    - ``APEXCORE_GPU_FP32_PEAK_GFLOPS`` — принудительный FP32-пик (GFLOPS).
    - ``APEXCORE_GPU_FP64_PEAK_GFLOPS`` — принудительный FP64-пик (GFLOPS).
    - ``APEXCORE_GPU_MEM_PEAK_GB_S``   — принудительный пик пропускной способности (GB/s).
    - ``APEXCORE_GPU_ARCH``            — принудительный ключ архитектуры.

Никаких новых зависимостей: PyYAML уже в проекте.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from pathlib import Path

import yaml

from apexcore.application.roofline import compute_dram_peak
from apexcore.domain.gpu import GpuDeviceInfo, GpuPeak
from apexcore.domain.models import SystemInfo

# ─── Модели таблицы (внутренние, из gpu_arch.yaml) ───────────────────────────


@dataclass(frozen=True)
class _ArchEntry:
    """Параметры одной архитектуры."""

    key: str
    fp32_flops_per_cu_per_clock: float
    fp64_ratio: float
    vendor: str


@dataclass(frozen=True)
class _FamilyRule:
    """Правило семейного распознавания: скомпилированные regex → arch."""

    arch: str
    patterns: tuple[re.Pattern[str], ...]


@dataclass(frozen=True)
class _ModelOverride:
    """Точечное переопределение по модели (приоритетнее family-правил)."""

    pattern: re.Pattern[str]
    arch: str | None
    mem_bandwidth_peak_gb_s: float | None
    note: str | None


@dataclass(frozen=True)
class _GpuArchTable:
    """Разобранная таблица ``gpu_arch.yaml``."""

    architectures: dict[str, _ArchEntry]
    family_rules: tuple[_FamilyRule, ...]
    models: tuple[_ModelOverride, ...]


_YAML_NAME = "gpu_arch.yaml"


def _read_arch_yaml_text() -> str:
    """Прочитать текст ``gpu_arch.yaml`` из пакетных данных.

    Приоритет — ``importlib.resources`` (работает и в installed wheel, и в
    editable). Фолбэк на путь рядом с исходником — на случай нестандартной
    упаковки (тот же приём, что в :mod:`application.winsat_scoring`).
    """
    try:
        return (
            resources.files("apexcore.data")
            .joinpath(_YAML_NAME)
            .read_text(encoding="utf-8")
        )
    except (FileNotFoundError, ModuleNotFoundError, AttributeError):
        path = Path(__file__).resolve().parent.parent / "data" / _YAML_NAME
        return path.read_text(encoding="utf-8")


def _parse_arch_table(raw: dict) -> _GpuArchTable:
    """Разобрать «сырой» YAML в типизированную таблицу."""
    architectures: dict[str, _ArchEntry] = {}
    for key, body in (raw.get("architectures") or {}).items():
        if not isinstance(body, dict):
            continue
        architectures[key] = _ArchEntry(
            key=key,
            fp32_flops_per_cu_per_clock=float(body["fp32_flops_per_cu_per_clock"]),
            fp64_ratio=float(body.get("fp64_ratio", 0.0)),
            vendor=str(body.get("vendor", "")).lower(),
        )

    family_rules: list[_FamilyRule] = []
    for rule in raw.get("family_rules") or []:
        if not isinstance(rule, dict):
            continue
        arch = str(rule.get("arch", ""))
        patterns = tuple(
            re.compile(str(p), re.IGNORECASE) for p in (rule.get("patterns") or [])
        )
        if arch and patterns:
            family_rules.append(_FamilyRule(arch=arch, patterns=patterns))

    models: list[_ModelOverride] = []
    for model in raw.get("models") or []:
        if not isinstance(model, dict):
            continue
        match = model.get("match")
        if not match:
            continue
        mem = model.get("mem_bandwidth_peak_gb_s")
        models.append(
            _ModelOverride(
                pattern=re.compile(str(match), re.IGNORECASE),
                arch=(str(model["arch"]) if model.get("arch") else None),
                mem_bandwidth_peak_gb_s=(float(mem) if mem is not None else None),
                note=(str(model["note"]) if model.get("note") else None),
            )
        )

    return _GpuArchTable(
        architectures=architectures,
        family_rules=tuple(family_rules),
        models=tuple(models),
    )


@lru_cache(maxsize=1)
def _arch_table() -> _GpuArchTable:
    """Lazy-cache таблицы (читаем YAML один раз за процесс)."""
    raw = yaml.safe_load(_read_arch_yaml_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError("gpu_arch.yaml должен быть YAML-словарём")
    return _parse_arch_table(raw)


# ─── Нормализация имени ──────────────────────────────────────────────────────


def _normalize_gpu_name(name: str) -> str:
    """Lowercase + убрать (R)/(TM)/(C) + схлопнуть пробелы.

    Пример: ``"NVIDIA GeForce RTX 4070 Ti"`` → ``"nvidia geforce rtx 4070 ti"``;
    ``"Intel(R) UHD Graphics 770"`` → ``"intel uhd graphics 770"``.
    """
    cleaned = re.sub(r"\((r|tm|c)\)", "", name.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


# ─── Резолв архитектуры ──────────────────────────────────────────────────────


def _match_model(norm_name: str) -> _ModelOverride | None:
    """Найти первое подходящее per-model переопределение (или None)."""
    for model in _arch_table().models:
        if model.pattern.search(norm_name):
            return model
    return None


def resolve_gpu_arch(name: str, vendor: str) -> str | None:
    """Определить ключ архитектуры по имени/вендору устройства.

    Порядок:
    1. Env-override ``APEXCORE_GPU_ARCH`` (если совпадает с ключом в таблице).
    2. Per-model пин из ``gpu_arch.yaml`` (секция ``models``) — приоритетнее.
    3. Семейное распознавание (секция ``family_rules``) по нормализованному имени.

    ``vendor`` пока используется только как санити-фильтр: если arch найден,
    но принадлежит другому вендору (напр. модель-строка случайно совпала с
    чужим паттерном), а вендор устройства известен и не совпадает — правило
    отбрасывается. Возвращает ``None``, если архитектуру определить нельзя.
    """
    table = _arch_table()

    override = os.environ.get("APEXCORE_GPU_ARCH", "").strip()
    if override and override in table.architectures:
        return override

    norm_name = _normalize_gpu_name(name)
    norm_vendor = _normalize_vendor(vendor)

    # 2. Per-model пин.
    model = _match_model(norm_name)
    if model is not None and model.arch is not None and _arch_vendor_ok(model.arch, norm_vendor):
        return model.arch

    # 3. Family-правила.
    for rule in table.family_rules:
        if not _arch_vendor_ok(rule.arch, norm_vendor):
            continue
        for pattern in rule.patterns:
            if pattern.search(norm_name):
                return rule.arch

    return None


def _normalize_vendor(vendor: str) -> str | None:
    """Свести строку вендора к ``nvidia`` / ``amd`` / ``intel`` (или None).

    Матчинг по границам слов (``\\b``) намеренный: подстрочный поиск ловит
    ложные совпадения — напр. «Intel(R) Corpor**ati**on» содержит «ati».
    """
    v = vendor.lower()
    if not v.strip():
        return None
    if "intel" in v:
        return "intel"
    if "nvidia" in v:
        return "nvidia"
    if "advanced micro devices" in v or re.search(r"\b(amd|ati)\b", v):
        return "amd"
    return None


def _arch_vendor_ok(arch: str, norm_vendor: str | None) -> bool:
    """True, если arch совместим с известным вендором (или вендор неизвестен)."""
    if norm_vendor is None:
        return True
    entry = _arch_table().architectures.get(arch)
    if entry is None or not entry.vendor:
        return True
    return entry.vendor == norm_vendor


# ─── Env-overrides для пиков ─────────────────────────────────────────────────


def _env_float(var: str) -> float | None:
    """Прочитать положительный float из env-переменной (или None)."""
    raw = os.environ.get(var, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


# ─── Публичный расчёт пика ───────────────────────────────────────────────────


def compute_gpu_peak(device: GpuDeviceInfo) -> GpuPeak:
    """Посчитать архитектурные (Roofline) пики устройства.

    Порядок для каждого пика:
    1. Env-override (``APEXCORE_GPU_FP32_PEAK_GFLOPS`` / ``_FP64_PEAK_GFLOPS`` /
       ``_MEM_PEAK_GB_S``) — для тестов и неизвестного железа.
    2. Расчёт по таблице архитектуры + per-model данным.
    3. ``None`` + note, если данных недостаточно.

    Возвращает :class:`domain.gpu.GpuPeak`; при полном отсутствии данных все
    поля ``None`` (следующая стадия исключит соответствующие ratio из балла).
    ``source`` описывает происхождение: ``roofline`` / ``model_table`` /
    ``env_override`` / ``fallback`` (или их комбинация через ``+``).
    """
    notes: list[str] = []
    sources: set[str] = set()

    # ── Разрешаем архитектуру ──
    arch_key = resolve_gpu_arch(device.name, device.vendor)
    arch_entry = _arch_table().architectures.get(arch_key) if arch_key else None
    norm_name = _normalize_gpu_name(device.name)
    model = _match_model(norm_name)

    # ── FP32 ──
    fp32_env = _env_float("APEXCORE_GPU_FP32_PEAK_GFLOPS")
    fp32_peak: float | None
    if fp32_env is not None:
        fp32_peak = fp32_env
        sources.add("env_override")
        notes.append("FP32-пик задан через APEXCORE_GPU_FP32_PEAK_GFLOPS")
    else:
        fp32_peak = _compute_fp32_peak(device, arch_entry)
        if fp32_peak is not None:
            sources.add("roofline")
        else:
            _note_fp32_gap(notes, device, arch_key)

    # ── FP64 ──
    fp64_env = _env_float("APEXCORE_GPU_FP64_PEAK_GFLOPS")
    fp64_peak: float | None
    if fp64_env is not None:
        fp64_peak = fp64_env
        sources.add("env_override")
        notes.append("FP64-пик задан через APEXCORE_GPU_FP64_PEAK_GFLOPS")
    else:
        fp64_peak = _compute_fp64_peak(device, arch_entry, fp32_peak, notes)

    # ── Пропускная способность VRAM ──
    mem_env = _env_float("APEXCORE_GPU_MEM_PEAK_GB_S")
    mem_peak: float | None
    if mem_env is not None:
        mem_peak = mem_env
        sources.add("env_override")
        notes.append("Пик пропускной способности задан через APEXCORE_GPU_MEM_PEAK_GB_S")
    else:
        mem_peak = _resolve_mem_bandwidth(model, notes, sources)

    source = "+".join(sorted(sources)) if sources else "fallback"

    return GpuPeak(
        fp32_peak_gflops=fp32_peak,
        fp64_peak_gflops=fp64_peak,
        mem_bandwidth_peak_gb_s=mem_peak,
        arch=arch_key,
        source=source,
        notes=notes,
    )


def _compute_fp32_peak(
    device: GpuDeviceInfo, arch_entry: _ArchEntry | None
) -> float | None:
    """FP32-пик в GFLOPS по формуле Roofline (или None, если данных нет)."""
    if arch_entry is None:
        return None
    if device.compute_units <= 0 or device.max_clock_mhz <= 0:
        return None
    # compute_units × MHz × flops/такт / 1000 = GFLOPS (MHz/1000 = GHz).
    return (
        device.compute_units
        * device.max_clock_mhz
        * arch_entry.fp32_flops_per_cu_per_clock
        / 1000.0
    )


def _compute_fp64_peak(
    device: GpuDeviceInfo,
    arch_entry: _ArchEntry | None,
    fp32_peak: float | None,
    notes: list[str],
) -> float | None:
    """FP64-пик в GFLOPS = fp32_peak × fp64_ratio (или None + note)."""
    if not device.fp64_supported:
        notes.append("FP64 не поддерживается устройством (fp64_supported=False)")
        return None
    if arch_entry is None or fp32_peak is None:
        return None
    if arch_entry.fp64_ratio <= 0:
        notes.append(
            f"Архитектура {arch_entry.key} без аппаратного FP64 (ratio=0) → FP64-пик не задан"
        )
        return None
    return fp32_peak * arch_entry.fp64_ratio


def _resolve_mem_bandwidth(
    model: _ModelOverride | None,
    notes: list[str],
    sources: set[str],
) -> float | None:
    """Пропускная способность VRAM из per-model таблицы (или None + note)."""
    if model is None:
        notes.append(
            "Модель GPU не в таблице — пик пропускной способности VRAM неизвестен"
        )
        return None
    if model.note:
        notes.append(model.note)
    if model.mem_bandwidth_peak_gb_s is None:
        return None
    sources.add("model_table")
    return model.mem_bandwidth_peak_gb_s


def integrated_gpu_mem_bandwidth_peak_gb_s(
    system_info: SystemInfo,
) -> tuple[float, str] | None:
    """Пик пропускной способности памяти для встроенного GPU (iGPU) в ГБ/с.

    Встроенный GPU не имеет собственной VRAM — он делит системную DRAM с CPU,
    поэтому его потолок пропускной способности памяти физически ограничен
    пропускной способностью системной DRAM (шину памяти опросить через OpenCL
    нельзя, поэтому per-model таблица для iGPU даёт None). Переиспользуем
    CPU/RAM-Roofline: :func:`application.roofline.compute_dram_peak` (та же
    функция, что кормит ``stream_peak_gb_s`` в «Общей оценке» и стресс-балле).

    ``compute_dram_peak`` возвращает МБ/с — нормируем в ГБ/с (÷1000), как это
    уже делается в ``general_benchmark`` и ``stress_score``.

    Возвращает ``(gb_s, note)`` либо ``None``, если DRAM-пик определить нельзя
    (тогда поведение iGPU остаётся прежним: mem-пик None → r_mem None).
    Оценка консервативна: iGPU не может превысить общую с CPU DRAM, так что
    это честный потолок; ``note`` делает это прозрачным для UI/БД.
    """
    dram_peak_mb_s = compute_dram_peak(system_info)
    if not dram_peak_mb_s or dram_peak_mb_s <= 0:
        return None
    gb_s = dram_peak_mb_s / 1000.0
    note = (
        f"iGPU: пик памяти = пропускная способность системной DRAM "
        f"(~{gb_s:.0f} ГБ/с, память делится с CPU)"
    )
    return gb_s, note


def _note_fp32_gap(notes: list[str], device: GpuDeviceInfo, arch_key: str | None) -> None:
    """Добавить пояснение, почему FP32-пик не посчитан."""
    if arch_key is None:
        notes.append(
            f"Архитектура GPU не распознана по имени '{device.name}' → FP32-пик не задан"
        )
    elif device.compute_units <= 0 or device.max_clock_mhz <= 0:
        notes.append(
            "Недостаточно данных OpenCL (compute_units/max_clock_mhz = 0) → FP32-пик не задан"
        )


__all__ = [
    "compute_gpu_peak",
    "integrated_gpu_mem_bandwidth_peak_gb_s",
    "resolve_gpu_arch",
]
