# Сторонние компоненты (Windows)

В этой директории во время сборки оказываются 24 .NET-DLL из релиза
**LibreHardwareMonitor v0.9.6** (2026-02-14). Без них модуль
`benchkit.infrastructure.sensors.lhm` не сможет читать температуры,
но и не упадёт — graceful degrade до WMI/CIM.

Сами файлы **не коммитятся** в git — их выкачивает `scripts/fetch_lhm.ps1`
с фиксированной версии релиза и проверяет SHA256.

## LibreHardwareMonitorLib.dll и зависимости

- **Источник:** https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases/tag/v0.9.6
- **Лицензия:** Mozilla Public License 2.0 (MPL-2.0).
  Полный текст: https://www.mozilla.org/en-US/MPL/2.0/
- **Что внутри:**
  - `LibreHardwareMonitorLib.dll` — основной модуль (CPU/GPU/материнка/storage сенсоры).
  - `HidSharp.dll`, `BlackSharp.Core.dll`, `DiskInfoToolkit.dll`,
    `RAMSPDToolkit-NDD.dll` — низкоуровневые зависимости (HID, AIO-блоки, SMBus).
  - `Microsoft.Bcl.*`, `Microsoft.Win32.TaskScheduler.dll`, `System.*` —
    netstandard2.0 polyfill'ы для запуска под .NET Framework 4.8.
- **Что НЕ копируется:** `LibreHardwareMonitor.exe`, `OxyPlot.*`, `Aga.Controls.dll`
  и `.pdb`-файлы — это GUI-обёртка LHM, нам не нужна.

## WinRing0x64.sys (kernel-driver)

В v0.9.6 драйвер **встроен в `LibreHardwareMonitorLib.dll`** как embedded
resource. При первом запуске LHM сама извлекает его в `%TEMP%`, регистрирует
kernel-сервис `WinRing0_1_2_0` и подключается к нему. Дальнейшие запуски
поднимают уже зарегистрированный сервис без UAC.

Инсталлер benchkit (`PrivilegesRequired=admin`) запускает `benchkit info`
один раз под admin-сессией прямо в `[Run]`, чтобы драйвер встал заранее.

- **Лицензия драйвера:** OpenLibSys License (BSD-style).
- **Источник:** https://github.com/QCute/WinRing0 (форк OpenLibSys).

## Соответствие требованиям MPL 2.0

LibreHardwareMonitorLib и её зависимости распространяются в неизменённом
виде, сборка официального релиза. При запросе исходников — направляем
пользователя на upstream GitHub.
