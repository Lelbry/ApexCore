# Astra Linux — паспорт тестового стенда

Single source of truth для всех ссылок «на каком билде Astra Linux
проверялся apexcore». Обновляется при смене железа/билда и должен
синхронизироваться с описанием PR/issues на GitHub при каждом
Astra-фиксе.

Если в commit-message или PR упоминается «протестировано на Astra» —
ссылаться сюда (postscript к коммиту вида
`Tested-on: Astra Linux SE 1.8.5.46 (bulletin 2026-0224SE18, kernel 6.1.158)`).

---

## Дистрибутив

| Параметр | Значение |
|---|---|
| Релиз | Astra Linux Special Edition «Орёл защищенности» |
| Major.Minor.Patch | **1.8.5** |
| Полный номер билда | **1.8.5.46** |
| Бюллетень | **№ 2026-0224SE18** |
| Дата фиксации дистрибутива | **11.02.2026** |
| Базовый Debian | 12 (bookworm) |
| Ядро | 6.1.158-1-generic |
| PARSEC security policy | активна (минимальный уровень) |
| Python в системе | 3.11.2 (apt) |

## Тестовое железо

| Компонент | Модель |
|---|---|
| CPU | AMD Ryzen 7 6800H (8C/16T, Zen 3+, Rembrandt) |
| iGPU | AMD Radeon 680M (RDNA 2, 12 CU) |
| Дискретная GPU | — (отсутствует) |
| RAM | 14.8 ГБ (LPDDR5/DDR5, точная модель не идентифицирована) |
| Накопитель | Phison CFESR512GMTCT-E9C-2 (NVMe SSD, 512 ГБ, FW EJFM31.2) |
| Платформа | Ноутбук |

## Что доступно на этом стенде «из коробки»

| Источник данных | Статус | Ключи в snapshot |
|---|---|---|
| `psutil.sensors_temperatures` | ✓ | `k10temp.tctl`, `k10temp.tdie` (CPU) |
| Linux hwmon — CPU (`k10temp`) | ✓ | `k10temp.tctl` |
| Linux hwmon — iGPU (`amdgpu`) | ✓ | `gpuamd/edge` (~44 °C idle) |
| Linux hwmon — NVMe (`nvme`) | ✓ | `storage/nvme_composite` (~36 °C idle) |
| `smartctl --scan` | ✓ | находит `/dev/nvme0` |
| `smartctl -a /dev/nvme0n1` (root) | ✓ | полный SMART + temperature thresholds |
| `smartctl -a /dev/nvme0n1` (user + `setcap cap_sys_rawio+ep`) | ✗ | возвращает только Identify (см. `problems_fixes.md` #10) |
| `nvidia-smi` / pynvml | ✗ | нет NVIDIA-драйвера (нет дискретной GPU) |
| LibreHardwareMonitorLib | — | Windows-only, на Linux не используется |
| HWiNFO/CoreTemp/AIDA64 SHM | — | Windows-only |

## Pre-flight требования для сборки `.deb`

Перед первым `bash new-app/scripts/build_astra.sh`:

```bash
sudo apt install -y dh-python debhelper devscripts build-essential \
                    fakeroot libcap2-bin polkit imagemagick python3-venv
```

Без этого dpkg-checkbuilddeps падает — см.
[`problems_fixes.md` #1](problems_fixes.md#1-unmet-build-dependencies-dh-python).

## Известные расхождения с «эталонным» Debian 12

| Где | Что отличается | Влияние |
|---|---|---|
| PATH у обычного пользователя | `/usr/sbin/` отсутствует в PATH (PARSEC может это форсить) | `which smartctl` → пусто. Фикс: `which_with_sbin()` (#5). |
| NVMe SMART log | требует `CAP_SYS_ADMIN`, `cap_sys_rawio` устарел | smartctl от user пустой. Фикс: hwmon как primary (#10). |
| `_apt` sandbox | не имеет доступа в `/home/<user>/` | NOTICE при `apt install ./local.deb`. Workaround: класть .deb в `/tmp/` (#7). |

## Что **не проверялось** (известные пробелы покрытия)

- Astra Common Edition (CE), Server Edition (SE Server) — только Workstation SE.
- Старшие билды 1.8.5.* < .46 и младшие 1.8.4/1.7.x — поведение PARSEC + ядра может отличаться.
- Дискретные GPU на Linux (NVIDIA + amdgpu). Сейчас iGPU only.
- Многодисковые конфигурации (multiple NVMe, NVMe + SATA).
- Intel-платформы с i915/xe hwmon — код есть, но не верифицирован на железе.

При попадании apexcore на другую конфигурацию — добавлять отдельный
раздел сюда, не перезаписывая текущий.

## Хронология тестов

| Дата | Билд Astra | Что тестировали | Журнал |
|---|---|---|---|
| 2026-05-21 | 1.8.5.46 / 2026-0224SE18 / kernel 6.1.158 | первичная Astra-сборка v0.8.7, hwmon GPU/disk pipeline, NVMe T° через hwmon | [`problems_fixes.md`](problems_fixes.md) #1–#10 |

При каждом новом тестовом сеансе добавлять строку в эту таблицу +
дописывать журнал в `problems_fixes.md`.
