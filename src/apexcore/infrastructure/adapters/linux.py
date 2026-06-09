"""Адаптер Linux (включая Astra Linux): psutil + /proc + /sys + lm-sensors."""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

import psutil

from apexcore.domain.cache import CacheTopology
from apexcore.infrastructure.adapters.base import PsutilBaseAdapter
from apexcore.infrastructure.adapters.cache import (
    default_cache_topology,
    detect_topology_from_sysfs,
)
from apexcore.infrastructure.gpu_filter import annotate_virtual
from apexcore.infrastructure.sensors import nvidia_ml, smartctl

logger = logging.getLogger(__name__)

CPUINFO_PATH = Path("/proc/cpuinfo")
HWMON_ROOT = Path("/sys/class/hwmon")


class LinuxAdapter(PsutilBaseAdapter):
    """Адаптер под Linux / Astra Linux SE.

    Источники данных:
    - модель CPU: ``/proc/cpuinfo`` (поле ``model name``);
    - GPU: ``lspci -nnk | grep -iE 'vga|3d|display'`` если доступен;
    - температуры: ``psutil.sensors_temperatures`` (поверх lm-sensors) + чтение
      ``/sys/class/hwmon/hwmonN/temp*_input`` как fallback (без необходимости root).
    """

    name = "linux"

    def _read_cpu_model(self) -> str | None:
        try:
            text = CPUINFO_PATH.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return None
        for line in text.splitlines():
            if line.lower().startswith("model name"):
                _, _, val = line.partition(":")
                return val.strip() or None
        return None

    def _enumerate_gpus(self) -> list[str]:
        if shutil.which("lspci") is None:
            return []
        try:
            out = subprocess.run(
                ["lspci"],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            ).stdout
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return []
        gpus: list[str] = []
        for line in out.splitlines():
            ll = line.lower()
            if any(tag in ll for tag in ("vga compatible controller", "3d controller", "display controller")):
                # Формат: "01:00.0 VGA compatible controller: NVIDIA Corporation ..."
                _, _, descr = line.partition(":")
                _, _, descr2 = descr.partition(":")
                gpus.append(annotate_virtual((descr2 or descr).strip()))
        return gpus

    def _read_temperatures(self) -> dict[str, float]:
        """Источники температур (объединяются, не fallback'ы).

        Все четыре источника читаются **всегда** и сливаются по
        непересекающимся пространствам ключей:

        - **psutil** — CPU coretemp/k10temp/cpu_thermal, ключи ``<chip>.<label>``;
        - **pynvml** — NVIDIA GPU, ключи ``nvml/...``;
        - **smartctl** — диски при наличии прав, ключи ``smartctl/...``;
        - **hwmon** — GPU iGPU/dGPU (``gpu/<chip>/<label>``), накопители
          (``disk/<chip>/<label>``), плюс fallback для CPU/MB/ACPI когда
          psutil пуст (часто на Astra SE без lm-sensors).

        Раньше hwmon читался **только** при пустом psutil. На Astra
        Ryzen 6800H + Radeon 680M это означало: psutil даёт CPU
        (k10temp), hwmon никогда не зовётся, и `gpu/amdgpu/edge` +
        `disk/nvme/...` не попадают в live snapshot — отсутствуют в
        WebUI sensor cards, в stress UI и в micro_run scoring.
        Объединение источников делает hwmon **первичным** источником
        T° GPU/диска на Linux: для них kernel-driver сам публикует
        sysfs-узлы без админских прав, чего нет ни в psutil, ни в
        smartctl (последний требует full root на kernel 6.1+).
        """
        temps: dict[str, float] = {}
        sensors_fn = getattr(psutil, "sensors_temperatures", None)
        if callable(sensors_fn):
            try:
                for chip, entries in (sensors_fn() or {}).items():
                    is_cpu_chip = chip in self._HWMON_CPU_CHIPS
                    is_mb_chip = chip in self._HWMON_MB_CHIPS
                    for entry in entries:
                        if not entry.current:
                            continue
                        label = entry.label or chip
                        if is_cpu_chip:
                            # LHM-совместимый префикс `cpu/<label>` →
                            # `parse_legacy_key` отнесёт к группе CPU и
                            # отрисует карточку в WebUI Sensors. Без этой
                            # нормализации `k10temp.Tctl` (psutil-формат)
                            # отбрасывался в parse_legacy_key и CPU-карточки
                            # на Linux не было совсем.
                            key = f"cpu/{label}"
                        elif is_mb_chip:
                            # `motherboard/acpi_zone` и т.п. → группа
                            # MOTHERBOARD. acpitz — ACPI thermal zone,
                            # не равна реальной T° мат-платы, но даёт
                            # +1 информативный датчик в Sensors UI.
                            key = f"motherboard/{label}"
                        elif entry.label:
                            key = f"{chip}.{label}"
                        else:
                            key = chip
                        temps[key] = float(entry.current)
            except (NotImplementedError, OSError) as exc:
                logger.debug("psutil.sensors_temperatures failed: %s", exc)

        # pynvml — NVIDIA GPU. Префикс `nvml/` не конфликтует с psutil/hwmon.
        temps.update(nvidia_ml.read_nvml_temperatures())

        # smartctl — диски (если smartctl запущен от root / имеет нужные
        # capabilities). На Astra SE 1.8 kernel 6.1.158 для NVMe требует
        # full root — для unprivileged запуска используется hwmon ниже.
        for k, v in smartctl.read_smartctl_temperatures().items():
            if k not in temps:
                temps[k] = v

        # hwmon — primary для GPU/диска, fallback для CPU/MB/ACPI.
        # Читается **всегда**, ключи не пересекаются с psutil (psutil
        # генерирует `<chip>.<label>`, hwmon GPU/диск — `gpu/...`/`disk/...`,
        # hwmon CPU/MB — `<chip>.<label>` совпадает с psutil → dedup через
        # `if k not in temps` ниже).
        for k, v in self._read_hwmon().items():
            if k not in temps:
                temps[k] = v
        return temps

    def _read_sensors(self) -> tuple[dict[str, float], dict[str, float]]:
        """Однопроходное чтение температур + voltage/power-like метрик.

        voltages-dict кроме напряжений содержит и **power** (через kind-
        override в ``_parse_gpu_vendor`` / ``_parse_nvml``) — так
        SensorSnapshot получает power-readings без расширения MetricSnapshot
        отдельным полем. Источники на Linux:

        - **NVIDIA**: pynvml power → ``nvml/<idx>/power_w``;
        - **AMD GPU (amdgpu hwmon)**: vddgfx, vddnb (мВ → В) →
          ``gpuamd/vddgfx`` / ``gpuamd/vddnb``; power1_average (мкВт → Вт)
          → ``gpuamd/power_average``;
        - **Battery** (BAT0 hwmon): in0 (мВ → В) →
          ``motherboard/battery`` (group MOTHERBOARD, label «Аккумулятор»).
        """
        temps = self._read_temperatures()
        voltages = nvidia_ml.read_nvml_power()
        # hwmon: voltage + power для amdgpu и system (battery).
        voltages.update(self._read_hwmon_voltages_powers())
        # hwmon: fan RPM. Ключи `fan/<chip>/<label>` обрабатываются
        # `_parse_fan` в sensor_keys.py (kind=FAN_RPM, группа FANS) —
        # default_kind=VOLTAGE из voltages-dict игнорируется handler-ом.
        voltages.update(self._read_hwmon_fans())
        return temps, voltages

    # hwmon chip name → LHM-совместимый префикс ключа. Используем уже
    # существующие LHM-префиксы (`gpuamd`, `gpuintel`), чтобы данные
    # из hwmon переиспользовали весь pipeline `parse_legacy_key →
    # SensorSnapshot → WebUI cards` без расширения mapping'ов в
    # `application/sensor_keys.py`. Так пользователь на AMD APU
    # (Ryzen 6800H + Radeon 680M) сразу видит T° iGPU из hwmon
    # `amdgpu` без NVIDIA-драйвера и без LHM (которого на Linux нет).
    _HWMON_GPU_PREFIX_BY_CHIP: dict[str, str] = {
        "amdgpu": "gpuamd",     # AMD discrete + iGPU (Ryzen APU, Radeon)
        "radeon": "gpuamd",     # старый AMD-драйвер (pre-amdgpu, <=R9 290)
        "i915": "gpuintel",     # Intel HD/UHD/Iris (kernel driver name)
        "xe": "gpuintel",       # Intel Arc (новый kernel-драйвер)
    }

    # hwmon chip names для накопителей. На Astra Linux SE 1.8 (kernel
    # 6.1.158) для NVMe `smartctl` требует full root — `cap_sys_rawio+ep`
    # ставится setcap'ом, но ядро всё равно блокирует SMART log без
    # CAP_SYS_ADMIN. При этом kernel nvme-driver сам публикует composite
    # температуру через hwmon без привилегий. Это делает hwmon
    # **первичным** источником T° дисков на Linux, а smartctl —
    # опциональным дополнением (когда запущен от root и нужны SMART
    # attributes, не только T°).
    _HWMON_DISK_CHIPS = frozenset({
        "nvme",          # NVMe SSD (kernel nvme-driver, composite temp)
        "drivetemp",     # SATA/SAS HDD/SSD (kernel module drivetemp, опц.)
    })

    # hwmon chip names CPU-датчиков. Используются для нормализации
    # как в psutil-loop (`sensors_temperatures`), так и в прямом
    # hwmon-обходе — для CPU-чипов ключ публикуется как `cpu/<label>`
    # (LHM-совместимый формат), чтобы `parse_legacy_key` отнёс его к
    # группе CPU и отрисовал карточку в WebUI Sensors. Без этого
    # `k10temp.Tctl` (psutil-формат с точкой) отбрасывался в
    # parse_legacy_key и Linux-пользователь не видел CPU-карточки.
    _HWMON_CPU_CHIPS = frozenset({
        "k10temp",       # AMD K10+ (Zen, Zen+, Zen2, Zen3, Zen4)
        "zenpower",      # альтернативный AMD-драйвер (более точный Tctl + per-core)
        "zenpower2",     # форк для Zen4
        "coretemp",      # Intel Core (DTS per-core)
        "cpu_thermal",   # ARM SoC (Raspberry Pi и пр., на всякий случай)
        "fam15h_power",  # AMD K15 family RAPL-like power
    })

    # hwmon chip names ACPI / системных температур (мат-плата, EC,
    # Super-IO chips). Публикуем как `motherboard/<label>` →
    # `parse_legacy_key` отнесёт к группе MOTHERBOARD. Покрытие
    # семейств Super-IO: Nuvoton (NCT*), ITE (IT*), Fintek (F*),
    # Winbond legacy (W*), SMSC. Большинство этих чипов требуют
    # `sensors-detect` + `modprobe <driver>` через wizard.
    _HWMON_MB_CHIPS = frozenset({
        "acpitz",          # ACPI thermal zone (стандарт на любой плате)
        # Intel PCH (Haswell .. Alder Lake)
        "pch_haswell", "pch_skylake", "pch_cannonlake",
        "pch_cometlake", "pch_icelake", "pch_tigerlake", "pch_alderlake",
        # Nuvoton Super-IO (типично на ASUS/MSI/Gigabyte)
        "nct6775", "nct6776", "nct6779", "nct6791", "nct6792",
        "nct6793", "nct6795", "nct6796", "nct6797", "nct6798",
        "nct7802", "nct7904",
        # ITE Super-IO (типично на бюджетных платах)
        "it87", "it8603", "it8607", "it8620", "it8628", "it8665",
        "it8686", "it8688", "it8689", "it8772", "it8792", "it8728",
        # Fintek Super-IO
        "f71808a", "f71862fg", "f71869", "f71869a", "f71882fg",
        "f71889ed", "f71889fg", "f71889a",
        # Winbond legacy
        "w83627hf", "w83627thf", "w83627ehf", "w83627dhg", "w83667hg",
        "w83781d", "w83783s", "w83791d", "w83792d", "w83793",
        # SMSC EMC
        "emc1402", "emc1403", "emc2305",
        # DIMM thermal sensors (SO-DIMM / DDR4-DDR5)
        "jc42", "spd5118",
        # AquaComputer (для энтузиастов с водой)
        "aquaero", "d5next", "octo", "highflownext",
    })

    # hwmon chip names накопителей. Расширяю на семейство accel-NVMe.
    # `nvme` — kernel driver на любых NVMe. `drivetemp` — SATA через
    # ATA passthrough (нужен `modprobe drivetemp` или auto-load).
    # `hwmonN/name=mt7902` — controller-specific (rare).
    # Уже определён выше — здесь только заметка для будущего.

    def _read_hwmon(self) -> dict[str, float]:
        """Прямое чтение /sys/class/hwmon (работает без root и без lm-sensors).

        Классификация через chip name → LHM-совместимый префикс ключа:

        - GPU-чипы (amdgpu/radeon → ``gpuamd/<label>``,
          i915/xe → ``gpuintel/<label>``) — попадают в `parse_legacy_key`
          через ``_GROUP_BY_PREFIX["gpuamd"|"gpuintel"] = GPU``;
        - Disk-чипы (nvme/drivetemp) → ``storage/<chip>_<label>`` —
          legacy LHM-формат с 2 сегментами, обрабатывается
          ``_parse_storage`` веткой ``len(parts) == 2``, группа STORAGE;
        - Остальные (k10temp/coretemp/acpitz/...) → ``<chip>.<label>`` —
          fallback для CPU/MB/ACPI когда psutil пустой.
        """
        out: dict[str, float] = {}
        if not HWMON_ROOT.exists():
            return out
        try:
            for hwmon_dir in sorted(HWMON_ROOT.iterdir()):
                name_file = hwmon_dir / "name"
                chip = name_file.read_text().strip() if name_file.exists() else hwmon_dir.name
                gpu_prefix = self._HWMON_GPU_PREFIX_BY_CHIP.get(chip)
                is_disk = chip in self._HWMON_DISK_CHIPS
                is_cpu = chip in self._HWMON_CPU_CHIPS
                is_mb = chip in self._HWMON_MB_CHIPS
                for temp_file in sorted(hwmon_dir.glob("temp*_input")):
                    try:
                        raw = int(temp_file.read_text().strip())
                    except (OSError, ValueError):
                        continue
                    label_path = temp_file.with_name(temp_file.name.replace("_input", "_label"))
                    label = (
                        label_path.read_text().strip()
                        if label_path.exists()
                        else temp_file.stem.replace("_input", "")
                    )
                    if gpu_prefix:
                        key = f"{gpu_prefix}/{label}"
                    elif is_disk:
                        # `storage/nvme_composite` (LHM 2-сегментный формат).
                        # При нескольких NVMe-устройствах может возникнуть
                        # коллизия имён (все одинаково шлют `composite`) —
                        # этого пока не наблюдалось, при появлении нужно
                        # будет добавить индекс устройства в ключ.
                        key = f"storage/{chip}_{label}"
                    elif is_cpu:
                        # `cpu/Tctl` (LHM-совместимый префикс) → группа CPU
                        # в `parse_legacy_key`. Зеркало логики из psutil-loop
                        # в `_read_temperatures` — dedup делает `if k not in
                        # temps` в caller.
                        key = f"cpu/{label}"
                    elif is_mb:
                        # Дубль-дилемма: psutil для acpitz отдаёт entry с
                        # пустым label → мы публикуем `motherboard/acpitz`
                        # (label берётся из chip name). hwmon тот же
                        # temp1_input без temp1_label-файла → label
                        # сходится в «temp1», ключ `motherboard/temp1`.
                        # Получается ДВА reading'а с одинаковым значением:
                        # «Acpitz 45°C» + «Temp1 45°C». Чтобы дедупликация
                        # `if k not in temps` сработала, при отсутствии
                        # temp_label-файла используем chip name (тот же
                        # формат, что psutil) — оба пути дадут идентичный
                        # ключ → второй пропустится.
                        label_path = temp_file.with_name(
                            temp_file.name.replace("_input", "_label")
                        )
                        if not label_path.exists():
                            label = chip
                        key = f"motherboard/{label}"
                    else:
                        key = f"{chip}.{label}"
                    out[key] = raw / 1000.0
        except OSError as exc:  # pragma: no cover
            logger.debug("hwmon read failed: %s", exc)
        return out

    def _read_hwmon_voltages_powers(self) -> dict[str, float]:
        """Чтение voltage / power из /sys/class/hwmon в LHM-совместимый dict.

        Все значения конвертируются в native units (V / W) перед публикацией:
        ``in*_input`` (мВ) → В, ``power*_average``/``power*_input`` (мкВт) → Вт.

        Ключи в LHM-совместимом формате:

        - amdgpu/radeon: ``gpuamd/<label>`` (vddgfx → Vcore GPU,
          vddnb → VDDNB, power_average → Мощность) — обрабатываются в
          `_parse_gpu_vendor` с kind-override.
        - i915/xe: ``gpuintel/<label>`` (если когда-то Intel-драйвер
          начнёт публиковать voltage/power через hwmon).
        - BAT0 (battery): ``motherboard/battery`` — обрабатывается
          generic 2-segment handler как kind=VOLTAGE в группе MOTHERBOARD.

        Возвращает плоский dict ``key → value`` для каждого найденного
        sensor input.
        """
        out: dict[str, float] = {}
        if not HWMON_ROOT.exists():
            return out

        def _read_int(p: Path) -> int | None:
            try:
                return int(p.read_text().strip())
            except (OSError, ValueError):
                return None

        def _read_label(input_file: Path, suffix_in: str = "_input") -> str:
            lbl = input_file.with_name(input_file.name.replace(suffix_in, "_label"))
            if lbl.exists():
                try:
                    return lbl.read_text().strip()
                except OSError:
                    pass
            return input_file.stem.replace(suffix_in, "")

        try:
            for hwmon_dir in sorted(HWMON_ROOT.iterdir()):
                name_file = hwmon_dir / "name"
                chip = name_file.read_text().strip() if name_file.exists() else hwmon_dir.name
                gpu_prefix = self._HWMON_GPU_PREFIX_BY_CHIP.get(chip)

                # Voltage inputs (in*_input в мВ → В). Для amdgpu это
                # vddgfx / vddnb (GPU core voltage / NB voltage = SoC
                # voltage на APU). Для BAT0 — battery voltage.
                for vin in sorted(hwmon_dir.glob("in*_input")):
                    raw = _read_int(vin)
                    if raw is None:
                        continue
                    label = _read_label(vin)
                    volts = raw / 1000.0
                    if not (0.0 < volts < 30.0):
                        continue  # outlier-guard (mb_battery до 21 В, остальное < 15 В)
                    if gpu_prefix:
                        out[f"{gpu_prefix}/{label}"] = volts
                    elif chip == "BAT0":
                        # Generic 2-segment handler: motherboard/battery → kind=VOLTAGE
                        # в группе MOTHERBOARD (label «Аккумулятор» из _LABEL_BY_NAME).
                        out["motherboard/battery"] = volts

                # Power inputs (power*_average / power*_input в мкВт → Вт).
                for pin in (
                    list(sorted(hwmon_dir.glob("power*_average")))
                    + list(sorted(hwmon_dir.glob("power*_input")))
                ):
                    raw = _read_int(pin)
                    if raw is None:
                        continue
                    suffix = "_average" if pin.name.endswith("_average") else "_input"
                    label = _read_label(pin, suffix)
                    watts = raw / 1_000_000.0  # uW → W
                    if not (0.0 < watts < 600.0):
                        continue  # outlier-guard
                    if gpu_prefix:
                        # `gpuamd/ppt` или `gpuamd/power_average` — handler
                        # `_parse_gpu_vendor` маппит оба в kind=POWER label
                        # «Мощность».
                        out[f"{gpu_prefix}/{label}"] = watts
        except OSError as exc:  # pragma: no cover
            logger.debug("hwmon voltage/power read failed: %s", exc)
        return out

    def _read_hwmon_fans(self) -> dict[str, float]:
        """Чтение fan*_input (RPM) из /sys/class/hwmon.

        Публикуется в ключе `fan/<chip>/<label>` (или `fan/<chip>` если
        label-файла нет) → `_parse_fan` в sensor_keys.py создаёт reading
        с kind=FAN_RPM в группе FANS. На out-of-the-box Astra обычно
        пустой результат — fan-датчики публикуют Super-IO драйверы
        (nct6798d / it87 / etc), которые требуют `sensors-detect`
        + `modprobe <driver>` через wizard. На некоторых ноутбуках
        amdgpu/i915 публикуют свои fan (`fan1_input` в hwmon-каталоге
        GPU-чипа) — они тоже подхватываются.
        """
        out: dict[str, float] = {}
        if not HWMON_ROOT.exists():
            return out
        try:
            for hwmon_dir in sorted(HWMON_ROOT.iterdir()):
                name_file = hwmon_dir / "name"
                chip = name_file.read_text().strip() if name_file.exists() else hwmon_dir.name
                for fin in sorted(hwmon_dir.glob("fan*_input")):
                    try:
                        raw = int(fin.read_text().strip())
                    except (OSError, ValueError):
                        continue
                    # 0 RPM — валидное состояние (idle вентилятор, zero-RPM
                    # режим у NVIDIA / системные fan-headers без подключённой
                    # вертушки). Передаём в UI, sensor_keys._parse_fan
                    # тоже валидирует 0..20000.
                    if raw < 0 or raw > 20000:
                        continue
                    lbl_path = fin.with_name(fin.name.replace("_input", "_label"))
                    label = (
                        lbl_path.read_text().strip()
                        if lbl_path.exists()
                        else fin.stem.replace("_input", "")
                    )
                    # `fan/<chip>/<label>` — handler _parse_fan ловит этот
                    # формат (2-3 сегмента). Например `fan/nct6798/cpu_fan`
                    # → label «CPU Fan», device «Вентиляторы», группа FANS.
                    out[f"fan/{chip}/{label}"] = float(raw)
        except OSError as exc:  # pragma: no cover
            logger.debug("hwmon fans read failed: %s", exc)
        return out

    def check_prerequisites(self) -> bool:
        # Под Astra Linux основная готовая утилита — stress-ng. Также проверим sysbench.
        return shutil.which("stress-ng") is not None or shutil.which("sysbench") is not None

    def get_cache_topology(self) -> CacheTopology:
        topology = detect_topology_from_sysfs()
        if topology is None:
            return default_cache_topology()
        return topology

    def get_frequencies_mhz(self) -> dict[str, float]:
        # Сначала пробуем стандартный путь через psutil (даёт обычно
        # одно или несколько усреднённых значений по сокету).
        result = super().get_frequencies_mhz()

        # Per-physical-core частоты через sysfs cpufreq + topology.
        # Linux выставляет scaling_cur_freq на КАЖДЫЙ logical CPU
        # (включая SMT-сиблинги). На Ryzen 6800H 8C/16T это давало
        # 16 «Ядро N» в UI, что неверно: SMT-thread — это не отдельное
        # ядро. Группируем через `cpu<N>/topology/core_id` и берём max
        # частоту среди siblings одного physical core (это самая
        # информативная — показывает текущий boost ядра при нагрузке
        # на любой из его SMT-thread'ов; mean бы занижал при idle
        # одного из siblings).
        #
        # На гибридных Intel (Alder/Raptor Lake) core_id у P-cores
        # и E-cores разные, поэтому группировка работает естественно
        # без специальной P/E разметки.
        per_physical: dict[int, float] = {}
        try:
            cpus_root = Path("/sys/devices/system/cpu")
            if cpus_root.exists():
                for cpu_dir in sorted(cpus_root.glob("cpu[0-9]*")):
                    cur = cpu_dir / "cpufreq" / "scaling_cur_freq"
                    if not cur.exists():
                        continue
                    try:
                        khz = int(cur.read_text().strip())
                    except (OSError, ValueError):
                        continue
                    mhz = khz / 1000.0
                    # Physical core id (через sysfs topology). Если
                    # файл отсутствует — fallback на logical CPU index.
                    core_id_file = cpu_dir / "topology" / "core_id"
                    physical_id: int
                    if core_id_file.exists():
                        try:
                            physical_id = int(core_id_file.read_text().strip())
                        except (OSError, ValueError):
                            physical_id = int(cpu_dir.name.replace("cpu", ""))
                    else:
                        physical_id = int(cpu_dir.name.replace("cpu", ""))
                    # max-aggregation для SMT-сиблингов на physical core.
                    if physical_id not in per_physical or mhz > per_physical[physical_id]:
                        per_physical[physical_id] = mhz
            if per_physical:
                # Сортировка для предсказуемого порядка в UI (Ядро 0..7).
                for i, (pid, mhz) in enumerate(sorted(per_physical.items())):
                    result[f"cpu/core_{i}"] = mhz
                values = list(per_physical.values())
                if "cpu_avg" not in result:
                    result["cpu_avg"] = sum(values) / len(values)
                if "cpu_min" not in result:
                    result["cpu_min"] = min(values)
                if "cpu_max" not in result:
                    result["cpu_max"] = max(values)
        except OSError as exc:  # pragma: no cover
            logger.debug("scaling_cur_freq read failed: %s", exc)

        # NVML GPU-clocks (Linux + NVIDIA) — добавляются с префиксом `nvml/`.
        result.update(nvidia_ml.read_nvml_frequencies())

        # amdgpu / radeon / i915 / xe — частоты через hwmon
        # (`freq*_input` в Hz → МГц). На APU обычно один sclk (shader
        # clock), на дискретных AMD дополнительно mclk (memory clock).
        result.update(self._read_hwmon_frequencies())
        return result

    def _read_hwmon_frequencies(self) -> dict[str, float]:
        """GPU-частоты из hwmon freq*_input.

        Ключи в LHM-совместимом формате ``gpuamd/<label>`` /
        ``gpuintel/<label>`` — handler `_parse_gpu_vendor` маппит
        `sclk` → «Graphics clock», `mclk` → «Memory clock».
        Значения hwmon в Hz, конвертируем в МГц (× 1e-6).
        """
        out: dict[str, float] = {}
        if not HWMON_ROOT.exists():
            return out
        try:
            for hwmon_dir in sorted(HWMON_ROOT.iterdir()):
                name_file = hwmon_dir / "name"
                chip = name_file.read_text().strip() if name_file.exists() else hwmon_dir.name
                gpu_prefix = self._HWMON_GPU_PREFIX_BY_CHIP.get(chip)
                if not gpu_prefix:
                    continue
                for fin in sorted(hwmon_dir.glob("freq*_input")):
                    try:
                        raw = int(fin.read_text().strip())
                    except (OSError, ValueError):
                        continue
                    lbl_path = fin.with_name(fin.name.replace("_input", "_label"))
                    label = (
                        lbl_path.read_text().strip()
                        if lbl_path.exists()
                        else fin.stem.replace("_input", "")
                    )
                    mhz = raw / 1_000_000.0  # Hz → MHz
                    if 0.0 < mhz < 10000.0:
                        out[f"{gpu_prefix}/{label}"] = mhz
        except OSError as exc:  # pragma: no cover
            logger.debug("hwmon frequencies read failed: %s", exc)
        return out
