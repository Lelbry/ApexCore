"""Тесты `interfaces/cli/sparkline.py` — unicode/ascii sparkline helper."""

from __future__ import annotations

import pytest

from apexcore.interfaces.cli.sparkline import sparkline, sparkline_with_range


@pytest.fixture(autouse=True)
def _force_unicode_sparkline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Детерминизм: все «старые» тесты ждут unicode-бары независимо от среды.

    Без этого на Windows-conhost (нет ``WT_SESSION``) auto-режим вернёт
    ASCII и проверки на конкретные символы ▁..█ упадут.
    """
    monkeypatch.setenv("APEXCORE_SPARKLINE", "unicode")


def test_empty_returns_dots() -> None:
    assert sparkline([], 5) == "·····"


def test_single_value_returns_middle_bar() -> None:
    # Один отсчёт — без диапазона, среднюю палку.
    result = sparkline([50.0], 5)
    assert len(result) == 5
    # Левые позиции — пустые (history короче окна).
    assert result.startswith("····")
    assert result[-1] in "▁▂▃▄▅▆▇█"


def test_monotonic_increasing_uses_full_range() -> None:
    """От min до max должно использовать первый и последний bar-символы."""
    result = sparkline([0.0, 25.0, 50.0, 75.0, 100.0], 5)
    assert result[0] == "▁"
    assert result[-1] == "█"


def test_flat_line_uses_one_bar_level() -> None:
    """Плоская линия — все символы одинаковы."""
    result = sparkline([50.0, 50.0, 50.0], 3)
    assert len(set(result)) == 1
    assert result[0] in "▁▂▃▄▅▆▇█"


def test_flat_nonzero_uses_middle_bar() -> None:
    """Ненулевое стабильное значение → средний блок (▅) — 'активность без изменений'."""
    result = sparkline([50.0, 50.0, 50.0], 3)
    assert result == "▅▅▅"


def test_flat_zero_uses_bottom_bar_not_middle() -> None:
    """**Регрессия v0.5.3**: при 0 RPM шкала визуально пустая, не «заполненная».

    Раньше плоская линия из нулей рисовалась средним уровнем (``▅▅▅``),
    и idle GPU fan в карточке «Вентиляторы» выглядел как работающий с
    заполненным баром. Теперь все значения == 0 → ``▁▁▁``.
    """
    result = sparkline([0.0, 0.0, 0.0], 3)
    assert result == "▁▁▁"
    # Контр-проверка: средний блок ▅ при нулях не используется.
    assert "▅" not in result


def test_width_clamps_to_recent() -> None:
    """sparkline берёт values[-width:]."""
    values = list(range(20))  # 0..19
    result = sparkline([float(v) for v in values], 5)
    assert len(result) == 5
    # Должно покрывать последние 5: 15..19
    assert result[0] == "▁"
    assert result[-1] == "█"


def test_width_zero() -> None:
    assert sparkline([1.0, 2.0], 0) == ""


def test_pads_when_history_shorter_than_width() -> None:
    """3 значения в окне 8 → 5 точек слева + 3 блока справа."""
    result = sparkline([1.0, 2.0, 3.0], 8)
    assert result.startswith("·····")
    assert len(result) == 8


def test_with_range_appends_min_max() -> None:
    result = sparkline_with_range([20.0, 50.0, 80.0], width=3)
    assert " 20..80" in result


def test_with_range_empty_returns_just_dots() -> None:
    result = sparkline_with_range([], width=3)
    assert result == "···"


def test_with_range_custom_format() -> None:
    result = sparkline_with_range(
        [20.5, 50.5, 80.5], width=3, fmt="{vmin:.1f}..{vmax:.1f}°"
    )
    assert "20.5..80.5°" in result


# ─── ASCII fallback (B.3 / sparkline_style="ascii") ────────────────────────


def test_ascii_monotonic_increasing(monkeypatch: pytest.MonkeyPatch) -> None:
    """В ASCII-режиме крайние бары — '.' (низ) и '@' (верх)."""
    monkeypatch.setenv("APEXCORE_SPARKLINE", "ascii")
    result = sparkline([0.0, 25.0, 50.0, 75.0, 100.0], 5)
    assert result == ".,-=@" or (result[0] == "." and result[-1] == "@")
    # Контр-проверка: unicode-блоков нет.
    for ch in "▁▂▃▄▅▆▇█":
        assert ch not in result


def test_ascii_flat_zero_is_bottom_bar(monkeypatch: pytest.MonkeyPatch) -> None:
    """**Регрессия v0.5.3** в ASCII-режиме: 0 RPM → '...' (нижний), не '++'."""
    monkeypatch.setenv("APEXCORE_SPARKLINE", "ascii")
    assert sparkline([0.0, 0.0, 0.0], 3) == "..."


def test_ascii_flat_nonzero_is_middle_bar(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ненулевое стабильное значение в ASCII → средний бар '+'."""
    monkeypatch.setenv("APEXCORE_SPARKLINE", "ascii")
    assert sparkline([50.0, 50.0, 50.0], 3) == "+++"


def test_ascii_empty_uses_space_pad(monkeypatch: pytest.MonkeyPatch) -> None:
    """В ASCII-режиме пустой отсчёт — пробел (точка-разделитель «·» не моноширинна)."""
    monkeypatch.setenv("APEXCORE_SPARKLINE", "ascii")
    assert sparkline([], 5) == "     "


def test_explicit_unicode_env_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """``APEXCORE_SPARKLINE=unicode`` форсирует unicode даже под Windows-conhost."""
    monkeypatch.setenv("APEXCORE_SPARKLINE", "unicode")
    result = sparkline([0.0, 100.0], 2)
    assert result == "▁█"


def test_invalid_env_falls_back_to_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    """Неузнанное значение env читается как auto — не должно крашить."""
    monkeypatch.setenv("APEXCORE_SPARKLINE", "blocks")  # не из списка
    result = sparkline([0.0, 50.0, 100.0], 3)
    # auto: либо unicode (Linux/WT_SESSION), либо ASCII (Windows-conhost) — оба валидны.
    assert len(result) == 3
