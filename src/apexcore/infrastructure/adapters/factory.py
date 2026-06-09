"""Фабрика OS-адаптеров: автоопределение текущей платформы."""

from __future__ import annotations

import platform

from apexcore.domain.errors import AdapterUnavailableError
from apexcore.domain.ports import OSAdapter


class AdapterFactory:
    """Возвращает корректный адаптер ОС для текущего хоста."""

    @staticmethod
    def detect() -> OSAdapter:
        system = platform.system().lower()
        if system == "windows":
            from apexcore.infrastructure.adapters.windows import WindowsAdapter

            return WindowsAdapter()
        if system == "linux":
            from apexcore.infrastructure.adapters.linux import LinuxAdapter

            return LinuxAdapter()
        raise AdapterUnavailableError(
            f"Платформа '{system}' не поддерживается. Поддерживаются Windows и Linux (включая Astra Linux)."
        )
