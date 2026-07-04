# apexcore

Кроссплатформенная (Windows 11 / Astra Linux) методика комплексной оценки производительности
компьютерной системы и её программная реализация.

## Возможности

- **Общая оценка производительности системы** *(шкала ×10 000)* — короткий
  композитный бенчмарк CPU + RAM + загрузочного диска. Балл = `GM(r_dgemm,
  r_stream, r_disk) × 10 000`. Прогон ~1.5 минуты, без термальной защиты:
  отвечает на вопрос «сколько может выдать система», не «сколько выдержит».
- **Стресс-нагрузка** *(шкала ×10 000)* — длительный параллельный стресс с
  термальным watchdog, Frame-Rate-Stability % (по образцу UL 3DMark) и
  своим «Стресс-баллом» = `GM(r_dgemm, r_stream, r_stability) × 10 000`.
  Можно сравнивать «бок о бок» с общей оценкой — разница показывает,
  насколько cooling сажает мощность под длительной нагрузкой.
- **Детальная общая оценка (scoring v2)** *(шкала ×1000)* — балл = доля от
  теоретического архитектурного пика (Roofline-модель, Williams 2009). 12
  микробенчмарков в 5 категориях (memory / flops / integer / crypto /
  fractal) → иерархическое взвешенное геометрическое среднее. Пресеты
  `fast` / `standard` / `accurate` с 95 % доверительными интервалами на
  лог-шкале (Lilja 2000), bootstrap для асимметричных распределений
  (Kalibera-Jones 2012).
- **Расширенный тест ОЗУ и кеша (Ram&Cache)** — 16 измерений
  Read / Write / Copy / Latency × DRAM / L1 / L2 / L3, без балла —
  диагностическая таблица 4×4.
- **Аналог Windows Winsat** — точная копия `Get-CimInstance Win32_Winsat`
  со шкалой 1.0–9.9 (CPUScore, MemoryScore, DiskScore, WinSPRLevel). Только
  Windows.
- **Раздел «Датчики»** — live-дашборд температур, частот, напряжений,
  мощности, вентиляторов и накопителей с группировкой по карточкам.
- Гибридный стресс-тест: собственные алгоритмы CPU / RAM (numpy / numba) +
  готовые утилиты (`stress-ng` на Linux, `prime95` на Windows).
- Хранение прогонов и базовых профилей в SQLite, экспорт в JSON / CSV.
- Чистая модульная архитектура (Hexagonal + Clean): платформенно-независимое
  ядро + адаптеры под Windows 11 и Astra Linux.

### Температуры на Windows без admin

С версии 0.5.1 чтение температур построено вокруг **fallback-chain** с
приоритетом источников без admin-прав: HWiNFO / CoreTemp / AIDA64 Shared
Memory → LibreHardwareMonitor (встроенный pythonnet, DLL'и в репозитории)
→ AMD Ryzen Master → ACPI → WMI. Если основной источник недоступен
(Memory Integrity, Smart App Control, антивирус), автоматически
переключаемся на следующий. Команда `apexcore doctor` показывает
конкретную причину отказа и предлагает решение.

С версии 0.5.2 опционально устанавливается `apexcore_sensord` (Windows-
сервис под LocalSystem) — держит LHM открытым весь life-time и публикует
снимок 100+ сенсоров в Global shared memory. Это даёт **UAC-free** доступ
к live-сенсорам из обычных не-admin процессов.

## ⚠ Миграция с v1 (breaking change)

При первом запуске apexcore **2.0 на venv с существующей БД** старые таблицы `runs`
и `baselines` будут **удалены** (старые баллы scoring v1 концептуально несовместимы
с новой шкалой Roofline-ratio · 1000). Новая таблица `micro_runs` создаётся пустая.
Это однократная процедура, выполняется автоматически в `migrations.apply_schema`.

Если нужно сохранить старые данные — перед первым запуском v2 сделайте бэкап:
```powershell
Copy-Item $env:APPDATA\apexcore\apexcore.sqlite3 .\apexcore-v1-backup.sqlite3
```

## Установка из исходников

```bash
python -m venv .venv
# Windows
.venv\Scripts\Activate.ps1
# Linux
source .venv/bin/activate

pip install -e ".[dev,fast]"
# для веб-визуализации (фаза 2, Windows)
pip install -e ".[webui]"
```

## Платформы

Поддерживаются **Windows 11** и **Astra Linux** / Ubuntu 22.04+. Релиз
**v1.1.0** — единый: под одним номером лежат установщики на обе ОС
(`apexcore-setup-1.1.0.exe` и `apexcore_1.1.0_amd64.deb`).

> **Windows Server (2016 / 2019 / 2022).** По зависимостям совместим: ядро
> (CLI, scoring, микробенчмарки, общая оценка, стресс, Ram & Cache) — чистый
> Python + numpy / scipy / psutil, без привязки к клиентским компонентам
> Windows 11; Windows-extras (`pywin32`, `wmi`, `pythonnet`) объявлены под
> `sys_platform == 'win32'`, что покрывает и Server; жёсткой проверки версии
> ОС в коде нет. Ограничения: графический подскор аналога WinSAT (`winsat dwm`)
> требует Desktop Experience (на Server Core деградирует в «нет данных»;
> CPU / Memory / Disk считаются своими движками и работают); WebView2-установщик
> требует WebView2 Runtime (на Win 11 предустановлен, на Server — вручную) —
> установка из исходников этого не требует. **⚠ На самой Windows Server не
> разворачивалось** — вывод по анализу зависимостей и кода.

## Быстрый старт

```bash
apexcore                                       # интерактивное TUI-меню (рекомендуется)
apexcore info                                  # сведения о системе
apexcore monitor --duration 10                 # 10 секунд телеметрии
apexcore micro run --preset standard           # детальный балл scoring v2 (~15 мин)
apexcore ram-cache run                         # Ram&Cache 4×4 (~2 мин)
apexcore stress run --engine builtin_cpu_fp --duration 30
apexcore bench run --profile cpu_heavy         # полный прогон бенчмарка
apexcore winsat run                            # Аналог Win32_Winsat, шкала 1.0-9.9 (Windows)
apexcore runs list                             # история прогонов
apexcore export <run_id> --format json
```

Запуск **«Общей оценки производительности системы»** (шкала ×10 000,
~1.5 минуты) — пункт 4 интерактивного меню. Из TUI же доступен и аналог
Winsat (пункт 4 → 2 на Windows).

## Структура проекта

```
src/apexcore/
    domain/           # модели + порты, без внешних зависимостей кроме pydantic
    application/      # use-cases, нормализация, статистика, диагностика, тренды
    infrastructure/   # адаптеры ОС, стресс-движки, репозитории, экспортеры
    interfaces/       # CLI (Typer) + опциональный Web UI (FastAPI)
    shared/           # конфиг, логирование, единицы измерения
```

## Тесты

```bash
pytest -q             # ~1022 unit + integration
ruff check .          # линт
mypy src              # типы
```

## Упаковка

- Windows: `scripts/build_windows.ps1` (PyInstaller + Inno Setup)
- Astra Linux: `scripts/build_astra.sh` (deb-пакет)
