"""Адаптер Windows: psutil + WMI для модели CPU/GPU + WMI/CIM для температур."""

from __future__ import annotations

import logging
import shutil
import subprocess
import winreg

import psutil

from apexcore.domain.cache import CacheTopology
from apexcore.domain.sensor_models import SourceBackend
from apexcore.infrastructure.adapters.base import PsutilBaseAdapter
from apexcore.infrastructure.adapters.cache import topology_from_wmi_kb
from apexcore.infrastructure.gpu_filter import annotate_virtual, gpu_priority
from apexcore.infrastructure.sensors import (
    lhm,
    nvidia_ml,
    ryzen_master,
    smartctl,
    wmi_temps,
)
from apexcore.infrastructure.sensors.probe import run_full_probe
from apexcore.infrastructure.sensors.shm import read_coretemp_sensors
from apexcore.infrastructure.sensors.shm.aida64 import (
    read_aida64_temperatures_and_voltages,
)
from apexcore.infrastructure.sensors.shm.hwinfo import (
    read_hwinfo_temperatures_and_voltages,
)

logger = logging.getLogger(__name__)


# Module-level кэш: «Python-пакет ``wmi`` сломан в этом процессе».
# Зачем: ``wmi`` на module-level вызывает ``GetObject("winmgmts:")``, что в
# background-потоке без CoInitialize даёт ``com_error MK_E_SYNTAX (-2147221020)``.
# Это **не** ``ImportError`` — узкий ``except ImportError`` ловить такое не будет
# и упадёт наверх в ``TelemetryService._run`` со «Сбор метрик завершился ошибкой».
#
# Решение зеркалирует ``infrastructure.sensors.wmi_temps._WMI_PACKAGE_BROKEN``:
# первый раз пробуем импорт под широкий ``except Exception``; если упало —
# помечаем флаг и больше не пытаемся (не платим ~0.5 с на каждый тик).
#
# **Регрессионный инвариант** (см. ARCHITECTURE.md «Что НЕ трогать без обсуждения»):
# флаг + ``except Exception`` сохраняются даже после введения dedicated worker'а.
# Сужение до ``ImportError`` вернёт регрессию #issue-wmi-com-syntax.
_WMI_PACKAGE_BROKEN = False


def _try_import_wmi_safe() -> bool:
    """True если ``import wmi`` прошёл успешно хоть раз в этом процессе.

    При первой неудаче (включая COM-ошибки `MK_E_SYNTAX` из background-потока)
    помечает module-level флаг и больше не пытается. См. wmi_temps.py для
    подробной мотивации; этот хелпер — параллельная реализация для адаптера.
    """
    global _WMI_PACKAGE_BROKEN
    if _WMI_PACKAGE_BROKEN:
        return False
    try:
        import wmi  # type: ignore  # noqa: F401
        return True
    except Exception as exc:  # COM-error не наследник ImportError
        _WMI_PACKAGE_BROKEN = True
        logger.debug("wmi package import failed (cached): %s", exc)
        return False


# Module-level side-channel: какой источник CPU temp сработал в последнем
# `_read_sensors()` и какое качество данных. Используется
# `application.diagnostics_sensors` для построения SensorCapabilities и
# для UX-баннеров — без расширения сигнатуры `_read_sensors` (которая
# завязана на контракт `PsutilBaseAdapter.get_current_metrics`).
_last_cpu_temp_source: SourceBackend | None = None
_last_cpu_temp_quality: str = "unavailable"


def get_last_cpu_temp_source() -> tuple[SourceBackend | None, str]:
    """Источник и качество CPU temp в последнем тике (для diagnostics/UX)."""
    return _last_cpu_temp_source, _last_cpu_temp_quality


# Эвристика «есть ли CPU-температура в snapshot». Используется для решения
# «пробовать ли следующий fallback». Локальная копия логики
# ``thermal_watchdog._is_cpu_temp_key`` — не импортируем чтобы избежать
# циклической зависимости (windows.py → thermal_watchdog не должен идти).
_CPU_KEY_HINTS = ("cpu", "core", "package", "tdie", "tctl", "ccd", "ccx", "coretemp")


def _has_cpu_temp(temps: dict[str, float]) -> bool:
    """Есть ли в словаре хотя бы один реальный CPU-сенсор?"""
    for key in temps:
        lower = key.lower()
        if any(blocked in lower for blocked in ("gpu", "nvme", "ssd", "storage", "wifi")):
            continue
        if any(token in lower for token in _CPU_KEY_HINTS):
            return True
    return False


def _is_acpi_fake_zone(temps: dict[str, float]) -> bool:
    """Все значения в диапазоне 25-30 °C → статичная ACPI zone.

    OEM-DSDT на ноутбуках часто публикует декоративные thermal zones со
    стабильными 25-30 °C даже под полной нагрузкой (см. ресерч §2.5).
    Это **не реальная температура CPU** — данные помечаются как
    ``approximate`` и UX-баннер просит установить HWiNFO/CoreTemp.

    Возвращает ``True`` если **все** значения внутри [25.0, 30.0].
    Пустой dict → ``False`` (нечего фильтровать).
    """
    if not temps:
        return False
    return all(25.0 <= float(v) <= 30.0 for v in temps.values())


class WindowsAdapter(PsutilBaseAdapter):
    """Адаптер под Windows 11.

    Источники данных:
    - модель CPU: WMI (win32_processor) при доступности pywin32/wmi, иначе
      реестр (ProcessorNameString);
    - GPU: WMI (win32_videocontroller) при наличии библиотеки;
    - температуры: psutil → perf-counter Thermal Zone → MSAcpi_ThermalZoneTemperature.
      LibreHardwareMonitor как внешний процесс больше не используется
      (см. issue #17 и infrastructure/sensors/lhm.py).
    """

    name = "windows"

    def _read_cpu_model(self) -> str | None:
        wmi_model = self._wmi_first("Win32_Processor", "Name")
        if wmi_model:
            return wmi_model
        return self._read_cpu_model_from_registry()

    def _enumerate_gpus(self) -> list[str]:
        # 1) Win32_VideoController через WMI — основной источник имени GPU.
        gpus = self._wmi_all("Win32_VideoController", "Name")
        if gpus:
            return self._sort_gpus_real_first([annotate_virtual(g) for g in gpus if g])
        # 2) Fallback на NVML — если WMI недоступен (COM-ошибка из background-
        #    потока, см. _WMI_PACKAGE_BROKEN) или провайдер пуст. NVML даёт
        #    точное имя ("NVIDIA GeForce RTX 4070 Ti") без admin и без WMI.
        try:
            from apexcore.infrastructure.sensors import nvidia_ml
            names = nvidia_ml.read_nvml_device_names()
            return self._sort_gpus_real_first(
                [annotate_virtual(n) for n in names.values() if n]
            )
        except Exception as exc:
            logger.debug("NVML GPU enumeration failed: %s", exc)
        return []

    @staticmethod
    def _sort_gpus_real_first(gpus: list[str]) -> list[str]:
        """Discrete GPU → integrated → virtual.

        Win32_VideoController в Windows-сессии часто возвращает первым
        ``Virtual Desktop Monitor`` / ``Microsoft Basic Display`` /
        ``Hyper-V Virtual Video`` (session-aware/RDP-stub адаптеры) или
        ``Intel UHD Graphics 770`` (integrated iGPU). Дискретная NVIDIA/AMD
        Radeon RX/Pro / Intel Arc оказывается дальше первой позиции —
        UI («apexcore info», правая панель Stress) берёт `gpu_list[0]` как
        «основную видеокарту» и показывает iGPU/virtual.

        Сортируем через ``gpu_priority``:
          0 — discrete (NVIDIA, AMD Radeon RX/Pro, Intel Arc)
          1 — integrated (Intel UHD/Iris, AMD APU Vega)
          2 — virtual (RDP, Hyper-V, Virtual Desktop Monitor)
        Сортировка стабильная: между equally-priority элементами порядок
        сохраняется (например 2 дискретных NVIDIA в SLI идут как пришли
        от WMI).
        """
        return sorted(gpus, key=gpu_priority)

    def get_frequencies_mhz(self) -> dict[str, float]:
        """Реальная частота CPU с учётом турбобуста.

        Источники по убыванию точности:
        1. **LHM per-core clocks** — точные MSR-значения с каждого ядра.
           Требует зарегистрированного WinRing0 (см. ``apexcore doctor``).
        2. **PDH ``% Processor Performance``** — отношение к базовой, умножаем
           на base. Работает без LHM/админа, но менее точно (агрегированный
           процент за интервал).
        3. **psutil.cpu_freq()** — на Windows отдаёт базовую из реестра (она
           не меняется под нагрузкой); используется как `cpu_base`/`cpu_max`.

        Семантика возвращаемых ключей:
        - ``cpu_avg`` — текущая средняя по ядрам (live);
        - ``cpu_min`` — минимум по ядрам (live), только если есть LHM;
        - ``cpu_max`` — максимум **capability** (turbo из реестра), не live;
        - ``cpu_base`` — базовая из реестра (для отображения «4321/3200»);
        - ``core_<n>`` — частота отдельного ядра.
        """
        freqs = super().get_frequencies_mhz()
        base = freqs.get("cpu_max") or freqs.get("cpu_avg") or 0.0

        # 1) Primary: LHM per-core clocks (точные MSR, требуют WinRing0).
        lhm_clocks = lhm.read_lhm_cpu_clocks()
        if lhm_clocks:
            values = list(lhm_clocks.values())
            freqs["cpu_avg"] = sum(values) / len(values)
            freqs["cpu_min"] = min(values)
            if base > 0:
                # cpu_max оставляем как capability (turbo из реестра) — нужен
                # для throttle-heuristics в _detect_throttling.
                freqs["cpu_base"] = base
            # Маппим имена LHM (`cpu/p_core_1`) на стабильные `core_<n>`
            # для совместимости с render.py и thermal_watchdog.
            for idx, (_, value) in enumerate(sorted(lhm_clocks.items())):
                freqs[f"core_{idx}"] = value
            # NVML GPU-clocks добавляются с префиксом `nvml/`, не конфликтуют
            # с CPU-ключами; рендер фильтрует их через `_LHM_PREFIX_TO_GROUP`.
            freqs.update(nvidia_ml.read_nvml_frequencies())
            return freqs

        # 2) Fallback: PDH (текущий способ при отсутствии LHM).
        if base <= 0:
            return freqs
        pct_total, pct_per_core = self._read_processor_performance_pct()
        if pct_total is not None:
            freqs["cpu_avg"] = base * pct_total / 100.0
            freqs["cpu_base"] = base
        for idx, pct in enumerate(pct_per_core):
            freqs[f"core_{idx}"] = base * pct / 100.0
        # NVML GPU-clocks добавляем и в fallback-ветке.
        freqs.update(nvidia_ml.read_nvml_frequencies())
        return freqs

    def _read_processor_performance_pct(self) -> tuple[float | None, list[float]]:
        """Считать % Processor Performance через PDH (win32pdh).

        Возвращает (total_pct, per_core_pct). 100% = базовая частота, >100% = турбобуст.
        При отсутствии pywin32 возвращает (None, []).
        """
        try:
            import win32pdh  # type: ignore
        except ImportError:
            return None, []
        try:
            query = win32pdh.OpenQuery()
            try:
                # Общий счётчик
                total_h = win32pdh.AddCounter(
                    query, r"\Processor Information(_Total)\% Processor Performance"
                )
                # Per-core: пытаемся добавить 0,0 / 0,1 / …
                per_core_h: list[int] = []
                idx = 0
                while idx < 256:
                    try:
                        h = win32pdh.AddCounter(
                            query,
                            rf"\Processor Information(0,{idx})\% Processor Performance",
                        )
                        per_core_h.append(h)
                        idx += 1
                    except Exception:
                        break
                # Для корректного snapshot нужны два вызова CollectQueryData
                win32pdh.CollectQueryData(query)
                # Минимальная пауза для дифф-счётчиков
                import time as _t
                _t.sleep(0.1)
                win32pdh.CollectQueryData(query)

                total_val: float | None = None
                try:
                    _, raw = win32pdh.GetFormattedCounterValue(
                        total_h, win32pdh.PDH_FMT_DOUBLE
                    )
                    total_val = float(raw)
                except Exception:
                    total_val = None

                per_core_vals: list[float] = []
                for h in per_core_h:
                    try:
                        _, raw = win32pdh.GetFormattedCounterValue(
                            h, win32pdh.PDH_FMT_DOUBLE
                        )
                        per_core_vals.append(float(raw))
                    except Exception:
                        per_core_vals.append(0.0)
                return total_val, per_core_vals
            finally:
                win32pdh.CloseQuery(query)
        except Exception as exc:
            logger.debug("PDH processor performance read failed: %s", exc)
            return None, []

    def _read_temperatures(self) -> dict[str, float]:
        """Тонкая обёртка — для случаев, когда напряжения не нужны.

        Hot-path (``get_current_metrics``) идёт через :meth:`_read_sensors`,
        чтобы LHM-опрос ``hardware.Update()`` выполнялся ровно один раз
        за снимок (Temperature + Voltage в одном проходе).
        """
        temps, _ = self._read_sensors()
        return temps

    def _read_sensors(self) -> tuple[dict[str, float], dict[str, float]]:
        """Гибридный pipeline чтения температур + напряжения.

        Порядок источников CPU temp (каждый следующий — fallback при пустом
        результате предыдущего, см. план §4 и P1 §1.2):

        1. **HWiNFO SHM** (``Global\\HWiNFO_SENS_SM2``) — индустриальный
           референс. Запущен → читаем без admin, signed-driver совместим
           с HVCI/SAC.
        2. **CoreTemp SHM** (``CoreTempMappingObjectEx``) — лёгкий freeware
           для CPU temp/load/power. Публичный SDK.
        3. **AIDA64 SHM** (``AIDA64_SensorValues``) — коммерческий, но если
           установлен — даёт silicon-level T° + Vcore без admin (P1.2).
        4. **AMD Ryzen Master** runtime-discovery установленного DLL — только
           desktop AMD (P1.4, needs verification on AMD).
        5. **LHM in-process** (pythonnet + LibreHardwareMonitorLib v0.9.6).
           Требует WinRing0 — не работает под HVCI/SAC/Defender carantine.
        6. **psutil** ``sensors_temperatures()`` — обычно пуст на Windows.
        7. **WMI perf-counter** ``Thermal Zone Information`` — фильтр
           25-30 °C под нагрузкой помечает как ``approximate``.
        8. **WMI MSAcpi** ``MSAcpi_ThermalZoneTemperature`` — тот же фильтр.

        Voltages доступны через HWiNFO, AIDA64, Ryzen Master и LHM.
        CoreTemp/WMI Vcore не публикуют.

        Дополняющие источники (add-on, не fallback):

        - ``nvidia_ml`` — pynvml даёт util/power/clocks NVIDIA GPU.
        - ``smartctl`` — T° NVMe/SATA.
        - ``lhm.read_lhm_fans()`` и ``lhm.read_lhm_cpu_power()`` — RPM/Watt
          через voltages-dict (M6 заменит на SensorSnapshot).

        Side-effect: module-level ``_last_cpu_temp_source`` / ``_last_cpu_temp_quality``
        — кто сработал и с каким качеством, для diagnostics/UX.
        """
        global _last_cpu_temp_source, _last_cpu_temp_quality

        temps: dict[str, float] = {}
        voltages: dict[str, float] = {}
        cpu_temp_source: SourceBackend | None = None
        cpu_temp_quality = "unavailable"
        probe = run_full_probe()

        # 1) HWiNFO SHM — самый качественный fallback, без admin.
        # Источник для CPU присваивается **только** если HWiNFO реально вернул
        # CPU-температуру (бывает что HWiNFO даёт лишь GPU/мат-плату — тогда
        # `_last_cpu_temp_source` остаётся None и UX показывает degraded).
        # GPU/мат-плата всё равно подмешиваются в temps/voltages — capability
        # помечает CPU как degraded, но GPU-данные пользователю нужны.
        if probe.shm_available.get("hwinfo", False):
            hwinfo_temps, hwinfo_voltages = read_hwinfo_temperatures_and_voltages()
            if hwinfo_temps:
                temps.update(hwinfo_temps)
                voltages.update(hwinfo_voltages)
                if _has_cpu_temp(hwinfo_temps):
                    cpu_temp_source = SourceBackend.HWINFO_SHM
                    cpu_temp_quality = "silicon"

        # 2) CoreTemp SHM — только CPU temp, без voltage.
        if not _has_cpu_temp(temps) and probe.shm_available.get("coretemp", False):
            coretemp_temps = read_coretemp_sensors()
            if coretemp_temps:
                temps.update(coretemp_temps)
                if _has_cpu_temp(coretemp_temps):
                    cpu_temp_source = SourceBackend.CORETEMP_SHM
                    cpu_temp_quality = "silicon"

        # 3) AIDA64 SHM — temp + voltage (включая Vcore). Покрытие как у
        # HWiNFO, но AIDA64 платный — добавлен в P1.2 для enthusiast-аудитории.
        # См. план P1 §1.2 и docs/research §3.2.
        if not _has_cpu_temp(temps) and probe.shm_available.get("aida64", False):
            aida_temps, aida_voltages = read_aida64_temperatures_and_voltages()
            if aida_temps:
                temps.update(aida_temps)
                voltages.update(aida_voltages)
                if _has_cpu_temp(aida_temps):
                    cpu_temp_source = SourceBackend.AIDA64_SHM
                    cpu_temp_quality = "silicon"

        # 4) Ryzen Master Monitoring SDK — AMD-only fallback (P1.4). Runtime-
        # discovery установленной у пользователя версии (DLL не редистрибутируем).
        # Тонкий шаг между AIDA64 и LHM: если SHM-источников нет на AMD desktop,
        # подписанный Ryzen Master драйвер уже зарегистрирован и доступен.
        if not _has_cpu_temp(temps) and ryzen_master.is_available():
            rm_temps = ryzen_master.read_ryzen_master_temperatures()
            rm_voltages = ryzen_master.read_ryzen_master_voltages()
            if rm_temps:
                temps.update(rm_temps)
                voltages.update(rm_voltages)
                if _has_cpu_temp(rm_temps):
                    # AMD-specific source — пока репортим через LHM-ярлык до
                    # появления отдельного SourceBackend.RYZEN_MASTER (P2).
                    cpu_temp_source = SourceBackend.LHM
                    cpu_temp_quality = "silicon"

        # 5) LHM in-process (текущий primary до v0.5.0). Voltages — только отсюда
        # (HWiNFO покрыл бы их выше, но если HWiNFO нет — Vcore через LHM).
        # `cpu_temp_source = LHM` ставим только если LHM реально вернул CPU
        # температуру: без admin WinRing0 не зарегистрирован, и LHM отдаёт
        # лишь GPU/мат-плату/RPM — в этом случае degraded mode корректнее.
        if not _has_cpu_temp(temps):
            lhm_temps, lhm_voltages = lhm.read_lhm_temperatures_and_voltages()
            if lhm_temps:
                temps.update(lhm_temps)
                voltages.update(lhm_voltages)
                if _has_cpu_temp(lhm_temps):
                    cpu_temp_source = SourceBackend.LHM
                    cpu_temp_quality = "silicon"
        else:
            # SHM сработал — но voltages из LHM всё равно тащим, если SHM их
            # не дал. Это редкий путь (CoreTemp без Vcore + LHM работает).
            if not voltages:
                _, lhm_voltages = lhm.read_lhm_temperatures_and_voltages()
                voltages.update(lhm_voltages)

        # Add-on: NVML — power/util/clocks для GPU, не пересекается с CPU.
        nvml_temps = nvidia_ml.read_nvml_temperatures()
        nvml_power = nvidia_ml.read_nvml_power()
        temps.update(nvml_temps)
        # Power от NVML временно кладём в voltages-dict — отдельного поля
        # `powers` в `MetricSnapshot` пока нет (M4 введёт SensorSnapshot).
        voltages.update(nvml_power)

        # Add-on: smartctl — T° дисков, не пересекается с CPU.
        # LHM публикует те же диски в формате `storage/composite_temperature`,
        # `storage/temperature_2` (2-сегментные ключи), а smartctl — в формате
        # `storage/<short>/temperature` (3-сегментный). Простое `k not in temps`
        # дубли не ловит из-за разных ключей → один и тот же физический диск
        # появляется в UI «Накопители» 2-3 раза (Composite + Sensor 2 + smartctl).
        # При наличии smartctl-данных он точнее (NVMe-Health + ATA Standard),
        # поэтому LHM `storage/*` 2-сегментные temperature-ключи выкидываем.
        # LHM-имена дисков (`read_lhm_storage_names`) при этом сохраняются —
        # они нужны frontend'у для обогащения device label, но не публикуют
        # readings.
        smartctl_temps = smartctl.read_smartctl_temperatures()
        if smartctl_temps:
            temps = {
                k: v
                for k, v in temps.items()
                if not (k.startswith("storage/") and k.count("/") == 1)
            }
        for k, v in smartctl_temps.items():
            if k not in temps:
                temps[k] = v

        # Add-on: LHM fans и cpu_power (если LHM запущен, даже если не он
        # дал CPU temp). Префикс `fan/` / `cpu_power/` — parse_legacy_key
        # распознаёт.
        for k, v in lhm.read_lhm_fans().items():
            voltages[k] = v
        for k, v in lhm.read_lhm_cpu_power().items():
            voltages[k] = v

        # 6) psutil fallback.
        if not _has_cpu_temp(temps):
            try:
                sensors_fn = getattr(psutil, "sensors_temperatures", None)
                if callable(sensors_fn):
                    for chip, entries in (sensors_fn() or {}).items():
                        for entry in entries:
                            label = entry.label or chip
                            if entry.current:
                                temps[label] = float(entry.current)
                    if _has_cpu_temp(temps):
                        cpu_temp_source = SourceBackend.PSUTIL
                        cpu_temp_quality = "silicon"
            except (AttributeError, NotImplementedError, OSError):
                pass

        # 7) WMI perf-counter — предпоследний tier, ACPI thermal zone.
        # Фильтр 25-30 °C: если значения статичны в этом диапазоне → пометить
        # как approximate, но **не отбрасывать** (на ноутбуках это единственный
        # источник). См. план §4 + ресерч §2.5.
        if not _has_cpu_temp(temps):
            zone_temps = wmi_temps.read_perf_counter_thermal_zone()
            if zone_temps:
                temps.update(zone_temps)
                cpu_temp_source = SourceBackend.PERF_COUNTER
                cpu_temp_quality = (
                    "approximate" if _is_acpi_fake_zone(zone_temps) else "silicon"
                )

        # 8) WMI MSAcpi — самый последний, тот же фильтр.
        if not _has_cpu_temp(temps):
            msacpi_temps = wmi_temps.read_msacpi_thermal_zone()
            if msacpi_temps:
                temps.update(msacpi_temps)
                cpu_temp_source = SourceBackend.WMI
                cpu_temp_quality = (
                    "approximate" if _is_acpi_fake_zone(msacpi_temps) else "silicon"
                )

        _last_cpu_temp_source = cpu_temp_source
        _last_cpu_temp_quality = cpu_temp_quality
        return temps, voltages

    def _read_cpu_model_from_registry(self) -> str | None:
        try:
            key_path = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
                value, _ = winreg.QueryValueEx(key, "ProcessorNameString")
                if isinstance(value, str):
                    clean = value.strip()
                    return clean or None
        except OSError:
            return None
        return None

    # ────────── вспомогательные ──────────

    def _wmi_first(self, cls: str, prop: str) -> str | None:
        if not _try_import_wmi_safe():
            return None
        try:
            import wmi  # type: ignore
            c = wmi.WMI()
            for obj in getattr(c, cls)():
                value = getattr(obj, prop, None)
                if value:
                    return str(value).strip()
        except Exception as exc:  # pragma: no cover
            logger.debug("WMI %s.%s read failed: %s", cls, prop, exc)
        return None

    def _wmi_all(self, cls: str, prop: str) -> list[str]:
        if not _try_import_wmi_safe():
            return []
        out: list[str] = []
        try:
            import wmi  # type: ignore
            c = wmi.WMI()
            for obj in getattr(c, cls)():
                value = getattr(obj, prop, None)
                if value:
                    out.append(str(value).strip())
        except Exception as exc:  # pragma: no cover
            logger.debug("WMI %s.%s enum failed: %s", cls, prop, exc)
        return out

    def check_prerequisites(self) -> bool:
        # Базово True — собственные движки доступны всегда; prime95 опционален.
        return shutil.which("prime95") is not None or True

    def get_cache_topology(self) -> CacheTopology:
        """Прочитать L2/L3 размер из ``Win32_Processor`` (значения в КБ).

        Win32_Processor не публикует L1, поэтому L1 всегда останется
        ``"fallback"`` (32 КБ). На современных x86 это разумная оценка.
        """
        l2_kb = self._wmi_int_first("Win32_Processor", "L2CacheSize")
        l3_kb = self._wmi_int_first("Win32_Processor", "L3CacheSize")
        return topology_from_wmi_kb(l1_kb=None, l2_kb=l2_kb, l3_kb=l3_kb)

    def _wmi_int_first(self, cls: str, prop: str) -> int | None:
        if not _try_import_wmi_safe():
            return None
        try:
            import wmi  # type: ignore
            c = wmi.WMI()
            for obj in getattr(c, cls)():
                value = getattr(obj, prop, None)
                if value:
                    try:
                        return int(value)
                    except (TypeError, ValueError):
                        continue
        except Exception as exc:  # pragma: no cover
            logger.debug("WMI %s.%s int read failed: %s", cls, prop, exc)
        return None

    @staticmethod
    def _safe_subprocess(args: list[str], timeout: float = 2.0) -> str | None:
        try:
            res = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            return res.stdout
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
