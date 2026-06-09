"""Locate system utilities considering Debian /usr/sbin convention.

На Debian-системах (включая Astra Linux SE) команды для администрирования
(`smartctl`, `dmidecode`, `setcap`, `sensors`, `sensors-detect`, `lspci`)
живут в `/usr/sbin/`, который **не входит в PATH** обычного пользователя
по умолчанию. На Astra SE это усиливается PARSEC security policy.

`shutil.which("smartctl")` для не-root юзера вернёт `None`, даже если
сам пакет smartmontools установлен и `/usr/sbin/smartctl` существует
(`ls -l` его видит, `which` — нет).

`which_with_sbin(name)` — это `shutil.which` + дополнительный обход
системных sbin-каталогов как fallback. Работает прозрачно: если в
обычном PATH нашлось — возвращает оттуда; если нет — проверяет
типичные sbin-локации.

Не требует sudo для чтения файлов из /usr/sbin (sbin блокирован только
по PATH, не по permissions; обычный пользователь может **исполнить**
большинство утилит, просто не может их найти через `which`).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

# Системные sbin-каталоги Debian/Astra/RHEL. Проверяем в этом порядке.
_SBIN_DIRS = (
    "/usr/sbin",
    "/sbin",
    "/usr/local/sbin",
)


def which_with_sbin(name: str) -> str | None:
    """`shutil.which` + sbin fallback.

    >>> which_with_sbin("smartctl")  # на Astra без root, smartmontools установлен
    '/usr/sbin/smartctl'
    >>> which_with_sbin("not-a-real-tool")
    None
    """
    if not name:
        return None
    # Сначала стандартный PATH-lookup
    found = shutil.which(name)
    if found:
        return found
    # Fallback: sbin-каталоги. Не используем os.access(X_OK) поскольку
    # на Astra SE могут быть mandatory access labels — оперируем по
    # наличию файла, а реальный exec проверится при subprocess.run.
    for sbin in _SBIN_DIRS:
        candidate = Path(sbin) / name
        if candidate.is_file():
            return str(candidate)
    return None


def has_sbin(name: str) -> bool:
    """Удобная обёртка: True если утилита найдена (включая в sbin)."""
    return which_with_sbin(name) is not None


__all__ = ["which_with_sbin", "has_sbin"]
