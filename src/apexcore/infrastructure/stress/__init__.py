"""Стресс-движки: собственные (numpy/numba) и внешние (stress-ng, prime95)."""

from apexcore.infrastructure.stress.registry import StressRegistry, build_default_registry

__all__ = ["StressRegistry", "build_default_registry"]
