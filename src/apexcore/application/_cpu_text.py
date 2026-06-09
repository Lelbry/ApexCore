"""Утилиты нормализации текстовых полей CPU.

Один модуль на проект, чтобы roofline.py и cpu_ranking.py использовали
одинаковую нормализацию ``cpu_model``. Без него два потребителя расходятся
со временем, и матчинг по подстрокам начинает давать разные результаты.
"""

from __future__ import annotations

import re


def normalize_cpu_model(cpu_model: str) -> str:
    """Lowercase + убрать (R)/(TM) + схлопнуть пробелы.

    Пример: ``"Intel(R) Core(TM) i9-12900K CPU @ 3.20GHz"`` →
    ``"intel core i9-12900k cpu @ 3.20ghz"``.
    """
    model = re.sub(r"\((r|tm)\)", "", cpu_model.lower())
    return re.sub(r"\s+", " ", model).strip()
