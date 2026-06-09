"""Типизированные исключения предметной области apexcore."""

from __future__ import annotations


class BenchkitError(Exception):
    """Базовое исключение apexcore."""


class AdapterUnavailableError(BenchkitError):
    """Подходящий OS-адаптер не найден или не инициализирован."""


class StressEngineUnavailableError(BenchkitError):
    """Запрошенный стресс-движок недоступен в текущей среде."""


class RepositoryError(BenchkitError):
    """Ошибки слоя хранения (репозитория)."""


class ConfigurationError(BenchkitError):
    """Ошибка конфигурации (неправильный профиль, неподдерживаемый параметр)."""


class TelemetryError(BenchkitError):
    """Сбой подсистемы сбора телеметрии."""
