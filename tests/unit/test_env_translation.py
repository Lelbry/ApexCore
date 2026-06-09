"""Тесты backward-compat трансляции ENV-переменных BENCHKIT_* → APEXCORE_*.

Контракт (shared/config.py:_translate_legacy_env_vars):
- BENCHKIT_X в окружении + APEXCORE_X не задан → APEXCORE_X = BENCHKIT_X
- если оба заданы → APEXCORE_X побеждает (явный приоритет)
- DeprecationWarning один раз за процесс (если найдены legacy-переменные)
- если BENCHKIT_X не задан → ничего не делаем
"""

from __future__ import annotations

import warnings

import pytest


@pytest.fixture(autouse=True)
def reset_warn_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    from apexcore.shared import config
    monkeypatch.setattr(config, "_env_warned", False, raising=False)


def test_no_legacy_env_no_translation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Нет BENCHKIT_X в env → APEXCORE_X не появляется."""
    monkeypatch.delenv("BENCHKIT_FOO", raising=False)
    monkeypatch.delenv("APEXCORE_FOO", raising=False)
    from apexcore.shared.config import _translate_legacy_env_vars
    _translate_legacy_env_vars()
    import os
    assert "APEXCORE_FOO" not in os.environ


def test_legacy_env_translates_to_new(monkeypatch: pytest.MonkeyPatch) -> None:
    """BENCHKIT_X задан, APEXCORE_X — нет → APEXCORE_X появляется из BENCHKIT_X."""
    monkeypatch.setenv("BENCHKIT_FOOBAR", "value123")
    monkeypatch.delenv("APEXCORE_FOOBAR", raising=False)
    from apexcore.shared.config import _translate_legacy_env_vars
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        _translate_legacy_env_vars()
    import os
    assert os.environ.get("APEXCORE_FOOBAR") == "value123"


def test_explicit_new_env_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Если задан APEXCORE_X — не перезаписываем (явный приоритет)."""
    monkeypatch.setenv("BENCHKIT_FOOBAR", "old")
    monkeypatch.setenv("APEXCORE_FOOBAR", "new")
    from apexcore.shared.config import _translate_legacy_env_vars
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        _translate_legacy_env_vars()
    import os
    assert os.environ["APEXCORE_FOOBAR"] == "new"


def test_warning_emitted_once_for_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Хотя бы одна BENCHKIT_X переменная → DeprecationWarning."""
    monkeypatch.setenv("BENCHKIT_SOMETHING", "x")
    monkeypatch.delenv("APEXCORE_SOMETHING", raising=False)
    from apexcore.shared.config import _translate_legacy_env_vars
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _translate_legacy_env_vars()
    deprecation_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert len(deprecation_warnings) == 1
    assert "BENCHKIT_" in str(deprecation_warnings[0].message)
