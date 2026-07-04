# cpu_ranking.yaml — публичная база десктопных CPU

Используется блоком **«Положение среди популярных CPU»** в карточке
результата теста Single-Core / Multi-Core
(`apexcore.interfaces.cli.render.render_single_multi_result`).

Логика: `application/cpu_ranking.py::match_cpu_ranking()` сопоставляет
`SystemInfo.cpu_model` пользователя с записями этого YAML по `cpu_pattern`,
а если ничего не нашёл — пытается approx по топологии (P/E ядрам).
В UI показывается только **рейтинговая позиция** (топ N%, M-е место
из N) — абсолютные числа `single_score` / `multi_score` пользователю
никогда не показываются.

## Схема

```yaml
schema_id: "cpu-ranking-v1"   # обязательно ровно эта строка
version: "0.1.0"               # bump при пополнении
created_at: "YYYY-MM-DD"
notes: "Описание набора."

cpus:
  - id: "intel-i9-12900k"      # машинный slug, kebab-case, уникален
    display_name: "Intel Core i9-12900K"
    cpu_pattern: "i9-12900k"   # нижний регистр, без (R)/(TM)
    family: "alder_lake"       # справочно, не участвует в матчинге
    physical_cores: 16
    p_cores: 8                 # для hybrid Intel; иначе null
    e_cores: 8                 # для hybrid Intel; иначе null
    logical_threads: 24
    single_score: 1985         # внутренние очки (single-thread)
    multi_score: 27100         # внутренние очки (multi-thread)
    notes: "" # свободный комментарий
```

## Откуда числа

`single_score` / `multi_score` пропорциональны типичным результатам
Cinebench R23 1T / nT по публичным обзорам:

- Tom's Hardware (reviews / hierarchy)
- AnandTech (Bench archive)
- GamersNexus Mega Charts
- Notebookcheck (для подтверждения)

Эти конкретные числа — публичные факты (как «у i9-12900K 16 ядер»),
не подлежат копирайту. **Целые базы данных** (например
`felixsteinke/cpu-spec-dataset` — AGPL-3.0) копировать **нельзя**:
лицензия несовместима с MIT-лицензией apexcore.

## Как добавить новый CPU

1. Найти **минимум 2** независимых обзора с числами Cinebench R23 1T/nT
   (или их аналогом через Geekbench 6, нормированным к шкале набора).
2. Усреднить, округлить до сотен (для multi) и десятков (для single).
3. Добавить запись в секцию `cpus:` соответствующего семейства.
4. Bump `version` (minor): `0.1.0` → `0.2.0`.
5. Запустить тесты: `pytest -q tests/unit/test_cpu_ranking.py` — должно
   быть зелено. Особо обратить внимание:
   - `test_yaml_loads_and_validates` — Pydantic-валидация.
   - Уникальность `id` (модель сама проверяет).

## Что НЕ добавляем

- **Серверные Xeon / EPYC** — рейтинг был бы некорректным (десктоп vs сервер).
- **Threadripper** — выходит за пределы пользовательской аудитории apexcore.
- **Мобильные H / HX / HS** — другие TDP и другие профили скейлинга.
- **Apple Silicon** — другая ISA (ARM), `Int64IopsBench` numba-LCG там работает
  на macOS, но соотношение Multi/Single сильно отличается из-за асимметричной
  топологии. Можно вынести в отдельную секцию позже.

## Связанные файлы

- `application/cpu_ranking.py` — Pydantic-модели и matcher.
- `interfaces/cli/render.py::_ranking_grid` — рендер секции.
- `tests/unit/test_cpu_ranking.py` — тесты модели/загрузки/матчинга.
- `tests/unit/test_render_single_multi_ranking.py` — тесты рендера.

См. также CLAUDE.md по правилам неизменности `domain/models.py`
(этот YAML и связанный код **не** требуют изменений в моделях — они
работают через опциональный параметр render-функции).
