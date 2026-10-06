using System;
using System.IO;
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
        var ports = new System.Collections.Generic.HashSet<int>();
        int Allocate()
        {
            int port;
            do
            {
                var listener = new TcpListener(IPAddress.Loopback, 0);
                listener.Start();
                try { port = ((IPEndPoint)listener.LocalEndpoint).Port; }
                finally { listener.Stop(); }
            } while (!ports.Add(port));
            return port;
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
