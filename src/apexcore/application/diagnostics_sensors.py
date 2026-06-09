"""Диагностика источников температурных датчиков.

Собирает структурированный отчёт «что работает, что нет» по всем известным
бэкендам чтения температуры (LHM in-process, psutil, WMI perf-counter,
MSAcpi/CIM, Linux hwmon, nvidia-smi). Используется в:

- ``apexcore doctor`` (CLI-команда диагностики);
- пункте меню «Настройки → Диагностика датчиков»;
- проверке статуса в команде ``apexcore info``.

Контракт: функция-агрегатор не падает ни при каких условиях, любой бэкенд
проваливается в graceful-degrade. Возвращает ``SensorDiagnostics`` —
обычный dataclass без Pydantic, чтобы не усложнять зависимости.
"""

from __future__ import annotations

import logging
import platform
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from apexcore.domain.sensor_models import DegradedReason, SourceBackend

if TYPE_CHECKING:
    from apexcore.domain.models import MetricSnapshot

logger = logging.getLogger(__name__)


@dataclass
class BackendStatus:
    """Состояние одного бэкенда чтения температуры.

    Поле ``reason`` опционально и заполняется только когда отказ можно
    классифицировать через probe-фазу (HVCI, SAC, Defender и т.д.).
    Для backend'ов, где причина не ложится в ``DegradedReason`` (например
    «smartctl не в PATH») оставляем ``None`` и фиксируем человекочитаемый
    ``detail``.
    """

    name: str
    ok: bool
    sensor_count: int = 0
    sample: dict[str, float] = field(default_factory=dict)
    detail: str = ""
    reason: DegradedReason | None = None


@dataclass
class SensorDiagnostics:
    """Сводный отчёт диагностики датчиков."""

    platform: str
    has_cpu_temperature: bool
    has_gpu_temperature: bool
    cpu_temp_source: str | None
    gpu_temp_source: str | None
    backends: list[BackendStatus] = field(default_factory=list)
    advice: list[str] = field(default_factory=list)
    degraded_reasons: list[DegradedReason] = field(default_factory=list)
    """Все ``DegradedReason`` из ``BackendStatus.reason`` — дедуплицированно.

    Используется UX-баннером и деревом решений в ``docs/troubleshooting.md``.
    """

    @property
    def driver_active(self) -> bool:
        """«Драйвер активен» = реальная CPU-температура считывается.

        Это и есть критерий, видимый пользователю в строке Info.
        """
        return self.has_cpu_temperature


def _check_lhm_dll() -> BackendStatus:
    """Проверить, что DLL LibreHardwareMonitorLib скачана в lib/."""
    if platform.system().lower() != "windows":
        return BackendStatus(
            name="LibreHardwareMonitor (DLL)",
            ok=False,
            detail="не применимо на этой ОС",
        )
    lib_dir = (
        Path(__file__).resolve().parent.parent
        / "infrastructure"
        / "sensors"
        / "lib"
    )
    dll = lib_dir / "LibreHardwareMonitorLib.dll"
    if dll.exists():
        return BackendStatus(
            name="LibreHardwareMonitor (DLL)",
            ok=True,
            detail=f"DLL найдена в {dll.name} ({dll.stat().st_size // 1024} КБ)",
        )
    return BackendStatus(
        name="LibreHardwareMonitor (DLL)",
        ok=False,
        detail=(
            f"DLL не найдена в {lib_dir}. "
            "Запустите scripts/fetch_lhm.ps1 для скачивания "
            "LibreHardwareMonitor v0.9.6."
        ),
        reason=DegradedReason.NO_LHM_DLL,
    )


def _classify_lhm_no_cpu_reason() -> DegradedReason:
    """Классифицировать причину «LHM запустился, но CPU-сенсоров нет».

    Использует ``run_full_probe()`` чтобы понять, что именно блокирует
    регистрацию WinRing0: HVCI, SAC, Defender quarantine или просто нет
    admin-прав. См. ресерч §2.2.

    Приоритет: NO_LHM_DLL > HVCI > SAC > Defender > AV > NO_ADMIN >
    CPU_UNSUPPORTED. NO_LHM_DLL первая потому что отсутствие DLL — это
    **актуальная** блокирующая причина (LHM не запустится), не probe-факт.
    Без этой проверки `_check_lhm_runtime` в degraded mode выводит
    «Smart App Control блокирует» даже когда корень — отсутствие DLL.
    """
    try:
        from apexcore.infrastructure.sensors.lhm import _LIB_DLL

        if not _LIB_DLL.exists():
            return DegradedReason.NO_LHM_DLL
    except Exception as exc:  # pragma: no cover
        logger.debug("classify: проверка DLL упала: %s", exc)

    try:
        from apexcore.infrastructure.sensors.probe import run_full_probe

        probe = run_full_probe()
    except Exception as exc:
        logger.debug("classify_lhm_no_cpu_reason: probe upal: %s", exc)
        return DegradedReason.UNKNOWN
    # Приоритет — самый блокирующий сигнал первым.
    if probe.hvci_enabled:
        return DegradedReason.HVCI_BLOCKED
    if probe.sac_enabled:
        return DegradedReason.SAC_BLOCKED
    if probe.defender_quarantine_winring0:
        return DegradedReason.DEFENDER_BLOCKED
    if probe.av_vendor:
        return DegradedReason.AV_BLOCKED
    if not probe.is_admin:
        return DegradedReason.NO_ADMIN
    return DegradedReason.CPU_UNSUPPORTED


def _check_lhm_runtime() -> BackendStatus:
    """Попытаться прочитать сенсоры через LHM in-process.

    При отказе классифицирует причину через probe-фазу (HVCI/SAC/Defender/
    no_admin) — это даёт UX-слою конкретное сообщение вместо generic
    «не работает».
    """
    if platform.system().lower() != "windows":
        return BackendStatus(
            name="LHM runtime (pythonnet)",
            ok=False,
            detail="не применимо на этой ОС",
        )
    try:
        from apexcore.infrastructure.sensors.lhm import (
            read_lhm_temperatures,
            read_lhm_tjmax,
        )

        temps = read_lhm_temperatures()
    except Exception as exc:
        # Импорт упал — это .NET runtime / pythonnet проблема.
        return BackendStatus(
            name="LHM runtime (pythonnet)",
            ok=False,
            detail=f"исключение при чтении: {exc!r}",
            reason=DegradedReason.NO_DOTNET_RUNTIME,
        )
    cpu_keys = [k for k in temps if _is_cpu_temp_key(k)]
    gpu_keys = [k for k in temps if "gpu" in k.lower()]
    detail_parts: list[str] = []
    reason: DegradedReason | None = None
    if cpu_keys:
        detail_parts.append(f"CPU-сенсоров: {len(cpu_keys)}")
    else:
        # LHM запустился, но CPU-температуры нет → классифицируем причину.
        reason = _classify_lhm_no_cpu_reason()
        detail_parts.append(
            f"CPU-сенсоров нет ({reason.short()})"
        )
    if gpu_keys:
        detail_parts.append(f"GPU-сенсоров: {len(gpu_keys)}")
    try:
        tj = read_lhm_tjmax()
        if tj:
            detail_parts.append(f"Сенсоров температурного лимита: {len(tj)}")
    except Exception:
        pass
    return BackendStatus(
        name="LHM runtime (pythonnet)",
        ok=bool(temps),
        sensor_count=len(temps),
        sample=dict(list(temps.items())[:5]),
        detail="; ".join(detail_parts) or "нет данных",
        reason=reason,
    )


def _check_hwinfo_shm() -> BackendStatus:
    """Проверить чтение HWiNFO через Shared Memory (если HWiNFO запущен)."""
    if platform.system().lower() != "windows":
        return BackendStatus(
            name="HWiNFO SHM",
            ok=False,
            detail="не применимо на этой ОС",
        )
    try:
        from apexcore.infrastructure.sensors.shm import read_hwinfo_sensors

        temps = read_hwinfo_sensors()
    except Exception as exc:
        return BackendStatus(
            name="HWiNFO SHM",
            ok=False,
            detail=f"исключение: {exc!r}",
        )
    if not temps:
        return BackendStatus(
            name="HWiNFO SHM",
            ok=False,
            detail=(
                "не запущен / нет SHM. Установите HWiNFO с "
                "https://www.hwinfo.com (free, Sensors-only mode, "
                "Settings → Shared Memory Support)."
            ),
        )
    cpu_keys = [k for k in temps if _is_cpu_temp_key(k)]
    return BackendStatus(
        name="HWiNFO SHM",
        ok=True,
        sensor_count=len(temps),
        sample=dict(list(temps.items())[:5]),
        detail=f"CPU-сенсоров: {len(cpu_keys)}; всего: {len(temps)} (silicon)",
    )


def _check_aida64_shm() -> BackendStatus:
    """Проверить чтение AIDA64 через Shared Memory (если AIDA64 запущен).

    AIDA64 коммерческий, но если у пользователя есть лицензия и AIDA64
    в трее — apexcore подхватывает silicon-level T° + Vcore без admin.
    См. P1 §1.2.
    """
    if platform.system().lower() != "windows":
        return BackendStatus(
            name="AIDA64 SHM",
            ok=False,
            detail="не применимо на этой ОС",
        )
    try:
        from apexcore.infrastructure.sensors.shm import read_aida64_sensors

        temps = read_aida64_sensors()
    except Exception as exc:
        return BackendStatus(
            name="AIDA64 SHM",
            ok=False,
            detail=f"исключение: {exc!r}",
        )
    if not temps:
        return BackendStatus(
            name="AIDA64 SHM",
            ok=False,
            detail=(
                "не запущен / нет SHM. AIDA64 — коммерческий "
                "(https://www.aida64.com); если установлен — запустите его в "
                "трее (Preferences → Sensor Values → Enable shared memory)."
            ),
        )
    cpu_keys = [k for k in temps if _is_cpu_temp_key(k)]
    return BackendStatus(
        name="AIDA64 SHM",
        ok=True,
        sensor_count=len(temps),
        sample=dict(list(temps.items())[:5]),
        detail=f"CPU-сенсоров: {len(cpu_keys)}; всего: {len(temps)} (silicon)",
    )


def _check_coretemp_shm() -> BackendStatus:
    """Проверить чтение CoreTemp через Shared Memory (если CoreTemp запущен)."""
    if platform.system().lower() != "windows":
        return BackendStatus(
            name="CoreTemp SHM",
            ok=False,
            detail="не применимо на этой ОС",
        )
    try:
        from apexcore.infrastructure.sensors.shm import read_coretemp_sensors

        temps = read_coretemp_sensors()
    except Exception as exc:
        return BackendStatus(
            name="CoreTemp SHM",
            ok=False,
            detail=f"исключение: {exc!r}",
        )
    if not temps:
        return BackendStatus(
            name="CoreTemp SHM",
            ok=False,
            detail=(
                "не запущен / нет SHM. Установите CoreTemp с "
                "https://www.alcpu.com/CoreTemp/ (~3 MB freeware)."
            ),
        )
    return BackendStatus(
        name="CoreTemp SHM",
        ok=True,
        sensor_count=len(temps),
        sample=dict(list(temps.items())[:5]),
        detail=f"CPU-сенсоров: {len(temps)} (silicon)",
    )


def _check_psutil_temps() -> BackendStatus:
    try:
        import psutil
    except ImportError:
        return BackendStatus(name="psutil", ok=False, detail="psutil не установлен")
    try:
        sensors = psutil.sensors_temperatures()
    except Exception as exc:
        return BackendStatus(
            name="psutil sensors_temperatures",
            ok=False,
            detail=f"исключение: {exc!r}",
        )
    if not sensors:
        return BackendStatus(
            name="psutil sensors_temperatures",
            ok=False,
            detail="ни одного сенсора не вернулось (нормально для Windows)",
        )
    flat: dict[str, float] = {}
    for chip, entries in sensors.items():
        for e in entries:
            label = getattr(e, "label", "") or "core"
            flat[f"{chip}/{label}"] = float(getattr(e, "current", 0.0))
    return BackendStatus(
        name="psutil sensors_temperatures",
        ok=bool(flat),
        sensor_count=len(flat),
        sample=dict(list(flat.items())[:5]),
        detail=f"источников: {len(sensors)}",
    )


def _check_wmi_perf_counter() -> BackendStatus:
    if platform.system().lower() != "windows":
        return BackendStatus(
            name="WMI perf-counter Thermal Zone",
            ok=False,
            detail="не применимо на этой ОС",
        )
    try:
        from apexcore.infrastructure.sensors import wmi_temps

        temps = wmi_temps.read_perf_counter_thermal_zone()
    except Exception as exc:
        return BackendStatus(
            name="WMI perf-counter Thermal Zone",
            ok=False,
            detail=f"исключение: {exc!r}",
        )
    return BackendStatus(
        name="WMI perf-counter Thermal Zone",
        ok=bool(temps),
        sensor_count=len(temps),
        sample=dict(list(temps.items())[:5]),
        detail=(
            "ACPI thermal zone (не реальная T° ядер CPU, "
            "не подходит для оценки нагрузки)"
            if temps
            else "перфкаунтер не отдаёт данных"
        ),
    )


def _check_msacpi_cim() -> BackendStatus:
    if platform.system().lower() != "windows":
        return BackendStatus(
            name="WMI MSAcpi (CIM-fallback)",
            ok=False,
            detail="не применимо на этой ОС",
        )
    try:
        from apexcore.infrastructure.sensors import wmi_temps

        temps = wmi_temps._read_msacpi_via_cim()
    except Exception as exc:
        return BackendStatus(
            name="WMI MSAcpi (CIM-fallback)",
            ok=False,
            detail=f"исключение: {exc!r}",
        )
    return BackendStatus(
        name="WMI MSAcpi (CIM-fallback)",
        ok=bool(temps),
        sensor_count=len(temps),
        sample=dict(list(temps.items())[:5]),
        detail=(
            "ACPI thermal zone (см. примечание выше)"
            if temps
            else "MSAcpi не доступен"
        ),
    )


def _check_hwmon() -> BackendStatus:
    if platform.system().lower() != "linux":
        return BackendStatus(
            name="Linux hwmon (/sys/class/hwmon)",
            ok=False,
            detail="не применимо на этой ОС",
        )
    root = Path("/sys/class/hwmon")
    if not root.exists():
        return BackendStatus(
            name="Linux hwmon (/sys/class/hwmon)",
            ok=False,
            detail="каталог не существует",
        )
    # Должны совпадать с `_HWMON_*_CHIPS` из `infrastructure/adapters/linux.py`.
    # Дублируем тут чтобы не тянуть platform-специфичный модуль в diagnostics
    # (адаптеры могут импортировать платформенные зависимости вроде pythonnet).
    _gpu_prefix_by_chip = {
        "amdgpu": "gpuamd",
        "radeon": "gpuamd",
        "i915": "gpuintel",
        "xe": "gpuintel",
    }
    _disk_chips = {"nvme", "drivetemp"}
    _cpu_chips = {"k10temp", "zenpower", "coretemp", "cpu_thermal"}
    _mb_chips = {
        "acpitz", "pch_haswell", "pch_skylake", "pch_cannonlake",
        "pch_cometlake", "pch_icelake", "pch_tigerlake", "pch_alderlake",
    }
    try:
        names: list[str] = []
        sample: dict[str, float] = {}
        gpu_chips_found: list[str] = []
        disk_chips_found: list[str] = []
        cpu_chips_found: list[str] = []
        mb_chips_found: list[str] = []
        for d in sorted(root.iterdir()):
            try:
                chip = (d / "name").read_text(encoding="utf-8").strip()
            except OSError:
                continue
            names.append(chip)
            gpu_prefix = _gpu_prefix_by_chip.get(chip)
            is_disk = chip in _disk_chips
            is_cpu = chip in _cpu_chips
            is_mb = chip in _mb_chips
            if gpu_prefix:
                gpu_chips_found.append(chip)
            if is_disk:
                disk_chips_found.append(chip)
            if is_cpu:
                cpu_chips_found.append(chip)
            if is_mb:
                mb_chips_found.append(chip)
            # Читаем все temp*_input этого hwmon-устройства, чтобы заполнить
            # `sample`. Формат ключей совпадает с `LinuxAdapter._read_hwmon`
            # (LHM-совместимые префиксы cpu/gpuamd/gpuintel/storage).
            for tin in sorted(d.glob("temp*_input")):
                try:
                    raw = int(tin.read_text(encoding="utf-8").strip())
                except (OSError, ValueError):
                    continue
                lbl_path = tin.with_name(tin.name.replace("_input", "_label"))
                label = (
                    lbl_path.read_text(encoding="utf-8").strip()
                    if lbl_path.exists()
                    else tin.stem.replace("_input", "")
                )
                if gpu_prefix:
                    key = f"{gpu_prefix}/{label}"
                elif is_disk:
                    key = f"storage/{chip}_{label}"
                elif is_cpu:
                    key = f"cpu/{label}"
                elif is_mb:
                    key = f"motherboard/{label}"
                else:
                    key = f"{chip}.{label}"
                sample[key] = raw / 1000.0
        if not names:
            return BackendStatus(
                name="Linux hwmon (/sys/class/hwmon)",
                ok=False,
                detail="ни одного hwmon-устройства",
            )
        cpu = [n for n in names if any(t in n for t in ("coretemp", "k10temp", "zenpower"))]
        detail_parts: list[str] = []
        if cpu:
            detail_parts.append(f"CPU-устройств: {', '.join(cpu)}")
        if gpu_chips_found:
            detail_parts.append(f"GPU-устройств: {', '.join(sorted(set(gpu_chips_found)))}")
        if disk_chips_found:
            detail_parts.append(f"Диск-устройств: {', '.join(sorted(set(disk_chips_found)))}")
        if not detail_parts:
            detail_parts.append(f"всего {len(names)} hwmon, CPU/GPU/Disk-датчиков нет")
        return BackendStatus(
            name="Linux hwmon (/sys/class/hwmon)",
            ok=bool(cpu) or bool(gpu_chips_found) or bool(disk_chips_found),
            sensor_count=len(sample) or len(names),
            sample=sample,
            detail="; ".join(detail_parts),
        )
    except Exception as exc:
        return BackendStatus(
            name="Linux hwmon (/sys/class/hwmon)",
            ok=False,
            detail=f"исключение: {exc!r}",
        )


def _check_nvidia_smi() -> BackendStatus:
    if shutil.which("nvidia-smi") is None:
        return BackendStatus(
            name="nvidia-smi",
            ok=False,
            detail="nvidia-smi не найден в PATH (нет NVIDIA-драйвера или встроенная GPU)",
        )
    try:
        r = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,temperature.gpu,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except Exception as exc:
        return BackendStatus(name="nvidia-smi", ok=False, detail=f"ошибка: {exc!r}")
    if r.returncode != 0:
        return BackendStatus(
            name="nvidia-smi",
            ok=False,
            detail=f"ненулевой код возврата: {r.returncode}",
        )
    line = (r.stdout or "").strip().splitlines()
    if not line:
        return BackendStatus(name="nvidia-smi", ok=False, detail="пустой ответ")
    parts = [p.strip() for p in line[0].split(",")]
    if len(parts) < 3:
        return BackendStatus(
            name="nvidia-smi", ok=False, detail=f"неожиданный формат: {line[0]!r}"
        )
    try:
        return BackendStatus(
            name="nvidia-smi",
            ok=True,
            sensor_count=len(line),
            sample={parts[0]: float(parts[1])},
            detail=f"{parts[0]}: T={parts[1]}°C, нагрузка={parts[2]}%",
        )
    except ValueError:
        return BackendStatus(
            name="nvidia-smi", ok=False, detail=f"не парсится: {line[0]!r}"
        )


def _is_cpu_temp_key(key: str) -> bool:
    """Локальная копия эвристики из thermal_watchdog (без импорта чтобы не плодить циклы)."""
    from apexcore.application.thermal_watchdog import _is_cpu_temp_key as _impl

    return _impl(key)


def _check_pynvml() -> BackendStatus:
    """Проверка `nvidia-ml-py` (pynvml): import, init, перечисление GPU."""
    try:
        import pynvml  # noqa: F401
    except ImportError:
        return BackendStatus(
            name="pynvml",
            ok=False,
            detail="пакет 'nvidia-ml-py' не установлен",
        )
    try:
        from apexcore.infrastructure.sensors import nvidia_ml

        if not nvidia_ml.is_available():
            return BackendStatus(
                name="pynvml",
                ok=False,
                detail="NVIDIA-драйвер не найден или NVML не инициализируется",
            )
        names = nvidia_ml.read_nvml_device_names()
        temps = nvidia_ml.read_nvml_temperatures()
        count = len(names)
        if count == 0:
            return BackendStatus(
                name="pynvml",
                ok=False,
                detail="NVML работает, но устройств не найдено",
            )
        first = next(iter(names.values()), "GPU?")
        first_temp = next(iter(temps.values()), None)
        if first_temp is not None:
            detail = f"{first}: T={first_temp:.0f}°C, устройств={count}"
        else:
            detail = f"{first}, устройств={count}"
        return BackendStatus(
            name="pynvml",
            ok=True,
            sensor_count=len(temps),
            sample=dict(list(temps.items())[:3]),
            detail=detail,
        )
    except Exception as exc:
        return BackendStatus(
            name="pynvml",
            ok=False,
            detail=f"NVML ошибка: {exc!r}",
        )


def _check_smartctl() -> BackendStatus:
    """Проверка `smartctl` (smartmontools): PATH/sbin + scan + sample-температуры.

    На Astra Linux SE 1.8 (kernel 6.1+) для чтения NVMe SMART log требуется
    full root — `cap_sys_rawio+ep` ставится `setcap`'ом, но ядро всё равно
    блокирует SMART log без CAP_SYS_ADMIN. В этом случае smartctl видит
    устройство через `--scan`, но `-A` возвращает только Identify-секцию.
    Тогда detail подсказывает пользователю что T° дисков всё равно есть —
    через kernel hwmon (`/sys/class/hwmon/*/name=nvme`).
    """
    from apexcore.infrastructure.sbin_lookup import has_sbin
    if not has_sbin("smartctl"):
        return BackendStatus(
            name="smartctl",
            ok=False,
            detail="smartctl не найден (winget install smartmontools / apt install smartmontools)",
        )
    try:
        from apexcore.infrastructure.sensors import smartctl as smartctl_mod

        temps = smartctl_mod.read_smartctl_temperatures()
        if not temps:
            # Особый случай: smartctl установлен, но без T°. На Linux часто
            # это означает «нужен root для SMART log», и при этом kernel
            # hwmon уже отдаёт T° NVMe бесплатно. Подскажем альтернативу.
            hint = "smartctl установлен, но устройств с T° не найдено"
            if platform.system().lower() == "linux":
                hwmon_root = Path("/sys/class/hwmon")
                has_nvme_hwmon = False
                if hwmon_root.exists():
                    try:
                        for d in hwmon_root.iterdir():
                            nf = d / "name"
                            if nf.exists() and nf.read_text(encoding="utf-8").strip() in {"nvme", "drivetemp"}:
                                has_nvme_hwmon = True
                                break
                    except OSError:
                        pass
                if has_nvme_hwmon:
                    hint += (
                        " (T° NVMe/SATA уже доступна через kernel hwmon — "
                        "smartctl для T° не нужен; для SMART attributes "
                        "запускать от root)"
                    )
                else:
                    hint += " — нужен root для чтения SMART log"
            return BackendStatus(
                name="smartctl",
                ok=False,
                detail=hint,
            )
        first_key, first_val = next(iter(temps.items()))
        return BackendStatus(
            name="smartctl",
            ok=True,
            sensor_count=len(temps),
            sample=dict(list(temps.items())[:3]),
            detail=f"{first_key.split('/')[1]}: T={first_val:.0f}°C, устройств={len(temps)}",
        )
    except Exception as exc:
        return BackendStatus(
            name="smartctl",
            ok=False,
            detail=f"smartctl ошибка: {exc!r}",
        )


def _check_throttle_detector() -> BackendStatus:
    """Проверка модуля детектора причины CPU-throttle."""
    try:
        from apexcore.application.throttle_detector import read_throttle_state

        state = read_throttle_state()
        return BackendStatus(
            name="throttle_detector",
            ok=True,
            sensor_count=1,
            sample={},  # cause не float, в sample не кладём
            detail=f"текущая причина: {state.cause.value}"
            + (f" — {state.detail}" if state.detail else ""),
        )
    except Exception as exc:
        return BackendStatus(
            name="throttle_detector",
            ok=False,
            detail=f"ошибка: {exc!r}",
        )


def diagnose_sensors() -> SensorDiagnostics:
    """Прогнать все бэкенды и собрать сводный отчёт.

    Эта функция — главная точка входа для всех UI-сценариев (`doctor`,
    меню «Настройки → Диагностика», info-команда).
    """
    sysname = platform.system()
    backends = [
        _check_lhm_dll(),
        _check_lhm_runtime(),
        _check_hwinfo_shm(),
        _check_coretemp_shm(),
        _check_aida64_shm(),
        _check_psutil_temps(),
        _check_wmi_perf_counter(),
        _check_msacpi_cim(),
        _check_hwmon(),
        _check_nvidia_smi(),
        _check_pynvml(),
        _check_smartctl(),
        _check_throttle_detector(),
    ]
    # Фильтруем «не применимо» бэкенды — они не должны мозолить глаза.
    backends = [b for b in backends if "не применимо" not in b.detail]

    # Где-то нашлась ли реальная CPU-температура? Приоритет источников
    # совпадает с fallback chain `WindowsAdapter._read_sensors` (см. план §4):
    # HWiNFO SHM → CoreTemp SHM → LHM → psutil → hwmon (Linux).
    cpu_source = None
    has_cpu = False
    for b in backends:
        if not b.ok:
            continue
        if b.name == "HWiNFO SHM" and any(_is_cpu_temp_key(k) for k in b.sample):
            has_cpu = True
            cpu_source = "HWiNFO Shared Memory (silicon)"
            break
        if b.name == "CoreTemp SHM" and any(_is_cpu_temp_key(k) for k in b.sample):
            has_cpu = True
            cpu_source = "CoreTemp Shared Memory (silicon)"
            break
        if b.name == "AIDA64 SHM" and any(_is_cpu_temp_key(k) for k in b.sample):
            has_cpu = True
            cpu_source = "AIDA64 Shared Memory (silicon)"
            break
        if b.name.startswith("LHM") and any(
            _is_cpu_temp_key(k) for k in b.sample
        ):
            has_cpu = True
            cpu_source = "LibreHardwareMonitor (DTS ядер CPU)"
            break
        if b.name.startswith("Linux hwmon") and "CPU-устройств:" in b.detail:
            has_cpu = True
            cpu_source = b.detail
            break
        if b.name.startswith("psutil") and any(
            _is_cpu_temp_key(k) for k in b.sample
        ):
            has_cpu = True
            cpu_source = "psutil sensors_temperatures"
            break

    # GPU. Приоритет: pynvml (структурированный API, util/power) → LHM
    # (memory_junction на consumer NVIDIA) → nvidia-smi (legacy) → Linux hwmon
    # (amdgpu/radeon/i915/xe — для случая Astra + Ryzen APU без NVIDIA).
    gpu_source = None
    has_gpu = False
    pynvml_b = next((b for b in backends if b.name == "pynvml" and b.ok), None)
    if pynvml_b is not None:
        has_gpu = True
        gpu_source = f"pynvml: {pynvml_b.detail}"
    else:
        for b in backends:
            if not b.ok:
                continue
            if b.name.startswith("LHM") and any("gpu" in k.lower() for k in b.sample):
                has_gpu = True
                gpu_source = "LibreHardwareMonitor (через NVAPI)"
                break
            if b.name == "nvidia-smi":
                has_gpu = True
                gpu_source = b.detail
                break
            # Linux hwmon: amdgpu/radeon → `gpuamd/<label>`, i915/xe →
            # `gpuintel/<label>` (см. `LinuxAdapter._read_hwmon`,
            # `_HWMON_GPU_PREFIX_BY_CHIP`). Покрывает AMD APU (Ryzen +
            # Radeon iGPU) и Intel iGPU без сторонних драйверов.
            if "hwmon" in b.name.lower() and any(
                k.startswith("gpuamd/") or k.startswith("gpuintel/")
                for k in b.sample
            ):
                has_gpu = True
                gpu_prefixes = sorted({
                    k.split("/", 1)[0]
                    for k in b.sample
                    if k.startswith(("gpuamd/", "gpuintel/"))
                })
                # Восстановим human-readable label из префикса.
                _label_map = {"gpuamd": "AMD", "gpuintel": "Intel"}
                vendors = ", ".join(_label_map.get(p, p) for p in gpu_prefixes)
                gpu_source = f"Linux hwmon: {vendors}"
                break

    # Дедуплицированный список причин отказа из всех backend'ов.
    degraded_reasons: list[DegradedReason] = []
    seen_reasons: set[DegradedReason] = set()
    for b in backends:
        if b.reason is not None and b.reason not in seen_reasons:
            degraded_reasons.append(b.reason)
            seen_reasons.add(b.reason)

    # Советы пользователю — дифференцированные по DegradedReason. См. план §5.1
    # и `docs/troubleshooting.md` (дерево решений по reason).
    advice: list[str] = []
    if sysname == "Windows" and not has_cpu:
        lhm_dll = next(
            (b for b in backends if b.name == "LibreHardwareMonitor (DLL)"), None
        )
        lhm_run = next(
            (b for b in backends if b.name == "LHM runtime (pythonnet)"), None
        )

        # Первая рекомендация — установить SHM-source (HWiNFO/CoreTemp).
        # Это работает БЕЗ admin и совместимо с HVCI/SAC, поэтому оно
        # должно быть первым в advice — самый лёгкий путь.
        advice.append(
            "Самый быстрый способ получить точные CPU-температуры: "
            "установите HWiNFO (https://www.hwinfo.com, free) или "
            "CoreTemp (https://www.alcpu.com/CoreTemp/, ~3 МБ). "
            "Benchkit автоматически их обнаружит через Shared Memory."
        )

        # Дальше — конкретные сценарии по DegradedReason.
        if DegradedReason.NO_LHM_DLL in seen_reasons:
            # Скрипт лежит внутри пакета scripts/, а не в корне репо.
            # Команда работает из корня репо в стандартной Windows PowerShell
            # 5.1; `pwsh` (PowerShell 7+) на Win10/11 из коробки нет.
            advice.append(
                "DLL LibreHardwareMonitor отсутствует — выполните из корня "
                "репозитория: `powershell -ExecutionPolicy Bypass -File "
                "\scripts\\fetch_lhm.ps1`. Скрипт скачает "
                "LibreHardwareMonitor v0.9.6 и положит DLL в "
                "src/apexcore/infrastructure/sensors/lib/. "
                "Или запустите `apexcore doctor --repair`."
            )
        if DegradedReason.HVCI_BLOCKED in seen_reasons:
            advice.append(
                "HVCI / Memory Integrity активен — WinRing0 не загрузится "
                "в принципе. Установите PawnIO (https://pawnio.eu) — это "
                "signed-driver совместимый с HVCI; LHM v0.9.6 умеет с ним "
                "работать. Альтернатива (не рекомендуется): Windows "
                "Security → Device Security → Core Isolation → выключить "
                "Memory Integrity."
            )
        if DegradedReason.SAC_BLOCKED in seen_reasons:
            advice.append(
                "Smart App Control блокирует unsigned-kernel-drivers. SAC "
                "можно только выключить один раз (без обратного включения "
                "без переустановки Windows). Лучшее решение — установить "
                "PawnIO (https://pawnio.eu), он совместим с SAC."
            )
        if DegradedReason.DEFENDER_BLOCKED in seen_reasons:
            advice.append(
                "Microsoft Defender карантинит WinRing0 как "
                "VulnerableDriver:WinNT/Winring0.* (это корректное "
                "поведение — у WinRing0 есть CVE-2020-14979). Установите "
                "PawnIO как замену: https://pawnio.eu."
            )
        if DegradedReason.AV_BLOCKED in seen_reasons:
            avp = next(
                (b for b in backends if b.reason == DegradedReason.AV_BLOCKED),
                None,
            )
            av_name = "ваш антивирус"
            try:
                from apexcore.infrastructure.sensors.probe import run_full_probe

                probe = run_full_probe()
                if probe.av_vendor:
                    av_name = probe.av_vendor
            except Exception:
                # probe не должен ронять advice — продолжаем с дефолтным av_name.
                pass
            advice.append(
                f"{av_name} блокирует kernel-driver. Добавьте исключение "
                "для каталога с LHM-DLL, либо установите PawnIO "
                "(https://pawnio.eu) — signed alternative."
            )
            del avp
        if DegradedReason.NO_ADMIN in seen_reasons:
            advice.append(
                "WinRing0 ещё не зарегистрирован в системе. Запустите "
                "apexcore ОДИН РАЗ от имени администратора — LHM-lib сама "
                "извлечёт WinRing0x64.sys и зарегистрирует kernel-сервис "
                "WinRing0_1_2_0. Дальнейшие запуски — без UAC."
            )
        if DegradedReason.NO_DOTNET_RUNTIME in seen_reasons:
            advice.append(
                "pythonnet не смог инициализировать .NET runtime. Проверьте "
                ".NET Framework 4.8 (предустановлен в Win10/11) или "
                "установите .NET 9 Desktop Runtime с "
                "https://dotnet.microsoft.com/download. Если apexcore был "
                "установлен через installer — переустановите."
            )
        if DegradedReason.CPU_UNSUPPORTED in seen_reasons:
            advice.append(
                "CPU temp недоступна. Часто причина не в LHM, а в том что "
                "PawnIO-драйвер не получил AMX-скрипты (его сервис "
                "apexcore_sensord не запущен или ему не дали доступ).\n"
                "Быстрое решение:\n"
                "  1. Закрой это окно\n"
                "  2. Правый клик по ярлыку «ApexCore» → "
                "«Запуск от имени администратора»\n"
                "  3. Дождись загрузки меню, выйди (q)\n"
                "  4. Запусти заново БЕЗ админа — CPU temp должна появиться\n"
                "Если не помогло, выполни в PowerShell от админа:\n"
                "  apexcore repair-drivers\n"
                "Только если это новейший CPU из ещё не выпущенного "
                "поколения — открой issue с моделью."
            )
        # Fallback если ничего не классифицировано
        if not seen_reasons and lhm_dll and lhm_dll.ok and lhm_run and lhm_run.ok:
            advice.append(
                "LHM работает, но CPU-сенсоров нет. Запустите apexcore ОДИН "
                "РАЗ от имени администратора для регистрации kernel-driver."
            )
        # Дополнительная проверка: установлен ли apexcore_sensord, но не Running?
        # Это типичный сценарий когда install_sensord_bundle.ps1 успел
        # зарегистрировать сервис, но `sc.exe start` не смог его поднять —
        # подсказываем как диагностировать без угадывания.
        try:
            import subprocess
            r = subprocess.run(
                ["sc.exe", "query", "apexcore_sensord"],
                capture_output=True, text=True, timeout=3,
            )
            if r.returncode == 0 and "RUNNING" not in r.stdout:
                advice.append(
                    "Сервис apexcore_sensord зарегистрирован, но НЕ запущен "
                    "(вероятно крашится при старте). Без него CPU temp не "
                    "подтянется без admin.\n"
                    "Диагностика причины (PowerShell):\n"
                    "  apexcore-sensord.exe selftest\n"
                    "  Get-Content $env:PROGRAMDATA\\apexcore\\sensord-boot.log\n"
                    "  Get-Content $env:PROGRAMDATA\\apexcore\\sensord.log -Tail 50\n"
                    "Восстановление: apexcore repair-drivers"
                )
        except Exception:  # noqa: BLE001
            pass
    if sysname == "Linux" and not has_cpu:
        advice.append(
            "На Linux реальная T° CPU обычно через hwmon (coretemp/k10temp). "
            "Если их нет — установите lm-sensors (`apt install lm-sensors` "
            "+ `sensors-detect`)."
        )
    if not has_gpu and shutil.which("nvidia-smi") is None:
        advice.append(
            "Видеокарта NVIDIA не обнаружена через nvidia-smi. Если у вас "
            "есть NVIDIA GPU — установите официальный драйвер."
        )
    # smartctl — опциональный, но полезный для секции «Диски» в «Датчики».
    smartctl_b = next((b for b in backends if b.name == "smartctl"), None)
    if smartctl_b and not smartctl_b.ok and "не в PATH" in smartctl_b.detail:
        if sysname == "Windows":
            advice.append(
                "T° дисков (NVMe/SATA) недоступна — установите smartmontools: "
                "`winget install smartmontools.smartmontools`."
            )
        else:
            advice.append(
                "T° дисков недоступна — установите smartmontools: "
                "`sudo apt install smartmontools`."
            )

    return SensorDiagnostics(
        platform=sysname,
        has_cpu_temperature=has_cpu,
        has_gpu_temperature=has_gpu,
        cpu_temp_source=cpu_source,
        gpu_temp_source=gpu_source,
        backends=backends,
        advice=advice,
        degraded_reasons=degraded_reasons,
    )


# ─── Capability summary для `apexcore info` ─────────────────────────────────


# Короткий лейбл backend'а для capability-строки. Используется только в
# ``build_capability_summary`` — не дублирует ``SourceBackend.value``, который
# kebab-case ("hwinfo-shm") и не годится для UI.
_BACKEND_LABEL: dict[SourceBackend, str] = {
    SourceBackend.HWINFO_SHM: "HWiNFO SHM",
    SourceBackend.CORETEMP_SHM: "CoreTemp SHM",
    SourceBackend.AIDA64_SHM: "AIDA64 SHM",
    SourceBackend.LHM: "LHM",
    SourceBackend.PSUTIL: "psutil",
    SourceBackend.HWMON: "hwmon",
    SourceBackend.PERF_COUNTER: "ACPI zone",
    SourceBackend.NVML: "NVML",
    SourceBackend.NVIDIA_SMI: "nvidia-smi",
    SourceBackend.SMARTCTL: "smartctl",
    SourceBackend.WMI: "WMI MSAcpi",
    SourceBackend.OTHER: "other",
}


def _vcore_present(voltages: dict[str, float]) -> bool:
    """Есть ли в `voltages` напряжение Vcore CPU.

    HWiNFO нормализует «CPU Core Voltage» в P1.5; до этого LHM-ключи
    приходят как ``cpu/vcore`` / ``cpu/cpu_core``. Эвристика проверяет
    оба варианта без жёсткой завязки на конкретный normalizer.
    """
    for key in voltages:
        low = key.lower()
        if "vcore" in low:
            return True
        if low.startswith("cpu/") and ("core" in low or "vcc" in low):
            return True
    return False


def _gpu_silicon_available() -> bool:
    """Доступен ли NVML — индикатор «silicon-level» GPU-метрик.

    Используется в capability-строке для пометки `+NVML` и квалификатора
    «silicon CPU/GPU». Не падает: при отсутствии pynvml / драйвера
    возвращает False.
    """
    try:
        from apexcore.infrastructure.sensors import nvidia_ml

        if not nvidia_ml.is_available():
            return False
        return bool(nvidia_ml.read_nvml_device_names())
    except Exception as exc:  # pragma: no cover - оборона от непредвиденных ошибок NVML
        logger.debug("build_capability_summary: NVML probe упал: %s", exc)
        return False


def build_capability_summary(snap: MetricSnapshot | None = None) -> str:
    """Однострочное описание текущей capability-матрицы для `apexcore info`.

    Возвращает строку вида:

    - ``"HWiNFO SHM+NVML (silicon CPU/GPU, Vcore доступен)"`` — когда HWiNFO
      запущен и NVIDIA-драйвер виден через NVML;
    - ``"ACPI zone (approximate CPU)"`` — когда сработал WMI perf-counter и
      значения статичны (фейковая зона);
    - ``"Источников нет (CPU не считывается — HVCI блокирует, см. `apexcore
      doctor`)"`` — когда ни один backend не отдал CPU-температуру (Windows).

    Helper не падает: при любом сбое возвращает строку «Capability недоступна».
    Используется в ``apexcore info`` сразу после системной таблицы; полная
    диагностика остаётся в ``apexcore doctor``.

    Параметр ``snap`` — последний ``MetricSnapshot`` (если есть). Нужен только
    для определения наличия Vcore в выводе. Без snap строка строится без
    суффикса про Vcore.
    """
    try:
        return _build_capability_summary_impl(snap)
    except Exception as exc:  # pragma: no cover - graceful degrade
        logger.debug("build_capability_summary: упал — %r", exc)
        return "Capability недоступна"


def _build_capability_summary_impl(snap: MetricSnapshot | None) -> str:
    sysname = platform.system().lower()

    # GPU-источник: NVML считаем silicon (структурированный API + util/power).
    gpu_has_silicon = _gpu_silicon_available()

    # 1) Windows: фактический источник CPU из side-channel в windows-адаптере.
    if sysname == "windows":
        try:
            from apexcore.infrastructure.adapters.windows import (
                get_last_cpu_temp_source,
            )

            source, quality = get_last_cpu_temp_source()
        except Exception as exc:  # pragma: no cover
            logger.debug("get_last_cpu_temp_source упал: %s", exc)
            source, quality = None, "unavailable"

        if source is None:
            # Ни один backend не сработал → классифицируем причину.
            reason = _classify_lhm_no_cpu_reason()
            parts = [
                "CPU не считывается",
                reason.short(),
                "см. `apexcore doctor`",
            ]
            return f"Источников нет ({' — '.join(parts[:2])}, {parts[2]})"

        cpu_label = _BACKEND_LABEL.get(source, str(source.value))
        head = f"{cpu_label}+NVML" if gpu_has_silicon else cpu_label

        cpu_quality = "approximate CPU" if quality == "approximate" else "silicon CPU"
        if gpu_has_silicon:
            cpu_quality += "/GPU"
        details: list[str] = [cpu_quality]

        if snap is not None:
            if _vcore_present(snap.voltages):
                details.append("Vcore доступен")
            else:
                details.append("Vcore недоступен")

        return f"{head} ({', '.join(details)})"

    # 2) Linux: side-channel'а нет, но и сценариев меньше — hwmon/psutil дают
    # silicon-level T° через coretemp/k10temp, fallback в psutil тоже считается
    # silicon. Определяем «есть ли CPU-температура» по snap.temperatures.
    if sysname == "linux":
        has_cpu = bool(snap is not None and any(
            _is_cpu_temp_key(k) for k in snap.temperatures
        ))
        if not has_cpu:
            return (
                "Источников нет (CPU не считывается — lm-sensors не настроен, "
                "см. `apexcore doctor`)"
            )
        head = "hwmon+NVML" if gpu_has_silicon else "hwmon"
        cpu_quality = "silicon CPU"
        if gpu_has_silicon:
            cpu_quality += "/GPU"
        details = [cpu_quality]
        if snap is not None:
            details.append(
                "Vcore доступен" if _vcore_present(snap.voltages) else "Vcore недоступен"
            )
        return f"{head} ({', '.join(details)})"

    # 3) Прочее (макОС / неизвестная ОС) — короткая generic-строка.
    head = "psutil+NVML" if gpu_has_silicon else "psutil"
    return f"{head} (CPU неопределённого качества)"


__all__ = [
    "BackendStatus",
    "SensorDiagnostics",
    "build_capability_summary",
    "diagnose_sensors",
]
