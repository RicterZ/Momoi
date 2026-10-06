using System;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.Http;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;

namespace Momoi.Desktop;

internal sealed record NapCatSettings(string BotQQ, int SocketPort, string AccessToken, int WebPort, string WebToken);

/// <summary>Own the official Node bundle; never attach to or terminate the user's desktop QQ.</summary>
internal sealed class NapCatHost(string workspace) : IAsyncDisposable
{
    private readonly SemaphoreSlim gate = new(1);
    private Process? process;
    private ProcessJob? job;
    private Task? stdout, stderr;
    private NapCatSettings? settings;
    private bool webReady;
    private readonly object logGate = new();
    private string Data => Path.Combine(workspace, "napcat");
    private string SettingsPath => Path.Combine(Data, "managed.json");
    public bool Running => process is not null && !process.HasExited;
    public string LoginUrl => Running && webReady && settings is not null
        ? $"http://127.0.0.1:{settings.WebPort}/webui?token={Uri.EscapeDataString(settings.WebToken)}"
        : throw new InvalidOperationException("请先在消息渠道设置中启动内置 QQ 客户端。");
    public object Status => new { available = File.Exists(Path.Combine(AppContext.BaseDirectory, "runtime", "napcat", "node.exe")), running = Running, ready = webReady && Running };

    private static int FreePort()
    {
        var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        try { return ((IPEndPoint)listener.LocalEndpoint).Port; }
        finally { listener.Stop(); }
    }

    private static void CheckPort(int port)
    {
        if (port < 1 || port > 65535) throw new InvalidDataException("QQ 组件配置中的端口无效。");
        var listener = new TcpListener(IPAddress.Loopback, port);
        try { listener.Start(); }
        catch (SocketException) { throw new IOException($"本机端口 {port} 已被占用，请停止其他 QQ 组件后重试。"); }
        finally { listener.Stop(); }
    }

    private static string Secret() => Convert.ToHexString(RandomNumberGenerator.GetBytes(32));
    private static void WriteJson(string path, object value)
    {
        string temporary = path + ".tmp-" + Guid.NewGuid().ToString("N");
        try
        {
            File.WriteAllText(temporary, JsonSerializer.Serialize(value, new JsonSerializerOptions { WriteIndented = true }));
            File.Move(temporary, path, overwrite: true);
        }
        finally { File.Delete(temporary); }
    }

    public bool ShouldAutoStart(out string botQQ)
    {
        botQQ = "";
        if (!File.Exists(SettingsPath) || !File.Exists(Path.Combine(workspace, "config.json"))) return false;
        var saved = JsonSerializer.Deserialize<NapCatSettings>(File.ReadAllText(SettingsPath));
        var channel = JsonNode.Parse(File.ReadAllText(Path.Combine(workspace, "config.json")))?["channels"]?["enabled"]?["napcat"];
        if (saved is null || channel is null) return false;
        botQQ = channel["bot_qq"]?.GetValue<string>() ?? "";
        return botQQ == saved.BotQQ && channel["url"]?.GetValue<string>() == $"ws://127.0.0.1:{saved.SocketPort}"
            && channel["access_token"]?.GetValue<string>() == saved.AccessToken;
    }

    public async Task<object> StartAsync(string botQQ, string entry, CancellationToken cancellationToken)
    {
        if (!Regex.IsMatch(botQQ, "^[0-9]{5,20}$")) throw new ArgumentException("请填写机器人 QQ 号码（5–20 位数字，与主人 QQ 区分）。");
        await gate.WaitAsync(cancellationToken);
        bool preparing = false;
        try
        {
            if (Running)
            {
                if (settings?.BotQQ != botQQ) throw new InvalidOperationException("请先停止当前 QQ 客户端，再切换账号。");
                return Connection();
            }
            await StopCoreAsync();
            preparing = true;
            string runtime = Path.Combine(AppContext.BaseDirectory, "runtime", "napcat");
            foreach (string file in new[] { "node.exe", "wrapper.node", "QQNT.dll", "config.json", "package.json", "napcat/napcat.mjs" })
                if (!File.Exists(Path.Combine(runtime, file))) throw new FileNotFoundException("QQ 运行组件不完整，请安装新版 Momoi 安装包。");
            if (!File.Exists(entry)) throw new FileNotFoundException("QQ 启动代码缺失，请更新或重新安装 Momoi。");
            Directory.CreateDirectory(Path.Combine(Data, "config"));
            settings = File.Exists(SettingsPath) ? JsonSerializer.Deserialize<NapCatSettings>(File.ReadAllText(SettingsPath)) : null;
            settings ??= new NapCatSettings(botQQ, FreePort(), Secret(), FreePort(), Secret());
            settings = settings with { BotQQ = botQQ };
            if (settings.SocketPort == settings.WebPort || string.IsNullOrWhiteSpace(settings.AccessToken) || string.IsNullOrWhiteSpace(settings.WebToken))
                throw new InvalidDataException("内置 QQ 组件配置无效，请检查 data/napcat/managed.json。");
            CheckPort(settings.SocketPort);
            CheckPort(settings.WebPort);
            WriteJson(SettingsPath, settings);
            string onebot = Path.Combine(Data, "config", $"onebot11_{botQQ}.json");
            var config = File.Exists(onebot) ? JsonNode.Parse(File.ReadAllText(onebot))!.AsObject() : new JsonObject();
            config["network"] ??= new JsonObject();
            var servers = config["network"]!["websocketServers"] as JsonArray ?? new JsonArray();
            foreach (var server in servers)
                if (server?["enable"]?.GetValue<bool>() == true && server["name"]?.GetValue<string>() != "momoi")
                    throw new InvalidDataException("该账号已有其他 WebSocket 服务配置，请先在 QQ 面板停用后重试。");
            // Preserve disabled external entries; replace only our named connection.
            for (int index = servers.Count - 1; index >= 0; index--)
                if (servers[index]?["name"]?.GetValue<string>() == "momoi") servers.RemoveAt(index);
            servers.Add(JsonSerializer.SerializeToNode(new {
                name = "momoi", enable = true, host = "127.0.0.1", port = settings.SocketPort,
                token = settings.AccessToken, messagePostFormat = "array", reportSelfMessage = false, enableForcePushEvent = true,
            }));
            config["network"]!["websocketServers"] = servers;
            WriteJson(onebot, config);
            // The account-specific file above is authoritative; do not rewrite other accounts.
            string webuiPath = Path.Combine(Data, "config", "webui.json");
            var webui = File.Exists(webuiPath) ? JsonNode.Parse(File.ReadAllText(webuiPath))!.AsObject() : new JsonObject();
            webui["host"] = "127.0.0.1";
            webui["port"] = settings.WebPort;
            webui["token"] = settings.WebToken;
            webui["autoLoginAccount"] = botQQ;
            webui["disableWebUI"] = false;
            WriteJson(webuiPath, webui);
            var info = new ProcessStartInfo(Path.Combine(runtime, "node.exe")) {
                UseShellExecute = false, CreateNoWindow = true, WorkingDirectory = runtime,
                RedirectStandardOutput = true, RedirectStandardError = true,
                StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8,
            };
            info.ArgumentList.Add("--require");
            info.ArgumentList.Add(entry);
            info.ArgumentList.Add(Path.Combine(runtime, "index.js"));
            info.ArgumentList.Add("-q"); info.ArgumentList.Add(botQQ);
            info.Environment["MOMOI_NAPCAT_RUNTIME"] = runtime;
            info.Environment["MOMOI_QQ_DATA"] = Path.Combine(Data, "qq");
            info.Environment["NAPCAT_WORKDIR"] = Data;
            info.Environment["NAPCAT_WEBUI_SECRET_KEY"] = settings.WebToken;
            info.Environment["NAPCAT_WEBUI_PREFERRED_PORT"] = settings.WebPort.ToString();
            // DLL search includes the shipped native libraries, not a machine-wide Node installation.
            info.Environment["PATH"] = runtime + Path.PathSeparator + info.Environment["PATH"];
            string nativeMarker = Path.Combine(Data, "qq", ".native-data-ready");
            Directory.CreateDirectory(Path.GetDirectoryName(nativeMarker)!);
            File.Delete(nativeMarker);
            job = new ProcessJob();
            process = Process.Start(info) ?? throw new InvalidOperationException("无法启动内置 QQ 客户端。");
            job.Assign(process);
            // Capture early native failures too; redact tokens and login URLs.
            stdout = DrainAsync(process.StandardOutput);
            stderr = DrainAsync(process.StandardError);
            using var client = new HttpClient(new HttpClientHandler { UseProxy = false }) { Timeout = TimeSpan.FromSeconds(2) };
            var deadline = DateTime.UtcNow.AddSeconds(60);
            while (DateTime.UtcNow < deadline)
            {
                cancellationToken.ThrowIfCancellationRequested();
                if (!Running) throw new IOException($"QQ 客户端启动失败（退出码 {process!.ExitCode}），请查看 {Path.Combine(Data, "logs")}。");
                try
                {
                    using var response = await client.GetAsync($"http://127.0.0.1:{settings.WebPort}/", cancellationToken);
                    if (response.IsSuccessStatusCode && File.Exists(nativeMarker)) { webReady = true; return Connection(); }
                }
                catch (Exception error) when (error is HttpRequestException || error is TaskCanceledException && !cancellationToken.IsCancellationRequested) { }
                await Task.Delay(250, cancellationToken);
            }
            throw new TimeoutException($"QQ 登录面板启动超时，请查看 {Path.Combine(Data, "logs")}。");
        }
        catch { if (preparing) await StopCoreAsync(); throw; }
        finally { gate.Release(); }
    }

    private object Connection() => new { running = Running, ready = webReady, url = $"ws://127.0.0.1:{settings!.SocketPort}", access_token = settings.AccessToken };
    private async Task DrainAsync(StreamReader reader)
    {
        string logs = Path.Combine(Data, "logs");
        Directory.CreateDirectory(logs);
        while (await reader.ReadLineAsync() is { } line)
        {
            if (settings is not null)
                line = line.Replace(settings.AccessToken, "[redacted]").Replace(settings.WebToken, "[redacted]");
            line = Regex.Replace(line, @"https?://\S+", "[url redacted]");
            lock (logGate) File.AppendAllText(Path.Combine(logs, "native-startup.log"), line + Environment.NewLine, Encoding.UTF8);
        }
    }
    private async Task StopCoreAsync()
    {
        webReady = false;
        if (Running) process!.Kill(entireProcessTree: true);
        job?.Dispose(); job = null;
        if (process is not null) { await process.WaitForExitAsync(); process.Dispose(); process = null; }
        if (stdout is not null) await stdout;
        if (stderr is not null) await stderr;
        stdout = stderr = null;
    }
    public async Task StopAsync()
    {
        await gate.WaitAsync();
        try { await StopCoreAsync(); }
        finally { gate.Release(); }
    }
    public async ValueTask DisposeAsync() => await StopAsync();
}
