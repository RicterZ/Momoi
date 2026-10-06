using System;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text.Json;

namespace Momoi.Desktop;

// Connection metadata only. CallHost must bind this endpoint before reporting ready.
internal static class QQCallSettings
{
    public static string Prepare(string workspace)
    {
        string directory = Path.Combine(workspace, "qq-call");
        Directory.CreateDirectory(directory);
        string path = Path.Combine(directory, "managed.json");
        string token;
        if (File.Exists(path))
        {
            using var previous = JsonDocument.Parse(File.ReadAllText(path));
            token = previous.RootElement.GetProperty("bridge_token").GetString() ?? "";
            if (token.Length != 64 || !System.Text.RegularExpressions.Regex.IsMatch(token, "^[0-9A-Fa-f]{64}$"))
                throw new InvalidDataException("语音电话认证文件损坏，请检查 data/qq-call/managed.json。");
        }
        else token = Convert.ToHexString(RandomNumberGenerator.GetBytes(32));
        var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        int port;
        try { port = ((IPEndPoint)listener.LocalEndpoint).Port; }
        finally { listener.Stop(); }
        string temporary = path + ".tmp-" + Guid.NewGuid().ToString("N");
        try
        {
            File.WriteAllText(temporary, JsonSerializer.Serialize(new {
                bridge_url = $"http://127.0.0.1:{port}", bridge_token = token
            }));
            File.Move(temporary, path, overwrite: true);
        }
        finally { File.Delete(temporary); }
        return path;
    }
}
