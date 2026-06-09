"""Экспорт прогона в CSV.

Файл получается «длинным»: одна строка на снимок ``MetricSnapshot``. Шапка
метаданных прогона записывается в начало как комментарии (``# key=value``),
чтобы файл оставался валидным CSV для большинства парсеров.
"""

from __future__ import annotations

import csv
from pathlib import Path

from apexcore.domain.models import BenchmarkResult


def export_run_csv(result: BenchmarkResult, out_path: Path) -> Path:
    """Сохранить metrics_history прогона в CSV (метаданные — в шапке-комментарии)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as fh:
        fh.write(f"# apexcore_run={result.id}\n")
        fh.write(f"# profile={result.config.profile_name}\n")
        fh.write(f"# start={result.start_time.isoformat()}\n")
        fh.write(f"# end={result.end_time.isoformat()}\n")
        fh.write(f"# os={result.system_info.os_name}\n")
        fh.write(f"# cpu={result.system_info.cpu_model}\n")
        fh.write(f"# final_score={result.final_score:.6f}\n")
        fh.write(f"# stress_results={len(result.stress_results)}\n")
        for sr in result.stress_results:
            fh.write(
                f"# stress: {sr.engine}({sr.category})="
                f"{sr.throughput:.4g} {sr.throughput_unit}, "
                f"threads={sr.threads}, dur={sr.duration_actual_sec:.2f}s\n"
            )

        writer = csv.writer(fh)
        writer.writerow(
            [
                "timestamp",
                "cpu_percent",
                "ram_percent",
                "ram_used_gb",
                "disk_read_mb",
                "disk_write_mb",
                "cpu_freq_avg",
                "temp_max",
                "throttled",
            ]
        )
        for snap in result.metrics_history:
            writer.writerow(
                [
                    snap.timestamp.isoformat(),
                    f"{snap.cpu_percent:.2f}",
                    f"{snap.ram_percent:.2f}",
                    f"{snap.ram_used_gb:.3f}",
                    f"{snap.disk_read_mb:.3f}",
                    f"{snap.disk_write_mb:.3f}",
                    f"{snap.frequencies.get('cpu_avg', 0):.0f}" if snap.frequencies else "",
                    f"{max(snap.temperatures.values()):.1f}" if snap.temperatures else "",
                    "1" if snap.cpu_throttled else "0",
                ]
            )
    return out_path
