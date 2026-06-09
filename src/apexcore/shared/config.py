"""Конфигурация apexcore.

Источники (в порядке приоритета):
1. Переменные окружения с префиксом ``APEXCORE_``.
2. Файл ``apexcore.yaml`` в директории данных пользователя.
3. Значения по умолчанию.

Директория данных определяется через ``platformdirs`` и кросс-платформенно:
- Windows: ``%APPDATA%\\apexcore``
- Linux:   ``~/.local/share/apexcore``

Backward compatibility (одна релиз, удалить в v0.10.0 — см. ARCHITECTURE.md):
- ENV: переменные с префиксом ``BENCHKIT_`` (старое имя проекта) автоматически
  транслируются в ``APEXCORE_``-эквиваленты при загрузке настроек, если новое
  имя не выставлено. Один раз за процесс печатается DeprecationWarning.
- Data dir: если новая директория пустая, а старая (``~/.local/share/benchkit``
  / ``%APPDATA%\\benchkit``) существует и содержит данные — содержимое
  переносится в новую. В старой остаётся marker ``.migrated_to_apexcore``,
  чтобы избежать повторной миграции.
"""

from __future__ import annotations

import os
import shutil
import warnings
from pathlib import Path
from typing import Any

import yaml
from platformdirs import user_data_dir
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

APP_NAME = "apexcore"
LEGACY_APP_NAME = "benchkit"  # удалить вместе с миграцией в v0.10.0
LEGACY_ENV_PREFIX = "BENCHKIT_"
APEXCORE_ENV_PREFIX = "APEXCORE_"
LEGACY_MIGRATION_MARKER = ".migrated_to_apexcore"

_env_warned: bool = False
_data_dir_migrated: bool = False


def _translate_legacy_env_vars() -> None:
    """Скопировать ``BENCHKIT_*`` → ``APEXCORE_*`` в os.environ, если новые не заданы.

    Идемпотентно (повторный вызов ничего не делает). Эмитит DeprecationWarning
    один раз за процесс, если нашлась хотя бы одна старая переменная.
    """
    global _env_warned
    found_legacy: list[str] = []
    for key, value in list(os.environ.items()):
        if not key.startswith(LEGACY_ENV_PREFIX):
            continue
        new_key = APEXCORE_ENV_PREFIX + key[len(LEGACY_ENV_PREFIX):]
        if new_key in os.environ:
            continue  # пользователь явно задал новый — старый игнорируем
        os.environ[new_key] = value
        found_legacy.append(key)
    if found_legacy and not _env_warned:
        _env_warned = True
        warnings.warn(
            "Используются устаревшие ENV-переменные с префиксом BENCHKIT_ "
            f"({', '.join(sorted(found_legacy))}). Переименуйте в APEXCORE_*. "
            "Поддержка BENCHKIT_* будет удалена в v0.10.0.",
            DeprecationWarning,
            stacklevel=2,
        )


def _migrate_legacy_data_dir(new_dir: Path) -> None:
    """Перенести содержимое старой ~/.local/share/benchkit в новую apexcore-папку.

    Срабатывает только если новая директория пустая и старая существует
    с данными. Маркер в старой папке предотвращает повторную миграцию.
    Любая ошибка миграции логируется в stderr (не падаем — данные остаются
    в старой папке, пользователь увидит warning).
    """
    global _data_dir_migrated
    if _data_dir_migrated:
        return
    try:
        legacy_dir = Path(user_data_dir(LEGACY_APP_NAME, appauthor=False))
    except Exception:
        _data_dir_migrated = True
        return
    if not legacy_dir.exists() or legacy_dir == new_dir:
        _data_dir_migrated = True
        return
    if (legacy_dir / LEGACY_MIGRATION_MARKER).exists():
        _data_dir_migrated = True
        return
    # Если в новой папке уже есть свои данные — не перетираем.
    try:
        if any(new_dir.iterdir()):
            _data_dir_migrated = True
            return
    except Exception:
        _data_dir_migrated = True
        return
    try:
        for item in legacy_dir.iterdir():
            if item.name == LEGACY_MIGRATION_MARKER:
                continue
            target = new_dir / item.name
            if target.exists():
                continue
            if item.is_dir():
                shutil.copytree(item, target)
            else:
                shutil.copy2(item, target)
        try:
            (legacy_dir / LEGACY_MIGRATION_MARKER).write_text(
                f"Данные перенесены в {new_dir}. Эту папку можно удалить.\n",
                encoding="utf-8",
            )
        except Exception:
            pass
        warnings.warn(
            f"Данные перенесены из старой папки {legacy_dir} в {new_dir}. "
            "Старая папка может быть удалена вручную.",
            DeprecationWarning,
            stacklevel=2,
        )
    except Exception as exc:
        # Не падаем — данные остались в старой папке, пользователь сможет
        # перенести вручную. Печатаем без logging чтобы не зависеть от его init.
        import sys as _sys
        print(
            f"apexcore: ошибка миграции данных из {legacy_dir} в {new_dir}: {exc}",
            file=_sys.stderr,
        )
    finally:
        _data_dir_migrated = True


def default_data_dir() -> Path:
    """Папка данных пользователя для прогонов и БД.

    Создаёт папку при отсутствии и (один раз за процесс) пытается перенести
    содержимое старой ``~/.local/share/benchkit`` папки (если есть).
    """
    path = Path(user_data_dir(APP_NAME, appauthor=False))
    path.mkdir(parents=True, exist_ok=True)
    _migrate_legacy_data_dir(path)
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


# Backward-compat alias — удалить в v0.10.0.
BenchkitSettings = ApexcoreSettings


def load_settings() -> ApexcoreSettings:
    """Прочитать настройки и применить overrides из YAML, если файл существует."""
    _translate_legacy_env_vars()
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
    "BenchkitSettings",  # DEPRECATED alias
    "default_data_dir",
    "load_settings",
]
