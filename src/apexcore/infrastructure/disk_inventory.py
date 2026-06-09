"""Кросс-платформенный перечень физических дисков для раздела «Датчики».

LHM и smartctl могут не видеть всех дисков (зависит от драйверов, прав
доступа, версии прошивки). Этот модуль перечисляет **все физические
диски** независимо от того, удалось ли с них считать температуру —
чтобы пользователь видел полный набор накопителей, а не только тот
что отдал LHM.

Поставляется как ``list[PhysicalDisk]`` с моделью, типом и буквами
дисков. Сопоставление с температурными показаниями делается в
``interfaces/cli/render_sensors.py`` по совпадению ``model`` или индекса.

Реализация:

- **Windows**: PowerShell ``Get-PhysicalDisk`` + ``Get-Partition`` через
  subprocess. PowerShell доступен из коробки на Win10/11; ``Get-PhysicalDisk``
  даёт BusType (NVMe/SATA/USB) и MediaType (SSD/HDD/SCM) точнее чем
  старый Win32_DiskDrive.
- **Linux/AstraLinux**: ``lsblk -J -d -o NAME,MODEL,TRAN,ROTA,SIZE,MOUNTPOINT``
  (JSON) — тот же набор полей. Mount-точки вместо букв.
- **Любая ошибка** → пустой список. Не пробрасываем исключения.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PhysicalDisk:
    """Физический диск с буквами/mount-точками и характеристиками."""

    index: int                 # PhysicalDriveN на Windows; порядковый на Linux
    model: str                 # "Kingston SKC3000D2048G"
    bus_type: str              # "NVMe" | "SATA" | "SAS" | "USB" | "SCSI" | ""
    media_type: str            # "SSD" | "HDD" | "SCM" | "Unspecified" | ""
    size_gb: float | None      # Объём в ГБ
    letters: list[str] = field(default_factory=list)  # ["C:", "D:"] или ["/", "/home"]

    @property
    def display_type(self) -> str:
        """Человекочитаемый тип: ``SSD M.2 NVMe``, ``HDD SATA``, ``SSD USB``..."""
        media = self.media_type or ""
        bus = self.bus_type or ""
        # Самые частые комбинации.
        if bus.upper() == "NVME":
            # На потребительском железе NVMe ≈ M.2 в подавляющем большинстве случаев,
            # но мы это формально не определяем — оставляем «NVMe».
            return "SSD NVMe" if media == "SSD" or not media else f"{media or 'Disk'} NVMe"
        if bus.upper() == "SATA":
            if media == "HDD":
                return "HDD SATA"
            if media == "SSD":
                return "SSD SATA"
            return f"{media or 'SATA'}"
        if bus.upper() == "USB":
            return f"{media or 'USB'} USB"
        # Generic fallback.
        parts = [p for p in (media, bus) if p]
        return " ".join(parts) if parts else "Диск"

    @property
    def display_title(self) -> str:
        """Заголовок для UI: ``[C:, D:] Kingston KC3000 2TB · SSD NVMe``."""
        letters_part = ""
        if self.letters:
            letters_part = f"[{', '.join(self.letters)}] "
        model = self.model or f"Диск {self.index}"
        type_str = self.display_type
        if type_str:
            return f"{letters_part}{model} · {type_str}"
        return f"{letters_part}{model}"

    @property
    def display_title_compact(self) -> str:
        """Компактный заголовок без типа диска: ``[C:] Kingston KC3000``.

        Тип (SSD/HDD/NVMe/SATA) опускаем — для глазного скана таблицы хватает
        буквы и модели. Полная форма с типом остаётся в ``display_title``
        для focus mode и debug-вывода.
        """
        letters_part = ""
        if self.letters:
            letters_part = f"[{', '.join(self.letters)}] "
        model = self.model or f"Диск {self.index}"
        return f"{letters_part}{model}"


def get_boot_drive_path() -> str:
    """Вернуть путь к корню загрузочного диска (где стоит ОС).

    - **Windows**: ``%SystemDrive%`` (обычно ``C:``). Возвращается с
      обратным слэшем — ``C:\\`` — чтобы был валидный путь для записи и
      ``shutil.disk_usage``. Fallback: ``Path.home().drive`` + ``\\``.
    - **Linux/AstraLinux**: ``"/"`` (mount-point root-FS).

    Не возвращает ``None``: на любой ОС какой-то загрузочный диск всегда
    есть, иначе процесс просто не запустился бы.
    """
    if sys.platform == "win32":
        # На Windows реальные env-vars регистронезависимые, но os.environ в
        # Python использует точное соответствие — обходим перебором ключей.
        drive = ""
        for key, value in os.environ.items():
            if key.upper() == "SYSTEMDRIVE":
                drive = value.strip()
                break
        if not drive:
            drive = Path.home().drive
        if not drive:
            drive = "C:"
        if not drive.endswith((":", "\\")):
            drive = drive + ":"
        return drive + "\\"
    return "/"


def get_boot_drive(
    disks: list[PhysicalDisk] | None = None,
) -> tuple[str, PhysicalDisk | None]:
    """Найти загрузочный диск + его метаданные (model/media_type/bus_type).

    Возвращает ``(boot_path, physical_disk_or_None)``. Если ``disks`` не
    задан, вызывает :func:`list_physical_disks` под капотом.

    Матчинг (только Windows) — по буквам диска в ``PhysicalDisk.letters``.
    Если буква загрузочного диска ни с одним физическим диском не
    совпала — возвращается ``(boot_path, None)``; вызывающий должен
    использовать ``UNKNOWN_PROFILE`` из :mod:`infrastructure.disk_peak`.

    На Linux матчинг по mount-point ``/`` (``letters == ["/"]``).
    """
    boot_path = get_boot_drive_path()
    if disks is None:
        disks = list_physical_disks()

    # Нормализуем «букву» (Windows: "C:"; Linux: "/").
    if sys.platform == "win32":
        # boot_path == "C:\\" → нам нужно "C:"
        match_key = boot_path.rstrip("\\").rstrip("/").upper()
    else:
        match_key = boot_path  # "/"

    for d in disks:
        for letter in d.letters:
            if letter.upper() == match_key:
                return (boot_path, d)
    return (boot_path, None)


def list_physical_disks() -> list[PhysicalDisk]:
    """Перечислить все физические диски системы.

    Возвращает список PhysicalDisk с моделью, типом, буквами/mount-точками.
    Пустой список при любой ошибке (PowerShell недоступен, lsblk нет, и т.п.).
    """
    if sys.platform == "win32":
        return _list_windows_disks()
    return _list_linux_disks()


# ─── Windows ──────────────────────────────────────────────────────────────


_PS_DISKS_SCRIPT = r"""
# КРИТИЧНО: Get-PhysicalDisk.DeviceId и Get-Disk.Number — РАЗНЫЕ нумерации
# (наблюдено на Z690: DeviceId 0,1,2,3 vs Number 0,1,3,4). Get-Partition
# использует .DiskNumber из Get-Disk.Number, поэтому именно Number — наш
# первичный ключ диска. Get-PhysicalDisk берём для BusType/MediaType,
# join с Get-Disk по FriendlyName.

# Стратегия match Get-Disk ↔ Get-PhysicalDisk:
# 1) exact FriendlyName/Model — основной случай;
# 2) fallback — самый близкий по Size в пределах 3% (для Storage Spaces:
#    virtual disk «M2 · Spaces» сидит поверх физического Kingston).
#    `Sort-Object | Select -First 1` гарантирует что выбран САМЫЙ близкий,
#    а не первый попавшийся в допуске (иначе 2TB HDD матчится с 2TB virtual);
# 3) каждый PhysicalDisk used max один раз — `$claimed` отслеживает.

$pdList = @(Get-PhysicalDisk)
$claimed = @{}

function Find-MatchingPhysicalDisk($disk) {
    $name = if ($disk.FriendlyName) { $disk.FriendlyName.Trim() } else { '' }
    $available = $pdList | Where-Object { -not $claimed.ContainsKey([string]$_.DeviceId) }

    if ($name) {
        $byName = $available | Where-Object {
            ($_.FriendlyName -and $_.FriendlyName.Trim() -eq $name) -or
            ($_.Model -and $_.Model.Trim() -eq $name)
        } | Select-Object -First 1
        if ($byName) {
            $claimed[[string]$byName.DeviceId] = $true
            return $byName
        }
    }
    if ($disk.Size -gt 0) {
        $target = [double]$disk.Size
        $closest = $available | Where-Object { $_.Size -gt 0 } | Sort-Object {
            [math]::Abs([double]$_.Size - $target)
        } | Select-Object -First 1
        if ($closest -and ([math]::Abs([double]$closest.Size - $target) / $target) -lt 0.03) {
            $claimed[[string]$closest.DeviceId] = $true
            return $closest
        }
    }
    return $null
}

$disks = Get-Disk | ForEach-Object {
    $disk = $_
    $pd = Find-MatchingPhysicalDisk $disk
    [PSCustomObject]@{
        DeviceId     = $disk.Number              # = DiskNumber из Get-Partition
        FriendlyName = if ($pd) { $pd.FriendlyName } else { $disk.FriendlyName }
        Model        = if ($pd) { $pd.Model } else { $disk.FriendlyName }
        BusType      = if ($pd) { $pd.BusType } else { $disk.BusType }
        MediaType    = if ($pd) { $pd.MediaType } else { '' }
        Size         = $disk.Size
    }
}

# Маппинг буква → DiskNumber через classic WMI Win32_LogicalDiskToPartition.
# Modern Get-Partition не отдаёт DriveLetter для Bitlocker-зашифрованных
# системных разделов (наблюдено: C: на NE-1TB пропадал). WMI же видит ВСЕ
# logical-to-partition mappings — это API ещё c WinNT, max-совместимое.
# Антецедент: Win32_DiskPartition.DeviceID="Disk #N, Partition #M"
# Зависимое:  Win32_LogicalDisk.DeviceID="C:"
$parts = @()
$wmiSeen = @{}
foreach ($rel in Get-CimInstance Win32_LogicalDiskToPartition -ErrorAction SilentlyContinue) {
    $ant = "$($rel.Antecedent.DeviceID)"
    $dep = "$($rel.Dependent.DeviceID)"
    if ($ant -match 'Disk\s*#(\d+)') {
        $diskNum = [int]$Matches[1]
        $letter = $dep.TrimEnd(':')
        $key = "$diskNum/$letter"
        if (-not $wmiSeen.ContainsKey($key)) {
            $wmiSeen[$key] = $true
            $parts += [PSCustomObject]@{
                DiskNumber  = $diskNum
                DriveLetter = $letter
            }
        }
    }
}

# Fallback через AccessPaths — на случай если CIM не сработает или диск
# не имеет logical-to-partition mapping (storage spaces virtual disk).
$allParts = Get-Partition -ErrorAction SilentlyContinue
foreach ($vol in Get-Volume -ErrorAction SilentlyContinue | Where-Object DriveLetter) {
    $letter = "$($vol.DriveLetter)"
    if ($wmiSeen.Values | Where-Object { $_ } | ForEach-Object {} -end {}) {}  # noop
    # Если этой буквы ещё нет — пытаемся через AccessPaths.
    $alreadyMapped = $false
    foreach ($p in $parts) { if ($p.DriveLetter -eq $letter) { $alreadyMapped = $true; break } }
    if ($alreadyMapped) { continue }
    $path = $vol.Path
    $part = $allParts | Where-Object { $_.AccessPaths -contains $path } | Select-Object -First 1
    if (-not $part) {
        try { $part = Get-Partition -DriveLetter $letter -ErrorAction Stop } catch { $part = $null }
    }
    if ($part) {
        $parts += [PSCustomObject]@{
            DiskNumber  = $part.DiskNumber
            DriveLetter = $letter
        }
    }
}

@{
    Disks      = @($disks)
    Partitions = @($parts)
} | ConvertTo-Json -Depth 4
"""


# BusType-коды из MSFT_PhysicalDisk (только частые на потребительском железе).
_BUS_TYPE_MAP = {
    1: "SCSI",
    2: "ATAPI",
    3: "ATA",
    4: "1394",
    5: "SSA",
    6: "Fibre",
    7: "USB",
    8: "RAID",
    9: "iSCSI",
    10: "SAS",
    11: "SATA",
    12: "SD",
    13: "MMC",
    15: "FileBackedVirtual",
    16: "StorageSpaces",
    17: "NVMe",
    18: "SCM",
}

_MEDIA_TYPE_MAP = {
    0: "Unspecified",
    3: "HDD",
    4: "SSD",
    5: "SCM",
}


def _coerce_enum(value: object, code_map: dict[int, str]) -> str:
    """Привести значение `BusType`/`MediaType` от Get-PhysicalDisk к строке.

    На современном Windows PowerShell ConvertTo-Json сериализует .NET-enum
    как строку (``"NVMe"``, ``"SSD"``). На старых — как int. Поддерживаем
    оба формата + игнорируем «Unknown»/«Unspecified» (возвращаем "").
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return ""
    if isinstance(value, int):
        result = code_map.get(value, "")
        return "" if result in ("Unspecified", "Unknown") else result
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned or cleaned.lower() in ("unknown", "unspecified"):
            return ""
        return cleaned
    return ""


def _list_windows_disks() -> list[PhysicalDisk]:
    out = _run_powershell(_PS_DISKS_SCRIPT, timeout=8.0)
    if out is None:
        return []
    try:
        data = json.loads(out)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.debug("Get-PhysicalDisk вернул не-JSON: %s", exc)
        return []

    raw_disks = data.get("Disks") or []
    raw_parts = data.get("Partitions") or []
    if isinstance(raw_disks, dict):
        raw_disks = [raw_disks]
    if isinstance(raw_parts, dict):
        raw_parts = [raw_parts]

    # Соберём letters per disk-number.
    letters_by_disk: dict[int, list[str]] = {}
    for p in raw_parts:
        if not isinstance(p, dict):
            continue
        try:
            num = int(p.get("DiskNumber"))
        except (TypeError, ValueError):
            continue
        letter = p.get("DriveLetter")
        if not letter:
            continue
        if isinstance(letter, int):
            letter = chr(letter)
        letter = str(letter).strip()
        if not letter:
            continue
        if not letter.endswith(":"):
            letter = f"{letter}:"
        letters_by_disk.setdefault(num, []).append(letter)

    disks: list[PhysicalDisk] = []
    for d in raw_disks:
        if not isinstance(d, dict):
            continue
        device_id = d.get("DeviceId")
        try:
            index = int(device_id)
        except (TypeError, ValueError):
            continue
        model = (
            d.get("FriendlyName") or d.get("Model") or f"PhysicalDrive{index}"
        )
        if isinstance(model, str):
            model = model.strip()
        # Get-PhysicalDisk возвращает BusType/MediaType как enum-string в JSON
        # ("NVMe", "SATA", "SSD"), но на старых билдах PowerShell — как int.
        bus_type = _coerce_enum(d.get("BusType"), _BUS_TYPE_MAP)
        media_type = _coerce_enum(d.get("MediaType"), _MEDIA_TYPE_MAP)
        size_b = d.get("Size")
        size_gb: float | None = None
        if isinstance(size_b, (int, float)) and size_b > 0:
            size_gb = float(size_b) / (1024**3)
        letters = sorted(letters_by_disk.get(index, []))
        disks.append(
            PhysicalDisk(
                index=index,
                model=str(model),
                bus_type=bus_type,
                media_type=media_type,
                size_gb=size_gb,
                letters=letters,
            )
        )
    # Сортируем по index — стабильный порядок в UI.
    disks.sort(key=lambda d: d.index)
    return disks


# ─── Linux ────────────────────────────────────────────────────────────────


def _list_linux_disks() -> list[PhysicalDisk]:
    """Через ``lsblk -J -d -o NAME,MODEL,TRAN,ROTA,SIZE``.

    На AstraLinux lsblk доступен из коробки. Mount-точки берём отдельным
    запросом для каждого устройства (через ``-J -o NAME,MOUNTPOINTS``
    без ``-d`` чтобы увидеть partition'ы).
    """
    out = _run_subprocess(
        ["lsblk", "-J", "-d", "-b", "-o", "NAME,MODEL,TRAN,ROTA,SIZE"],
        timeout=3.0,
    )
    if out is None:
        return []
    try:
        data = json.loads(out)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.debug("lsblk вернул не-JSON: %s", exc)
        return []

    raw = data.get("blockdevices") or []
    if not isinstance(raw, list):
        return []

    # Mount-points отдельным запросом (с partition'ами).
    mounts_by_disk = _list_linux_mountpoints()

    disks: list[PhysicalDisk] = []
    for idx, dev in enumerate(raw):
        if not isinstance(dev, dict):
            continue
        name = dev.get("name") or f"disk{idx}"
        model = (dev.get("model") or "").strip() or f"Диск {name}"
        tran = (dev.get("tran") or "").upper()  # nvme/sata/usb/...
        rota = dev.get("rota")
        media = "HDD" if rota in (1, "1", True) else "SSD"
        size_b = dev.get("size")
        size_gb: float | None = None
        try:
            if size_b is not None and float(size_b) > 0:
                size_gb = float(size_b) / (1024**3)
        except (TypeError, ValueError):
            size_gb = None
        # bus_type
        bus_alias = {"NVME": "NVMe", "SATA": "SATA", "USB": "USB", "SAS": "SAS"}
        bus_type = bus_alias.get(tran, tran)
        letters = sorted(mounts_by_disk.get(str(name), []))
        disks.append(
            PhysicalDisk(
                index=idx,
                model=model,
                bus_type=bus_type,
                media_type=media,
                size_gb=size_gb,
                letters=letters,
            )
        )
    return disks


def _list_linux_mountpoints() -> dict[str, list[str]]:
    """Mount-точки по физическому диску (партиции объединяются в его список)."""
    out = _run_subprocess(
        ["lsblk", "-J", "-o", "NAME,MOUNTPOINTS"], timeout=3.0
    )
    if out is None:
        return {}
    try:
        data = json.loads(out)
    except (json.JSONDecodeError, TypeError):
        return {}

    result: dict[str, list[str]] = {}
    for dev in data.get("blockdevices") or []:
        if not isinstance(dev, dict):
            continue
        name = dev.get("name")
        if not name:
            continue
        mounts: list[str] = []
        for partition in dev.get("children") or []:
            if not isinstance(partition, dict):
                continue
            for mp in partition.get("mountpoints") or []:
                if mp and isinstance(mp, str):
                    mounts.append(mp)
        # Самого диска mount-точки тоже учитываем (для дисков без partition'ов).
        for mp in dev.get("mountpoints") or []:
            if mp and isinstance(mp, str):
                mounts.append(mp)
        if mounts:
            result[str(name)] = mounts
    return result


# ─── helpers ──────────────────────────────────────────────────────────────


_NO_WINDOW_FLAG = 0
if sys.platform == "win32":
    _NO_WINDOW_FLAG = 0x08000000  # CREATE_NO_WINDOW


def _run_powershell(script: str, timeout: float = 5.0) -> str | None:
    """Запустить inline-скрипт PowerShell и вернуть stdout. None при ошибке."""
    cmd = [
        "powershell",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy", "Bypass",
        "-Command", script,
    ]
    return _run_subprocess(cmd, timeout=timeout)


def _run_subprocess(cmd: list[str], timeout: float = 5.0) -> str | None:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            creationflags=_NO_WINDOW_FLAG,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("subprocess failed (%s): %s", cmd[0] if cmd else "?", exc)
        return None
    return proc.stdout or None
