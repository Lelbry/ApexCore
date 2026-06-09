# Аналог Windows Winsat

Пятый функциональный режим apexcore — оценка компьютера по шкале **1.0–9.9**,
точно имитирующая `Get-CimInstance Win32_Winsat`. Не пересекается с
1000-балльной Roofline-шкалой `apexcore micro run` (scoring v2).

## Запуск

```powershell
apexcore winsat run                  # 5 подтестов, по 5 с каждый
apexcore winsat run --duration 10    # дольше, точнее
apexcore winsat formal               # алиас для `run`, в стиле winsat
apexcore winsat query                # последний сохранённый отчёт
apexcore winsat list --limit 20      # последние N прогонов
apexcore winsat run --export out.json # JSON-экспорт
```

Из меню: `apexcore` → **Аналог Windows Winsat** (только Windows; на Linux пункт скрыт).

## Архитектура

```
domain/winsat.py                       # Pydantic-модели
application/
  winsat_scoring.py                    # формула metric → 1.0..9.9
  winsat_service.py                    # WinsatService.run_formal()
infrastructure/
  microbench/disk.py                   # Sequential / Random Read
  persistence/winsat_repo.py           # SqliteWinsatRepository
data/winsat_thresholds.yaml            # калибровочные пороги
interfaces/cli/
  commands/winsat.py                   # CLI: run / formal / query / list
  menu/winsat_screen.py                # WinsatScreen
  render.py                            # render_winsat_report
```

## Подкатегории

| Подкатегория  | Метрика                          | Источник                                     | Статус |
|---------------|----------------------------------|----------------------------------------------|--------|
| CPUScore      | hm(AES-256, SHA-1) MB/s          | `Aes256Bench` + `Sha1Bench` (cryptography, hashlib) | PASS  |
| MemoryScore   | memory_read MB/s                 | `MemoryReadBench` (np.sum, 256 МБ float64)   | PASS  |
| DiskScore     | min(seq 64K, random 16K) MB/s    | `DiskSequentialReadBench` + `DiskRandomReadBench` | PASS  |
| GraphicsScore | —                                | —                                             | N/A   |
| D3DScore      | —                                | —                                             | N/A   |
| **WinSPRLevel** | min(всех PASS подскоров) | агрегация                                     | —     |

## Алгоритм скоринга

Для каждой подкатегории — таблица пороговых точек `(value, score)` в
`data/winsat_thresholds.yaml`. Между точками — линейная интерполяция по
`log2(value)`. Снаружи — clamp на `[1.0, 9.9]`.

```python
def score_from_metric(value, points):
    if value <= points[0].value:  return 1.0
    if value >= points[-1].value: return 9.9
    # линейная интерполяция по log2
    t = (log2(value) - log2(p_lo.value)) / (log2(p_hi.value) - log2(p_lo.value))
    return p_lo.score + t * (p_hi.score - p_lo.score)
```

CPUScore = HM(AES-256, SHA-1) MB/s — гармоническое среднее штрафует
дисбаланс между крипто-подсистемами.
DiskScore = min(seq, random) — как у настоящего Winsat.
WinSPRLevel = min среди PASS-подскоров. NA/ERROR игнорируются.

## Калибровка

Пороги откалиброваны под скриншот пользователя (Ryzen 5/7 + Gen3 NVMe →
CPU 9.5 / Memory 9.5 / Disk 8.7) + публичные обзоры (Anandtech, TechPowerUp).
После прогонов на разных машинах возможно потребуется подкрутить — тогда
увеличить `version` в YAML.

Контрольные точки (см. `tests/unit/test_winsat_scoring.py`):
- 76 800 MB/s CPU → 9.5
- 55 000 MB/s memory_read → 9.5
- 3 500 MB/s seq read → 8.7
- 1 800 MB/s random read → 8.7

## Хранение

SQLite-таблица `winsat_runs` (миграция v3, additive — micro_runs не дропаются).
Полный `WinsatReport` в JSON-колонке + индексные поля для list/sort.

```sql
CREATE TABLE winsat_runs (
    id              TEXT PRIMARY KEY,
    started_at      TEXT NOT NULL,
    ended_at        TEXT NOT NULL,
    cpu_score       REAL,
    memory_score    REAL,
    disk_score      REAL,
    graphics_score  REAL,
    d3d_score       REAL,
    winspr_level    REAL,
    cpu_model       TEXT,
    os_name         TEXT,
    payload_json    TEXT NOT NULL
);
```

## MVP-ограничения

1. **Только Windows.** На Linux команда возвращает «недоступно на этой ОС»
   (exit 2), пункт меню скрыт.
2. **GraphicsScore / D3DScore = N/A.** Реализация D3D11-бенчмарка требует
   bundled native engine (C++ exe) — запланирована на следующий релиз.
3. **Disk queue depth = 1.** Python sync I/O. Реальный Winsat использует
   `-n 0..4` (asynchronous overlapped I/O).
4. **Без `FILE_FLAG_NO_BUFFERING`.** Page cache ОС может прогревать
   повторные чтения. Mitigation: 256 МБ файл + 4 warmup-итерации.

## Запланировано (следующий релиз)

- D3D11 native bench (Direct3D Batch / Alpha / Tex / ALU / Geom / CBuffer)
- Video Memory bandwidth
- Media Foundation video encode/decode
- Disk un-cached I/O через ctypes-обёртку над `CreateFileW(0x20000000)`
- Поддержка Linux: эквиваленты через OpenGL / Vulkan compute

## Связанные файлы

- [Pydantic-модели](../src/apexcore/domain/winsat.py)
- [Scoring](../src/apexcore/application/winsat_scoring.py)
- [Service](../src/apexcore/application/winsat_service.py)
- [Disk-бенчмарки](../src/apexcore/infrastructure/microbench/disk.py)
- [SQLite repo](../src/apexcore/infrastructure/persistence/winsat_repo.py)
- [CLI](../src/apexcore/interfaces/cli/commands/winsat.py)
- [Menu](../src/apexcore/interfaces/cli/menu/winsat_screen.py)
- [Render](../src/apexcore/interfaces/cli/render.py) — `render_winsat_report`
- [Калибровка](../src/apexcore/data/winsat_thresholds.yaml)
