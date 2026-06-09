"""Модели для раздела «Датчики» (M4 issue #3).

Параллельны существующему ``MetricSnapshot`` (он остаётся для micro/stress/
scoring как публичный контракт — см. ``ARCHITECTURE.md`` про неприкосновенность
``domain/models.py``). Новые модели описывают данные с явной структурой
``group → device → sensor → kind/value``, готовые к рендерингу в новом
TUI «Датчики» (M5) и к будущему web-дашборду трендов.

Соглашение: все Pydantic-модели — `extra="forbid"`, `frozen=True` (DTO
без скрытого состояния). Энумы — `str`-backed, чтобы значения сериализовались
человекочитаемо при экспорте CSV/JSON.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

# ─── Энумы группировки ──────────────────────────────────────────────────────


class SensorGroup(str, Enum):
    """Аппаратная группа сенсора — для разбиения на карточки TUI."""

    CPU = "cpu"
    GPU = "gpu"
    MEMORY = "memory"
    MOTHERBOARD = "motherboard"
    STORAGE = "storage"
    FANS = "fans"  # вентиляторы CPU / шасси / помпа / GPU
    POWER_SUPPLY = "psu"  # на будущее, сейчас не публикуется


class SensorKind(str, Enum):
    """Физический тип величины — определяет unit и иконку в UI."""

    TEMPERATURE = "temperature"  # °C
    VOLTAGE = "voltage"  # В
    FREQUENCY = "frequency"  # МГц
    POWER = "power"  # Вт
    FAN_RPM = "fan_rpm"  # об/мин
    LOAD = "load"  # % утилизации (CPU/GPU)
    USAGE_BYTES = "usage_bytes"  # используемая память


class SourceBackend(str, Enum):
    """Откуда пришло значение — для диагностики и rendering-hints.

    На UI «Датчики» (M5) показывается мелким шрифтом рядом с группой:
    «CPU [Ryzen 7 5800X] · LHM». При расхождении двух источников
    (LHM vs nvidia-smi) пользователь видит первоисточник.

    SHM-источники (HWINFO_SHM/CORETEMP_SHM/AIDA64_SHM) — чтение Shared
    Memory чужих установленных утилит через ``OpenFileMapping``. Это
    юридически чистый путь (OS-API уровня, не использование SDK) и
    обычно даёт качество данных силиконового уровня без admin-прав
    самого apexcore. См. ``docs/research`` §3.
    """

    LHM = "lhm"
    HWINFO_SHM = "hwinfo-shm"
    CORETEMP_SHM = "coretemp-shm"
    AIDA64_SHM = "aida64-shm"
    PERF_COUNTER = "perf-counter"
    NVML = "nvml"
    NVIDIA_SMI = "nvidia-smi"
    SMARTCTL = "smartctl"
    PSUTIL = "psutil"
    HWMON = "hwmon"
    WMI = "wmi"
    OTHER = "other"


# ─── Throttle ───────────────────────────────────────────────────────────────


class ThrottleCause(str, Enum):
    """Причина CPU-throttle. Перенесена из ``application/throttle_detector``
    в domain-слой — это «состояние датчика», часть отчёта `SensorSnapshot`.
    """

    NONE = "none"
    THERMAL = "thermal"
    POWER = "power"
    CURRENT = "current"
    VR_THERMAL = "vr_thermal"
    OTHER = "other"


class ThrottleState(BaseModel):
    """Текущее состояние CPU-throttle: причина + опциональная расшифровка."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cause: ThrottleCause = Field(default=ThrottleCause.NONE, description="Причина throttle.")
    detail: str = Field(default="", description="Свободный текст: имя сенсора, дельта counter'а.")

    @property
    def active(self) -> bool:
        return self.cause is not ThrottleCause.NONE


# ─── Один отсчёт ────────────────────────────────────────────────────────────


class SensorReading(BaseModel):
    """Одно показание одного датчика в один момент.

    Это плоский DTO для строки в таблице/карточке UI. Группировка по
    ``group``/``device`` делается на стороне рендера, не в модели.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    group: SensorGroup = Field(..., description="Аппаратная группа (CPU/GPU/...).")
    device: str = Field(..., description="Человекочитаемое имя устройства, например 'Intel Core i9-12900K'.")
    sensor: str = Field(..., description="Стабильный machine-key датчика, например 'p_core_1'.")
    label: str = Field(..., description="RU-имя для UI: 'Ядро P1', 'Memory Junction'.")
    kind: SensorKind = Field(..., description="Физическая величина: temperature/voltage/...")
    value: float = Field(..., description="Численное значение в единицах ``unit``.")
    unit: str = Field(..., description="Единица измерения: °C, В, МГц, Вт, об/мин, %, ГБ.")
    threshold_warn: float | None = Field(
        default=None,
        description="Жёлтый порог. Если железо не публикует — None, ячейка без подкраски.",
    )
    threshold_crit: float | None = Field(
        default=None,
        description="Красный порог. Например, Tjmax для CPU, slowdown для GPU.",
    )
    source: SourceBackend = Field(..., description="Backend-источник для диагностики.")


# ─── Снимок ─────────────────────────────────────────────────────────────────


class SensorSnapshot(BaseModel):
    """Все показания всех датчиков в один момент времени.

    Заменяет роль ``MetricSnapshot.temperatures/voltages/frequencies``
    в UI-слое. ``MetricSnapshot`` продолжает существовать для micro/stress/
    scoring; ``SensorSnapshot`` живёт параллельно. Конвертация —
    ``application/sensor_service.metric_to_sensor_snapshot``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    timestamp: datetime = Field(..., description="Момент снятия отсчёта.")
    readings: list[SensorReading] = Field(
        default_factory=list,
        description="Все показания. Группировка по `group`/`device` на стороне UI.",
    )
    throttle: ThrottleState = Field(
        default_factory=ThrottleState,
        description="Состояние CPU-throttle (cause + detail).",
    )

    # ─── удобные срезы для UI ─────────────────────────────────────────────

    def by_group(self, group: SensorGroup) -> list[SensorReading]:
        """Все показания одной группы (CPU/GPU/...)."""
        return [r for r in self.readings if r.group is group]

    def by_kind(self, kind: SensorKind) -> list[SensorReading]:
        """Все показания одного типа (только температуры / только частоты / …)."""
        return [r for r in self.readings if r.kind is kind]


# ─── Probe-фаза и capability-матрица (P0, релиз v0.5.1) ─────────────────────


class DegradedReason(str, Enum):
    """Конкретная причина отказа сенсорного источника.

    Используется в ``SensorCapabilities.degraded_reasons`` и в
    ``application/diagnostics_sensors.BackendStatus.reason``. Цель —
    дифференцировать UX-сообщения вместо generic «нет данных».

    См. ``docs/research`` §2 и §5.1 для расширенных формулировок советов
    пользователю. ``DegradedReason`` отвечает на вопрос «почему», а не
    «что делать» — последнее формируется в ``advice_lines``.
    """

    NO_LHM_DLL = "no_lhm_dll"
    """DLL LibreHardwareMonitorLib не найдена в ``sensors/lib/``."""

    NO_DOTNET_RUNTIME = "no_dotnet_runtime"
    """pythonnet не смог инициализировать .NET runtime (нет F4.8 / coreclr)."""

    HVCI_BLOCKED = "hvci_blocked"
    """Memory Integrity (HVCI) активен → WinRing0 не загрузится в принципе."""

    SAC_BLOCKED = "sac_blocked"
    """Smart App Control активен → unsigned/legacy-driver блокируется."""

    DEFENDER_BLOCKED = "defender_blocked"
    """Microsoft Defender карантинит WinRing0x64.sys (VulnerableDriver:WinNT/Winring0.*)."""

    AV_BLOCKED = "av_blocked"
    """Сторонний AV (Avast/Kaspersky/AVG) блокирует драйвер."""

    NO_ADMIN = "no_admin"
    """Нет admin-прав — kernel-driver не зарегистрировать первый раз."""

    COM_INIT_FAILED = "com_init_failed"
    """WMI COM-апартмент не инициализируется в текущем потоке."""

    CPU_UNSUPPORTED = "cpu_unsupported"
    """LHM не распознаёт CPU (старая версия LHM / новейший CPU)."""

    ACPI_FAKE_ZONE = "acpi_fake_zone"
    """ACPI thermal zone отдаёт статичные 25–30 °C (битый DSDT, OEM)."""

    ARM_PLATFORM = "arm_platform"
    """Windows на ARM64 (Snapdragon) — нет MSR-доступа, LHM не поддерживается."""

    UNKNOWN = "unknown"
    """Причина не классифицируется (общий catch-all для отчётов)."""

    def short(self) -> str:
        """Короткое RU-описание для inline в UI-ячейках («нет данных (…)»)."""
        mapping = {
            DegradedReason.NO_LHM_DLL: "DLL не скачана",
            DegradedReason.NO_DOTNET_RUNTIME: "нет .NET runtime",
            DegradedReason.HVCI_BLOCKED: "HVCI блокирует драйвер",
            DegradedReason.SAC_BLOCKED: "Smart App Control блокирует",
            DegradedReason.DEFENDER_BLOCKED: "Defender карантинит драйвер",
            DegradedReason.AV_BLOCKED: "антивирус блокирует драйвер",
            DegradedReason.NO_ADMIN: "нужны admin-права",
            DegradedReason.COM_INIT_FAILED: "WMI COM недоступен",
            DegradedReason.CPU_UNSUPPORTED: "CPU не поддерживается LHM",
            DegradedReason.ACPI_FAKE_ZONE: "ACPI zone — не реальная T",
            DegradedReason.ARM_PLATFORM: "ARM64: нет MSR-доступа",
            DegradedReason.UNKNOWN: "причина неизвестна",
        }
        return mapping[self]


class ProbeResult(BaseModel):
    """Снимок состояния системы для выбора стратегии чтения сенсоров.

    Заполняется один раз при старте процесса в ``infrastructure.sensors.
    probe.run_full_probe()`` и кэшируется module-level (не на диск —
    пользователь может установить HWiNFO во время сессии, и перезапуск
    должен сразу подхватить). Все поля устойчивы к ошибкам — при
    недоступности источника берётся консервативное значение.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    timestamp: datetime = Field(
        ..., description="Момент снятия probe (для отладки)."
    )
    architecture: str = Field(
        ..., description="x64 / ARM64 / x86 (по platform.machine())."
    )
    is_admin: bool = Field(
        ..., description="Запущен ли apexcore под админом (нужно для регистрации WinRing0)."
    )
    dotnet_versions: list[str] = Field(
        default_factory=list,
        description=(
            "Установленные .NET runtime версии (через winreg + clr_loader.find_runtimes()). "
            "Пустой список = pythonnet/LHM не запустятся."
        ),
    )
    hvci_enabled: bool = Field(
        default=False,
        description="HVCI / Memory Integrity активен. Если True — WinRing0 не загрузится.",
    )
    sac_enabled: bool = Field(
        default=False,
        description="Smart App Control активен. Если True — unsigned-driver блокируется.",
    )
    vbl_enabled: bool = Field(
        default=False,
        description="Vulnerable Driver Blocklist активен (по умолчанию с Win11 22H2+).",
    )
    defender_quarantine_winring0: bool = Field(
        default=False,
        description="Defender карантинил WinRing0 (видно через Get-MpThreatDetection).",
    )
    av_vendor: str | None = Field(
        default=None,
        description="Сторонний AV-вендор (Avast/Kaspersky/AVG) или None.",
    )
    shm_available: dict[str, bool] = Field(
        default_factory=dict,
        description=(
            "Доступны ли SHM-источники (HWiNFO/CoreTemp/AIDA64). "
            "Ключи: 'hwinfo', 'coretemp', 'aida64'."
        ),
    )


class SensorCapabilities(BaseModel):
    """Итоговая capability-матрица: что мы можем читать и с каким качеством.

    Формируется в ``application/diagnostics_sensors`` из ``ProbeResult`` +
    результатов реальных попыток чтения. Используется UI-слоем для
    статус-баннера («Источник CPU: HWiNFO SHM (silicon)») и для
    дифференцированных сообщений вместо пустого «—».
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    cpu_temp_source: SourceBackend | None = Field(
        default=None,
        description="Действующий источник CPU-температуры (None = ничего не работает).",
    )
    cpu_temp_quality: str = Field(
        default="unavailable",
        description="silicon / approximate / unavailable. См. docs/research §5.2.",
    )
    gpu_temp_source: SourceBackend | None = Field(
        default=None,
        description="Действующий источник GPU-температуры.",
    )
    gpu_temp_quality: str = Field(
        default="unavailable",
        description="silicon / approximate / unavailable.",
    )
    vcore_available: bool = Field(
        default=False,
        description="Доступно ли чтение Vcore CPU (только LHM/HWiNFO SHM на сегодня).",
    )
    degraded_reasons: list[DegradedReason] = Field(
        default_factory=list,
        description="Список конкретных причин отказа (для UX и дерева решений).",
    )
    advice_lines: list[str] = Field(
        default_factory=list,
        description="Готовые строки рекомендаций для UX-баннера degraded mode.",
    )

    @property
    def is_degraded(self) -> bool:
        """``True`` если хотя бы один источник недоступен или approximate."""
        return (
            bool(self.degraded_reasons)
            or self.cpu_temp_quality != "silicon"
            or self.gpu_temp_quality != "silicon"
        )
