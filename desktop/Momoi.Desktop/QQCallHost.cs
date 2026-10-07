using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Net.Http;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Threading;
using System.Threading.Tasks;

namespace Momoi.Desktop;

/// <summary>Own voice children; normal QQ messaging remains usable if voice fails.</summary>
internal sealed class QQCallHost : IAsyncDisposable
{
    private readonly string workspace, app, bridge, data, token, tokenFile, nativeUrl, hostUrl, mediaUrl;
    private readonly CancellationTokenSource stop = new();
    private readonly HttpClient http = new(new HttpClientHandler { UseProxy = false }) { Timeout = TimeSpan.FromSeconds(8) };
    private readonly List<Process> children = new();
    private readonly List<Task> readers = new();
    private readonly object logGate = new();
    private ProcessJob? job;
    private Process? media;
    private Task? monitor;
    private string phase = "disabled", error = "";
    private DateTime nextAttempt;
    private bool loginRequested;
    private int transientFailures;
    public QQCallHost(string workspace, string napcatEntry)
    {
        this.workspace = workspace;
        app = Path.GetFullPath(Path.Combine(Path.GetDirectoryName(napcatEntry)!, "..", ".."));
        bridge = Path.Combine(app, "qq_call_bridge");
        data = Path.Combine(workspace, "qq-call");
        using var settings = JsonDocument.Parse(File.ReadAllText(QQCallSettings.Prepare(workspace)));
        var root = settings.RootElement;
        token = root.GetProperty("bridge_token").GetString()!;
        tokenFile = root.GetProperty("token_file").GetString()!;
        mediaUrl = root.GetProperty("bridge_url").GetString()!;
        nativeUrl = root.GetProperty("native_url").GetString()!;
        hostUrl = root.GetProperty("host_url").GetString()!;
        http.DefaultRequestHeaders.Authorization = new("Bearer", token);
    }
    public bool Available => File.Exists(Path.Combine(bridge, "napcat-plugin", "index.mjs"));
    public void ConfigureNapCat(ProcessStartInfo info)
    {
        if (!Available) return; // Older releases still support ordinary messaging.
        info.Environment["MOMOI_QQ_CALL_PLUGIN"] = Path.Combine(bridge, "napcat-plugin", "index.mjs");
        info.Environment["MAIBOT_QQ_CALL_BRIDGE_TOKEN_FILE"] = tokenFile;
        info.Environment["MAIBOT_QQ_CALL_BRIDGE_HOST"] = "127.0.0.1";
        info.Environment["MAIBOT_QQ_CALL_BRIDGE_PORT"] = new Uri(nativeUrl).Port.ToString();
        info.Environment["MAIBOT_QQ_CALL_AV_HOST_HOST"] = "127.0.0.1";
        info.Environment["MAIBOT_QQ_CALL_AV_HOST_PORT"] = new Uri(hostUrl).Port.ToString();
        info.Environment["NAPCAT_DISABLE_MULTI_PROCESS"] = "1";
    }
    public void StartMonitoring(Func<bool> qqRunning) => monitor = MonitorAsync(qqRunning);
    private bool Enabled()
    {
        var config = JsonNode.Parse(File.ReadAllText(Path.Combine(workspace, "config.json")));
        return config?["channels"]?["enabled"]?["napcat"]?["voice_call"]?["enabled"]?.GetValue<bool>() == true;
    }
    private void Status(string state, string message = "")
    {
        phase = state; error = message;
        string path = Path.Combine(data, "status.json"), temporary = path + ".tmp";
        File.WriteAllText(temporary, JsonSerializer.Serialize(new { phase, error, updated_at = DateTimeOffset.UtcNow.ToUnixTimeSeconds() }));
        File.Move(temporary, path, overwrite: true);
    }
    private async Task MonitorAsync(Func<bool> qqRunning)
    {
        try
        {
            while (!stop.IsCancellationRequested)
            {
                try
                {
                    if (!qqRunning() || !Enabled())
                    {
                        if (job is not null) await StopChildrenAsync();
                        Status(qqRunning() ? "disabled" : "waiting_qq", qqRunning() ? "" : "请先启动内置 QQ 并登录");
                    }
                    else if (job is null && DateTime.UtcNow >= nextAttempt)
                    {
                        await StartChildrenAsync(stop.Token);
                    }
                    else if (job is not null)
                    {
                        if (media is null || media.HasExited) throw new IOException("语音媒体进程已退出，请查看 qq-call/logs。");
                        if (!loginRequested)
                        {
                            try
                            {
                                using var relogin = await http.PostAsync(nativeUrl + "/v1/avsdk/relogin", null, stop.Token);
                                loginRequested = relogin.IsSuccessStatusCode;
                            }
                            catch (HttpRequestException) { }
                            catch (TaskCanceledException) when (!stop.IsCancellationRequested) { }
                        }
                        using var response = await http.GetAsync(mediaUrl + "/v1/status", stop.Token);
                        response.EnsureSuccessStatusCode();
                        using var value = JsonDocument.Parse(await response.Content.ReadAsStringAsync(stop.Token));
                        bool ready = value.RootElement.TryGetProperty("ready", out var flag) && flag.GetBoolean();
                        Status(ready ? "ready" : "waiting", ready ? "" : value.RootElement.GetProperty("error").GetString() ?? "语音服务未就绪");
                        // Detect a lost AVSDK child even if the media endpoint is still alive.
                        using var host = await http.GetAsync(hostUrl + "/v1/status", stop.Token);
                        host.EnsureSuccessStatusCode();
                        transientFailures = 0;
                    }
                }
                catch (OperationCanceledException) when (stop.IsCancellationRequested) { break; }
                catch (Exception failure)
                {
                    string directory = Path.Combine(data, "logs");
                    Directory.CreateDirectory(directory);
                    lock (logGate) File.AppendAllText(Path.Combine(directory, "service.log"),
                        $"{DateTimeOffset.Now:O} {failure.GetType().Name}: {failure.Message.Replace(token, "[redacted]")}{Environment.NewLine}", Encoding.UTF8);
                    bool transient = failure is HttpRequestException or TaskCanceledException;
                    if (transient && job is not null && media is not null && !media.HasExited && ++transientFailures < 3)
                    {
                        Status("waiting", "语音服务暂未响应，正在重试连接…");
                    }
                    else
                    {
                        await StopChildrenAsync();
                        transientFailures = 0;
                        Status("failed", transient ? "语音服务连接失败，请查看 data/qq-call/logs 中的启动日志。" : failure.Message.Replace(token, "[redacted]"));
                        nextAttempt = DateTime.UtcNow.AddSeconds(10);
                    }
                }
                await Task.Delay(1000, stop.Token);
            }
        }
        catch (OperationCanceledException) when (stop.IsCancellationRequested) { }
        finally { await StopChildrenAsync(); Status("stopped"); }
    }
    private Process Launch(ProcessStartInfo info, string name)
    {
        info.UseShellExecute = false; info.CreateNoWindow = true;
        info.RedirectStandardOutput = true; info.RedirectStandardError = true;
        info.StandardOutputEncoding = Encoding.UTF8; info.StandardErrorEncoding = Encoding.UTF8;
        info.WorkingDirectory = data;
        var process = Process.Start(info) ?? throw new IOException("无法启动 " + name);
        children.Add(process);
        job!.Assign(process);
        readers.Add(DrainAsync(process.StandardOutput, name));
        readers.Add(DrainAsync(process.StandardError, name));
        return process;
    }
    private async Task DrainAsync(StreamReader reader, string name)
    {
        string directory = Path.Combine(data, "logs"); Directory.CreateDirectory(directory);
        while (await reader.ReadLineAsync() is { } line)
        {
            line = line.Replace(token, "[redacted]");
            lock (logGate) File.AppendAllText(Path.Combine(directory, name + ".log"), line + Environment.NewLine, Encoding.UTF8);
        }
    }
    private async Task StartChildrenAsync(CancellationToken cancellationToken)
    {
        string native = Path.Combine(AppContext.BaseDirectory, "runtime", "qq-call");
        string python = Path.Combine(AppContext.BaseDirectory, "runtime", "python", "python.exe");
        string hostScript = Path.Combine(bridge, "windows", "start-av-host.ps1");
        string mediaEntry = Path.Combine(app, "momoi", "desktop", "call_entry.py");
        foreach (string file in new[] { Path.Combine(native, "qq", "Files", "QQ.exe"), hostScript, python, mediaEntry })
            if (!File.Exists(file)) throw new FileNotFoundException("语音运行组件缺失，请安装新版完整安装包。", file);
        if (Process.GetCurrentProcess().SessionId == 0) throw new IOException("语音电话需要在 Windows 桌面会话中启动。");
        Status("starting", "正在启动语音宿主");
        job = new ProcessJob();
        var host = new ProcessStartInfo("powershell.exe");
        foreach (string argument in new[] { "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", hostScript,
            "-NativeRoot", native, "-DataRoot", data, "-BridgeRoot", bridge,
            "-HostPort", new Uri(hostUrl).Port.ToString(), "-BridgePort", new Uri(nativeUrl).Port.ToString() }) host.ArgumentList.Add(argument);
        Launch(host, "av-host");
        var deadline = DateTime.UtcNow.AddSeconds(40);
        bool hostReady = false;
        while (DateTime.UtcNow < deadline)
        {
            cancellationToken.ThrowIfCancellationRequested();
            try
            {
                using var response = await http.GetAsync(hostUrl + "/v1/status", cancellationToken);
                response.EnsureSuccessStatusCode();
                using var value = JsonDocument.Parse(await response.Content.ReadAsStringAsync(cancellationToken));
                if (value.RootElement.GetProperty("data").GetProperty("ready").GetBoolean()) { hostReady = true; break; }
            }
            catch (Exception failure) when (failure is HttpRequestException || failure is TaskCanceledException && !cancellationToken.IsCancellationRequested) { }
            await Task.Delay(250, cancellationToken);
        }
        if (!hostReady) throw new TimeoutException("AVSDK 宿主启动超时，请查看 qq-call/logs/av-host.log。");
        var mediaInfo = new ProcessStartInfo(python);
        foreach (string argument in new[] { "-I", "-B", "-X", "utf8", mediaEntry }) mediaInfo.ArgumentList.Add(argument);
        mediaInfo.Environment["QQ_CALL_RUNTIME"] = data;
        mediaInfo.Environment["QQ_CALL_TOKEN_FILE"] = tokenFile;
        mediaInfo.Environment["QQ_CALL_WINDOWS_BRIDGE"] = bridge;
        mediaInfo.Environment["QQ_CALL_NATIVE_URL"] = nativeUrl;
        mediaInfo.Environment["QQ_CALL_HOST_URL"] = hostUrl;
        mediaInfo.Environment["QQ_CALL_PORT"] = new Uri(mediaUrl).Port.ToString();
        mediaInfo.Environment["PYTHONUTF8"] = "1";
        media = Launch(mediaInfo, "media");
        // Allow the HTTP listener to come up before monitoring its readiness.
        deadline = DateTime.UtcNow.AddSeconds(15);
        while (DateTime.UtcNow < deadline)
        {
            if (media.HasExited) throw new IOException("语音媒体服务启动失败，请查看 qq-call/logs/media.log。");
            try
            {
                using var response = await http.GetAsync(mediaUrl + "/healthz", cancellationToken);
                if (response.IsSuccessStatusCode) { Status("waiting", "等待 QQ 登录及虚拟音频设备就绪"); return; }
            }
            catch (Exception failure) when (failure is HttpRequestException || failure is TaskCanceledException && !cancellationToken.IsCancellationRequested) { }
            await Task.Delay(250, cancellationToken);
        }
        throw new TimeoutException("语音媒体服务启动超时，请查看 qq-call/logs/media.log。");
    }
    private async Task StopChildrenAsync()
    {
        if (job is null) return;
        // Restore scoped virtual audio settings before closing the process job.
        if (media is not null && !media.HasExited)
        {
            try
            {
                using var response = await http.PostAsync(mediaUrl + "/v1/shutdown", null);
                await media.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(8));
            }
            catch (Exception failure) when (failure is HttpRequestException || failure is TaskCanceledException || failure is TimeoutException) { }
        }
        job.Dispose(); job = null;
        foreach (var child in children) { await child.WaitForExitAsync(); child.Dispose(); }
        children.Clear(); media = null; loginRequested = false;
        await Task.WhenAll(readers); readers.Clear();
    }
    public async ValueTask DisposeAsync()
    {
        stop.Cancel();
        if (monitor is not null) await monitor;
        else await StopChildrenAsync();
        http.Dispose(); stop.Dispose();
    }
}
