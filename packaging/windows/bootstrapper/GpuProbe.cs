using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using System.Text.Json;

namespace Apexcore.Bootstrapper;

/// <summary>
/// Read-only probe: какие видеокарты установлены (NVIDIA / AMD / Intel).
/// Не ставит драйверы — только информирует пользователя баннером на
/// Welcome / Components-шаге wizard'а.
/// </summary>
public static class GpuProbe
{
    public record Result(string? Nvidia, string? Amd, string? Intel, string? Reason);

    public static Result Run()
    {
        try
        {
            var psi = new ProcessStartInfo("powershell.exe",
                "-NoProfile -NonInteractive -Command " +
                "\"Get-CimInstance Win32_VideoController | " +
                "Select-Object Name, AdapterCompatibility | ConvertTo-Json -Compress\"")
            {
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                UseShellExecute = false,
                CreateNoWindow = true,
                WindowStyle = ProcessWindowStyle.Hidden,
            };
            using var p = Process.Start(psi);
            if (p == null) return new(null, null, null, "PowerShell start failed");
            var stdout = p.StandardOutput.ReadToEnd();
            p.WaitForExit(8000);

            string? nvidia = null, amd = null, intel = null;
            if (!string.IsNullOrWhiteSpace(stdout))
            {
                using var doc = JsonDocument.Parse(stdout);
                var entries = new List<JsonElement>();
                if (doc.RootElement.ValueKind == JsonValueKind.Array)
                    entries.AddRange(doc.RootElement.EnumerateArray());
                else
                    entries.Add(doc.RootElement);

                foreach (var entry in entries)
                {
                    var name = entry.TryGetProperty("Name", out var n) ? n.GetString() ?? "" : "";
                    var vendor = (entry.TryGetProperty("AdapterCompatibility", out var v) ? v.GetString() ?? "" : "")
                        .ToLowerInvariant();
                    if (vendor.Contains("nvidia") && nvidia == null) nvidia = name;
                    else if ((vendor.Contains("amd") || vendor.Contains("advanced micro")) && amd == null) amd = name;
                    else if (vendor.Contains("intel") && intel == null) intel = name;
                }
            }
            string? reason = null;
            if (nvidia == null && amd == null && intel == null)
                reason = "Дискретная / интегрированная GPU не обнаружена.";

            return new(nvidia, amd, intel, reason);
        }
        catch (Exception ex)
        {
            return new(null, null, null, $"GPU probe ошибка: {ex.Message}");
        }
    }
}

public static class EnvironmentProbe
{
    public record Result(
        string Platform,
        string OsVersion,
        string Architecture,
        bool Wmi,
        bool PowerShell,
        bool Winget,
        bool WebView2Runtime);

    public static Result Run()
    {
        var osv = Environment.OSVersion.VersionString;
        var arch = Environment.Is64BitOperatingSystem ? "x64" : "x86";
        return new Result(
            Platform: "windows",
            OsVersion: osv,
            Architecture: arch,
            Wmi: HasCommand("powershell"),
            PowerShell: HasCommand("powershell"),
            Winget: HasCommand("winget"),
            WebView2Runtime: true /* мы уже работаем в WebView2 — runtime есть */
        );
    }

    private static bool HasCommand(string exe)
    {
        try
        {
            var psi = new ProcessStartInfo("where.exe", exe)
            {
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                UseShellExecute = false,
                CreateNoWindow = true,
            };
            using var p = Process.Start(psi);
            if (p == null) return false;
            p.WaitForExit(2000);
            return p.ExitCode == 0;
        }
        catch { return false; }
    }
}
