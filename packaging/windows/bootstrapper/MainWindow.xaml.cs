using System;
using System.IO;
using System.Text.Json;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Input;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.Wpf;

namespace Apexcore.Bootstrapper;

public partial class MainWindow : Window
{
    private Bridge? _bridge;

    public MainWindow()
    {
        InitializeComponent();
        Loaded += MainWindow_Loaded;
        // Перетаскивание borderless окна через WPF native MouseDown.
        // (Внутри WebView2 -webkit-app-region:drag тоже работает на свежих
        // runtime, это второй слой.)
        MouseLeftButtonDown += (_, e) =>
        {
            if (e.ButtonState == MouseButtonState.Pressed && e.OriginalSource == this)
                DragMove();
        };
    }

    private async void MainWindow_Loaded(object sender, RoutedEventArgs e)
    {
        try
        {
            // CoreWebView2 user-data в temp — чтобы не оставлять следов в %APPDATA%
            // после удаления инсталлера (но если bootstrapper устанавливается
            // permanently — этот путь должен быть в %LOCALAPPDATA%).
            var userDataDir = Path.Combine(Path.GetTempPath(), "apexcore-setup-wv2");
            Directory.CreateDirectory(userDataDir);
            // На чистых Win10 / LTSC / N WebView2 Runtime может отсутствовать —
            // без него CreateAsync бросит исключение и UI не откроется. Если
            // runtime нет, а забандленный бутстрапер есть — ставим тихо.
            await EnsureWebView2RuntimeAsync();
            var env = await CoreWebView2Environment.CreateAsync(null, userDataDir);
            await WebView.EnsureCoreWebView2Async(env);

            // Маппим Resources/wwwroot/ как виртуальный хост.
            var wwwroot = Path.Combine(AppContext.BaseDirectory, "Resources", "wwwroot");
            WebView.CoreWebView2.SetVirtualHostNameToFolderMapping(
                "apexcore-setup.localhost",
                wwwroot,
                CoreWebView2HostResourceAccessKind.Allow);

            // Bridge — приёмник postMessage от JS.
            _bridge = new Bridge(this, WebView);
            WebView.CoreWebView2.WebMessageReceived += _bridge.OnWebMessage;

            // Указываем JS, что мы в WebView2-режиме (а не в браузере под FastAPI).
            WebView.CoreWebView2.AddScriptToExecuteOnDocumentCreatedAsync(
                "window.IS_WEBVIEW2 = true; " +
                $"window.__APEXCORE_VERSION__ = {JsonSerializer.Serialize(BuildInfo.Version)};");

            // Старт
            WebView.CoreWebView2.Navigate("https://apexcore-setup.localhost/index.html");
        }
        catch (Exception ex)
        {
            System.Windows.MessageBox.Show(
                "Не удалось инициализировать WebView2.\n\n" +
                "Установите Microsoft Edge WebView2 Runtime:\n" +
                "https://developer.microsoft.com/en-us/microsoft-edge/webview2/\n\n" +
                $"Подробности: {ex.Message}",
                "ApexCore Setup",
                MessageBoxButton.OK,
                MessageBoxImage.Error);
            System.Windows.Application.Current.Shutdown(1);
        }
    }

    /// <summary>Есть ли установленный WebView2 Runtime (Evergreen или fixed).</summary>
    private static bool IsWebView2RuntimeAvailable()
    {
        try
        {
            var v = CoreWebView2Environment.GetAvailableBrowserVersionString(null);
            return !string.IsNullOrEmpty(v);
        }
        catch
        {
            return false;
        }
    }

    /// <summary>
    /// Если WebView2 Runtime отсутствует — тихо ставит его забандленным
    /// Evergreen-бутстрапером (MicrosoftEdgeWebview2Setup.exe). Без admin
    /// ставится per-user. Если бандла нет — no-op (CreateAsync затем бросит,
    /// и пользователь увидит ручную инструкцию в catch'е MainWindow_Loaded).
    /// </summary>
    private async Task EnsureWebView2RuntimeAsync()
    {
        if (IsWebView2RuntimeAvailable())
            return;
        var setup = Path.Combine(AppContext.BaseDirectory, "MicrosoftEdgeWebview2Setup.exe");
        if (!File.Exists(setup))
            return;
        try
        {
            // /silent /install: per-user без admin, machine-wide с admin.
            var psi = new System.Diagnostics.ProcessStartInfo(setup, "/silent /install")
            {
                UseShellExecute = false,
                CreateNoWindow = true,
            };
            var proc = System.Diagnostics.Process.Start(psi);
            if (proc != null)
                await Task.Run(() => proc.WaitForExit(180_000));
        }
        catch
        {
            // Не получилось — CreateAsync ниже бросит, покажем ручную инструкцию.
        }
    }
}

internal static class BuildInfo
{
    public static string Version =>
        typeof(BuildInfo).Assembly.GetName().Version?.ToString(3) ?? "0.0.0";
}
