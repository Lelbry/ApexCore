; Inno Setup script для apexcore (silent engine для WebView2 bootstrapper'a).
; Собирать: iscc packaging\windows\installer.iss
; Перед сборкой:
;   1) pwsh -File scripts/sync_version.ps1     — генерит build/version.iss
;   2) pwsh -File scripts/build_branding.ps1   — генерит build/branding/*
;   3) pyinstaller packaging/windows/apexcore.spec
;   4) pyinstaller packaging/windows/apexcore-sensord.spec

#define MyAppName "ApexCore"
; Версия читается из build/version.iss — единый source of truth (pyproject.toml).
; Файл генерируется scripts/sync_version.ps1. Если файла нет — fallback на 0.0.0,
; iscc упадёт с понятной ошибкой.
#ifexist "..\..\build\version.iss"
  #include "..\..\build\version.iss"
#else
  #define MyAppVersion "0.0.0-dev"
  #pragma warning "build/version.iss не найден — версия не синхронизирована с pyproject.toml. " + \
                  "Запустите scripts/sync_version.ps1 перед iscc."
#endif
#define MyAppPublisher "lelbry"
#define MyAppURL "https://github.com/Lelbry/apexcore"
#define MyAppExeName "apexcore.exe"
#define DistDir "..\..\dist\apexcore"
; Standalone PyInstaller-бандл sensord-сервиса. Кладётся в подкаталог
; {app}\apexcore-sensord\ — install_sensord_bundle.ps1 ищет его именно там.
#define SensordDistDir "..\..\dist\apexcore-sensord"

[Setup]
; AppId стабилен между версиями — installer считает новые версии обновлением
; и переустанавливает поверх, реюзая старый каталог установки по AppId
; (см. GetExistingDir в [Code]).
AppId={{C7D9D6F1-8F2E-4B5A-9F3F-77B9B1A1B8AF}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; DisableDirPage=no — всегда показывать выбор папки, даже при upgrade.
; Иначе Inno при наличии prior install (тот же AppId) автоматически скипает
; страницу и ставит в старый путь; для rebrand v0.8.x→v0.9.0 это вводит в
; заблуждение (пользователь не видит куда ставится новый ApexCore).
DisableDirPage=no
OutputDir=..\..\dist\installer
OutputBaseFilename=apexcore-setup-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64
WizardStyle=modern
PrivilegesRequired=admin
ChangesEnvironment=yes

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
; Иконка для Start Menu / Desktop ярлыков. Генерируется build_branding.ps1
; через ImageMagick из packaging/branding/source/apex-logo.png в build/branding/.
Source: "..\..\build\branding\apex-logo.ico"; DestDir: "{app}\assets"; Flags: ignoreversion skipifsourcedoesntexist
; Standalone бандл sensord (Windows-сервис AMX-persistent-loader).
; Это self-contained PyInstaller-EXE (свой Python + pywin32 + LHM DLL'и);
; регистрация сервиса не требует ни venv'а, ни системного Python.
; Кладём в подкаталог {app}\apexcore-sensord\ — install_sensord_bundle.ps1
; и Inno-задача из [Run] ниже работают с этим путём.
Source: "{#SensordDistDir}\*"; DestDir: "{app}\apexcore-sensord"; Flags: recursesubdirs createallsubdirs ignoreversion
; PowerShell-скрипт для постоянной регистрации PawnIO как Windows-сервиса.
Source: "..\..\scripts\install_pawnio_service.ps1"; DestDir: "{app}\scripts"; Flags: ignoreversion
; Production-installer sensord-сервиса (sc.exe-based, использует
; apexcore-sensord.exe из {app}\apexcore-sensord\).
Source: "..\..\scripts\install_sensord_bundle.ps1"; DestDir: "{app}\scripts"; Flags: ignoreversion
; Dev-installer (для editable-install в venv — для разработчиков).
; В production не запускается, кладётся для справки.
Source: "..\..\scripts\install_sensord.ps1"; DestDir: "{app}\scripts"; Flags: ignoreversion
; Бандлим PawnIO_setup.exe из namazso/PawnIO.Setup. Раньше installer
; полагался только на `winget install namazso.PawnIO`, но в профилях
; где winget впервые запрашивает msstore-agreement (нужен RU-код региона)
; шаг падает молча и PawnIO MSI не ставится. С бандлом гарантированно
; ставим из локального файла; winget оставлен как fallback.
; Кладём в {app}\drivers\ (не {tmp}+deleteafterinstall) — нужен для
; `apexcore repair-drivers` если sensord/PawnIO нужно перерегистрировать.
Source: "..\..\build\bundles\PawnIO_setup.exe"; DestDir: "{app}\drivers"; Flags: ignoreversion skipifsourcedoesntexist; Tasks: pawnio
; Бандл smartmontools-installer (NSIS, /S = silent). Без бандла полагались
; на winget, но без явного --source winget шаг застревал на msstore-
; agreement-prompt в новых профилях → engine висел на waituntilterminated.
Source: "..\..\build\bundles\smartmontools.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall ignoreversion skipifsourcedoesntexist; Tasks: smartmontools
; Лицензия и notice о компонентах третьих лиц — обязательная атрибуция (MPL для
; LHM, GPL для smartmontools/stress-ng). Кладутся в корень установки чтобы
; пользователь и аудитор могли проверить условия.
Source: "..\..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\NOTICE.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
; Start Menu — основная точка входа (TUI меню) + быстрые ярлыки на webui/help/info.
; IconFilename использует build/branding/apex-logo.ico (генерится
; build_branding.ps1). Если файла нет — Inno подберёт иконку из exe.
; skipifsourcedoesntexist в [Files] делает иконку опциональной для сборки.
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; \
    Comment: "Интерактивное меню apexcore (TUI)"; \
    IconFilename: "{app}\assets\apex-logo.ico"; \
    WorkingDir: "{app}"
Name: "{group}\{#MyAppName} Web UI"; Filename: "{app}\{#MyAppExeName}"; Parameters: "webui"; \
    Comment: "Запустить локальный Web UI ApexCore в браузере"; \
    IconFilename: "{app}\assets\apex-logo.ico"; \
    WorkingDir: "{app}"
Name: "{group}\{#MyAppName} Info"; Filename: "{cmd}"; Parameters: "/K {app}\{#MyAppExeName} info"; \
    Comment: "Сведения о системе"; \
    IconFilename: "{app}\assets\apex-logo.ico"
Name: "{group}\Удалить {#MyAppName}"; Filename: "{uninstallexe}"

; Desktop shortcut — через task `desktopicon` (default checked).
; {autodesktop} = {commondesktop} при admin, {userdesktop} при non-admin.
; Раньше был {commondesktop} — но Windows 11 кеширует public desktop, и
; иногда ярлык не отображается без refresh. autodesktop надёжнее.
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; \
    Tasks: desktopicon; \
    Comment: "ApexCore — оценка производительности компьютера"; \
    IconFilename: "{app}\assets\apex-logo.ico"; \
    WorkingDir: "{app}"

[Tasks]
; addtopath включён по умолчанию — иначе `apexcore menu` из новой PowerShell
; даёт CommandNotFoundException. БЕЗ флагов = отмечено по умолчанию каждый
; раз. Был `checkedonce` который отмечает только первый раз; при upgrade с
; v0.8.x state Inno в реестре считает что юзер уже выбирал → checkbox
; приходит unchecked → PATH не добавляется. Убрал.
Name: "addtopath"; Description: "Добавить apexcore в системный PATH"; GroupDescription: "Дополнительно:"
; Desktop icon отмечен по умолчанию.
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; GroupDescription: "Дополнительно:"
Name: "pawnio"; Description: "Установить PawnIO + постоянный сервис (драйвер для доступа к MSR/PCI/SuperIO)"; GroupDescription: "Дополнительные источники сенсоров:"
Name: "sensord"; Description: "Установить apexcore_sensord (T° ЦП / Vcore / DIMM / потребление БЕЗ UAC при каждом запуске)"; GroupDescription: "Дополнительные источники сенсоров:"
Name: "smartmontools"; Description: "Установить smartmontools (T° NVMe/SATA-дисков в разделе «Датчики»)"; GroupDescription: "Дополнительные источники сенсоров:"

[Code]
// --- Upgrade поверх предыдущей установки (тот же AppId) ---
// AppId стабилен между версиями; Inno переустанавливает поверх по AppId.
// Если запись о предыдущей установке в реестре указывает на существующий
// каталог, реюзаем его как DefaultDirName (GetExistingDir), чтобы upgrade
// шёл в ту же папку, а не создавал вторую.

function GetLegacyInstallPath(): string;
var
  Path: string;
begin
  Result := '';
  if RegQueryStringValue(
       HKLM,
       'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{C7D9D6F1-8F2E-4B5A-9F3F-77B9B1A1B8AF}_is1',
       'InstallLocation',
       Path
     ) then
    Result := Path;
end;

function GetExistingDir(Default: string): string;
var
  Legacy: string;
begin
  Legacy := GetLegacyInstallPath();
  if (Legacy <> '') and DirExists(Legacy) then begin
    Result := Legacy;
  end else begin
    Result := Default;
  end;
end;

function AddToPath(): Boolean;
var
  Paths: string;
begin
  if not RegQueryStringValue(HKLM, 'SYSTEM\CurrentControlSet\Control\Session Manager\Environment', 'Path', Paths) then
    Paths := '';
  if Pos(LowerCase(ExpandConstant('{app}')), LowerCase(Paths)) > 0 then begin
    Result := True;
    exit;
  end;
  if (Length(Paths) > 0) and (Paths[Length(Paths)] <> ';') then
    Paths := Paths + ';';
  Paths := Paths + ExpandConstant('{app}');
  Result := RegWriteStringValue(HKLM, 'SYSTEM\CurrentControlSet\Control\Session Manager\Environment', 'Path', Paths);
end;

function RemoveFromPath(): Boolean;
var
  Paths: string;
  AppPath: string;
  P: Integer;
begin
  Result := True;
  if not RegQueryStringValue(HKLM, 'SYSTEM\CurrentControlSet\Control\Session Manager\Environment', 'Path', Paths) then
    exit;
  AppPath := ExpandConstant('{app}');
  P := Pos(LowerCase(AppPath), LowerCase(Paths));
  if P = 0 then exit;
  Delete(Paths, P, Length(AppPath));
  StringChangeEx(Paths, ';;', ';', True);
  if (Length(Paths) > 0) and (Paths[1] = ';') then Delete(Paths, 1, 1);
  if (Length(Paths) > 0) and (Paths[Length(Paths)] = ';') then Delete(Paths, Length(Paths), 1);
  Result := RegWriteStringValue(HKLM, 'SYSTEM\CurrentControlSet\Control\Session Manager\Environment', 'Path', Paths);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and IsTaskSelected('addtopath') then
    AddToPath();
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    RemoveFromPath();
end;

[Run]
; ──────── PawnIO (T° ЦП, Vcore, потребление, DIMM-сенсоры) ────────
; LHM v0.9+ использует PawnIO (WHQL-подписанный драйвер от namazso) вместо
; старого WinRing0. Без PawnIO LHM не читает MSR/PCI/Super-I/O →
; CPU-температура/Vcore/Ppt пустые.
;
; Шаг 1: ставим PawnIO_setup.exe из бандла (NSIS, /S = silent). Раньше
; полагались на winget, но в новых профилях winget блокируется msstore-
; agreement-промптом и шаг падает молча.
; Шаг 2: winget-fallback на случай если бандл не сошёлся.
; Шаг 3: install_pawnio_service.ps1 — postoянная регистрация PawnIO
; как Windows-сервиса (start=auto), чтобы apexcore без UAC видел сенсоры.
Filename: "{app}\drivers\PawnIO_setup.exe"; Parameters: "-install -silent"; \
    Tasks: pawnio; \
    Flags: runhidden waituntilterminated skipifdoesntexist; \
    StatusMsg: "Установка PawnIO (T° ЦП / Vcore / DIMM)..."

Filename: "winget.exe"; Parameters: "install --silent --accept-source-agreements --accept-package-agreements --source winget --id namazso.PawnIO"; \
    Tasks: pawnio; \
    Flags: runhidden waituntilterminated skipifdoesntexist; \
    StatusMsg: "PawnIO winget-fallback..."

Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\scripts\install_pawnio_service.ps1"" -NoPrompt"; \
    Tasks: pawnio; \
    Flags: runhidden waituntilterminated; \
    StatusMsg: "Регистрация PawnIO как постоянного сервиса..."

; ──────── apexcore_sensord (UAC-free режим на каждый запуск) ────────
; Без этого шага apexcore при каждом запуске требует UAC чтобы прочитать
; CPU температуру/Vcore/потребление через LHM+PawnIO. С этим шагом
; sensord-сервис (LocalSystem, autostart) держит LHM открытым и публикует
; snapshot всех сенсоров в Global shared memory; apexcore-клиент читает
; оттуда без admin. Один UAC при установке — дальше apexcore работает
; обычным юзером.
;
; install_sensord_bundle.ps1 использует standalone PyInstaller-EXE
; apexcore-sensord.exe из {app}\apexcore-sensord\ (он содержит свой
; embedded Python + pywin32 + LHM-зависимости). Никаких сложных
; pywin32+venv-трюков — sc.exe-based регистрация. Если предыдущий
; apexcore_sensord ещё зарегистрирован — сносит и ставит заново.
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\scripts\install_sensord_bundle.ps1"" -InstallDir ""{app}"" -NoPrompt"; \
    Tasks: sensord; \
    Flags: runhidden waituntilterminated; \
    StatusMsg: "Регистрация apexcore_sensord (UAC-free сенсоры)..."

; ──────── smartmontools (T° дисков) ────────
; Бандленный NSIS-installer smartmontools (1.4 MB). /S = silent.
; Winget убран — без --source winget он застревал на msstore-agreement-
; prompt в новых профилях и engine навсегда висел на waituntilterminated.
Filename: "{tmp}\smartmontools.exe"; Parameters: "/S"; \
    Tasks: smartmontools; \
    Flags: runhidden waituntilterminated skipifdoesntexist; \
    StatusMsg: "Установка smartmontools (T° дисков)..."

; ──────── Postinstall ────────
; Запускаем через cmd /K чтобы TUI окно осталось открытым после exit (иначе
; conhost закроется и пользователь не успеет увидеть результат). Полный путь
; к apexcore.exe гарантирует работу даже если ChangesEnvironment=yes WM_-broadcast
; не дошёл до Explorer'а (например при запуске installer'а из существующей PS).
; Запуск напрямую через {app}\apexcore.exe (без cmd-обёртки) — TUI меню само
; держит окно пока пользователь не нажмёт Esc/Q. Полный путь гарантирует
; работу даже если в текущей сессии Explorer'а PATH не обновился.
Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; \
    Description: "Запустить ApexCore (TUI меню)"; \
    Flags: postinstall nowait skipifsilent unchecked

Filename: "{app}\{#MyAppExeName}"; Parameters: "webui"; WorkingDir: "{app}"; \
    Description: "Запустить Web UI (http://127.0.0.1:8765)"; \
    Flags: postinstall nowait skipifsilent unchecked

Filename: "{app}\{#MyAppExeName}"; Parameters: "info"; WorkingDir: "{app}"; \
    Description: "Показать сведения о системе"; \
    Flags: postinstall nowait skipifsilent unchecked

[UninstallRun]
; ── 1. Снести sensord-сервис ──
; install_sensord_bundle.ps1 -Uninstall теперь использует Remove-ServiceForced
; с bounded timeout 5 сек + kill PID + kill orphan-процессов
; apexcore-sensord.exe. Без этого SCM держал бы apexcore-sensord.exe file
; handle и Inno uninstaller не мог бы удалить папку {app}\apexcore-sensord\
; (классическая ошибка «Папка используется другой программой»).
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\scripts\install_sensord_bundle.ps1"" -InstallDir ""{app}"" -Uninstall"; \
    Flags: runhidden waituntilterminated; \
    RunOnceId: "uninst_sensord"

; ── 2. Kill orphan-процессы apexcore (TUI, webui) ──
; Если пользователь запустил `apexcore webui` или TUI меню и забыл
; закрыть — Inno uninstaller не может удалить apexcore.exe потому что
; файл занят. taskkill /F /IM ловит ВСЕ apexcore.exe (одно имя,
; неважно из какой папки) — это OK потому что после деинсталляции exe
; всё равно не должен оставаться запущенным.
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM apexcore.exe /T"; \
    Flags: runhidden; RunOnceId: "kill_apexcore"
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM apexcore-sensord.exe /T"; \
    Flags: runhidden; RunOnceId: "kill_apexcore_sensord"

; PawnIO-сервис мы не удаляем автоматически — он может быть нужен другим
; приложениям (HWiNFO, LHM standalone и т.п.). Пользователь снимает его
; вручную через install_pawnio_service.ps1 -Uninstall, если хочет.

[UninstallDelete]
; Удалить apexcore-bench/ subdir и temp-файлы которые могут остаться
; от disk-benchmarks (создаются в boot_path\apexcore-bench\, см.
; infrastructure/microbench/disk.py). Также cleanup пользовательских
; данных в %APPDATA%\apexcore — НЕ делаем (там БД с прогонами).
Type: filesandordirs; Name: "{sd}\apexcore-bench"
Type: filesandordirs; Name: "{app}\scripts\__pycache__"
