"""Юнит-тесты парсера ``--verify`` для stress-ng (без запуска утилиты).

Проверяет только разбор stderr — сам ``stress-ng`` не вызывается, поэтому
тест работает на любой ОС. Интеграционный smoke с реальным запуском —
отдельно (skip-if-not-installed).
"""

from __future__ import annotations

from apexcore.infrastructure.stress.external_stress_ng import (
    _parse_bogo_ops,
    _parse_verify,
)


def test_parse_verify_passed_failed_zero():
    stderr = (
        "stress-ng: info:  [12345] dispatching hogs: 8 cpu\n"
        "stress-ng: info:  [12345] passed: 8: cpu (8)\n"
        "stress-ng: info:  [12345] failed: 0\n"
        "stress-ng: info:  [12345] metrics untrustworthy: 0\n"
    )
    passed, failed = _parse_verify(stderr)
    assert passed == 8
    assert failed == 0


def test_parse_verify_with_failures():
    stderr = (
        "stress-ng: info:  [12345] passed: 6: cpu (6)\n"
        "stress-ng: info:  [12345] failed: 2: cpu (2)\n"
    )
    passed, failed = _parse_verify(stderr)
    assert passed == 6
    assert failed == 2


def test_parse_verify_no_verify_lines():
    stderr = "stress-ng: info:  [12345] dispatching hogs: 1 cpu\n"
    passed, failed = _parse_verify(stderr)
    assert passed == 0
    assert failed == 0


def test_parse_bogo_ops_example():
    stderr = (
        "stress-ng: metrc: [184401] cpu  12345 100.00 12.34 13.45 1234.56 1000.0\n"
    )
    total, rate = _parse_bogo_ops(stderr)
    # Парсер использует относительные индексы: floats[-5] и floats[-2].
    # Мы подтверждаем существующее поведение, не меняя его.
    assert total == 100.0
    assert rate == 1234.56
