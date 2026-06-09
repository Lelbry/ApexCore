# apexcore Scoring v2 — формальная спецификация

> **Версия документа:** 2.0.0
> **Статус:** active (этап реализации v2 в `dev`)
> **Связанные документы:** `docs/research/aggregated_overall_performance_assessment.md` (исследование, на котором основан этот документ).

## 1. Цели

Документ описывает, как apexcore вычисляет «общую оценку производительности» начиная с версии 2.0. Цели спецификации:

1. Зафиксировать формулы, чтобы все компоненты (CLI, меню, web-UI, экспортёры) использовали одну и ту же арифметику.
2. Обеспечить **академическую защитимость** интерпретации балла в рамках дипломной работы — каждое решение опирается на peer-reviewed источник.
3. Дать чёткий контракт для миграции старых данных и сравнения новых результатов.

## 2. Концепция балла

**Балл apexcore = эффективность системы относительно её собственного архитектурного пика, выраженная в процентах × 1000.**

- 1000 — система работает на 100% теоретического пика (физически невозможно, но это верхняя граница);
- 500 — система использует 50% своего архитектурного пика (типично для реальных нагрузок);
- 100 — 10% пика (плохо охлаждаемая, ограниченная батареей, или сильно загруженная фоном).

### 2.1 Почему Roofline, а не SPEC-style ref-machine

- **SPEC-стиль** (Sun Fire V490 как эталон у SPEC; Lenovo M720q у BAPCo; Dell Precision 3460 у Geekbench) требует **физической эталонной машины**, которой у дипломника нет. Объявить собственный ноутбук эталоном для всех — не защитимо.
- **Roofline-модель** (Williams, Waterman, Patterson 2009, *Communications of the ACM* 52(4):65–76, DOI 10.1145/1498765.1498785) — peer-reviewed подход, в котором эталоном является **архитектурный предел железа конкретной машины**. Балл интерпретируется как доля от теоретического максимума.
- Этот подход применяется в академических исследованиях performance optimization уже более 15 лет; имеет тысячи цитирований.

### 2.2 Формула в одну строку

```
APEXCORE_SCORE = 1000 · ∏_subsystems (∏_categories (HM_workloads(r_ij))^w_ij )^w_subsystem
```

Где `r_ij = measured_value / theoretical_peak` (для throughput-метрик), все веса нормированы к ∑w = 1.

## 3. Источник данных: 12 микробенчмарков

Источник данных для скоринга — **только** микробенчмарки CPU (`infrastructure/microbench/`). Stress-движки в скоринге **не участвуют** — они используются отдельно для теста стабильности.

### 3.1 Состав

| Категория | Подтесты | Единица |
|---|---|---|
| `memory` | `memory_read`, `memory_write`, `memory_copy` | MB/s |
| `flops` | `flops_sp`, `flops_dp` | GFLOPS |
| `integer` | `int_iops_24`, `int_iops_32`, `int_iops_64` | GIOPS |
| `crypto` | `aes_256`, `sha1` | MB/s |
| `fractal` | `julia_sp`, `mandelbrot_dp` | FPS |

Источник: `infrastructure/microbench/registry.py::build_default_microbench_registry()`.

### 3.2 Почему именно эти

1. **Детерминированный алгоритм:** одинаковая реализация на всех платформах (Windows, Astra Linux), идентичные единицы измерения внутри категории.
2. **Покрытие подсистем:** memory bandwidth, FPU (SP+DP), ALU (24/32/64-bit), crypto-ускорение (AES-NI/SHA-NI), real-world FP-нагрузка (фракталы).
3. **Привязка к индустриальным эталонам:** STREAM (McCalpin 1995) для memory, BLAS DGEMM (Dongarra et al. 2003) для flops, Mandelbrot/Julia (Devaney 1989) для fractals — все имеют peer-reviewed основание.

## 4. Reference values (Roofline-эталоны)

### 4.1 Иерархия источников

Reference value для каждого подтеста выбирается по правилу:

1. **Roofline (теоретический пик)** — основной источник. Где применим:
   - `memory_*`: `channels × DDR_speed_MTs × 8 bytes / 1e9` GB/s.
   - `flops_*`: `physical_cores × SIMD_width × 2 (FMA) × clock_GHz`. SIMD_width: AVX2 = 8 SP / 4 DP, AVX-512 = 16 SP / 8 DP.
   - `int_iops_*`: `physical_cores × 4 ops/cycle × clock_GHz` (general-purpose ALU throughput, Hennessy-Patterson 6th ed., Ch.3).
   - `aes_256` (если AES-NI): эмпирическая оценка ~1.3 GB/s на GHz на ядро (Intel datasheet).
   - `sha1` (если SHA-NI): ~3 cycles/byte → `clock_GHz / 3 · 1000` MB/s на ядро.

2. **Empirical proxy** — для тестов без чёткого теоретического предела:
   - `julia_sp`, `mandelbrot_dp` — нет аналитического Roofline, используется медиана из `data/empirical_reference.yaml`.
   - `aes_256`/`sha1` без аппаратных инструкций — fallback на empirical proxy.

3. **Frozen machine** — на будущее (опционально). Полезен, если в дипломе нужно формально сослаться на конкретную аппаратную конфигурацию.

### 4.2 Формула ratio

```
r = measured_value / reference_value         (higher-is-better)
r = reference_value / measured_value         (lower-is-better)
```

Все micro-тесты — throughput (higher-is-better). Latency-тестов в micro нет (есть в stress, но stress не участвует в скоринге).

### 4.3 Notes-флаги

В `OverallScore.notes` фиксируются:
- `roofline_partial` — часть подтестов не покрыта Roofline (использован empirical_proxy).
- `roofline_unavailable` — Roofline вообще не вычислен (нет CPU info).
- `provisional` — reference set не финализирован (для empirical proxy с n<10).

## 5. Агрегация (трёхуровневая иерархия)

### 5.1 Внутри категории — гармоническое среднее (HM)

Внутри категории все подтесты имеют **одинаковую единицу** (memory все в MB/s, fractal все в FPS, и т.д.). Используется HM:

```
r_category = HM(r_1, …, r_k) = k / Σ(1/r_i)
```

Обоснование: Smith J.E. (1988), *Communications of the ACM* 31(10):1202–1206, DOI 10.1145/63039.63043:

> «*For rates, the harmonic mean produces results consistent with the inversely related execution times.*»

HM также чувствителен к слабому звену (weakest-link sensitive): если один из тестов сильно проседает, HM отражает это сильнее, чем GM или AM. Для bottleneck-sensitive метрик (а throughput внутри подсистемы как раз такой) это правильное поведение. Тот же принцип использует UL PCMark Gaming.

### 5.2 Между категориями — взвешенное геометрическое среднее (GM)

Между разнородными категориями (FLOPS vs IOPS vs FPS) единицы разные, GM корректнее:

```
R_CPU_compute = exp( Σ w_i · ln(r_i) / Σ w_i )
              для i ∈ {flops, integer, crypto, fractal}
```

Обоснование: Fleming P.J., Wallace J.J. (1986), *Communications of the ACM* 29(3):218–221, DOI 10.1145/5666.5673:

> «*Using the arithmetic mean to summarize normalized benchmark results leads to mistaken conclusions that can be avoided by using the preferred method: the geometric mean.*»

GM инвариантно к выбору референсной машины (Mashey 2004), статистически корректно для лог-нормально распределённых ratio (Iqbal & John 2010).

### 5.3 Между подсистемами — взвешенное GM

```
R_MEM = r_memory                      # одна категория = одна подсистема
R_overall = exp( w_MEM · ln(R_MEM) + w_CPU · ln(R_CPU_compute) ) / (w_MEM + w_CPU) )
```

Дефолтные веса: `w_MEM = 1.0`, `w_CPU_compute = 1.0` (equal subsystem weights). Обоснование: OECD/JRC Handbook on Composite Indicators (Nardo et al., DOI 10.1787/9789264043466-en) — equal weights — рекомендуемый дефолт когда нет эмпирической базы для асимметрии.

### 5.4 Итоговая шкала

```
overall_score = 1000 · R_overall
```

Множитель 1000 — нормировка по образцу BAPCo SYSmark 30 §2.4 («calibration system → 1000»). Для Roofline-интерпретации:
- `R_overall = 1.0` → balance achieved (100% architectural peak) → score = 1000.
- `R_overall = 0.5` → 50% peak → score = 500.

## 6. Множественные прогоны и CI

### 6.1 Пресеты

| Пресет | n_runs | Агрегация per-workload | CI | Время |
|---|---|---|---|---|
| **fast** | 1 | значение прогона | — | ~5 мин |
| **standard** | 3 | median-of-3 | — | ~15 мин |
| **accurate** | 5 | mean (или trimmed mean при n≥10) | t-CI на лог-шкале | ~25 мин |

Median-of-3 для n=3 — стандарт SPEC CPU 2017 Run Rules: «*Reportable runs can use the median, or the slower of two runs*».

### 6.2 CI на лог-шкале

Поскольку benchmark ratios мультипликативны, CI считается **на лог-шкале**:

```
y_j = ln(R_j)               для j-го прогона
ȳ = (1/n) · Σ y_j
s_y = sqrt(Σ(y_j - ȳ)² / (n-1))

CI_95%(R) = [ exp(ȳ - t_{0.975, n-1} · s_y/√n),
              exp(ȳ + t_{0.975, n-1} · s_y/√n) ]

CI_95%(score) = 1000 · CI_95%(R)
```

Обоснование: Lilja D.J. (2000), *Measuring Computer Performance*, Cambridge UP, ISBN 0-521-64105-5, гл.4 + Приложение C.

Для n≥10 при выраженной асимметрии — bootstrap (Kalibera & Jones 2012, *Proc. of 3rd EuroPerf Workshop*):

1. Из исходных n прогонов сэмплируются B=1000 ресэмплов с возвращением.
2. Для каждого ресэмпла — пересчёт `geomean_score`.
3. CI = 2.5-й и 97.5-й перцентили распределения.

Триггер для bootstrap: `scipy.stats.skew(per_run_ratios) > 1.0` (умеренная асимметрия).

## 7. Thermal stability — отдельная метрика

Thermal stability **не входит** в overall_score. Это самостоятельная метрика по образцу UL 3DMark Stress Test (UL Benchmarks support article «Stress test result screen»):

```
THERMAL_STABILITY = 100 · min(f) / max(f)
```

где `f` — `cpu_avg` частота из `MetricSnapshot.frequencies` всех snapshot’ов теста.

Порог pass: `THERMAL_STABILITY ≥ 97%`.

> «*The main result from the Stress Test is your PC's Frame Rate Stability expressed as a percentage. […] To pass the test, your system's frame rate stability must be at least 97% and all loops must be completed.*»
> — UL Benchmarks Support, Stress test result screen.

Дополнительно (опционально) считается **TSC (Thermal Sensitivity Coefficient)**:

```
TSC = (S_cold - S_steady) / S_cold
```

где `S_cold` — балл первой минуты, `S_steady` — балл после 10 минут. Полезно как одночисловой индикатор устойчивости в отчётах.

## 8. Версионирование

`OverallScore.scoring_version` фиксирует версию формулы:

- `"2.0.0"` — этот документ. Roofline + HM/GM + 1000-шкала.
- `"1.x"` — старая версия (composite_score / sigmoid+z-score). Несовместима с 2.0.

Сравнение баллов разных версий **запрещено**. CLI/WebUI отказываются сравнивать v1/v2 без флага `--force`.

При смене reference-формулы (например, добавление `r_memory_lat` в R_MEM) поднимается minor-версия (`2.1.0`). При смене subsystem weights — major (`3.0.0`).

## 9. Обратная совместимость

При первом запуске v2.0 на venv с существующей БД:
- Старые таблицы `runs` и `baselines` дропаются (v1 содержала только composite_score, который концептуально неверен).
- Создаётся новая таблица `micro_runs` для v2-результатов.
- `BenchmarkResult.final_score` остаётся в модели для обратной совместимости JSON-payload, но всегда заполняется 0.0.
- Реальный балл живёт в `MicroBenchSuiteResult.overall.overall_score`.

## 10. Сравнение с другими пользователями

Roofline-баллы естественно сравнимы между пользователями **без backend**:

- Не «у меня 1850, у тебя 1620» как абсолют.
- А «моя система использует 0.42 от своего архитектурного пика, твоя — 0.38 от своего».
- Это интерпретация в духе Williams 2009: оценивается **операционная эффективность относительно roofline**, не «общая мощь».

Дополнительные опции на будущее:
- Локальный экспорт/импорт JSON (вариант A из исследования).
- GitHub-based community leaderboard (вариант C, требует git-аккаунта).
- Backend с публичной БД (вариант B, отдельный SaaS-проект, не для дипломной работы).

## 11. Источники (краткий список, полный — в исследовательском отчёте)

1. Williams S., Waterman A., Patterson D. (2009). Roofline: An Insightful Visual Performance Model for Multicore Architectures. *Communications of the ACM* 52(4):65–76. DOI: 10.1145/1498765.1498785.
2. Fleming P.J., Wallace J.J. (1986). How not to lie with statistics. *CACM* 29(3):218–221. DOI: 10.1145/5666.5673.
3. Smith J.E. (1988). Characterizing computer performance with a single number. *CACM* 31(10):1202–1206. DOI: 10.1145/63039.63043.
4. Mashey J.R. (2004). War of the benchmark means. *SIGARCH CAN* 32(4):1–14. DOI: 10.1145/1040136.1040137.
5. Iqbal M.F., John L.K. (2010). Confusion by all means. UCAS-6.
6. Lilja D.J. (2000). *Measuring Computer Performance*. Cambridge UP. ISBN 0-521-64105-5.
7. Kalibera T., Jones R.E. (2012). Quantifying Performance Change with Robot-Run Benchmarks. *Proc. 3rd EuroPerf Workshop*.
8. McCalpin J.D. (1995). Memory Bandwidth and Machine Balance in Current High Performance Computers. *IEEE Computer Society TCCA Newsletter*, December 1995.
9. Dongarra J.J., Luszczek P., Petitet A. (2003). The LINPACK Benchmark. *Concurrency and Computation* 15(9):803–820. DOI: 10.1002/cpe.728.
10. Hennessy J.L., Patterson D.A. (2017). *Computer Architecture: A Quantitative Approach*, 6th ed. Morgan Kaufmann.
11. UL Benchmarks. Stress test result screen / 3DMark Time Spy formula.
12. BAPCo. SYSmark 30 Whitepaper Rev. 1.1 (2022).
13. Nardo M. et al. *Handbook on Constructing Composite Indicators*. OECD. DOI: 10.1787/9789264043466-en.
