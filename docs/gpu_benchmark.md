# Оценка GPU — детерминированный Roofline-балл (кроссвендорный OpenCL)

> **Версия документа:** 1.0.0
> **Статус:** реализовано в v1.1.0. Проверено на Windows (NVIDIA RTX 4070 Ti + Intel UHD 770). На Astra Linux / AMD Radeon 680M — graceful degrade; полный GPU-compute зависит от установленного OpenCL ICD и ждёт прогона на стенде.
> **Связанные документы:** [`general_benchmark.md`](general_benchmark.md)
> (тот же принцип: Roofline-ratio + GM, шкала ×10 000, `None`-семантика —
> GPU-аналог этого дока), [`stress_score.md`](stress_score.md) (та же
> шкала ×10 000 и GM нормализованных ratio; опциональный GPU-стресс —
> идейный аналог стресс-балла CPU), [`scoring_v2.md`](scoring_v2.md)
> (общая Roofline-методика и обоснование GM между разнородными
> подсистемами).

## 1. Цель

GPU-балл — это **одно число типа `6500`**, которое выражает, насколько
близко видеокарта работает к своему **архитектурному пику** по двум
осям: вычисления (FP32) и пропускная способность видеопамяти (VRAM).
Назначение — быстрая, детерминированная и кроссвендорная оценка GPU
(«насколько эффективно карта использует своё железо») без обёртки над
внешними инструментами вроде FurMark / 3DMark и без графического
контекста.

Главные инварианты:

- **Детерминированность.** Одинаковая карта на одинаковом драйвере →
  одинаковое число (расхождение от запуска к запуску ≤ ±200 баллов на
  шкале ×10 000). Тот же порядок стабильности, что у общей оценки.
- **Симметричная шкала с общей оценкой и стресс-баллом**
  (`GPU_BENCHMARK_SCALE = 10 000`). Пользователь видит все баллы «бок о
  бок» и сравнивает CPU-, стресс- и GPU-метрику в одних попугаях.
- **Кроссвендорность через один бэкенд.** OpenCL (NVIDIA / AMD / Intel),
  свои `.cl`-кернелы, ctypes к ICD-loader'у. **Без новых зависимостей**
  Python (конвенция проекта — см. `CLAUDE.md`).
- **Roofline, а не эталонная база GPU.** Знаменатель — архитектурный
  предел *конкретной* карты, а не «попугаи как у конкурента». Тот же
  подход, что в scoring v2 и общей оценке (Williams 2009).
- **Честность к игровым/встроенным GPU.** FP64 намеренно **вне** балла
  (см. §7): на GeForce (1/64) и Intel iGPU (нет FP64) двойная точность
  урезана, её включение в балл занизило бы игровую карту несправедливо.
- **Без графики.** Нагрузка — compute (OpenCL), headless. Портируется на
  Astra Linux без X-сервера/рендер-контекста; «пончик» FurMark не нужен.

## 2. Формула

```
r_fp32 = fp32_gflops        / fp32_peak_gflops           # утилизация ALU
r_mem  = mem_bandwidth_gb_s / mem_bandwidth_peak_gb_s     # утилизация VRAM

# clamp ≤ 1.0 — не штрафовать топовое железо за консервативный пик
r_fp32 = min(r_fp32, 1.0)
r_mem  = min(r_mem,  1.0)

R     = GM(r_fp32, r_mem)                                 # √(r_fp32 · r_mem)
score = round(GPU_BENCHMARK_SCALE * R)   # GPU_BENCHMARK_SCALE = 10 000
```

Определения компонентов:

| Символ  | Числитель (измерено)          | Знаменатель (Roofline-пик)          | Ед.    |
|---------|-------------------------------|-------------------------------------|--------|
| `r_fp32`| `fp32_gflops`                 | `fp32_peak_gflops` (§3)             | GFLOPS |
| `r_mem` | `mem_bandwidth_gb_s`          | `mem_bandwidth_peak_gb_s` (§3)      | GB/s   |

**Только два компонента** входят в headline-балл: вычисления и память.
FP64 и PCIe-копирование измеряются и показываются как сырые скорости,
но в GM **не участвуют** (см. §6–§7).

Если **любой** из двух ratio = `None` (нет архитектурного пика для
устройства → знаменатель неизвестен; либо фаза не выполнилась →
числитель отсутствует) → `score = None`. Это та же строгая логика, что
в общей оценке: неполный прогон нельзя сравнивать с полным. В доменной
модели ([`domain/gpu.py`](../src/apexcore/domain/gpu.py)) это отражено
тем, что `r_fp32` / `r_mem` / `score` — `float | None`.

Реализация: [`application/gpu_benchmark_score.py`](../src/apexcore/application/gpu_benchmark_score.py)
— pure-функция, без сайд-эффектов, по образцу
`general_benchmark_score.py`.

### 2.1 Почему GM (а не HM)

`r_fp32` и `r_mem` относятся к **разным по природе** подсистемам GPU
(вычислительный конвейер ALU vs контроллер памяти) и меряются в разных
единицах (GFLOPS vs GB/s). По логике scoring v2 (`docs/scoring_v2.md`
§5.2) агрегация разнородных подсистем — уровень **GM**, а не HM
(HM применяется внутри однородной категории). Тот же выбор сделан в
общей оценке (`general_benchmark.md` §2) и стресс-балле.

Источник: Fleming P.J., Wallace J.J. (1986). *How not to lie with
statistics: the correct way to summarize benchmark results.*

## 3. Roofline-пики GPU

Считает [`application/gpu_roofline.py::compute_gpu_peak(device)`](../src/apexcore/application/gpu_roofline.py)
по [`GpuDeviceInfo`](../src/apexcore/domain/gpu.py) + таблице
[`data/gpu_arch.yaml`](../src/apexcore/data/gpu_arch.yaml). Результат —
[`GpuPeak`](../src/apexcore/domain/gpu.py) (`fp32_peak_gflops`,
`fp64_peak_gflops`, `mem_bandwidth_peak_gb_s`, `arch`, `source`,
`notes`). `data/gpu_arch.yaml` — прямой GPU-аналог CPU-таблицы
`_INTEL_HYBRID_SKU_TABLE` из `application/roofline.py`: маппинг
«архитектура → операций на блок за такт + fp64_ratio».

### 3.1 FP32-пик (архитектурный)

**Формула:**

```
fp32_peak_gflops = compute_units × clock_GHz × fp32_flops_per_cu_per_cycle(arch)
```

- `compute_units` = `GpuDeviceInfo.compute_units` — число вычислительных
  блоков из `clGetDeviceInfo(CL_DEVICE_MAX_COMPUTE_UNITS)`. Для NVIDIA
  это SM, для AMD — CU, для Intel — Xe-core/EU-группа.
- `clock_GHz` = `GpuDeviceInfo.max_clock_mhz / 1000` —
  `CL_DEVICE_MAX_CLOCK_FREQUENCY` (boost-частота драйвера).
- `fp32_flops_per_cu_per_cycle(arch)` — из `gpu_arch.yaml` по ключу
  `arch`. Значение уже учитывает FMA = 2 FLOP (как в CPU-таблице
  `SIMD_OPS_PER_CYCLE`, где SP AVX2 = 32 = 8 lanes × 2 FMA × 2). Для
  NVIDIA Ada 1 SM = 128 FP32-ALU × 2 (FMA) = **256 FP32-FLOP/такт**.

**Разрешение `arch`.** `gpu_roofline` определяет архитектуру по
`GpuDeviceInfo.name` / `vendor` / `platform_name` (например
`«NVIDIA GeForce RTX 4070 Ti»` → `nvidia_ada`) и записывает ключ в
`GpuDeviceInfo.arch` и `GpuPeak.arch`. Если модель не распознана →
`fp32_peak_gflops = None` → `r_fp32 = None` → балл `None`
(`source = "fallback"`, соответствующая запись в `notes`).

### 3.2 Пример-якорь: RTX 4070 Ti

```
fp32_peak = 60 CU (SM) × 2.655 ГГц × 256 FP32-FLOP/CU/такт
          = 40 780.8 GFLOPS ≈ 40.7 TFLOPS
```

Совпадает с паспортным пиком NVIDIA (≈ 40.1 TFLOPS FP32 у RTX 4070 Ti;
расхождение — из-за фактической boost-частоты, о `clamp ≤ 1.0` см.
ниже). FP64-пик той же карты = `40 780.8 / 64 ≈ 637 GFLOPS`
(`fp64_ratio = 1/64` для Ada, §7) — показывается как отдельная скорость,
в балл не входит.

### 3.3 Пик пропускной способности VRAM

**Формула:**

```
mem_bandwidth_peak_gb_s = mem_clock_effective_GHz × bus_width_bytes × ddr_factor
```

Пик памяти по спецификации архитектуры/модели из `gpu_arch.yaml`
(эффективная частота шины × ширина шины). OpenCL `clGetDeviceInfo` не
отдаёт ни ширину шины, ни частоту памяти напрямую, поэтому пик VRAM
резолвится **по модели** (таблица), а не по рантайм-полям устройства.
Где модель не покрыта таблицей → `mem_bandwidth_peak_gb_s = None` →
`r_mem = None` → балл `None`.

Для **встроенных** GPU (Intel iGPU, AMD Radeon 680M) выделенной VRAM
нет — «видеопамять» это разделяемая системная DRAM. Пик считается по
конфигурации системной памяти и помечается заметкой в `GpuPeak.notes`
(«пик памяти = разделяемая DRAM»), см. §8.

### 3.4 `clamp ≤ 1.0` — зачем

Как в общей оценке и стресс-балле, каждый ratio ограничен сверху
единицей. Boost-частота из драйвера, версия vBIOS и заводской разгон
могут дать *измеренную* производительность чуть выше нашего табличного
пика; без clamp это дало бы `score > 10 000` и сломало бы шкалу. Clamp
трактует «≥ пика» как «100% от пика».

## 4. Кроссвендорный бэкенд (OpenCL через ctypes)

[`infrastructure/gpu/opencl_backend.py`](../src/apexcore/infrastructure/gpu/opencl_backend.py)
— реализация порта [`domain/ports.py::GpuComputeBackend`](../src/apexcore/domain/ports.py).
Загружает ICD-loader через `ctypes.CDLL` и выполняет собственные
`.cl`-кернелы. **Внешние инструменты (FurMark, clpeak, mixbench) не
вызываются** — только свой код.

### 4.1 Загрузка loader'а по платформам

| ОС           | Библиотека ICD-loader'а | Как получить                          |
|--------------|-------------------------|---------------------------------------|
| Windows      | `OpenCL.dll`            | ставит драйвер GPU (NVIDIA/AMD/Intel) |
| Astra/Linux  | `libOpenCL.so.1`, `libOpenCL.so` | пакет `ocl-icd-libopencl1` + ICD вендора |

Порядок и имена библиотек — в `opencl_backend.py`. Устройства
перечисляются через `clGetPlatformIDs` → `clGetDeviceIDs`
(`CL_DEVICE_TYPE_GPU`), маппятся в [`GpuDeviceInfo`](../src/apexcore/domain/gpu.py)
(`clGetDeviceInfo`: name, vendor, `MAX_COMPUTE_UNITS`,
`MAX_CLOCK_FREQUENCY`, `GLOBAL_MEM_SIZE`, `MAX_WORK_GROUP_SIZE`,
`cl_khr_fp64` → `fp64_supported`).

### 4.2 Контракт порта

Совпадает с уже описанным `GpuComputeBackend`:

- `is_available() -> bool` — загрузился ли loader и есть ли хоть одно
  устройство. **Не бросает** исключение — при отсутствии OpenCL/железа
  просто `False` (graceful degrade).
- `list_devices() -> list[GpuDeviceInfo]` — пустой список, если ничего
  не найдено.
- `supports(device_index, kind) -> bool` — например, FP64 на iGPU без
  `cl_khr_fp64` → `False`.
- `measure(device_index, kind, duration_sec, cancel_token) -> GpuMeasurement`
  — прогоняет кернел заданное время; при `cancel_token.set()` завершается
  в пределах одной единицы работы и возвращает измерение по факту.

Тип нагрузки — [`GpuWorkloadKind`](../src/apexcore/domain/gpu.py):
`FP32`, `FP64`, `MEM_BANDWIDTH`, `PCIE_H2D`, `PCIE_D2H`,
`SUSTAINED_STRESS`.

### 4.3 Graceful degrade

Если `is_available()` вернул `False` (нет ICD, нет GPU-устройств, битый
драйвер) — раздел GPU-бенчмарка показывает понятное «OpenCL/устройство
недоступно», а не трейсбек. Балл при этом не строится (нет измерений).
Тот же принцип, что у сенсоров (`DegradedReason`) и общей оценки.

## 5. Кернелы (свои `.cl`)

Кернелы лежат рядом с бэкендом (`infrastructure/gpu/kernels/*.cl`) и
подбираются под каждый `GpuWorkloadKind`:

| Kind             | Кернел / операция                              | Метрика   |
|------------------|------------------------------------------------|-----------|
| `FP32`           | плотная цепочка FMA `a = a·b + c` (FP32)        | GFLOPS    |
| `FP64`           | то же в `double` (если `cl_khr_fp64`)          | GFLOPS    |
| `MEM_BANDWIDTH`  | STREAM-triad `a[i] = b[i] + scale·c[i]`         | GB/s      |
| `PCIE_H2D`       | `clEnqueueWriteBuffer` host→device              | GB/s      |
| `PCIE_D2H`       | `clEnqueueReadBuffer` device→host               | GB/s      |
| `SUSTAINED_STRESS` | длинная ALU-цепочка, макс. загрузка (power-virus) | — (телеметрия) |

FP32/FP64 считают GFLOPS из известного числа FMA на элемент × число
элементов / время. `MEM_BANDWIDTH` — GB/s из объёма прочитанных+
записанных байт / время (McCalpin STREAM). Кернелы верифицируют
контрольную сумму результата; несовпадение поднимает
`GpuMeasurement.error_count` (важно для стресс-режима, §9).

## 6. Тесты скоростей (сырые числа)

Каждый тест выдаёт **сырую скорость** и запускается как в составе
полного прогона, так и **по отдельности** (точечный запуск — как
отдельные микробенчи CPU). Соответствие полям
[`GpuBenchmarkReport`](../src/apexcore/domain/gpu.py):

| Тест                        | Kind            | Поле отчёта          | Ед.    | В балле? |
|-----------------------------|-----------------|----------------------|--------|:--------:|
| FP32-вычисления             | `FP32`          | `fp32_gflops`        | GFLOPS | **да**   |
| FP64-вычисления             | `FP64`          | `fp64_gflops`        | GFLOPS | нет (§7) |
| Пропускная способность VRAM | `MEM_BANDWIDTH` | `mem_bandwidth_gb_s` | GB/s   | **да**   |
| Копирование PCIe H2D        | `PCIE_H2D`      | `pcie_h2d_gb_s`      | GB/s   | нет      |
| Копирование PCIe D2H        | `PCIE_D2H`      | `pcie_d2h_gb_s`      | GB/s   | нет      |

FP64 и PCIe — информационные скорости: полезны для профиля карты
(насколько «просажена» двойная точность, какова реальная скорость шины
PCIe), но в GM не входят. FP64 запускается только при
`GpuDeviceInfo.fp64_supported == True`; иначе `fp64_gflops = None` и в
`notes` — «FP64 не поддерживается устройством».

## 7. Что входит и что НЕ входит в балл

**Входит в headline-балл (GM):**

- `r_fp32` — утилизация FP32-конвейера.
- `r_mem` — утилизация пропускной способности VRAM.

**НЕ входит (показывается отдельными числами):**

- **FP64 GFLOPS** — информационный тест. Обоснование: на потребительских
  (GeForce — FP64 = 1/64 от FP32) и встроенных (Intel iGPU — аппаратного
  FP64 нет вовсе) GPU двойная точность **намеренно урезана**
  производителем. Включение `r_fp64` в GM систематически и несправедливо
  занизило бы балл игровой карты — притом что для игр/графики/ML-инференса
  FP64 не нужен. FP64 GFLOPS показывается как отдельная скорость, если
  устройство её поддерживает. (Ср. `stress_score.md` §10 «двойной штраф»
  и `general_benchmark.md` §8 — тот же принцип «не подкручивать шкалу».)
- **PCIe H2D / D2H (GB/s)** — характеристика шины хост↔устройство, а не
  самой карты; сильно зависит от слота/чипсета материнской платы.
  Полезно как диагностика, но в балл «мощности GPU» не входит.

Итог: `score = GM(r_fp32, r_mem) × 10 000` — ровно два множителя.

## 8. Пайплайн прогона

[`application/gpu_benchmark.py::GpuBenchmarkOrchestrator.run()`](../src/apexcore/application/gpu_benchmark.py).
Последовательный прогон фаз для выбранного устройства
(`GpuDeviceInfo.index`). Итог — заполненный
[`GpuBenchmarkReport`](../src/apexcore/domain/gpu.py) (таблица
`gpu_benchmark_runs`, схема v5):

| # | Фаза              | Kind            | Длит.  | Заполняет                     |
|---|-------------------|-----------------|-------:|-------------------------------|
| 0 | enumerate + resolve peak | —        | ~0 с   | `device`, `*_peak_gflops`, `arch`, `peak_source` |
| 1 | FP32              | `FP32`          | ~10 с  | `fp32_gflops`, `fp32_duration_sec` |
| 2 | FP64 (если есть)  | `FP64`          | ~10 с  | `fp64_gflops`, `fp64_duration_sec` |
| 3 | VRAM bandwidth    | `MEM_BANDWIDTH` | ~10 с  | `mem_bandwidth_gb_s`, `mem_bandwidth_duration_sec` |
| 4 | PCIe H2D + D2H    | `PCIE_H2D`/`PCIE_D2H` | ~5 с | `pcie_h2d_gb_s`, `pcie_d2h_gb_s`, `pcie_duration_sec` |
| 5 | scoring           | —               | ~0 с   | `r_fp32`, `r_mem`, `score`, `notes` |

Фаза 5 вызывает `gpu_roofline` (пики) + `gpu_benchmark_score`
(ratio + GM). FP64/PCIe заполняют отчёт, но на `score` не влияют
(§7). Полный прогон ~30–40 с. При отмене (`cancel_token`) выставляется
`GpuBenchmarkReport.cancelled = True`, отчёт сохраняется по факту
выполненных фаз.

## 9. Опциональный стресс-режим («наш FurMark-эквивалент»)

Отдельный длительный режим (`GpuWorkloadKind.SUSTAINED_STRESS`) —
GPU-аналог CPU-стресс-теста. Не headline-балл, а **термо/стабилити-тест**:

- **Нагрузка:** длинный OpenCL power-virus-кернел (максимальная загрузка
  ALU), **compute, не рендер**. Headless — работает на Astra Linux без
  графического контекста (в отличие от «пончика» FurMark).
- **Телеметрия** во время прогона: температура, потребление (Вт),
  загрузка (%), троттлинг. Источники:
  - **NVIDIA** — NVML (`pynvml`, если доступен) / `nvidia-smi`.
  - **AMD/Linux** — hwmon (`/sys/class/drm/.../hwmon/...`,
    `amdgpu`-сенсоры).
- **Вердикт `PASS`/`FAIL`** — по образцу CPU-стресса
  (`stress_orchestrator.py`): FAIL при обнаруженном троттлинге,
  перегреве или росте `GpuMeasurement.error_count` (кернел верифицирует
  контрольную сумму — вычислительные ошибки под нагрузкой = FAIL).

Реализация телеметрии — рядом с бэкендом
(`infrastructure/gpu/telemetry_*.py`); оркестрация — в
`application/gpu_benchmark.py` (отдельный метод стресс-прогона). При
отсутствии источника телеметрии (нет NVML, нет hwmon) стресс-режим
деградирует: гоняет нагрузку и ловит `error_count`, но температуру/
троттлинг помечает как недоступные (см. §10).

## 10. Платформенные заметки и edge-cases

### 10.1 По вендорам/платформам

| Конфигурация                     | FP32 | VRAM BW | FP64 | Стресс-телеметрия | Комментарий |
|----------------------------------|:----:|:-------:|:----:|:-----------------:|-------------|
| **NVIDIA (Windows/Linux)**       | ✓    | ✓       | ✓ (1/N) | NVML          | Полный путь. Якорь §3.2. |
| **Intel iGPU**                   | ✓    | ✓ (DRAM)| обычно нет | hwmon/нет   | FP64 часто отсутствует; пик памяти = разделяемая DRAM (заметка в `notes`). |
| **AMD iGPU (Radeon 680M)**       | ✓    | ✓ (DRAM)| обычно нет | hwmon        | То же; на Astra зависит от ICD (§10.2). |
| **AMD дискретные**               | ✓    | ✓       | ✓ (зависит) | hwmon      | FP64-ratio по архитектуре из `gpu_arch.yaml`. |

Для iGPU (Intel и AMD APU) пик памяти считается по конфигурации
системной DRAM и помечается заметкой «пик памяти = разделяемая DRAM» —
это честнее, чем притворяться выделенной VRAM.

### 10.2 Astra Linux

Зависит от наличия **OpenCL ICD**:

- ICD установлен (например Mesa **Rusticl** или **Clover** для Radeon
  680M на стенде SE 1.8.5.46, либо `ocl-icd` + проприетарный вендор) →
  FP32 + VRAM bandwidth считаются, балл строится.
- ICD отсутствует → `GpuComputeBackend.is_available()` = `False`, балл
  не строится; при наличии сенсоров GPU раздел **деградирует до
  телеметрии** (temp/power через hwmon) без вычислительного балла.

Тестовый стенд — Astra Linux SE 1.8.5.46 (AMD Ryzen 7 6800H +
Radeon 680M iGPU); граничные случаи Astra фиксируются в
[`docs/Astra/problems_fixes.md`](Astra/problems_fixes.md).

### 10.3 `None`-семантика (сводно)

| Ситуация                                             | Последствие |
|------------------------------------------------------|-------------|
| Нет OpenCL / нет GPU-устройства                       | `is_available()=False` → нет измерений → `score=None` |
| Архитектура не в `gpu_arch.yaml`                      | `fp32_peak`/`mem_peak`=`None` → соответствующий ratio `None` → `score=None` |
| Фаза FP32 **или** VRAM не выполнилась                 | её числитель `None` → её ratio `None` → `score=None` |
| Нет FP64 (`fp64_supported=False`)                     | `fp64_gflops=None`; **балл считается** (FP64 вне GM) |
| Нет PCIe-замера                                       | `pcie_*=None`; **балл считается** (PCIe вне GM) |
| Нет NVML/hwmon в стресс-режиме                        | температура/троттлинг недоступны; вердикт по `error_count` |

Ключевое отличие от общей оценки: **отсутствие FP64 или PCIe балл НЕ
обнуляет** (они вне формулы) — обнуляют только пропажа FP32, VRAM или
их пиков.

## 11. Шкала и интерпретация

Балл линеен и прямо пропорционален средней (геометрической) доле от
пика: `score = R × 10 000`, где `R = GM(r_fp32, r_mem)`. Интерпретация:
доля от архитектурного потолка в тех же попугаях, что общая оценка и
стресс-балл.

- Карта, работающая на **70% от пика** по обеим осям (`r_fp32 =
  r_mem = 0.70`), даёт `R = 0.70` → **7000 из 10 000**.
- При асимметрии считается GM: например `r_fp32 = 0.55`, `r_mem = 0.77`
  → `R = √(0.55·0.77) ≈ 0.65` → **≈ 6500 из 10 000**.

Пример-расчёт (дискретная карта, реальные утилизации OpenCL-кернелов):

- `r_fp32 ≈ 0.60` (FP32-кернел ~60% от архитектурного пика),
  `r_mem ≈ 0.75` (STREAM-triad ~75% от пика VRAM)
- `R = GM(0.60, 0.75) ≈ 0.67` → **~6700 баллов**

Калибровочные ориентиры:

| Балл          | Что это значит                                    |
|--------------:|---------------------------------------------------|
| **10 000**    | Теоретический потолок (недостижим)                |
| **6500–8000** | Дискретная карта эффективно грузит ALU и VRAM     |
| **5000–6500** | Хорошая дискретная / сильный iGPU                 |
| **3000–5000** | Встроенная графика / слабая дискретная            |
| **< 3000**    | Сильно ограниченный / виртуальный GPU             |

Шкала линейна — критерий «лучше / хуже» работает в сравнении прогонов
*одной* методики (не абсолют между вендорами, см. §12).

## 12. Чем отличается от других баллов в apexcore

| Балл                              | Шкала    | Покрытие              | Cooling? | Назначение |
|-----------------------------------|---------:|-----------------------|:--------:|------------|
| **GPU-балл (этот док)**           | ×10 000  | GPU FP32 + VRAM BW     | нет      | «Насколько эффективно карта грузит ALU и память» |
| **General Benchmark**             | ×10 000  | CPU + RAM + Boot-диск  | нет      | «Насколько мощная система» (CPU-side) |
| **Стресс-балл** (`stress_score`)  | ×10 000  | CPU + RAM              | да       | «Выживает ли CPU/RAM под нагрузкой» |
| **Scoring v2 / Тест CPU**         | ×1 000   | 12 микробенчмарков CPU | нет      | Детальная разбивка по CPU-подсистемам |

GPU-стресс-режим (§9) — идейный аналог CPU-стресс-балла, но выдаёт
**вердикт PASS/FAIL + телеметрию**, а не число на шкале ×10 000.

## 13. Что балл НЕ умеет

- **Сравнить разные архитектуры абсолютно.** Как и вся Roofline-линейка
  apexcore (scoring v2, общая оценка, стресс-балл), балл измеряет
  «эффективность использования собственного железа», а не «абсолютную
  мощность как у конкурента». Карта с большим пиком имеет больший
  знаменатель: при одинаковой измеренной FP32-производительности она
  получит **меньший** ratio.
- **Заменить игровой бенчмарк.** Здесь compute-Roofline (FP32 + VRAM),
  а не FPS в реальных играх с растеризацией, RT-ядрами и driver-overhead.
- **Оценить RT / Tensor / видеокодеки.** Ray-tracing-ядра, тензорные
  блоки и медиа-движки вне OpenCL-FP32/BW-модели. Первая итерация — два
  ratio.

## 14. Что НЕ нужно добавлять (антипаттерны)

- **FP64 в GM.** Урезанная на потребительских/встроенных GPU двойная
  точность занизила бы игровую карту несправедливо (§7). Осознанное
  решение — FP64 вне балла.
- **PCIe в GM.** Это характеристика шины/платформы, не карты. Оставить
  информационной скоростью.
- **Веса между `r_fp32` и `r_mem`.** Обе оси одинаково важны для
  «эффективности GPU»; веса дали бы «подкрутку» под желаемый результат
  (ср. `general_benchmark.md` §8, `stress_score.md` §10).
- **Обёртка над FurMark / 3DMark / clpeak.** Методика — свои `.cl`-кернелы
  и Roofline; внешние инструменты недетерминированы под наш контракт и
  тянут зависимости/графический контекст.
- **Эталонная база публичных GPU.** Roofline уже даёт детерминированность
  без внешней БД — как в scoring v2 и общей оценке.
- **Cooling-фактор в headline-балл.** Термо-аспект — отдельный
  стресс-режим (§9) с вердиктом PASS/FAIL. Смешивать «сколько карта
  может выдать» и «сколько выдержит» в одно число — ошибка (тот же
  принцип разделения, что у общей оценки vs стресс-балла CPU).

## 15. Ссылки на модули

| Слой            | Модуль                                                   | Роль |
|-----------------|----------------------------------------------------------|------|
| domain          | [`domain/gpu.py`](../src/apexcore/domain/gpu.py)         | `GpuDeviceInfo`, `GpuMeasurement`, `GpuPeak`, `GpuBenchmarkReport`, `GpuWorkloadKind`, `GpuDeviceType` |
| domain (порты)  | [`domain/ports.py`](../src/apexcore/domain/ports.py)     | `GpuComputeBackend`, `GpuBenchmarkRepository` |
| application     | [`application/gpu_roofline.py`](../src/apexcore/application/gpu_roofline.py) | `compute_gpu_peak(device)` → `GpuPeak` (Roofline-пики) |
| application     | [`application/gpu_benchmark_score.py`](../src/apexcore/application/gpu_benchmark_score.py) | pure-функция `GM(r_fp32, r_mem) × 10 000` |
| application     | [`application/gpu_benchmark.py`](../src/apexcore/application/gpu_benchmark.py) | `GpuBenchmarkOrchestrator` — пайплайн фаз + стресс-режим |
| infrastructure  | [`infrastructure/gpu/opencl_backend.py`](../src/apexcore/infrastructure/gpu/opencl_backend.py) | реализация `GpuComputeBackend` (OpenCL/ctypes) + `.cl`-кернелы |
| data            | [`data/gpu_arch.yaml`](../src/apexcore/data/gpu_arch.yaml) | таблица «арх → flops/CU/такт + fp64_ratio + пик VRAM» |
| persistence     | таблица `gpu_benchmark_runs` (схема v5)                   | JSON `GpuBenchmarkReport` |

## 16. Валидационные якоря арх-пика

Проверочные точки формулы `fp32_peak = CU × ГГц × flops/CU/такт` (для
регрессионных тестов `gpu_roofline` — по образцу
`tests/unit/test_roofline.py`). Значения `flops/CU/такт` и `fp64_ratio`
— из `data/gpu_arch.yaml`.

| Устройство         | arch          | CU (SM) | Частота, ГГц | FP32 flop/CU/такт | FP32-пик, TFLOPS | fp64_ratio |
|--------------------|---------------|--------:|-------------:|------------------:|-----------------:|:----------:|
| RTX 4070 Ti        | `nvidia_ada`  | 60      | 2.655        | 256               | ≈ 40.7           | 1/64       |
| RTX 3080 (GA102)   | `nvidia_ampere` | 68    | 1.710        | 256               | ≈ 29.8           | 1/64       |
| RX 6800 XT (RDNA2) | `amd_rdna2`   | 72      | 2.250        | 128               | ≈ 20.7           | 1/16       |
| Radeon 680M (iGPU) | `amd_rdna2`   | 12      | 2.200        | 128               | ≈ 3.4            | —¹         |
| Intel Arc A770     | `intel_xe_hpg`| 32      | 2.100        | 256               | ≈ 17.2           | —²         |

¹ iGPU — пик памяти = разделяемая DRAM (заметка в `GpuPeak.notes`);
FP64 обычно недоступен.
² Intel Xe — аппаратный FP64 отсутствует на потребительских Arc →
`fp64_gflops=None`, FP64-тест пропускается.

> Числа FP32-пика в таблице — расчётные по формуле и служат якорями
> тестов; точные значения `flops/CU/такт`, частот и `fp64_ratio` берутся
> из `data/gpu_arch.yaml` (single source of truth, как
> `_INTEL_HYBRID_SKU_TABLE` для CPU). Расхождение с паспортом вендора на
> единицы процентов ожидаемо (boost-частота) и поглощается `clamp ≤ 1.0`.

## 17. Источники

1. Williams S., Waterman A., Patterson D. (2009). *Roofline: An
   Insightful Visual Performance Model for Multicore Architectures.*
   Communications of the ACM 52(4):65–76. DOI 10.1145/1498765.1498785.
2. Fleming P.J., Wallace J.J. (1986). *How not to lie with statistics:
   the correct way to summarize benchmark results.* Communications of
   the ACM 29(3):218–221. DOI 10.1145/5666.5673. (Обоснование GM.)
3. McCalpin J.D. (1995). *Memory Bandwidth and Machine Balance in
   Current High Performance Computers.* IEEE TCCA Newsletter.
   (STREAM-triad для `MEM_BANDWIDTH`.)
4. Khronos Group. *The OpenCL Specification* (`clGetDeviceInfo`,
   `CL_DEVICE_MAX_COMPUTE_UNITS` / `MAX_CLOCK_FREQUENCY`, `cl_khr_fp64`).
5. NVIDIA. *Ada / Ampere Architecture Whitepapers* (FP32-ALU на SM,
   FP64-ratio 1/64 на потребительских GeForce).
6. AMD. *RDNA / RDNA2 ISA & Architecture* (ALU на CU, FP64-ratio).
7. BAPCo. *SYSmark 30 scoring methodology — калибровка шкалы.*
   (Обоснование множителя-шкалы, ср. scoring v2 §5.4.)
