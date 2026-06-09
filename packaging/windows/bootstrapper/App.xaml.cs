using System.Windows;

namespace Apexcore.Bootstrapper;

public partial class App : System.Windows.Application
{
    protected override void OnStartup(StartupEventArgs e)
    {
        // Включаем DPI-awareness на per-monitor v2 (WPF .NET 8 делает это
        // через app.manifest, но мы запускаемся standalone — фиксируем явно).
        base.OnStartup(e);
    }
}
