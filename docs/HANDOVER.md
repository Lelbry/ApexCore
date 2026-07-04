# HANDOVER — что осталось сделать

Документ-«состояние» проекта для следующего сеанса работы (своего или AI-ассистента).
Цель: за 2 минуты понять «где мы», что сделано и что в работе.

> Дата последнего обновления: 2026-07-04
> Текущий релиз: **`v1.1.0`** = `dev` HEAD (кроссвендорный GPU-модуль + снос benchkit-шимов). `main` двигается тегами при milestone-релизах через fast-forward из `dev`.
> Автор коммитов: `Lelbry <Lelbry@users.noreply.github.com>`. AI не упоминать.
>
> **Примечание о staleness:** секции ниже про scoring v2 / комплексный бенчмарк / стабильность сенсоров — исторические (всё это давно в релизе). Актуальная сводка релизов и что «точно стоит знать» — в `CLAUDE.md`. Здесь оставлено как журнал.

## GPU-модуль (v1.1.0, готово)

Кроссвендорная оценка видеокарты — end-to-end, в релизе. Спека: [`gpu_benchmark.md`](gpu_benchmark.md), релиз-ноты: [`RELEASE_1.1.0.md`](RELEASE_1.1.0.md).

- **Roofline-бенчмарк** (шкала ×10 000): FP32/FP64 (GFLOPS), пропускная способность VRAM (STREAM-triad, GB/s), PCIe H2D/D2H (GB/s). Балл = `GM(r_fp32, r_mem) × 10 000`; FP64 и PCIe измеряются, но в балл не входят (см. `gpu_benchmark.md` §7). Ratio clamp ≤ 1.0.
- **GPU-стресс** (термостабильность): длительная FP32-нагрузка + посекундная телеметрия (темп/мощность/частота/загрузка) + вердикт PASS/WARN/FAIL/UNKNOWN.
- **Кроссвендорность через OpenCL** (ctypes к ICD-loader'у, `infrastructure/gpu/{opencl_backend,_ocl}.py`) — без единой новой Python-зависимости.
- **CLI** `apexcore gpu list/run/test/stress`, **пункт 5 меню** «Оценка производительности GPU» (`menu/gpu_screen.py`), **экран Web UI** (`static/js/screens/gpu.js`), **единая История** (GPU-прогоны в общей ленте).
- **БД**: схема v6 (additive), таблицы `gpu_benchmark_runs` (v5) + `gpu_stress_runs` (v6); репозитории `SqliteGpuBenchmarkRepository` / `SqliteGpuStressRepository`.
- **Платформа**: проверено на Windows (NVIDIA RTX 4070 Ti + Intel UHD 770). На Astra/AMD graceful degrade («GPU/OpenCL не обнаружен»); реальный GPU-compute на Radeon 680M **не проверен** и зависит от установленного OpenCL ICD — ждёт прогона на стенде.

Также в этом цикле: **сняты backward-compat шимы `benchkit → apexcore`** (были на 1 релиз — CLI-alias, ENV-трансляция, миграция data-dir, symlink, Conflicts/Replaces/Provides). Детали — секция «Rename» в `CLAUDE.md`.

## Общая оценка производительности системы (v4 schema, в релизе)

Ветка `claude/exciting-poitras-313d40`, worktree `exciting-poitras-313d40`, [PR #28](https://github.com/Lelbry/apexcore/pull/28).
План: [`C:\Users\alexp\.claude\plans\dev-polished-tiger.md`](file:///C:/Users/alexp/.claude/plans/dev-polished-tiger.md). Спека: [`docs/general_benchmark.md`](general_benchmark.md). Готово:

- **Новый раздел меню** (пункт 4 главного меню): `BenchmarkScreen` объединяет **Комплексный бенчмарк** и **Аналог Windows Winsat** (переехал с главного экрана).
- **Комплексный бенчмарк**: последовательный прогон DGEMM → STREAM → seq read → random read → seq write на boot-диске; формула `GM(r_dgemm, r_stream, r_disk) × 10 000` (без cooling, GPU не входит). Прогон ~95-100 с.
- **`infrastructure/disk_peak.py`**: фиксированная таблица peak по `media_type/bus_type` (NVMe 3500/600/2500 · SATA SSD 550/400/500 · HDD 200/5/180). Gen3/4/5 NVMe не различаем — clamp ≤1.0 не штрафует топ.
- **`infrastructure/disk_inventory.get_boot_drive`**: `%SystemDrive%` на Windows (case-insensitive), `/` на Linux.
- **`infrastructure/microbench/disk.DiskSequentialWriteBench`**: один проход 256 МБ + fsync (без циклирования, ~1 GB записи на прогон, износ SSD пренебрежим). Read-бенчи дополнены параметром `target_dir`.
- **Отчёт** (`render_general_benchmark_report`): цветная таблица `box.ROUNDED` с подсветкой % (≥70 зелёный / 40-70 жёлтый / <40 красный), Panel «Что показывает % от максимума», центрированная плашка «Ваш итоговый балл», Panel «Шкала баллов», dim-rule перед `Enter`.
- **БД**: миграция `v4` (additive), таблица `general_benchmark_runs`, `SqliteGeneralBenchmarkRepository` с `resolve_id`.
- **История прогонов**: новый kind `general` с подписью «Общая оценка производительности системы» (рядом со stress/micro/winsat).
- **WinsatScreen**: убран пункт «Список последних прогонов» (общая лента), `b — Назад` (не на главный — это подэкран `BenchmarkScreen`), эмодзи убраны (рендерились квадратиками на PowerShell).
- **HomeScreen**: новый порядок 1 Инфо · 2 Датчики · **3 Стресс** · **4 Общая оценка** · 5 CPU · 6 Ram & CPU Cache · 7 История ваших тестов · 8 Web UI · 9 Настройки.
- **Тесты**: +47 новых (`test_general_benchmark_*`, `test_disk_peak`, `test_disk_boot_drive`, `test_benchmark_screen`, write-кейсы в `test_disk_bench`). Общее: **1022 passed, 7 skipped** (0 регрессий).

## P0 стабильности сенсоров (v0.5.1, готов к ревью)

Полный список изменений — в плане `C:\Users\alexp\.claude\plans\benchkit-python-optimized-gizmo.md`. Кратко:

- **Probe-фаза** (`infrastructure/sensors/probe.py`): detection HVCI / Smart App Control / Vulnerable Driver Blocklist / Defender quarantine / AV vendor / SHM-источников / .NET runtime — один проход за процесс, кэш in-memory.
- **HWiNFO + CoreTemp SHM readers** (`infrastructure/sensors/shm/`): чтение Shared Memory чужих утилит как **первый приоритет** в fallback chain. Юридически чисто (OS-API уровня), без admin, совместимо с HVCI/SAC.
- **Reorder fallback chain** в `WindowsAdapter._read_sensors`: HWiNFO → CoreTemp → LHM → psutil → WMI perf-counter → WMI MSAcpi.
- **Фильтр 25-30 °C** для ACPI fake zones (битый OEM DSDT): помечает source как `approximate` вместо silicon.
- **Дифференциация `DegradedReason`** (`domain/sensor_models.py`): 11 значений — `HVCI_BLOCKED`, `SAC_BLOCKED`, `DEFENDER_BLOCKED`, `AV_BLOCKED`, `NO_ADMIN`, `NO_LHM_DLL`, `NO_DOTNET_RUNTIME`, `COM_INIT_FAILED`, `CPU_UNSUPPORTED`, `ACPI_FAKE_ZONE`, `ARM_PLATFORM`.
- **Self-repair**: `apexcore doctor --repair` — интерактивные действия с подтверждением.
- **UX degraded mode**: inline-reason в `render.py`, баннер в diagnostics, `ThermalWatchdog.no_data_reason` (отличает «нет данных» от «trigger не сработал»).
- **Bundling**: DLL коммитятся в git (~3 МБ), .NET 9 framework-dependent в Inno Setup installer (`scripts/fetch_dotnet9.ps1` + auto-detect в `lhm._configure_runtime`).
- **Тесты**: новые `test_sensors_probe.py`, `test_sensors_shm_hwinfo.py`, `test_sensors_shm_coretemp.py`, `test_acpi_fake_zone_filter.py` + регрессии в `test_thermal_watchdog.py` и `test_windows_adapter.py` (расширена fixture `_silence_real_sensors`).
- **Docs**: ARCHITECTURE.md секция «Температурные сенсоры» переписана с новой fallback-диаграммой, новый файл `docs/troubleshooting.md` с деревом решений по `DegradedReason`.

P1/P2 — backlog (см. план): AIDA64 SHM, WMI dedicated worker thread, AMD Ryzen Master DLL runtime-discovery, кэш «last known good», PawnIO Pawn-модули.

## P1 стабильности сенсоров (v0.5.2, готов к ревью)

Ветка `sensors-stability-p1`, worktree `stability-orca-bohr-9c4d`. Stacked-PR над `sensors-stability-p0`. План: `C:\Users\alexp\.claude\plans\benchkit-p1-plan.md`. Готово:

- **P1.1**: `application/diagnostics_sensors.build_capability_summary(snap)` — одна строка «HWiNFO SHM+NVML (silicon CPU/GPU, Vcore доступен)» в `apexcore info` после системной таблицы; degraded-сценарий классифицирует причину через probe.
- **P1.2**: `infrastructure/sensors/shm/aida64.py` — regex-парсер pseudo-XML `AIDA64_SensorValues`; добавлен в fallback chain как шаг 3 (после CoreTemp). Расширил `_CPU_PATTERNS` AIDA64-label'ами («CPU», «CPU Diode», «CPU IA Cores»).
- **P1.3**: `infrastructure/sensors/wmi_temps._WmiWorker` — singleton thread с `CoInitializeEx(COINIT_APARTMENTTHREADED)` + queue.Queue. Снимает корневую причину COM-ошибки `MSAcpi`. Safety-net `_WMI_PACKAGE_BROKEN` сохранён как инвариант.
- **P1.4**: `infrastructure/sensors/ryzen_master.py` — runtime-discovery `Platform.AMD.RyzenMaster.dll` (DLL не редистрибутируем). Шаг 4 fallback chain, только AMD CPU. **Нужна верификация на AMD desktop** (Intel машина у разработчика).
- **P1.5**: `infrastructure/sensors/shm/_common.normalize_voltage_key` — отдельный набор regex'ов для voltage labels («CPU Core Voltage» → `cpu/vcore`, «CPU SoC Voltage» → `cpu/soc`, DRAM, GPU Core, +12V/+5V/+3.3V). HWiNFO + AIDA64 теперь корректно публикуют Vcore.
- **Тесты**: +64 новых (14 capability_summary + 14 aida64 + 15 wmi_worker + 19 ryzen_master + 2 hwinfo voltage). Общее ~896 passed + 4 skipped.
- **Docs**: ARCHITECTURE.md секция «Температурные сенсоры» обновлена с новой 8-шаговой диаграммой и подсекцией «Capability matrix».

## Версии и теги (snapshot)

| Тег | Коммит | Что | Можно удалить? |
|---|---|---|---|
| `rollback-pre-v0.1.0` | `6a43683` | Точка отката: main до scoring v2 | ❌ нет (бессрочный safety-net) |
| `v0.0.1` | `b2e81a0` | Первый прототип (Блок 1) | ❌ нет |
| `v0.1.0` | `d9486f5` | Scoring v2 + TUI menu | ❌ нет |
| `v0.2.0-rc1` | `fdd2bd3` | Issue #17 + Ram&Cache, pre-release | ✅ можно после v0.5.0 утрясётся |
| `v0.3.0-winsat` | (см. tag) | Аналог Windows Winsat | ❌ нет |
| **`v0.5.0`** | (актуальный) | = `origin/main` = `origin/dev`. Save-point перед стабилизацией сенсоров | ❌ нет |

После релиза v0.5.0 `main` и `dev` равны. Следующий milestone — стабильность сенсорного слоя (см. промпт ресерча `docs/research/sensor_reliability_research*` если есть, иначе обсудить).

**Удалённые ветки** (post-cleanup): `Save` (тегом `v0.0.1`), `feature/issue-17-windows-temps` (смержена в dev). Активные ветки только `main` + `dev`.

**Откат**: `git reset --hard rollback-pre-v0.1.0 && git push --force-with-lease origin main` — возвращает main в pre-scoring-v2 состояние.

## Состояние v2.0 (готово)

✅ **Scoring v2 backbone** — 13 этапов плана `synthetic-skipping-squirrel.md`.
Готово: спецификация, доменные модели, Roofline-калькулятор, references,
weights, ядро `geomean_score()`, multi-run + CI, persistence, чистка v1,
`ScoringService`, `thermal.py` + `StabilityService`, render-функции,
флаг `--preset` для CLI, обновлённая документация.

✅ **Расширенный тест ОЗУ и кеша (Ram&Cache)** — диагностический тест:
16 измерений Read/Write/Copy/Latency × DRAM/L1/L2/L3, русская сноска под
таблицей результатов. Не входит в общий балл, не сохраняется в SQLite
(только показ; для скриптов — CLI-флаг `--export PATH.json`).
Спека: [`docs/ram_cache.md`](ram_cache.md). CLI: `apexcore ram-cache list`
/ `run [--tests …] [--export …]`, пункт 4 в HomeScreen меню (внутри
3 подпункта в стиле «Расширенное тестирование процессора»: список,
все тесты, выбранные тесты).

✅ **1296+ unit + integration тестов проходят** (+skipped — Linux/macOS-only). ruff чисто на всём новом коде.

✅ **Температуры на Windows без LibreHardwareMonitor-процесса** (issue #17).
LHM-библиотека встроена внутрипроцессно через pythonnet, DLL'и и .NET 8
бандлятся в инсталлер. Юзеру **больше не нужно** запускать LibreHardwareMonitor
руками или включать его HTTP-сервер. Подробности — `ARCHITECTURE.md` →
«Температурные сенсоры (Windows)».

✅ **CLI работает:**
```powershell
apexcore micro run --preset standard       # ~15 мин, balanced score
apexcore micro run --preset accurate -d 5  # ~25 мин с 95% CI
apexcore micro run --preset fast --tests memory_read,flops_sp  # частное тестирование
```

✅ **Smoke-test на i9-12900K + DDR5-6400:** balanced score 286 (29% от Roofline-пика),
сохранён в БД (новая таблица `micro_runs`).

✅ **Положение среди популярных CPU** — секция под сводкой в карточке
«Тест Single-Core / Multi-Core». Сравнение `SystemInfo.cpu_model` с
выборкой из ~41 десктопного CPU (Intel 9-14gen, AMD Ryzen 3000-7000) в
`data/cpu_ranking.yaml`; рендер показывает топ N% по Single и Multi,
сноску об ограниченности базы. Источники, схема и инструкция по
пополнению — `data/README_cpu_ranking.md`. Код: `application/cpu_ranking.py`,
рендер: `interfaces/cli/render.py::_ranking_grid`.

## Что ещё не сделано (по плану)

### Этап 13 (P2, отложен по запросу пользователя): RAM revisited

Пользователь явно сказал «позже мы переработаем оценку RAM».

- Добавить `memory_lat` micro-тест (pointer-chasing на 64MB, аналог `lat_mem_rd` LMbench).
- Расширить `R_MEM` до иерархии: `R_MEM = GM(r_memory_bw, r_memory_lat)`.
- Roofline для `memory_lat`: теоретический минимум по DDR timings (DDR4-3200 CL16 ≈ 10 нс).
- **Триггер:** создать GitHub issue с метками `enhancement` + `pending-revision` перед началом v2.1.

### Этап 14 (P3, опционально): Community сравнение

Roofline-баллы естественно сравнимы между ПК (% от собственного пика). Возможные расширения:

- `bench export-result <id>` — JSON в стандартном формате для обмена.
- `bench compare-with <file.json>` — side-by-side с чужим результатом.
- В будущем: GitHub-based leaderboard (PR-модель, без backend).

### Подключение scoring v2 в меню (СДЕЛАНО)

Меню давно на scoring v2. `CpuTestsScreen` гоняет Single/Multi через `ScoringService`
(+ рейтинг CPU), полный прогон и точечные микробенчи; общая оценка — `BenchmarkScreen`
(пункт 4). Экран стабильности/термотеста реализован для CPU-стресса и (в v1.1.0) для GPU.

### UI-команды под scoring v2 (СДЕЛАНО / устарело)

- `interfaces/cli/commands/runs.py` — теперь **единая лента** прогонов (`stress` / `micro` /
  `winsat` / `general` / GPU), берёт данные из соответствующих таблиц, а не legacy `final_score=0`.
- `trend.py` / `compare.py` / `diagnose.py` — **удалены** (мёртвый scoring v1, коммит `c300a1a`).
- Web UI — `/api/history` объединяет типы прогонов; отдельные экраны на реальном backend.
- (Опционально, не блокер) `csv_exporter.py` — расширенные колонки scoring v2 при желании.

### Reference CLI (не сделано, опционально)

Команды для финализации empirical reference:

- `bench reference create --from-runs <UUID...>` — собирает empirical YAML из набора прогонов.
- `bench reference show` — вывод текущего активного reference.
- `bench reference set-default <PATH>` — копирует YAML в `~/.config/apexcore/reference.yaml`.

## Открытые GitHub issues

> **Актуально (2026-07-04):** открытым остаётся по сути один — **#22**: live-дашборд
> GPU-стресса (enhancement поверх уже готового модуля — обогатить экран стресса живой
> телеметрией/графиком в реальном времени). Остальные из snapshot ниже закрыты или
> вошли в релизы v0.6.0–v1.1.0. Полный актуальный список — `gh issue list --state open`.

<details><summary>Исторический snapshot (2026-05-09, 12 штук) — оставлен как журнал</summary>

Полный список на тот момент: `gh issue list --state open`.

### Метки и работа с ними

- **`research-pending`** (#3, #6, #7, #8, #9): ожидают результата исследования. **В разработку не двигать** до пересмотра.
- **`pending-revision`** (#10, #11, #12): возможно радикальная переработка или удаление модуля. **Тоже не двигать**.
- **`postponed`** (#13): отложено до завершения основного TUI-функционала.
- **Обычные `bug`/`enhancement`/`ux`**: можно брать в работу.

### Готовые к работе issues (быстрые wins)

- **#5** — столбец «Бэкенд» в таблице micro заменить на исполняемый файл. Файл: `cli/render.py`.
- **#14** — Настройки: добавить «Открыть папку настроек». Файл: `menu/screens.py:SettingsScreen`.

### Отложенные / ждут ресерч/решения

- **#3** (`research-pending`) — Мониторинг в реальном времени: переработка UX.
- **#6, #7, #9** (`research-pending`) — Стресс-тесты: имена движков, телеметрия, перегруженный итог.
- **#8** (`research-pending`, `bug`) — Стресс-тесты: проверка реальной нагрузки на CPU/RAM. **Ждёт ручной верификации после `67b40bb`**: открыть Диспетчер задач при стресс-движках, посмотреть полку CPU.
- **#10, #11, #12** (`pending-revision`) — История прогонов: модули под пересмотр.
- **#13** (`postponed`) — Web UI на Windows: вернуться после завершения основного TUI.
- **#15** — Настройки: «P-cores» / «P+E-cores» режим (связано с архитектурой адаптеров).

### Закрытые

- **#17** — отказ от LibreHardwareMonitor как внешнего процесса. Вместо HTTP-сервера
  на :8085 — внутрипроцессный pythonnet + LibreHardwareMonitorLib + WMI fallback.
  См. `ARCHITECTURE.md` → «Температурные сенсоры (Windows)» и
  `infrastructure/sensors/{lhm,wmi_temps}.py`. Поставка DLL/`.NET 8`/WinRing0 —
  через `scripts/fetch_lhm.ps1` + `installer.iss`. В v0.9.0+ production-сборка
  включает `apexcore_sensord` Windows-сервис который держит LHM open + публикует
  сенсоры в Global SHM mapping → CPU-температура доступна **без admin** при
  каждом запуске. Для dev-отладки без installer'а — опциональный
  `new-app\scripts\dev.ps1` (self-elevating wrapper).
- **#1** — статус датчиков встроен в таблицу `render_system_info` (`render.py:111-117`).
- **#2** — `render.py:101-103` показывает «базовая X ГГц · турбо до Y ГГц» (без ложного нижнего края).
- **#4** — экран выбора тестов CPU/RAM теперь активен после прогона: накопительная таблица результатов, ввод диапазонов `1-3`, перезапуск дубликатов. Реализация: `SelectMicroTestsScreen`/`SelectRamCacheTestsScreen` + хук `Screen.handle_unknown_input` в `nav.py`. Коммиты `c621004`, `33bfb6a`, `60bd050`, `d91f6be` в `dev`.
- **#16** — RU-эквиваленты `и/р/й` для `b/h/q/?` зафиксированы в `nav.py:BACK_KEYS/HOME_KEYS/QUIT_KEYS` и в `CLAUDE.md`.

</details>

## Тесты — текущее состояние

```powershell
cd E:\Benchmark\new-app
.\..\.venv\Scripts\Activate.ps1
pytest -q       # 1296 collected (Linux/macOS-only тесты skipped на Windows)
ruff check .    # чисто на новом коде; pre-existing: sparkline.py SIM108,
                #   webui/server.py RUF100, test_winsat_scoring.py I001,
                #   test_disk_bench.py SIM105.
```

## Где живёт исследование

- `E:\Benchmark\docs\research\aggregated_overall_performance_assessment.md` — агрегированный отчёт по теме «общая оценка», основа для scoring v2.
- `E:\Benchmark\docs\research\aggregated_stress_testing.md` — агрегированный отчёт по теме «стресс-тестирование», основа для будущей переработки stress.
- `E:\Benchmark\docs\research\claude_research_*.md`, `gemini_research_*.md`, `deep-research-report_*.md` — исходные отчёты от трёх независимых AI-исследователей.

## Совет следующему агенту

1. **Прочитай только**: `CLAUDE.md` (если есть в worktree) → этот `HANDOVER.md` → `docs/CODEMAP.md` → `ARCHITECTURE.md`. Не читай весь репозиторий «для контекста».
2. **На вопрос «как реализован X»**: посмотри в CODEMAP.md → найди файл → читай только его.
3. **Перед изменением** конкретного файла прочитай его до того, как редактировать.
4. **Не пиши длинных коммит-сообщений** (10–15 строк достаточно). Длинные многострочные heredocs съедают контекст.
5. **Кириллица в комментариях** — нормально, в командных строках (commit messages) **используй короткие** русские фразы или английский.
