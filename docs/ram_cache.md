# Расширенный тест ОЗУ и кеша (Ram&Cache)

Матрица 4×4 со скоростями **Read / Write / Copy / Latency** для уровней
**DRAM / L1 / L2 / L3**. Тест диагностический, **не входит в общий балл
(scoring v2)**: результаты не сохраняются в SQLite — это «снимок здесь и
сейчас», аналог `apexcore info`. Для сохранения результата используется
JSON-экспорт.

## CLI

```bash
apexcore ram-cache list                                    # перечень 16 измерений
apexcore ram-cache run                                     # все 16, длительность из настроек меню
apexcore ram-cache run --duration 1.0                      # быстрый smoke
apexcore ram-cache run --tests dram_read,l3_latency        # только выбранные
apexcore ram-cache run --tests 1,3,5                       # по номерам из `list`
apexcore ram-cache run --export ramcache.json              # сохранить отчёт
```

## Меню

Пункт «Расширенный тест оперативной памяти и кеша (Ram&Cache)» на главном
экране. Внутри:

1. Посмотреть список тестов
2. Запустить все тесты
3. Запустить выбранные тесты (вводятся номера или имена через запятую)

Длительность одного измерения (по умолчанию 8 с/измерение → ~2 минуты на
полный прогон) настраивается в `Настройки → Длительность тестирования →
Расширенный тест ОЗУ и кеша`. Сохранить отчёт в файл из меню нельзя — это
интерактивный режим. Для архивирования прогона используйте CLI-флаг
`--export PATH.json` (см. раздел «CLI» выше).

## Что измеряется

|        | Read¹       | Write²      | Copy³       | Latency⁴   |
|--------|-------------|-------------|-------------|------------|
| DRAM   | пропускная способность чтения вне LLC | запись вне LLC | копирование (read+write) вне LLC | задержка cache-miss обращений |
| L3     | те же операции, размер буфера = 50% L3 | …             | …             | …            |
| L2     | размер буфера = 50% L2 | …             | …             | …            |
| L1     | размер буфера = 50% L1 (типично 16 КБ) | …             | …             | …            |

- **Read** (`MB/s`) — последовательное чтение float64-буфера. Через `numba`
  компилируется в плотный цикл с автовекторизацией; без `numba` — `np.sum`.
- **Write** (`MB/s`) — последовательная запись с чередованием двух скаляров,
  чтобы оптимизатор / write-combine не свернули запись в no-op.
- **Copy** (`MB/s`) — `dst[i] = src[i]` (numba) или `np.copyto` (fallback).
  Считается как `read + write` (как в STREAM Triad). Размер буфера делится
  пополам — `src` и `dst` оба должны помещаться в нужный уровень.
- **Latency** (`ns`) — pointer-chasing по перетасованным индексам.
  Префетчер не может предсказать следующий адрес → каждое чтение цепляет
  cache-miss соответствующего уровня.

## Имена тестов

Каноническое имя — `<level>_<operation>` в нижнем регистре. Полный список:

```
dram_read   dram_write   dram_copy   dram_latency
l3_read     l3_write     l3_copy     l3_latency
l2_read     l2_write     l2_copy     l2_latency
l1_read     l1_write     l1_copy     l1_latency
```

В `--tests` можно передавать имена или номера (1..16) из вывода
`apexcore ram-cache list`.

## Как определяется размер кеша

| ОС | Источник | Заглушка |
|---|---|---|
| Windows | `Win32_Processor.L2CacheSize`, `L3CacheSize` через WMI (значения в КБ). L1 у Win32_Processor нет → fallback. | 32 КБ / 256 КБ / 8 МБ |
| Linux  | `/sys/devices/system/cpu/cpu0/cache/index{0..N}/{level,type,size}`. Для L1 берётся data-кеш. | те же fallback'и |
| Прочее | Только дефолты | те же fallback'и |

В таблице рядом с уровнем выводится колонка **Источник**: `wmi` / `sysfs` /
`fallback`. DRAM всегда помечен как `fallback` — там «размер уровня» это
просто размер тестового буфера (256 МБ, заведомо больше любого LLC).

## Ограничения метода

1. **Python-overhead vs. numba**. Чистый NumPy на маленьких буферах (L1, L2)
   тратит существенную долю времени на интерпретацию цикла, а не на сам
   доступ к памяти. Если установлены extras `[fast]` (`pip install -e
   ".[fast]"`), тест использует JIT-компилированные ядра numba и
   значения становятся репрезентативными. Без numba L1/L2 значения нужно
   читать как «нижний предел» — реальный CPU умеет больше.

   Принудительно отключить numba для проверки fallback можно через
   переменную окружения: `APEXCORE_DISABLE_NUMBA=1`.

2. **Один поток**. Тест последовательный — оценка пропускной способности
   на одно ядро. Многопоточная агрегация уже измеряется в стресс-тесте
   `builtin_ram_bw`.

3. **Шум измерений на L1/L2**. Размер буфера выбирается как 50% от уровня
   с запасом на стек/код, но измерения всё равно подвержены влиянию
   фоновой активности ОС. Для надёжных чисел запускайте тест с длительностью
   ≥ 4 с/измерение и при отсутствии другой нагрузки.

## Поток выполнения

```
apexcore ram-cache run [--tests …] [--duration N]
    └─ RamCacheService.run(duration, selected_pairs=…)
        1. AdapterFactory.detect() → SystemInfo + CacheTopology (через WMI/sysfs)
        2. Цикл по выбранным парам (level, op) в каноническом порядке:
             RamCacheBench(level, op, buffer_bytes).run(duration)
        3. RamCacheReport (метрики + system_info + topology)
    └─ render_ram_cache_report(report) — таблица 4×4 + сноска ¹²³⁴
    └─ при --export — RamCacheReport.model_dump_json() в файл
```

## Связанные файлы

- `src/apexcore/domain/cache.py` — Pydantic-модели `CacheTopology`, `RamCacheReport`.
- `src/apexcore/infrastructure/adapters/cache.py` — парсеры WMI/sysfs + fallback.
- `src/apexcore/infrastructure/microbench/ram_cache.py` — `RamCacheBench`.
- `src/apexcore/application/ram_cache_service.py` — `RamCacheService`, `all_test_names()`, `parse_test_name()`.
- `src/apexcore/interfaces/cli/commands/ram_cache.py` — CLI: `ram-cache list`, `ram-cache run [--tests]`.
- `src/apexcore/interfaces/cli/menu/screens.py:RamCacheScreen` — экран меню (4 пункта + b/q).
- `src/apexcore/interfaces/cli/render.py:render_ram_cache_report` — таблица 4×4.
- `src/apexcore/interfaces/cli/messages.py:RAMCACHE_FOOTNOTES` — русские описания метрик.
- `tests/unit/test_ram_cache.py`, `tests/unit/test_cache_topology.py` — unit-тесты.

## Источники подхода

- McCalpin, J. D. (1995). *Memory Bandwidth and Machine Balance in Current
  High Performance Computers.* (STREAM benchmark) — основа Copy-метрики.
- McVoy, L. & Staelin, C. (1996). *lmbench: portable tools for performance
  analysis* — основа методики pointer-chasing для Latency.
