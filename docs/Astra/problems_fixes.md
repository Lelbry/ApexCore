# Report: Astra Linux SE 1.8.5.46 — встреченные проблемы и их фиксы

Журнал реальных проблем при сборке + установке + работе apexcore на
**Astra Linux Special Edition 1.8.5.46 «Орёл защищенности»** (бюллетень
**№ 2026-0224SE18**, дата фиксации дистрибутива **11.02.2026**, ядро
**6.1.158-1-generic**, Debian 12 base). Каждая запись содержит
**симптом**, **причину**, **фикс**, **коммит** с правкой, **lessons learned**.

Файл live-обновляется при каждой новой находке. Для быстрой ориентации
— короткая шпаргалка в [`install_pitfalls.md`](install_pitfalls.md).
Точные параметры стенда (билд, ядро, железо, что доступно из коробки)
— в [`test_environment.md`](test_environment.md). При новом тестовом
сеансе на другой Astra-конфигурации добавлять отдельный раздел туда,
не переписывая текущий.

**Тестовая конфигурация** (краткая выжимка из `test_environment.md`):
ноутбук AMD Ryzen 7 6800H + Radeon 680M iGPU (RDNA 2), 14.8 ГБ RAM,
Phison CFESR512GMTCT-E9C-2 NVMe SSD (512 ГБ, FW EJFM31.2), Astra
Linux SE 1.8.5.46 (Python 3.11.2 из apt).

---

## Build phase (`bash new-app/scripts/build_astra.sh`)

### #1. Unmet build dependencies: dh-python

- **Симптом**: `dpkg-checkbuilddeps: ошибка: Unmet build dependencies: dh-python`
- **Причина**: Astra base не имеет `dh-python` в default install
- **Фикс пользователю**: `sudo apt install -y dh-python`
- **Фикс в репо**: документация (build_astra.sh не может проверять заранее — он сам
  через dpkg-buildpackage)
- **Lessons**: документировать минимальный набор build-deps явно в README/install_pitfalls

### #2. debhelper compat double-declaration

- **Симптом**: `dh: error: debhelper compat level specified both in debian/compat and via build-dependency on debhelper-compat`
- **Коммит**: `327af41`
- **Причина**: debhelper 13+ требует один способ — Build-Depends ИЛИ файл `compat`,
  не оба сразу
- **Фикс**: удалён `debian/compat`, остался `Build-Depends: debhelper-compat (= 13)` в `control`
- **Lessons**: при создании debian/-конфигурации использовать **только** современный
  Build-Depends синтаксис; legacy compat-файл не нужен

### #3. dh_usrlocal: /usr/local/ для пакетов запрещён

- **Симптом**: `dh_usrlocal: error: debian/apexcore/usr/local/bin/apexcore is not a directory`
- **Коммит**: `65a6a51`
- **Причина**: Debian Policy запрещает пакетам класть файлы в `/usr/local/` —
  туда кладёт локальный администратор системы
- **Фикс**: install в `/usr/bin/<wrapper>`; обновлены `debian/rules`, `.desktop`,
  `wrapper` docstring
- **Lessons**: для пакетных бинарей стандарт = `/usr/bin/`; `/usr/local/` —
  только ручные установки

### #4. dh_dwz падает на manylinux wheels

- **Симптом**:
  ```
  dh_dwz: warning: ...numpy/.../_multiarray_umath.cpython-311-x86_64-linux-gnu.so returned exit code 1
  dh_dwz: error: Aborting due to earlier error
  ```
- **Коммиты**: `bee71d7` (override) + `4429b9d` (DEB_BUILD_OPTIONS fallback)
- **Причина**: pre-built numpy/scipy wheels stripped без DWARF info, dh_dwz пытается
  обработать их и считает отсутствие DWARF за error
- **Фикс**:
  1. `override_dh_dwz: @true` в `debian/rules`
  2. `export DEB_BUILD_OPTIONS = nostrip nodwz noddebs` в начале `rules` как fallback
- **Lessons**: для embedded-venv `.deb` отключать **все** helpers касающиеся binaries:
  `dh_dwz`, `dh_strip`, `dh_shlibdeps`, `dh_makeshlibs`, `dh_strip_nondeterminism`,
  `dh_compress` (последний особенно — он ломает `.py` в venv)

---

## Install / runtime phase

### #5. /usr/sbin не в PATH у обычного пользователя

- **Симптом**: `which smartctl` → пусто, хотя `ls /usr/sbin/smartctl` показывает файл
- **Коммит**: `b7492b4`
- **Причина**: Debian convention (`/usr/sbin` для root-утилит). На Astra SE
  дополнительно PARSEC policy может сбрасывать PATH даже после `export` в `.bashrc`
- **Фикс в репо**: новый `infrastructure/sbin_lookup.py::which_with_sbin()`:
  - сначала стандартный `shutil.which()` (PATH lookup)
  - если не нашло — fallback на `/usr/sbin`, `/sbin`, `/usr/local/sbin`
- **Применён в 4 файлах** (8 мест замены):
  - `infrastructure/sensors/smartctl.py::is_available` + `_run_smartctl`
  - `application/diagnostics_sensors.py::_check_smartctl`
  - `interfaces/webui/setup_router.py::_probe_environment` + 3 setcap/sensors-detect
- **Lessons**: на Linux никогда не полагаться только на PATH для sbin-утилит;
  использовать абстракцию которая знает про стандартные sbin-каталоги

### #6. AMD iGPU температура «нет данных» при наличии amdgpu hwmon

- **Симптом**: `apexcore doctor` → `GPU ✗ нет данных`, хотя
  `/sys/class/hwmon/hwmon4/name=amdgpu, temp1_input=46000` (label=edge, 46°C)
- **Коммит**: `6889092`
- **Причина**: `_read_hwmon` писал ключ `amdgpu.edge` — общий формат `<chip>.<label>`,
  классификатор GPU temperature не распознавал такие ключи как GPU
- **Фикс**:
  1. В `linux.py::_read_hwmon`: набор `_GPU_HWMON_CHIPS = {amdgpu, radeon, i915, xe}`.
     Для этих chip'ов префикс `gpu/<chip>/<label>` вместо `<chip>.<label>`.
  2. В `diagnostics_sensors.py` GPU detection: после pynvml/LHM/nvidia-smi смотрит
     на ключи с префиксом `gpu/` среди hwmon-сенсоров.
- **Lessons**: универсальное чтение hwmon недостаточно — нужно знать какие chip
  относятся к каким категориям (CPU/GPU/MB/Disk). Wishlist: расширить mapping
  и для других классов (acpitz → MB, nvme → Disk).

### #7. _apt sandbox warning при `apt install ./local.deb` из ~/

- **Симптом**:
  ```
  N: Загрузка выполняется от лица суперпользователя без ограничений песочницы,
     так как файл «...benchkit_0.8.7_amd64.deb» недоступен для пользователя «_apt»
     - pkgAcquire::Run (13: Отказано в доступе)
  ```
- **Статус**: NOTICE (не error), установка проходит успешно
- **Причина**: `_apt` системный пользователь не имеет доступа в `/home/alex/` для
  sandbox-режима. apt fallback на root-режим без sandbox — установка работает,
  предупреждение косметика.
- **Workaround**: положить `.deb` в `/tmp/` перед установкой:
  `sudo cp file.deb /tmp/ && sudo apt install /tmp/file.deb`
- **Не блокер**, фикс в репо не нужен

---

## Sensor classification edge cases (выявлены при apexcore doctor на AMD APU)

### #8. smartctl видит NVMe, но «устройств с T° не найдено»

- **Симптом**: `apexcore doctor` показывает `smartctl ✓ Сенсоров: -` с пояснением
  «устройств с T° не найдено», хотя `/usr/sbin/smartctl --scan` находит `/dev/nvme0n1`
- **Причина**: smartctl без capability `cap_sys_rawio+ep` видит устройства, но не
  читает их SMART-данные (включая температуру) — kernel требует root или CAP_SYS_RAWIO
- **Фикс пользователю**: `sudo setcap cap_sys_rawio+ep /usr/sbin/smartctl`
- **Фикс автоматический**: wizard `apexcore setup` на шаге Progress прозванивает
  `pkexec setcap` (5 опционально через `pkexec sensors-detect --auto`)
- **TODO в репо**: рассмотреть fallback на чтение NVMe температуры через hwmon
  (`/sys/class/hwmon/<n>/name=nvme/temp1_input` — у пользователя 29°C), если
  smartctl недоступен. См. план Фаза C.1.2.

### #9. `apexcore doctor` → GPU «нет данных» при наличии amdgpu hwmon (после #6)

- **Симптом**: после rebuild с фиксом #6 (linux.py::_read_hwmon префиксит
  `gpu/amdgpu/edge`) — `apexcore doctor` всё ещё `GPU ✗ нет данных`, хотя
  `Linux hwmon ✓ Сенсоров 6, CPU-устройств: k10temp` (amdgpu присутствует
  в системе, видно по `ls /sys/class/hwmon/*/name`).
- **Причина**: фикс #6 был **неполным**. Изменён только `LinuxAdapter._read_hwmon`
  (snapshot в рантайме). А `application/diagnostics_sensors.py::_check_hwmon`
  (используется в `apexcore doctor`) — отдельный код, который читал только
  **имена** чипов через `/sys/class/hwmon/*/name`, не заполняя `BackendStatus.sample`
  значениями. GPU-детекция в `diagnose_sensors()` (строка 717) ищет ключи
  `gpu/...` именно в `b.sample` — а он всегда был пуст для hwmon.
- **Фикс**: `_check_hwmon` теперь читает все `temp*_input` каждого hwmon
  и заполняет `sample` с префиксом `gpu/<chip>/<label>` для chips из
  `{amdgpu, radeon, i915, xe}` (зеркало `_GPU_HWMON_CHIPS` из `linux.py`).
  Также в `detail` добавляется строка `GPU-устройств: amdgpu` для UX.
- **Lessons**: при изменении классификации ключей сенсоров **проверять ВСЕ**
  места, где они используются — `LinuxAdapter._read_hwmon` (live snapshot) и
  `diagnostics_sensors._check_hwmon` (doctor health-sweep) живут параллельно
  и легко расходятся. В идеале — единый источник истины (TODO: extract
  `_read_hwmon_raw()` helper в `infrastructure/sensors/hwmon.py`).

### #10. NVMe температура: `cap_sys_rawio` недостаточен → hwmon как primary source

- **Симптом**: `sudo setcap cap_sys_rawio+ep /usr/sbin/smartctl` ставится
  (verify через `getcap`), но `smartctl -a /dev/nvme0n1` от обычного
  пользователя возвращает **только** Identify-секцию (Model/Firmware/
  Namespace) — SMART log с температурой не приходит. `apexcore doctor`
  по-прежнему `smartctl ✗ устройств с T° не найдено`.
- **Конфигурация**: Astra Linux SE 1.8.5, kernel 6.1.158, Phison
  CFESR512GMTCT-E9C-2 NVMe.
- **Причина**: на kernel 6.1+ команда NVMe Get Log Page для SMART
  health requires `CAP_SYS_ADMIN`, не `CAP_SYS_RAWIO` (которая нужна
  только для legacy ioctl). `setcap cap_sys_rawio+ep` устарела как
  workaround для современного NVMe-стека. Альтернатива — `+cap_sys_admin`,
  но это **полное** ядро-admin, что неуместно для accessibility-утилит.
- **Решение**: kernel nvme-driver **сам** публикует composite
  температуру через hwmon (`/sys/class/hwmon/<n>/name=nvme,
  temp1_input=...`) **без всяких привилегий**. На тестовой машине
  получаем `35.85°C` для unprivileged user.
- **Фикс в репо**:
  1. `LinuxAdapter._read_hwmon` (linux.py): добавлен `_HWMON_DISK_CHIPS =
     {nvme, drivetemp}`, для них ключ публикуется как
     `storage/<chip>_<label>` (LHM-совместимый 2-сегментный формат) →
     попадает в `_parse_storage`-handler из `sensor_keys.py` и
     отображается в WebUI sensor-cards в группе STORAGE.
  2. `_read_temperatures` (linux.py): убран условный fallback
     «hwmon только если psutil пуст» — hwmon теперь читается **всегда**,
     слияние через `if k not in temps`. Раньше на Astra psutil давал
     CPU temp → hwmon никогда не звался → GPU/disk hwmon-температуры
     не попадали в live snapshot.
  3. Параллельно: GPU префиксы переключены с `gpu/<chip>/<label>` (3
     segments, в pipeline отбрасывались) на LHM-формат `gpuamd/<label>`
     / `gpuintel/<label>` (2 segments) — теперь WebUI рисует карточки
     GPU без правок sensor_keys.py.
  4. `_check_smartctl` (diagnostics): при отсутствии T° через smartctl,
     если в hwmon найден `nvme` или `drivetemp`, в detail
     подсказывается, что T° есть через kernel hwmon и smartctl нужен
     только для SMART attributes (запуск от root).
- **Lessons**:
  - На современных kernel-ах НЕ полагаться на `cap_sys_rawio` для NVMe
    SMART. Документация smartmontools всё ещё рекомендует её — это
    устарело для kernel 6.x.
  - Hwmon — **первичный** источник T° GPU/диска на Linux, потому что
    kernel-drivers сами публикуют sysfs-узлы. Сторонние инструменты
    (smartctl, lm-sensors, nvidia-smi) — опциональные дополнения,
    дающие больше деталей при наличии привилегий.
  - LHM-совместимый формат ключей (`gpuamd/`, `gpuintel/`, `storage/`)
    переиспользует pipeline parsing/render/WebUI без изменений —
    хороший паттерн на будущее.

### #11. WebUI Sensors-карточки CPU отсутствуют на Linux (psutil-формат ключей)

- **Симптом**: после фиксов #6/#9/#10 `apexcore doctor` показывает CPU
  / GPU / диск температуры корректно, но симуляция `parse_legacy_key`
  на живом snapshot говорит, что `k10temp.Tctl` (CPU temp от psutil)
  отбрасывается:
  ```
  k10temp.Tctl                     [DROPPED]
  gpuamd/edge                      gpu        AMD GPU        Edge      46.0
  storage/nvme_Composite           storage    Накопитель     Composite 34.85
  ```
  То есть в **WebUI Sensors** на Astra появлялись карточки GPU и
  Storage, но **не появлялась карточка CPU** (её нет в SensorSnapshot).
- **Причина**: psutil-loop в `LinuxAdapter._read_temperatures`
  публиковал CPU ключи в формате `<chip>.<label>` (через точку,
  legacy psutil-формат). `parse_legacy_key` ищет только LHM-стиль
  через слэш — `cpu/...` / `gpunvidia/...` и т.п. Префиксы `k10temp`,
  `coretemp` не были в `_GROUP_BY_PREFIX`, ключи через точку вообще
  не парсятся (там `partition("/")`). Итог: CPU температура была в
  raw dict (для doctor, stress test r_thermal, CLI render), но НЕ
  в SensorSnapshot для WebUI.
- **Фикс**: в `_read_temperatures` (psutil-loop) и `_read_hwmon`
  добавлен `_HWMON_CPU_CHIPS = {k10temp, zenpower, coretemp,
  cpu_thermal}`. Для этих чипов ключ публикуется как `cpu/<label>`
  (LHM-совместимый формат) → `parse_legacy_key` относит к группе CPU.
  Зеркало логики в `diagnostics_sensors._check_hwmon` для `sample`.
- **Lessons**:
  - Шаблон **«hwmon chip → LHM-prefix»** один на 3 категории (CPU/GPU/
    Disk). При добавлении новой категории (Fan, MB) повторять.
  - psutil на Linux под капотом читает тот же hwmon, что и наш прямой
    обход → есть **избыточное** чтение sysfs, дубли в memory dict
    (но не в UI после parse_legacy_key). Кандидат на оптимизацию:
    отказаться от psutil-loop для chips, которые уже покрыты hwmon-
    обходом. Не блокер, дубли безвредны.

### #12. WebUI на Astra показывает «Static UI not bundled»

- **Симптом**: `apexcore webui --port 8765` запускается, FastAPI слушает,
  `GET /` отдаёт `200 OK`, но в браузере открывается единственный
  fallback-HTML: «apexcore / Static UI not bundled». То есть Python-код
  доехал, а статика (HTML/CSS/JS/SVG) — нет.
- **Причина**: в `pyproject.toml` `[tool.setuptools.package-data]` были
  записи для `.sql` / `lib/*.dll` / `*.yaml`, но **не было** для
  `webui/static/**`. На Windows-инсталлере проблема скрыта: PyInstaller
  собирает рядом с .exe **всё** что нашёл в исходниках. На Astra сборка
  .deb идёт через wheel (`pip wheel . --no-deps` в build_astra.sh
  шаг [3/6]), и setuptools отбрасывает не-`.py` файлы без явного
  package-data — в итоге `apexcore/interfaces/webui/static/` в .whl
  отсутствует, и `STATIC_DIR.exists()` в `server.py:1212` даёт False.
- **Фикс**: расширен `[tool.setuptools.package-data]` для
  `apexcore.interfaces.webui` с включением `static/*.html`,
  `static/css/**/*.css`, `static/js/**/*.js`, `static/assets/**/*`,
  `static/vendor/**/*`, `static/setup/**/*`.
- **Проверка**: локальный `pip wheel . --no-deps` теперь даёт .whl,
  в котором есть `static/index.html`, все 12 `js/screens/*.js`,
  4 `css/*.css`, 4 `setup/css/*.css`, `assets/apex-logo.png` и т.д.
- **Lessons**: для не-Python ресурсов в setuptools обязательно явно
  перечислять в `package-data`. PyInstaller-сборка на Windows
  **маскирует** этот класс ошибок — реальная проверка идёт только при
  установке через pip (wheel/sdist) или .deb (через wheel). Имеет
  смысл добавить в CI тест `pip wheel . --no-deps && unzip -l ...whl
  | grep static/index.html` чтобы регрессия отлавливалась.

### #13. WebUI tour на Astra — 7 находок одним коммитом

После первого реального прогона WebUI на Astra 1.8.5.46 пользователь
зафиксировал ряд багов и UX-неточностей. Корни были разной природы
(UI, backend, semantic), фикс уложился в один коммит. Каждый подпункт
самостоятелен; см. также `dev-inherited-goose.md` (план фиксов с
обоснованием).

#### F-1. Custom duration `0.5` отбрасывался в Стресс-тесте

- **Симптом**: ввод дробного значения в поле «или введите свою длительность»
  не применяется, кнопка «Запустить» остаётся с пресетом.
- **Причина**: `parseInt(e.target.value, 10)` (stress.js) отбрасывал дробную
  часть, валидация требовала `v >= 1` (целые минуты).
- **Фикс**: `parseFloat`, `min="0.1"` в input, диапазон `0.1-1440` минут.
  Позволяет короткие dev-тесты (0.5 мин = 30 сек на N движков).

#### F-2 + F-6. «Стресс-тест нельзя отменить»

- **Симптом**: пользователь видел «12+ минут стресс-теста не
  останавливается при заявленных 10 минутах»; кнопки «Отмена» в DOM
  вообще не было.
- **Причина 1 (F-6)**: UI Stress-экран вызывает `api.benchStart` →
  `/api/bench/start` → `_BenchController`, который **не имел** ни
  `cancel_token`, ни endpoint `/api/bench/cancel`. Хотя
  `BenchmarkService.run(config, cancel_token)` уже умеет проверять
  токен между движками И пробрасывать его в каждый
  `engine.run(..., cancel_token=cancel_token)`, и все builtin
  стресс-движки (`builtin_cpu`, `builtin_ram`, `builtin_fft_stress`,
  `builtin_large_*`, external_*) уважают токен через
  `run_threaded_loop` — этот pipeline нечем было дёрнуть из UI.
- **Причина 2 (F-2)**: в `stress.js` live-card не было `<button>` для
  отмены — только текстовая «note» в правой колонке.
- **Фикс F-6**: `_BenchController._cancel_token: threading.Event` поле,
  в `start()` создаётся свежий Event и передаётся в `svc.run(...)`;
  новый метод `cancel()` ставит `set()` (идемпотентен); новый endpoint
  `POST /api/bench/cancel`; в api.js — `benchStop()`. После cancel
  результат сохраняется со `status="cancelled"` (уже было в svc).
- **Фикс F-2**: кнопка «⏹ Отменить тест» в live progress-card; bind
  на `api.benchStop()`; UI-флаг `cancelRequested` рисует «отмена
  запрошена…» до завершения worker-потока.

#### F-3. GPU имя обрезается до «Advanced micro devices…»

- **Симптом**: на Astra с iGPU Radeon 680M в topbar показывалось
  обрезанное вендор-имя вместо человеческого «Radeon 680M».
- **Причина**: `lspci` отдаёт `"Advanced Micro Devices, Inc. [AMD/ATI]
  Rembrandt [Radeon 680M] (rev 02)"`. Regex в `topbar.js::shortGpuLabel`
  искал только `(RTX|GTX|RX|Arc)`, `Radeon` не матчился, fallback
  обрезал до 22 символов.
- **Фикс**: первой проверкой регэксп на квадратные скобки lspci
  (`/\[([^\]]+?)\]/g`), берём последнюю с цифрами или известным brand
  (Radeon/GeForce/Iris/UHD/Xe/Arc/RTX/GTX), игнорируя технические
  `[AMD/ATI]` / `[Rembrandt]`. Vendor-regex расширен до `Radeon|GeForce|Iris`
  как fallback. На Windows discrete (NVIDIA WMI clean-name) поведение
  не меняется.

#### F-4. Ложный red-баннер «троттлинг активен» на idle AMD APU

- **Симптом**: на idle Astra (CPU 4%) Sensors-экран показывал красный
  баннер «ТРОТТЛИНГ АКТИВЕН · freq ratio 0.73 < 0.85».
- **Причина**: `_heuristic_throttle` срабатывал при `cpu_avg / cpu_max
  < 0.85` без учёта нагрузки. На AMD APU idle частоты падают по
  P-state idle в powersave-governor — это **штатное** энергосбережение,
  не throttle.
- **Фикс**: добавлен параметр `cpu_percent` в `read_throttle_state` и
  `_heuristic_throttle`. Heuristic пропускает срабатывание при
  `cpu_percent < 80%` — без значимой нагрузки нет смысла говорить о
  throttle. Прокинуто из caller'ов (`sensor_service.metric_to_sensor_snapshot`
  + `base.PsutilBaseAdapter._detect_throttling`). Регрессионные тесты:
  `test_heuristic_disabled_on_idle_amd_apu` + `test_still_triggers_under_real_load`.

#### F-5. После F5 «WS down» висел пару минут

- **Симптом**: после refresh страницы overlay «service is not running»
  не появлялся мгновенно, шапка статично писала «WS down».
- **Причина**: `MetricsSocket` намеренно не имеет auto-reconnect
  (low-level wrapper). При F5 первая попытка connect могла промахнуться
  по race-condition между closeold/openNew.
- **Фикс**: в `app.js` после первого `down`-события — одна soft-retry
  попытка через 2 сек **до** показа overlay. Если retry поднял
  соединение — UI оживает без участия пользователя. Архитектура ws.js
  (no auto-reconnect в low-level) сохранена.

#### F-7. «10 мин» в Стресс-тесте крутил 20 минут (UX-баг семантики)

- **Симптом**: профиль `cpu_heavy` запускал 2 движка последовательно
  (`builtin_cpu_int` + `builtin_cpu_fp`), каждый по `duration_sec`
  секунд. UI пресет «10 мин» → backend гонял 20 мин реального стресса.
- **Причина**: разное толкование «10 мин» между UI (общее время) и
  backend (per-engine). UI отправлял `duration_sec = 600`, backend
  гонял каждый из 2 движков по 600 сек.
- **Фикс**: UI хардкодит `ENGINES_PER_PROFILE = { cpu_heavy: 2 }` и в
  `computeDuration()` делит введённое `totalMin` на `N_ENGINES` перед
  отправкой. Label кнопки показывает обе цифры: «Запустить · 10 мин
  (5 мин × 2 движка)». В правой колонке «ЗАМЕТКИ» добавлен блок «тип
  нагрузки» с объяснением двух фаз cpu_heavy (целочисленная AES+SHA-1
  → плавающая запятая DGEMM). Семантика для пользователя теперь
  интуитивная: «введи общее время, я разделю».

#### Lessons на будущее

- **Контроллеры WebUI должны поддерживать cancel из коробки**, даже если
  «не критично сейчас». Backend (BenchmarkService, builtin stress-движки)
  уже умел cancel_token — потерян был только последний мост от REST
  endpoint'а до контроллера. Если service-layer что-то умеет, controller
  обязан это выставлять наружу.
- **Семантика «общее vs per-engine» должна быть явной в API**. Дефолт
  «duration_sec на движок» — backend-friendly, но не соответствует
  пользовательской интуиции. На длинной дистанции лучше переименовать
  поле в `duration_sec_per_engine` или ввести второе `total_duration_sec`
  с server-side делением.
- **Heuristic'и без guard'а по нагрузке — рассадник false positives**.
  Любая freq-ratio эвристика должна проверять `cpu_percent >= threshold`,
  иначе срабатывает на идле любой современной системы с агрессивным
  powersave.
- **UI low-level WS-wrapper без auto-reconnect** — это валидная
  архитектура, но bootstrap-layer должен компенсировать race на F5
  через soft retry. Иначе UX зачастую кажется «сломанным» сразу после
  страницы.
- **lspci формат с квадратными скобками** — стандартный паттерн для
  Linux GPU-имён. Backend-парсинг был бы более устойчивым, но JS-fix
  минимально-инвазивен и не трогает hot-path.

### #14. После rebuild WebUI: Storage-карточка отсутствует, мало датчиков, LAST STRESS показывает «0 FAIL»

Доп.итерация после первой проверки 7 фиксов из #13. Пользователь
зафиксировал:
- Sensors-экран показывает только CPU + GPU карточки (без Storage),
  и «мало датчиков» в каждой;
- после короткого 0.5-минутного стресс-теста LAST STRESS показывает
  «0 FAIL», стабильность/пик-температура «—».

#### F-8. Storage hwmon-карточка отбрасывалась в sensors.js

- **Симптом**: `storage/nvme_Composite` корректно создаёт SensorReading
  с group=STORAGE (проверено через симуляцию `parse_legacy_key` на
  ноуте), но в WebUI Sensors карточки «Накопители» нет.
- **Причина**: `_parse_storage` на Linux для legacy 2-сегментного
  формата `storage/nvme_composite` использовал generic
  `device="Накопитель"` (`lhm_names` пуст — LHM на Linux отсутствует).
  В `sensors.js::renderCards` стоит фильтр:
  ```
  if (hasInventory && !enriched.matched) continue;
  ```
  Если smartctl-scan нашёл устройство (на Astra нашёл — Identify
  работает без root через `cap_sys_rawio`), inventory непуст, но
  reading с device="Накопитель" не сматчился substring'ом с
  «Phison CFESR...» из inventory → reading отбрасывался как orphan.
- **Фикс**: `_parse_storage` для 2-сегментного формата при пустом
  `lhm_names` (= Linux hwmon путь) делает fallback на единственное
  устройство из `smartctl_info` (типичный single-NVMe laptop). Device
  становится «Phison CFESR512GMTCT-E9C-2 · SSD M.2 NVMe» — substring
  match в `enrichStorageDevice` срабатывает, карточка отрисовывается.
  Если smartctl-info пуст (smartctl недоступен) — device="Накопитель",
  а `hasInventory=false` в JS → filter не срабатывает, карточка
  всё равно показывается.
  Source меняется с `LHM` на `HWMON` для корректного badge в UI.

#### F-9. LAST STRESS «0 FAIL» — UX без понимания

- **Симптом**: после 30-секундного отменённого/завершённого прогона
  блок LAST STRESS показывает крупно «0», под ним красный FAIL chip,
  «стабильность —», «пиковая температура —». Пользователь не
  понимает, что произошло.
- **Причина**: 30 сек < `RELIABLE_DURATION_SEC = 90` — это слишком
  короткий прогон для thermal stability и r_thermal. Плюс `BenchmarkResult.
  final_score` на пути `_BenchController → BenchmarkService.run` всегда
  равен `0.0` (см. `benchmark_service.py:113` — scoring v1 deprecated,
  реальный stress_score считается через `application/stress_score.
  compute_stress_score_context` в pipeline `StabilityService` /
  TUI-меню, и WebUI на этот pipeline пока не переключён — pending).
- **Фикс**: `renderLastStress()` (stress.js) теперь показывает «—»
  вместо «0» когда `final_score === 0`, не рисует FAIL chip когда
  thermal-метрик нет, и добавляет warning-плашку для **коротких** /
  **отменённых** прогонов с пояснением:
  - Cancelled: «Прогон отменён на N сек — стресс-балл не считается,
    thermal stability не успела собраться».
  - Short (< 90 сек): «Прогон слишком короткий для надёжного балла
    (минимум 90 сек = 1.5 мин). Для оценки thermal stability
    рекомендуется ≥ 10 мин общего стресса».
- **Известный техдолг**: WebUI Stress-экран использует legacy путь
  через `_BenchController` (scoring v1, `final_score=0`). Полный
  переход на pipeline `compute_stress_score_context` для WebUI —
  отдельная задача (требует нового endpoint и переподключения UI
  Stress на `stress_orchestrator` или `StabilityService`).

#### F-10. «Мало датчиков» на Linux — добавили MOTHERBOARD группу

- **Симптом**: на Astra Sensors-экран показывает только CPU (Tctl +
  Частота) и GPU (Edge). Пользователь жалуется «мало датчиков».
- **Причина**: `acpitz` (ACPI thermal zone, обычно 45 °C — generic
  system temp) и `pch_*` (Intel PCH chipset temp) в snapshot были
  с psutil-форматом `acpitz.temp1` / `pch_alderlake.temp1` через
  точку → отбрасывались `parse_legacy_key`. На Linux без lm-sensors
  это потеря ~1-3 датчиков, что усиливало ощущение «мало данных».
- **Фикс**: `_HWMON_MB_CHIPS = {acpitz, pch_haswell, pch_skylake,
  ..., pch_alderlake}` — для этих чипов ключ нормализуется в
  `motherboard/<label>` (зеркало паттерна для CPU/GPU/Disk).
  Парсится как MOTHERBOARD-группа, отрисовывается отдельной
  карточкой «Материнская плата». acpitz — **не** реальная T° мат-
  платы (см. ARCHITECTURE.md), но информативная metric, явно
  отделённая от CPU в UI.

#### Lessons (доп.)

- **Filter «orphan reading» в UI должен иметь fallback**: жёсткий
  `continue` на mismatch с inventory работает на Windows
  (LHM-storage имеет device-name) и ломается на Linux (hwmon-
  storage с generic device-name). Backend должен наполнять device-
  name из доступных источников (lhm_names ⊕ smartctl_info) до
  отправки в UI.
- **Legacy путь `BenchmarkService.run` для UI Stress — техдолг**.
  `final_score=0` в legacy результате путает пользователя.
  Долгосрочное решение — переподключить UI Stress на pipeline
  `compute_stress_score_context` (через новый `_StressController`
  или endpoint вокруг `StabilityService`). Текущий фикс — UI-side
  работа со словом «—» вместо «0», чтобы не врать.

### #15. F-8 не сработал — Storage карточка по-прежнему отбрасывается

Доп.фикс после второй WebUI-проверки (см. #14 → F-8).

- **Симптом**: пользователь после rebuild с F-8 (backend-fallback в
  `_parse_storage` через `smartctl_info`) видит карточки CPU + GPU +
  «Материнская плата» (F-10 сработал ✓), но Storage-карточки нет.
- **Причина**: на свежей установке после `apt install --reinstall`
  capability `cap_sys_rawio+ep` на `/usr/sbin/smartctl` теряется
  (apt восстанавливает оригинальный binary без extended attributes),
  поэтому `read_smartctl_devices_info()` возвращает `{}`. Мой backend-
  fallback в `_parse_storage` срабатывает только при `len(smartctl_info)
  == 1`, а при пустом — берёт ветку `device = "Накопитель"`. Inventory
  через `list_physical_disks` (lsblk) при этом непуст (lsblk не требует
  привилегий), поэтому `hasInventory=true` в sensors.js, substring-match
  «накопитель» vs «phison cfes...» проваливается → JS-filter отбрасывает.
- **Фикс**: ослабить filter в `sensors.js::renderCards` — на single-
  device системе (типичный laptop = 1 NVMe) orphan-reading с generic
  device-name сматчить с **единственным** устройством inventory и
  обогатить именем из `list_physical_disks` (model + display_type +
  letters). Это покрывает кейс «smartctl без capability, но lsblk
  видит диск» без backend-изменений и без зависимости от capability-
  state. Файл: `interfaces/webui/static/js/screens/sensors.js`.
- **Альтернатива (не сделана)**: ставить `cap_sys_rawio+ep` через
  `dpkg-statoverride` или в `postinst`-скрипте `.deb`, чтобы capability
  переживала reinstall. Это отдельный задач (постоянная capability =
  отдельная политика безопасности).
- **Lessons**:
  - **Capability state не переживает apt reinstall**. Любой fix
    зависящий от `setcap` нестабилен — нужно либо postinst-script
    с restored cap, либо UI-fallback не требующий cap.
  - **list_physical_disks (lsblk-based) — надёжнее smartctl** для
    inventory на Linux. lsblk не требует привилегий и работает на
    любом kernel. smartctl нужен только для SMART log с T° (что и так
    закрыто через kernel hwmon, см. #10).

### #16. F-12: расширение Sensors на Linux до ~27 датчиков (+400% к F-11)

После того как пользователь сравнил Astra-вид Sensors (6 датчиков) с
Windows-аналогом (~73 датчика, эталон), мы провели dump
`/sys/class/hwmon/*` на ноуте Astra и нашли неиспользуемые в snapshot
датчики, которые kernel уже публикует БЕЗ привилегий:

```
/sys/class/hwmon/hwmon0 name=ACAD     # AC adapter (без полезных полей)
/sys/class/hwmon/hwmon1 name=acpitz   # temp1=45°C ✓ (уже F-10)
/sys/class/hwmon/hwmon2 name=BAT0     # in0=14.685 V — НЕ ИСПОЛЬЗОВАЛСЯ
/sys/class/hwmon/hwmon3 name=nvme     # temp1=Composite ✓ (уже F-11)
/sys/class/hwmon/hwmon4 name=amdgpu   # temp1=edge ✓ (F-6),
                                      # power1_average=25 W PPT,
                                      # in0=1.449 V vddgfx,
                                      # in1=0.650 V vddnb,
                                      # freq1=2.179 GHz sclk
                                      # — ВСЕ КРОМЕ TEMP НЕ ИСПОЛЬЗОВАЛИСЬ
/sys/class/hwmon/hwmon5 name=k10temp  # temp1=Tctl ✓ (F-7)
```

Плюс `/sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq` отдаёт
**16 per-core частот** для 8C/16T Ryzen 6800H (раньше шли в snapshot
как ``core_<N>`` — без `cpu/` префикса → отбрасывались
`parse_legacy_key`).

#### Изменения

1. **`infrastructure/adapters/linux.py`**:
   - Новый `_read_hwmon_voltages_powers()` — обходит все hwmon и читает
     `in*_input` (мВ → В), `power*_average` / `power*_input` (мкВт → Вт).
     Для amdgpu/radeon публикует `gpuamd/<label>` (vddgfx / vddnb /
     power_average). Для BAT0 — `motherboard/battery` (kind=VOLTAGE,
     label «Аккумулятор»). Outlier-guards для V (0-30) и W (0-600).
   - Новый `_read_hwmon_frequencies()` — `freq*_input` для amdgpu/i915.
     Hz → МГц, ключи `gpuamd/sclk` / `gpuamd/mclk` (handler
     `_parse_gpu_vendor` маппит в «Graphics clock» / «Memory clock»).
   - `_read_sensors`: теперь дополняет voltages-dict результатами
     `_read_hwmon_voltages_powers()` рядом с NVML power.
   - `get_frequencies_mhz`: per-core частоты теперь публикуются в
     LHM-совместимом формате `cpu/core_<idx>` (раньше — без префикса,
     ключи отбрасывались `parse_legacy_key`). 16 ядер → 16 отдельных
     readings с label «Ядро 0..15» в группе CPU. Также дополняется
     `gpuamd/sclk` через новый `_read_hwmon_frequencies`.

2. **`application/sensor_keys.py`**:
   - Новый handler `_parse_gpu_vendor()` — для ключей
     `gpuamd/<metric>` / `gpuintel/<metric>` определяет kind по metric:
     edge/junction/mem → TEMPERATURE; vddgfx/vddnb → VOLTAGE;
     power_average/ppt → POWER; sclk/mclk → FREQUENCY. Source=HWMON.
     LHM-имена (gpu_core/gpu_clock/gpu_core_voltage) handler пропускает
     обратно в generic 2-segment — на Windows AMD discrete карты с LHM
     поведение не меняется.
   - `_LABEL_BY_NAME["battery"] = "Аккумулятор"` для motherboard hwmon.

#### Ожидаемая карта Sensors на Astra после F-12

| Группа | Датчики | Источник |
|---|---|---|
| **CPU** | Tctl + cpu_avg + cpu_min + cpu_max + 16× Ядро N (per-core частоты) = ~20 | psutil + sysfs |
| **GPU (AMD GPU)** | Edge + Vcore GPU + VDDNB + Мощность + Graphics clock = **5** | amdgpu hwmon |
| **Материнская плата** | Acpitz + Temp1 + **Аккумулятор** = 3 | psutil + BAT0 |
| **Накопители** | Phison NVMe Composite = 1 | nvme hwmon |
| **Всего** | **~29** | (vs 6 до F-12, vs 73 на Windows LHM) |

Это **физический потолок** на данной конфигурации без admin-привилегий
и без lm-sensors с правильно загруженными модулями. Дальнейший прогресс
требует:
- `zenpower` модуль → per-core CPU temp (требует DKMS-build, не входит
  в Astra-репо);
- `jc42` модуль → DIMM температура (для DDR4 SO-DIMM, может быть);
- `nct6798d` или подобный → motherboard sensors (зависит от чипа Super-IO
  на материнской плате);
- `amd_energy` → CPU package power (на Zen 3+ доступен, но не загружен
  в Astra-ядре 6.1.158).

Это **отдельная** задача (для будущей итерации) — wizard может
предлагать `sudo modprobe zenpower` / `sudo apt install lm-sensors &&
sudo sensors-detect` чтобы добавить эти источники. Сейчас F-12 — это
максимум на out-of-the-box Astra без user-action'ов.

#### Lessons

- **Kernel hwmon — недооценённый источник на Linux**. Помимо температур
  он публикует voltage/power/clock без привилегий. Систематический
  обход всех `in*_input` / `power*_*` / `freq*_input` даёт +20 датчиков
  на типичной AMD APU + дисплеем.
- **LHM-совместимый префикс key + kind-aware handler** — масштабируемый
  паттерн. Можно добавлять метрики (через mapping в `_parse_gpu_vendor`)
  без изменений `MetricSnapshot` структуры или sensor_service конвертера.
- **Per-core частоты на Linux уже были в snapshot** (`core_<N>` ключи),
  но отбрасывались парсером — проверять что published-ключи реально
  имеют `<prefix>/...` формат, иначе они невидимы для UI.

### #17. F-12.1: 16 SMT-ядер → 8 физических + «Ppt 29.04 В» → «Мощность 29.04 Вт»

Доп.фикс после первой проверки F-12. Пользователь заметил два бага:

#### 17.1. «Ppt 29.04 В» вместо «Мощность 29.04 Вт» в GPU карточке

- **Симптом**: после F-12 в GPU карточке появилась строка
  «Ppt 29.04 В» — это неправильно. GPU PPT (Package Power Tracking) —
  это **мощность в Ваттах**, не напряжение.
- **Причина**: в `/sys/class/hwmon/<n>/power1_label` lable записан как
  «PPT» (uppercase). Мой `_parse_gpu_vendor` искал `mapping["ppt"]`
  (lowercase), не нашёл → возвращал `None` → fallback в generic
  2-segment handler, который видит ключ в `voltages`-dict и
  публикует с `kind=VOLTAGE`, label=«Ppt» (через capitalize). Так
  PPT приобрёл единицу «В» вместо «Вт».
- **Фикс**: lowercased metric в `_parse_gpu_vendor` перед mapping-lookup:
  `metric = parts[-1].lower()`. Покрывает все варианты регистра в
  hwmon labels (PPT, Edge, Tctl и т.п.).

#### 17.2. «Ядро 0..15» (16) вместо 8 физических ядер у Ryzen 6800H

- **Симптом**: CPU карточка показывает 16 ядер `Ядро 0..15`, хотя
  Ryzen 7 6800H физически имеет 8 ядер (Zen 3+ Rembrandt). На
  Windows-эталоне (12900K) показано 16 = 8P + 8E (тоже физических).
- **Причина**: `/sys/devices/system/cpu/cpu<N>/cpufreq/scaling_cur_freq`
  существует для каждого **logical CPU**, включая SMT-сиблингов
  (2 thread'а на 1 ядро у Zen). Я публиковал по одному `cpu/core_<N>`
  ключу на каждый logical CPU → 16 readings вместо 8.
- **Фикс**: группировка через `cpu<N>/topology/core_id`. Все siblings
  одного physical core'а сводятся в один reading с **max** частотой
  среди их scaling_cur_freq (max более информативен — показывает
  текущий boost ядра при нагрузке на любой из его SMT-thread'ов;
  mean бы занижал, когда один из siblings в idle). Затем
  пересортированы 0..N-1 для предсказуемого порядка в UI.
- **Бонус**: на гибридных Intel (Alder/Raptor Lake) core_id у P-cores
  и E-cores разные → группировка работает естественно без отдельной
  P/E-разметки. На Windows этот путь не используется (там LHM
  собирает per-core напрямую через MSR с готовой P/E классификацией).

#### Тесты

`1073 passed, 7 skipped` — регрессий нет. Особых юнит-тестов на
sysfs-topology не написано (mock'ировать `Path` иерархию в unit-тесте
слишком хрупко); ручной integration на ноуте показывает корректное
поведение.

### #18. Сравнение Linux ⟷ Windows: что физически недоступно (после F-12.1)

Пользователь сравнил Astra-карту (теперь ~21 датчик) с эталоном
Windows (~73 датчика через LHM). Делаю детальный pivot — какие
группы недоступны, почему, и что нужно для их включения.

| Группа | Windows LHM | Astra после F-12.1 | Источник | Доступность |
|---|---|---|---|---|
| **CPU temp** | per-core (16 × DTS) + Package | Tctl, нет per-core | MSR / `zenpower` | **❌ требует `zenpower` модуль (DKMS, kernel-headers)** |
| **CPU частота** | per-core boost (16) + базовая | 8 физических + min/avg/max | sysfs cpufreq | ✅ работает |
| **CPU Vcore** | Vcore + VID per-core (8) | — | MSR `MSR_PWR_UNIT` + `msr-tools` | **❌ требует `msr` модуль + admin** |
| **CPU Package Power** | Мощность пакета | — | `amd_energy` модуль / RAPL | **❌ требует `amd_energy` (не загружен в Astra 6.1)** |
| **GPU temp** | edge/junction/mem (3) | edge (1) | amdgpu hwmon | ✅ что доступно — публикуется (Rembrandt не выдаёт junction/mem) |
| **GPU Vcore** | gpu_core_voltage | Vcore GPU (vddgfx) | amdgpu hwmon | ✅ работает |
| **GPU VDDNB** | (нет на NVIDIA) | VDDNB | amdgpu hwmon | ✅ работает (специфично AMD APU) |
| **GPU Power** | TGP/PPT | Мощность (PPT) после F-17.1 | amdgpu hwmon | ✅ работает |
| **GPU Clock** | gfx + memory | Graphics clock (sclk) | amdgpu hwmon | ✅ работает (Rembrandt не выдаёт mclk, у APU memory общая с системой) |
| **DIMM temp** | DIMM 1, DIMM 3 (DDR4/5) | — | `jc42` модуль | **❌ требует `jc42` (для SO-DIMM на ноутбуках часто отсутствует sensor) ** |
| **Motherboard voltages** | +5V, +12V, AVCC3, AVSB, VRM MOS, etc | Аккумулятор (BAT0) | Super-IO chip drv | **❌ требует `nct6798d` / `it87` (зависит от модели мат-платы)** |
| **Motherboard temp** | Сокет, M.2, Чипсет (PCH), Система | Acpitz, Temp1 (generic) | Super-IO chip drv | **❌ как выше** |
| **CMOS-батарея** | CMOS-батарея | — | Super-IO drv | **❌ как выше** |
| **Вентиляторы** | Помпа, ЦП, System fan 1/2/6 (5) | — | Super-IO drv / EC | **❌ требует `nct6798d` или специфичный EC-driver** |
| **Накопители** | NVMe Composite, model, letter | NVMe Composite ✓ | kernel `nvme` | ✅ работает (single-NVMe) |

**Итого**: на Astra без admin-action'ов **~21 датчик** vs Windows LHM **~73** — это physical-ceiling. Разрыв создают **5 групп**, требующих kernel-модулей:

1. **`zenpower`** — per-core CPU temp (DKMS-сборка из исходников, требует kernel-headers, не входит в Astra-репо).
2. **`msr` + `msr-tools`** — CPU Vcore/VID per-core (requires admin + CAP_SYS_RAWIO).
3. **`amd_energy`** — CPU package power (на Zen 3+ доступен, но в Astra 6.1.158 нет в `lsmod`; вероятно нужен `modprobe`).
4. **`nct6798d`** / `it87` / `nct6775` — Super-IO chip для motherboard voltages/temps/fans (зависит от модели мат-платы ноутбука; на пользовательском MOZA PitHouse — нужна проверка `dmidecode -s baseboard-manufacturer`).
5. **`jc42`** — DIMM temperature (для laptop SO-DIMM **обычно отсутствует** SPD Hub с termal sensor → даже с модулем не покажет).

#### Wishlist для будущего

Wizard на шаге **«Components»** мог бы предлагать:
- ☐ Установить `lm-sensors` + запустить `sensors-detect --auto` (под pkexec)
- ☐ Загрузить `amd_energy` для CPU package power (`modprobe amd_energy`)
- ☐ Загрузить `msr` модуль для MSR-доступа (`modprobe msr`)
- ☐ (Опционально) Собрать `zenpower-dkms` для per-core CPU temp

Это **отдельная задача** — для текущей итерации F-12.1 ставит
physical-ceiling на out-of-the-box Astra: **21 датчик**, что в 3.5×
больше старых 6 и покрывает все категории кроме motherboard-чипа
и fan-сенсоров.

### #19. F-13: дедуп acpitz/temp1, topbar GPU multi-value, fans + 40+ Super-IO чипов

После F-12.1 пользователь нашёл:
- В «Материнской плате» дубль: `Acpitz 52°C` + `Temp1 52°C` — одна и
  та же температура показывается дважды.
- В topbar GPU-тайл показывает только температуру (48°C), а CPU
  показывает 5 метрик (T° / V / W / % / GHz) — нужна симметрия.
- Общий вопрос: что предпринять чтобы на **разных Astra-сборках**
  (другие материнские платы, другие CPU/GPU) показывалось больше
  датчиков?

#### 19.1. Дубль `Acpitz` + `Temp1` в группе MOTHERBOARD

- **Причина**: psutil для chip=acpitz отдаёт entry с пустым label →
  ключ становится `motherboard/acpitz`. Hwmon-loop читает тот же
  `/sys/class/hwmon/<n>/temp1_input` без `_label`-файла → label
  выводится из stem replace («temp1») → ключ `motherboard/temp1`.
  Два разных ключа, одинаковое значение — dedup через
  `if k not in temps` не сработал.
- **Фикс**: в hwmon-loop для MB-чипов при **отсутствии** label-файла
  использовать chip name (как psutil), чтобы ключи сходились
  → дедуп работает.

#### 19.2. Topbar GPU-тайл: расширен до T° / V / W / clock

- **Причина**: `pickFromAny` / `pickFirst` искал старые LHM-имена
  (`gpunvidia/gpu_core`, `gpunvidia/gpu_power`, `nvml/0/clock_graphics`).
  Мои новые hwmon-ключи `gpuamd/vddgfx`, `gpuamd/power_average`,
  `gpuamd/sclk` в списках отсутствовали — топбар их игнорировал
  и оставлял только температуру.
- **Фикс**: в `topbar.js::refreshTiles()` блок GPU расширен AMD-
  hwmon-вариантами. Теперь топбар показывает 5 метрик симметрично с CPU.

#### 19.3. Universal support — расширение поддерживаемых hwmon-чипов

**Расширил `_HWMON_MB_CHIPS`** с 8 чипов до **~40** покрывающих
основные семейства Super-IO:

| Семейство | Производители | Чипы |
|---|---|---|
| Nuvoton NCT | ASUS, MSI, Gigabyte (топ-сегмент) | nct6775/76/79/91/92/93/95/96/97/98, 7802, 7904 |
| ITE | ASRock, GIGABYTE B-серия, бюджет | it87, 8603, 8607, 8620, 8628, 8665, 8686, 8688, 8689, 8772, 8792, 8728 |
| Fintek | Mini-ITX, embedded | f71808a, 71862fg, 71869, 71882fg, 71889ed, 71889fg, 71889a |
| Winbond (legacy) | старые Intel/AMD | w83627hf/thf/ehf/dhg, 83667hg, 83781d, 83783s, 83791d, 83792d, 83793 |
| SMSC EMC | embedded | emc1402, 1403, 2305 |
| DIMM SPD | DDR4 SO-DIMM с thermal | jc42, spd5118 |
| Custom (water) | энтузиасты | aquaero, d5next, octo, highflownext |
| Intel PCH | Haswell..Alder Lake | pch_haswell..pch_alderlake |
| ACPI | стандарт | acpitz |

При наличии загруженного драйвера (`modprobe nct6798d` etc.) hwmon
чипа сразу подхватится в группу MOTHERBOARD без дополнительной
работы пользователя.

Расширил `_HWMON_CPU_CHIPS`: добавлены `zenpower2` (Zen4 fork),
`fam15h_power` (AMD K15 RAPL).

#### 19.4. Чтение fan*_input (вентиляторы)

Новый метод `_read_hwmon_fans()` обходит все hwmon и читает
`fan*_input` (RPM). Ключи публикуются как `fan/<chip>/<label>` →
`_parse_fan` в sensor_keys.py создаёт reading kind=FAN_RPM в группе
FANS. На пользовательской Astra без Super-IO драйвера результат
пустой (на ноуте `fan*_input` физически отсутствует во всех
hwmon-каталогах), но при загрузке модуля через wizard wentilator-
карточка появится автоматически.

#### 19.5. Roadmap для maximum coverage на разных Astra-сборках

| Источник | Что даёт | Как включить | Сложность |
|---|---|---|---|
| `sensors-detect` + lm-sensors | Auto-detect Super-IO chip + загрузка драйвера | `sudo apt install lm-sensors && sudo sensors-detect --auto && sudo /etc/init.d/kmod start` | ⭐ wizard может сделать через pkexec |
| `modprobe amd_energy` | CPU package power (Zen 3+) | `echo amd_energy > /etc/modules-load.d/amd_energy.conf` | ⭐ wizard |
| `modprobe msr` | CPU Vcore / VID через MSR | `echo msr > /etc/modules-load.d/msr.conf` (+ admin для чтения) | ⭐⭐ требует CAP_SYS_RAWIO |
| `zenpower-dkms` | per-core CPU temp (Zen 3+) | `apt install zenpower-dkms` или git clone + dkms build | ⭐⭐⭐ требует kernel-headers |
| `modprobe jc42` / `spd5118` | DIMM SO-DIMM thermal (если SPD Hub есть) | `modprobe jc42` | ⭐ wizard |
| `modprobe drivetemp` | SATA disk thermal через ATA passthrough | `modprobe drivetemp` | ⭐ wizard |
| `setcap cap_sys_rawio+ep` на smartctl | SMART log без root | `dpkg-statoverride` в postinst .deb | ⭐⭐ конфликт с apt reinstall |
| NVIDIA driver | nvidia-smi + pynvml метрики | `apt install nvidia-driver-XXX` (вне Astra-репо) | ⭐⭐⭐ ручная установка |
| `dmidecode` | модели DIMM/мат-платы (не sensors) | `apt install dmidecode` + admin | ⭐⭐ требует admin |

**Что я сделал прямо в коде** (без user-action):
- `_HWMON_MB_CHIPS` расширен на 40+ Super-IO чипов — если их драйвер уже загружен, sensors появятся автоматически.
- `_HWMON_CPU_CHIPS` расширен на `zenpower2`, `fam15h_power`.
- `_read_hwmon_fans()` — fan-датчики при наличии в hwmon.

**Что нужно добавить в wizard** (Phase B.2a wizard «Components» шаг):
1. ☐ `sudo apt install lm-sensors && sudo sensors-detect --auto` — auto Super-IO
2. ☐ `sudo modprobe amd_energy` — CPU power
3. ☐ `sudo modprobe drivetemp` — SATA disk
4. ☐ `sudo modprobe jc42` — DIMM (если есть SPD Hub)
5. ☐ (опц.) `sudo apt install zenpower-dkms` — per-core CPU temp

Это **отдельная итерация**, но архитектурно подготовлено.

### #20. F-15: micro_runs не показывались в /api/history (AttributeError)

После Phase B.2c (`apexcore micro run --preset fast` — успешный
прогон с score=284 на AMD Ryzen 6800H) запись была сохранена в БД
(`/home/alex/.local/share/apexcore/apexcore.sqlite3`, UUID
5e29e7c6-...), но в WebUI Истории её **не было** — экран показывал
только 4 stress-теста, dropdown «Тип теста» содержал только
«Все типы» + «Стресс-тест» без «Расш. тест CPU».

- **Причина**: в `server.py::history_unified` (`/api/history`) код
  читал `m.overall_score` напрямую от объекта `MicroBenchSuiteResult`,
  а это поле находится **внутри** вложенного `m.overall` (Pydantic
  sub-model `OverallScore`). `getattr` падал AttributeError → exception
  тихо ловился в `except Exception: logger.debug(...)` (низкий уровень,
  не виден в production-логах) → micro_runs не попадали в response
  → UI dropdown их не видел → пользователь видит только stress.
- **Фикс**:
  - Доступ к score через `getattr(m, "overall", None)` →
    `ov.overall_score`. Pydantic-safe (None если overall не заполнен,
    как у legacy/standalone прогонов без preset).
  - Все `logger.debug` для exception в `/api/history` изменены на
    `logger.exception` — теперь любой будущий баг будет виден в
    server-логе с полным traceback. Покрывает micro / general / winsat
    блоки.
- **Lessons**:
  - **`logger.debug` для catched exceptions в production-endpoint —
    плохой паттерн**: в production debug-уровень обычно отключён,
    exception становится «невидимым» багом. Для silent fallback'ов
    нужен `logger.warning` минимум, для критических — `logger.exception`.
  - **Pydantic-модели с вложенными sub-models** требуют двухуровневого
    доступа. SQLite-столбцы (`overall_score`, `ci_lower`, `ci_upper`,
    `scoring_version`) сериализованы для **быстрого filter без парсинга
    JSON**, но при чтении через repository это **payload_json** который
    Pydantic парсит в `MicroBenchSuiteResult.overall.<...>`.
  - **AttributeError тестами поймать нельзя без integration-test**
    реального CLI → webui → /api/history round-trip. Кандидат на
    добавление в test_history_endpoint.

### #21. Wheel молча уезжает со старым кодом (stale build/lib переживает git reset)

- **Симптом**: после `astra_rebuild_install.sh` (git reset --hard origin/dev →
  build → reinstall) установленный `server.py` НЕ содержал новый endpoint
  `/api/hardware`, хотя git HEAD на Astra его содержал. При этом НОВЫЙ файл
  `dram_info.py` в пакет попал нормально. `/api/system` работал,
  `/api/hardware` → `{"detail":"Not Found"}` (404). Sanity-шаг скрипта
  при этом рапортовал «✓ свежий код» (проверял старый маркер).
- **Коммит**: фикс в `build_astra.sh` + `astra_rebuild_install.sh`
  (цикл фиксов mock-данных, см. `/api/hardware`).
- **Причина**: `pip wheel . --no-deps` через setuptools переиспользует
  закешированный `new-app/build/lib/` между сборками. `build/` — untracked
  (в .gitignore), поэтому `git reset --hard` его НЕ трогает — stale build/lib
  переживает все ресеты. setuptools `build_py` копирует в build/lib по mtime:
  новые файлы добавляются, а изменённые перезаписываются не всегда → wheel
  собирается из смеси свежих новых + старых изменённых файлов.
- **Фикс**: в `build_astra.sh` перед `pip wheel` —
  `rm -rf build/lib build/bdist.* src/*.egg-info` + флаг `--no-cache-dir`.
  В `astra_rebuild_install.sh` sanity-шаг теперь сравнивает установленный
  `server.py` с `src/` через `cmp -s` (старый маркер `_HWMON_CPU_CHIPS` —
  фича из прошлых коммитов, она НЕ доказывает свежесть кода).
- **Lessons**: (1) sanity-проверка пересборки обязана сравнивать
  installed ⟷ source байт-в-байт, а не grep по старому маркеру — иначе
  stale-сборка проходит как «ок» и баг тихо уезжает в тест/отчёт.
  (2) untracked `build/`-кеши + `git reset --hard` = классическая ловушка:
  reset чистит только tracked-файлы, build/lib остаётся от прошлой ветки/версии.

### #22. .deb непереносим: venv от системного python (shebang на build-путь + привязка к /usr/bin/python3)

- **Симптом**: на стенде всё работало, но `.deb` НЕ запустился бы на чужой
  машине. Две причины (обе замаскированы тем, что build и install — на одной
  машине): (1) shebang консольных скриптов venv =
  `#!/home/.../debian/apexcore/opt/apexcore/.venv/bin/python3` (build-staging
  путь!) — на чистой машине его нет → «bad interpreter: No such file»;
  (2) `.venv/bin/python3 → /usr/bin/python3` → жёсткая привязка к системному
  Python 3.11 (на Astra с другим python пакет не работает).
- **Коммит**: бандл python-build-standalone (ветка `claude/astra-bundled-python`).
- **Причина**: `python3 -m venv` создаёт venv, чей `bin/python3` ссылается на
  системный интерпретатор, а pip пишет shebang с АБСОЛЮТНЫМ build-путём venv.
  При сборке под `debian/apexcore/...` этот путь — staging, его нет после
  установки на другой машине.
- **Фикс**: вместо системного venv бандлим **relocatable CPython**
  (`python-build-standalone`, install_only, gnu) как `/opt/apexcore/.venv`:
  самодостаточный интерпретатор + stdlib, не зависит от системного python.
  `debian/rules` распаковывает его, ставит cp311-колёса оффлайн и **переписывает
  shebang консольных скриптов на рантайм-путь** `#!/opt/apexcore/.venv/bin/python3`.
  Из runtime-`Depends` убраны `python3`/`python3-venv`. Скрипт
  `scripts/fetch_python_standalone.sh` качает интерпретатор (кеш).
- **Lessons**: (1) self-contained .deb с venv от системного python НЕ переносим —
  shebang и symlink ведут на build-машину; всегда проверять на чистой машине
  (или хотя бы переименовать build-дерево и запустить). (2) Релоцируемый
  standalone-python развязывает и от build-пути, и от версии системного Python
  одним приёмом.

### #23. Wizard: «Завершить» — немой тупик на Linux

- **Симптом**: пользователь проходит все шаги `apexcore setup`, жмёт «Завершить» —
  и ничего не происходит, кнопка «не работает».
- **Причина**: закрытие окна было реализовано только под WebView2 (Windows-
  bootstrapper). В обычном браузере (Astra first-run wizard через FastAPI) окна-
  хоста нет: клик `finish` ОТРАБАТЫВАЛ на сервере (маркер `setup_completed`
  писался), но в UI не было ни закрытия, ни редиректа, ни сообщения → выглядело
  как сломанная кнопка.
- **Фикс**: `static/setup/js/app.js handleFinish` — в браузере (не WebView2) после
  `finish` показываем экран «✓ ApexCore настроен» + кнопку «Открыть ApexCore»
  (→ `/` на том же порту) + фидбэк при ошибке. На Windows-WebView2 поведение
  не изменилось (окно закрывает нативный host).
- **Lessons**: общий UI для WebView2 и браузера — все «оконные» действия
  (close/minimize) надо дублировать браузерным эквивалентом, иначе на Linux
  немой тупик. Проверять wizard в реальном браузере, не только в WebView2.

### #24. Wizard ставил неверную capability (cap_sys_rawio недостаточен)

- **Симптом**: после wizard `dmidecode -t 17` и `smartctl -A` от обычного юзера
  всё равно не работали, хотя setcap «прошёл».
- **Причина**: wizard ставил `cap_sys_rawio+ep` на оба бинаря. Но dmidecode
  читает root-only `/sys/firmware/dmi/tables/DMI` (права `0400`) — для этого
  нужен `cap_dac_read_search` (bypass DAC на чтение файла), а не raw I/O.
  smartctl на NVMe требует `cap_sys_admin` для `NVME_IOCTL_ADMIN_CMD` (near-root).
- **Фикс**: `setup_router.py` — dmidecode → `cap_dac_read_search+ep` (проверено:
  даёт все модули DRAM обычному юзеру → `/api/hardware` без sudo). smartctl —
  setcap-шаг УБРАН (решение по безопасности): T° диска идёт через kernel hwmon,
  а SMART на NVMe требовал бы cap_sys_admin — не даём ради nice-to-have, SMART
  по root при необходимости. Минус один pkexec-промпт.
- **Lessons**: capability подбирать под КОНКРЕТНЫЙ барьер (DAC-права файла vs
  raw I/O vs admin-ioctl), а не «на всякий случай rawio». Проверять реальным
  запуском от непривилегированного юзера, не только `getcap`.

### #25. «Общая оценка»: нет итогового балла — диск-бенч писал в неписаемый корень

- **Симптом**: «Общая оценка системы» завершается без Итогового балла и без
  «% от пика» по диску. В `notes` отчёта:
  `disk_seq_read упал: [Errno 13] Permission denied: '/apexcore-winsat-*.bin'`.
- **Причина**: `boot_path = get_boot_drive()` на Linux = `/` (корень монтирования),
  и `_resolve_target_dir` при неудаче создать `/apexcore-bench` возвращал тот же
  неписаемый `/` → `mkstemp(dir="/")` падал. Все 3 диск-фазы → None → `r_disk`
  None → итоговый балл `None` (GM требует все три ratio). Кросс-платформенно:
  ломало и non-admin Windows (`C:\` тоже защищён).
- **Фикс**: `microbench/disk.py _resolve_target_dir` — при неписаемом корне
  fallback в писаемый `~/.cache/apexcore-bench` (на типовой одно-дисковой
  системе тот же физ.накопитель). НЕ системный tempdir сразу — `/tmp` часто
  tmpfs (RAM) и завысил бы скорость диска.
- **Lessons**: «boot-диск» ≠ писаемый корень монтирования. Для disk-бенча нужен
  писаемый каталог на том же ФИЗИЧЕСКОМ диске, не mount root. Полный прогон
  «Общей оценки» на Astra до этой сессии не проверялся (в аудите — только UI).

### #26. Стресс-балл никогда не считался на Linux (stress-ng vs DGEMM/STREAM)

- **Симптом**: «Стресс-тест» завершается, но «Оценка под нагрузкой» (стресс-балл)
  не выводится, хотя термал и стабильность собраны.
- **Причина**: `pick_cpu/ram_stressor` на Linux выбирали stress-ng matrixprod/vm
  (как «максимальный нагрев»), но они отдают `bogo-ops/s`. А `compute_stress_
  score_context` матчит throughput ПО ЕДИНИЦАМ — ищет `GFLOPS` (DGEMM) и `GB/s`
  (STREAM). bogo-ops не матчатся → `r_dgemm/r_stream` = None → балл None. Так как
  apexcore жёстко зависит от stress-ng (Depends), на Astra балл был недоступен
  ВСЕГДА.
- **Фикс**: `registry.py pick_cpu/ram_stressor` → `builtin_large_dgemm` (GFLOPS) +
  `builtin_large_stream` (GB/s): сильно греют И дают throughput, который скоринг
  сопоставляет с Roofline-пиком. Решение пользователя: scored-стресс на встроенных
  движках.
- **Lessons**: движок нагрузки для СКОРИНГА обязан отдавать ту метрику, которую
  ждёт формула балла (здесь — GFLOPS/GB/s vs Roofline). «Лучший нагреватель» и
  «измеримый под нагрузкой» — разные требования.

### #27. OOM / вылет браузера на машине с малым ОЗУ (DGEMM/STREAM × N потоков)

- **Симптом**: «Общая оценка» (и потенциально стресс) на ноуте 14.8 ГБ — браузер
  полностью вылетал с «недостаточно оперативной памяти» (системный OOM).
- **Причина**: `builtin_large_dgemm`/`builtin_large_stream` запускались без лимита
  python-потоков (= число ядер, 16). Для BLAS-DGEMM это N одновременных
  `np.matmul`, каждый со своим буфером C (~128 МБ) + оверсабскрайб BLAS; STREAM —
  N×3 массива. Пик 2–12 ГБ + Chrome → система уходит в OOM, ОС убивает браузер.
  Затрагивало и стресс (server.py/_BenchController, stress_menu), и «Общую
  оценку» (general_benchmark.py).
- **Фикс**: DGEMM = 1 python-поток + `threadpool_limits(logical-2)` — BLAS
  параллелит ВНУТРИ один matmul (1 буфер C, тот же нагрев). STREAM = `~logical/4`
  потоков. Пик памяти ~0.8–1.2 ГБ вместо 3–12. Применено в server.py, stress_menu
  и general_benchmark.
- **Lessons**: BLAS-движок (DGEMM) надо гонять в 1 python-поток + внутренняя
  BLAS-параллельность, а не N python-потоков × matmul — иначе буферы и
  оверсабскрайб. Тяжёлые бенчи проверять на машине с МАЛЫМ ОЗУ, не только на
  desktop с 32 ГБ. (Прим.: проверка прогонов по SSH рвётся под 100% CPU —
  голодает сеть; полные прогоны валидировать локально.)

### #28. Wizard: чекбокс «Запустить CLI» на финальном шаге (Astra)

- **Запрос**: на Windows финальный шаг мастера имеет чекбокс «Запустить CLI»
  (PowerShell от админа для полного Winsat). На Astra его не было — добавить
  параллельно, не ломая Windows-путь.
- **Коммит**: `fb60aea`
- **Реализация**: `done.js` — `if (!isLinux)`-блок (Windows: CLI+ярлык) оставлен
  как есть, добавлена `else`-ветка с Linux-чекбоксом «Запустить консольное
  приложение (CLI)» (подпись «Откроется терминал с интерактивным меню»). На
  бэкенде `setup_router.py _launch_cli_terminal_linux()` спавнит первый
  доступный терминал-эмулятор (`x-terminal-emulator`→konsole на Astra Fly,
  далее fly-term/konsole/xfce4/mate/gnome/xterm) с `apexcore`, detached
  (`start_new_session`), guard по `$DISPLAY`/`$WAYLAND_DISPLAY`. Вызывается в
  `finish`-обработчике только при `platform==Linux` и `launch_cli`. **Root не
  нужен**: базовые сенсоры идут через hwmon, capability для dmidecode мастер
  ставит на шаге установки.
- **Lessons**: для запуска CLI из браузерного мастера на Linux нужен
  терминал-эмулятор + графическая сессия (в отличие от Windows, где WebView2
  закрывает окно и запускает PowerShell сам). Список терминалов должен иметь
  fallback — Astra Fly использует konsole через `x-terminal-emulator`.

### #29. Новый элемент UI «не появляется» после rebuild — кэш браузера (нет Cache-Control)

- **Симптом**: после пересборки `.deb` с новым CLI-чекбоксом (#28) на шаге 6
  мастера чекбокс **не появился**, хотя установленный `done.js` байт-в-байт
  совпадал с `src/` (`cmp` IDENTICAL) и сервер реально отдавал новый файл.
- **Коммит**: `adb9051`
- **Причина**: `/static` монтировался голым `StaticFiles` **без `Cache-Control`**.
  Браузер эвристически кэширует ES-модули и отдавал старый `done.js` (без
  чекбокса), не перезапрашивая. Усугубляло то, что `.deb`-сборка (debhelper,
  reproducible builds) **пинит mtime файлов к дате changelog** → `Last-Modified`
  не менялся между патчами одной версии (1.0.0), и conditional-ревалидация по
  времени не срабатывала. ETag менялся (по контенту), но без `Cache-Control`
  браузер вообще не слал conditional-запрос.
- **Фикс**: `server.py` — подкласс `_NoCacheStaticFiles(StaticFiles)`,
  проставляющий `Cache-Control: no-cache` на ответы `/static`. `no-cache` ≠
  `no-store`: кэш разрешён, но требует ревалидации по ETag (дешёвый 304, когда
  не менялось; 200 с новым контентом после upgrade). Тест: `test_webui_static_cache.py`.
  Пользователю для сброса уже залипшего модуля — один Ctrl+Shift+R; дальше
  свежесть держится сама.
- **Lessons**: (1) если «новый UI-элемент не появляется» после пересборки, а
  файл на диске свежий — **первый подозреваемый кэш браузера, не код**;
  проверять `curl -I` заголовки + содержимое того, что реально отдаёт сервер.
  (2) Любой self-served WebUI обязан слать `Cache-Control: no-cache` (или
  versioned-asset стратегию) на JS/HTML — иначе после `apt upgrade`/reinstall
  у пользователя залипает старый код. (3) `.deb`/reproducible-builds пинят mtime
  → на `Last-Modified` для cache-busting полагаться нельзя, только ETag+revalidate.

### #30. (зарезервировано для следующей Astra-находки)

---

## Memory / pattern fixes (cross-cutting)

### #M.1. Кросс-shell BOM/LF normalization

- **Статус**: устранено в коммите `acec2cf` (до Astra-теста)
- Все `.ps1` с кириллицей имеют UTF-8 BOM (иначе PowerShell 5.1 ru-RU mojibake)
- `.gitattributes` форсит `eol=lf` для `*.sh` и `debian/*` (иначе Git autocrlf
  ломает шибанг на Linux: `#!/bin/sh\r` → bad interpreter)
- **Lessons**: development на Windows + deploy на Linux — обязательно нужны
  `.gitattributes` с явными правилами line-endings

---

## Шаблон для новой записи

```markdown
### #N. <короткое название симптома>

- **Симптом**: <что увидел пользователь>
- **Коммит**: <hash>
- **Причина**: <корневая причина>
- **Фикс**: <что изменено>
- **Lessons**: <вывод на будущее>
```
