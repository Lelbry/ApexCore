# ApexCore Bootstrapper (.NET 8 + WebView2)

Финальный пользовательский `apexcore-setup-X.X.X.exe` — это **именно этот**
проект. Он:

1. Открывает borderless WPF-окно 960×680.
2. Хостит `WebView2`, который рендерит `Resources/wwwroot/index.html` —
   копия `src/apexcore/interfaces/webui/static/setup/`.
3. Принимает JS-команды через `window.chrome.webview.postMessage`:
   - `windowAction` (minimize / maximize / close)
   - `browse` (диалог выбора папки)
   - `probeGpu` / `probeEnvironment` (read-only WMI / where.exe)
   - `startInstall` — запускает silent `apexcore-engine.exe` (Inno Setup),
     парсит лог, ретранслирует прогресс обратно в JS.
   - `finish` — запускает `apexcore webui` / открывает README / закрывает окно.

## Сборка

Требуется .NET 8 SDK + WebView2 SDK (NuGet тянется автоматически).

```powershell
# из корня проекта
pwsh -File scripts/sync_version.ps1      # build/version.txt
pwsh -File scripts/build_branding.ps1    # build/branding/apex-logo.ico
iscc packaging/windows/installer.iss     # dist/engine/apexcore-engine.exe (silent)
Copy-Item -Recurse -Force `
    src/apexcore/interfaces/webui/static/setup/* `
    packaging/windows/bootstrapper/Resources/wwwroot/
$env:APEXCORE_VERSION = Get-Content build/version.txt
dotnet publish packaging/windows/bootstrapper/Bootstrapper.csproj `
    -c Release -r win-x64 `
    -o dist/installer
```

Финальный артефакт: `dist/installer/apexcore-setup.exe` (~50–80 МБ self-contained).

## WebView2 Runtime

- На Windows 11 предустановлен.
- На Windows 10 нужен Evergreen Runtime (~2 МБ MSI):
  https://developer.microsoft.com/en-us/microsoft-edge/webview2/

Если runtime отсутствует — bootstrapper покажет MessageBox с ссылкой
на установку и завершится с exit code 1.

## Архитектура моста

JS отправляет:
```js
window.chrome.webview.postMessage(JSON.stringify({
    id: 42, action: 'startInstall',
    options: { path: 'C:\\Program Files\\ApexCore', tasks: ['pawnio'] }
}));
```

C# принимает в `Bridge.OnWebMessage`, разбирает `BridgeMessage`, маршрутизирует.
Ответ:
```csharp
_bridge.Reply(42, data: new { ok = true });
// → window.chrome.webview.postMessage("{\"reply\":42,\"data\":{\"ok\":true}}")
```

Progress events (без reply, push from C#):
```csharp
_bridge.PostEvent(new { @event = "progress", percent = 64, step = "..." });
```

JS-side handles в `bridge.js::makeWebView2Bridge` (см. dispatchProgress).
