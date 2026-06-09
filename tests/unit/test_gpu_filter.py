"""Тесты `infrastructure/gpu_filter.py` — пометка виртуальных GPU."""

from __future__ import annotations

import pytest

from apexcore.infrastructure.gpu_filter import annotate_virtual, is_virtual

# ─────────────────────────── viртуальные адаптеры ────────────────────────────


@pytest.mark.parametrize(
    "name",
    [
        # VR-стримеры / удалённый рабочий стол
        "Virtual Desktop Monitor",
        "Microsoft Basic Display Adapter",
        "Microsoft Remote Display Adapter",
        "Microsoft Hyper-V Video",
        "Microsoft Hyper-V Virtual Display Adapter",
        "Citrix Indirect Display Adapter",
        "Citrix Display Only Adapter",
        "Parsec Virtual Display Adapter",
        "Steam Streaming Display",
        "Moonlight Virtual Display",
        "AnyDesk Virtual Monitor",
        "DameWare Mirror Driver",
        "TeamViewer Virtual Display",
        "TeamViewer Mirror Driver",
        # Гипервизоры
        "VirtualBox Graphics Adapter",
        "VMware SVGA 3D",
        "QEMU Standard VGA",
        # Прочее ПО удалённого доступа
        "Splashtop Display Adapter",
        "NoMachine Display Adapter",
    ],
)
def test_annotate_virtual_marks_known_virtuals(name):
    assert annotate_virtual(name) == f"{name} (виртуальный)"
    assert is_virtual(name) is True


@pytest.mark.parametrize(
    "name",
    [
        # Топовые дискретки
        "NVIDIA GeForce RTX 4070 Ti",
        "NVIDIA GeForce RTX 5090",
        "AMD Radeon RX 7900 XTX",
        "AMD Radeon Pro W7800",
        # Интегрированные
        "Intel(R) UHD Graphics 770",
        "Intel(R) Iris Xe Graphics",
        "AMD Radeon(TM) Graphics",
        # Старые
        "NVIDIA GeForce GTX 1080 Ti",
        # Linux lspci-style
        "NVIDIA Corporation AD104 [GeForce RTX 4070 Ti]",
        "Intel Corporation AlderLake-S GT1 [UHD Graphics 770]",
        "Advanced Micro Devices, Inc. [AMD/ATI] Navi 31 [Radeon RX 7900 XTX]",
    ],
)
def test_annotate_virtual_leaves_real_gpus_unchanged(name):
    assert annotate_virtual(name) == name
    assert is_virtual(name) is False


# ─────────────────────────── edge cases ────────────────────────────


def test_annotate_virtual_empty_string_returns_empty():
    assert annotate_virtual("") == ""
    assert is_virtual("") is False


def test_annotate_virtual_case_insensitive():
    """Паттерны должны работать вне зависимости от регистра."""
    assert annotate_virtual("virtual desktop monitor") == "virtual desktop monitor (виртуальный)"
    assert annotate_virtual("CITRIX INDIRECT DISPLAY ADAPTER") == (
        "CITRIX INDIRECT DISPLAY ADAPTER (виртуальный)"
    )


def test_annotate_virtual_does_not_double_mark():
    """Если строка уже помечена — не вешаем суффикс ещё раз."""
    once = annotate_virtual("Virtual Desktop Monitor")
    assert annotate_virtual(once) == once


def test_annotate_virtual_with_extra_text():
    """Виртуальное имя в составе более длинной строки тоже распознаётся."""
    name = "(MSI) Microsoft Basic Display Adapter (driver v.1.0)"
    assert is_virtual(name) is True
    assert annotate_virtual(name).endswith("(виртуальный)")


def test_annotate_virtual_avoids_false_positive_on_virtual_word_inside():
    """Имя, где 'virtual' стоит не в начале — не должно считаться виртуальным.

    Гипотетический случай: «NVIDIA Virtual GPU Manager» — пока не существует
    как видеокарта, но защитимся от ложного срабатывания посередине слова.
    Паттерн '^virtual\\s' требует начала строки.
    """
    assert is_virtual("NVIDIA NVENC Virtual Camera Helper") is False
    assert annotate_virtual("NVIDIA NVENC Virtual Camera Helper") == (
        "NVIDIA NVENC Virtual Camera Helper"
    )
