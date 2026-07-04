"""Конфигурация apexcore.

Источники (в порядке приоритета):
1. Переменные окружения с префиксом ``APEXCORE_``.
2. Файл ``apexcore.yaml`` в директории данных пользователя.
3. Значения по умолчанию.

Директория данных определяется через ``platformdirs`` и кросс-платформенно:
- Windows: ``%APPDATA%\\apexcore``
- Linux:   ``~/.local/share/apexcore``
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from platformdirs import user_data_dir
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

APP_NAME = "apexcore"
APEXCORE_ENV_PREFIX = "APEXCORE_"


def default_data_dir() -> Path:
    """Папка данных пользователя для прогонов и БД.

    Создаёт папку при отсутствии.
    """
    path = Path(user_data_dir(APP_NAME, appauthor=False))
    path.mkdir(parents=True, exist_ok=True)
    return path


class ApexcoreSettings(BaseSettings):
    """Глобальные настройки apexcore."""

    model_config = SettingsConfigDict(
        env_prefix=APEXCORE_ENV_PREFIX,
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    data_dir: Path = Field(default_factory=default_data_dir, description="Папка данных и БД.")
    db_path: Path | None = Field(
        default=None,
        description="Путь к SQLite БД. Если None — формируется как data_dir/apexcore.sqlite3.",
    )
    sampling_rate_sec: float = Field(default=0.5, description="Интервал семплера телеметрии, с.")
    log_level: str = Field(default="INFO", description="Уровень логирования.")
    profiles_path: Path | None = Field(
        default=None,
        description="Путь к YAML с пользовательскими профилями нагрузки. По умолчанию data_dir/profiles.yaml.",
    )

    def ensure_paths(self) -> ApexcoreSettings:
        """Гарантировать существование папок и заполнить вычислимые пути."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if self.db_path is None:
            self.db_path = self.data_dir / "apexcore.sqlite3"
        if self.profiles_path is None:
            self.profiles_path = self.data_dir / "profiles.yaml"
        return self


def load_settings() -> ApexcoreSettings:
    """Прочитать настройки и применить overrides из YAML, если файл существует."""
    settings = ApexcoreSettings().ensure_paths()
    yaml_path = settings.data_dir / "apexcore.yaml"
    if yaml_path.exists():
        try:
            with yaml_path.open("r", encoding="utf-8") as fh:
                overrides: dict[str, Any] = yaml.safe_load(fh) or {}
            settings = ApexcoreSettings(**{**settings.model_dump(), **overrides}).ensure_paths()
        except Exception:
            # Молча игнорируем некорректный YAML; настройки уже валидны по умолчанию.
            pass
    return settings


__all__ = [
    "APP_NAME",
    "ApexcoreSettings",
    "default_data_dir",
    "load_settings",
]
