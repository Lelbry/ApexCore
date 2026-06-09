"""CLI-команда `apexcore info` — сведения о текущей системе."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apexcore.domain.models import MetricSnapshot, SystemInfo
    from apexcore.domain.ports import OSAdapter


def info() -> None:
    """Вывести сведения об ОС, CPU, RAM, GPU и состоянии датчиков.

    **Производительность.** Раньше команда занимала ~7 с из-за
    `diagnose_sensors()`, который последовательно прогоняет 7 бэкендов
    с PowerShell-сабпроцессами (~1.5 с каждый). Сейчас:

    - проверка драйвера — один вызов `adapter.get_current_metrics()`
      (адаптер сам идёт по своему оптимизированному пайплайну);
    - базовая частота читается из реестра Windows / sysfs Linux прямо
      в `get_system_info` — синхронно, без отдельного LHM-вызова;
    - два тяжёлых вызова (`get_system_info`, `get_current_metrics`)
      запускаются **параллельно** через ThreadPoolExecutor;
    - во время сбора данных пользователь видит spinner со статусом.

    Турбо/живая частота показываются в дашборде «Sensors», в карточке
    `info` — только статические значения. Полная диагностика всех
    бэкендов — в `apexcore doctor`.
    """
    from apexcore.application.diagnostics_sensors import build_capability_summary
    from apexcore.application.thermal_watchdog import _is_cpu_temp_key
    from apexcore.infrastructure.adapters import AdapterFactory
    from apexcore.interfaces.cli.render import console, render_system_info

    adapter = AdapterFactory.detect()

    # Spinner + параллельный сбор. Status сам стирается после выхода.
    with console.status(
        "[cyan]Собираю информацию о системе…[/]", spinner="dots"
    ):
        sys_info, snap = _collect_in_parallel(adapter)

    # ── Состояние драйвера термосенсоров. ─────────────────────────────────
    # Если получили snapshot и в нём есть CPU-температуры — драйвер активен.
    driver_active = False
    if snap is not None:
        driver_active = any(_is_cpu_temp_key(k) for k in snap.temperatures)

    # ── Capability-строка. После snap, чтобы side-channel ``_last_cpu_temp_source``
    # в windows-адаптере успел заполниться текущим тиком. См. план P1.1.
    capability = build_capability_summary(snap)

    # Базовая частота читается напрямую из реестра/sysfs в `get_system_info`
    # и приходит в полях `sys_info.cpu_base_mhz` / `cpu_base_p_mhz` / `cpu_base_e_mhz`.
    # Турбо/живая частота показываются в дашборде «Sensors», не здесь.
    render_system_info(
        sys_info,
        sensor_driver_active=driver_active,
        capability_summary=capability,
    )


def _collect_in_parallel(
    adapter: OSAdapter,
) -> tuple[SystemInfo, MetricSnapshot | None]:
    """Параллельно собрать SystemInfo и текущие метрики.

    Два вызова независимы и блокирующие (psutil + WMI / LHM). Запускаем в
    двух потоках. Раньше тут был ещё ``read_lhm_cpu_max_clock_mhz`` для
    турбо-частоты — удалён, теперь турбо/живая частота отображаются
    только в дашборде «Sensors», а карточка `info` показывает базовые
    значения из реестра/sysfs.
    """
    def _safe_metrics() -> MetricSnapshot | None:
        try:
            return adapter.get_current_metrics()
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=2) as ex:
        sys_info_fut = ex.submit(adapter.get_system_info)
        snap_fut = ex.submit(_safe_metrics)
        sys_info = sys_info_fut.result()
        snap = snap_fut.result()

    return sys_info, snap
