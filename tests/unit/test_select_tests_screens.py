"""Регрессионные тесты для issue #4.

Проверяют, что:
1. ``CpuTestsScreen._run_some`` и ``RamCacheScreen._run_some`` теперь делают
   ``push(SelectMicroTestsScreen / SelectRamCacheTestsScreen)``.
2. Новые экраны принимают «1,2» напрямую через ``handle_unknown_input``
   (без промежуточного пункта меню).
3. Пустой Enter и нераспознанные токены ведут себя ожидаемо.
4. После прогона цикл накапливает результаты и перерисовывает экран,
   а пустой Enter в цикле выходит на родительский экран.
5. Базовый ``Screen.handle_unknown_input`` возвращает ``None`` — другие
   экраны не задеваются.
"""

from __future__ import annotations

from datetime import datetime

from apexcore.interfaces.cli.menu.nav import NavAction, Screen
from apexcore.interfaces.cli.menu.screens import (
    CpuTestsScreen,
    RamCacheScreen,
    SelectMicroTestsScreen,
    SelectRamCacheTestsScreen,
)


def _patch_micro_registry(monkeypatch, names: list[str]) -> None:
    class _FakeTest:
        def __init__(self, name: str) -> None:
            self.name = name

    monkeypatch.setattr(
        "apexcore.infrastructure.microbench.build_default_microbench_registry",
        lambda: [_FakeTest(n) for n in names],
    )


# ─── push на экран выбора (#4) ──────────────────────────────────────────────


def test_cpu_run_some_pushes_select_micro_screen():
    parent = CpuTestsScreen()
    res = parent._run_some()
    assert res.action == NavAction.PUSH
    assert isinstance(res.next_screen, SelectMicroTestsScreen)
    assert res.next_screen._parent is parent


def test_ram_run_some_pushes_select_ram_cache_screen():
    parent = RamCacheScreen()
    res = parent._run_some()
    assert res.action == NavAction.PUSH
    assert isinstance(res.next_screen, SelectRamCacheTestsScreen)
    assert res.next_screen._parent is parent


# ─── _parse_range_token ─────────────────────────────────────────────────────


def test_parse_range_token_basic():
    from apexcore.interfaces.cli.menu.screens import _parse_range_token

    assert _parse_range_token("1-3", 5) == [0, 1, 2]
    assert _parse_range_token("3-1", 5) == [0, 1, 2]  # инвертированный
    assert _parse_range_token("4-7", 12) == [3, 4, 5, 6]


def test_parse_range_token_clamps_to_count():
    from apexcore.interfaces.cli.menu.screens import _parse_range_token

    # 1..100, но всего 5 номеров — обрезаем по верхней границе.
    assert _parse_range_token("1-100", 5) == [0, 1, 2, 3, 4]
    # Нулевой номер игнорируется (нумерация 1-based).
    assert _parse_range_token("0-2", 5) == [0, 1]


def test_parse_range_token_rejects_non_range():
    from apexcore.interfaces.cli.menu.screens import _parse_range_token

    assert _parse_range_token("1", 5) is None  # одно число
    assert _parse_range_token("dram-read", 5) is None  # имя
    assert _parse_range_token("1-2-3", 5) is None  # три части
    assert _parse_range_token("a-b", 5) is None  # не цифры
    assert _parse_range_token("1-", 5) is None
    assert _parse_range_token("-3", 5) is None


def test_select_micro_parse_input_accepts_range(monkeypatch):
    parent = CpuTestsScreen()
    screen = SelectMicroTestsScreen(parent=parent)
    names = ["memory_read", "memory_write", "memory_copy", "flops_sp", "flops_dp"]

    assert screen._parse_input("1-3", names) == {"memory_read", "memory_write", "memory_copy"}
    assert screen._parse_input("1-2,5", names) == {"memory_read", "memory_write", "flops_dp"}
    assert screen._parse_input("4-100", names) == {"flops_sp", "flops_dp"}


def test_select_ram_parse_input_accepts_range(monkeypatch):
    parent = RamCacheScreen()
    screen = SelectRamCacheTestsScreen(parent=parent)
    names = ["dram_read", "dram_write", "dram_copy", "dram_latency"]
    monkeypatch.setattr(
        "apexcore.application.ram_cache_service.parse_test_name",
        lambda name: ("dram", name.split("_", 1)[1]) if name in names else None,
    )

    wanted, unknown = screen._parse_input("1-3", names)
    assert unknown == []
    assert wanted == {("dram", "read"), ("dram", "write"), ("dram", "copy")}


# ─── handle_unknown_input на SelectMicroTestsScreen ─────────────────────────


def test_select_micro_handle_empty_input_goes_back(monkeypatch):
    parent = CpuTestsScreen()
    screen = SelectMicroTestsScreen(parent=parent)
    _patch_micro_registry(monkeypatch, ["memory_read"])

    res = screen.handle_unknown_input("")
    assert res is not None
    assert res.action == NavAction.BACK


def test_select_micro_handle_garbage_stays_with_flash(monkeypatch):
    parent = CpuTestsScreen()
    screen = SelectMicroTestsScreen(parent=parent)
    _patch_micro_registry(monkeypatch, ["memory_read"])

    res = screen.handle_unknown_input("abc")
    assert res is not None
    assert res.action == NavAction.STAY
    assert res.flash is not None
    assert "Не распознано" in res.flash


def _fake_micro_suite(names_in_order: list[str], threads: int = 0):
    from apexcore.domain.models import (
        CpuCores,
        MicroBenchResult,
        MicroBenchSuiteResult,
        SystemInfo,
    )

    now = datetime(2026, 5, 9, 12, 0, 0)
    sys_info = SystemInfo(
        cpu_model="FakeCPU",
        cpu_cores=CpuCores(physical=4, logical=8),
        os_name="windows",
        os_version="11",
        ram_total_gb=16.0,
        timestamp=now,
    )
    results = [
        MicroBenchResult(
            name=n,
            category="memory",
            value=1000.0,
            unit="MB/s",
            duration_actual_sec=1.0,
            iterations=10,
            threads=threads or 1,
        )
        for n in names_in_order
    ]
    return MicroBenchSuiteResult(
        system_info=sys_info,
        results=results,
        start_time=now,
        end_time=now,
        duration_sec_per_test=1.0,
        threads=threads,
    )


def test_select_micro_loop_accumulates_and_back_on_empty(monkeypatch):
    """Сценарий: ввести «1,2» → цикл прогон+аккумулирование → пустой Enter → back()."""
    parent = CpuTestsScreen()
    screen = SelectMicroTestsScreen(parent=parent)

    names = ["memory_read", "memory_write", "flops_sp", "flops_dp"]
    _patch_micro_registry(monkeypatch, names)

    captured: list[set[str]] = []

    def fake_run_pass(selected):
        captured.append(set(selected) if selected is not None else set())
        # Фейковый Suite только с теми тестами, которые "запросили".
        return _fake_micro_suite([n for n in names if n in selected])

    monkeypatch.setattr(parent, "_run_pass", fake_run_pass)
    monkeypatch.setattr(screen, "_redraw_screen", lambda: None)
    monkeypatch.setattr(
        "apexcore.interfaces.cli.menu.screens.render_microbench_suite",
        lambda *a, **kw: None,
        raising=False,
    )
    # render_microbench_suite импортируется внутри _run_loop, поэтому мокаем
    # на уровне исходного модуля.
    monkeypatch.setattr(
        "apexcore.interfaces.cli.render.render_microbench_suite",
        lambda *a, **kw: None,
    )
    # Prompt.ask: пустая строка → выход.
    monkeypatch.setattr(
        "apexcore.interfaces.cli.menu.screens.Prompt.ask",
        lambda *a, **kw: "",
    )

    res = screen.handle_unknown_input("1,2")
    assert res is not None
    assert res.action == NavAction.BACK
    assert captured == [{"memory_read", "memory_write"}]
    # Накопленное состояние сохранилось на экране.
    assert set(screen._accumulated.keys()) == {"memory_read", "memory_write"}


def test_select_micro_loop_runs_then_takes_more_then_exits(monkeypatch):
    """Сценарий: «1,2» → прогон → ввести «3» → ещё прогон → пустой → back()."""
    parent = CpuTestsScreen()
    screen = SelectMicroTestsScreen(parent=parent)

    names = ["memory_read", "memory_write", "flops_sp", "flops_dp"]
    _patch_micro_registry(monkeypatch, names)

    captured: list[set[str]] = []

    def fake_run_pass(selected):
        captured.append(set(selected) if selected is not None else set())
        return _fake_micro_suite([n for n in names if n in selected])

    monkeypatch.setattr(parent, "_run_pass", fake_run_pass)
    monkeypatch.setattr(screen, "_redraw_screen", lambda: None)
    monkeypatch.setattr(
        "apexcore.interfaces.cli.render.render_microbench_suite",
        lambda *a, **kw: None,
    )

    inputs = iter(["3", ""])
    monkeypatch.setattr(
        "apexcore.interfaces.cli.menu.screens.Prompt.ask",
        lambda *a, **kw: next(inputs),
    )

    res = screen.handle_unknown_input("1,2")
    assert res is not None
    assert res.action == NavAction.BACK
    assert captured == [{"memory_read", "memory_write"}, {"flops_sp"}]
    assert set(screen._accumulated.keys()) == {
        "memory_read",
        "memory_write",
        "flops_sp",
    }


# ─── handle_unknown_input на SelectRamCacheTestsScreen ──────────────────────


def test_select_ram_handle_unknown_token_returns_flash(monkeypatch):
    parent = RamCacheScreen()
    screen = SelectRamCacheTestsScreen(parent=parent)
    monkeypatch.setattr(
        "apexcore.application.ram_cache_service.all_test_names",
        lambda: ["dram_read"],
    )
    monkeypatch.setattr(
        "apexcore.application.ram_cache_service.parse_test_name",
        lambda name: ("dram", "read") if name == "dram_read" else None,
    )

    res = screen.handle_unknown_input("garbage")
    assert res is not None
    assert res.action == NavAction.STAY
    assert res.flash is not None
    assert "Не распознан" in res.flash


def test_select_ram_handle_empty_input_goes_back(monkeypatch):
    parent = RamCacheScreen()
    screen = SelectRamCacheTestsScreen(parent=parent)
    monkeypatch.setattr(
        "apexcore.application.ram_cache_service.all_test_names",
        lambda: ["dram_read"],
    )

    res = screen.handle_unknown_input("")
    assert res is not None
    assert res.action == NavAction.BACK


# ─── базовый Screen — обратная совместимость ────────────────────────────────


def test_default_screen_handle_unknown_input_returns_none():
    s = Screen()
    assert s.handle_unknown_input("any") is None
    assert s.handle_unknown_input("") is None
