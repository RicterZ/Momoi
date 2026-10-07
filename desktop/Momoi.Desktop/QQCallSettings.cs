using System;
using System.IO;
using System.Diagnostics;
using System.Collections.Generic;
using System.Text.RegularExpressions;
using System.Net;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text.Json;

namespace Momoi.Desktop;

// One endpoint allocation per shell lifetime; backend updates reuse the same connection.
internal static class QQCallSettings
{
    private static readonly object gate = new();
    private static string? prepared;
    public static string Prepare(string workspace)
    {
        lock (gate) return PrepareCore(workspace);
    }
    private static string PrepareCore(string workspace)
    {
        string directory = Path.Combine(workspace, "qq-call");
        Directory.CreateDirectory(directory);
        string path = Path.Combine(directory, "managed.json");
        if (prepared == path && File.Exists(path)) return path;
        string token;
        if (File.Exists(path))
        {
            using var previous = JsonDocument.Parse(File.ReadAllText(path));
            token = previous.RootElement.GetProperty("bridge_token").GetString() ?? "";
            if (token.Length != 64 || !System.Text.RegularExpressions.Regex.IsMatch(token, "^[0-9A-Fa-f]{64}$"))
                throw new InvalidDataException("语音电话认证文件损坏，请检查 data/qq-call/managed.json。");
        }
        else token = Convert.ToHexString(RandomNumberGenerator.GetBytes(32));
        var ports = new HashSet<int>();
        var excluded = new List<(int Start, int End)>();
        foreach (string family in new[] { "ipv4", "ipv6" })
        {
            var info = new ProcessStartInfo("netsh.exe") { UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true };
            foreach (string argument in new[] { "interface", family, "show", "excludedportrange", "protocol=tcp" }) info.ArgumentList.Add(argument);
            using var process = Process.Start(info) ?? throw new IOException("无法检查系统保留端口。");
            var output = process.StandardOutput.ReadToEndAsync();
            // A cold Windows runner can take longer than five seconds to load netsh.
            // Keep checking excluded ranges: a successful .NET bind alone does not
            // establish that the later native listener can use a reserved port.
            if (!process.WaitForExit(30000))
            {
                process.Kill(entireProcessTree: true);
                process.WaitForExit();
                throw new IOException($"系统保留端口检查超时（netsh {family}，30 秒），请重试。");
            }
            if (process.ExitCode != 0) throw new IOException("无法检查系统保留端口，请检查网络配置。");
            foreach (Match match in Regex.Matches(output.GetAwaiter().GetResult(), @"(?m)^\s*(\d+)\s+(\d+)\s*\*?\s*$"))
                excluded.Add((int.Parse(match.Groups[1].Value), int.Parse(match.Groups[2].Value)));
        }
        int Allocate()
        {
            for (int attempt = 0; attempt < 256; attempt++)
            {
                int port = RandomNumberGenerator.GetInt32(49152, 65536);
                if (ports.Contains(port) || excluded.Exists(range => port >= range.Start && port <= range.End)) continue;
                var listener = new TcpListener(IPAddress.Loopback, port);
                listener.Server.ExclusiveAddressUse = true;
                try { listener.Start(); ports.Add(port); return port; }
                catch (SocketException) { }
                finally { listener.Stop(); }
            }
            throw new IOException("无法分配语音服务端口，请检查系统网络限制。");
        }
        int mediaPort = Allocate(), nativePort = Allocate(), hostPort = Allocate();
        string tokenFile = Path.Combine(directory, "control.token");
        File.WriteAllText(tokenFile, token);
        string temporary = path + ".tmp-" + Guid.NewGuid().ToString("N");
        try
        {
            File.WriteAllText(temporary, JsonSerializer.Serialize(new {
                bridge_url = $"http://127.0.0.1:{mediaPort}", bridge_token = token,
                native_url = $"http://127.0.0.1:{nativePort}", host_url = $"http://127.0.0.1:{hostPort}", token_file = tokenFile
            }));
            File.Move(temporary, path, overwrite: true);
        }
        finally { File.Delete(temporary); }
        prepared = path;
        return path;
    }
}
