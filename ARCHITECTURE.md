# Архитектура apexcore

Версия: **2.0** (scoring v2, май 2026). Предыдущая v1 — в git-history до коммита `c90f578`.

## Общая картина

Hexagonal Architecture (Ports & Adapters) + Clean-слои. Платформенно-независимое ядро
(`domain` + `application`) общается с внешним миром только через порты.

```
                +-------------------+
                |    interfaces/    |   CLI (Typer + интерактивное меню), Web UI (FastAPI, опц.)
                +---------+---------+
                          |
                +---------v---------+
                |   application/    |   use-cases: scoring, stability, telemetry, нормализация, диагностика
                +---------+---------+
                          |
                +---------v---------+
                |     domain/       |   модели (Pydantic) + порты (ABC)
                +---------+---------+
                          ^
                          |  реализуют
                +---------+---------+
                |  infrastructure/  |   OSAdapter (Win/Linux), StressEngine, MicroBench, SQLite, Exporters
                +-------------------+
```

## Шесть функциональных режимов

Приложение делает шесть самостоятельных вещей; они не пересекаются по данным:

| Режим | Источник данных | Балл | Где живёт |
|---|---|---|---|
| **Общая оценка производительности (детальная)** | 12 микробенчмарков (memory/flops/integer/crypto/fractal) | `APEXCORE_SCORE = 1000·R_overall` (Roofline-ratio) | `application/scoring_service.py` |
| **Тест стабильности (стресс-нагрузка)** | стресс-движки + телеметрия + Roofline | Frame-Rate-Stability % + **Стресс-балл** = `10 000·GM(r_dgemm, r_stream, r_stability)` | `application/{stability_service,stress_score,stress_orchestrator}.py`, спека: [`docs/stress_score.md`](docs/stress_score.md) |
| **Общая оценка производительности системы (комплексный бенчмарк)** | DGEMM + STREAM + boot-диск (seq read + random read + seq write) | `10 000·GM(r_dgemm, r_stream, r_disk)` — без cooling, GPU не входит | `application/{general_benchmark,general_benchmark_score}.py`, спека: [`docs/general_benchmark.md`](docs/general_benchmark.md) |
| **Частное тестирование компонента** | подмножество micro-тестов (одна категория) | подскор по категории | через `ScoringService.run_overall(selected_workloads=...)` |
| **Расширенный тест ОЗУ и кеша (Ram&Cache)** | 16 микро-замеров: Read/Write/Copy/Latency × DRAM/L1/L2/L3 | без балла — диагностическая таблица 4×4 (пропускная способность и задержки подсистемы памяти) | `application/ram_cache_service.py`, спека: [`docs/ram_cache.md`](docs/ram_cache.md) |
| **Аналог Windows Winsat** | AES-256 + SHA-1 + memory_read + disk seq + disk random | шкала **1.0–9.9** (Win32_Winsat-формат), WinSPRLevel = min(подскоров) | `application/winsat_service.py`, спека: [`docs/winsat.md`](docs/winsat.md). Только Windows. |

**Семантическая разница «комплексный бенчмарк» vs «стресс»**: оба используют ту же шкалу ×10 000 и тот же подход GM-ratio, но **комплексный** отвечает на вопрос «сколько может выдать система», а **стресс** — «сколько выдержит под нагрузкой» (учитывает термальную стабильность через `r_stability`). Можно сравнивать «бок о бок» — разница покажет, сколько cooling/throttling сажает мощность.

## Слои и ответственности

### `domain/`

Без зависимостей кроме `pydantic`.

- **models** (`models.py`):
  - Системные: `SystemInfo`, `CpuCores`, `MetricSnapshot`.
  - Бенчмарки: `BenchmarkConfig`, `BenchmarkResult`, `StressResult`, `MicroBenchResult`, `MicroBenchSuiteResult`.
  - **Scoring v2**: `OverallScore`, `ThermalStabilityResult` (поля см. `models.py`).
  - Legacy для compare/diagnose: `BaselineProfile`, `NormalizedScore`, `Diagnostic`, `DiagnosticSeverity`.
- **winsat** (`winsat.py`): `WinsatReport`, `WinsatSubscore`, `WinsatStatus` (отдельный файл — не пересекается с публичным контрактом `models.py`).
- **general_benchmark** (`general_benchmark.py`): `GeneralBenchmarkReport` (полный отчёт комплексного бенчмарка: измерения, пики, ratio, score, метаданные boot-диска).
- **ports** (`ports.py`): `OSAdapter`, `StressEngine`, `MetricsBus`, `ResultRepository`, `BaselineRepository`, **`MicroRunRepository`** (новый в v2), **`WinsatRepository`** (v3), **`GeneralBenchmarkRepository`** (v4).
- **errors** (`errors.py`): типизированные исключения.

### `application/` (бизнес-логика)

**Scoring v2 (новое в этой версии):**

- **`roofline.py`** — расчёт теоретических пиков архитектуры по `SystemInfo` (Williams 2009). DRAM bandwidth через PowerShell CIM (Win) / dmidecode (Linux), FLOPS через cores × SIMD × clock, AES-NI/SHA-NI heuristic по cpu_model.
- **`references.py`** — `ReferenceSet` = Roofline + empirical fallback из `data/empirical_reference.yaml`.
- **`weights.py`** — `WeightsProfile` из `data/weights/<name>.yaml`. Дефолт — equal subsystem weights.
- **`scoring.py`** — чистая функция `geomean_score(suite, reference, weights) → OverallScore`. HM внутри категории, GM между категориями, шкала ×1000.
- **`multi_run.py`** — пресеты `fast`/`standard`/`accurate` (n=1/3/5), CI на лог-шкале (Lilja 2000) или bootstrap для асимметрии (Kalibera-Jones 2012).
- **`scoring_service.py`** — `ScoringService.run_overall(preset, ...)` оркестратор: prepares reference, гоняет N прогонов, агрегирует, сохраняет в `MicroRunRepository`.
- **`thermal.py`** — `compute_thermal_stability(metrics_history) → ThermalStabilityResult`. Frame-Rate-Stability % по UL 3DMark.
- **`stability_service.py`** — `StabilityService.run_stability(duration_sec=600, ...)` — 10-минутный stress + thermal-анализ.

**Стресс-балл и комплексный бенчмарк (шкала ×10 000):**

- **`stress_score.py`** — `compute_stress_score(r_dgemm, r_stream, r_stability) → float | None` и `compute_stress_score_context(...)`. Pure. `STRESS_SCORE_SCALE = 10_000`. Спека: [`docs/stress_score.md`](docs/stress_score.md).
- **`general_benchmark_score.py`** — `compute_general_benchmark_score(r_dgemm, r_stream, r_disk) → float | None`. Pure, clamp каждого ratio к ≤ 1.0 (не штрафует топовое железо: NVMe Gen5, AVX-512 без heuristic-detect). `GENERAL_BENCHMARK_SCALE = 10_000`. Дублирует `stress_score.py` намеренно — разная семантика (нет cooling-фактора). Также `_disk_ratio_from_components(seq_read, random_read, seq_write)` собирает `r_disk = GM(трёх компонентов)` с per-component clamp.
- **`general_benchmark.py`** — `GeneralBenchmarkOrchestrator(adapter).run(params, cancel_token, on_progress)`. Последовательный прогон 5 фаз с cooldown 5 с между DGEMM ↔ STREAM (без cooldown disk → CPU не нужен). Без `SafetyGate` / watchdog — нагрузка короткая. Возвращает `GeneralBenchmarkReport`. Не зависит от персистенции — сохранение делает вызывающий код (Screen / CLI).

**Legacy / диагностика:**

- **`benchmark_service.py`** — оркестратор стресс-прогона (для `bench run`). В v2 не вычисляет `final_score` (всегда 0.0); реальный балл живёт в `MicroBenchSuiteResult.overall`.
- **`telemetry_service.py`** — фоновый сэмплер, ring-buffer на 100k снимков, публикация в `MetricsBus`.
- **`normalization.py`** — `normalize_run` / `baseline_from_run(s)` для команды `compare` (regression detection, не публичный балл).
- **`statistics.py`** — Welch t-test, Mann-Whitney U, Shapiro-Wilk, Cohen's d.
- **`diagnostics.py`** — правила анализа `metrics_history` (термотротлинг, частотная вариативность).
- **`trends.py`** — rolling mean / p95 для временных рядов.

### `infrastructure/`

- **`adapters/`**: `WindowsAdapter` (psutil + WMI/CIM), `LinuxAdapter` (psutil + `/sys/class/hwmon`), `AdapterFactory.detect()`. Температуры на Windows читаются гибридным pipeline через `infrastructure/sensors/` — см. ниже.
- **`sensors/`** (Windows-only источники температур):
  - `lhm.py` — внутрипроцессное чтение через **LibreHardwareMonitorLib** (pythonnet + bundled DLL). Покрытие: CPU package, per-core, GPU, материнская плата, VRM. Заменяет внешний процесс LibreHardwareMonitor (issue #17). **С v2.1**: каждая `read_lhm_*` функция сначала пробует shared-memory snapshot от `apexcore_sensord` (см. ниже), и только при недоступности — прямой LHM-collector. Это даёт UAC-free режим: live-сенсоры читаются из обычного non-admin процесса.
  - `wmi_temps.py` — `read_perf_counter_thermal_zone` и `read_msacpi_thermal_zone` как fallback'и без сторонних DLL.
  - `lib/` — bundled DLL'и LHM (MPL-2.0), HidSharp (Apache-2.0), драйвер PawnIO (LHM v0.9+ перешёл с WinRing0 на PawnIO). Скачивается `scripts/fetch_lhm.ps1` во время сборки.
- **`services/`** — long-running системные сервисы под повышенными привилегиями:
  - `sensord.py` — Windows-сервис `apexcore_sensord` через `win32serviceutil.ServiceFramework`. Запускается как LocalSystem, держит `Computer` LHM открытым весь life-time, каждые 250 мс пишет snapshot всех сенсоров (Temperature/Voltage/Power/Fan/Clock/Tj_max) в Global shared memory `Global\apexcore_sensors`. Поддерживает frozen-режим (PyInstaller bundle) — в `main()` есть ветка для SCM-старта `sensord.exe` без аргументов.
  - `shm_layout.py` — чистый pack/unpack бинарного формата snapshot'а (header 24 B + N записей переменной длины: u16 key_len, UTF-8 key, f32 value). Буфер 64 КБ.
  - `shm_adapter.py` — read-only клиент Global mapping через прямой Win32 API (`OpenFileMappingW(FILE_MAP_READ)` + `MapViewOfFile` через ctypes; Python `mmap.mmap` для read-only Global mapping не работает — он запрашивает FILE_MAP_WRITE). API: `read_shm_snapshot()`, `read_shm_temperatures()`, `read_shm_cpu_power()`, и т.д. — drop-in замена `read_lhm_*` функций.
- **`stress/`**: 4 встроенных (`builtin_cpu_int/fp`, `builtin_ram_bw/lat`) + `builtin_large_dgemm` / `builtin_large_stream` (для стресса и комплексного бенчмарка) + 4 внешних (`stress-ng-cpu/vm/matrix`, `prime95`). `StressRegistry`, `profile_engines()`.
- **`microbench/`**: 12 тестов в 5 категориях + диск; `time_loop` с поддержкой `cancel_token`. `build_default_microbench_registry()`.
  - **`disk.py`** — `DiskSequentialReadBench`, `DiskRandomReadBench`, `DiskSequentialWriteBench`. Все принимают опциональный `target_dir: Path | None` в конструкторе (по умолчанию `tempfile.gettempdir()`). Write-бенч — **один проход** ровно `FILE_SIZE_MB` (256 МБ) + fsync, не циклится; минимизирует износ SSD (~1 GB записи за прогон комплексного бенчмарка с warmup; при типовом TBW NVMe 600 ТБ это ~600 000 запусков).
- **`disk_inventory.py`** — `list_physical_disks()` (через `Get-PhysicalDisk` / `lsblk -J`) + **`get_boot_drive_path() → str`** и **`get_boot_drive(disks=None) → tuple[str, PhysicalDisk | None]`**. Boot detection: `%SystemDrive%` на Windows (case-insensitive перебор `os.environ`), `Path.home().drive` как fallback; mount-point `/` на Linux.
- **`disk_peak.py`** — `DiskPeakProfile` и `lookup_disk_peak(media_type, bus_type)`. Фиксированная таблица типовых пиков для трёх паттернов IO: NVMe (3500/600/2500 MB/s), SATA SSD (550/400/500), HDD (200/5/180; random — seek-killer). Gen3/4/5 NVMe не различаются — clamp ≤1.0 в формуле спасает топовое железо. Источник детерминизма балла «бок о бок» для идентичных систем.
- **`persistence/`**: `SqliteResultRepository` (для stability-runs), **`SqliteMicroRunRepository`** (для scoring v2), **`SqliteWinsatRepository`** (v3), **`SqliteGeneralBenchmarkRepository`** (v4), `SqliteBaselineRepository`. Schema v4 в `schema.sql`. Миграции в `migrations.py`: `v1 → v2` дропает старые таблицы (несовместимая шкала), `v2 → v3` / `v3 → v4` additive (только `CREATE IF NOT EXISTS`).
- **`exporters/`**: JSON / CSV.

### `interfaces/`

- **`cli/`**: Typer-приложение. Подкоманды: `info`, `monitor`, `stress`, `bench`, `runs`, `export`, `webui`, `micro`, `winsat`, `ram-cache`. **Команда `micro run --preset {fast,standard,accurate}` запускает scoring v2.** `commands/runs.py` объединяет 4 типа прогонов: `stress`, `micro`, `winsat`, `general` (комплексный бенчмарк).
- **`cli/menu/`**: интерактивное меню (rich-based) — `nav.py` (Screen/MenuLoop), `screens.py` (HomeScreen, CpuTestsScreen, RamCacheScreen, StressScreen, HistoryScreen, SettingsScreen), **`benchmark_screen.py`** (`BenchmarkScreen` — подменю «Общая оценка производительности системы» = комплексный бенчмарк + Winsat), `winsat_screen.py` (`WinsatScreen`), `cancel.py` (Ctrl+C → threading.Event), `settings_store.py` (durations YAML), `runners.py` (общий progress-bar для micro).
  - **HomeScreen порядок** (важно для тестов / UX): 1 Информация · 2 Датчики · 3 Стресс-нагрузка · 4 **Общая оценка производительности системы** · 5 CPU · 6 Ram & CPU Cache · 7 История ваших тестов · 8 Web UI · 9 Настройки · q Выход. Старый прямой пункт Winsat убран — он живёт внутри BenchmarkScreen.
- **`cli/render.py`**: rich-таблицы, **`render_overall_score`**, **`render_thermal_stability`**, **`render_general_benchmark_report`** (отчёт комплексного бенчмарка: `box.ROUNDED` таблица «Итоги по подсистемам» с подсветкой процентов ≥70/40-70/<40 → green/yellow/red, Panel-пояснение «% от максимума», центрированная плашка «Ваш итоговый балл», отдельный Panel «Шкала баллов», dim-rule перед `Enter`).
- **`webui/`** (опционально): FastAPI на `127.0.0.1`, Chart.js. **Не обновлялся под scoring v2 / комплексный бенчмарк** (issue: показывает legacy `final_score`).

#### CLI-меню: совместимость с разными терминалами и раскладками

**Проблема: «наслоение» меню в classic Windows PowerShell (conhost.exe).** Rich
очищает экран через ANSI escape `\x1b[2J\x1b[H`. В современном Windows Terminal
это работает корректно, но в classic conhost.exe (заголовок окна
«Администратор: Windows PowerShell») та же последовательность очищает
**только видимую область** и оставляет хвосты от прошлых выводов на тех
строках, где новый вывод (например, `Panel.fit`) уже старого по ширине.

**Решение** — патч в `src/apexcore/interfaces/cli/render.py`:
`console.clear` подменён обёрткой `_hard_clear`, которая на Windows
дополнительно вызывает нативный `os.system("cls")`. Это гарантирует
полную очистку экрана и скроллбэка во всех Windows-консолях. Все вызовы
`console.clear()` (включая `MenuLoop.render` в `nav.py`) автоматически
используют патч, так как импортируют тот же `console` объект из `render`.
**Не удалять патч при рефакторинге render.py** — без него меню «наслаивается»
у пользователей classic conhost.

**Кросс-раскладка ввода (RU/EN).** В `nav.py` глобальные ключи навигации
расширены однобуквенными русскими эквивалентами на тех же физических
клавишах: `b/и` (назад, B = И), `h/р` (главная, H = Р), `q/й` (выход, Q = Й).
Полные слова (`назад`, `главная`, `выход`, `помощь`, `домой`) приняты как
до правок. Подтверждения y/n принимают `y/yes/д/да` и `n/no/н/нет` —
здесь raw layout-mapping не применяется, потому что русская «н» (на
клавише Y) семантически означает «нет», иначе RU-пользователь, нажав ту
же клавишу что и yes, получил бы no.

**Атавизм частоты ЦП.** В `render_stress_final_report` и
`render_thermal_stability` намеренно не показывается строка «Частота ЦП
min/max МГц». На большинстве сборок Windows-адаптера значения
`cpu_min` / `cpu_max` приходят из `cpu_model_name` (статический base
clock), не из реальных performance counters — поэтому min == max и
отображение визуально статично. Если когда-нибудь адаптер начнёт
отдавать реальные текущие частоты — строку можно вернуть. В info /
monitor частота оставлена, потому что там это live-метрика текущего
состояния системы, не результат прогона.

#### Сенсоры температур: WMI и плавающая COM-ошибка

`infrastructure/sensors/wmi_temps.py` содержит четыре источника
температуры для Windows. `read_msacpi_thermal_zone()` использует
Python-пакет `wmi` как primary, с fallback на PowerShell `Get-CimInstance`.

**Известная проблема: COM-ошибка при импорте `wmi` из background-потока.**
Сам `import wmi` на module-level выполняет `GetObject("winmgmts:")`, что
требует **инициализированного COM-апартмента в текущем потоке**. Когда
`TelemetryService._run` крутится в отдельном threading-потоке (а COM в
этом потоке не инициализирован), импорт падает с
`com_error: -2147221020 (MK_E_SYNTAX)`. Ошибка плавающая, потому что
зависит от того, был ли COM ранее проинициализирован другими модулями
(например, pythonnet/LHM в main-thread) до момента первого импорта.

**Решение в `wmi_temps.py`:**
- блок `except` при `import wmi` ловит `Exception` (а не только
  `ImportError`) — любая COM-ошибка трактуется как «пакет недоступен»;
- module-level флаг `_WMI_PACKAGE_BROKEN` кэширует факт неудачи —
  повторные тики телеметрии (раз в 0.5 с) сразу идут в CIM-fallback
  без попытки бесполезного импорта;
- CIM-fallback использует `subprocess powershell + Get-CimInstance`,
  который не требует COM в Python и работает из любого потока.

**Не сужать `except` обратно** до `ImportError` — это вернёт плавающую
ошибку, которую трудно отлавливать в проде.

#### CPU-температура: ACPI thermal zone ≠ реальная температура CPU

`_is_cpu_temp_key` в `application/thermal_watchdog.py` определяет, какие
ключи из `MetricSnapshot.temperatures` считать CPU-температурой
(используется в watchdog, в строке статуса стресс-теста и в финальном
отчёте). Принимаются только «настоящие» CPU-сенсоры: `cpu/*`, `core*`,
`coretemp/*`, `k10temp/*`, `package`, `tdie`, `tctl`, `ccd`, `ccx`.

**ACPI thermal zone (`thermal_zone_*`, `\thermal zone information(*)\temperature`)
намеренно НЕ принимается как CPU.** Эти зоны на Windows доступны через
WMI perf-counter / `MSAcpi_ThermalZoneTemperature`, но физически это
температура корпуса/чипсета — статичная (~25–30 °C), не растёт даже при
полной загрузке CPU. Пользователь сравнивал с AIDA64 и убедился: реальная
температура ядер CPU читается через DTS (Digital Thermal Sensor) MSR,
для которого нужен драйвер уровня ядра (`WinRing0`, входит в LHM).

**Если LHM не загружается** (`Tj_max = 100 °C` от fallback) — реальной
CPU-температуры в apexcore не будет; лучше показать «нет данных», чем
ложно-низкие 25–30 °C, при которых thermal watchdog никогда не сработает.

В шапке прогона (`stress_menu._detect_cpu_temp_source`) отображается
явная диагностика: «✓ LibreHardwareMonitor (DTS ядер CPU)» или
«✗ температура CPU не считывается …». Это даёт пользователю понять,
будет ли в строке статуса реальная температура.

### `shared/`

- `config.py` — `pydantic-settings`, чтение `.env` / YAML.
- `logging_setup`, `units`, `timing`.

## Scoring v2 — формула

**Спецификация:** [`docs/scoring_v2.md`](docs/scoring_v2.md).
**Исследовательская база:** [`../docs/research/aggregated_overall_performance_assessment.md`](../docs/research/aggregated_overall_performance_assessment.md).

```
1. Per-workload ratio:
   r_ij = measured_value / reference_value     (Roofline или empirical proxy)

2. Внутри категории — HM (Smith 1988):
   r_memory  = HM(memory_read, memory_write, memory_copy)
   r_flops   = HM(flops_sp, flops_dp)
   r_integer = HM(int_iops_24, int_iops_32, int_iops_64)
   r_crypto  = HM(aes_256, sha1)
   r_fractal = HM(julia_sp, mandelbrot_dp)

3. Между категориями — weighted GM (Fleming-Wallace 1986):
   R_MEM         = r_memory
   R_CPU_compute = GM_w(r_flops, r_integer, r_crypto, r_fractal)

4. Между подсистемами — weighted GM:
   R_overall = GM_w(R_MEM, R_CPU_compute)

5. Шкала ×1000 (BAPCo SYSmark стиль):
   APEXCORE_SCORE = 1000 · R_overall
```

CI на лог-шкале (Lilja 2000):
```
y_j = ln(R_j)
ȳ ± t_{0.975, n-1} · s_y/√n  →  exp(...)  →  CI(R)  ×1000  →  CI(score)
```

## Общая оценка производительности системы — формула

**Спецификация:** [`docs/general_benchmark.md`](docs/general_benchmark.md). Идейно дублирует стресс-балл (`docs/stress_score.md`), но **без cooling-фактора** и с диском вместо стабильности частот.

```
1. Per-subsystem ratio (с clamp ≤ 1.0):
   r_dgemm  = min(measured_dgemm_gflops / compute_flops_peak("dp"),  1.0)
   r_stream = min(measured_stream_gb_s  / (compute_dram_peak() / 1000), 1.0)

2. Disk ratio собирается отдельно:
   r_disk = GM(
       min(seq_read_mb_s    / peak_seq_read,    1.0),
       min(random_read_mb_s / peak_random_read, 1.0),
       min(seq_write_mb_s   / peak_seq_write,   1.0),
   )
   где peaks — из infrastructure/disk_peak.lookup_disk_peak(media, bus)

3. Итог:
   R_overall = GM(r_dgemm, r_stream, r_disk)
   score     = round(10 000 · R_overall)
```

Если **любой** из трёх ratio = `None` → `score = None` (та же семантика, что в стресс-балле). Типичные значения: 4000-6000 десктоп, 6000-8000 мощная конфигурация, 10 000 теоретический потолок.

## Поток комплексного бенчмарка

```
TUI (BenchmarkScreen._run_general)
    └─ GeneralBenchmarkOrchestrator(adapter).run(params, cancel_token, on_progress)
        1. adapter.get_system_info() → SystemInfo
        2. compute_flops_peak("dp") + compute_dram_peak() → roofline peaks (могут быть None)
        3. get_boot_drive() → (path, PhysicalDisk | None) → lookup_disk_peak(...)
        4. shutil.disk_usage(boot_path) — sanity-check ≥ 1 ГБ свободно
        5. Фаза DGEMM (~30 с)   ─── BuiltinLargeDgemmEngine.run() ──→ GFLOPS
        6. Cooldown 5 с (термальная пауза)
        7. Фаза STREAM (~30 с)  ─── BuiltinLargeStreamEngine.run() ─→ GB/s
        8. Cooldown 5 с
        9. disk_seq_read       ─── DiskSequentialReadBench(target_dir=boot_path)
       10. disk_random_read    ─── DiskRandomReadBench(...)
       11. disk_seq_write      ─── DiskSequentialWriteBench(...) — один проход 256 МБ + fsync
       12. compute ratio + clamp + GM + ×10 000 → score
       13. Вернуть GeneralBenchmarkReport (Pydantic)
    └─ SqliteGeneralBenchmarkRepository.save(report) (тихо, без вывода UUID)
    └─ render_general_benchmark_report(report)
```

Sanity-warnings (в `report.notes`): если `stream_peak < 5 GB/s` → подозрение на виртуальную среду; если `< 1 ГБ` свободно на boot — фаза диска пропускается, `score = None`.

## Поток `apexcore micro run --preset standard`

```
CLI (commands/micro.py)
    └─ _run_with_scoring(preset='standard', ...)
        └─ ScoringService.run_overall(...)
            1. AdapterFactory.detect() → SystemInfo
            2. build_reference(SystemInfo) → ReferenceSet (Roofline + empirical)
            3. load_weights("default") → WeightsProfile
            4. Цикл n_runs раз (для standard = 3):
                run_microbench_suite(tests, duration, threads, cancel)
                  └─ time_loop с cancel_token
            5. multi_run.aggregate_multi_run(suites, ref, weights, "standard")
                  └─ median-of-3 на per-workload values
                  └─ scoring.geomean_score(...) → OverallScore
            6. SqliteMicroRunRepository.save(suite)
            7. CLI: render_microbench_suite + render_overall_score
```

## Поток теста стабильности 10 минут

```
CLI/UI
    └─ StabilityService.run_stability(duration_sec=600)
        1. AdapterFactory.detect() → SystemInfo
        2. TelemetryService.start() ─── сэмплирует ──→ MetricsBus
                                                       └─ UI: rich.Live (live-таблица)
        3. Параллельно: stress-движки профиля
        4. TelemetryService.stop() → metrics_history
        5. compute_thermal_stability(metrics_history) → ThermalStabilityResult
        6. CLI: render_thermal_stability (PASS/FAIL @ 97%)
```

## Температурные сенсоры (Windows)

С релизом v0.5.1 чтение температур построено вокруг **диффренцированного
fallback-chain** с probe-фазой на старте. Главный сдвиг по сравнению с
v0.5.0: отказ от прямой зависимости на WinRing0 (CVE-2020-14979, Microsoft
Vulnerable Driver Blocklist, Defender карантинит сигнатуру) — теперь LHM
один из источников, а первый приоритет идёт через чтение Shared Memory
индустриальных утилит мониторинга (HWiNFO, CoreTemp). Это даёт качество
данных силиконового уровня **без admin** и **без зависимости** от
HVCI/SAC/AV. Подробное обоснование — в `docs/research/research_Надежное_
чтение_датчиков_markdown.md`.

### Probe-фаза при старте

`infrastructure/sensors/probe.py` собирает один раз за процесс снимок
системы:

- через `winreg`: установленные .NET runtime, HVCI / Memory Integrity,
  Smart App Control, Vulnerable Driver Blocklist;
- через ctypes `OpenFileMapping`: наличие SHM-объектов HWiNFO
  (`Global\HWiNFO_SENS_SM2`), CoreTemp (`CoreTempMappingObjectEx`),
  AIDA64 (`AIDA64_SensorValues`);
- через PowerShell subprocess: AV-vendor (Defender / Avast / ...),
  Defender quarantine для WinRing0;
- через `platform.machine()`: x64 / ARM64 / x86.

Результат — `ProbeResult` (frozen Pydantic, см. `domain/sensor_models.py`).
Кэшируется in-memory на жизнь процесса (без disk-cache — пользователь
может установить HWiNFO в любой момент).

### Fallback chain в `WindowsAdapter._read_sensors`

```
```
1. HWiNFO Shared Memory  (probe → доступен? читаем через ctypes)
   └─ shm/hwinfo.py: парсинг HWiNFOSensor[] / HWiNFOEntry[]
   └─ нормализация temps через shm/_common.normalize_sensor_key(),
      voltages — через normalize_voltage_key() (P1.5)

2. CoreTemp Shared Memory (probe → доступен?)
   └─ shm/coretemp.py: CoreTempSharedDataEx с ucDeltaToTjMax / ucFahrenheit

3. AIDA64 Shared Memory (probe → доступен?) — P1.2
   └─ shm/aida64.py: regex-парсер pseudo-XML строки AIDA64_SensorValues
   └─ нормализация temps + voltages через те же helpers, что HWiNFO

4. AMD Ryzen Master DLL runtime-discovery — P1.4, AMD-only
   └─ ryzen_master.py: ctypes-обёртка над Platform.AMD.RyzenMaster.dll
      (DLL не редистрибутируется, только runtime-discovery установленной
      у пользователя версии); needs verification on AMD desktop

5. LHM с shm-first fallback (lhm.py)
   └─ С v2.1: каждая read_lhm_* функция сперва пробует snapshot из
      Global\apexcore_sensors через services/shm_adapter.py (UAC-free
      путь, см. секцию ниже). Если snapshot недоступен/протух — прямой
      collector через pythonnet → LibreHardwareMonitorLib.dll → PawnIO
      driver. Прямой путь требует admin для CreateFile(\\.\PawnIO).
   └─ PawnIO заменил WinRing0 в LHM v0.9+ (WHQL-подписанный драйвер).
      Может не загрузиться под HVCI / SAC / Defender quarantine.

6. psutil.sensors_temperatures() — обычно пуст на Windows

7. WMI perf-counter Thermal Zone (Get-Counter)
   └─ фильтр 25-30°C: если все значения статичны → quality="approximate"

8. WMI MSAcpi_ThermalZoneTemperature
   └─ P1.3: dedicated worker thread с COM apartment (queue.Queue
      request/response, timeout 2 с) → legacy import → CIM-fallback
      через PowerShell Get-CimInstance; safety-net `_WMI_PACKAGE_BROKEN`
      сохранён как инвариант (см. ARCHITECTURE.md)
```

Любая ошибка любого источника → graceful degrade на следующий, наружу
не ломает.

### apexcore_sensord — long-running LHM host для UAC-free режима

**Проблема.** Драйвер PawnIO даёт user-mode коду доступ к MSR/PCI
через подписанные AMX-скрипты. Сам сервис PawnIO в SCM может быть
`start=auto` (см. `install_pawnio_service.ps1`). Но **AMX-скрипты**
(`IntelMSR.bin`, `LpcIO.bin` и т.п.) загружаются в драйвер на handle:
один handle = один blob. Первичное открытие \\.\PawnIO через CreateFile
требует admin — SDDL драйвера (`D:P(A;;GA;;;SY)(A;;GA;;;BA)`)
разрешает доступ только SY/BA. Без sensord каждый запуск apexcore
просит UAC.

**Решение.** Сервис `apexcore_sensord` (LocalSystem, autostart) при
старте однократно делает Computer.Open() через LHM, которая через
PawnIOLib.dll грузит все нужные AMX-blob'ы и удерживает executor'ы
живыми. Каждые 250 мс — Computer.Update() и pack_snapshot() в
shared memory `Global\apexcore_sensors` (108+ значений типов temp /
volt / power / fan / clock / tjmax). ACL mapping'а
(`D:P(A;;GR;;;WD)(A;;GA;;;SY)(A;;GA;;;BA)`) даёт Everyone:GENERIC_READ,
поэтому non-admin процессы открывают view через OpenFileMappingW +
MapViewOfFile (см. services/shm_adapter._open_global_mapping).

Где встраивается в pipeline: shm-first логика в lhm.py (уровень 5
fallback chain). Если sensord установлен и Running — read_lhm_*
вернут данные из mapping за O(1) без admin. Если нет — прямой LHM
с обычными ограничениями.

**Поставка.** В коробке (`installer.iss`) sensord-сервис ставится
как отдельная задача «Установить apexcore_sensord». Под капотом —
отдельный PyInstaller-бандл `apexcore-sensord.exe`
(`packaging/windows/sensord.spec`, self-contained: свой Python + pywin32
+ LHM DLL'и), который Inno Setup кладёт в `{app}\apexcore-sensord\`.
`scripts/install_sensord_bundle.ps1` дёргает
`apexcore-sensord.exe install` (win32serviceutil узнаёт frozen-
режим и пишет sys.executable в SCM как binPath) +
`sc.exe config + start`. При удалении apexcore'а [UninstallRun]
снимает сервис.

**Dev-режим** (без коробки, editable install в venv) использует
`scripts/install_sensord.ps1` — он делает 6 этапов pywin32+venv
танцев (см. секцию «AMX persistent loader» в ARCHITECTURE.md).
Production-EXE этих сложностей не имеет.

**Graceful fallback.** Если сервис не установлен или упал, lhm.py
автоматически идёт на прямой LHM (с admin-требованием).
Существующие пользователи без обновления коробки не ломаются.

### Capability matrix (P1.1)

`application/diagnostics_sensors.build_capability_summary(snap)` собирает
одну строку для `apexcore info`:

```
Capability: HWiNFO SHM+NVML (silicon CPU/GPU, Vcore доступен)
Capability: ACPI zone (approximate CPU)
Capability: Источников нет (CPU не считывается — HVCI блокирует драйвер,
            см. `apexcore doctor`)
```

Источник CPU берётся из side-channel `windows.get_last_cpu_temp_source()`;
GPU — из `nvidia_ml.is_available()`; Vcore — из `snap.voltages`.

### Дифференциация причин отказа (`DegradedReason`)

`application/diagnostics_sensors.py` классифицирует отказы LHM в
конкретный `DegradedReason`:

| Reason | Что значит | UX-совет |
|---|---|---|
| `NO_LHM_DLL` | DLL не в `sensors/lib/` | `apexcore doctor --repair` |
| `NO_DOTNET_RUNTIME` | pythonnet не нашёл runtime | Установите .NET 9 |
| `HVCI_BLOCKED` | Memory Integrity активен | PawnIO или HWiNFO/CoreTemp |
| `SAC_BLOCKED` | Smart App Control | PawnIO |
| `DEFENDER_BLOCKED` | Defender carantine WinRing0 | PawnIO |
| `AV_BLOCKED` | Avast/Kaspersky/AVG | Исключение в AV или PawnIO |
| `NO_ADMIN` | WinRing0 ещё не зарегистрирован | Один запуск под admin |
| `CPU_UNSUPPORTED` | LHM не знает чип | Обновить apexcore |
| `ACPI_FAKE_ZONE` | Битый OEM DSDT, 25-30°C статично | HWiNFO для реального DTS |
| `ARM_PLATFORM` | Snapdragon X | Hard limit (Qualcomm SPU) |

Все backend'ы возвращают `BackendStatus` с опциональным `reason` —
дедуплицируется в `SensorDiagnostics.degraded_reasons` для UX-баннера
и `docs/troubleshooting.md` дерева решений.

### Качество данных

Snapshot помечается одним из трёх уровней:
- **silicon** — реальные DTS-сенсоры (HWiNFO/CoreTemp/LHM/psutil);
- **approximate** — ACPI thermal zone (фильтр 25-30°C, ARM chassis);
- **unavailable** — никакой источник не сработал.

Module-level в `windows.py`: `_last_cpu_temp_source` /
`_last_cpu_temp_quality` — side-channel для UX-баннера в
`render.py::_cpu_temp_degraded_inline()`.

### Поставка (см. `packaging/windows/`)

- **LHM DLL в git** (с v0.5.1): 24 файла в `sensors/lib/`. MPL-2.0
  разрешает (см. NOTICE.md). Уход от runtime-fetch при первом запуске —
  одна failure mode исключена. `fetch_lhm.ps1` остаётся идемпотентным
  для обновлений LHM-версии.

- **Bundled .NET 9 в Inno Setup installer** (с v0.5.1): `scripts/
  fetch_dotnet9.ps1` скачивает framework-dependent .NET 9 runtime
  (~70 МБ), `build_windows.ps1` копирует его в `dist/apexcore/dotnet/`.
  В runtime `lhm._configure_runtime` авто-обнаруживает bundled-папку
  через `_find_bundled_dotnet_root()` — не нужен `APEXCORE_DOTNET_ROOT`.
  Используется .NET 9 (а не .NET 8) — обход pythonnet issue #2595.

- **WinRing0** остаётся embedded resource в LHM DLL (для случаев, когда
  HVCI/SAC выключены). При первом admin-запуске LHM-lib извлекает
  `WinRing0x64.sys` и регистрирует kernel-сервис `WinRing0_1_2_0`. Для
  Secured-core / HVCI пользователю рекомендуем PawnIO (https://pawnio.eu)
  как signed-alternative.

### Self-repair

`apexcore doctor --repair` (новое в v0.5.1) — интерактивный режим:

- если `NO_LHM_DLL` → предложить запустить `fetch_lhm.ps1`;
- если `NO_DOTNET_RUNTIME` и есть bundled dotnet/ → подсказать env-vars;
- остальные `DegradedReason` → инструкции через `advice_lines`.

Никаких автоматических destructive-действий — каждый шаг требует
подтверждения пользователя.

### AstraLinux

Тестовый эталон: **Astra Linux SE 1.8.5.46** (бюллетень **№ 2026-0224SE18**,
**11.02.2026**, ядро **6.1.158-1-generic**, Debian 12 base). Полный
паспорт стенда + список того, что доступно из коробки + известные
пробелы покрытия — `docs/Astra/test_environment.md`. Журнал реальных
проблем со сборкой/установкой и их фиксов — `docs/Astra/problems_fixes.md`,
короткая шпаргалка — `docs/Astra/install_pitfalls.md`.

`/sys/class/hwmon` — **первичный** источник T° GPU/диска: работает без
root и сторонних демонов, kernel-drivers (`amdgpu`, `i915`, `xe`, `nvme`,
`drivetemp`) сами публикуют sysfs-узлы. `LinuxAdapter._read_hwmon`
маппит chip name на LHM-совместимый префикс (amdgpu/radeon → `gpuamd/`,
i915/xe → `gpuintel/`, nvme/drivetemp → `storage/<chip>_`), благодаря
чему данные сразу попадают в `parse_legacy_key` → SensorSnapshot →
WebUI sensor-cards без расширения mapping'ов в `application/sensor_keys.py`.
Bundling .NET 9 / DLL на Linux не выполняется (pythonnet/LHM не
используются — Linux kernel сам отдаёт MSR через hwmon).

`smartctl` на kernel 6.1+ для NVMe требует **CAP_SYS_ADMIN**, а не
`cap_sys_rawio` — последняя устарела как workaround. Поэтому apexcore
полагается на kernel hwmon для T° NVMe, а `setcap cap_sys_rawio+ep`
из wizard'а оставлен только для SATA/scan-сценариев и SMART-attributes.
Подробности — `problems_fixes.md` #10.

## Persistence: схема SQLite v4

Файл `infrastructure/persistence/schema.sql`:

| Таблица | Для чего | Главные поля |
|---|---|---|
| `runs` | Стресс-прогоны (от `BenchmarkService`) | `id`, `profile_name`, `start_time`, `final_score=0`, `payload_json` |
| `baselines` | Baseline-профили для compare | `id`, `name`, `system_fingerprint`, `payload_json` |
| **`micro_runs`** (v2) | Scoring v2 — общая оценка детальная | `id`, `preset`, `n_runs`, `overall_score`, `ci_lower/upper`, `scoring_version`, `payload_json` |
| **`winsat_runs`** (v3) | Аналог Win32_Winsat | `id`, `started_at`, `cpu_score`, `memory_score`, `disk_score`, `winspr_level`, `payload_json` |
| **`general_benchmark_runs`** (v4) | Комплексный бенчмарк, шкала ×10 000 | `id`, `started_at`, `score`, `dgemm_gflops`, `stream_gb_s`, `disk_seq_read/random_read/seq_write_mb_s`, `disk_media_label`, `payload_json` |
| `schema_version` | Версия схемы (текущая = 4) | `version` |

Миграции в `migrations.py`:
- **v1 → v2**: дроп `runs` + `baselines` (старые баллы scoring v1 несовместимы с новой шкалой). Свежие БД создаются сразу с актуальной версией.
- **v2 → v3**: additive (`CREATE IF NOT EXISTS winsat_runs`).
- **v3 → v4**: additive (`CREATE IF NOT EXISTS general_benchmark_runs`).

Repos: `SqliteResultRepository`, `SqliteBaselineRepository`, `SqliteMicroRunRepository`, `SqliteWinsatRepository`, `SqliteGeneralBenchmarkRepository` (все экспортируются из `infrastructure/persistence/__init__.py`). Каждый имеет `resolve_id(prefix)` для CLI-команд `runs show <prefix>` / `delete <prefix>`.

## Применяемые паттерны

- **Ports & Adapters** — все внешние эффекты только через интерфейсы из `domain.ports`.
- **Strategy** — стратегии нормализации, пресеты scoring (fast/standard/accurate), стат-теста.
- **Factory / Registry** — `AdapterFactory.detect()`, `StressRegistry.available()`, `build_default_microbench_registry()`.
- **Observer / Pub-Sub** — `MetricsBus`: коллектор → подписчики (CLI, БД, websocket).
- **Repository** — `ResultRepository`, `BaselineRepository`, `MicroRunRepository`.
- **Use-Case (Application Service)** — `ScoringService`, `StabilityService`, `BenchmarkService`.
- **DTO** — Pydantic-модели на границах слоёв (`OverallScore`, `ThermalStabilityResult` особенно с `extra="forbid"` для контракта).
- **Pure functions** — ядро `scoring.py` без побочных эффектов: `harmonic_mean`, `geometric_mean`, `geomean_score`. Тестируется напрямую.

## Целевая совместимость ОС

Текущие тестовые эталоны: Windows 11 (RTX 4000-series, см. memory
`user_hardware.md`) и **Astra Linux SE 1.8.5.46** (бюллетень
**№ 2026-0224SE18**, **11.02.2026**, ядро **6.1.158-1-generic**, AMD
Ryzen 7 6800H + Radeon 680M iGPU, Phison NVMe — см.
`docs/Astra/test_environment.md`). Поведение на других билдах/железе
не проверено — при новой комбинации заводить отдельный раздел в
`test_environment.md`.

| Возможность | Windows 11 | Astra Linux SE 1.8.5.46 |
|---|:---:|:---:|
| psutil базовая телеметрия | ✅ | ✅ |
| Температуры | LibreHardwareMonitorLib (in-process) → psutil → WMI/CIM | `/sys/class/hwmon` (без root, LHM-совместимые ключи) |
| GPU температура | NVML → LHM → nvidia-smi | hwmon `amdgpu`/`radeon`/`i915`/`xe` (без root) |
| Диск температура | smartctl (Windows допускает SMART log без admin) | hwmon `nvme`/`drivetemp` (без root); smartctl SMART log требует `CAP_SYS_ADMIN`, доступен только из-под root |
| DRAM info для Roofline | PowerShell `Get-CimInstance Win32_PhysicalMemory` | `dmidecode -t 17` (требует root) или env-vars |
| SIMD/AES-NI/SHA-NI detect | по `cpu_model` (heuristic) | то же |
| Стресс-движки CPU | `prime95` (внешний) + builtins | `stress-ng` (apt) + builtins |
| Стресс-движки RAM | `prime95 -m` + builtins | `stress-ng --vm` + builtins |
| Упаковка | PyInstaller + Inno Setup | deb-пакет (debhelper-compat 13) |

## Тестирование

Структура `tests/`:
- **`unit/`** — чистые тесты (без сети, БД-фиксиктуры через `tmp_path`):
  - `test_models.py`, `test_roofline.py`, `test_references.py`, `test_weights.py`, `test_scoring.py`, `test_multi_run.py`, `test_scoring_service.py`, `test_thermal.py`, `test_normalization.py`, `test_microbench.py`, `test_diagnostics.py`, `test_statistics.py`, `test_trends.py`, `test_stress_registry.py`, `test_menu_settings.py`.
- **`integration/`** — с реальной SQLite через `tmp_path`:
  - `test_sqlite_repo.py`, `test_micro_run_repo.py`.

Команда: `pytest -q` из ``.

## Что не входит в v2.0 (отложено)

См. `HANDOVER.md` для актуального списка.

- Подключение scoring v2 в **меню screens** (сейчас только через CLI-флаг `--preset`).
- Миграция UI команд `runs list`/`compare`/`trend` под новые поля `OverallScore` (сейчас они работают на legacy schema).
- Экспортёры CSV/JSON под новые поля.
- Web UI обновление под scoring v2.
- `memory_lat` micro-тест (по запросу пользователя — на v2.1).
- Параллельный стресс-режим для теста стабильности (сейчас движки идут последовательно — issue #8 в репо).
- Reference CLI: `bench reference create/show/set-default` для финализации empirical YAML.
