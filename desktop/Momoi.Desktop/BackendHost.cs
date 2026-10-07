using System;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using Momoi.Update;

namespace Momoi.Desktop;

internal sealed record BackendReady(string Url, string Token);

internal sealed class BackendHost : IAsyncDisposable
{
    private Process? process;
    private bool processStarted;
    private ProcessJob? job;
    private Task? outputReader, errorReader;
    private readonly TaskCompletionSource<BackendReady> ready = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private readonly string logPath;
    private readonly object logGate = new();
    public event Action<string>? StartupProgress;
    public Task Completion => process?.WaitForExitAsync() ?? Task.CompletedTask;

    public BackendHost(string workspace)
    {
        string logs = Path.Combine(workspace, "logs");
        Directory.CreateDirectory(logs);
        logPath = Path.Combine(logs, $"desktop-{DateTime.Now:yyyyMMdd-HHmmss}-{Guid.NewGuid():N}.log");
    }

    private static int AvailablePort()
    {
        var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        try { return ((IPEndPoint)listener.LocalEndpoint).Port; }
        finally { listener.Stop(); }
    }

    public async Task<BackendReady> StartAsync(string workspace, CodeRelease release, CancellationToken cancellationToken)
    {
        int dashboardPort = AvailablePort();
        int embeddingPort;
        do { embeddingPort = AvailablePort(); } while (embeddingPort == dashboardPort);
        string backend = Path.Combine(AppContext.BaseDirectory, "runtime", "python", "python.exe");
        string entry = Path.Combine(release.Directory, "app", "backend_entry.py");
        string model = Path.Combine(AppContext.BaseDirectory, "models", "bge-small-zh-v1.5");
        if (!File.Exists(backend) || !File.Exists(entry) || !File.Exists(Path.Combine(model, "model_optimized.onnx")))
            throw new FileNotFoundException("安装文件不完整，请重新安装 Momoi。");
        var info = new ProcessStartInfo(backend)
        {
            UseShellExecute = false, CreateNoWindow = true,
            RedirectStandardInput = true, RedirectStandardOutput = true, RedirectStandardError = true,
            StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8,
            WorkingDirectory = workspace,
        };
        foreach (string argument in new[] { "-I", "-u", "-B", "-X", "utf8", entry, "--install-dir", AppContext.BaseDirectory, "--workspace", workspace, "--model-path", model,
            "--dashboard-port", dashboardPort.ToString(), "--embedding-port", embeddingPort.ToString() })
            info.ArgumentList.Add(argument);
        info.Environment["MOMOI_QQ_CALL_MANAGED"] = QQCallSettings.Prepare(workspace);
        if (TimeZoneInfo.TryConvertWindowsIdToIanaId(TimeZoneInfo.Local.Id, out string? localZone))
            info.Environment["MOMOI_DESKTOP_TIMEZONE"] = localZone;
        info.Environment["PYTHONUTF8"] = "1";
        info.Environment["HF_HUB_OFFLINE"] = "1";
        info.Environment["TRANSFORMERS_OFFLINE"] = "1";
        info.Environment["NO_COLOR"] = "1";
        process = new Process { StartInfo = info };
        job = new ProcessJob();
        if (!process.Start()) throw new InvalidOperationException("无法启动后台程序。");
        processStarted = true;
        job.Assign(process);
        outputReader = ReadOutputAsync(process.StandardOutput, $"http://127.0.0.1:{dashboardPort}");
        errorReader = ReadErrorsAsync(process.StandardError);
        _ = WatchExitAsync();
        try { return await ready.Task.WaitAsync(TimeSpan.FromMinutes(3), cancellationToken); }
        catch (TimeoutException) { throw new TimeoutException($"启动超时，请查看日志：{logPath}"); }
    }

    private async Task ReadOutputAsync(StreamReader stream, string expectedUrl)
    {
        while (await stream.ReadLineAsync() is { } line)
        {
            // Never write the ready payload / authentication token to the log.
            try
            {
                using var document = JsonDocument.Parse(line);
                var root = document.RootElement;
                if (root.TryGetProperty("event", out var progressEvent) && progressEvent.GetString() == "startup_progress")
                {
                    if (root.TryGetProperty("stage", out var stage))
                    {
                        string? detail = stage.GetString() switch
                        {
                            "model" => "2/5 · 加载本地模型",
                            "workspace" => "3/5 · 初始化配置",
                            "services" => "4/5 · 启动服务",
                            _ => null,
                        };
                        if (detail is not null) StartupProgress?.Invoke(detail);
                    }
                    continue;
                }
                if (root.TryGetProperty("event", out var eventName) && eventName.GetString() == "ready")
                {
                    string url = root.GetProperty("url").GetString()!;
                    string token = root.GetProperty("token").GetString()!;
                    if (url != expectedUrl || string.IsNullOrWhiteSpace(token))
                        { ready.TrySetException(new InvalidOperationException("后台返回了无效的面板地址。")); continue; }
                    ready.TrySetResult(new BackendReady(url, token));
                    continue;
                }
            }
            catch (JsonException) { }
            Log(line, "stdout");
        }
    }

    private async Task ReadErrorsAsync(StreamReader stream)
    {
        while (await stream.ReadLineAsync() is { } line) Log(line, "stderr");
    }

    private async Task WatchExitAsync()
    {
        await process!.WaitForExitAsync();
        ready.TrySetException(new InvalidOperationException($"后台启动失败（退出码 {process.ExitCode}），请查看日志：{logPath}"));
    }

    private void Log(string line, string stream)
    {
        lock (logGate) File.AppendAllText(logPath, line + Environment.NewLine, Encoding.UTF8);
        LiveLog.Write("backend", stream, line);
    }

    public static async Task MaintenanceAsync(string workspace, CodeRelease release, string snapshot, bool restore)
    {
        var info = new ProcessStartInfo(Path.Combine(AppContext.BaseDirectory, "runtime", "python", "python.exe"))
        {
            UseShellExecute = false, CreateNoWindow = true,
            RedirectStandardOutput = true, RedirectStandardError = true,
            StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8,
            WorkingDirectory = workspace,
        };
        foreach (string argument in new[] { "-I", "-B", "-X", "utf8", Path.Combine(release.Directory, "app", "backend_entry.py"),
            "--workspace", workspace, restore ? "--restore" : "--snapshot", snapshot }) info.ArgumentList.Add(argument);
        using var child = Process.Start(info) ?? throw new InvalidOperationException("无法启动更新备份程序。");
        using var ownership = new ProcessJob();
        ownership.Assign(child);
        var output = child.StandardOutput.ReadToEndAsync();
        var errors = child.StandardError.ReadToEndAsync();
        try { await child.WaitForExitAsync().WaitAsync(TimeSpan.FromMinutes(2)); }
        catch (TimeoutException) { ownership.Dispose(); await child.WaitForExitAsync(); throw; }
        await output;
        string error = await errors;
        if (child.ExitCode != 0) throw new InvalidOperationException("更新备份或恢复失败：" + error);
    }

    public async ValueTask DisposeAsync()
    {
        if (process is not null && processStarted)
        {
            if (!process.HasExited)
            {
                try { await process.StandardInput.WriteLineAsync("stop"); await process.StandardInput.FlushAsync(); }
                catch (IOException) { }
                process.StandardInput.Close();
                try { await process.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(15)); }
                catch (TimeoutException) { job?.Dispose(); if (!process.HasExited) process.Kill(entireProcessTree: true); await process.WaitForExitAsync(); }
            }
            if (outputReader is not null) await outputReader;
            if (errorReader is not null) await errorReader;
            process.Dispose();
            process = null;
        }
        process?.Dispose();
        process = null;
        job?.Dispose();
        job = null;
    }
}
