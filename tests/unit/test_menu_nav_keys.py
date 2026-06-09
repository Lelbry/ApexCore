"""Юнит-тесты глобальных ключей навигации меню.

Проверяет, что:
1. Однобуквенные шорткаты доступны и в EN-, и в RU-раскладке (на тех же
   физических клавишах: B = И, H = Р, Q = Й).
2. Полные русские слова (``назад``, ``главная`` и т.п.) тоже принимаются.
3. y/n-подтверждения корректно различают «да/нет» по семантике, а не по
   физическому совпадению клавиш (русская «н» — это «нет», не «yes»).
4. Между наборами ключей нет конфликтов (одна и та же буква не означает
   одновременно «назад» и «выход» и т.п.).
"""

from __future__ import annotations

from apexcore.interfaces.cli.menu.nav import (
    BACK_KEYS,
    CONFIRM_NO_KEYS,
    CONFIRM_YES_KEYS,
    HELP_KEYS,
    HOME_KEYS,
    QUIT_KEYS,
    _confirm,
)

# ─── BACK ────────────────────────────────────────────────────────────────────


def test_back_keys_en_and_ru_layout():
    assert "b" in BACK_KEYS  # EN
    assert "и" in BACK_KEYS  # RU layout: на клавише B
    assert "back" in BACK_KEYS
    assert "назад" in BACK_KEYS
    assert "0" in BACK_KEYS  # альтернатива для NumPad


# ─── HOME ────────────────────────────────────────────────────────────────────


def test_home_keys_en_and_ru_layout():
    assert "h" in HOME_KEYS  # EN
    assert "р" in HOME_KEYS  # RU layout: на клавише H
    assert "home" in HOME_KEYS
    assert "главная" in HOME_KEYS
    assert "домой" in HOME_KEYS


# ─── QUIT ────────────────────────────────────────────────────────────────────


def test_quit_keys_en_and_ru_layout():
    assert "q" in QUIT_KEYS  # EN
    assert "й" in QUIT_KEYS  # RU layout: на клавише Q
    assert "quit" in QUIT_KEYS
    assert "exit" in QUIT_KEYS
    assert "выход" in QUIT_KEYS


# ─── HELP ────────────────────────────────────────────────────────────────────


def test_help_keys():
    assert "?" in HELP_KEYS  # символ — одинаков в обеих раскладках
    assert "help" in HELP_KEYS
    assert "помощь" in HELP_KEYS


# ─── y/n confirm ─────────────────────────────────────────────────────────────


def test_confirm_yes_keys_semantic_only():
    assert "y" in CONFIRM_YES_KEYS
    assert "yes" in CONFIRM_YES_KEYS
    assert "д" in CONFIRM_YES_KEYS  # «да»
    assert "да" in CONFIRM_YES_KEYS
    # ВАЖНО: «н» (физически на клавише Y) НЕ должно быть в YES — это «нет».
    assert "н" not in CONFIRM_YES_KEYS


def test_confirm_no_keys_semantic_only():
    assert "n" in CONFIRM_NO_KEYS
    assert "no" in CONFIRM_NO_KEYS
    assert "н" in CONFIRM_NO_KEYS  # «нет»
    assert "нет" in CONFIRM_NO_KEYS
    # «д» (= «да») не должно случайно попасть в NO.
    assert "д" not in CONFIRM_NO_KEYS


# ─── No-conflict invariants ──────────────────────────────────────────────────


def test_navigation_key_sets_disjoint():
    """Никакая буква/слово не означает одновременно две разные команды."""
    pairs = [
        ("BACK", BACK_KEYS),
        ("HOME", HOME_KEYS),
        ("QUIT", QUIT_KEYS),
        ("HELP", HELP_KEYS),
    ]
    for i, (name_a, set_a) in enumerate(pairs):
        for name_b, set_b in pairs[i + 1 :]:
            common = set_a & set_b
            assert not common, (
                f"Конфликт между {name_a} и {name_b}: {common}"
            )


def test_yes_no_disjoint():
    common = CONFIRM_YES_KEYS & CONFIRM_NO_KEYS
    assert not common, f"yes/no overlap: {common}"


# ─── _confirm() ──────────────────────────────────────────────────────────────


def test_confirm_accepts_yes_variants(monkeypatch):
    for ans in ("y", "Y", "yes", "YES", "д", "да", "  да  "):
        monkeypatch.setattr(
            "apexcore.interfaces.cli.menu.nav.Prompt.ask",
            lambda *a, **kw: ans,
        )
        assert _confirm("?") is True, f"должно быть True для {ans!r}"


def test_confirm_rejects_other_inputs(monkeypatch):
    for ans in ("n", "no", "н", "нет", "", "что-то ещё", "abc"):
        monkeypatch.setattr(
            "apexcore.interfaces.cli.menu.nav.Prompt.ask",
            lambda *a, **kw: ans,
        )
        assert _confirm("?") is False, f"должно быть False для {ans!r}"


# ─── Issue #13: пустой Enter не показывает «Неизвестный пункт» ──────────────


def test_empty_input_does_not_set_unknown_flash(monkeypatch, capsys):
    """Пустой ввод (Enter без символов) — просто перерисовать экран, без flash.

    Issue #13 фиксировал случай, когда пользователь нажимал Enter без ввода
    и получал предупреждение «Неизвестный пункт». На практике это вылазило
    на любом экране при двойном Enter. Фикс: на верхнем уровне ``run()`` пустой
    ``choice`` пропускается без flash.
    """
    from apexcore.interfaces.cli.menu.nav import MenuLoop, Screen

    class _Stub(Screen):
        title = "stub"

        def items(self):
            return []

    inputs = iter(["", "q"])  # сначала пустой Enter, потом выход
    monkeypatch.setattr(
        "apexcore.interfaces.cli.menu.nav.Prompt.ask",
        lambda *a, **kw: next(inputs),
    )
    loop = MenuLoop(_Stub())
    loop.run()
    captured = capsys.readouterr()
    # Главная проверка: после пустого Enter не появилось предупреждение.
    assert "Неизвестный пункт" not in captured.out


def test_unknown_input_still_flashes(monkeypatch, capsys):
    """Несуществующий пункт (не пустой) — flash остаётся."""
    from apexcore.interfaces.cli.menu.nav import MenuLoop, Screen

    class _Stub(Screen):
        title = "stub"

        def items(self):
            return []

    inputs = iter(["zzz", "q"])
    monkeypatch.setattr(
        "apexcore.interfaces.cli.menu.nav.Prompt.ask",
        lambda *a, **kw: next(inputs),
    )
    loop = MenuLoop(_Stub())
    loop.run()
    captured = capsys.readouterr()
    assert "Неизвестный пункт" in captured.out
