"""Тесты `infrastructure/cpu_affinity.py` — pinned_to_cpus context manager."""

from __future__ import annotations

import os
import platform
from unittest.mock import MagicMock

import pytest

from apexcore.infrastructure import cpu_affinity
from apexcore.infrastructure.cpu_affinity import (
    is_supported,
    pinned_to_cpus,
)

# ─────────────────────────── empty input ────────────────────────────


def test_pinned_to_cpus_empty_list_yields_false():
    """Пустой список CPU — сразу yield False, никакого syscall."""
    with pinned_to_cpus([]) as applied:
        assert applied is False


# ─────────────────────────── Linux ────────────────────────────


@pytest.mark.skipif(not hasattr(os, "sched_setaffinity"), reason="sched_*affinity отсутствует")
def test_pinned_to_cpus_linux_calls_sched_setaffinity(monkeypatch):
    """На Linux вызывается sched_setaffinity + восстановление в finally."""
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    calls: list[tuple[int, frozenset[int]]] = []

    def fake_get(pid):
        return {0, 1, 2, 3}

    def fake_set(pid, mask):
        calls.append((pid, frozenset(mask)))

    monkeypatch.setattr(os, "sched_getaffinity", fake_get)
    monkeypatch.setattr(os, "sched_setaffinity", fake_set)

    with pinned_to_cpus([2]) as applied:
        assert applied is True

    # Первый вызов — установка {2}, второй — восстановление {0,1,2,3}.
    assert calls[0] == (0, frozenset({2}))
    assert calls[1] == (0, frozenset({0, 1, 2, 3}))


def test_pinned_to_cpus_linux_oserror_returns_false(monkeypatch):
    """Симулируем Linux и OSError при sched_setaffinity (EPERM)."""
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    # raising=False — sched_*affinity отсутствует на Windows;
    # monkeypatch.setattr без этого флага падает с AttributeError.
    monkeypatch.setattr(os, "sched_getaffinity", lambda pid: {0, 1}, raising=False)

    def boom(pid, mask):
        raise OSError("EPERM")

    monkeypatch.setattr(os, "sched_setaffinity", boom, raising=False)
    with pinned_to_cpus([0]) as applied:
        assert applied is False


# ─────────────────────────── Windows ────────────────────────────


def test_pinned_to_cpus_windows_calls_set_thread_affinity_mask(monkeypatch):
    """На Windows вызывается SetThreadAffinityMask + восстановление."""
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    # Соберём фейковый ctypes/windll. _pin_windows импортирует ctypes
    # лениво — патчим напрямую перед вызовом.
    fake_handle = MagicMock(name="ThreadHandle")
    fake_set = MagicMock(side_effect=[0xFFFF, 0xABCD])  # prev, restore-return
    fake_kernel32 = MagicMock()
    fake_kernel32.GetCurrentThread.return_value = fake_handle
    fake_kernel32.SetThreadAffinityMask = fake_set
    fake_windll = MagicMock()
    fake_windll.kernel32 = fake_kernel32

    fake_ctypes = MagicMock()
    fake_ctypes.windll = fake_windll
    fake_ctypes.c_size_t = int

    monkeypatch.setattr(cpu_affinity, "_pin_windows", _make_pin_windows(fake_set, fake_handle))

    with pinned_to_cpus([0, 1]) as applied:
        assert applied is True

    # Первый вызов set должен быть с маской bit0+bit1 = 0b11 = 3.
    assert fake_set.call_count == 2
    _, mask_arg = fake_set.call_args_list[0].args
    assert mask_arg == 0b11
    # Восстановление — с предыдущей маской 0xFFFF.
    restore_args = fake_set.call_args_list[1].args
    assert restore_args[1] == 0xFFFF


def _make_pin_windows(fake_set, fake_handle):
    """Маленькая обёртка, имитирующая нашу _pin_windows без реального ctypes."""

    def fake_pin(cpu_ids):
        mask = 0
        for cpu in cpu_ids:
            mask |= 1 << cpu
        prev = fake_set(fake_handle, mask)
        if prev == 0:
            return False, lambda: None

        def restore():
            fake_set(fake_handle, prev)

        return True, restore

    return fake_pin


def test_pinned_to_cpus_windows_zero_return_means_failed(monkeypatch):
    """Если SetThreadAffinityMask вернёт 0 — applied=False, restore=no-op."""
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    fake_set = MagicMock(return_value=0)

    def fake_pin(cpu_ids):
        prev = fake_set(None, 0b1)
        if prev == 0:
            return False, lambda: None
        return True, lambda: None

    monkeypatch.setattr(cpu_affinity, "_pin_windows", fake_pin)

    with pinned_to_cpus([0]) as applied:
        assert applied is False


# ─────────────────────────── unsupported OS ────────────────────────────


def test_pinned_to_cpus_macos_is_noop(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    with pinned_to_cpus([0, 1, 2]) as applied:
        assert applied is False


def test_pinned_to_cpus_yields_even_if_restore_raises(monkeypatch):
    """Если restore в finally падает — не должно сорвать context manager."""
    monkeypatch.setattr(platform, "system", lambda: "Linux")

    def fake_pin(cpu_ids):
        def bad_restore():
            raise RuntimeError("simulated restore failure")

        return True, bad_restore

    monkeypatch.setattr(cpu_affinity, "_pin_linux", fake_pin)
    with pinned_to_cpus([0]) as applied:
        assert applied is True
    # Если бы пробросило — этот ассерт не выполнился бы.


# ─────────────────────────── is_supported ────────────────────────────


def test_is_supported_on_linux(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    # На рабочей Linux-системе sched_setaffinity есть.
    expected = hasattr(os, "sched_setaffinity")
    assert is_supported() == expected


def test_is_supported_on_darwin(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    assert is_supported() is False
