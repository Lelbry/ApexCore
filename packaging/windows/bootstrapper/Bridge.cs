using System;
using System.IO;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.Wpf;

namespace Apexcore.Bootstrapper;

/// <summary>
/// JS&lt;-&gt;C# мост. Принимает window.chrome.webview.postMessage от UI,
/// маршрутизирует по action, отвечает через PostWebMessageAsString.
/// </summary>
public sealed class Bridge
{
    private readonly Window _window;
    private readonly WebView2 _web;
    private readonly Installer _installer;

    public Bridge(Window window, WebView2 web)
    {
        _window = window;
        _web = web;
        _installer = new Installer(this);
    }

    public void OnWebMessage(object? sender, CoreWebView2WebMessageReceivedEventArgs e)
    {
        BridgeMessage? msg = null;
        try
        {
            var json = e.TryGetWebMessageAsString();
            msg = JsonSerializer.Deserialize<BridgeMessage>(json, JsonOpts);
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"[bridge] parse error: {ex}");
            return;
        }
        if (msg == null) return;

        switch (msg.Action)
        {
            case "windowAction":
                HandleWindowAction(msg.Value);
                break;
            case "startInstall":
                _ = _installer.RunAsync(msg.Id, msg.Options);
                break;
            case "browse":
                HandleBrowse(msg.Id, msg.Default);
                break;
            case "probeDisk":
                HandleProbeDisk(msg.Id, msg.Default);
                break;
            case "probeGpu":
                _ = HandleProbeGpu(msg.Id);
                break;
            case "probeEnvironment":
                _ = HandleProbeEnvironment(msg.Id);
                break;
            case "finish":
                HandleFinish(msg.Id, msg.Options);
                break;
            case "persistTheme":
                // Сохранять не требуется — theme.js сам пишет localStorage.
                break;
            default:
                Reply(msg.Id, error: $"unknown action: {msg.Action}");
                break;
        }
    }

    public void PostEvent(object payload)
    {
        var json = JsonSerializer.Serialize(payload, JsonOpts);
        _window.Dispatcher.Invoke(() => _web.CoreWebView2.PostWebMessageAsString(json));
    }

    public void Reply(int? id, object? data = null, string? error = null)
    {
        var payload = new
        {
            reply = id,
            data,
            error,
        };
        PostEvent(payload);
    }

    private void HandleWindowAction(string? action)
    {
        _window.Dispatcher.Invoke(() =>
        {
            switch (action)
            {
                case "minimize": _window.WindowState = WindowState.Minimized; break;
                case "maximize":
                    _window.WindowState = _window.WindowState == WindowState.Maximized
                        ? WindowState.Normal : WindowState.Maximized;
                    break;
                case "close":
                    if (System.Windows.MessageBox.Show(_window, "Прервать установку?", "ApexCore Setup",
                            MessageBoxButton.YesNo, MessageBoxImage.Question) == MessageBoxResult.Yes)
                    {
                        System.Windows.Application.Current.Shutdown(1);
                    }
                    break;
            }
        });
    }

    private void HandleBrowse(int? replyId, string? defaultPath)
    {
        _window.Dispatcher.Invoke(() =>
        {
            using var dlg = new FolderBrowserDialog
            {
                SelectedPath = defaultPath ?? @"C:\Program Files\ApexCore",
                Description = "Выберите папку установки ApexCore",
                ShowNewFolderButton = true,
            };
            var res = dlg.ShowDialog();
            Reply(replyId, data: res == DialogResult.OK ? dlg.SelectedPath : null);
        });
    }

    private void HandleProbeDisk(int? replyId, string? path)
    {
        try
        {
            var probePath = !string.IsNullOrWhiteSpace(path) ? path : @"C:\";
            var root = Path.GetPathRoot(probePath);
            if (string.IsNullOrEmpty(root)) root = @"C:\";
            var info = new DriveInfo(root);
            if (!info.IsReady)
            {
                Reply(replyId, error: $"диск {root} не готов");
                return;
            }
            Reply(replyId, data: new
            {
                root,
                fs = info.DriveFormat,
                total_bytes = info.TotalSize,
                available_bytes = info.AvailableFreeSpace,
                total_gb = Math.Round(info.TotalSize / 1073741824.0, 1),
                available_gb = Math.Round(info.AvailableFreeSpace / 1073741824.0, 1),
            });
        }
        catch (Exception ex)
        {
            Reply(replyId, error: ex.Message);
        }
    }

    private async Task HandleProbeGpu(int? replyId)
    {
        try
        {
            var data = await Task.Run(GpuProbe.Run);
            Reply(replyId, data);
        }
        catch (Exception ex)
        {
            Reply(replyId, error: ex.Message);
        }
    }

    private async Task HandleProbeEnvironment(int? replyId)
    {
        try
        {
            var data = await Task.Run(EnvironmentProbe.Run);
            Reply(replyId, data);
        }
        catch (Exception ex)
        {
            Reply(replyId, error: ex.Message);
        }
    }

    private void HandleFinish(int? replyId, FinishOptions? opts)
    {
        opts ??= new FinishOptions();
        try
        {
            if (opts.LaunchWebUI)
            {
                var apexcore = Path.Combine(Installer.GetInstallDir(), "apexcore.exe");
                if (File.Exists(apexcore))
                {
                    System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo(apexcore, "webui")
                    {
                        UseShellExecute = true,
                    });
                }
            }
            if (opts.LaunchCLI)
            {
                // Запускаем apexcore.exe через PowerShell -NoExit чтобы TUI окно
                // осталось открытым. ОТ АДМИНИСТРАТОРА (Verb=runas → UAC):
                // bootstrapper сам не elevated (Inno делает self-elevation),
                // поэтому без runas консоль стартует без прав, и ПЕРВЫЙ прогон
                // Winsat (winsat dwm — графика + игровая графика/D3D) приходит
                // NA. С elevation первый Winsat считается полностью.
                // КРИТИЧНО: полный путь к exe — PATH в свежей PS-сессии может
                // ещё не подхватить broadcast нового PATH. Пробелы/кириллица в
                // пути — экранируем кавычками. Если пользователь отменит UAC,
                // Process.Start бросит исключение → ловится ниже (CLI не запустится).
                var apexcoreCli = Path.Combine(Installer.GetInstallDir(), "apexcore.exe");
                if (File.Exists(apexcoreCli))
                {
                    System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo("powershell.exe",
                        $"-NoExit -Command & '{apexcoreCli}'")
                    {
                        UseShellExecute = true,
                        WorkingDirectory = Installer.GetInstallDir(),
                        Verb = "runas",
                    });
                }
            }
            if (opts.OpenReadme)
            {
                System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo(
                    "https://github.com/Lelbry/benchmark_by_lelbry/blob/dev/README.md")
                {
                    UseShellExecute = true,
                });
            }
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"[bridge] finish error: {ex}");
        }
        Reply(replyId, data: new { ok = true });

        _window.Dispatcher.Invoke(() => System.Windows.Application.Current.Shutdown(0));
    }

    internal static readonly JsonSerializerOptions JsonOpts = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    };
}

public sealed class BridgeMessage
{
    [JsonPropertyName("id")] public int? Id { get; set; }
    [JsonPropertyName("action")] public string Action { get; set; } = "";
    [JsonPropertyName("value")] public string? Value { get; set; }
    [JsonPropertyName("default")] public string? Default { get; set; }
    [JsonPropertyName("options")] public FinishOptions? Options { get; set; }
}

public sealed class FinishOptions
{
    [JsonPropertyName("path")] public string? Path { get; set; }
    [JsonPropertyName("tasks")] public string[]? Tasks { get; set; }
    [JsonPropertyName("acceptLicense")] public bool? AcceptLicense { get; set; }
    [JsonPropertyName("launch_webui")] public bool LaunchWebUI { get; set; } = true;
    [JsonPropertyName("launch_cli")] public bool LaunchCLI { get; set; }
    [JsonPropertyName("desktop_shortcut")] public bool DesktopShortcut { get; set; }
    [JsonPropertyName("open_readme")] public bool OpenReadme { get; set; }
    [JsonPropertyName("cancel")] public bool Cancel { get; set; }
}
