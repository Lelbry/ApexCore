"""End-to-end тест WinsatService на реальной системе.

Skip на не-Windows: модуль доступен только на Windows.
Длительность подтестов — 0.3 с, чтобы не замедлять CI.
"""

from __future__ import annotations

import sys

import pytest

from apexcore.application.winsat_service import WinsatService
from apexcore.domain.winsat import WinsatStatus
from apexcore.infrastructure.adapters import AdapterFactory


@pytest.mark.skipif(sys.platform != "win32", reason="winsat работает только на Windows")
def test_run_formal_returns_complete_report() -> None:
    adapter = AdapterFactory.detect()
    service = WinsatService(adapter)
    report = service.run_formal(duration_sec_per_test=0.3, save=False)

    # Все 5 подскоров заполнены.
    assert report.cpu_score is not None
    assert report.memory_score is not None
    assert report.disk_score is not None
    assert report.graphics_score is not None
    assert report.d3d_score is not None

    # Реальные подскоры (CPU/Memory) всегда PASS на Windows.
    assert report.cpu_score.status == WinsatStatus.PASS
    assert report.memory_score.status == WinsatStatus.PASS
    # Disk может быть PASS или ERROR (если в tempdir < 1 ГБ свободно), но не NA.
    assert report.disk_score.status in (WinsatStatus.PASS, WinsatStatus.ERROR)
    # Graphics/D3D с v0.8.6 вызывают native `winsat dwm -xml`. Под админом —
    # PASS с XML-парсингом GraphicsScore/GamingScore. Без админа winsat-DWM
    # падает с access denied → ERROR. До v0.8.6 эти поля были NA-заглушками.
    assert report.graphics_score.status in (
        WinsatStatus.PASS, WinsatStatus.NA, WinsatStatus.ERROR,
    )
    assert report.d3d_score.status in (
        WinsatStatus.PASS, WinsatStatus.NA, WinsatStatus.ERROR,
    )

    # WinSPRLevel в допустимом диапазоне.
    assert 1.0 <= report.winspr_level <= 9.9


def test_is_supported_matches_platform() -> None:
    expected = sys.platform == "win32"
    assert WinsatService.is_supported() is expected


@pytest.mark.skipif(sys.platform == "win32", reason="проверка поведения на Linux")
def test_run_formal_raises_on_non_windows() -> None:
    adapter = AdapterFactory.detect()
    service = WinsatService(adapter)
    with pytest.raises(RuntimeError, match="недоступен"):
        service.run_formal(duration_sec_per_test=0.1)
