# Astra Linux 1.8 — build/install pitfalls

Краткая шпаргалка типичных граблей при сборке `.deb` и установке apexcore
на **Astra Linux SE 1.8.5.46 «Орёл защищенности»** (бюллетень **№
2026-0224SE18**, **11.02.2026**, ядро **6.1.158-1-generic**, Debian 12
base). Каждый пункт связан с конкретной находкой в
[`problems_fixes.md`](problems_fixes.md) — там симптомы, причины,
коммиты с фиксами. Полный паспорт тестового стенда (железо, что
доступно из коробки, известные пробелы покрытия) —
[`test_environment.md`](test_environment.md).

## Build phase

1. **debian/compat + Build-Depends: debhelper-compat** → double-declaration error
   → Использовать **только** Build-Depends, файл `debian/compat` удалить.
   _См. [#2](problems_fixes.md#2-debhelper-compat-double-declaration)._

2. **Установка в `/usr/local/bin/`** → `dh_usrlocal` блокирует по Debian Policy
   → Использовать `/usr/bin/<wrapper>`, `/usr/local/` зарезервирован для локального админа.
   _См. [#3](problems_fixes.md#3-dh_usrlocal-usrlocal-для-пакетов-запрещён)._

3. **dh_dwz падает на manylinux wheels** (numpy/scipy native `.so` без DWARF info)
   → `override_dh_dwz: @true` + `export DEB_BUILD_OPTIONS = nostrip nodwz noddebs` в `rules`.
   _См. [#4](problems_fixes.md#4-dh_dwz-падает-на-manylinux-wheels)._

4. **dh_compress ломает venv** — сжимает `.py`/`.pyc` внутри venv → broken imports
   → `override_dh_compress: @true` для embedded-venv пакетов.

5. **Build-deps на Astra** не идут автоматически в Recommends. Перед сборкой:
   ```
   sudo apt install -y dh-python debhelper devscripts build-essential \
                       fakeroot libcap2-bin polkit imagemagick python3-venv
   ```

6. **dpkg-buildpackage кладёт `.deb` в parent**, наш `build_astra.sh` mv-ит в
   `new-app/dist/` (двух-уровневый репо: корень + `new-app/`, не корневая `dist/`).

## Install / runtime phase

7. **`/usr/sbin` не в PATH у обычного пользователя на Astra SE**
   (PARSEC policy может сбрасывать PATH даже после `export` в `.bashrc`)
   → В коде использовать `which_with_sbin()` из `infrastructure/sbin_lookup.py`.
   _См. [#5](problems_fixes.md#5-usrsbin-не-в-path-у-обычного-пользователя)._

8. **AMD APU iGPU температура** живёт в `/sys/class/hwmon/hwmonN/` где name=`amdgpu`
   → `linux.py::_read_hwmon` префиксит `gpu/<chip>/<label>` для `amdgpu`/`radeon`/`i915`/`xe`.
   _См. [#6](problems_fixes.md#6-amd-igpu-температура-нет-данных-при-наличии-amdgpu-hwmon)._

9. **smartctl на NVMe требует capability** — без `setcap cap_sys_rawio+ep`
   smartctl видит устройство, но не получает SMART data
   → Wizard ставит cap через `pkexec` на шаге Progress (Components → setcap для smartctl/dmidecode).
   Ручной путь: `sudo setcap cap_sys_rawio+ep /usr/sbin/smartctl`.

## Build/install последовательность (cheatsheet)

### Первая установка с нуля

```bash
# 1. Один раз — system-deps:
sudo apt install -y dh-python debhelper devscripts build-essential \
                    fakeroot libcap2-bin polkit imagemagick python3-venv

# 2. Клонирование:
git clone https://github.com/Lelbry/apexcore.git
cd benchmark_by_lelbry
git checkout v0.8.7         # или последний тег

# 3. Сборка + установка одной командой (см. ниже):
bash new-app/scripts/astra_rebuild_install.sh

# 4. CLI smoke:
apexcore info
apexcore doctor

# 5. Wizard для capabilities + sensors-detect:
apexcore setup
# pkexec прозвонит несколько раз → вводить пароль
```

### Итерационный цикл (pull → rebuild → reinstall)

Во время разработки этот цикл выполняется десятки раз — поэтому
обёрнут в скрипт-заклинатель `scripts/astra_rebuild_install.sh`:

```bash
cd ~/benchmark_by_lelbry

# Подтянуть origin/dev + полная пересборка + переустановка:
bash new-app/scripts/astra_rebuild_install.sh

# То же, но на конкретный коммит/тег:
bash new-app/scripts/astra_rebuild_install.sh v0.8.7
bash new-app/scripts/astra_rebuild_install.sh abc1234

# Локальные правки без git pull:
bash new-app/scripts/astra_rebuild_install.sh --no-reset
```

Что скрипт делает (7 шагов):
1. `git fetch + reset --hard` на указанную ссылку (по умолчанию `origin/dev`);
2. `pkill` запущенным `apexcore webui` / `apexcore setup` — иначе reinstall
   молча оставит **старый** код в памяти живого процесса;
3. чистка `debian/apexcore`, `debian/.debhelper`, `debian/files`,
   `debian/*.substvars` — без этого dpkg-buildpackage может реюзать
   старые артефакты сборки и не подхватить свежий код;
4. `export DEB_BUILD_OPTIONS="nostrip nodwz noddebs"` + `build_astra.sh`;
5. `cp .deb` → `/tmp/` (избегаем `_apt` sandbox warning, см.
   [#7](problems_fixes.md#7-_apt-sandbox-warning-при-apt-install-localdeb-из-));
6. `sudo apt install --reinstall -y /tmp/apexcore_*.deb`;
7. sanity-проверки: версия пакета через `dpkg -l apexcore`, наличие
   `_HWMON_CPU_CHIPS` в установленном `linux.py` (= sensor pipeline
   актуален), наличие `static/index.html` в установленном пакете
   (= WebUI не покажет «Static UI not bundled»).

После — `apexcore doctor` / `apexcore webui` / `apexcore micro run`
запускать вручную.
