# Troubleshooting сенсорного слоя apexcore

Этот документ — практический справочник «что делать когда не работает».
Структура — дерево решений по `DegradedReason`, который вы видите в
выводе `apexcore doctor`. Для архитектурного обзора смотрите
[ARCHITECTURE.md](../ARCHITECTURE.md) раздел «Сенсоры температур».

Команда:

```powershell
apexcore doctor
```

Выводит:
- статус каждого источника (HWiNFO/CoreTemp/LHM/WMI/...);
- блок «Обнаруженные проблемы» — список конкретных `DegradedReason`;
- блок «Что сделать» — дифференцированные советы.

Для автоматического исправления попробуйте `apexcore doctor --repair`.

---

## Дерево решений по DegradedReason

### `NO_LHM_DLL` — DLL LibreHardwareMonitor не найдена в `sensors/lib/`

С v0.5.1 DLL коммитятся в git, поэтому при свежем checkout они должны
быть. Если их нет — вы либо обновляетесь с более старой версии, либо
руками удалили `lib/*.dll`.

**Решение:**

```powershell
# Авто (рекомендуется):
apexcore doctor --repair

# Или вручную:
powershell -ExecutionPolicy Bypass -File scripts\fetch_lhm.ps1
```

Скрипт скачивает LHM v0.9.6 с GitHub Releases (~700 КБ), проверяет
SHA256 и кладёт DLL в `src/apexcore/infrastructure/sensors/lib/`.

---

### `NO_DOTNET_RUNTIME` — pythonnet не нашёл .NET

LHM работает через pythonnet, которому нужен .NET runtime. По умолчанию
apexcore использует **.NET Framework 4.8** (предустановлен в Win10/11).

**Если у вас Windows 10 LTSC / корпоративный образ без .NET:**

Установите .NET 9 Desktop Runtime: https://dotnet.microsoft.com/download

Если вы установили apexcore через Inno Setup installer — в комплекте
идёт bundled .NET 9 framework-dependent runtime в `<install>/dotnet/`,
и `lhm._configure_runtime` подхватит его автоматически.

Для dev-окружения можно явно указать путь:

```powershell
$env:APEXCORE_DOTNET_ROOT = "C:\path\to\dotnet"
```

В этой папке должен быть `apexcore.runtimeconfig.json` (создаётся
`scripts/fetch_dotnet9.ps1`).

---

### `HVCI_BLOCKED` — Memory Integrity блокирует WinRing0

HVCI / Memory Integrity по умолчанию включена в Windows 11 24H2 на
чистой установке. Под ней kernel-driver WinRing0 (внутри LHM) **не
загружается в принципе** — он подписан истёкшим сертификатом OpenLibSys.

**Решения, в порядке предпочтения:**

1. **Установите PawnIO** — signed kernel-driver, совместимый с HVCI/SAC.
   LHM v0.9.6 умеет с ним работать без правок.

   Скачать: https://pawnio.eu

2. **Установите HWiNFO** (https://www.hwinfo.com) — у него собственный
   подписанный драйвер `HWiNFO64A.SYS`, совместимый с HVCI. Запустите
   в Sensors-only mode, включите Settings → Shared Memory Support.
   apexcore автоматически прочитает данные через SHM.

3. **Установите CoreTemp** (https://www.alcpu.com/CoreTemp/) — лёгкая
   альтернатива (~3 МБ).

4. (Не рекомендуется) Выключить Memory Integrity в Windows Security →
   Device Security → Core Isolation. Снижает безопасность системы.

---

### `SAC_BLOCKED` — Smart App Control блокирует драйверы

Smart App Control (Win 11) разрешает только signed reputable kernel-
drivers. SAC можно только **выключить один раз** (без обратного
включения без переустановки Windows).

**Решение:** установить PawnIO (https://pawnio.eu) или HWiNFO / CoreTemp
(тот же путь что и для HVCI).

---

### `DEFENDER_BLOCKED` — Defender карантинит WinRing0

Microsoft Defender помечает `WinRing0x64.sys` как
`VulnerableDriver:WinNT/Winring0.A` (или B/C/D/E/F/G — серия сигнатур
Feb-Mar 2025). Это **корректное** поведение Defender — у WinRing0 есть
неисправленная CVE-2020-14979 (escalation of privilege).

**Решение:**

1. Установите PawnIO (https://pawnio.eu) как замену WinRing0 — это
   signed-driver без известных CVE.
2. Альтернатива: HWiNFO с его собственным драйвером.

Не рекомендуется добавлять WinRing0 в исключения Defender — CVE
реальная.

---

### `AV_BLOCKED` — Сторонний антивирус блокирует драйвер

Avast / AVG / Kaspersky / Bitdefender часто блокируют WinRing0 по тем же
причинам что и Defender (плюс anti-cheat системы — Riot Vanguard,
EasyAntiCheat).

**Решение:**

1. Добавить исключение для папки с LHM DLL: `src/apexcore/
   infrastructure/sensors/lib/` в настройках AV.
2. Или установить PawnIO / HWiNFO как альтернативу.

---

### `NO_ADMIN` — Нет прав администратора

Регистрация WinRing0 как kernel-сервиса (одноразовая операция) требует
admin. После первого admin-запуска `WinRing0_1_2_0` остаётся
зарегистрированным, и дальше apexcore работает без UAC.

**Решение:**

```powershell
# Один раз — от админа (UAC). Самый простой способ: правый клик по
# ярлыку ApexCore на рабочем столе → «Запуск от имени администратора».
apexcore

# Дальше — обычным юзером:
apexcore
```

Если ставили через Inno-installer с галкой `apexcore_sensord` — admin не
нужен, сервис уже зарегистрирован.

---

### `COM_INIT_FAILED` — WMI COM-апартмент не инициализируется

Редкий сценарий: ``import wmi`` в background-thread без CoInitialize.
apexcore уже обрабатывает это через флаг `_WMI_PACKAGE_BROKEN` (см.
`ARCHITECTURE.md`) — переключается на CIM-fallback через PowerShell. Если
ошибка всё-таки видна — pull-request приветствуется.

---

### `CPU_UNSUPPORTED` — CPU не распознан LibreHardwareMonitor

Например, Intel Panther Lake (PR #2332 в LHM ещё не смержен) или
новейший AMD Zen на момент релиза apexcore.

**Решения:**

1. Обновите apexcore — мы выпускаем релизы с свежей LHM v0.9.6+.
2. Используйте HWiNFO — он обычно поддерживает новейшие CPU быстрее
   чем LHM.
3. Откройте issue в репозитории apexcore с моделью вашего CPU.

---

### `ACPI_FAKE_ZONE` — OEM DSDT публикует фейковую температуру

На многих ноутбуках OEM в DSDT оставляет 1-2 «декоративные» thermal
zone, отдающие константы 25-30 °C даже под полной нагрузкой. Это **не
баг WMI** — реальные DTS per-core просто не подключены к ACPI thermal
zones. См. ресерч §2.5.

apexcore детектит это автоматически: значения в диапазоне 25.0-30.0 °C
помечаются как `approximate` (а не silicon).

**Решение:** установите HWiNFO или CoreTemp — они читают реальные DTS
через kernel-driver.

---

### `ARM_PLATFORM` — Windows на Snapdragon X / SQ1-SQ3

На ARM64 Windows прямой доступ к Tj-сенсорам **технически невозможен**:
Qualcomm SPU + Microsoft Pluton TPM + Total Memory Encryption блокируют
SoC-регистры. Публичного API от Qualcomm нет.

apexcore ограничивается одной ACPI thermal zone (~chassis temperature,
обновляется ~1 Гц, точность ±5 °C), помеченной как `approximate`.

Это hard limit — никакой workaround в архитектуре MIT-приложения
невозможен. Подробнее: ресерч §2.6 и §6.

---

## Проверки на отдельные источники

### HWiNFO Shared Memory недоступен

1. HWiNFO запущен? (`Get-Process HWiNFO64`)
2. Sensors-only mode выбран при запуске?
3. Settings → Shared Memory Support включён?
4. Free-версия имеет лимит **12 часов** работы SHM подряд. Перезапустите
   HWiNFO если он работает дольше.

### CoreTemp Shared Memory недоступен

1. CoreTemp запущен? (`Get-Process CoreTemp`)
2. CoreTemp ≥ 1.7? (старые версии используют `CoreTempMappingObject`
   без `Ex`, apexcore читает `CoreTempMappingObjectEx`).
3. CoreTemp нужен с admin-правами (он сам это запросит).

### LHM DLL загружается, но CPU-сенсоров нет

1. Запустите `apexcore doctor` — он покажет конкретный `DegradedReason`
   (HVCI/SAC/Defender/no_admin).
2. Следуйте советам по дереву решений выше.

---

## Откат к стабильной версии

Если что-то сломалось после обновления:

```powershell
git fetch
git reset --hard v0.5.0  # последний save-point перед стабилизацией сенсоров
```

Тег `v0.5.0` зафиксирован — не удалять.
