# apexcore Stress Score — детерминированный балл стресс-нагрузки

> **Версия документа:** 2.0.0 (4-компонентная формула с r_thermal)
> **Статус:** active
> **Связанные документы:** `docs/scoring_v2.md` (общий scoring v2 — та же шкала ×1000 и те же принципы Roofline), `docs/research/stress_test_mark_method.md` (обоснование r_thermal), `docs/research/stress_score_validity.md` (анализ валидности).

## 1. Цель

Стресс-балл — это **единственное число типа `4250`**, агрегирующее результаты комбинированной нагрузки CPU + RAM + охлаждение в детерминированную метрику. Цель — дать пользователю сравнимый показатель *sustainable performance* — устойчивой производительности с учётом теплового запаса.

**UI-лейбл балла в финальном экране стресс-теста** — «Оценка под нагрузкой (CPU+RAM+охлаждение)». Внутреннее имя константы и модуля — `stress_score`, оставлено для обратной совместимости. Чистая оценка производительности (без thermal-компонента) — отдельный раздел меню **«Общая оценка производительности системы»** (`application/general_benchmark.py`); ссылку на него содержит подпись под Panel-плашкой.

Главные инварианты:

- **Детерминированность.** Два идентичных сервера в одинаковых условиях охлаждения должны давать балл с расхождением ≤ ±300 единиц при шкале ×10 000.
- **Чувствительность к ограничениям.** Если у одной из двух идентичных систем хуже охлаждение или больше фоновой нагрузки — балл должен это отразить.
- **Sustainable performance свойство** (research §3.3): «слабая+холодная ≥ сильная+горячая». Без учёта thermal headroom это свойство невыразимо.
- **Шкала ×10 000 (≠ ×1000 у общего scoring v2).** С 4-компонентной формулой и `cap=1.15` для r_thermal максимально возможный балл ≈ 11 500.

## 2. Формула

```
r_dgemm     = dgemm_gflops / roofline_dgemm_peak       # утилизация SIMD
r_stream    = stream_gb_s   / dram_peak                 # утилизация DRAM
r_stability = frame_rate_stability_pct / 100            # лагающий thermal-индикатор
r_thermal   = clamp(1 + α·(headroom − 1), 0.50, 1.15)   # leading thermal headroom

R_stress     = GM(r_dgemm, r_stream, r_stability, r_thermal)
stress_score = STRESS_SCORE_SCALE · R_stress             # STRESS_SCORE_SCALE = 10 000
```

где `α = 0.5`, `headroom = (TJmax − T_max) / 30°C`.

Если хотя бы один из четырёх ratio = `None` (нет CPU temp, нет roofline для текущего CPU, нет частотной телеметрии, прогон < 90 сек) → балл = `None`, плашка в финальной карточке не выводится; рядом — строка «Оценка под нагрузкой недоступна» с конкретной причиной.

Реализация: `application/stress_score.py::compute_stress_score_context()`. Pure-функция, без сайд-эффектов.

## 3. Обоснование выбора GM (а не HM)

В scoring v2 (`scoring.py`) HM используется **внутри категории** — для weakest-link sensitivity между подтестами одной природы (например, `memory_read` + `memory_write` + `memory_copy`). GM используется **между категориями** — для агрегации разных по природе подсистем (`R_MEM × R_CPU_compute`).

Стресс-балл агрегирует **четыре разных подсистемы**: compute (DGEMM), memory (STREAM), cooling (стабильность частот под нагрузкой), thermal headroom (запас до TJmax). По логике scoring v2 это уровень GM, не HM.

Источник методики GM нормализованных ratio: Fleming P.J., Wallace J.J. (1986). *How not to lie with statistics: the correct way to summarize benchmark results.* Communications of the ACM 29(3):218–221.

Sustainable performance argument — research `stress_test_mark_method.md` §3 (Аррениус, тепловая инерция кулера, headroom на условия).

## 4. Откуда берутся Roofline-пики и thermal-лимит

### 4.1 DGEMM peak

`application/roofline.py::compute_flops_peak(system_info, "dp")`.

**Базовая формула:** `physical_cores × ops_per_cycle(SIMD_level, "dp") × clock_GHz`.

**Гибридные Intel (Alder/Raptor Lake — i9-12900K, i7-13700K, …):** Если CPU распознан таблицей `_INTEL_HYBRID_SKU_TABLE`, формула суммирует P-cores и E-cores раздельно: `P_n × ops × P_GHz + E_n × ops × E_GHz`. Это устраняет систематическое завышение peak на ~14% (гомогенная формула умножает все ядра на P-core boost, игнорируя что E-cores работают медленнее).

Источники: Intel Optimization Reference Manual §2.4 (Alder Lake hybrid topology), Intel ARK для конкретных SKU.

### 4.2 DRAM peak

`application/roofline.py::compute_dram_peak(system_info)`.

**Формула:** `effective_channels × speed_MTs × 8 bytes/transfer`, где
`effective_channels = min(modules, max_channels(cpu_model))`.

Раньше формула суммировала пропускную способность модулей (`modules × speed × 8`), что давало 2× завышение на типичном desktop с 4 DIMM (4 модуля в dual-channel = 2 канала, не 4). `_max_dram_channels` распознаёт платформу по строке `cpu_model`:

| Platform | max_channels |
|---|---|
| Desktop Intel/AMD (Core i3-i9, Ryzen 3-9) | 2 |
| HEDT Intel (Xeon W, i9-...x/xe) | 4 |
| HEDT AMD (Threadripper non-PRO) | 4 |
| Server AMD (Threadripper PRO, EPYC 7xxx) | 8 |
| Server AMD Zen 4 (EPYC 9xx4, 8004) | 12 |
| Server Intel (Xeon Scalable, E5/E7) | 6 |

Источники: JEDEC JESD79-4 (DDR4 channel definition), AnandTech/ServeTheHome platform reviews.

### 4.3 r_thermal и TJmax

`application/stress_score.py::compute_r_thermal(t_max, tjmax)`. Точная формула в research `stress_test_mark_method.md` §5.1.

`TJmax` — документированный предел рабочей температуры кристалла. Резолвится через `application/roofline.py::resolve_tjmax(system_info)`:
1. Env-override `APEXCORE_TJMAX` (для тестов и edge cases).
2. Таблица `CPU_TJMAX_TABLE` по семейству CPU (по строке `cpu_model`):

| Семейство | TJmax |
|---|---|
| Intel desktop (Core, Pentium, Celeron) | 100 °C |
| Intel HEDT/Xeon W | 100 °C |
| Intel Xeon Scalable | 100 °C |
| AMD Ryzen 5000 (Zen 3) | 90 °C |
| AMD Ryzen 7000+ (Zen 4/5) | 95 °C |
| AMD Threadripper | 95 °C |
| AMD EPYC Genoa (9xx4) | 95 °C |
| AMD EPYC Bergamo (8004) | 105 °C |

Если CPU не распознан таблицей — `tjmax=None` → `r_thermal=None` → балл не строится.

**MSR-чтение** (`MSR_TEMPERATURE_TARGET` Intel / SMU AMD) **не реализовано** — только табличный fallback. Откладывается (см. research §10).

### 4.4 frame_rate_stability_pct

`application/thermal.py::compute_thermal_stability()`. Формула: `100 · min(cpu_avg_freq) / max(cpu_avg_freq)` по всему окну прогона. Источник методики: UL 3DMark Stress Test, threshold 97%.

### 4.5 T_max

Берётся напрямую из `ThermalStabilityResult.temp_max_c` — то же поле, которое заполняет `compute_thermal_stability` по telemetry sensor sampling. Требует CPU temp через LHM/PawnIO/sensord (см. CLAUDE.md). Без admin/без sensord → `t_max=None` → балл не строится.

## 5. Длительность прогона и точность

Балл вычисляется **всегда** при наличии всех четырёх ratio — даже при коротком прогоне (< 90 сек). Это сознательное решение (запрос пользователя 2026-05-17): пользователю полезнее увидеть приближённое число с пометкой «оценка приближённая», чем «недоступно».

Константа `RELIABLE_DURATION_SEC = 90.0` в `stress_score.py` — порог визуальной маркировки:
- `duration_sec ≥ 90` — Panel с подписью «производительность CPU+RAM с учётом стабильности частот и теплового запаса».
- `duration_sec < 90` — Panel с warning внутри: «Прогон N с короче 90 с — оценка приближённая. Для честной sustainable-метрики используйте 10–60 минут (кулер выходит на тепловой стационар 60–120 с)».

Обоснование 90 сек как порога: тепловой стационар воздушного кулера 60–120 секунд (research §3.2). На 30-секундном окне «горячая» система ещё не показала всех своих проблем — `T_max` нерепрезентативен. **Для честной оценки sustainable performance рекомендуется 10–60 минут** (research §8.3).

## 6. Grace-window thermal watchdog

`application/thermal_watchdog.py`. Логика watchdog'а изменена для согласованности с r_thermal-методикой:

- **Зона safe (T < Tj_max − margin):** watchdog не вмешивается; grace-таймер сброшен.
- **Зона warning (Tj_max − margin ≤ T < Tj_max):** запускается grace-таймер. В течение `DEFAULT_GRACE_WINDOW_SEC = 60` секунд watchdog **не отменяет** прогон — даёт измерить sustainable performance в зоне Critical. Если T опустилась ниже warning — таймер сброшен.
- **Истечение grace-window:** `WatchdogTrigger(reason="grace_window_expired")` — плановый стоп, прогон завершается с сохранёнными измерениями.
- **Зона hard-stop (T ≥ Tj_max):** instant cancel с `reason="tjmax_reached"`, безопасность сохраняется.

Backward-compat: `ThermalWatchdog(..., grace_window_sec=0)` восстанавливает старое поведение instant stop по threshold (`reason="threshold_reached"`).

Это **win-win**: r_thermal получает точное измерение T_max в зоне Critical, а абсолютный предел Tj_max остаётся жёстким барьером. См. ответ пользователя 2026-05-17.

## 7. Override через окружение

Для тестов и для случаев, когда автоопределение не сработало:

| Переменная | Что переопределяет | Пример |
|---|---|---|
| `APEXCORE_SIMD` | SIMD-уровень CPU | `avx2`, `avx512` |
| `APEXCORE_CPU_GHZ` | Max (turbo) частота CPU | `5.0` |
| `APEXCORE_DRAM_MTS` | Скорость памяти, MT/s | `3200` |
| `APEXCORE_DRAM_MODULES` | Число модулей памяти | `4` |
| `APEXCORE_TJMAX` | TJmax в °C | `100` |

С этими переменными можно зафиксировать Roofline-пики для регрессионных тестов — что и делается в `tests/unit/test_stress_score.py::fixed_env`.

## 8. Типичные значения

На типовом desktop с реальным BLAS (DGEMM ~30% от теоретического AVX2-пика), реальным STREAM (~70% от честного DRAM-пика после фикса каналов), нормальным охлаждением (T_max=70°C, r_thermal=1.0) и отсутствием тротлинга балл попадает в диапазон **5000–7000**:

- `r_dgemm ≈ 0.30` — встроенный naive DGEMM
- `r_stream ≈ 0.65` — после фикса каналов (без него — 0.32)
- `r_stability ≈ 0.99` — нормально охлаждаемая система
- `r_thermal = 1.00` — T_max=70°C, headroom ровно на эталон

GM(0.30, 0.65, 0.99, 1.00) = 0.66 → ~6 600 баллов. На холодной системе (T_max=55°C, r_thermal=1.13) тот же расчёт даёт ~7 500 баллов. На горячей (T_max=95°C, r_thermal=0.58) — ~4 100 баллов. Шкала линейна.

## 9. Что балл НЕ умеет

- **Сравнить разные архитектуры.** Сервер с AVX-512 имеет больший знаменатель, чем сервер с AVX2 — значит, при одинаковом measured DGEMM AVX-512-сервер получит **меньший** ratio. Это особенность Roofline-нормировки: она измеряет не «производительность как у конкурента», а «эффективность использования собственного железа».
- **Заменить общий APEXCORE_SCORE.** Общий балл (см. `docs/scoring_v2.md`) включает 12 микробенчмарков и не зависит от условий охлаждения. Стресс-балл — это **отдельная характеристика устойчивости под нагрузкой**.
- **Спрогнозировать срок службы.** Балл оценивает thermal headroom (запас от долгосрочной деградации), но не моделирует деградацию во времени — это задача отдельного long-term endurance-теста.
- **Замерить sustainable на 30 сек.** Гейт ≥ 90 сек отсекает короткие прогоны; для real sustainable нужны 10–60 минут.

## 10. Что НЕ нужно добавлять (антипаттерны)

- **Двойной штраф за тротлинг.** Тротлинг уже снижает `r_stability` (частоты «садятся» → ratio падает) и `r_thermal` (T_max близко к TJmax). Третий множитель «штраф за тротлинг» исказил бы шкалу.
- **Эталонная база публичных CPU.** Идея «попугаев, сравнимых между Cinebench-публикациями» неприменима — нет публичных данных по нашим конкретным DGEMM/STREAM-реализациям. Roofline + r_thermal решают задачу детерминированности без внешней базы.
- **Веса между r_dgemm / r_stream / r_stability / r_thermal.** Все четыре подсистемы одинаково важны для sustainable performance. Введение весов даст эффект «подкрутки» под желаемый результат — научно не защитимо.
- **r_thermal_ram (DDR temp).** JEDEC спецификация DDR5: PMIC удваивает refresh rate выше 85°C → уже скрыто бьёт по `r_stream`. Включение DDR-температуры рисует двойной счёт штрафа. См. research §8.1.

## 11. Источники

1. Williams S., Waterman A., Patterson D. (2009). *Roofline: An Insightful Visual Performance Model for Multicore Architectures.* Communications of the ACM 52(4):65–76. DOI 10.1145/1498765.1498785.
2. Fleming P.J., Wallace J.J. (1986). *How not to lie with statistics: the correct way to summarize benchmark results.* Communications of the ACM 29(3):218–221.
3. Smith J.E. (1988). *Characterizing computer performance with a single number.* Communications of the ACM 31(10):1202–1206.
4. UL Solutions. *3DMark Stress Test methodology — Frame Rate Stability ≥ 97%.* (Источник критерия `r_stability`.)
5. BAPCo. *SYSmark 30 scoring methodology — 1000-scale.* (Обоснование шкалы.)
6. Intel SDM Vol. 3B §15 (MSR_TEMPERATURE_TARGET, package thermal sensors).
7. Intel Optimization Reference Manual §2.4 (Alder Lake hybrid topology).
8. JEDEC JESD79-4 (DDR4 SDRAM Standard, channel definition).
9. JEDEC JESD79-5 (DDR5 SDRAM Standard, thermal management).
10. Research документ `docs/research/stress_test_mark_method.md` — обоснование 4-компонентной формулы с r_thermal.
11. Research документ `docs/research/stress_score_validity.md` — анализ валидности старой 3-компонентной формулы.
