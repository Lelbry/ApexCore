"""Детекция гибридной топологии CPU (Intel 12th Gen+: P-cores + E-cores).

Возвращает ``HybridTopology`` либо ``None``. ``None`` означает «не hybrid» —
все ядра одного класса (AMD, классический Intel) — либо «недоступно»
(нет API, ошибка). Вызывающий код в обоих случаях откатывается на простой
формат «N ядер / M потоков».

Источники:

- Windows: ``GetLogicalProcessorInformationEx(RelationProcessorCore)``
  через ``ctypes``. Доступен с Windows 7, не требует админ-прав.
  Группируем ядра по ``EfficiencyClass`` (UCHAR; больше = производительнее) —
  если уникальных классов ≥2, max-class = P-cores, min-class = E-cores.
  Потоки на каждом ядре — popcount маски ``GroupAffinity[0].Mask``.
- Linux 5.20+: ``/sys/devices/system/cpu/types/intel_{core,atom}_*/cpus``
  (см. ABI ``Documentation/admin-guide/pm/intel_pstate.rst``). Кол-во
  физических ядер выводится из threads-per-core: P-cores на Alder/Raptor/
  Meteor Lake имеют SMT (2 thread/core), E-cores — нет.

Любая ошибка (OSError, AttributeError, отсутствие API) тихо приводит к
``None`` — детекция P/E это enhancement, а не обязательная функциональность.
"""

from __future__ import annotations

import logging
import platform
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HybridTopology:
    """Разбивка гибридного CPU на P-ядра и E-ядра.

    ``p_cpus``/``e_cpus`` — индексы **логических** CPU (`0`, `1`, ...),
    которые принадлежат соответствующему типу ядра. Нужны для дальнейшего
    чтения per-CPU данных (базовая частота из реестра/sysfs). Опциональны
    (default — пустой tuple), чтобы не ломать старые конструкторы в тестах.
    """

    p_cores: int
    e_cores: int
    p_threads: int
    e_threads: int
    p_cpus: tuple[int, ...] = ()
    e_cpus: tuple[int, ...] = ()


def detect_hybrid_topology() -> HybridTopology | None:
    """Вернуть разбивку P/E ядер или ``None``, если CPU не hybrid."""
    system = platform.system().lower()
    try:
        if system == "windows":
            return _detect_windows()
        if system == "linux":
            return _detect_linux()
    except Exception:
        # Graceful degradation: P/E detection — enhancement, не обязательная функция.
        logger.debug("hybrid topology detection failed", exc_info=True)
    return None


# ─────────────────────────── Windows ────────────────────────────


_RELATION_PROCESSOR_CORE = 0


def _detect_windows() -> HybridTopology | None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    get_info = kernel32.GetLogicalProcessorInformationEx
    get_info.argtypes = [
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.DWORD),
    ]
    get_info.restype = wintypes.BOOL

    # Первый вызов: узнать требуемый размер буфера.
    size = wintypes.DWORD(0)
    get_info(_RELATION_PROCESSOR_CORE, None, ctypes.byref(size))
    if size.value == 0:
        return None

    buf = (ctypes.c_ubyte * size.value)()
    ok = get_info(_RELATION_PROCESSOR_CORE, buf, ctypes.byref(size))
    if not ok:
        return None

    return _parse_windows_buffer(bytes(buf), size.value)


def _parse_windows_buffer(buf: bytes, total: int) -> HybridTopology | None:
    """Распарсить буфер SYSTEM_LOGICAL_PROCESSOR_INFORMATION_EX[].

    Записи переменной длины: первые 8 байт — header (Relationship: DWORD,
    Size: DWORD), затем PROCESSOR_RELATIONSHIP. По полю Size прыгаем
    к следующей записи.

    PROCESSOR_RELATIONSHIP layout (после 8-байтного header):
      offset 8  Flags          BYTE   (бит 0 = LTP_PC_SMT)
      offset 9  EfficiencyClass BYTE
      offset 10 Reserved[20]
      offset 30 GroupCount     WORD
      offset 32 GroupAffinity[0]:
                  Mask   KAFFINITY (8 байт на x64, 4 на x86)
                  Group  WORD
                  Reserved[3]
    Для текущих систем GroupCount=1, нас интересует только маска [0].
    """
    # Для каждого EfficiencyClass храним список «ядер»: для каждого ядра —
    # tuple индексов логических CPU, входящих в его affinity-маску.
    cores_by_class: dict[int, list[tuple[int, ...]]] = {}
    ptr_size = 8 if platform.architecture()[0] == "64bit" else 4
    offset = 0
    while offset + 32 + ptr_size <= total:
        rec_size = int.from_bytes(buf[offset + 4 : offset + 8], "little")
        if rec_size <= 0 or offset + rec_size > total:
            break
        eff_class = buf[offset + 9]
        mask = int.from_bytes(
            buf[offset + 32 : offset + 32 + ptr_size], "little"
        )
        cpu_indices = tuple(_bits_to_indices(mask))
        if cpu_indices:
            cores_by_class.setdefault(eff_class, []).append(cpu_indices)
        offset += rec_size

    return _classify(cores_by_class)


def _bits_to_indices(mask: int) -> list[int]:
    """Вернуть позиции установленных бит маски (как список CPU-индексов)."""
    result = []
    idx = 0
    while mask:
        if mask & 1:
            result.append(idx)
        mask >>= 1
        idx += 1
    return result


def _classify(
    cores_by_class: dict[int, list[tuple[int, ...]]],
) -> HybridTopology | None:
    """Если ≥2 разных EfficiencyClass — выдать P (max-class) и E (min-class).

    Промежуточные классы (на текущих CPU не встречаются) сливаем с P-cores,
    чтобы не терять ядра в счётчике «всего».
    """
    classes = sorted(cores_by_class.keys())
    if len(classes) < 2:
        return None

    e_class = classes[0]
    e_cores = cores_by_class[e_class]
    p_cores: list[tuple[int, ...]] = []
    for cls in classes[1:]:
        p_cores.extend(cores_by_class[cls])
    if not p_cores or not e_cores:
        return None

    p_cpus = tuple(sorted({cpu for core in p_cores for cpu in core}))
    e_cpus = tuple(sorted({cpu for core in e_cores for cpu in core}))
    return HybridTopology(
        p_cores=len(p_cores),
        e_cores=len(e_cores),
        p_threads=sum(len(c) for c in p_cores),
        e_threads=sum(len(c) for c in e_cores),
        p_cpus=p_cpus,
        e_cpus=e_cpus,
    )


# ─────────────────────────── Linux ────────────────────────────


def _detect_linux(
    types_dir: Path = Path("/sys/devices/system/cpu/types"),
) -> HybridTopology | None:
    if not types_dir.is_dir():
        return None

    p_cpu_list: list[int] = []
    e_cpu_list: list[int] = []
    for sub in sorted(types_dir.iterdir()):
        if not sub.is_dir():
            continue
        cpus_file = sub / "cpus"
        if not cpus_file.exists():
            continue
        try:
            cpu_list = _parse_cpu_list(cpus_file.read_text().strip())
        except OSError:
            continue
        if sub.name.startswith("intel_core"):
            p_cpu_list.extend(cpu_list)
        elif sub.name.startswith("intel_atom"):
            e_cpu_list.extend(cpu_list)

    if not p_cpu_list or not e_cpu_list:
        return None

    # Текущие гибридные Intel (Alder/Raptor/Meteor Lake): P-cores с SMT,
    # E-cores без SMT. Если когда-нибудь появится SMT на E-cores, число
    # будет завышено в 2 раза — fallback в render по-прежнему корректен.
    p_threads = len(p_cpu_list)
    e_threads = len(e_cpu_list)
    p_cores = p_threads // 2 if p_threads >= 2 else p_threads
    e_cores = e_threads
    return HybridTopology(
        p_cores=p_cores,
        e_cores=e_cores,
        p_threads=p_threads,
        e_threads=e_threads,
        p_cpus=tuple(sorted(p_cpu_list)),
        e_cpus=tuple(sorted(e_cpu_list)),
    )


def _parse_cpu_list(spec: str) -> list[int]:
    """Распарсить запись sysfs вида '0-7,16-23' в список CPU-индексов."""
    result: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            result.extend(range(int(lo), int(hi) + 1))
        else:
            result.append(int(part))
    return result
