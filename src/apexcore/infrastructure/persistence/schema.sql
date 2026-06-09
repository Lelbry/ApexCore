-- Схема SQLite для apexcore. Применяется через migrations.apply_schema().
-- Все timestamp хранятся как ISO-8601 строки в UTC.

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

-- Полный документ прогона хранится как JSON для гибкости + индексные поля
-- вынесены в колонки, чтобы быстро фильтровать список.
CREATE TABLE IF NOT EXISTS runs (
    id            TEXT PRIMARY KEY,
    profile_name  TEXT NOT NULL,
    start_time    TEXT NOT NULL,
    end_time      TEXT NOT NULL,
    final_score   REAL NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'completed',
    cpu_model     TEXT,
    os_name       TEXT,
    payload_json  TEXT NOT NULL  -- BenchmarkResult в JSON.
);

CREATE INDEX IF NOT EXISTS idx_runs_profile_start ON runs(profile_name, start_time DESC);
CREATE INDEX IF NOT EXISTS idx_runs_start ON runs(start_time DESC);

-- Базовые профили нормализации.
CREATE TABLE IF NOT EXISTS baselines (
    id                  TEXT PRIMARY KEY,
    name                TEXT NOT NULL UNIQUE,
    profile_name        TEXT NOT NULL,
    system_fingerprint  TEXT NOT NULL,
    sample_size         INTEGER NOT NULL,
    created_at          TEXT NOT NULL,
    payload_json        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_baselines_profile ON baselines(profile_name);

-- Прогоны микробенчмарков (scoring v2). Главное хранилище для
-- общей оценки производительности по docs/scoring_v2.md. Каждая
-- запись = агрегированный MicroBenchSuiteResult (с overall, ci, preset).
CREATE TABLE IF NOT EXISTS micro_runs (
    id              TEXT PRIMARY KEY,
    start_time      TEXT NOT NULL,
    end_time        TEXT NOT NULL,
    preset          TEXT,            -- fast / standard / accurate (NULL для legacy/standalone)
    n_runs          INTEGER NOT NULL DEFAULT 1,
    overall_score   REAL,            -- 1000 * R_overall (NULL если score не посчитан)
    ci_lower        REAL,
    ci_upper        REAL,
    cpu_model       TEXT,
    os_name         TEXT,
    scoring_version TEXT,             -- '2.0.0' для v2 runs
    payload_json    TEXT NOT NULL    -- полный MicroBenchSuiteResult с overall.
);

CREATE INDEX IF NOT EXISTS idx_micro_runs_start ON micro_runs(start_time DESC);
CREATE INDEX IF NOT EXISTS idx_micro_runs_preset ON micro_runs(preset, start_time DESC);
CREATE INDEX IF NOT EXISTS idx_micro_runs_score ON micro_runs(overall_score DESC);

-- Прогоны Winsat-аналога (шкала 1.0–9.9). Не пересекаются с micro_runs:
-- здесь итог в формате Win32_Winsat (CPUScore/MemoryScore/DiskScore/
-- GraphicsScore/D3DScore + WinSPRLevel = min подскоров). См. docs/winsat.md.
CREATE TABLE IF NOT EXISTS winsat_runs (
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

CREATE INDEX IF NOT EXISTS idx_winsat_runs_started ON winsat_runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_winsat_runs_winspr ON winsat_runs(winspr_level DESC);

-- Прогоны «Оценок общей производительности» (general benchmark, шкала ×10 000).
-- Композитный балл CPU + RAM + Boot-диск, формула GM(r_dgemm, r_stream, r_disk).
-- Отдельная таблица от winsat_runs (другая шкала) и от micro_runs (нет cooling-фактора).
-- См. docs/general_benchmark.md.
CREATE TABLE IF NOT EXISTS general_benchmark_runs (
    id                      TEXT PRIMARY KEY,
    started_at              TEXT NOT NULL,
    ended_at                TEXT NOT NULL,
    score                   REAL,            -- ×10 000, NULL если score не посчитан
    dgemm_gflops            REAL,
    stream_gb_s             REAL,
    disk_seq_read_mb_s      REAL,
    disk_random_read_mb_s   REAL,
    disk_seq_write_mb_s     REAL,
    disk_media_label        TEXT,            -- "NVMe" / "SATA SSD" / "HDD"
    cpu_model               TEXT,
    os_name                 TEXT,
    payload_json            TEXT NOT NULL    -- полный GeneralBenchmarkReport
);

CREATE INDEX IF NOT EXISTS idx_gb_runs_started ON general_benchmark_runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_gb_runs_score ON general_benchmark_runs(score DESC);
