using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;

namespace Apexcore.Bootstrapper;

/// <summary>
/// Запускает silent Inno Setup engine (apexcore-engine.exe) и парсит
/// прогресс из его лога. Отчитывается через Bridge.PostEvent({event:progress}).
/// </summary>
public sealed class Installer
{
    private readonly Bridge _bridge;
    private static string? _lastInstallDir;

    // Прогресс делят два потока: TailLog обновляет _filePct/_step по Inno-логу,
    // heartbeat раз в секунду эмитит max(время-easing, _filePct). Бар движется
    // даже когда Inno молчит (antivirus задерживает лог; [Run]-шаги PawnIO/
    // sensord/smartmontools идут без file-copy сигналов).
    private volatile int _filePct = 5;
    private volatile string _step = "Подготовка";

    public Installer(Bridge bridge) { _bridge = bridge; }

    public static string GetInstallDir() =>
        _lastInstallDir ?? @"C:\Program Files\ApexCore";

    public async Task RunAsync(int? replyId, FinishOptions? opts)
    {
        opts ??= new FinishOptions();
        var dir = string.IsNullOrWhiteSpace(opts.Path) ? @"C:\Program Files\ApexCore" : opts.Path;
        _lastInstallDir = dir;
        _filePct = 5;
        _step = "Подготовка";
        var tasks = (opts.Tasks != null && opts.Tasks.Length > 0)
            ? string.Join(",", opts.Tasks)
            : "pawnio,sensord,smartmontools";

        var enginePath = Path.Combine(AppContext.BaseDirectory, "engine", "apexcore-engine.exe");
        if (!File.Exists(enginePath))
        {
            _bridge.PostEvent(new {
                @event = "progress", percent = 100, step = "Ошибка: engine не найден",
                log_line = $"apexcore-engine.exe отсутствует: {enginePath}",
                state = "error",
            });
            _bridge.Reply(replyId, error: $"engine not found: {enginePath}");
            return;
        }

        var logFile = Path.Combine(Path.GetTempPath(), $"apexcore-setup-{Guid.NewGuid():N}.log");
        var args = string.Join(" ", new[] {
            // /VERYSILENT — UI рисует наш wizard, отдельное Inno-окно нам не
            // нужно. (Раньше казалось что /VERYSILENT вешает engine; реальной
            // причиной был Read-Host в install_pawnio_service.ps1 — fixed.)
            "/VERYSILENT", "/SUPPRESSMSGBOXES", "/SP-", "/NORESTART",
            $"/LOG=\"{logFile}\"",
            $"/DIR=\"{dir}\"",
            $"/TASKS=\"{tasks}\"",
        });

        _bridge.PostEvent(new {
            @event = "progress", percent = 2, step = "Запуск engine'а",
            log_line = $"apexcore-engine.exe {args}",
        });

        var startTs = DateTime.UtcNow;
        var proc = new Process
        {
            StartInfo = new ProcessStartInfo(enginePath, args)
            {
                UseShellExecute = false,
                CreateNoWindow = true,
                WindowStyle = ProcessWindowStyle.Hidden,
            },
        };
        try { proc.Start(); }
        catch (Exception ex)
        {
            _bridge.PostEvent(new {
                @event = "progress", percent = 100, step = "Не удалось запустить engine",
                log_line = ex.Message, state = "error",
            });
            _bridge.Reply(replyId, error: ex.Message);
            return;
        }

        // Tail логи параллельно с ожиданием процесса.
        var tailCts = new CancellationTokenSource();
        var tailTask = Task.Run(() => TailLog(logFile, startTs, tailCts.Token));
        // Heartbeat: каждую секунду пингуем UI текущим elapsed, даже если log
        // пуст или ещё не создан. Иначе при медленном antivirus-скане
        // (PawnIO setup elevated child) tail возвращает раньше времени и UI
        // замерзает на 5% / "00:07" до самого WaitForExit.
        var heartbeatCts = new CancellationTokenSource();
        var heartbeatTask = Task.Run(async () => {
            while (!heartbeatCts.IsCancellationRequested)
            {
                try { await Task.Delay(1000, heartbeatCts.Token); } catch { return; }
                var elapsed = (DateTime.UtcNow - startTs).TotalSeconds;
                // Время-зависимый «мягкий» прогресс: асимптотически к ~90% за
                // ~25 с. Двигает бар плавно, даже когда Inno не пишет Dest-строк.
                // Реальный процент по файлам (_filePct) перекрывает, если выше.
                int timePct = 5 + (int)(85.0 * (1.0 - Math.Exp(-elapsed / 25.0)));
                int pct = Math.Clamp(Math.Max(timePct, _filePct), 5, 97);
                _bridge.PostEvent(new {
                    @event = "progress",
                    percent = pct,
                    step = _step,
                    elapsed_sec = elapsed,
                });
            }
        });

        await Task.Run(proc.WaitForExit);
        var exit = proc.ExitCode;
        heartbeatCts.Cancel();
        tailCts.Cancel();
        try { await heartbeatTask; } catch { /* swallow */ }
        try { await tailTask; } catch { /* swallow */ }

        // Сохранить engine log в {app}\logs\install.log — иначе временный TEMP-файл
        // удаляется и пользователь не видит что пошло не так в [Run] цепочке.
        var preservedLog = TryPreserveLog(logFile, dir);

        if (exit != 0)
        {
            _bridge.PostEvent(new {
                @event = "progress", percent = 100, step = $"Engine завершился с кодом {exit}",
                log_line = $"Лог: {preservedLog ?? logFile}", state = "error",
            });
            _bridge.Reply(replyId, error: $"engine exit code {exit}");
            return;
        }
        _bridge.PostEvent(new {
            @event = "progress", percent = 100, step = "Готово", state = "done",
            log_line = preservedLog != null ? $"Лог сохранён: {preservedLog}" : "Engine завершился успешно",
            elapsed_sec = (DateTime.UtcNow - startTs).TotalSeconds,
        });
        _bridge.Reply(replyId, data: new { ok = true });
    }

    private static string? TryPreserveLog(string tempLog, string installDir)
    {
        if (!File.Exists(tempLog) || string.IsNullOrWhiteSpace(installDir)) return null;
        try
        {
            var logsDir = Path.Combine(installDir, "logs");
            Directory.CreateDirectory(logsDir);
            var dst = Path.Combine(logsDir, "install.log");
            File.Copy(tempLog, dst, overwrite: true);
            return dst;
        }
        catch
        {
            // ACL / нет места / read-only — оставляем как есть
            return null;
        }
    }

    /// <summary>
    /// Парсит Inno Setup log — это plain-text. Извлекаем процент,
    /// текущий шаг и последнюю строку из формата:
    ///   "Source Filename: ..."
    ///   "Dest filename: ..."
    ///   "Installing the application..."
    ///   "Status changed to '...'"
    /// </summary>
    private async Task TailLog(string logFile, DateTime startTs, CancellationToken ct)
    {
        // Дожидаемся появления файла. Раньше было 5s, но antivirus-сканер
        // elevated PawnIO_setup.exe может задерживать запись Inno-лога на
        // 10-20 секунд — поэтому 60 секунд таймаут.
        for (int i = 0; i < 600 && !File.Exists(logFile); i++)
        {
            if (ct.IsCancellationRequested) return;
            await Task.Delay(100, ct);
        }
        if (!File.Exists(logFile)) return;

        using var fs = new FileStream(logFile, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
        using var rdr = new StreamReader(fs, Encoding.UTF8);

        var totalFiles = -1;
        var copiedFiles = 0;
        var rxDest = new Regex(@"^Dest filename:\s*(.+)$", RegexOptions.IgnoreCase);
        var rxStatus = new Regex(@"^.*Status:\s*(.+)$", RegexOptions.IgnoreCase);
        var rxInstalling = new Regex(@"^Installing the application", RegexOptions.IgnoreCase);

        while (!ct.IsCancellationRequested)
        {
            var line = await rdr.ReadLineAsync();
            if (line == null)
            {
                await Task.Delay(150, ct);
                continue;
            }

            string? logLine = null;
            bool stepChanged = false;
            if (rxInstalling.IsMatch(line)) { _step = "Распаковка файлов"; stepChanged = true; }
            var m = rxDest.Match(line);
            if (m.Success)
            {
                copiedFiles++;
                if (totalFiles < 0)
                {
                    // Эстимэйт по эвристике: ~140 файлов в среднем apexcore-bundle.
                    totalFiles = 140;
                }
                _filePct = Math.Clamp(5 + (int)(85.0 * copiedFiles / totalFiles), 5, 95);
                var fname = Path.GetFileName(m.Groups[1].Value);
                logLine = $"copy {fname}";
                if (fname.Contains("PawnIO", StringComparison.OrdinalIgnoreCase))
                    _step = "Распаковка PawnIO драйвера";
                else if (fname.Contains("sensord", StringComparison.OrdinalIgnoreCase))
                    _step = "Регистрация apexcore_sensord";
                else if (fname.Contains("LibreHardwareMonitor", StringComparison.OrdinalIgnoreCase))
                    _step = "LibreHardwareMonitor DLL";
            }
            var s = rxStatus.Match(line);
            if (s.Success) { _step = s.Groups[1].Value.Trim(); stepChanged = true; }

            // Процентом управляет heartbeat (монотонно/плавно) — тут шлём только
            // обновление шага и строку лога, чтобы не дёргать бар назад.
            if (logLine != null || stepChanged)
            {
                _bridge.PostEvent(new {
                    @event = "progress",
                    step = _step,
                    log_line = logLine,
                    elapsed_sec = (DateTime.UtcNow - startTs).TotalSeconds,
                });
            }
        }
    }
}
