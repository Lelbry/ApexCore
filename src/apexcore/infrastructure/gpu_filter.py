"""Аннотация виртуальных видеоадаптеров в списке GPU.

Системы пользователей часто содержат «GPU», которые на деле — программные
адаптеры VR-стримеров (Virtual Desktop), удалённого доступа (RDP, Citrix,
Parsec, AnyDesk), гипервизоров (Hyper-V, VirtualBox, VMware) или Windows
fallback-драйвер «Microsoft Basic Display». В таблице системной информации
они мешают быстро увидеть реальные дискретные/интегрированные GPU.

Подход — мягкий: не убираем такие записи из списка, а добавляем суффикс
«(виртуальный)». Так пользователь видит полный список и сам понимает, что
существенно. Если паттерн не сработал (неизвестный виртуальный адаптер) —
строка отдаётся без пометки, что безопасно: при ложно-положительном
срабатывании мы только повесили лишний суффикс на реальный GPU; при
ложно-отрицательном — список выглядит как раньше.
"""

from __future__ import annotations

import re

_VIRTUAL_SUFFIX = " (виртуальный)"


# Паттерны подобраны по реальным записям из Win32_VideoController.Name и
# lspci -mm на распространённых сетапах. Список консервативный: добавляем
# новые имена только когда уверены, что они не пересекутся с настоящими GPU.
_VIRTUAL_GPU_RE = re.compile(
    r"(?:"
    r"^virtual\s"  # 'Virtual Desktop Monitor' и пр. — слово в начале
    r"|microsoft basic display"
    r"|microsoft remote display"
    r"|microsoft hyper-?v (video|virtual)"
    r"|citrix (indirect|display only)"
    r"|parsec virtual"
    r"|steam streaming"
    r"|moonlight virtual"
    r"|anydesk virtual"
    r"|dameware mirror"
    r"|teamviewer (virtual|mirror)"
    r"|virtualbox graphics"
    r"|vmware svga"
    r"|qemu"
    r"|splashtop display"
    r"|nomachine display"
    r")",
    re.IGNORECASE,
)


def annotate_virtual(name: str) -> str:
    """Вернуть исходное имя GPU + ' (виртуальный)' если оно матчит паттерн."""
    if not name:
        return name
    if name.endswith(_VIRTUAL_SUFFIX):
        return name  # уже помечен — не дублируем
    if _VIRTUAL_GPU_RE.search(name):
        return f"{name}{_VIRTUAL_SUFFIX}"
    return name


def is_virtual(name: str) -> bool:
    """Удобный хелпер для тестов / возможной фильтрации в будущем."""
    return bool(name) and _VIRTUAL_GPU_RE.search(name) is not None


# Паттерны для определения "интегрированной" iGPU. Дополняют is_discrete:
# если ни discrete, ни integrated не сматчилось — приоритет ставим как
# у integrated (consensus: лучше показать что-то реальное, чем оставить
# в конце).
_INTEGRATED_GPU_RE = re.compile(
    r"(?:"
    r"intel\(?r\)?\s*(uhd|hd|iris|xe)\s*graphics"
    r"|intel\s*(uhd|hd|iris|xe)"
    r"|amd\s*radeon\s*(vega|graphics)\b"            # Ryzen APU iGPU
    r"|radeon\s*(vega|graphics)\s*\d"               # Vega 8/11 APU
    r"|amd\s*ryzen\s.*graphics"                     # Ryzen APU integrated
    r")",
    re.IGNORECASE,
)


# Паттерны для дискретных GPU: NVIDIA полностью (GeForce/Quadro/Tesla/RTX/GTX/
# T-series), AMD Radeon RX/Pro/HD x000, Intel Arc.
_DISCRETE_GPU_RE = re.compile(
    r"(?:"
    r"nvidia"
    r"|geforce"
    r"|quadro"
    r"|tesla"
    r"|\brtx\b"
    r"|\bgtx\b"
    r"|amd\s*radeon\s*(rx|pro)\b"
    r"|radeon\s*(rx|pro)\b"
    r"|firepro"
    r"|intel\s*arc\b"
    r")",
    re.IGNORECASE,
)


def gpu_priority(name: str) -> int:
    """Приоритет GPU для сортировки в UI: 0=discrete, 1=integrated, 2=virtual.

    UI («apexcore info», правая панель Stress, dashboard topbar) показывает
    одну видеокарту как «основную» — берёт первый элемент из gpu_list.
    Логично чтобы первым был самый значимый GPU:
    дискретный (NVIDIA / AMD Radeon RX/Pro / Intel Arc) → интегрированный
    (Intel UHD/Iris, AMD Vega APU) → виртуальный (RDP, Hyper-V, Virtual
    Desktop Monitor).

    Сортировка стабильная: между equally-priority элементами порядок
    сохраняется (например 2 дискретных NVIDIA в SLI идут как пришли от WMI).

    Если имя пустое — приоритет 2 (junk идёт в конец).
    """
    if not name:
        return 2
    if is_virtual(name):
        return 2
    if _DISCRETE_GPU_RE.search(name):
        return 0
    if _INTEGRATED_GPU_RE.search(name):
        return 1
    # Неизвестный паттерн — даём приоритет 1 (как iGPU): лучше показать
    # реальную карту чем спрятать её за виртуальной.
    return 1
