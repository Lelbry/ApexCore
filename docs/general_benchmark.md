# Оценки общей производительности — детерминированный композитный балл

> **Версия документа:** 1.0.0
> **Статус:** active
> **Связанные документы:** [`stress_score.md`](stress_score.md) (та же
> шкала ×10 000, тот же принцип GM), [`scoring_v2.md`](scoring_v2.md)
> (общая Roofline-методика), [`winsat.md`](winsat.md) (другой
> бенчмарк под этим же подразделом меню).

## 1. Цель

Композитный балл системы — это **одно число типа `7500`**, которое
агрегирует мощность CPU + RAM + загрузочного диска в детерминированную
метрику. Назначение — быстрая сравнительная оценка железа («какой ПК
быстрее») без необходимости проводить полный стресс-тест.

Главные инварианты:

- **Детерминированность.** Одинаковые конфигурации → одинаковые числа
  (расхождение от запуска к запуску ≤ ±200 баллов на шкале ×10 000).
- **Симметричная шкала со стресс-баллом** (`STRESS_SCORE_SCALE = 10 000`).
  Пользователь видит оба «бок о бок» и понимает, как сильно cooling
  сажает мощность под нагрузкой.
- **Без cooling-фактора.** Принципиальное отличие от стресс-балла: здесь
  нет `r_stability`. Прогон короткий, температурный режим — не цель.
  Балл отвечает на вопрос «сколько система может выдать», а не «сколько
  она выдержит».

## 2. Формула

```
r_dgemm  = measured_dgemm_gflops / compute_flops_peak("dp")
r_stream = measured_stream_gb_s  / (compute_dram_peak() / 1000)
r_disk   = GM(
    seq_read_mb_s    / disk_profile.seq_read_mb_s,
    random_read_mb_s / disk_profile.random_read_mb_s,
    seq_write_mb_s   / disk_profile.seq_write_mb_s,
)

# clamp ≤ 1.0 — не штрафовать топовое железо
r_dgemm  = min(r_dgemm,  1.0)
r_stream = min(r_stream, 1.0)
r_disk   = min(r_disk,   1.0)  # уже clamp'нут поэлементно

R = GM(r_dgemm, r_stream, r_disk)
score = round(GENERAL_BENCHMARK_SCALE * R)   # GENERAL_BENCHMARK_SCALE = 10 000
```

Если **любой** ratio = `None` (нет roofline для текущего CPU, нет места
на boot-диске, движок упал, и т.п.) → `score = None`. Это та же логика,
что и в стресс-балле: пользователь не должен сравнивать неполные
прогоны с полными.

Реализация: [`application/general_benchmark_score.py`](../src/apexcore/application/general_benchmark_score.py).
Pure-функция, без сайд-эффектов.

## 3. Roofline-пики

### CPU (DGEMM)
[`application/roofline.py::compute_flops_peak(system_info, "dp")`](../src/apexcore/application/roofline.py).
Формула: `physical_cores × SIMD_OPS_PER_CYCLE[simd][dp] × clock_GHz`.
SIMD-уровень эвристически по модели CPU (см. таблицы в файле). Override
через `APEXCORE_SIMD=avx2`, `APEXCORE_CPU_GHZ=5.0`.

### RAM (STREAM)
[`application/roofline.py::compute_dram_peak()`](../src/apexcore/application/roofline.py).
Формула: `modules × speed_MTs × 8 bytes`. На Windows через
`Win32_PhysicalMemory.ConfiguredClockSpeed`, на Linux через `dmidecode`.
Override: `APEXCORE_DRAM_MTS=3200`, `APEXCORE_DRAM_MODULES=4`.

### Boot-диск
[`infrastructure/disk_peak.py`](../src/apexcore/infrastructure/disk_peak.py)
содержит фиксированную таблицу пиков по `media_type` + `bus_type`:

| Диск          | seq read   | random read | seq write |
|---------------|-----------:|------------:|----------:|
| NVMe          | 3500 MB/s  | 600 MB/s    | 2500 MB/s |
| SATA SSD      | 550 MB/s   | 400 MB/s    | 500 MB/s  |
| HDD           | 200 MB/s   | 5 MB/s      | 180 MB/s  |
| unknown       | 200 MB/s   | 5 MB/s      | 180 MB/s  |

NVMe Gen3/Gen4/Gen5 не различаем — clamp `≤1.0` спасает топовое железо
от просадки балла из-за наших консервативных пиков. Random на HDD: 5 MB/s
— это реальный показатель seek-killer-нагрузки.

Boot-диск определяется через
[`infrastructure/disk_inventory.py::get_boot_drive()`](../src/apexcore/infrastructure/disk_inventory.py)
— на Windows через `%SystemDrive%` (`C:\`), на Linux mount-point `/`.
Матчинг с физическим диском по букве/mount-точке.

## 4. Пайплайн прогона

[`application/general_benchmark.py::GeneralBenchmarkOrchestrator.run()`](../src/apexcore/application/general_benchmark.py).
Последовательный (не параллельный) прогон фаз:

| # | Фаза              | Длительность | Что измеряется             |
|---|-------------------|-------------:|----------------------------|
| 1 | DGEMM             | ~30 с        | `dgemm_gflops`             |
| 2 | cooldown          | 5 с          | (термальная пауза)         |
| 3 | STREAM            | ~30 с        | `stream_gb_s`              |
| 4 | cooldown          | 5 с          |                            |
| 5 | disk seq read     | ~10 с        | `disk_seq_read_mb_s`       |
| 6 | disk random read  | ~10 с        | `disk_random_read_mb_s`    |
| 7 | disk seq write    | ~0.1–3 с     | `disk_seq_write_mb_s`      |

Итого: ~90–100 с на прогон.

Cooldown между CPU-фазами критичен на ноутбуках/мини-ПК — без него
DGEMM прогревает CPU настолько, что STREAM-фаза замеряется с
throttling'ом, и `r_stream` врёт. На десктопах с хорошим охлаждением
эффект минимален, но 5 c погоды не делают.

Disk seq write **намеренно не циклится**: один проход ровно 256 МБ
файла (`DiskSequentialWriteBench`). При типовом TBW NVMe 600 ТБ это
~600 000 прогонов = 1640+ лет ежедневного использования — износ
пренебрежим. Random write **не делаем** — это самый агрессивный
паттерн по износу, и для общей оценки сигнал от seq write уже
достаточен.

## 5. Шкала и интерпретация

При типичных CPU+RAM (DGEMM ~30% от теоретического AVX2-пика, STREAM
~50% от DRAM-пика) и NVMe Gen3+:

- `r_dgemm ≈ 0.30`, `r_stream ≈ 0.50`, `r_disk ≈ 0.70`
- `R = GM(0.30, 0.50, 0.70) ≈ 0.47` → **~4700 баллов**

Калибровочные ориентиры:

| Балл        | Что это значит                                  |
|------------:|-------------------------------------------------|
| **10 000**  | Теоретический потолок (недостижим)              |
| **7000–8000** | Очень мощная актуальная конфигурация (RTX-класс)|
| **5000–7000** | Хороший современный десктоп                   |
| **3500–5000** | Средний десктоп / рабочая станция             |
| **< 3000**  | Слабая / устаревшая / виртуальная среда         |

Шкала линейна — критерий «лучше / хуже» работает в сравнении прогонов.

## 6. Чем отличается от других баллов в apexcore

| Балл                              | Шкала    | Покрытие              | Cooling? | Назначение |
|-----------------------------------|---------:|-----------------------|:--------:|------------|
| **General Benchmark (этот док)**  | ×10 000  | CPU + RAM + Boot-диск | нет      | «Насколько мощная система» |
| **Стресс-балл** (`stress_score`)  | ×10 000  | CPU + RAM             | да       | «Выживает ли под нагрузкой» |
| **Scoring v2 / Тест CPU**         | ×1 000   | 12 микробенчмарков    | нет      | Детальная разбивка по подсистемам |
| **Аналог Winsat**                 | 1.0–9.9  | CPU + Memory + Disk   | нет      | Совместимость с Win32_Winsat |

## 7. Что балл НЕ умеет

- **GPU-compute** не входит в формулу (первая итерация). Если хочешь
  оценить GPU отдельно — пока используй внешние утилиты (3DMark,
  Geekbench Compute).
- **Сравнить разные архитектуры точно.** Roofline-нормировка измеряет
  «эффективность использования собственного железа», а не «абсолютную
  мощность как у конкурента».
- **Заменить полный apexcore-прогон.** Здесь только три ratio. Для
  детальной разбивки по micro-тестам используй раздел «Расширенное
  тестирование процессора» + scoring v2.

## 8. Что НЕ нужно добавлять (антипаттерны)

- **Веса между r_dgemm / r_stream / r_disk.** Все три одинаково важны
  для общего ощущения «как быстро работает ПК». Введение весов даст
  эффект «подкрутки» под желаемый результат.
- **Эталонная база CPU-моделей.** Это уже сделано для Single/Multi-Core
  (см. [`cpu_advanced`](cpu_advanced.md)), здесь Roofline решает ту
  же задачу детерминированности без внешней базы.
- **Длинный прогон (>5 минут).** Это уже стресс-тест, у него есть
  отдельный раздел меню и собственный балл с cooling-фактором.

## 9. Источники

1. Williams S., Waterman A., Patterson D. (2009). *Roofline: An
   Insightful Visual Performance Model for Multicore Architectures.*
   Communications of the ACM 52(4):65–76.
2. Fleming P.J., Wallace J.J. (1986). *How not to lie with statistics:
   the correct way to summarize benchmark results.* Communications of
   the ACM 29(3):218–221.
3. McCalpin J.D. (1995). *Memory Bandwidth and Machine Balance in
   Current High Performance Computers.* IEEE TCCA Newsletter.
   (STREAM-методика.)
4. BAPCo. *SYSmark 30 scoring methodology — 1000-scale.* (Обоснование
   шкалы.)
