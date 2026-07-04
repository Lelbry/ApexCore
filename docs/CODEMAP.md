# Карта кода apexcore

Документ для быстрой навигации (особенно для AI-ассистентов в новом чате).
Каждая строка = один файл + 1 предложение «зачем он».

## domain/ — модели и контракты

- `models.py` — Pydantic v2 модели. Ключевые: `SystemInfo`, `MetricSnapshot`, `BenchmarkResult`, `MicroBenchSuiteResult` (содержит `OverallScore`), `OverallScore` (scoring v2), `ThermalStabilityResult`.
- `cache.py` — модели расширенного теста ОЗУ и кеша: `CacheLevel`, `CacheTopology`, `RamCacheMetric`, `RamCacheReport`.
- `winsat.py` — модели Winsat-аналога: `WinsatReport`, `WinsatSubscore`, `WinsatStatus`.
- `general_benchmark.py` — `GeneralBenchmarkReport` (полный отчёт комплексного бенчмарка: measured / peaks / ratio / score / boot-drive метаданные / notes).
- `gpu.py` — доменные модели GPU-бенчмарка: `GpuWorkloadKind`, `GpuDeviceType`, `GpuDeviceInfo`, `GpuMeasurement`, `GpuPeak`, `GpuBenchmarkReport` (Roofline, шкала ×10 000), `GpuStressVerdict`, `GpuStressSample`, `GpuStressReport` (термостабильность). Отдельно от `models.py` — GPU-специфичная шкала/ratio.
- `sensor_models.py` — `DegradedReason`, `ProbeResult`, `SensorSnapshot`, `BackendStatus`, `SensorDiagnostics` (P0 стабильности сенсоров).
- `ports.py` — абстрактные интерфейсы: `OSAdapter`, `StressEngine`, **`GpuComputeBackend`** (кроссвендорный GPU-compute, реализация — OpenCL), `ResultRepository`, `BaselineRepository`, `MicroRunRepository`, `WinsatRepository`, **`GeneralBenchmarkRepository`** (v4), **`GpuBenchmarkRepository`** (v5), **`GpuStressRepository`** (v6), `MetricsBus`.
- `errors.py` — типизированные исключения.
- `__init__.py` — реэкспорт всех публичных моделей.

## application/ — бизнес-логика

### Scoring v2 (общая оценка производительности)

- `roofline.py` — теоретические пики архитектуры (FLOPS/IOPS/memory bandwidth/AES/SHA) по `SystemInfo`. Поддержка env-override `APEXCORE_CPU_GHZ`, `APEXCORE_DRAM_MTS`, `APEXCORE_SIMD`.
- `references.py` — `ReferenceSet` = Roofline + empirical fallback. `build_reference(system_info)`.
- `weights.py` — `WeightsProfile` из YAML. `load_weights(name)`, `normalize_weights()`.
- `scoring.py` — **ядро**: `harmonic_mean`, `geometric_mean`, `weighted_geometric_mean`, `compute_workload_ratios`, `geomean_score(suite, ref, weights) → OverallScore`. `SCORING_VERSION = "2.0.0"`.
- `multi_run.py` — пресеты `fast`/`standard`/`accurate`, `aggregate_multi_run()`, `compute_ci_logscale()`, `trimmed_mean()`. `PRESET_RUNS = {"fast":1, "standard":3, "accurate":5}`.
- `scoring_service.py` — `ScoringService.run_overall(preset, ...)` — оркестратор общей оценки.
- `thermal.py` — `compute_thermal_stability(metrics_history) → ThermalStabilityResult`. `PASS_THRESHOLD_PERCENT = 97.0`.
- `stability_service.py` — `StabilityService.run_stability(duration_sec=600)` — 10-минутный stress + thermal.
- `ram_cache_service.py` — `RamCacheService.run(duration_sec_per_metric)` — оркестратор Ram&Cache (16 измерений). Не сохраняет в SQLite.

### Winsat-аналог (шкала 1.0–9.9)

- `winsat_scoring.py` — формула `score_from_metric(value, points)` (log2-интерполяция), `compute_cpu_score`, `compute_memory_score`, `compute_disk_score`, `compute_winspr_level`. Загружает калибровку из `data/winsat_thresholds.yaml`.
- `winsat_service.py` — `WinsatService.run_formal(duration_sec_per_test)` — оркестратор 5 подтестов (AES + SHA + memory_read + disk_seq + disk_random). Только Windows.

### Стресс-балл и комплексный бенчмарк (шкала ×10 000)

- `stress_score.py` — pure: `compute_stress_score(r_dgemm, r_stream, r_stability)` + `compute_stress_score_context(...)`. `STRESS_SCORE_SCALE = 10_000`. Спека [`docs/stress_score.md`](stress_score.md).
- `stress_orchestrator.py` — оркестратор стресс-прогона с SafetyGate / ThermalWatchdog / ParallelStressRunner / телеметрией. Собирает `StressFinalReport`.
- `general_benchmark_score.py` — pure: `compute_general_benchmark_score(r_dgemm, r_stream, r_disk)`, `_disk_ratio_from_components(seq_read, random_read, seq_write)`, `_clamp_ratio`. Clamp ≤ 1.0 на каждый ratio. `GENERAL_BENCHMARK_SCALE = 10_000`.
- `general_benchmark.py` — `GeneralBenchmarkOrchestrator(adapter).run(params, cancel_token, on_progress)`. Последовательный прогон 5 фаз (DGEMM → STREAM → seq read → random read → seq write) с cooldown 5 с между CPU-фазами. Без watchdog. Спека [`docs/general_benchmark.md`](general_benchmark.md).

### GPU-бенчмарк и GPU-стресс (кроссвендорный OpenCL, шкала ×10 000)

- `gpu_roofline.py` — `compute_gpu_peak(device, system_info) → GpuPeak` по `data/gpu_arch.yaml`: FP32-пик = `CU × MHz × flops_per_cu_per_clock / 1000`; FP64 = FP32 × ratio (None если ratio 0 / нет HW-FP64); mem-bandwidth из per-model таблицы. Env-override `APEXCORE_GPU_{FP32,FP64,MEM}_PEAK_*`, `APEXCORE_GPU_ARCH`.
- `gpu_benchmark_score.py` — pure: `compute_gpu_benchmark_score(r_fp32, r_mem) → GPU_BENCHMARK_SCALE × GM(...)` либо None. `GpuBenchmarkScoreContext`, `_clamp_ratio` (≤1.0). FP64/PCIe в формулу не входят. `GPU_BENCHMARK_SCALE = 10_000`.
- `gpu_benchmark.py` — `GpuBenchmarkOrchestrator(adapter, backend).run(device_index, params, cancel_token, on_progress)`. Фазы FP32 → FP64 → VRAM → PCIe H2D/D2H с cooldown; собирает `GpuBenchmarkReport`. Graceful degrade без бэкенда/устройств (`score=None`, notes). Pure от persistence.
- `gpu_stress.py` — `GpuStressOrchestrator(adapter, backend).run(device_index, duration_sec, ...)`: длительная FP32-нагрузка (`SUSTAINED_STRESS`) в рабочем потоке + посекундная телеметрия через инъектируемый `TelemetrySampler` → `GpuStressReport` c вердиктом PASS/WARN/FAIL/UNKNOWN (пороги `THROTTLE_*`).

### Legacy / диагностика

- `benchmark_service.py` — `BenchmarkService.run(config)` для стресс-прогона. `final_score=0` всегда (legacy-поле).
- `telemetry_service.py` — фоновый сэмплер; `InMemoryMetricsBus`, `TelemetryService`.
- `normalization.py` — `normalize_run` / `baseline_from_run(s)` для `compare` (regression detection).
- `statistics.py` — Welch t / Mann-Whitney / Shapiro-Wilk / Cohen's d.
- `diagnostics.py` — правила анализа `metrics_history` (термотротлинг, частотная вариативность).
- `trends.py` — rolling mean / p95.

## infrastructure/ — реализации портов

### adapters/ — OS-специфика

- `base.py` — `PsutilBaseAdapter` (общая psutil-логика).
- `windows.py` — `WindowsAdapter`. Температуры — гибридный pipeline через `infrastructure/sensors/`: LHM → psutil → perf-counter Thermal Zone → MSAcpi. L2/L3 cache — WMI `Win32_Processor`.
- `linux.py` — `LinuxAdapter` (psutil + `/sys/class/hwmon` + `/sys/.../cache` для L1/L2/L3).
- `cache.py` — `default_cache_topology()`, `detect_topology_from_sysfs()`, `topology_from_wmi_kb()`, `parse_size_string()`. Дефолты: L1=32 КБ, L2=256 КБ, L3=8 МБ, DRAM=256 МБ.
- `factory.py` — `AdapterFactory.detect()` по `platform.system()`.

### Корневые модули infrastructure/

- `disk_inventory.py` — `list_physical_disks()` (через `Get-PhysicalDisk` / `lsblk -J`), `PhysicalDisk` dataclass, **`get_boot_drive_path()`** + **`get_boot_drive(disks=None)`** (Windows: `%SystemDrive%`, Linux: `/`).
- `disk_peak.py` — `DiskPeakProfile`, `lookup_disk_peak(media_type, bus_type)`. Фиксированная таблица типовых пиков для NVMe / SATA SSD / HDD / unknown (для `r_disk` ratio в комплексном бенчмарке).

### sensors/ — источники датчиков (Windows)

- `lhm.py` — `read_lhm_temperatures()`, ленивый singleton поверх **LibreHardwareMonitorLib** (pythonnet). Решает issue #17 — заменяет внешний процесс LHM. `_configure_runtime()` подхватывает bundled .NET 8 через `APEXCORE_DOTNET_ROOT`. Graceful degrade при отсутствии DLL/.NET/WinRing0.
- `wmi_temps.py` — `read_perf_counter_thermal_zone()` и `read_msacpi_thermal_zone()` через PowerShell + Get-Counter / Get-CimInstance. Используются как fallback'и.
- `lib/` — bundled DLL'и LHM (MPL-2.0), HidSharp, драйвер WinRing0. Сами файлы не коммитятся; скачиваются `scripts/fetch_lhm.ps1` во время сборки.

### gpu/ — GPU-compute бэкенд (OpenCL)

- `opencl_backend.py` — `OpenClGpuBackend` (реализация `domain.ports.GpuComputeBackend`): перечисление устройств + замеры FP32/FP64/STREAM-triad/PCIe кернелами, таймленными device-side событиями. `build_default_gpu_backend()` в `__init__.py` — фабрика.
- `_ocl.py` — тонкая ctypes-обёртка над системным ICD-loader (`OpenCL.dll` / `libOpenCL.so.1`), только нужные вызовы OpenCL 1.2. `OpenClError`. Без `pyopencl` и любых новых зависимостей.

### stress/ — стресс-движки

- `base.py` — `run_threaded_loop(work_fn, duration, threads, cancel_token)`.
- `builtin_cpu.py` — `BuiltinCpuIntEngine` (LCG), `BuiltinCpuFpEngine` (matmul).
- `builtin_ram.py` — `BuiltinRamBandwidthEngine` (STREAM Triad), `BuiltinRamLatencyEngine` (pointer-chasing).
- `external_prime95.py` — обёртка над `prime95 -t` через `Popen` + cancel_token.
- `external_stress_ng.py` — обёртки `stress-ng --cpu` / `--vm` / `--cpu-method matrixprod` через `Popen`.
- `registry.py` — `StressRegistry`, `build_default_registry()`, `profile_engines(profile)`, `PROFILES`.

### microbench/ — 12 тестов общей оценки

- `base.py` — `MicroBench` Protocol, `time_loop(work, duration, cancel_token)`, `CancelledError`.
- `memory.py` — `MemoryReadBench`, `MemoryWriteBench`, `MemoryCopyBench` (256MB float64, np.sum/fill/copyto).
- `flops.py` — `FlopsSpBench`, `FlopsDpBench` (numpy.matmul 1024×1024).
- `integer.py` — `Int24/32/64IopsBench` (LCG-цепочки на numba/numpy).
- `crypto.py` — `Aes256Bench` (AES-256-CBC через `cryptography`), `Sha1Bench` (`hashlib`).
- `fractal.py` — `JuliaSpBench`, `MandelbrotDpBench` (numba JIT-кернелы 512×512).
- `ram_cache.py` — `RamCacheBench(level, operation, buffer_bytes)` для Ram&Cache теста (numba+numpy fallback, поддержка `APEXCORE_DISABLE_NUMBA`).
- `disk.py` — `DiskSequentialReadBench` (64K блоки), `DiskRandomReadBench` (16K + seek), **`DiskSequentialWriteBench`** (64K, один проход 256 МБ + fsync; не циклится). Все принимают опциональный `target_dir: Path | None` для прогона на конкретном диске. `_make_test_file(size_mb, target_dir)` — фабрика временного файла с urandom-содержимым.
- `registry.py` — `build_default_microbench_registry()` — 12 тестов в каноническом порядке.

### persistence/

- `schema.sql` — SQLite-схема v6: `runs`, `baselines`, `micro_runs`, `winsat_runs`, **`general_benchmark_runs`** (v4), **`gpu_benchmark_runs`** (v5), **`gpu_stress_runs`** (v6), `schema_version`.
- `migrations.py` — `apply_schema(conn)`, `CURRENT_VERSION = 6`. v1 → v2: drop `runs` + `baselines`. v2 → v3 / v3 → v4 / v4 → v5 / v5 → v6: additive (`CREATE IF NOT EXISTS`).
- `sqlite_repo.py` — `SqliteResultRepository`, `SqliteBaselineRepository`, **`SqliteMicroRunRepository`** (v2).
- `winsat_repo.py` — **`SqliteWinsatRepository`** (v3) — хранилище Winsat-отчётов.
- `general_benchmark_repo.py` — **`SqliteGeneralBenchmarkRepository`** (v4) — хранилище `GeneralBenchmarkReport` с `resolve_id(prefix)`.
- `gpu_benchmark_repo.py` — **`SqliteGpuBenchmarkRepository`** (v5) — хранилище `GpuBenchmarkReport` (JSON + индексы `score`/`device_name`).
- `gpu_stress_repo.py` — **`SqliteGpuStressRepository`** (v6) — хранилище `GpuStressReport` (headline = вердикт, без балла).

### exporters/

- `json_exporter.py` — экспорт `BenchmarkResult` в JSON.
- `csv_exporter.py` — экспорт в CSV.

## interfaces/ — UI

### cli/ — Typer-приложение

- `main.py` — корневое Typer-приложение, регистрация подкоманд + интерактивное меню.
- `render.py` — rich-таблицы. **`render_overall_score(overall, preset)`**, **`render_thermal_stability(thermal)`**, `render_microbench_suite`, `render_metric_summary`, **`render_ram_cache_report(report)`** — таблица 4×4 + сноска ¹²³⁴.
- `messages.py` — локализованные строки для CLI (`RAMCACHE_FOOTNOTES` — описания Read/Write/Copy/Latency на русском).
- `commands/info.py`, `monitor.py`, `bench.py`, `runs.py`, `export.py`, `webui.py`, `doctor.py` — команды. (Старые `compare.py`/`diagnose.py`/`trend.py` удалены коммитом `c300a1a` — мёртвый scoring v1.)
- **`commands/gpu.py`** — `gpu list` (устройства) / `gpu run` (полный бенчмарк → балл + сохранение) / `gpu test -w fp32|fp64|mem|pcie` (одиночный замер без скоринга) / `gpu stress --duration` (термотест → вердикт). Graceful degrade без OpenCL/GPU.
- `commands/runs.py` — объединённая лента 4 типов прогонов: `stress` / `micro` / `winsat` / **`general`** (комплексный бенчмарк, подпись «Общая оценка производительности системы»). Хелперы `collect_unified_listing`, `render_unified_listing`, `show_run_by_ref`, `export_run_by_ref`, `_resolve_to_ref`, `delete_run`.
- `commands/stress.py` — `stress list/run`.
- **`commands/micro.py`** — `micro list`, `micro run [--preset fast|standard|accurate]`. Через `--preset` → `_run_with_scoring()` → `ScoringService`.
- **`commands/ram_cache.py`** — `ram-cache run [--duration] [--export FILE.json]`. Диагностический тест Read/Write/Copy/Latency для DRAM/L1/L2/L3.
- **`commands/winsat.py`** — `winsat run/formal/query/list` (только Windows). Шкала 1.0–9.9, формат Win32_Winsat.

### cli/menu/ — интерактивное меню

- `app.py` — `run_menu()` точка входа.
- `nav.py` — `Screen`, `MenuLoop`, `NavResult`, глобальные команды (b/h/q/?).
- `screens.py` — конкретные экраны: `HomeScreen` (порядок: 1 Инфо · 2 Датчики · 3 Стресс · 4 **Общая оценка** · 5 **GPU** · 6 CPU · 7 Ram & CPU Cache · 8 История ваших тестов · 9 Web UI · 10 Настройки), `CpuTestsScreen`, **`RamCacheScreen`**, `StressScreen`, `HistoryScreen`, `SettingsScreen`, `DurationsScreen`.
- `winsat_screen.py` — **`WinsatScreen`** (только Windows): запуск winsat formal, query last, about (без list runs — лента живёт в общем `HistoryScreen`). `b — Назад` (не на главный — это подэкран `BenchmarkScreen`).
- `benchmark_screen.py` — **`BenchmarkScreen`** — подменю «Общая оценка производительности системы»: пункт 1 «Комплексный бенчмарк (CPU + RAM + Boot-диск)» (~95-100 с прогон, балл «попугаев» ×10 000), пункт 2 «Аналог Windows Winsat» (только Windows). История доступна через общий `HistoryScreen`. Тихо сохраняет в `SqliteGeneralBenchmarkRepository` до рендера (нет вывода UUID).
- `gpu_screen.py` — **`GpuScreen`** — подменю «Оценка производительности GPU» (пункт 5 главного экрана): список устройств / полный бенчмарк (балл до 10 000) / одиночный замер / GPU-стресс (вердикт). Длительности из `menu_settings.yaml` (`gpu_compute`, `gpu_pcie`). Тихо сохраняет в GPU-репозитории; история — в общем `HistoryScreen`.
- `cancel.py` — `cancellable()` контекст-менеджер: SIGINT → threading.Event.
- `runners.py` — `run_microbench_suite(tests, duration, threads, sys_info, cancel)` с rich.Progress.
- `settings_store.py` — YAML-настройки длительности тестов в data_dir (включая `ram_cache`).

### webui/ (опционально, FastAPI)

- `server.py` — FastAPI на `127.0.0.1`, websocket-стрим.
- `static/index.html` — Chart.js dashboard.
- `static/js/screens/gpu.js` — экран «Тест GPU»: два режима (Roofline-бенчмарк + стресс), device-picker, прогресс, карточка результата (балл X / 10000 либо бейдж вердикта + спарклайн). Без GPU (`available:false`) — «OpenCL/GPU не обнаружен».

## shared/

- `config.py` — `ApexcoreSettings` (pydantic-settings), пути в `~/.local/share/apexcore/` (Linux) / `%APPDATA%\apexcore\` (Win).
- `logging_setup.py`, `units.py`, `timing.py`.

## data/ — пакетные YAML

- `empirical_reference.yaml` — empirical fallback для тестов без Roofline (`julia_sp`, `mandelbrot_dp`, AES/SHA без NI). Provisional, требуют замены после набора 10+ прогонов.
- `weights/default.yaml` — equal subsystem weights.
- `winsat_thresholds.yaml` — калибровочные пороги для Winsat-аналога (CPU/Memory/disk_seq/disk_random → score 1.0–9.9 через log2-интерполяцию).
- `gpu_arch.yaml` — таблица GPU-архитектур для `gpu_roofline.py`: `fp32_flops_per_cu_per_clock` / `fp64_ratio` по вендорам (NVIDIA SM / AMD CU / Intel EU) + per-model пропускная способность VRAM.

## docs/

- `scoring_v2.md` — формальная спецификация scoring v2 (детальная общая оценка, шкала ×1000).
- `stress_score.md` — спецификация стресс-балла (шкала ×10 000, c cooling-фактором).
- `general_benchmark.md` — спецификация комплексного бенчмарка «Общая оценка производительности системы» (шкала ×10 000, без cooling, CPU+RAM+диск).
- `gpu_benchmark.md` — спецификация GPU-оценки: Roofline-балл `GM(r_fp32, r_mem)` × 10 000 (FP64/PCIe вне балла) + GPU-стресс с вердиктом PASS/WARN/FAIL/UNKNOWN, кроссвендорный OpenCL.
- `winsat.md` — спецификация Winsat-аналога (шкала 1.0–9.9).
- `ram_cache.md` — спецификация расширенного теста ОЗУ и кеша (Ram&Cache).
- `troubleshooting.md` — дерево решений по `DegradedReason` сенсорного слоя.
- `CODEMAP.md` — этот файл.
- `HANDOVER.md` — что осталось сделать (живой backlog).

## tests/

### unit/ — чистые тесты (~1296 шт total, включая integration)

Scoring v2: `test_models.py`, `test_roofline.py`, `test_references.py`, `test_weights.py`, `test_scoring.py`, `test_multi_run.py`, `test_cache_topology.py`, `test_ram_cache.py`, `test_scoring_service.py`, `test_thermal.py`, `test_microbench.py`, `test_normalization.py`, `test_diagnostics.py`, `test_statistics.py`, `test_trends.py`, `test_stress_registry.py`, `test_menu_settings.py`.

Сенсоры: `test_sensors_wmi.py`, `test_sensors_lhm.py`, `test_sensors_probe.py`, `test_sensors_shm_hwinfo.py`, `test_sensors_shm_coretemp.py`, `test_sensors_shm_aida64.py`, `test_acpi_fake_zone_filter.py`, `test_wmi_worker.py`, `test_ryzen_master.py`, `test_capability_summary.py`, `test_classify_reasons.py`, `test_doctor_repair.py`, `test_shm_layout.py`, `test_shm_adapter.py`, `test_windows_adapter.py`.

Стресс и render: `test_stress_score.py`, `test_stress_menu.py`, `test_render_stress_final_report.py`, `test_thermal_watchdog.py`, `test_sparkline.py`.

**Общая оценка производительности системы (комплексный бенчмарк, v4):**
- `test_general_benchmark_score.py` — pure: GM, clamp ≥1.0, None-семантика, `_disk_ratio_from_components`.
- `test_general_benchmark_orchestrator.py` — orchestrator с моками stress-движков и disk-бенчей; happy-path + skip-disk + unknown-disk.
- `test_general_benchmark_repo.py` — CRUD + миграция + `resolve_id` (exact + prefix).
- `test_disk_peak.py` — таблица peak по media_type, fallback для unknown.
- `test_disk_boot_drive.py` — `get_boot_drive_path/get_boot_drive` (Windows: `%SystemDrive%` monkeypatch; Linux: `/` skipped on Windows).
- `test_disk_bench.py` — `DiskSequentialReadBench/RandomReadBench/SequentialWriteBench`; `target_dir`, write единственным проходом, cleanup, метаданные.
- `test_benchmark_screen.py` — структура `BenchmarkScreen` (1 комплексный + 2 Winsat на Windows), `HomeScreen` (новый порядок пунктов).

### integration/ — с реальной SQLite

- `test_sqlite_repo.py` — Result/Baseline репозитории.
- `test_micro_run_repo.py` — `SqliteMicroRunRepository` save/get/list/delete/resolve_id.
- `test_winsat_repo.py` — `SqliteWinsatRepository`.
- `test_winsat_service.py` — `WinsatService` end-to-end (skipped на Linux).

## scripts/

- `validation/` — скрипты искусственной деградации системы для проверки точности диагностики.
- `build_windows.ps1` — PyInstaller + Inno Setup.
- `build_astra.sh` — deb-пакет для Astra Linux.

## packaging/

- `windows/` — Inno Setup `.iss` + PyInstaller `.spec`.
- `astra/debian/` — control / postinst / rules для deb.
