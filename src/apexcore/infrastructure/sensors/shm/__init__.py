"""Чтение Shared Memory чужих утилит мониторинга железа.

Индустриальный приём: HWiNFO/CoreTemp/AIDA64 уже запущены под admin
с подписанным kernel-driver, и **публично документируют** свой формат
shared-memory (см. ``docs/research`` §3). Чтение SHM = вызов OS-API
``OpenFileMapping``, а не «использование SDK» — лицензионно чисто и
не делает apexcore derivative-work.

Этот подмодуль — первый приоритет в fallback-chain ``windows.py::
_read_sensors`` (см. план §4): он покрывает enthusiast-аудиторию,
которая держит HWiNFO/CoreTemp/AIDA64 в трее, **без admin** для самого
apexcore и **без зависимости** от WinRing0 / HVCI / Smart App Control.

Контракт всех функций:

- возвращают ``dict[str, float]`` с apexcore-ключами (``cpu/package``,
  ``cpu/core_0``, ``gpu/temperature`` и т.д.) — это совместимо с
  ``lhm.read_lhm_temperatures()`` и понимается ``thermal_watchdog._is_cpu_temp_key``;
- при недоступности SHM или любых ошибках чтения — пустой словарь,
  без исключений наружу (graceful degrade).

См. также:

- ``probe.probe_shm_available()`` — проверка наличия SHM-объектов до
  попытки чтения;
- ``docs/research`` §3.1 (HWiNFO), §3.2 (AIDA64 — в P1), §3.3 (CoreTemp).
"""

from __future__ import annotations

from apexcore.infrastructure.sensors.shm.aida64 import read_aida64_sensors
from apexcore.infrastructure.sensors.shm.coretemp import read_coretemp_sensors
from apexcore.infrastructure.sensors.shm.hwinfo import read_hwinfo_sensors

__all__ = [
    "read_aida64_sensors",
    "read_coretemp_sensors",
    "read_hwinfo_sensors",
]
