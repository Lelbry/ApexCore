"""Регрессия: /static отдаётся с Cache-Control: no-cache.

Без этого браузер эвристически кэширует ES-модули WebUI/мастера и после
переустановки/upgrade отдаёт устаревший JS (симптом: новый элемент UI
«не появляется», хотя файл на диске свежий). См. _NoCacheStaticFiles.
"""

from __future__ import annotations

import asyncio

import pytest

# extras 'webui' — fastapi нужен для server.py. Нет — skip.
pytest.importorskip("fastapi")

from apexcore.interfaces.webui.server import _NoCacheStaticFiles


def test_static_response_has_no_cache_header(tmp_path):
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "app.js").write_text("export const a = 1;", encoding="utf-8")

    sf = _NoCacheStaticFiles(directory=str(static_dir))
    scope = {"type": "http", "method": "GET", "headers": []}
    response = asyncio.run(sf.get_response("app.js", scope))

    assert response.status_code == 200
    assert response.headers.get("cache-control") == "no-cache"
