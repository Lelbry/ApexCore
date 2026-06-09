# ApexCore — компоненты третьих лиц

ApexCore распространяется по лицензии MIT (см. файл `LICENSE`).
В состав дистрибутива входят следующие компоненты сторонних авторов:

## Bundled (в установщике)

| Компонент | Лицензия | URL |
|---|---|---|
| LibreHardwareMonitor | MPL-2.0 | https://github.com/LibreHardwareMonitor/LibreHardwareMonitor |
| .NET 9 Runtime | MIT (Microsoft) | https://dotnet.microsoft.com/ |
| PawnIO | MIT | https://github.com/namazso/PawnIO |
| smartmontools (внешний exe) | GPL-2.0+ | https://www.smartmontools.org/ |
| WebView2 Runtime (Microsoft) | proprietary, redistributable | https://developer.microsoft.com/microsoft-edge/webview2/ |

## Опциональные внешние утилиты

| Компонент | Лицензия | URL |
|---|---|---|
| stress-ng | GPL-2.0+ | https://github.com/ColinIanKing/stress-ng |
| Prime95 | proprietary freeware | https://www.mersenne.org/download/ |

## Python-зависимости (runtime)

Все зависимости лицензированы по permissive-лицензиям, совместимым с MIT.

| Пакет | Лицензия |
|---|---|
| pydantic, pydantic-settings | MIT |
| typer, rich, click | MIT |
| numpy | BSD-3-Clause |
| scipy | BSD-3-Clause |
| psutil | BSD-3-Clause |
| PyYAML | MIT |
| cryptography | Apache-2.0 / BSD-3-Clause |
| platformdirs | MIT |
| pythonnet, clr_loader | MIT |
| pywin32 | PSF-2.0 |
| nvidia-ml-py | BSD-3-Clause |
| FastAPI, uvicorn, websockets | MIT / BSD-3-Clause |

## Лицензии (полные тексты)

- MIT — см. `LICENSE` в корне репозитория.
- MPL-2.0 — текст в `src/apexcore/infrastructure/sensors/lib/NOTICE.md`
  и на https://www.mozilla.org/en-US/MPL/2.0/.
- GPL-2.0+ — внешние исполняемые файлы вызываются как отдельные процессы
  (subprocess), что не создаёт производного произведения для целей GPL.
  Тексты лицензий — на сайтах соответствующих проектов.
- Apache-2.0 — https://www.apache.org/licenses/LICENSE-2.0.
- BSD-3-Clause — https://opensource.org/license/bsd-3-clause/.

## Изменения

При добавлении / обновлении / удалении любой зависимости — обновлять этот
файл синхронно. Также см. `packaging/windows/bootstrapper/Resources/wwwroot/js/steps/license.js`
(п. 5 пользовательского соглашения) — содержит сокращённый список,
синхронизировать вручную.
