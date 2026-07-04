# Research-brief: раздел «Датчики» (замена «Мониторинг в реальном времени»)

**Issue:** [#3](https://github.com/Lelbry/apexcore/issues/3) (`research-pending`, `ux`)
**Статус:** проект, ждёт утверждения пользователем перед фазой реализации.
**Дата:** 2026-05-14
**Ветка:** `dev`.

---

## TL;DR

1. **Текущий модуль «Мониторинг» собирает больше, чем показывает.** `MetricSnapshot` содержит per-core температуры/частоты, voltages, поле под power — UI выводит только `max(temps)` и `cpu_avg`.
2. **Переименовать в «Датчики»**, переписать вывод на адаптивный TUI: dashboard карточками на широких терминалах, длинная per-sensor таблица — на узких (классический conhost.exe / ssh AstraLinux 80×24).
3. **Добавить бэкенды**: `sensors -j` (lm-sensors, Linux), `nvidia-smi` или `nvidia-ml-py` (NVIDIA на обеих ОС), `smartctl -j` (диски). Это закрывает gap'ы по сравнению с готовыми решениями (btop, nvtop, HWiNFO).
4. **Persistence — SQLite** (новая миграция, две таблицы) с прицелом на будущую web-версию для трендов. Экспорт CSV.
5. **Реализация — 6 фаз** (M1 = этот документ, M6 = закрытие issue).

---

## 1. Текущее состояние модуля

### 1.1 Входная точка и pipeline

- CLI: `interfaces/cli/commands/monitor.py:1-50` — команда `apexcore monitor -d <sec> -r <rate>`. Поднимает `TelemetryService`, печатает live-снимки, в конце выводит таблицу и сводку.
- Сэмплер: `application/telemetry_service.py` — `TelemetryService._run()` опрашивает `OSAdapter` каждые `sampling_rate_sec` (default 0.5с). История = `deque(maxlen=100_000)` в памяти процесса; при `stop()` возвращается список снимков.
- Адаптеры: `infrastructure/adapters/{windows,linux}.py` — собирают `MetricSnapshot` через psutil + `infrastructure/sensors/*`.

### 1.2 Что собирается (`domain/models.py:45-71`)

```python
class MetricSnapshot:
    timestamp: datetime
    cpu_percent: float
    ram_percent: float
    ram_used_gb: float
    disk_read_mb: float
    disk_write_mb: float
    temperatures: dict[str, float]   # все сенсоры (°C)
    frequencies: dict[str, float]    # cpu_avg/min/max, core_<n> (МГц)
    voltages:    dict[str, float]    # напряжения (В) — заполняется LHM
    cpu_throttled: bool
    power_w: float | None            # объявлено, никем не заполняется
```

Источники температур (Windows pipeline в `WindowsAdapter._read_temperatures`):
1. `infrastructure/sensors/lhm.py` — pythonnet → LibreHardwareMonitorLib → WinRing0 → DTS MSR. CPU package + per-core (`p_core_<n>`, `e_core_<n>`), GPU temp/Vcore, motherboard, VRM, DIMM.
2. `psutil.sensors_temperatures()` — fallback на части систем.
3. `infrastructure/sensors/wmi_temps.py:read_perf_counter_thermal_zone` — `Get-Counter '\Thermal Zone Information(*)\Temperature'`.
4. `infrastructure/sensors/wmi_temps.py:read_msacpi_thermal_zone` — Python-`wmi` или CIM-fallback. ACPI thermal zone намеренно **не считается** CPU-температурой (`thermal_watchdog._is_cpu_temp_key` — см. `ARCHITECTURE.md`).

Источники температур (Linux pipeline в `LinuxAdapter`):
1. `psutil.sensors_temperatures()` — coretemp, k10temp, zenpower, cpu_thermal.
2. `infrastructure/sensors/hwmon_thresholds.py:read_hwmon_tjmax()` — пороги из `/sys/class/hwmon/hwmon*/temp{N}_crit`. Возвращает **только пороги**, не текущие значения. Текущие — через psutil.

### 1.3 Что отображается (`interfaces/cli/render.py:123-207`)

| Функция | Что показывает | Что прячет |
|---|---|---|
| `render_metric_snapshot(snap)` | CPU%, RAM% (ГБ), freq_avg, `max(temps)`, disk R/W, throttle-флаг — одной строкой с `│` | Per-core temps, voltages, per-core freq, имя горячего сенсора |
| `render_metric_table(snapshots, max_rows=30)` | 7 колонок: время, CPU%, RAM%, freq, `T°max`, disk, ✓/· | Per-sensor breakdown, voltages, throttle cause |
| `render_metric_summary(snapshots)` | min/avg/max по CPU%, RAM%, freq, `T°max`, число тактов с throttle | Какой именно сенсор был горячим |

**Меню:** `interfaces/cli/menu/screens.py:HomeScreen._monitor` — обёртка, спрашивает длительность, запускает `monitor()`.

### 1.4 Конкретные проблемы UX (из issue #3 и кода)

1. **`max(temps)` теряет контекст.** Пользователь не знает, что именно нагрелось — package, core 7, VRM или GPU.
2. **Per-core частоты не выводятся.** `MetricSnapshot.frequencies` содержит `core_<n>`, в render использовано только `cpu_avg`.
3. **Voltages невидимы.** LHM публикует Vcore CPU/GPU, +12V, VRM_in — рендера нет.
4. **`power_w` объявлено, не заполнено.** Адаптеры всегда возвращают `None`.
5. **Throttle = бинарный флаг.** Нет различения thermal / power / current / VR. На Windows LHM умеет `cpu_clock_throttle_*`, на Linux — `/sys/devices/system/cpu/cpu*/thermal_throttle/*_throttle_count`.
6. **Одна строка с `│`** перегружена визуально, плохо читается, при изменении ширины терминала может ломаться.
7. **История теряется при выходе.** Нельзя сравнить две сессии, нельзя посмотреть «что было пару секунд назад» (issue #3 явно жалуется).
8. **`Throt` не отображался при тесте.** Возможно, текущий детектор throttle не срабатывает на короткой нагрузке (см. issue #20 про CPU temp на коротком прогоне).

---

## 2. Обзор готовых решений

Цель — взять лучшие UX-приёмы под наш CLI-контекст. Не копируем целиком — берём идеи.

### 2.1 LibreHardwareMonitor (GUI, Windows)

Древовидная группировка `Hardware → Sensor type → Sensor name`. Каждое железо (CPU, GPU, MB) — корневой узел; внутри — папки Temperatures / Voltages / Clocks / Powers / Fans / Loads. Это **естественная иерархия**, которой нет в нашем плоском dict.

**Что взять:** структуру `(group, device, kind, sensor)` для модели данных и для группировки в TUI.

### 2.2 btop / btop++ (TUI, кросс-платформа)

4-блочный rich-Layout: CPU panel (per-core barchart + sparkline overall), MEM panel (bar+sparkline для used/avail/swap), NET (RX/TX sparkline), PROC (отсортированный список).

Ключевые UX-приёмы:
- Sparkline через unicode `▁▂▃▄▅▆▇█` (Braille для более плотных графиков — но Braille плохо рендерится в conhost.exe).
- Адаптивный resize: при ширине < N колонок одна из панелей скрывается или меняет layout.
- Цветовой код по threshold (зелёный → жёлтый → красный).
- Hotkeys в подвале (`F2 Options`, `q Quit` и т.д.).

**Что взять:** sparkline-helper, threshold colour-coding, hotkeys footer, adaptive layout (через ширину `Console.width`).

### 2.3 nvtop (TUI, Linux)

Фокус на одной hardware-группе (GPU). Верх — крупные sparkline графики (util %, mem util %, temp). Низ — таблица процессов с GPU mem usage.

**Что взять:** идею крупных sparkline'ов для «выбранного устройства» (если пользователь нажмёт `[G]` — раскроется GPU-панель на весь экран).

### 2.4 s-tui (TUI, Linux)

Сфокусирован на CPU thermal/throttling. Показывает per-core частоты, температуру, **причину throttle** (Power Limit / Thermal Limit / Current Limit) — читает из MSR через `/sys/devices/system/cpu/cpu*/thermal_throttle/`.

**Что взять:** разбор throttle cause с текстовым ярлыком («ограничение по теплу» / «по питанию» / «по току»). На Windows — через LHM `Clock Throttle/*` сенсоры.

### 2.5 lm-sensors (`sensors -j`, Linux)

Не TUI, но формат JSON-вывода — образец правильной группировки:

```json
{
  "k10temp-pci-00c3": {
    "Adapter": "PCI adapter",
    "Tctl": { "temp1_input": 45.5 },
    "Tdie": { "temp2_input": 45.5 },
    "Tccd1": { "temp3_input": 44.0 }
  },
  "nvme-pci-0100": {
    "Adapter": "PCI adapter",
    "Composite": {
      "temp1_input": 38.85,
      "temp1_max": 81.85,
      "temp1_crit": 84.85
    }
  }
}
```

**Что взять:** структуру данных `chip → sensor → {input, max, crit}`. Это идеально ложится на нашу `SensorReading` (см. 4.1). Использовать `sensors -j` как основной Linux-бэкенд — он сразу даёт thresholds.

### 2.6 HWiNFO64 / AIDA64 sensor view (GUI, Windows)

Плотная многострочная таблица: `Sensor name | Current | Min | Max | Average`. Группы (CPU/GPU/MB/Drives) — collapsible headers. Threshold cells подкрашены жёлтым/красным.

**Что взять:** колонки Min/Max/Avg в нашей per-sensor таблице, threshold colour-coding.

### 2.7 htop / glances (TUI)

Общего назначения, без фокуса на термоданных. Релевантно только в части UX рамки (фиксированная шапка, прокручиваемая середина, подвал с хоткеями).

### 2.8 Сводка — что переносим в apexcore

| Источник идеи | Приём | Куда переносим |
|---|---|---|
| LHM GUI | Иерархия group → device → sensor | Модель `SensorReading` |
| btop | Sparkline + adaptive layout + threshold colours | Render-helper + `Layout` |
| nvtop | Detail view одной группы | Hotkey `[G]/[C]/[M]` — раскрыть панель |
| s-tui | Throttle cause | Throttle parser + `ThrottleState` |
| lm-sensors | JSON-структура с порогами | `sensors -j` backend + `threshold_warn/crit` поля |
| HWiNFO | Колонки Min/Max/Avg + colour-coded cells | Per-sensor таблица + rolling stats |

---

## 3. Бэкенды на двух ОС

### 3.1 Текущее покрытие

| Источник | Windows | AstraLinux | Уже в проекте | Лицензия | Что даёт |
|---|---|---|---|---|---|
| LibreHardwareMonitorLib (in-process) | ✅ | ❌ | ✅ `infrastructure/sensors/lhm.py` | MPL-2.0 | CPU package+per-core, GPU temp/Vcore, MB/VRM, RAM volts |
| psutil | ✅ частично | ✅ частично | ✅ адаптеры | BSD | базовый fallback temp |
| `/sys/class/hwmon` | ❌ | ✅ | ✅ `hwmon_thresholds.py` (пороги) | kernel | thresholds; текущие значения через psutil |
| WMI thermal zone | fallback | ❌ | ✅ `wmi_temps.py` | MS EULA | (плохой fallback — статичная ~25–30°C) |

### 3.2 Что добавить

| Источник | Что даёт | Когда срабатывает | Реализация |
|---|---|---|---|
| `sensors -j` (lm-sensors) | Правильная группировка `chip → sensor → input/max/crit` + fan RPM + voltages | Linux, если установлен `lm-sensors` и сделан `sensors-detect` | Subprocess + `json.loads`. Без новых Python deps. Graceful fallback на raw hwmon если `sensors` не в PATH. |
| `nvidia-smi` / `nvidia-ml-py` | NVIDIA GPU: temp, power, freq core/mem, util GPU/mem, fan, P-state | Windows + Linux, если установлен NVIDIA driver | **Рекомендация: `nvidia-ml-py` (pynvml)** — официальный binding, structured access (`pynvml.nvmlDeviceGetTemperature(handle, NVML_TEMPERATURE_GPU)`), ~10x быстрее subprocess. PATH-независим. Graceful import (LinuxAdapter/WindowsAdapter не падают если NVIDIA нет). |
| `smartctl -j` (smartmontools) | T° NVMe/SATA, статусы health, SMART-attributes (например, `temperature_celsius` для SATA, `nvme_smart_health_information_log` для NVMe) | Windows + Linux, если установлен пакет smartmontools | Subprocess + `json.loads`. Может требовать root на Linux (для некоторых SATA). На NVMe `/dev/nvme0n1` — обычно доступно пользователю. Кэшировать список устройств (раз в 5 секунд). |

### 3.3 Python-зависимости — финальный выбор

Пользователь разрешил новые зависимости. Решение по каждой:

| Кандидат | Решение | Почему |
|---|---|---|
| **`nvidia-ml-py>=12`** | ✅ **Добавить** | Структурированный API, кросс-платформенный, не парсит текст. На системах без NVIDIA `import pynvml` работает, `pynvml.nvmlInit()` выбросит `NVMLError_LibraryNotFound` — ловим в graceful try/except. Pure-Python wheel. |
| `pySMART` | ❌ Не добавлять | `smartctl -j` через subprocess — короче и понятнее. Дополнительная Python-зависимость над smartctl не оправдана. |
| `PySensors` (libsensors binding) | ❌ Не добавлять | Требует `libsensors4-dev` на сборочной машине; `sensors -j` через subprocess уже даёт JSON. |
| `py-cpuinfo` | ⏸ Под вопросом | Если в шапке экрана нужно отображать «AMD Ryzen 7 5800X (Zen 3)» — да. Но `info` модуль уже это решает через WMI/`/proc/cpuinfo`. Отложить до M3. |
| `psutil` | без изменений | Уже в зависимостях. |

**Итог по pyproject.toml**: добавить только `nvidia-ml-py>=12`. Все прочие новые бэкенды — subprocess + `json`.

### 3.4 Приоритизация источников

**Windows pipeline** (для каждого `(group, kind)`):
1. `lhm.py` (in-process) — основной;
2. `nvidia-ml-py` — для NVIDIA GPU (LHM покрывает NVIDIA temp/freq, но не util и не P-state в свежих драйверах — pynvml надёжнее);
3. `psutil` — fallback CPU;
4. `wmi_temps` — последний fallback (с дисклеймером в UI: «ACPI thermal zone — не реальная температура CPU»);
5. `smartctl` — для дисков.

**AstraLinux pipeline**:
1. `sensors -j` (lm-sensors) — основной для CPU/MB/RAM;
2. `nvidia-ml-py` — NVIDIA GPU;
3. `/sys/class/hwmon` — fallback если `sensors` не установлен;
4. `psutil.sensors_temperatures()` — последний fallback;
5. `smartctl` — диски.

Diagnostics-экран (`apexcore doctor`) должен показывать какой бэкенд активен по каждой группе, с инструкциями для пользователя если что-то не работает.

### 3.5 Out of scope для MVP

- AMD GPU (ROCm / `radeontop`) — отложить, при необходимости подключить `pyamdgpu` или subprocess `radeontop -d -`.
- Intel GPU (`intel_gpu_top`) — отложить.
- Звуковые карты, чипсетные сенсоры экзотики — `sensors -j` покроет если они есть в `/sys/class/hwmon`.

---

## 4. Рекомендуемая архитектура

### 4.1 Модель данных

`domain/models.py` помечен «не трогать без обсуждения» (CLAUDE.md), поэтому новые модели вводим **параллельно** старому `MetricSnapshot` (его оставляем для micro/stress/scoring). Файл новых моделей — `domain/sensor_models.py`.

```python
# domain/sensor_models.py
from enum import Enum
from pydantic import BaseModel, ConfigDict

class SensorGroup(str, Enum):
    CPU = "cpu"
    GPU = "gpu"
    MEMORY = "memory"
    MOTHERBOARD = "motherboard"
    STORAGE = "storage"
    POWER_SUPPLY = "psu"  # future

class SensorKind(str, Enum):
    TEMPERATURE = "temperature"
    VOLTAGE = "voltage"
    FREQUENCY = "frequency"
    POWER = "power"
    FAN_RPM = "fan_rpm"
    LOAD = "load"        # % utilization
    USAGE_BYTES = "usage_bytes"  # memory used

class ThrottleCause(str, Enum):
    NONE = "none"
    THERMAL = "thermal"
    POWER = "power"
    CURRENT = "current"
    VR_THERMAL = "vr_thermal"
    OTHER = "other"

class SensorReading(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    group: SensorGroup
    device: str            # "AMD Ryzen 7 5800X" / "RTX 3070" / "X570 Aorus Master" / "Samsung 980 Pro"
    sensor: str            # "package" | "core_0" | "vrm_mos" | "vcore" | "junction"
    label: str             # человекочитаемое RU-имя для UI: "Ядро 0", "VRM MOS"
    kind: SensorKind
    value: float
    unit: str              # "°C" | "V" | "MHz" | "W" | "RPM" | "%"
    threshold_warn: float | None = None
    threshold_crit: float | None = None
    source: str            # "lhm" | "lm-sensors" | "pynvml" | "smartctl" | "psutil" | "hwmon" — для диагностики

class ThrottleState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    cause: ThrottleCause
    detail: str = ""       # "core 3 hit Tjmax 100°C" — опциональная расшифровка

class SensorSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    timestamp: datetime
    readings: list[SensorReading]
    throttle: ThrottleState
```

**Мостик к старому `MetricSnapshot`**: `application/sensor_service.py` (новый) умеет:
- `collect() -> SensorSnapshot` — основной интерфейс.
- `to_metric_snapshot(snap) -> MetricSnapshot` — обратная совместимость для micro/stress/scoring (они продолжают читать flat dict).

Старый `TelemetryService` остаётся для micro/stress. Новый `SensorService` крутит свой ring-buffer уже из `SensorSnapshot`'ов.

### 4.2 UX — TUI «Датчики»

**Команда:** `apexcore sensors [--duration N] [--rate R] [--no-save]` (старый `apexcore monitor` — оставить как alias на 1-2 релиза, для скриптов).

**Меню:** `HomeScreen._monitor` → `HomeScreen._sensors` с обновлённым заголовком «📡 Датчики (real-time)».

#### 4.2.1 Адаптивный макет

`console.size.width` определяет режим:

- **width ≥ 110** — `rich.Layout` карточками, 2 столбца по 2-3 строки (CPU + GPU сверху, Память + Материнка + Диски снизу):

```
┌─ Датчики · 14:23:05 · 00:18 от старта · ✓ LHM ✓ pynvml ✗ smartctl ─────────────┐
│                                                                                  │
│ ┌─ CPU · AMD Ryzen 7 5800X ──────────────┐ ┌─ GPU · RTX 3070 ─────────────────┐ │
│ │ Package        68.0 °C  ▁▂▃▅▆▇▆   ✓   │ │ Core           72 °C  ▂▄▅▆▇▆    │ │
│ │ Ядро 0         65.2 °C  ▁▂▃▅▆▇         │ │ Память         78 °C  ▁▂▃▅▆     │ │
│ │ Ядро 1         71.0 °C  ▁▂▄▇█▆   ⚠    │ │ Hot Spot       85 °C  ▁▃▆█▇  ⚠  │ │
│ │ ... (свернуть ядра — [C])              │ │ Питание       165 W  ▂▄▆█▇      │ │
│ │ VRM MOS        58.5 °C  ▁▁▂▂▃▃         │ │ Частота      1845 МГц ▃▄▅▆▇     │ │
│ │ Vcore          1.20  V                  │ │ Вентилятор   1850 об/мин        │ │
│ │ Частота        4321/5500 МГц (87%)      │ │ Загрузка       92 %             │ │
│ │ Throttle       нет                      │ └──────────────────────────────────┘ │
│ └─────────────────────────────────────────┘                                       │
│                                                                                   │
│ ┌─ Память ────────────────────────┐ ┌─ Материнка ──────┐ ┌─ Диски ─────────────┐ │
│ │ DIMM 1          42 °C            │ │ Chipset    52 °C │ │ Samsung 980 Pro    │ │
│ │ DIMM 2          41 °C            │ │ +12V       12.05V│ │   Состав   48 °C   │ │
│ │ Использовано 18.3 / 32 ГБ (57%)  │ │ +5V         5.02V│ │   SMART  ✓ ОК      │ │
│ │ Своп          0.0 / 4 ГБ         │ │ Fan CPU  1200/мин│ └─────────────────────┘ │
│ └──────────────────────────────────┘ └──────────────────┘                          │
│                                                                                   │
│ [Esc]/[Q] выход  [P] пауза  [S] экспорт CSV  [C] свернуть ядра  [G] фокус GPU    │
└──────────────────────────────────────────────────────────────────────────────────┘
```

- **width < 110** — fallback: одна длинная `rich.Table`, одна строка на сенсор:

```
Датчики · 14:23:05 · 00:18 от старта
✓ LHM   ✓ pynvml   ✗ smartctl

Группа       Сенсор             Тек     Мин    Макс    Сред   Тренд
─────────────────────────────────────────────────────────────────────
CPU          Package           68.0    45.2   82.1    61.3   ▁▃▅▇▆
CPU          Ядро 0            65.2    42.1   78.4    58.9   ▁▂▅▇▆
CPU          Ядро 1            71.0  ⚠ 46.0 ⚠ 82.1    62.5   ▁▂▆█▇
CPU          VRM MOS           58.5    38.0   64.2    52.1   ▁▂▃▄▅
CPU          Vcore (В)          1.20    1.18   1.32    1.24   ▂▃▄▅▄
CPU          Частота (МГц)     4321    2200   5500    3850   ▃▄▆█▆
GPU          Core              72.0    38.0   78.0    56.2   ▂▄▆▇▆
GPU          Memory            78.0    42.0   82.0    62.4   ▃▅▆█▇
GPU          Питание (Вт)     165.0    35.0  180.0   110.0   ▂▄▆█▇
Память       DIMM 1            42.1    35.0   44.0    39.8   ▁▂▃▃▃
Диски        980 Pro           48.0    35.0   52.0    42.1   ▁▂▃▄▄

Throttle: нет
[Esc]/[Q] выход  [P] пауза  [S] экспорт CSV
```

Граница 110 — эмпирическая, проверим на classic conhost (по умолчанию 120) и AstraLinux ssh (часто 80).

#### 4.2.2 Sparkline helper

Свой helper — `interfaces/cli/sparkline.py`:

```python
_BARS = "▁▂▃▄▅▆▇█"
def sparkline(values: list[float], width: int = 12) -> str:
    """Unicode-sparkline за последние `width` отсчётов."""
    if not values:
        return "·" * width
    tail = values[-width:]
    vmin, vmax = min(tail), max(tail)
    if vmax - vmin < 1e-6:
        return _BARS[3] * len(tail)
    return "".join(_BARS[int((v - vmin) / (vmax - vmin) * 7)] for v in tail)
```

Без новых зависимостей. Работает в classic conhost (unicode `▁-█` рендерится).

#### 4.2.3 Threshold colour-coding

- **зелёный** при `value < threshold_warn`
- **жёлтый** при `value ≥ threshold_warn`
- **красный** при `value ≥ threshold_crit`

Источники порогов:
- LHM — `Tjmax` через DTS (для Ryzen — обычно 95°C, Intel — 100°C).
- lm-sensors — `temp{N}_max` (warn) и `temp{N}_crit` (crit).
- smartctl — `temperature_warning_threshold` / `temperature_critical_threshold` для NVMe.
- pynvml — `NVML_TEMPERATURE_THRESHOLD_SLOWDOWN` (warn) и `NVML_TEMPERATURE_THRESHOLD_SHUTDOWN` (crit).
- Fallback (если нет порогов из железа) — таблица в `infrastructure/sensors/thresholds_default.py`: для CPU temp 80/95, GPU temp 80/90, VRM 75/95, NVMe 70/85.

#### 4.2.4 Throttle с расшифровкой

Новый модуль `application/throttle_detector.py`:

- **Windows (LHM)** — LHM публикует сенсоры вида `Clock Throttle/EDP-Other`, `Clock Throttle/Thermal`. Если значение > 0 — throttle активен. Маппим на `ThrottleCause`.
- **Linux** — читать `/sys/devices/system/cpu/cpu*/thermal_throttle/core_throttle_count`, `package_throttle_count`. Сравниваем counter с предыдущим тиком: если вырос — throttle случился.

В UI: вместо `✓/·` — `[нет]` / `[⚠ тепловой]` / `[⚠ по питанию]` / `[⚠ VRM]`.

#### 4.2.5 Hotkeys

- `Esc` / `Q` / `й` — выход в меню
- `P` / `З` — пауза/возобновить
- `S` / `Ы` — экспорт текущей сессии в CSV
- `C` / `С` — свернуть/развернуть per-core блок
- `G` / `П` — focus mode для GPU (карточка GPU на весь экран, как в nvtop)
- `H` / `Р` — на главную (стандарт `nav.py`)

RU-эквиваленты обязательны (см. `CLAUDE.md` про `nav.py:BACK_KEYS/HOME_KEYS/QUIT_KEYS`).

### 4.3 Persistence (SQLite)

Решение пользователя: писать в SQLite, чтобы будущая web-версия с трендами читала ту же БД. Упор всё равно на CLI — web должен корректно работать и без него, но они делят данные.

#### 4.3.1 Схема

Новая миграция — `CURRENT_VERSION + 1` в `infrastructure/persistence/migrations.py`. Добавить в `schema.sql`:

```sql
CREATE TABLE sensor_sessions (
    id TEXT PRIMARY KEY,         -- UUID4
    started_at TEXT NOT NULL,    -- ISO 8601
    ended_at TEXT,
    hostname TEXT,
    os TEXT,                     -- "Windows 11 26200" / "Astra Linux 1.7"
    cpu_model TEXT,
    gpu_model TEXT,
    sample_count INTEGER DEFAULT 0
);

CREATE TABLE sensor_samples (
    session_id TEXT NOT NULL REFERENCES sensor_sessions(id) ON DELETE CASCADE,
    timestamp TEXT NOT NULL,
    grp TEXT NOT NULL,           -- 'cpu' | 'gpu' | ...
    device TEXT NOT NULL,
    sensor TEXT NOT NULL,
    kind TEXT NOT NULL,
    value REAL NOT NULL,
    threshold_warn REAL,
    threshold_crit REAL
);

CREATE INDEX idx_sensor_samples_session_ts
    ON sensor_samples(session_id, timestamp);
CREATE INDEX idx_sensor_samples_lookup
    ON sensor_samples(session_id, grp, device, sensor);
```

#### 4.3.2 Запись батчами

`infrastructure/persistence/sensor_repository.py`:
- `start_session(meta) -> session_id` — открывает session, возвращает id.
- `append_batch(session_id, snapshots: list[SensorSnapshot])` — INSERT'ы пачкой в одной транзакции.
- `end_session(session_id)` — обновляет `ended_at`, `sample_count`.

`SensorService` буферизует sample'ы 5 секунд, потом одним батчем пишет в БД (≈100-200 rows). Это снижает fsync-нагрузку и не блокирует UI.

#### 4.3.3 Размер данных

10 сенсоров × 2 Hz × 1 час = 72 000 строк. Размер row ≈ 100 байт → ~7 МБ/час. На SSD незаметно.

#### 4.3.4 Retention

В `Settings`:
- `sensor_history_max_sessions: int = 10` — хранить последние 10.
- При старте новой сессии — `DELETE FROM sensor_sessions WHERE id NOT IN (SELECT id FROM sensor_sessions ORDER BY started_at DESC LIMIT 10)`. Каскадно чистятся `sensor_samples`.

#### 4.3.5 Экспорт CSV

`apexcore sensors export <session_id> --out file.csv` (и hotkey `[S]` в TUI).
Формат: `timestamp,group,device,sensor,kind,value,unit,threshold_warn,threshold_crit`. Дефолтная папка — `data/sensors/{started_at}_{session_id[:8]}.csv`.

### 4.4 Кросс-платформенность

#### 4.4.1 Windows pipeline

```
sensors collect cycle:
├── LHM (lhm.py)             → CPU/GPU/MB readings, voltages, frequencies
├── pynvml                   → GPU util %, P-state, fan, mem usage (если NVIDIA)
├── smartctl -j              → drives (если в PATH)
├── psutil                   → CPU/RAM% (всегда)
└── throttle_detector        → LHM clock-throttle sensors → ThrottleState
```

WMI thermal zone оставляем как последний fallback с дисклеймером, **не** считаем CPU temp (см. `_is_cpu_temp_key`).

#### 4.4.2 AstraLinux pipeline

```
sensors collect cycle:
├── sensors -j               → CPU/MB/RAM readings (если lm-sensors установлен)
│   └── fallback: /sys/class/hwmon (raw)
├── pynvml                   → GPU (если NVIDIA driver)
├── smartctl -j              → drives (если smartmontools установлен)
├── psutil                   → fallback CPU/RAM/disk%
└── throttle_detector        → /sys/devices/system/cpu/cpu*/thermal_throttle → ThrottleState
```

#### 4.4.3 Diagnostics ↔ Backend mapping

Расширить `application/diagnostics_sensors.py`:

```
diagnose_sensors() возвращает per-backend статус:
  - lhm_dll        : ok | not_found | not_loaded
  - lhm_runtime    : ok | no_pythonnet | no_dotnet | winring0_blocked
  - pynvml         : ok | no_nvidia_driver | not_installed
  - lm_sensors     : ok | not_in_path | no_detect_run
  - hwmon_raw      : ok | empty
  - smartctl       : ok | not_in_path | no_devices
  - wmi_msacpi     : ok | com_broken | not_available
  - psutil         : ok (всегда)

И вычислять рекомендации:
  - "Установите lm-sensors: sudo apt install lm-sensors && sudo sensors-detect"
  - "Драйвер NVIDIA не найден. Если у вас GPU NVIDIA, установите проприетарный драйвер."
  - "smartctl не найден. Установите smartmontools для температур дисков."
```

В TUI шапке — однострочная сводка активных backend'ов с цветами (✓/✗).

---

## 5. Phased plan реализации

| Фаза | Что | Файлы | Зависимости |
|---|---|---|---|
| **M1** ← *сейчас* | Research-doc (этот файл), утверждение пользователем | `docs/research/sensor_dashboard_brief.md` | — |
| **M2** | Сбор sample-данных с двух машин пользователя (см. § 6) | — | M1 утверждён |
| **M3** | Новые бэкенды + тесты (без UI-изменений) | `infrastructure/sensors/lm_sensors.py`, `nvidia_ml.py`, `smartctl.py`, `thresholds_default.py`; `application/throttle_detector.py`; расширение `diagnostics_sensors.py`; тесты `tests/unit/test_sensors_{lm,nvml,smartctl}.py`; `pyproject.toml` += `nvidia-ml-py` | M2 (sample-данные) |
| **M4** | Параллельные модели `SensorReading/Snapshot` + `SensorService` + конвертер в `MetricSnapshot` | `domain/sensor_models.py`, `application/sensor_service.py` | M3 |
| **M5** | Адаптивный TUI-экран «Датчики» + sparkline | `interfaces/cli/commands/sensors.py` (новый), `interfaces/cli/sparkline.py`, `interfaces/cli/render_sensors.py`, обновление `menu/screens.py:HomeScreen` | M4 |
| **M6** | SQLite-persistence + миграция + экспорт CSV | `infrastructure/persistence/sensor_repository.py`, новая миграция, `schema.sql` дополнить | M5 |
| **M7** | Документация (`CODEMAP.md`, `HANDOVER.md`, `ARCHITECTURE.md` секция «Датчики»), удаление alias `monitor` (через 1-2 релиза с deprecation-warning), закрытие issue #3 | docs + cleanup | M6 |

**Что критично между фазами:**
- Каждая фаза — отдельный PR/группа коммитов на `dev`.
- Между M3 и M5 старый монитор должен продолжать работать (через конвертер `to_metric_snapshot`).
- После M6 alias `apexcore monitor` ещё работает и логирует deprecation-warning. Удалить в отдельном milestone.

**Риски и про что договариваться отдельно:**
- **Изменения `domain/models.py`** — если пользователь захочет заменить старый `MetricSnapshot` на новый, нужна явная сверка с micro/stress/scoring (там завязаны схемы БД).
- **Web-версия** (issue #13) — после M6 модель данных готова для веб-дашборда трендов.
- **AMD GPU / Intel GPU** — отдельный milestone.

---

## 6. Что нужно от пользователя перед M3

Чтобы я не гадал про реальные имена сенсоров на твоём железе, нужны выводы команд с обеих машин.

### 6.1 Windows (из этого worktree, под админом)

```powershell
# В корне репозитория, в PowerShell — окно само поднимется через UAC
.\new-app\scripts\dev.ps1 doctor

# Полная информация о системе и сенсорах
.\new-app\scripts\dev.ps1 info

# 15-секундный прогон с rate 0.5 — нужны полные ключи temperatures/voltages/frequencies
.\new-app\scripts\dev.ps1 monitor -d 15 -r 0.5

# Если есть NVIDIA GPU:
nvidia-smi -q -d TEMPERATURE,POWER,CLOCK,UTILIZATION

# Если установлен smartmontools:
smartctl --scan
smartctl -j -a /dev/nvme0     # или sda, в зависимости от диска
```

### 6.2 AstraLinux

```bash
# Базовая информация
cat /etc/os-release | head -5
cat /proc/cpuinfo | grep "model name" | head -1
lspci | grep -iE "vga|3d|display"

# hwmon (всегда доступно)
for f in /sys/class/hwmon/*/name; do
    echo "=== $f ==="
    cat "$f"
    dir=$(dirname "$f")
    ls "$dir/" | grep -E "^temp[0-9]+(_label|_input|_max|_crit|_input)?$|^fan[0-9]+_input$|^in[0-9]+_input$" | head -20
done

# lm-sensors (если установлен — установка: sudo apt install lm-sensors && sudo sensors-detect)
which sensors && sensors -j
which sensors && sensors -A      # без adapter prefix — компактнее

# NVIDIA (если есть)
which nvidia-smi && nvidia-smi -q -d TEMPERATURE,POWER,CLOCK,UTILIZATION

# smartctl
which smartctl && smartctl --scan
which smartctl && sudo smartctl -j -a /dev/nvme0n1   # или /dev/sda

# Какая ширина у твоего терминала по умолчанию
echo $COLUMNS
tput cols
```

### 6.3 Референсы UX (опционально, по желанию)

- 1-2 скриншота `btop`, `nvtop`, `HWiNFO sensor view` — если хочешь акцент на конкретный приём.
- Какой terminal эмулятор используешь на AstraLinux (xterm / gnome-terminal / Konsole)? Это влияет на render unicode-блоков.

После этих данных смогу написать детальный M3 implementation plan с реальными именами сенсоров твоего железа (например, `Tctl` vs `Tdie` для Ryzen, `temp1` vs `temp2` для k10temp на твоей версии ядра).

### 6.4 Зафиксированные реальные данные пользователя (2026-05-14)

**Windows · NVIDIA GPU** (RTX 4000-серия, drv 596.21 / CUDA 13.2):

Подтверждённые поля (через `nvidia-smi -q`):

| Поле NVML / nvidia-smi | Реальное значение в примере | Решение для UI |
|---|---|---|
| `Utilization → GPU` | 48 % | ✅ показывать |
| `Utilization → Memory` | 17 % | ✅ показывать |
| `Utilization → Encoder/Decoder/JPEG/OFA` | 0 % каждый | ⚠ показывать только если ≠0 (свернуть в footer) |
| `Temperature → GPU Current Temp` | 50 °C | ✅ основная T° для GPU |
| `Temperature → Memory Current Temp` | **N/A** | ❌ на consumer RTX 30/40 NVIDIA не публикует — gracefully скрывать строку, не показывать «—» |
| `Temperature → GPU T.Limit Temp` | 39 °C | ⚠ это **thermal margin** (запас до slowdown), не абсолютная температура — НЕ показывать как градусы; в pynvml брать абсолютные пороги через `nvmlDeviceGetTemperatureThreshold(NVML_TEMPERATURE_THRESHOLD_GPU_MAX/SLOWDOWN/SHUTDOWN)` |
| `Temperature → GPU Target Temperature` | 88 °C | ✅ показывать как warning threshold |
| `Power → Average Power Draw` | 180.74 W | ✅ показывать |
| `Power → Instantaneous Power Draw` | 182.28 W | ✅ это значение в live-режиме |
| `Power → Current Power Limit` | 364.80 W | ✅ показывать в шапке («лимит: 365 W») |
| `Power → Default Power Limit` | 285.00 W | для tooltip / диагностики |
| `Clocks → Graphics` | 2910 MHz | ✅ |
| `Clocks → Memory` | 10701 MHz | ✅ |
| `Clocks → SM` | 2910 MHz | дубль Graphics на gaming-GPU — не дублировать в UI |
| `Clocks → Video` | 2190 MHz | опционально (encoder clock) |
| `Max Clocks → Graphics` | 3105 MHz | ✅ для отношения current/max |
| `Applications Clocks` | "deprecated" | ❌ не использовать |
| `Clock Samples → Duration/Number/Max/Min/Avg` | "Not Found" | ❌ NVML API упразднён в новых драйверах |
| `Module Power / GPU Memory Power` | N/A | ❌ skipping |
| `EDPp Multiplier` | N/A | ❌ skipping |

**Импликации для `infrastructure/sensors/nvidia_ml.py`**:

```python
# Pseudo-API (M3):
class NvidiaGpuReader:
    def collect(self) -> list[SensorReading]:
        # Безусловно собираем:
        # - util gpu/mem  (NVML_PCIE_UTIL_TX_BYTES is irrelevant; use nvmlDeviceGetUtilizationRates)
        # - temp gpu      (nvmlDeviceGetTemperature, NVML_TEMPERATURE_GPU)
        # - power_w       (nvmlDeviceGetPowerUsage / 1000.0)
        # - clock_graphics, clock_mem (nvmlDeviceGetClockInfo)
        # - fan_pct       (nvmlDeviceGetFanSpeed) — может быть N/A на пассивных
        #
        # Условно (если поле возвращает не-N/A):
        # - temp_mem      (nvmlDeviceGetMemoryInfo + try NVML_TEMPERATURE_GPU; на consumer 4000 будет N/A)
        # - encoder/decoder util — только если > 0
        #
        # Пороги (для colour-coding) — раз в N тиков:
        # - threshold_warn = nvmlDeviceGetTemperatureThreshold(handle, NVML_TEMPERATURE_THRESHOLD_GPU_MAX)
        # - threshold_crit = nvmlDeviceGetTemperatureThreshold(handle, NVML_TEMPERATURE_THRESHOLD_SLOWDOWN)
```

Раздел `Clock Samples` явно «Not Found» в новых драйверах — НЕ опираться на него. Min/Max/Avg считаем сами в `SensorService` из истории `SensorSnapshot`.

**Windows · smartctl** — НЕ установлен на машине пользователя. Решение: на M3 делаем `smartctl` бэкенд опциональным, при отсутствии — в diagnostics показываем «smartctl не установлен» с предложением `winget install smartmontools.smartmontools` или ссылкой на https://www.smartmontools.org/.

**Windows · apexcore doctor / info / monitor** (собрано через `apexcore.exe` без админ-прав 2026-05-14):

**Конфигурация системы:**
- OS Windows 11 24H2 (10.0.26200) AMD64, хост LelbryPC
- CPU: **Intel i9-12900K** — Alder Lake P+E (8P + 8E = 16 phys / 24 log). Базовая частота 3.20 ГГц
- RAM 31.7 ГБ
- GPU: Intel UHD 770 (iGPU) + NVIDIA RTX 4070 Ti + Virtual Desktop Monitor
- Драйвер термосенсоров: ✗ не активен (без админ-прав)

**Активные backend'ы (БЕЗ админ-прав):**

| Backend | Статус | Сенсоров | Подробность |
|---|---|---|---|
| LHM DLL | ✓ | — | DLL найдена (1175 КБ) |
| LHM runtime (pythonnet) | ✓ | 3 | CPU-сенсоров 0 (WinRing0 не зарегистрирован); GPU-сенсоров 3 |
| `psutil.sensors_temperatures()` | ✗ | — | `AttributeError`: на Windows этого метода нет |
| WMI perf-counter Thermal Zone | ✓ | 1 | ACPI thermal zone (НЕ реальная T° CPU) |
| WMI MSAcpi (CIM-fallback) | ✗ | — | MSAcpi не доступен из обычного процесса |
| nvidia-smi | ✓ | 1 | RTX 4070 Ti: T=53°C, нагрузка 75% |

**Активные backend'ы С админ-правами (после регистрации WinRing0):**

| Backend | Статус | Сенсоров | Подробность |
|---|---|---|---|
| LHM DLL | ✓ | — | DLL найдена (1175 КБ) |
| **LHM runtime (pythonnet)** | ✓ | **34** | **CPU: 21, GPU: 3, Tj_max: 16** |
| `psutil.sensors_temperatures()` | ✗ | — | (на Windows всегда AttributeError) |
| WMI perf-counter Thermal Zone | ✗ | — | перфкаунтер не отдаёт данных (отдельный bug? — раньше работал в безадминке) |
| WMI MSAcpi (CIM-fallback) | ✓ | 1 | ACPI thermal zone — **стал доступен под админом** |
| nvidia-smi | ✓ | 1 | RTX 4070 Ti: T=52°C, нагрузка 70% |

**Реальные ключи LHM, полученные через прямой `read_lhm_*()` вызов (без админа = только GPU):**

```
TEMPERATURES (без админа):
  gpunvidia/gpu_core                =  51.00 °C
  gpunvidia/gpu_hot_spot            =  60.78 °C   ← это «T°max» в текущем monitor
  gpunvidia/gpu_memory_junction     =  44.00 °C   ← ВАЖНО: NVML на consumer 40-серии возвращает N/A для mem temp, а LHM здесь даёт реальное значение

VOLTAGES:
  gpuintel/gpu_core                 =   0.307 V   ← Intel UHD 770 iGPU
  gpunvidia/gpu_core_voltage        =   1.145 V   ← RTX 4070 Ti Vcore

TJMAX:        (пусто, требует CPU-сенсоров)
CPU MAX CLOCK: None  (требует админа)
```

**Критическая находка для дизайна**: на Windows для **GPU Memory Junction temp** надо использовать **LHM**, а не pynvml. NVIDIA отключила эту метрику в NVML на consumer RTX 30/40-серии, но LHM получает её через альтернативный API (вероятно NVAPI напрямую). Это меняет приоритеты в § 3.4:

- **Для GPU на Windows**: LHM primary (Memory Junction!), pynvml — для util/fan/power-detail.
- **Для GPU на Linux**: только pynvml (LHM Windows-only) → memory temp будет N/A → gracefully скрывать строку в UI.

**Полный admin-дамп LHM-ключей на i9-12900K + RTX 4070 Ti (2026-05-14, idle):**

**Температуры (34 ключа):**

```
CPU (19):
  cpu/cpu_package           = 44.00 °C   ← package temp (DTS aggregate)
  cpu/core_average          = 32.19 °C   ← среднее по ядрам
  cpu/core_max              = 44.00 °C   ← max по ядрам (= cpu_package обычно)
  cpu/p_core_1..p_core_8    = 30-44  °C  ← 8 P-cores, нумерация с 1 (НЕ с 0!)
  cpu/e_core_1..e_core_8    = 28-30  °C  ← 8 E-cores, нумерация с 1

GPU (3):
  gpunvidia/gpu_core             = 51.00 °C
  gpunvidia/gpu_hot_spot         = 63.50 °C   ← обычно «горячее» значение
  gpunvidia/gpu_memory_junction  = 44.00 °C   ← ВАЖНО: LHM видит, NVML нет

Память (2):
  memory/dimm_1            = 39.50 °C
  memory/dimm_3            = 40.00 °C   ← у пользователя 2 планки в слотах 1 и 3 (не подряд)

Материнка (7):
  motherboard/cpu          = 42.50 °C   ← сенсор на сокете
  motherboard/cpu_socket   = 38.00 °C
  motherboard/m2_1         = 31.00 °C   ← M.2 slot temp
  motherboard/pch          = 49.00 °C   ← чипсет
  motherboard/pcie_x1      = 11.00 °C   ← OUTLIER, явно битый сенсор (или зарезервированный)
  motherboard/system       = 41.00 °C
  motherboard/vrm_mos      = 40.00 °C   ← VRM

Диски (3):
  storage/composite_temperature = 32.00 °C
  storage/temperature           = 29.00 °C
  storage/temperature_2         = 61.85 °C   ← вероятно горячий NVMe SSD под нагрузкой
```

**Напряжения (34 ключа):**

```
CPU (17):
  cpu/cpu_core              = 1.340 V   ← общий Vcore
  cpu/p_core_1..p_core_8    = 1.25-1.35 V   ← per-core VID для P
  cpu/e_core_1..e_core_8    = 1.30-1.38 V   ← per-core VID для E

GPU (2):
  gpuintel/gpu_core              = 0.312 V   ← iGPU UHD 770
  gpunvidia/gpu_core_voltage     = 1.145 V   ← dGPU 4070 Ti

Материнка (15):
  motherboard/vcore             = 1.362 V
  motherboard/12v               = 12.096 V
  motherboard/5v                = 5.060 V
  motherboard/dimm              = 1.400 V
  motherboard/cpu_i_o, cpu_system_agent, cpu_termination, voltage_1, voltage_2, vref, vsb, avcc3, avsb, cmos_battery
```

**Tjmax (16 ключей — все 100°C, классика Alder Lake):**

```
cpu/p_core_1..8 = 100.0 °C
cpu/e_core_1..8 = 100.0 °C
```

**Max CPU clock (через `read_lhm_cpu_max_clock_mhz`):** 4880.73 МГц (≈ 4.88 ГГц — соответствует turbo из `info`).

**Подтверждённые баги для импликаций M3:**

1. **`T°max = 61-63°C` в текущем `monitor` — это НЕ CPU.** В дампе видно, что CPU package = 44°C, а 63°C — это **`gpunvidia/gpu_hot_spot`** или **`storage/temperature_2`** (NVMe SSD под нагрузкой). Текущий `render_metric_snapshot` берёт `max(temperatures.values())` и показывает это как «температура» — пользователь думает «CPU горячий», но это GPU/SSD. **Это и есть та проблема, которую решает раздел «Датчики»**.

2. **`Freq, МГц = 3200` застрял** даже под админом и при активном LHM. Значит источник `cpu_avg` в `MetricSnapshot.frequencies` — это **не LHM**, а `psutil.cpu_freq().current`, который на Windows возвращает базовую (не live). Это **отдельный bug** адаптера: в `WindowsAdapter` нужно использовать `read_lhm_cpu_max_clock_mhz()` или per-core clock из LHM-сенсоров (если они публикуются). В M3 при создании `SensorReading(kind=FREQUENCY)` обязательно брать из LHM, не из psutil.

3. **Throttle counter отсутствует в LHM-дампе.** Нет ключей `cpu/clock_throttle_*`. Это значит на i9-12900K LHM либо не публикует throttle, либо это в другой группе сенсоров (не Temperature/Voltage). В `_collect_temperatures` фильтр `SensorType.Temperature`. Чтобы получить throttle на Windows, в M3 нужен **отдельный сбор `SensorType.Clock` или MSR-чтение** через LHM (`Computer.HardwareItems[i].Sensors[j]` со всеми типами). Подход: в M3 расширить `lhm.py` функцией `read_lhm_throttle_state() -> ThrottleState`.

4. **Per-core VID работает.** Можно показывать Vcore по каждому ядру отдельно — это уникальная фича apexcore'а (в HWiNFO тоже есть). Группировать с per-core temp в одной строке таблицы.

5. **Outlier `motherboard/pcie_x1 = 11.00°C`** — фильтровать в UI (либо скрывать значения вне диапазона 10-150°C, либо помечать «invalid»).

6. **`memory/dimm_3` без `dimm_2`** — у пользователя двухканальная RAM в слотах 1 и 3 (классическая раскладка для Z690). UI должен показывать «DIMM 1: 39.5°C / DIMM 3: 40.0°C» как есть, не пытаться нормализовать в 1-2.

**Импликации для модели `SensorReading` (финал):**

Ключ LHM `cpu/p_core_3` парсится так:
```python
SensorReading(
    group=SensorGroup.CPU,
    device="Intel i9-12900K",          # из info / py-cpuinfo
    sensor="p_core_3",                  # raw key из lhm.py
    label="Ядро P3",                    # для UI
    kind=SensorKind.TEMPERATURE,
    value=36.0,
    unit="°C",
    threshold_warn=None,                # из решения § 7.1 — нет порогов от железа = не подкрашиваем
    threshold_crit=100.0,               # из read_lhm_tjmax()['cpu/p_core_3']
    source="lhm"
)
```

Для GPU `gpunvidia/gpu_hot_spot`:
```python
SensorReading(
    group=SensorGroup.GPU,
    device="NVIDIA GeForce RTX 4070 Ti",
    sensor="hot_spot",
    label="Hot Spot",
    kind=SensorKind.TEMPERATURE,
    value=63.50,
    unit="°C",
    threshold_warn=None,               # NVML вернёт None для memory, дополнительно проверим SLOWDOWN
    threshold_crit=None,
    source="lhm"
)
```

**Mapping LHM-prefix → SensorGroup:**

| LHM prefix | SensorGroup | device |
|---|---|---|
| `cpu/` | CPU | Из `cpu_info.model_name` (из info) |
| `gpunvidia/` | GPU | «NVIDIA GeForce RTX 4070 Ti» (из nvidia-smi name) |
| `gpuintel/` | GPU | «Intel UHD Graphics 770» |
| `gpuamd/` | GPU | имя AMD |
| `memory/` | MEMORY | «RAM DIMM <N>» |
| `motherboard/` | MOTHERBOARD | имя мат. платы (если LHM знает) |
| `storage/` | STORAGE | «NVMe SSD» / «Samsung 980 Pro» (из smartctl, fallback — generic) |

**Известный bug проекта (отдельная задача, не блокирует issue #3)**: после регистрации `WinRing0_1_2_0.sys` процесс apexcore, запущенный **без** админ-прав, всё ещё не получает CPU-сенсоров из LHM (`apexcore doctor` снова показывает «WinRing0 не зарегистрирован?»). Это противоречит инструкции в подсказке `doctor` («Дальнейшие запуски — без UAC»). Гипотезы:
- WinRing0-сервис требует обращения только из admin-token-процессов (DACL на сервисе).
- Сервис не стартует автоматически (`SERVICE_DEMAND_START`) и apexcore не делает `StartService` без админ-прав.
- В новых сборках WinRing0 (с подписью драйвера) поведение поменялось.

**Impact на M3**: на Windows для CPU-температуры **обязательно нужен админ-запуск через `dev.ps1`**. Это уже задокументировано в `CLAUDE.md`. В M3 мы не правим этот bug — он отдельная задача (можно открыть новый issue: «WinRing0 требует админа на каждый запуск»). Раздел «Датчики» при отсутствии CPU-сенсоров корректно покажет «нет данных» (по решению § 7.1).

**Подтверждение проблемы issue #3 на живых данных** — в выводе `apexcore monitor` колонка `T°max` показывает 60-64°C, но **что это** — пользователь не знает:

```
07:21:46 │  0.0%  │ 51.3% │ 3200 МГц │ T°max 61.3 │ ...
```

Это и есть тот самый максимум по `MetricSnapshot.temperatures.values()` — там сейчас лежат:
- GPU temp от LHM (~53°C — пик нагрузки)
- GPU temps от 3-х LHM-сенсоров (Hot Spot ~60-64°C)
- WMI ACPI zone (~30°C, не CPU)

Значение 60-64°C — это GPU Hot Spot. CPU temp отсутствует физически. Пользователь видит «T°max» и интуитивно понимает это как «CPU температура», но это **GPU**. Это **прямое визуальное подтверждение проблемы** в issue #3, которую новый раздел «Датчики» должен исправить — каждый сенсор должен быть подписан явно `[CPU/GPU/MB] [Package/Core 0/Hot Spot/...]`, источник (`source: lhm | wmi | nvidia-smi`) обязателен в модели данных и хотя бы в diagnostics-режиме.

Также подтверждается: **`Freq, МГц` в выводе застрял на `3200`** (= базовая) — без LHM/MSR live-частота на Windows недоступна. Это ещё одна причина критичности админ-запуска для регистрации WinRing0.

**Импликации для M3:**

1. **CPU P+E ядра**: в `lhm.py` сейчас уже есть нормализация `p_core_<n>` / `e_core_<n>`. В новой модели `SensorReading` нужно вынести это в `label`: `Ядро P0` / `Ядро E0` (более явно, чем `Core 0`/`Core 1` индексами).
2. **iGPU + dGPU**: на машине пользователя два GPU. Нужна группировка `device` поля: `device="NVIDIA RTX 4070 Ti"` и `device="Intel UHD 770"` — карточки в TUI отдельные.
3. **Frozen frequency**: без LHM live-частоты на Windows нет. Решение — в `SensorReading(kind=FREQUENCY)` source=`lhm` обязателен; psutil/WMI fallback не дают live данных. Если LHM не активен — показывать «нет данных» (по решению пользователя из § 7.1).
4. **Virtual Desktop Monitor**: в `info` GPU-list попал виртуальный экран. В UI «Датчики» отфильтровать — только физические устройства с активными сенсорами.

**AstraLinux** — на момент 2026-05-14 машина недоступна, sample-данные временно отсутствуют. M3 начинаем с Windows-части бэкендов (LHM расширение + pynvml). Linux-часть (`lm_sensors.py`) откладываем до получения доступа к машине.

### 6.5 Что ещё нужно от пользователя — один админ-запуск

`apexcore doctor` явно пишет инструкцию:

> «DLL загружается, но CPU-сенсоров нет. Это значит, что kernel-драйвер WinRing0 ещё не зарегистрирован в системе. Запустите apexcore ОДИН РАЗ от имени администратора — LHM-lib сама извлечёт WinRing0x64.sys из ресурсов и зарегистрирует kernel-сервис WinRing0_1_2_0. Дальнейшие запуски — без UAC.»

После этого LHM должен публиковать на i9-12900K:
- CPU package + per-core temps (`cpu/package`, `cpu/p_core_<0..7>`, `cpu/e_core_<0..7>`)
- CPU voltages (`cpu/vcore`, `cpu/vid`)
- CPU live frequencies (`cpu/p_core_<n>_clock`)
- Throttle counters

Для админ-запуска используется `dev.ps1` — он сам поднимется через UAC. Подробная пошаговая инструкция — в чате после § 6.

---

## 7. Принятые решения по открытым вопросам (2026-05-14)

После согласования с пользователем:

1. **Threshold defaults — НЕ использовать.** Если железо не публикует пороги (`temp{N}_max/crit` отсутствует, NVML не возвращает threshold), показываем «нет данных» или прочерк, без colour-coding. **Никаких догадочных значений в `thresholds_default.py`** — лучше нейтральная ячейка, чем неверный «жёлтый» на 70°C там, где Tjmax вообще 110°C. Файл `thresholds_default.py` из плана удаляется.

2. **Retention — 10 сессий по умолчанию.** Опция `sensor_history_max_sessions` в Settings (`SettingsStore`), детали UI решаются в M5 (отдельная кнопка в «Настройки» или просто читается из YAML).

3. **Alias `apexcore monitor` — оставляем 1-2 релиза с deprecation-warning.** Решение моё, аргументация: у пользователя могут быть скрипты с `apexcore monitor` (см. CLAUDE.md про повторяемые проверки в PowerShell). Сломать их сразу — лишняя боль. После M5 команда печатает `[deprecated] apexcore monitor → apexcore sensors`, через 1-2 релиза в M7 удаляем.

4. **`domain/models.py` — параллельный `sensor_models.py`.** Старый `MetricSnapshot` остаётся для micro/stress/scoring (там завязка на БД и публичный контракт). Новый `SensorReading/SensorSnapshot` — отдельный файл. Конвертер `to_metric_snapshot()` для обратной совместимости.

5. **Web-версия — отложена до полной готовности CLI.** Не делаем `/api/sensors/{session_id}` в M6. SQLite-схема всё равно строится с прицелом на будущий web (нормализованная таблица `sensor_samples`, индексы на `(session_id, timestamp)`). Web появится в отдельном milestone после M7, не блокирует issue #3.

6. **Focus mode (hotkey `[G]`/`[C]`) — включаем в M5 MVP.** Решение моё, аргументация: на узких терминалах (классический conhost.exe 80×30 / ssh AstraLinux) dashboard-карточки в две колонки не помещаются. Focus mode позволяет на любой ширине развернуть один блок (CPU или GPU) на весь экран — как в nvtop. Реализация — toggle между `Layout` и `Panel` через rich-`Console.size.width` check + hotkey handler. ~30-50 строк кода.

**Обновления, вытекающие из решений:**

- В § 4.2.3 убрать упоминание `thresholds_default.py`. Threshold colour-coding активен **только** если железо публикует пороги; иначе ячейка без подкраски + примечание «нет порогов» в tooltip/diagnostics.
- В § 4.3.4 `sensor_history_max_sessions: int = 10` добавить в `settings_store.py` как новый ключ.
- В § 5 фаза M7 — добавить «удаление alias `apexcore monitor`» (в отдельный релиз после M6).
- В § 5 фаза M5 — focus mode hotkey `[G]/[C]/[M]` обязателен в MVP.

---

## Источники

- LibreHardwareMonitor — https://github.com/LibreHardwareMonitor/LibreHardwareMonitor
- btop — https://github.com/aristocratos/btop
- nvtop — https://github.com/Syllo/nvtop
- s-tui — https://github.com/amanusk/s-tui
- lm-sensors — https://github.com/lm-sensors/lm-sensors
- nvidia-ml-py — https://pypi.org/project/nvidia-ml-py/
- smartmontools — https://www.smartmontools.org/
- Rich (Layout / Live / Table) — https://rich.readthedocs.io/

## Связанные файлы в проекте

- [interfaces/cli/commands/monitor.py](../../src/apexcore/interfaces/cli/commands/monitor.py)
- [interfaces/cli/render.py](../../src/apexcore/interfaces/cli/render.py)
- [application/telemetry_service.py](../../src/apexcore/application/telemetry_service.py)
- [application/diagnostics_sensors.py](../../src/apexcore/application/diagnostics_sensors.py)
- [domain/models.py](../../src/apexcore/domain/models.py)
- [infrastructure/sensors/lhm.py](../../src/apexcore/infrastructure/sensors/lhm.py)
- [infrastructure/sensors/wmi_temps.py](../../src/apexcore/infrastructure/sensors/wmi_temps.py)
- [infrastructure/sensors/hwmon_thresholds.py](../../src/apexcore/infrastructure/sensors/hwmon_thresholds.py)
- [interfaces/cli/menu/screens.py](../../src/apexcore/interfaces/cli/menu/screens.py)
- [ARCHITECTURE.md](../../ARCHITECTURE.md) — секции «Температурные сенсоры (Windows)», «Сенсоры температур: WMI и плавающая COM-ошибка», «CPU-температура: ACPI thermal zone ≠ реальная температура CPU»
- [CLAUDE.md](../../CLAUDE.md) — конвенции, что не трогать
- [HANDOVER.md](../HANDOVER.md) — открытые issues
